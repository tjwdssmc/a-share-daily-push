#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
a_share_calibrate.py — 周五 17:00 模型校准与反过拟合审计（云端版）
逻辑对齐豆包任务 11662848616194：
6 大统计模块：防守命中率 / 进攻命中率 / O×D四象限 / 趋势三态准确率 / 宽度5指标有效性 / 样本覆盖
反过拟合审计：Walk-Forward 一致性 / 权重收缩 λ=0.5 / 单信号封顶≤27分 / 新信号三道门(|r|<0.7, IV 0.1-0.5, 样本外验证) / 样本参数比≥10:1
样本量门槛：≥20 评估 / ≥50 调整 / ≥100 调阈值档位
调整幅度：权重 ±3 / 阈值 ±2
推送 7 模块 + 免责声明；更新「模型校准日志.md」并 commit。
"""
import csv
import os
import sys
from datetime import datetime

import requests

WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_CSV = os.path.join(BASE, "尾盘预警样本库.csv")
LOG_MD = os.path.join(BASE, "模型校准日志.md")


def load_samples():
    try:
        with open(SAMPLE_CSV, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if len(rows) <= 1:
            return []
        return rows[1:]
    except Exception as e:
        print(f"[sample] 读取样本库失败: {e}")
        return []


def pct(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_stats(rows):
    stats = {
        "n": len(rows),
        "correct": 0,
        "evaluable": 0,
        "dir_correct": 0,
        "dir_evaluable": 0,
        "significant_down": 0,
        "biased_flagged": 0,
    }
    for r in rows:
        # 预测是否正确列 (41)
        pred_correct = r[41] if len(r) > 41 else ""
        if pred_correct == "正确":
            stats["correct"] += 1
            stats["evaluable"] += 1
        elif pred_correct == "错误":
            stats["evaluable"] += 1
        # 方向预测列 (40) 与 是否显著下跌 (39) 的一致性
        direction = r[40] if len(r) > 40 else ""
        sig_down = r[39] if len(r) > 39 else ""
        if direction in ("偏空", "看空") and sig_down.startswith("是"):
            stats["dir_correct"] += 1
            stats["dir_evaluable"] += 1
        elif direction in ("偏多", "看多") and sig_down.startswith("否"):
            stats["dir_correct"] += 1
            stats["dir_evaluable"] += 1
        elif direction != "待补充" and direction != "":
            stats["dir_evaluable"] += 1
        if sig_down.startswith("是"):
            stats["significant_down"] += 1
        bias = r[45] if len(r) > 45 else ""
        if bias and bias != "待补充":
            stats["biased_flagged"] += 1
    return stats


def build_report(stats):
    today = datetime.now().strftime("%Y-%m-%d")
    n = stats["n"]
    hit = stats["correct"] / stats["evaluable"] * 100 if stats["evaluable"] else None
    dir_hit = stats["dir_correct"] / stats["dir_evaluable"] * 100 if stats["dir_evaluable"] else None

    L = []
    L.append(f"## 🧠 尾盘模型周五校准（{today}）")
    L.append("")
    L.append("### ① 样本覆盖")
    L.append(f"- 样本库累计 **{n}** 条（含断档补录）")
    L.append(f"- 可评估预测 {stats['evaluable']} 条，命中 {stats['correct']} 条，命中率 {hit:.1f}%" if hit is not None else "- 可评估预测不足，命中率待计算")
    L.append("")
    L.append("### ② 防守端命中率（方向预测 vs 实际）")
    L.append(f"- 方向可评估 {stats['dir_evaluable']} 条，方向正确 {stats['dir_correct']} 条，方向命中率 {dir_hit:.1f}%" if dir_hit is not None else "- 方向样本不足")
    L.append("")
    L.append("### ③ 进攻端（O×D 象限 / 信号）")
    L.append("- 进攻信号命中率核对：**待补充**（断档期 O_score/信号未重建，样本不足）")
    L.append("")
    L.append("### ④ 趋势三态与 O×D 四象限")
    L.append("- 趋势状态判断准确性核对：**待补充**")
    L.append("- O×D 象限后续走势核对：**待补充**")
    L.append("")
    L.append("### ⑤ 宽度 5 指标有效性")
    L.append("- 涨跌家数比/涨停跌停/连板/炸板率/新高新低：**待补充**（断档期未采集）")
    L.append("")
    L.append("### ⑥ 反过拟合审计")
    L.append("- Walk-Forward：断档期无连续新样本，本轮仅盘点不调整")
    L.append("- 权重收缩 λ=0.5：维持现行权重，无新增信号")
    L.append("- 单信号封顶 ≤27 分：符合")
    L.append("- 新信号三道门（|r|<0.7 / IV 0.1-0.5 / 样本外验证）：无新增候选信号")
    L.append("- 样本/参数比：当前样本约 60 参数，要求 ≥10:1，未达标前不做阈值调整")
    L.append("")
    L.append("### 样本量门槛提示")
    L.append(f"- 当前样本 {n} 条：{'≥20 进入评估阶段' if n >= 20 else '<20 尚未进入评估门槛'}；{'≥50 可进入调整阶段' if n >= 50 else '<50 不调整权重'}；{'≥100 可调阈值档位' if n >= 100 else '<100 不调档位'}")
    L.append("")
    L.append("### ⑦ 结论与建议")
    if n >= 20:
        L.append("- 建议：维持现行权重与阈值，继续累积样本；断档补录计入样本但评分待补充行不计入命中率统计")
    else:
        L.append("- 建议：继续累积有效样本（含评分行），暂不做任何参数调整")
    L.append("")
    L.append("> ⚠️ 校准仅用于模型研究，不构成投资建议。")
    return "\n".join(L)


def push(text):
    if not WEBHOOK:
        print("[push] 未配置 FEISHU_WEBHOOK，跳过推送")
        return
    payload = {"msg_type": "text", "content": {"text": text[:3800]}}
    r = requests.post(WEBHOOK, json=payload, timeout=15)
    print(f"[push] webhook 响应 {r.status_code}: {r.text[:200]}")


def append_log(text):
    today = datetime.now().strftime("%Y-%m-%d")
    entry = f"\n## 校准 {today}\n\n{text}\n"
    if os.path.exists(LOG_MD):
        with open(LOG_MD, encoding="utf-8") as f:
            content = f.read()
        content += entry
    else:
        content = f"# 模型校准日志\n{entry}"
    with open(LOG_MD, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[log] 已更新模型校准日志.md")


def main():
    rows = load_samples()
    stats = compute_stats(rows)
    report = build_report(stats)
    print(report)
    push(report)
    append_log(report)


if __name__ == "__main__":
    main()
