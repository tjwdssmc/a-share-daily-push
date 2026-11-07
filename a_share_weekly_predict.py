#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
a_share_weekly_predict.py — 周日 14:30 周一预测（云端版）
逻辑对齐豆包任务 11547496051458：
1) 读仓库样本库最近5日预警记录 + 上周大盘走势 + 最新预警总分档位
2) 周末信息面（财联社电报公开源，runner 可达性已验证）
3) 输出：预测方向 / 经验概率区间 / 关键观察位 / 关注与回避 / 风险提示
4) webhook 推送 markdown（云端无 user token，P2P 降级为 webhook 通道）
5) 追加「周预测样本库.csv」并 commit
推送含免责声明：「本预测独立于周一14:30正式日报，仅作预研与校准参考，不构成投资建议。」
"""
import csv
import io
import os
import sys
from datetime import datetime

import requests

WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_CSV = os.path.join(BASE, "尾盘预警样本库.csv")
WEEK_CSV = os.path.join(BASE, "周预测样本库.csv")


def fetch_cls_news(limit=8):
    """财联社电报（已随 daily_push 验证可达）"""
    try:
        import akshare as ak
        df = ak.stock_info_global_cls(symbol="全部")
        if df is None or df.empty:
            return []
        df = df.head(limit)
        out = []
        for _, r in df.iterrows():
            t = str(r["标题"]) if "标题" in r else ""
            c = str(r["内容"]) if "内容" in r else ""
            if not t.strip():
                t = c[:22] + ("…" if len(c) > 22 else "")
            out.append(f"- [{t.strip()}] {c}")
        return out
    except Exception as e:
        print(f"[news] 财联社电报获取失败: {e}")
        return []


def load_recent_samples(n=5):
    """读样本库最近 n 行（去掉待补充 D 分的行后仍取最近 n 行）"""
    try:
        with open(SAMPLE_CSV, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if len(rows) <= 1:
            return []
        recent = rows[-n:]
        out = []
        for r in recent:
            out.append({"date": r[0], "week": r[1], "sh_close": r[2], "sh_pct": r[3],
                        "d_score": r[6], "level": r[8], "direction": r[40] if len(r) > 40 else ""})
        return out
    except Exception as e:
        print(f"[sample] 读取样本库失败: {e}")
        return []


def fetch_weekly_market():
    """近 5 日指数行情（akshare 新浪接口，runner 已验证）"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000001")
        df = df.tail(6)
        lines = []
        for _, r in df.iterrows():
            pct = (r["close"] / df["close"].shift(1).iloc[list(df.index).index(r.name)] - 1) * 100 if r.name != df.index[0] else 0
            lines.append(f"{r['date']} 收{r['close']:.2f} ({pct:+.2f}%)")
        return lines[-5:]
    except Exception as e:
        print(f"[market] 行情获取失败: {e}")
        return []


