# -*- coding: utf-8 -*-
"""
A股每日收盘自检（云端版 v1.0）
- 不消耗豆包额度，GitHub Actions 定时触发
- 读取样本库CSV → akshare回填行情 → 统计命中率 → 生成报告 → 飞书webhook推送 → commit回仓库
"""
import os
import sys
import csv
import json
import datetime
import subprocess
import requests
import pandas as pd

# ============================================================
# 配置
# ============================================================
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_CSV = os.path.join(REPO_DIR, "尾盘预警样本库.csv")
LOG_MD = os.path.join(REPO_DIR, "每日自检日志.md")
WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK", "")
KEYWORD = "A股大盘战报推送"  # webhook安全关键词，必须出现在正文

# ============================================================
# 工具函数
# ============================================================
def is_trading_day(date_str):
    """简单判断：周末非交易日（节假日由akshare数据自然跳过）"""
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    return d.weekday() < 5

def get_index_data():
    """获取上证指数历史日线数据"""
    try:
        import akshare as ak
        df = ak.stock_zh_index_daily(symbol="sh000001")
        df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
        df = df.sort_values('date').reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[WARN] 获取上证指数数据失败: {e}")
        return None

def find_next_trading_day(df, date_str, n=1):
    """找到date_str之后第n个交易日的涨跌幅"""
    if df is None or date_str not in df['date'].values:
        return None
    idx = df[df['date'] == date_str].index[0]
    target_idx = idx + n
    if target_idx >= len(df):
        return None
    base_close = df.loc[idx, 'close']
    target_close = df.loc[target_idx, 'close']
    return round((target_close / base_close - 1) * 100, 2)

def find_max_drawdown(df, date_str, n=10):
    """找到date_str之后n个交易日内的最大跌幅"""
    if df is None or date_str not in df['date'].values:
        return None
    idx = df[df['date'] == date_str].index[0]
    end_idx = min(idx + n + 1, len(df))
    if idx + 1 >= end_idx:
        return None
    base_close = df.loc[idx, 'close']
    segment = df.loc[idx+1:end_idx-1, 'close']
    min_close = segment.min()
    return round((min_close / base_close - 1) * 100, 2)

# ============================================================
# 核心逻辑
# ============================================================
def backfill_samples(df_index):
    """回填样本库中的走势数据"""
    if not os.path.exists(SAMPLE_CSV):
        print("[ERROR] 样本库不存在")
        return None, 0

    df = pd.read_csv(SAMPLE_CSV, dtype=str)
    updated = 0

    for i, row in df.iterrows():
        date_str = str(row.get('日期', '')).strip()
        if not date_str or date_str == 'nan':
            continue

        # 回填后1日
        if pd.isna(row.get('后1日涨跌幅')) or str(row.get('后1日涨跌幅', '')).strip() in ('', '待回填', 'nan'):
            val = find_next_trading_day(df_index, date_str, 1)
            if val is not None:
                df.at[i, '后1日涨跌幅'] = str(val)
                updated += 1

        # 回填后5日
        if pd.isna(row.get('后5日涨跌幅')) or str(row.get('后5日涨跌幅', '')).strip() in ('', '待回填', 'nan'):
            val = find_next_trading_day(df_index, date_str, 5)
            if val is not None:
                df.at[i, '后5日涨跌幅'] = str(val)
                updated += 1

        # 回填后10日
        if pd.isna(row.get('后10日涨跌幅')) or str(row.get('后10日涨跌幅', '')).strip() in ('', '待回填', 'nan'):
            val = find_next_trading_day(df_index, date_str, 10)
            if val is not None:
                df.at[i, '后10日涨跌幅'] = str(val)
                updated += 1

        # 回填后续最大跌幅
        if pd.isna(row.get('后续最大跌幅')) or str(row.get('后续最大跌幅', '')).strip() in ('', '待回填', 'nan'):
            val = find_max_drawdown(df_index, date_str, 10)
            if val is not None:
                df.at[i, '后续最大跌幅'] = str(val)
                updated += 1

        # 标注是否显著下跌
        if pd.isna(row.get('是否显著下跌')) or str(row.get('是否显著下跌', '')).strip() in ('', '待回填', 'nan'):
            d10 = df.at[i, '后10日涨跌幅']
            mdd = df.at[i, '后续最大跌幅']
            try:
                d10_f = float(d10) if d10 not in ('', 'nan', '待回填') else None
                mdd_f = float(mdd) if mdd not in ('', 'nan', '待回填') else None
                if d10_f is not None and mdd_f is not None:
                    if d10_f <= -3 or mdd_f <= -5:
                        df.at[i, '是否显著下跌'] = '是'
                    else:
                        df.at[i, '是否显著下跌'] = '否'
            except (ValueError, TypeError):
                pass

    # 保存更新后的CSV
    df.to_csv(SAMPLE_CSV, index=False, encoding='utf-8-sig')
    return df, updated


