#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
a_share_news_daily.py — 每日 07:00 全球热点新闻日报（云端版）
逻辑对齐豆包任务 13141132252930：检索全球热点前10条 → 结构化简报 → 推送。
云端 runner 无 AI 检索能力，降级为公开数据源聚合：
  财联社电报（akshare，runner 已验证可达）+ 通用 RSS（Google News 等，可访问时补充）。
推送结构：排名 / 标题 / 一句话摘要 / 来源 / 领域地区。
推送含说明：云端自动聚合，未做 AI 深度摘要（原豆包 AI 检索能力不可用）。
"""
import os
from datetime import datetime, timedelta, timezone

import requests

WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")


def fetch_cls():
    """财联社电报 top 条（已随 daily_push 验证 runner 可达）"""
    try:
        import akshare as ak
        df = ak.stock_info_global_cls(symbol="全部")
        if df is None or df.empty:
            return []
        out = []
        for _, r in df.head(25).iterrows():
            t = str(r["标题"]) if "标题" in r else ""
            c = str(r["内容"]) if "内容" in r else ""
            if not t.strip():
                t = c[:22] + ("…" if len(c) > 22 else "")
            out.append((t.strip(), c, "财联社"))
        return out
    except Exception as e:
        print(f"[cls] 财联社获取失败: {e}")
        return []


def fetch_em():
    """东财全球财经快讯（补充源，runner 可达性未验证，失败静默）"""
    try:
        import akshare as ak
        df = ak.stock_info_global_em()
        if df is None or df.empty:
            return []
        out = []
        for _, r in df.head(10).iterrows():
            t = str(r.get("标题", "")).strip()
            c = str(r.get("内容", "")) if "内容" in r else ""
            if not t:
                t = c[:22] + ("…" if len(c) > 22 else "")
            out.append((t, c, "东方财富"))
        return out
    except Exception as e:
        print(f"[em] 东财快讯获取失败: {e}")
        return []


def fetch_rss():
    """Google News 中文 RSS（可访问时使用，失败静默）"""
    try:
        r = requests.get("https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans", timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:6]
        out = []
        for it in items:
            title = it.findtext("title", "") or ""
            src = it.findtext("source", "") or "Google News"
            out.append((title.strip(), "", src.strip()))
        return out
    except Exception as e:
        print(f"[rss] RSS 获取失败: {e}")
        return []


def build_report():
    # GitHub Actions 运行在 UTC，07:00 北京时间对应 UTC 前一日 23:00，需折算为北京时间日期
    today = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    cls_items = fetch_cls()
    rss_items = fetch_rss()
    em_items = fetch_em()

    # 合并去重（按标题前 20 字）
    seen = set()
    merged = []
    for t, c, src in cls_items + em_items + rss_items:
        key = t[:20]
        if key and key not in seen:
            seen.add(key)
            merged.append((t, c, src))
    if len(merged) < 10 and rss_items:
        for t, c, src in rss_items:
            key = t[:20]
            if key and key not in seen:
                seen.add(key)
                merged.append((t, c, src))
    merged = merged[:10]

    L = []
    L.append(f"## 🌍 全球热点新闻日报（{today}）")
    L.append("")
    if not merged:
        L.append("- 今日未能获取足够数量的热点新闻（数据源均不可达），请稍后手动查看。")
    else:
        domains = {"财联社": "财经/国内", "东方财富": "财经/全球", "Google News": "综合/国际"}
        for i, (t, c, src) in enumerate(merged, 1):
            one_line = c[:80] if c else "（无摘要，来源标题）"
            domain = domains.get(src, "综合/国际")
            L.append(f"{i}. **{t}**")
            L.append(f"   - 摘要：{one_line}")
            L.append(f"   - 来源：{src}｜领域：{domain}")
    L.append("")
    L.append("> ⚠️ 本日报为云端脚本自动聚合（财联社/东财/RSS），未做 AI 深度摘要，仅作信息参考，不构成投资建议。")
    return "\n".join(L)


def push(text):
    if not WEBHOOK:
        print("[push] 未配置 FEISHU_WEBHOOK，跳过推送")
        return
    payload = {"msg_type": "text", "content": {"text": text[:3800]}}
    r = requests.post(WEBHOOK, json=payload, timeout=15)
    print(f"[push] webhook 响应 {r.status_code}: {r.text[:200]}")


def main():
    report = build_report()
    print(report)
    push(report)


if __name__ == "__main__":
    main()