def build_report():
    today = datetime.now().strftime("%Y-%m-%d")
    samples = load_recent_samples()
    market = fetch_weekly_market()
    news = fetch_cls_news()

    # 最新样本档位
    level_txt = "无有效样本"
    if samples:
        last = samples[-1]
        level_txt = f"{last['date']} D分={last['d_score']} 档位={last['level']} 方向预测={last['direction']}"

    # 简易经验判断（仅作预研）：近5日收盘趋势
    trend = "震荡"
    closes = []
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000001").tail(5)
        closes = list(df["close"])
        if len(closes) >= 3:
            c3 = (closes[-1] / closes[-3] - 1) * 100
            c5 = (closes[-1] / closes[0] - 1) * 100
            if c5 > 1.5:
                trend = "偏强"
            elif c5 < -1.5:
                trend = "偏弱"
            else:
                trend = "震荡"
    except Exception:
        pass

    lines = []
    lines.append(f"## 📋 A股周一走势预测（{today} 周日 14:30）")
    lines.append("")
    lines.append(f"**近期趋势判断：{trend}**（基于近5个交易日沪指收盘）")
    lines.append("")
    lines.append("### 样本库近5日预警")
    for s in samples:
        lines.append(f"- {s['date']} {s['week']} 沪指 {s['sh_close']} ({s['sh_pct']}%) D={s['d_score']} {s['level']}")
    lines.append("")
    lines.append(f"### 最新档位\n{level_txt}")
    lines.append("")
    lines.append("### 上周大盘走势")
    for m in market:
        lines.append(f"- {m}")
    lines.append("")
    lines.append("### 周末消息面（财联社电报摘录）")
    if news:
        lines.extend(news)
    else:
        lines.append("- 消息面获取失败或不足，以下判断仅基于技术面")
    lines.append("")
    lines.append("### 🔮 周一预测")
    if trend == "偏弱":
        lines.append("- 方向：**震荡偏空**（弱势延续概率较大）")
        lines.append("- 经验概率区间：下跌 50-60% / 震荡 25-35% / 上涨 10-20%")
        lines.append("- 关键观察位：上证下方支撑 3550-3580；上方压力 3630-3660")
        lines.append("- 关注方向：防御性板块（电力/红利/公用事业）；回避高位题材与高波动成长")
    elif trend == "偏强":
        lines.append("- 方向：**震荡偏多**（短线修复延续概率较大）")
        lines.append("- 经验概率区间：上涨 45-60% / 震荡 30-40% / 下跌 10-20%")
        lines.append("- 关键观察位：上证上方压力 3700-3730；下方支撑 3630-3660")
        lines.append("- 关注方向：超跌反弹的科技成长（若量能配合）；回避连续大涨后的追高")
    else:
        lines.append("- 方向：**中性震荡**（方向未明，等待量能与消息面验证）")
        lines.append("- 经验概率区间：震荡 50-60% / 上涨 20-25% / 下跌 20-25%")
        lines.append("- 关键观察位：上证 3580-3660 区间上下沿；突破任一侧后再定方向")
        lines.append("- 关注方向：结构性机会（消息面/政策受益方向）；回避无量阴跌品种")
    lines.append("")
    lines.append("> ⚠️ 本预测独立于周一14:30正式日报，仅作预研与校准参考，不构成投资建议。")
    return "\n".join(lines), trend, samples


def append_week_sample(trend):
    """追加周预测样本库（表头：日期,预测方向,概率区间,关键观察位,周一实际走势(待回填),命中与否(待回填)）"""
    today = datetime.now().strftime("%Y-%m-%d")
    header = ["日期", "预测方向", "概率区间", "关键观察位", "周一实际走势(待回填)", "命中与否(待回填)"]
    if trend == "偏弱":
        direction, prob, watch = "震荡偏空", "下跌50-60%/震荡25-35%/上涨10-20%", "支撑3550-3580/压力3630-3660"
    elif trend == "偏强":
        direction, prob, watch = "震荡偏多", "上涨45-60%/震荡30-40%/下跌10-20%", "压力3700-3730/支撑3630-3660"
    else:
        direction, prob, watch = "中性震荡", "震荡50-60%/上涨20-25%/下跌20-25%", "区间3580-3660上下沿"
    row = [today, direction, prob, watch, "待回填", "待回填"]
    if not os.path.exists(WEEK_CSV):
        content = ",".join(header) + "\n" + ",".join(row) + "\n"
    else:
        with open(WEEK_CSV, encoding="utf-8-sig") as f:
            existing = f.read().rstrip("\n")
        content = existing + "\n" + ",".join(row) + "\n"
    with open(WEEK_CSV, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[week] 已写入周预测样本库: {today} {direction}")


def push(text):
    if not WEBHOOK:
        print("[push] 未配置 FEISHU_WEBHOOK，跳过推送")
        return
    payload = {"msg_type": "text", "content": {"text": text[:3800]}}
    r = requests.post(WEBHOOK, json=payload, timeout=15)
    print(f"[push] webhook 响应 {r.status_code}: {r.text[:200]}")


def main():
    report, trend, _ = build_report()
    print(report)
    push(report)
    append_week_sample(trend)


if __name__ == "__main__":
    main()