def calc_statistics(df):
    """计算统计指标"""
    stats = {
        'total_samples': len(df),
        'direction_correct': 0,
        'direction_total': 0,
        'tier_stats': {},
        'deviation_warning': [],
    }

    # 方向预测核对
    recent_predictions = []
    for _, row in df.iterrows():
        pred = str(row.get('方向预测', '')).strip()
        correct = str(row.get('预测是否正确', '')).strip()
        if pred in ('偏多', '中性', '偏空') and correct in ('正确', '错误'):
            stats['direction_total'] += 1
            if correct == '正确':
                stats['direction_correct'] += 1
            recent_predictions.append(correct)

    # 连续错误检测
    if len(recent_predictions) >= 3:
        last3 = recent_predictions[-3:]
        if all(c == '错误' for c in last3):
            stats['deviation_warning'].append("⚠️ 连续3次方向预测错误！")

    # 分档统计
    for _, row in df.iterrows():
        tier = str(row.get('档位', '')).strip()
        sig = str(row.get('是否显著下跌', '')).strip()
        if tier and tier != 'nan' and tier != '待补充':
            if tier not in stats['tier_stats']:
                stats['tier_stats'][tier] = {'total': 0, 'sig_decline': 0}
            stats['tier_stats'][tier]['total'] += 1
            if sig == '是':
                stats['tier_stats'][tier]['sig_decline'] += 1

    return stats


def generate_report(df, stats, backfill_count):
    """生成自检报告markdown"""
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    lines.append(f"# A股每日自检报告（云端版）")
    lines.append(f"**生成时间**：{today}")
    lines.append(f"**{KEYWORD}**")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 一、数据回填摘要
    lines.append("## 一、数据回填摘要")
    lines.append(f"- 累计样本量：**{stats['total_samples']}** 条")
    lines.append(f"- 本次回填字段数：**{backfill_count}** 个")
    lines.append("")

    # 二、方向预测核对
    lines.append("## 二、方向预测核对")
    if stats['direction_total'] > 0:
        acc = stats['direction_correct'] / stats['direction_total'] * 100
        lines.append(f"- 已核对样本：**{stats['direction_total']}** 条")
        lines.append(f"- 预测正确：**{stats['direction_correct']}** 条")
        lines.append(f"- 累计命中率：**{acc:.1f}%**")
    else:
        lines.append("- 暂无已核对的方向预测样本")
    lines.append("")

    # 三、分档防守效果
    lines.append("## 三、分档防守效果（后续显著下跌比例）")
    if stats['tier_stats']:
        lines.append("| 档位 | 样本量 | 显著下跌 | 比例 |")
        lines.append("|------|--------|----------|------|")
        for tier, s in sorted(stats['tier_stats'].items()):
            ratio = s['sig_decline'] / s['total'] * 100 if s['total'] > 0 else 0
            lines.append(f"| {tier} | {s['total']} | {s['sig_decline']} | {ratio:.1f}% |")
    else:
        lines.append("- 暂无分档统计数据")
    lines.append("")

    # 四、偏差预警
    lines.append("## 四、偏差预警")
    if stats['deviation_warning']:
        for w in stats['deviation_warning']:
            lines.append(f"- {w}")
    else:
        lines.append("- ✅ 无偏差预警（未触发连续3次错误阈值）")
    lines.append("")

    # 五、最近5条样本
    lines.append("## 五、最近5条样本概览")
    lines.append("| 日期 | 沪指涨跌 | 档位 | 后1日 | 后5日 | 方向预测 | 核对 |")
    lines.append("|------|----------|------|-------|-------|----------|------|")
    for _, row in df.tail(5).iterrows():
        d = str(row.get('日期', ''))[:10]
        chg = str(row.get('沪指涨跌幅', ''))
        tier = str(row.get('档位', ''))[:8]
        d1 = str(row.get('后1日涨跌幅', ''))
        d5 = str(row.get('后5日涨跌幅', ''))
        pred = str(row.get('方向预测', ''))
        corr = str(row.get('预测是否正确', ''))
        lines.append(f"| {d} | {chg} | {tier} | {d1} | {d5} | {pred} | {corr} |")
    lines.append("")

    lines.append("---")
    lines.append("⚠️ 本自检为模型迭代工具，不构成投资建议。样本量有限，统计结果仅供参考。")

    return "\n".join(lines)


