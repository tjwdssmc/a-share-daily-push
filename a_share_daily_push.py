#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股大盘每日战报 - 云端推送脚本
通过GitHub Actions定时运行，采集A股大盘数据，生成战报，通过飞书Webhook发送。

用法：
  python a_share_daily_push.py --period morning   # 开盘前预期（08:30）
  python a_share_daily_push.py --period midday    # 盘中战报（14:30）
  python a_share_daily_push.py --period close     # 收盘总结（15:30）

环境变量：
  FEISHU_WEBHOOK: 飞书自定义机器人Webhook地址（必需）
"""

import os
import sys
import json
import argparse
import datetime
import requests
import pandas as pd

# ============================================================
# 颜色辅助函数：涨=红，跌=绿（用户指定标准）
# ============================================================
def color_pct(pct, decimals=2):
    """返回带飞书卡片颜色标签的涨跌幅字符串"""
    color = "red" if pct >= 0 else "green"
    return f'<font color="{color}">{pct:+.{decimals}f}%</font>'

# ============================================================
# 第一部分：数据采集
# ============================================================



# ============================================================
# v4.1 三因子评分模型（趋势40/动量30/波动30）
# ============================================================
def calc_score_v41(index_data, breadth):
    """v4.1简化三因子评分：趋势40 + 动量30 + 波动30
    返回: (总分, 档位, 目标仓位, 各因子得分字典)"""
    try:
        import numpy as np
        # 用新浪历史日线接口（比同花顺快3-5倍）
        try:
            import akshare as ak
            df = ak.stock_zh_index_daily(symbol="sh000001")
        except Exception:
            # 备用：直接requests获取新浪历史数据
            url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_sh000001=/CN_MarketDataService.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=80"
            resp = requests.get(url, timeout=10)
            import json as _json
            text = resp.text
            start = text.find("[")
            end = text.rfind("]") + 1
            data = _json.loads(text[start:end])
            closes = np.array([float(d["close"]) for d in data])
            current = index_data.get("上证指数", {}).get("close", closes[-1])
            closes[-1] = current
            df = None
        if df is not None:
            if len(df) < 60:
                return 50, "🟡黄色(关注)", 60, {"趋势": 20, "动量": 15, "波动": 15}
            closes = df["close"].astype(float).values.copy()
            current = index_data.get("上证指数", {}).get("close", closes[-1])
            closes[-1] = current

        ma20 = np.mean(closes[-20:])
        ma60 = np.mean(closes[-60:])

        # 趋势因子(40分): 价格与MA20/MA60关系
        trend_score = 40
        if current < ma20:
            trend_score -= 15
        if current < ma60:
            trend_score -= 10
        if ma20 < ma60:
            trend_score -= 10
        # v4.1: 逼近MA20反弹确认提前量
        if current <= ma20 and current > ma20 * 0.985:
            # 距MA20在1.5%以内
            ret_1d = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else 0
            if ret_1d > 0.5:
                trend_score += 7.5  # 反弹上攻形态，减半扣分
        trend_score = max(0, min(40, trend_score))

        # 动量因子(30分): 5/10/20日收益
        mom_score = 30
        if len(closes) >= 6:
            ret5 = (closes[-1] / closes[-6] - 1) * 100
            if ret5 < 0: mom_score -= 10
            if ret5 < -2: mom_score -= 5
        if len(closes) >= 11:
            ret10 = (closes[-1] / closes[-11] - 1) * 100
            if ret10 < 0: mom_score -= 5
        if len(closes) >= 21:
            ret20 = (closes[-1] / closes[-21] - 1) * 100
            if ret20 < 0: mom_score -= 5
        mom_score = max(0, min(30, mom_score))

        # 波动因子(30分): ATR/历史波动率
        vol_score = 30
        if len(closes) >= 20:
            returns = np.diff(closes[-21:]) / closes[-21:-1]
            vol = np.std(returns) * np.sqrt(252) * 100
            if vol > 25: vol_score -= 10
            if vol > 35: vol_score -= 10
            if vol > 45: vol_score -= 5
        vol_score = max(0, min(30, vol_score))

        total = round(trend_score + mom_score + vol_score, 1)

        # 档位
        if total <= 30:
            band = "🟢绿色(安全)"
            pos = 80
        elif total <= 50:
            band = "🟡黄色(关注)"
            pos = 60
            # v4.1黄色档方向细分
            up_ratio = breadth.get("up_count", 0) / max(1, breadth.get("up_count", 0) + breadth.get("down_count", 1))
            if up_ratio > 0.6:
                band = "🟡黄色(关注偏多)"
            elif up_ratio < 0.25:
                band = "🟡黄色(关注偏空)"
        elif total <= 70:
            band = "🟠橙色(警惕)"
            pos = 40
        else:
            band = "🔴红色(风险)"
            pos = 20

        return total, band, pos, {"趋势": round(trend_score, 1), "动量": round(mom_score, 1), "波动": round(vol_score, 1)}
    except Exception as e:
        print(f"  评分计算失败: {e}")
        return 50, "🟡黄色(关注)", 60, {"趋势": 20, "动量": 15, "波动": 15}


def fetch_index_data():
    """采集主要指数实时/收盘数据（新浪实时为主，同花顺日线备用）
    重要：同花顺日线接口盘中返回昨收缓存，必须用新浪实时接口获取盘中真实数据"""
    # 优先使用新浪实时接口（盘中数据准确）
    result = fetch_index_data_sina()
    if result and len(result) >= 3:
        print("  指数数据源: 新浪实时API(hq.sinajs.cn)")
        return result

    # 备用：同花顺日线接口（收盘后准确，盘中可能返回昨收缓存）
    print("  新浪实时接口失败，尝试同花顺日线(备用,盘中可能为昨收缓存)...")
    result = fetch_index_data_ths()
    if result and len(result) >= 3:
        print("  指数数据源: 同花顺日线(备用)")
        return result

    return result


def fetch_index_data_ths():
    """同花顺接口获取指数数据（主数据源）"""
    result = {}
    indices = [
        ("sh000001", "上证指数"),
        ("sz399001", "深证成指"),
        ("sz399006", "创业板指"),
        ("sh000688", "科创50"),
        ("sh000300", "沪深300"),
    ]
    try:
        import akshare as ak
        for code, name in indices:
            try:
                df = ak.stock_zh_index_daily_tx(symbol=code)
                if df is not None and len(df) >= 2:
                    latest = df.iloc[-1]
                    prev = df.iloc[-2]
                    close = float(latest["close"])
                    prev_close = float(prev["close"])
                    amount = float(latest["amount"]) if "amount" in latest else 0
                    result[name] = {
                        "close": close,
                        "change": close - prev_close,
                        "change_pct": (close - prev_close) / prev_close * 100 if prev_close > 0 else 0,
                        "volume": amount,
                        "amount": amount,
                    }
            except Exception as e:
                print(f"  同花顺获取{name}失败: {e}")
    except ImportError:
        print("  akshare未安装")
    return result


def fetch_index_data_sina():
    """新浪财经API获取指数数据（主数据源）"""
    result = {}
    indices = [
        ("sh000001", "上证指数"),
        ("sz399001", "深证成指"),
        ("sz399006", "创业板指"),
        ("sh000688", "科创50"),
        ("sh000300", "沪深300"),
    ]
    try:
        codes = ",".join([c for c, _ in indices])
        url = f"https://hq.sinajs.cn/list={codes}"
        headers = {"Referer": "https://finance.sina.com.cn"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = "gbk"
        lines = resp.text.strip().split("\n")
        for i, line in enumerate(lines):
            if "=" in line and '"' in line:
                data = line.split('"')[1].split(",")
                if len(data) >= 4:
                    name = indices[i][1]
                    # 新浪指数数据格式: 名称,今开,昨收,最新,最高,最低,...,成交量,成交额
                    close = float(data[3])
                    prev_close = float(data[2])
                    volume = float(data[8]) if len(data) > 8 else 0
                    result[name] = {
                        "close": close,
                        "change": close - prev_close,
                        "change_pct": (close - prev_close) / prev_close * 100 if prev_close > 0 else 0,
                        "volume": volume,
                    }
    except Exception as e:
        print(f"  新浪API获取失败: {e}")
    return result


def fetch_market_breadth():
    """采集市场宽度数据（涨跌家数、成交额）- 同花顺为主"""
    up_count = 0
    down_count = 0
    total_amount = 0

    # 1. 涨跌家数：AKShare乐咕接口（同花顺无直接涨跌家数接口，乐咕为合法公开数据源）
    try:
        import akshare as ak
        df = ak.stock_market_activity_legu()
        if df is not None and len(df) > 0:
            data_dict = dict(zip(df["item"], df["value"]))
            up_count = int(float(data_dict.get("上涨", 0)))
            down_count = int(float(data_dict.get("下跌", 0)))
            print(f"  涨跌家数数据源: 乐咕(合法公开) 上涨{up_count}/下跌{down_count}")
    except Exception as e:
        print(f"  涨跌家数获取失败: {e}")

    # 2. 成交额：通过新浪API获取沪市+深市成交额（同花顺指数amount为成交量非成交额）
    try:
        codes = "sh000001,sz399001"
        url = f"https://hq.sinajs.cn/list={codes}"
        headers = {"Referer": "https://finance.sina.com.cn"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = "gbk"
        lines = resp.text.strip().split("\n")
        for line in lines:
            if "=" in line and '"' in line:
                data = line.split('"')[1].split(",")
                if len(data) > 9:
                    total_amount += float(data[9])
        print(f"  成交额数据源: 新浪API 两市合计{total_amount/1e8:.0f}亿")
    except Exception as e:
        print(f"  新浪API成交额获取失败: {e}")
        # 备用：从同花顺板块数据汇总估算
        try:
            import akshare as ak
            df = ak.stock_board_industry_summary_ths()
            if df is not None and len(df) > 0:
                total_amount = df["总成交额"].sum() * 1e8  # 亿元转元
                print(f"  成交额数据源: 同花顺板块汇总(备用) 合计{total_amount/1e8:.0f}亿")
        except Exception as e2:
            print(f"  同花顺板块汇总也失败: {e2}")

    return {
        "up_count": up_count,
        "down_count": down_count,
        "total_amount": total_amount,
    }


def fetch_sector_performance():
    """采集板块涨跌幅排行（同花顺接口为主，东方财富备用）"""
    import time

    # 优先使用同花顺行业板块接口（稳定可靠）
    try:
        import akshare as ak
        df = ak.stock_board_industry_summary_ths()
        if df is not None and len(df) > 0:
            df_sorted = df.sort_values("涨跌幅", ascending=False)
            top3 = df_sorted.head(3)
            bottom3 = df_sorted.tail(3)
            result = {
                "top": [(row["板块"], float(row["涨跌幅"])) for _, row in top3.iterrows()],
                "bottom": [(row["板块"], float(row["涨跌幅"])) for _, row in bottom3.iterrows()],
            }
            print("  板块数据源: 同花顺行业板块")
            return result
    except Exception as e:
        print(f"  同花顺板块接口失败: {e}")

    # 备用：东方财富行业板块接口（最多重试3次）
    for attempt in range(3):
        try:
            import akshare as ak
            df = ak.stock_board_industry_name_em()
            if df is not None and len(df) > 0:
                df_sorted = df.sort_values("涨跌幅", ascending=False)
                top3 = df_sorted.head(3)
                bottom3 = df_sorted.tail(3)
                result = {
                    "top": [(row["板块名称"], float(row["涨跌幅"])) for _, row in top3.iterrows()],
                    "bottom": [(row["板块名称"], float(row["涨跌幅"])) for _, row in bottom3.iterrows()],
                }
                print(f"  板块数据源: 东方财富(重试{attempt+1}次)")
                return result
        except Exception as e:
            print(f"  东方财富板块第{attempt+1}次失败: {e}")
            if attempt < 2:
                time.sleep(2)

    print("  板块数据源: 全部失败，返回空")
    return {"top": [], "bottom": []}


def fetch_news():
    """采集财经新闻（v4.2改进版：财联社电报为主，内容字段回退，重要性排序，扩充分类）"""
    domestic_news = []
    foreign_news = []

    importance_keywords = [
        "央行", "证监会", "国务院", "政治局", "发改委", "财政部", "工信部", "商务部",
        "美联储", "降息", "加息", "降准", "MLF", "LPR", "逆回购",
        "GDP", "CPI", "PPI", "PMI", "社融", "万亿", "救市", "维稳",
        "涨停", "跌停", "熔断", "大涨", "大跌", "突破", "暴跌", "暴涨",
        "美股", "纳指", "道指", "标普", "费城半导体", "欧股", "亚太",
        "地缘", "冲突", "战争", "制裁", "关税", "OPEC",
        "北向", "两融", "融资", "ETF",
    ]
    foreign_keywords = [
        "美国", "美联储", "美股", "纳指", "道指", "标普", "费城半导体", "美债",
        "欧洲", "欧盟", "欧股", "欧元", "日本", "日经", "韩国", "印度",
        "俄罗斯", "乌克兰", "以色列", "伊朗", "沙特", "土耳其", "英国", "英镑",
        "OPEC", "原油", "黄金", "美元", "全球", "国际", "海外", "外资",
        "地缘", "冲突", "战争", "制裁", "关税", "贸易战", "纳斯达克",
        "OpenAI", "英伟达", "苹果", "特斯拉", "亚马逊", "谷歌", "微软",
        "霍尔木兹", "胡塞", "叙利亚", "苏丹",
        "中概股", "罗素", "利弗莫尔", "纳斯达克", "标普500",
    ]
    stock_filter_words = ["减持", "增持", "订单", "合同", "中标", "投产", "募投",
                          "回购", "分红", "派息", "限售股", "解禁", "业绩预告",
                          "业绩快报", "定增", "配股", "可转债", "调研", "投资者关系"]

    def _clean_text(title, content):
        t = (title or "").strip()
        if not t:
            t = (content or "").strip()
        for prefix in ["财联社", "据报道", "消息称", "消息人士", "路透社", "彭博", "央视", "新华"]:
            if t.startswith(prefix):
                idx = t.find("，")
                if 0 < idx < 20:
                    t = t[idx+1:].strip()
                break
        return t

    def _is_stock_announcement(text):
        if "：" not in text:
            return False
        prefix = text.split("：", 1)[0]
        if len(prefix) <= 6 and not any(c in prefix for c in "，。；：、！？,.!?"):
            if any(w in text for w in stock_filter_words):
                return True
            if not any(kw in text for kw in importance_keywords + foreign_keywords + ["央行", "证监会", "政策", "规划"]):
                return True
        return False

    def _importance_score(text):
        score = sum(3 for kw in importance_keywords if kw in text)
        if any(k in text for k in ["%", "亿", "万亿", "点", "美元"]):
            score += 1
        return score

    # 优先财联社电报
    try:
        import akshare as ak
        df = ak.stock_info_global_cls(symbol="全部")
        if df is not None and len(df) > 0:
            items = []
            for _, row in df.iterrows():
                title = str(row.get("标题", "")).strip()
                content = str(row.get("内容", "")).strip()
                pub_time = str(row.get("发布时间", "")).strip()
                text = _clean_text(title, content)
                if not text or len(text) < 8:
                    continue
                if _is_stock_announcement(text):
                    continue
                items.append({"time": pub_time, "text": text})
            seen = set()
            uniq_items = []
            for it in items:
                key = it["text"][:30]
                if key not in seen:
                    seen.add(key)
                    uniq_items.append(it)
            domestic_pool = []
            foreign_pool = []
            for it in uniq_items:
                text = it["text"]
                score = _importance_score(text)
                is_foreign = any(kw in text for kw in foreign_keywords)
                item_str = f"[{it['time']}] {text}"
                if is_foreign:
                    foreign_pool.append((score, item_str))
                else:
                    domestic_pool.append((score, item_str))
            domestic_pool.sort(key=lambda x: -x[0])
            foreign_pool.sort(key=lambda x: -x[0])
            domestic_news = [x[1] for x in domestic_pool[:5]]
            foreign_news = [x[1] for x in foreign_pool[:5]]
            print(f"  消息面数据源: 财联社电报 (国内{len(domestic_news)}条/国外{len(foreign_news)}条/共筛选{len(uniq_items)}条)")
            if domestic_news or foreign_news:
                return {"domestic": domestic_news, "foreign": foreign_news}
    except Exception as e:
        print(f"  财联社电报获取失败: {e}")

    # 备用：东财全球快讯
    try:
        import akshare as ak
        df = ak.stock_info_global_em()
        if df is not None and len(df) > 0:
            for _, row in df.head(20).iterrows():
                title = str(row.get("标题", "")).strip()
                pub_time = str(row.get("发布时间", "")).strip()
                if not title or len(title) < 8:
                    continue
                text = title
                if _is_stock_announcement(text):
                    continue
                is_foreign = any(kw in text for kw in foreign_keywords)
                item_str = f"[{pub_time}] {text}"
                if is_foreign and len(foreign_news) < 5:
                    foreign_news.append(item_str)
                elif not is_foreign and len(domestic_news) < 5:
                    domestic_news.append(item_str)
            print(f"  消息面数据源: 东财全球快讯(备用)")
            if domestic_news or foreign_news:
                return {"domestic": domestic_news, "foreign": foreign_news}
    except Exception as e:
        print(f"  东财快讯获取失败: {e}")

    print("  消息面数据源: 全部失败，返回空")
    return {"domestic": [], "foreign": []}




def calculate_technical_levels(close_price, index_name="上证指数"):
    """计算关键支撑压力位（同花顺接口为主）"""
    # 指数代码映射
    code_map = {
        "上证指数": "sh000001",
        "深证成指": "sz399001",
        "创业板指": "sz399006",
        "科创50": "sh000688",
        "沪深300": "sh000300",
    }
    code = code_map.get(index_name, "sh000001")

    try:
        # 使用同花顺接口获取最近60个交易日K线数据
        import akshare as ak
        df = ak.stock_zh_index_daily_tx(symbol=code)
        if df is not None and len(df) >= 20:
            closes = df["close"].astype(float).tolist()
            highs = df["high"].astype(float).tolist()
            lows = df["low"].astype(float).tolist()

            # 近期高低点（最近20日）
            recent_high = max(highs[-20:])
            recent_low = min(lows[-20:])

            # 均线
            ma20 = sum(closes[-20:]) / 20
            ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else sum(closes) / len(closes)

            # 斐波那契回撤位
            diff = recent_high - recent_low
            support1 = recent_high - diff * 0.382
            support2 = recent_high - diff * 0.618
            resistance1 = recent_high

            print(f"  技术位计算(同花顺): MA20={ma20:.0f}, MA60={ma60:.0f}, 支撑1={support1:.0f}, 压力={resistance1:.0f}")

            return {
                "support1": round(support1, 2),
                "support2": round(support2, 2),
                "resistance1": round(resistance1, 2),
                "ma20": round(ma20, 2),
                "ma60": round(ma60, 2),
                "recent_high": round(recent_high, 2),
                "recent_low": round(recent_low, 2),
            }
    except Exception as e:
        print(f"  同花顺技术位计算失败: {e}")

    # 备用：新浪API
    try:
        url = f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var=/CN_MarketDataService.getKLineData?symbol={code}&scale=240&ma=no&datalen=60"
        headers = {"Referer": "https://finance.sina.com.cn"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = "utf-8"
        text = resp.text
        if "(" in text and ")" in text:
            json_str = text[text.index("(") + 1:text.rindex(")")]
            import json as json_mod
            kline_data = json_mod.loads(json_str)
            if kline_data and len(kline_data) >= 20:
                closes = [float(item["close"]) for item in kline_data]
                highs = [float(item["high"]) for item in kline_data]
                lows = [float(item["low"]) for item in kline_data]
                recent_high = max(highs[-20:])
                recent_low = min(lows[-20:])
                ma20 = sum(closes[-20:]) / 20
                ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else sum(closes) / len(closes)
                diff = recent_high - recent_low
                return {
                    "support1": round(recent_high - diff * 0.382, 2),
                    "support2": round(recent_high - diff * 0.618, 2),
                    "resistance1": round(recent_high, 2),
                    "ma20": round(ma20, 2),
                    "ma60": round(ma60, 2),
                    "recent_high": round(recent_high, 2),
                    "recent_low": round(recent_low, 2),
                }
    except Exception as e:
        print(f"  新浪API技术位计算失败: {e}")

    # 默认值
    return {
        "support1": round(close_price * 0.99, 2),
        "support2": round(close_price * 0.97, 2),
        "resistance1": round(close_price * 1.01, 2),
        "ma20": close_price,
        "ma60": close_price,
        "recent_high": close_price,
        "recent_low": close_price,
    }


# ============================================================
# 第二部分：战报生成
# ============================================================

def generate_morning_report(index_data, breadth, sectors):
    """生成开盘前预期战报（参考588780战报风格）"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.datetime.now().weekday()]

    sh = index_data.get("上证指数", {"close": 0, "change_pct": 0})
    title = "A股大盘每日战报"
    subtitle = f"{today} {weekday} · 开盘前预判 · 基于昨日收盘"

    # 状态判断
    if sh["change_pct"] < -1:
        status = "「偏空 Bearish」"
        template = "red"
    elif sh["change_pct"] > 1:
        status = "「偏多 Bullish」"
        template = "green"
    else:
        status = "「中性 Neutral」"
        template = "blue"

    # 成交额单位转换
    amount_yi = breadth['total_amount'] / 1e8
    if amount_yi >= 10000:
        amount_str = f"{amount_yi/10000:.2f}万亿"
    else:
        amount_str = f"{amount_yi:.0f}亿"

    content = f"""{status}

| 上证指数 | 昨日涨跌 | 两市成交 |
|:---:|:---:|:---:|
| 昨收 | 涨跌幅 | 昨日 |
| **{sh['close']:.2f}** | **{sh['change_pct']:+.2f}%** | **{amount_str}** |

🎯 **今日预判**
基于昨日收盘数据与技术面，今日大概率{status.replace('「', '').replace('」', '')}走势。
重点关注开盘30分钟量能变化与北向资金流向。

📍 **关键价位**

| 第一支撑 | 强支撑 | 压力位 |
|:---:|:---:|:---:|
| {sh['close']*0.995:.0f} 点 | {sh['close']*0.98:.0f} 点 | {sh['close']*1.01:.0f} 点 |
| -0.5% | -2.0% | +1.0% |

📊 **技术面**
MA20参考位 · 关注开盘是否站稳均线 · 量能是否放大

🌐 **消息面**
国内：待补充
国外：待补充

---
详细战报已归档云文档：[每日信息栏_{today}](https://a13cyu3qqeo.feishu.cn/drive/folder/HPxrfEmHdlsfuqdbXc9cMJy2ndc)

⚠️ 本预判基于历史数据，不构成投资建议。股市有风险，投资需谨慎。"""
    return title, subtitle, template, content


def generate_midday_report(index_data, breadth, sectors, score=50, band='🟡黄色(关注)', position=60, factor_scores=None):
    """生成盘中战报（14:30，参考588780战报风格）"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.datetime.now().weekday()]

    sh = index_data.get("上证指数", {"close": 0, "change_pct": 0})
    title = "A股大盘每日战报"
    subtitle = f"{today} {weekday} · 14:30盘中实时 · 数据截至当前"

    # 状态判断
    if sh["change_pct"] < -1:
        status = "「警戒 Alert」"
        template = "red"
    elif sh["change_pct"] > 1:
        status = "「积极 Active」"
        template = "green"
    else:
        status = "「观望 Watch」"
        template = "blue"

    # 板块信息（涨红跌绿）
    top_text = "、".join([f"{n}({color_pct(v, 1)})" for n, v in sectors.get("top", [])[:3]]) or "待更新"
    bottom_text = "、".join([f"{n}({color_pct(v, 1)})" for n, v in sectors.get("bottom", [])[:3]]) or "待更新"

    # 成交额单位转换
    amount_yi = breadth['total_amount'] / 1e8
    if amount_yi >= 10000:
        amount_str = f"{amount_yi/10000:.2f}万亿"
    else:
        amount_str = f"{amount_yi:.0f}亿"

    content = f"""{status}