def send_to_feishu(report_md):
    """通过飞书webhook推送报告"""
    if not WEBHOOK_URL:
        print("[WARN] 未设置FEISHU_WEBHOOK环境变量，跳过推送")
        return False

    # 飞书webhook text格式（自定义机器人不支持md标签）
    content = report_md[:3000]
    payload = {
        "msg_type": "text",
        "content": {
            "text": content
        }
    }

    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            print("✅ 飞书推送成功")
            return True
        else:
            print(f"[WARN] 飞书推送返回: {result}")
            return False
    except Exception as e:
        print(f"[ERROR] 飞书推送异常: {e}")
        return False


def append_to_log(report_md):
    """追加到自检日志"""
    try:
        with open(LOG_MD, 'a', encoding='utf-8') as f:
            f.write("\n\n---\n\n")
            f.write(report_md)
        print("✅ 自检日志已更新")
    except Exception as e:
        print(f"[WARN] 写入自检日志失败: {e}")


def git_commit_and_push():
    """提交更新到GitHub仓库"""
    try:
        # 配置git
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"],
                       capture_output=True, cwd=REPO_DIR)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
                       capture_output=True, cwd=REPO_DIR)

        # 添加文件
        subprocess.run(["git", "add", "尾盘预警样本库.csv", "每日自检日志.md"],
                       capture_output=True, cwd=REPO_DIR)

        # 检查是否有变更
        result = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                capture_output=True, cwd=REPO_DIR)
        if result.returncode == 0:
            print("ℹ️ 无变更需要提交")
            return True

        # 提交
        msg = f"chore: 每日自检更新 {datetime.datetime.now().strftime('%Y-%m-%d')}"
        subprocess.run(["git", "commit", "-m", msg], capture_output=True, cwd=REPO_DIR)

        # 推送
        push_result = subprocess.run(["git", "push"], capture_output=True, text=True, cwd=REPO_DIR)
        if push_result.returncode == 0:
            print("✅ 已提交并推送到GitHub")
            return True
        else:
            print(f"[WARN] git push失败: {push_result.stderr[:200]}")
            return False
    except Exception as e:
        print(f"[WARN] git提交异常: {e}")
        return False


# ============================================================
# 主流程
# ============================================================
def main():
    print("=" * 50)
    print("A股每日收盘自检（云端版 v1.0）")
    print(f"运行时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 1. 获取行情数据
    print("\n[1/5] 获取上证指数历史数据...")
    df_index = get_index_data()
    if df_index is not None:
        print(f"  获取到 {len(df_index)} 条日线数据")
    else:
        print("  ⚠️ 行情数据获取失败，跳过回填")

    # 2. 回填样本
    print("\n[2/5] 回填样本库走势数据...")
    df, backfill_count = backfill_samples(df_index)
    if df is None:
        print("  ❌ 样本库读取失败，终止")
        sys.exit(1)
    print(f"  回填 {backfill_count} 个字段，累计样本 {len(df)} 条")

    # 3. 统计
    print("\n[3/5] 计算统计指标...")
    stats = calc_statistics(df)
    print(f"  方向预测命中率: {stats['direction_correct']}/{stats['direction_total']}")

    # 4. 生成报告
    print("\n[4/5] 生成自检报告...")
    report = generate_report(df, stats, backfill_count)

    # 5. 推送 + 日志 + commit
    print("\n[5/5] 推送报告并归档...")
    send_to_feishu(report)
    append_to_log(report)

    # 仅在GitHub Actions环境中commit（检测GITHUB_ACTIONS环境变量）
    if os.environ.get("GITHUB_ACTIONS") == "true":
        git_commit_and_push()
    else:
        print("ℹ️ 本地运行，跳过git commit")

    print("\n" + "=" * 50)
    print("✅ 自检完成")
    print("=" * 50)


if __name__ == "__main__":
    main()