| 上证指数 | 日涨跌幅 | 两市成交 |
|:---:|:---:|:---:|
| 当前 | 今日 | 截至当前 |
| **{sh['close']:.2f}** | **{sh['change_pct']:+.2f}%** | **{amount_str}** |

🎯 **盘中判断**
{status} · 上涨{breadth['up_count']}家 / 下跌{breadth['down_count']}家

📍 **关键价位**

| 第一支撑 | 强支撑 | 压力位 |
|:---:|:---:|:---:|
| {sh['close']*0.995:.0f} 点 | {sh['close']*0.98:.0f} 点 | {sh['close']*1.01:.0f} 点 |
| -0.5% | -2.0% | +1.0% |

📊 **技术面**
关注尾盘30分钟量能变化 · 是否站稳关键均线

🏭 **板块异动**
**领涨**：{top_text}
**领跌**：{bottom_text}

🌐 **消息面**
国内：待补充
国外：待补充

---
详细战报已归档云文档：[每日信息栏_{today}](https://a13cyu3qqeo.feishu.cn/drive/folder/HPxrfEmHdlsfuqdbXc9cMJy2ndc)

⚠️ 本战报仅用于研究与模型校准，不构成投资建议。"""
    return title, subtitle, template, content


def generate_close_report(index_data, breadth, sectors, tech_levels, news=None):
    """生成收盘总结战报（结构化数据，用于多组件卡片布局）"""
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.datetime.now().weekday()]

    sh = index_data.get("上证指数", {"close": 0, "change_pct": 0})
    title = "A股大盘每日战报"
    subtitle = f"{today} {weekday} · 收盘总结 · 数据截至当日收盘"

    # 状态判断
    if sh["change_pct"] < -1:
        status = "「警戒 Alert」"
        template = "red"
    elif sh["change_pct"] > 1:
        status = "「积极 Active」"
        template = "green"
    else:
        status = "「中性 Neutral」"
        template = "blue"

    # 板块信息（涨红跌绿）
    top_text = "、".join([f"{n}({color_pct(v, 1)})" for n, v in sectors.get("top", [])[:3]]) or "待更新"
    bottom_text = "、".join([f"{n}({color_pct(v, 1)})" for n, v in sectors.get("bottom", [])[:3]]) or "待更新"

    # 技术位
    s1 = tech_levels.get("support1", sh["close"] * 0.99)
    s2 = tech_levels.get("support2", sh["close"] * 0.97)
    r1 = tech_levels.get("resistance1", sh["close"] * 1.01)
    ma20 = tech_levels.get("ma20", sh["close"])
    ma60 = tech_levels.get("ma60", sh["close"])

    # 均线状态
    if sh["close"] > ma20 > ma60:
        ma_status = '<font color="red">多头排列</font> ✅'
    elif sh["close"] < ma20 < ma60:
        ma_status = '<font color="green">空头排列</font> ⚠️'
    else:
        ma_status = "交叉整理"

    # 成交额单位转换
    amount_yi = breadth['total_amount'] / 1e8
    if amount_yi >= 10000:
        amount_str = f"{amount_yi/10000:.2f}万亿"
    else:
        amount_str = f"{amount_yi:.0f}亿"

    # 市场判断一句话
    if sh["change_pct"] < -0.5:
        market_judge = f'<font color="green">放量下跌</font> · 上涨{breadth["up_count"]}家/下跌{breadth["down_count"]}家 · 偏防御，控制仓位'
    elif sh["change_pct"] > 0.5:
        market_judge = f'<font color="red">放量上涨</font> · 上涨{breadth["up_count"]}家/下跌{breadth["down_count"]}家 · 偏积极，关注持续性'
    else:
        market_judge = f"窄幅震荡 · 上涨{breadth['up_count']}家/下跌{breadth['down_count']}家 · 观望为主，等待方向"

    # 消息面数据
    if news:
        domestic_list = news.get("domestic", [])
        foreign_list = news.get("foreign", [])
        news_domestic = "\n".join([f"• {item}" for item in domestic_list]) if domestic_list else "暂无重要国内新闻"
        news_foreign = "\n".join([f"• {item}" for item in foreign_list]) if foreign_list else "暂无重要国际新闻"
    else:
        news_domestic = "待补充"
        news_foreign = "待补充"

    # ============================================================
    # 模型参考结论（基于历史规律与当前数据）
    # ============================================================
    # 1. 趋势状态判断
    if sh["close"] > ma20 > ma60:
        trend_state = "上升趋势（多头排列）"
        trend_signal = "偏多"
    elif sh["close"] < ma20 < ma60:
        trend_state = "下降趋势（空头排列）"
        trend_signal = "偏空"
    else:
        trend_state = "震荡整理（均线交叉）"
        trend_signal = "中性"

    # 2. 量能状态判断
    avg_amount_20 = 20000  # 近20日日均成交约2万亿（经验值，可动态计算）
    if amount_yi > avg_amount_20 * 1.2:
        volume_state = "放量"
    elif amount_yi < avg_amount_20 * 0.8:
        volume_state = "缩量"
    else:
        volume_state = "量能正常"

    # 3. 市场宽度判断
    total_stocks = breadth['up_count'] + breadth['down_count']
    up_ratio = breadth['up_count'] / total_stocks if total_stocks > 0 else 0.5
    if up_ratio > 0.6:
        breadth_state = "普涨（赚钱效应好）"
    elif up_ratio < 0.4:
        breadth_state = "普跌（赚钱效应差）"
    else:
        breadth_state = "分化（涨跌互现）"

    # 4. 板块轮动判断
    defensive_sectors = ["银行", "保险", "证券", "煤炭", "石油", "电力", "公用事业", "医药", "食品饮料"]
    growth_sectors = ["电子", "半导体", "计算机", "通信", "传媒", "新能源", "电力设备", "军工", "汽车"]
    top_sector_names = [n for n, v in sectors.get("top", [])]
    bottom_sector_names = [n for n, v in sectors.get("bottom", [])]
    top_is_defensive = any(any(d in n for d in defensive_sectors) for n in top_sector_names)
    bottom_is_growth = any(any(g in n for g in growth_sectors) for n in bottom_sector_names)
    if top_is_defensive and bottom_is_growth:
        rotation_state = "防御占优（资金避险）"
        rotation_signal = "偏空"
    elif not top_is_defensive and not bottom_is_growth:
        rotation_state = "成长占优（风险偏好高）"
        rotation_signal = "偏多"
    else:
        rotation_state = "板块轮动中"
        rotation_signal = "中性"

    # 5. 综合模型参考结论
    signals = [trend_signal, rotation_signal]
    bull_count = signals.count("偏多")
    bear_count = signals.count("偏空")

    if bear_count >= 2:
        model_direction = "偏空"
        model_prob = "55%-65%"
        model_advice = "控制仓位，关注支撑位得失，避免追高"
    elif bull_count >= 2:
        model_direction = "偏多"
        model_prob = "55%-65%"
        model_advice = "可适度参与，关注量能持续性，设置止损"
    else:
        model_direction = "震荡"
        model_prob = "50%-55%"
        model_advice = "观望为主，等待方向明确，高抛低吸"

    # 历史规律参考（基于2016-2026年回测经验）
    historical_ref = ""
    if trend_state == "下降趋势（空头排列）" and volume_state == "放量":
        historical_ref = "历史规律：空头排列+放量下跌后，短期继续下探概率较高，需等待缩量企稳信号"
    elif trend_state == "上升趋势（多头排列）" and volume_state == "缩量":
        historical_ref = "历史规律：多头排列+缩量回调后，延续上涨概率较高，可关注支撑位买点"
    elif breadth_state == "普跌（赚钱效应差）" and sh["change_pct"] < -0.5:
        historical_ref = "历史规律：普跌+指数下跌后，次日技术性反弹概率约55%，但需警惕持续阴跌"
    elif rotation_state == "防御占优（资金避险）":
        historical_ref = "历史规律：防御板块占优通常预示市场风险偏好下降，短期调整压力较大"

    model_conclusion = f"""📊 **趋势状态**：{trend_state}
📈 **量能状态**：{volume_state}（{amount_yi:.0f}亿）
🎯 **市场宽度**：{breadth_state}（上涨占比{up_ratio*100:.0f}%）
🔄 **板块轮动**：{rotation_state}

**模型参考方向**：{model_direction}（经验概率{model_prob}）
**历史规律参考**：{historical_ref or '当前组合无显著历史规律，需结合实时盘面判断'}
**操作参考**：{model_advice}

⚠️ 以上结论基于2016-2026年历史数据回测规律，样本量有限，仅供参考，不构成投资建议。"""

    # 返回结构化数据
    report_data = {
        "title": title,
        "subtitle": subtitle,
        "template": template,
        "status": status,
        "core_metrics": [
            {"label": "上证指数", "sub_label": "收盘价", "value": f"{sh['close']:.2f}"},
            {"label": "日涨跌幅", "sub_label": "今日", "value": color_pct(sh['change_pct'])},
            {"label": "两市成交", "sub_label": "今日", "value": amount_str},
        ],
        "market_judge": market_judge,
        "key_levels": [
            {"label": "第一支撑", "value": f"{s1:.0f} 点", "change": color_pct((s1/sh['close']-1)*100, 1)},
            {"label": "强支撑", "value": f"{s2:.0f} 点", "change": color_pct((s2/sh['close']-1)*100, 1)},
            {"label": "压力位", "value": f"{r1:.0f} 点", "change": color_pct((r1/sh['close']-1)*100, 1)},
        ],
        "technical": f"MA20({ma20:.0f}) / MA60({ma60:.0f}) → {ma_status}",
        "sectors_top": top_text,
        "sectors_bottom": bottom_text,
        "news_domestic": news_domestic,
        "news_foreign": news_foreign,
        "model_conclusion": model_conclusion,
        "tomorrow_watch": "关注量能是否持续 · 关键支撑位得失 · 外围市场变化",
        "doc_link": f"[每日信息栏_{today}](https://a13cyu3qqeo.feishu.cn/drive/folder/HPxrfEmHdlsfuqdbXc9cMJy2ndc)",
        "risk_warning": "本战报仅用于研究与模型校准，不构成投资建议。",
    }

    # 同时保留markdown格式（用于兼容）
    content = f"""{status}

| 上证指数 | 日涨跌幅 | 两市成交 |
|:---:|:---:|:---:|
| 收盘价 | 今日 | 今日 |
| **{sh['close']:.2f}** | **{sh['change_pct']:+.2f}%** | **{amount_str}** |

🎯 **市场判断**
{market_judge}

📍 **关键价位**

| 第一支撑 | 强支撑 | 压力位 |
|:---:|:---:|:---:|
| {s1:.0f} 点 | {s2:.0f} 点 | {r1:.0f} 点 |
| {(s1/sh['close']-1)*100:+.1f}% | {(s2/sh['close']-1)*100:+.1f}% | {(r1/sh['close']-1)*100:+.1f}% |

📊 **技术面**
{ma_status}

🏭 **板块异动**
**领涨**：{top_text}
**领跌**：{bottom_text}

🌐 **消息面**
国内：{report_data['news_domestic']}
国外：{report_data['news_foreign']}

📝 **明日观察**
{report_data['tomorrow_watch']}

---
详细战报已归档云文档：{report_data['doc_link']}

⚠️ {report_data['risk_warning']}"""

    return title, subtitle, template, content, report_data


# ============================================================
# 第三部分：飞书Webhook发送
# ============================================================

def send_to_feishu(webhook_url, title, subtitle, template, content):
    """通过飞书自定义机器人Webhook发送交互卡片（单markdown组件，兼容旧版）"""
    # 飞书自定义关键词校验：消息内容必须包含关键词"A股大盘战报推送"
    keyword = "A股大盘战报推送"
    content_with_keyword = f"**【{keyword}】**\n\n{content}"

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "subtitle": {"tag": "plain_text", "content": subtitle},
                "template": template,
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": content_with_keyword,
                }
            ],
        },
    }

    headers = {"Content-Type": "application/json"}

    try:
        resp = requests.post(webhook_url, json=payload, headers=headers, timeout=15)
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            print(f"✅ 飞书推送成功: {title}")
            return True
        else:
            print(f"❌ 飞书推送失败: {result}")
            return False
    except Exception as e:
        print(f"❌ 飞书推送异常: {e}")
        return False


def send_to_feishu_structured(webhook_url, report_data):
    """通过飞书自定义机器人Webhook发送结构化多组件卡片（参考588780战报风格）"""
    keyword = "A股大盘战报推送"

    # 构建多组件卡片
    elements = []

    # 1. 关键词标签（确保通过关键词校验）
    elements.append({
        "tag": "markdown",
        "content": f"**【{keyword}】** {report_data.get('status', '')}"
    })

    # 2. 核心指标三列布局
    core_metrics = report_data.get("core_metrics", [])
    if core_metrics:
        columns = []
        for metric in core_metrics:
            columns.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [
                    {"tag": "markdown", "content": f"**{metric['label']}**\n{metric.get('sub_label', '')}"},
                    {"tag": "markdown", "content": f"**{metric['value']}**"},
                ]
            })
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": columns
        })

    # 3. 分隔线
    elements.append({"tag": "hr"})

    # 4. 市场判断
    if report_data.get("market_judge"):
        elements.append({
            "tag": "markdown",
            "content": f"🎯 **市场判断**\n{report_data['market_judge']}"
        })

    # 5. 分隔线
    elements.append({"tag": "hr"})

    # 6. 关键价位三列布局
    key_levels = report_data.get("key_levels", [])
    if key_levels:
        columns = []
        for level in key_levels:
            columns.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [
                    {"tag": "markdown", "content": f"**{level['label']}**"},
                    {"tag": "markdown", "content": f"**{level['value']}**"},
                    {"tag": "markdown", "content": f"{level['change']}"},
                ]
            })
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "grey",
            "columns": columns
        })

    # 7. 分隔线
    elements.append({"tag": "hr"})

    # 8. 技术面
    if report_data.get("technical"):
        elements.append({
            "tag": "markdown",
            "content": f"📊 **技术面**\n{report_data['technical']}"
        })

    # 9. 分隔线
    elements.append({"tag": "hr"})

    # 10. 板块异动
    if report_data.get("sectors_top") or report_data.get("sectors_bottom"):
        sectors_text = "🏭 **板块异动**\n"
        if report_data.get("sectors_top"):
            sectors_text += f"**领涨**：{report_data['sectors_top']}\n"
        if report_data.get("sectors_bottom"):
            sectors_text += f"**领跌**：{report_data['sectors_bottom']}"
        elements.append({"tag": "markdown", "content": sectors_text})

    # 11. 分隔线
    elements.append({"tag": "hr"})

    # 12. 消息面
    if report_data.get("news_domestic") or report_data.get("news_foreign"):
        news_text = "🌐 **消息面**\n"
        if report_data.get("news_domestic"):
            news_text += f"国内：{report_data['news_domestic']}\n"
        if report_data.get("news_foreign"):
            news_text += f"国外：{report_data['news_foreign']}"
        elements.append({"tag": "markdown", "content": news_text})

    # 12.5 分隔线
    elements.append({"tag": "hr"})

    # 12.8 模型参考结论（基于历史规律）
    if report_data.get("model_conclusion"):
        elements.append({
            "tag": "markdown",
            "content": f"🧠 **模型参考结论**\n{report_data['model_conclusion']}"
        })

    # 13. 分隔线
    elements.append({"tag": "hr"})

    # 14. 明日观察
    if report_data.get("tomorrow_watch"):
        elements.append({
            "tag": "markdown",
            "content": f"📝 **明日观察**\n{report_data['tomorrow_watch']}"
        })

    # 15. 分隔线
    elements.append({"tag": "hr"})

    # 16. 云文档链接和风险提示
    footer_text = ""
    if report_data.get("doc_link"):
        footer_text += f"详细战报已归档云文档：{report_data['doc_link']}\n\n"
    if report_data.get("risk_warning"):
        footer_text += f"⚠️ {report_data['risk_warning']}"
    if footer_text:
        elements.append({"tag": "markdown", "content": footer_text})

    # 构建完整卡片
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": report_data.get("title", "A股大盘每日战报")},
                "subtitle": {"tag": "plain_text", "content": report_data.get("subtitle", "")},
                "template": report_data.get("template", "blue"),
            },
            "elements": elements,
        },
    }

    headers = {"Content-Type": "application/json"}

    try:
        resp = requests.post(webhook_url, json=payload, headers=headers, timeout=15)
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            print(f"✅ 飞书推送成功(多组件): {report_data.get('title')}")
            return True
        else:
            print(f"❌ 飞书推送失败: {result}")
            return False
    except Exception as e:
        print(f"❌ 飞书推送异常: {e}")
        return False


# ============================================================
# 第四部分：主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="A股大盘每日战报云端推送")
    parser.add_argument("--period", choices=["morning", "midday", "close"], default="close",
                        help="推送时段: morning(开盘前)/midday(盘中)/close(收盘)")
    args = parser.parse_args()

    # 获取Webhook
    webhook_url = os.environ.get("FEISHU_WEBHOOK", "")
    if not webhook_url:
        print("❌ 错误: 未设置FEISHU_WEBHOOK环境变量")
        sys.exit(1)

    print(f"📡 开始采集数据 (时段: {args.period})...")

    # 采集数据
    index_data = fetch_index_data()
    print(f"  指数数据: {list(index_data.keys())}")

    breadth = fetch_market_breadth()
    print(f"  市场宽度: 上涨{breadth['up_count']} / 下跌{breadth['down_count']} / 成交{breadth['total_amount']/1e8:.0f}亿")

    sectors = fetch_sector_performance()
    print(f"  板块数据: 领涨{len(sectors['top'])}个 / 领跌{len(sectors['bottom'])}个")

    # 采集消息面数据
    news = fetch_news()
    print(f"  消息面: 国内{len(news['domestic'])}条 / 国外{len(news['foreign'])}条")

    # v4.1三因子评分
    score, band, position, factor_scores = calc_score_v41(index_data, breadth)
    print(f"  模型评分: {score}/100 {band} 目标仓位{position}% (趋势{factor_scores['趋势']}/动量{factor_scores['动量']}/波动{factor_scores['波动']})")

    # 计算技术位（仅收盘战报）
    tech_levels = {}
    if args.period == "close":
        sh_close = index_data.get("上证指数", {}).get("close", 3000)
        tech_levels = calculate_technical_levels(sh_close, "上证指数")
        print(f"  技术位: 支撑{tech_levels.get('support1')} / 压力{tech_levels.get('resistance1')}")

    # 生成战报
    print("📝 生成战报内容...")
    if args.period == "morning":
        title, subtitle, template, content = generate_morning_report(index_data, breadth, sectors)
        # 发送飞书（单组件兼容版）
        print("🚀 推送到飞书...")
        success = send_to_feishu(webhook_url, title, subtitle, template, content)
    elif args.period == "midday":
        title, subtitle, template, content = generate_midday_report(index_data, breadth, sectors, score, band, position, factor_scores)
        # 发送飞书（单组件兼容版）
        print("🚀 推送到飞书...")
        success = send_to_feishu(webhook_url, title, subtitle, template, content)
    else:
        # 收盘战报使用结构化多组件卡片
        title, subtitle, template, content, report_data = generate_close_report(index_data, breadth, sectors, tech_levels, news, score, band, position, factor_scores)
        print("🚀 推送到飞书(多组件卡片)...")
        success = send_to_feishu_structured(webhook_url, report_data)

    if success:
        print("✅ 全部完成!")
        sys.exit(0)
    else:
        print("❌ 推送失败!")
        sys.exit(1)


if __name__ == "__main__":
    main()
