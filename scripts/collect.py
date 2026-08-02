#!/usr/bin/env python3
"""ポケ情報ボード 収集スクリプト.

feeds.yml の設定に従い、Googleニュース/YouTube/X(RSSブリッジ)を巡回して
記事を収集・重複除去・AI要約し、docs/index.html と docs/data.json を生成する。

- 個別フィードの取得失敗は警告ログのみでスキップし、全体は止めない
- ANTHROPIC_API_KEY が無い場合は要約をスキップして正常終了する
"""

import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
FEEDS_YML = ROOT / "feeds.yml"
DATA_JSON = DOCS / "data.json"
INDEX_HTML = DOCS / "index.html"

JST = timezone(timedelta(hours=9))
USER_AGENT = "Mozilla/5.0 (compatible; PokeInfoBoard/1.0; +https://github.com/)"
REQUEST_TIMEOUT = 20

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
SUMMARY_BATCH_SIZE = 12
SNIPPET_FALLBACK_LEN = 110

# カラーパレット(メインカラー #86B3E0 に調和させたトーン)
COLORS = {
    "main": "#86B3E0",   # メイン
    "bg": "#EFF4FA",     # 背景
    "line": "#D3DEEA",   # 罫線
    "ink": "#2B3A4C",    # テキスト(ダークネイビー)
    "white": "#FFFFFF",
    "new": "#E57A7A",    # NEWバッジ(ソフトレッド)
}

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("collect")


# ----------------------------------------------------------------- 取得

def google_news_url(query: str) -> str:
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"


def fetch_feed(url: str):
    """フィードを取得して feedparser の結果を返す。失敗時は None(警告のみ)。"""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            log.warning("パース失敗のためスキップ: %s (%s)", url, parsed.bozo_exception)
            return None
        return parsed
    except Exception as exc:  # noqa: BLE001 - 1ソースの失敗で全体を止めない
        log.warning("取得失敗のためスキップ: %s (%s)", url, exc)
        return None


# ----------------------------------------------------------------- 正規化

TAG_RE = re.compile(r"<[^>]+>")
TRAILING_SOURCE_RE = re.compile(r"\s*[-–—|]\s*[^-–—|]{1,40}$")
NON_WORD_RE = re.compile(r"\W", re.UNICODE)


def strip_html(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text or "")).strip()


def compare_key(text: str) -> str:
    """記号・空白の全半角ゆれを無視して比較するためのキー。"""
    return NON_WORD_RE.sub("", text or "").lower()


def normalize_title_for_id(title: str) -> str:
    """媒体名や配信元表記のゆれを落として、同一記事が同じキーになるようにする。

    Yahoo!ニュース等は「タイトル (配信元) - Yahoo!ニュース」の形で配信するため、
    末尾の媒体名と括弧書きを繰り返し取り除く。
    """
    t = title or ""
    for _ in range(3):
        stripped = TRAILING_SOURCE_RE.sub("", t)
        stripped = re.sub(r"\s*[(（][^()（）]{1,40}[)）]\s*$", "", stripped)
        if stripped == t or not stripped:
            break
        t = stripped
    return re.sub(r"\s+", "", t).lower()


def item_id(title: str) -> str:
    norm = normalize_title_for_id(title)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def strip_redundant_source(title: str, source: str) -> str:
    """媒体名は別枠で表示するので、タイトル末尾の「 - 媒体名」を落とす。"""
    if not source:
        return title
    stripped = TRAILING_SOURCE_RE.sub("", title)
    if stripped and stripped != title and compare_key(title[len(stripped) :]) == compare_key(source):
        return stripped.strip()
    return title


def is_redundant_snippet(snippet: str, title: str) -> bool:
    """抜粋がタイトルの再掲でしかないか判定する。"""
    s_key, t_key = compare_key(snippet), compare_key(title)
    if not s_key or len(t_key) < 10:
        return not s_key
    return s_key.startswith(t_key) or t_key.startswith(s_key)


def entry_timestamp(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
    return None


def normalize_entry(entry, source_name: str, item_type: str) -> dict | None:
    raw_title = strip_html(entry.get("title", ""))
    link = entry.get("link", "")
    if not raw_title or not link:
        return None
    ts = entry_timestamp(entry)
    if ts is None:
        return None
    snippet = strip_html(entry.get("summary", "") or entry.get("description", ""))
    src = source_name
    if not src:
        src_info = entry.get("source")
        src = strip_html(src_info.get("title", "")) if src_info else ""

    # 媒体名は別枠で表示するのでタイトルからは落とし、
    # タイトルの再掲でしかないGoogleニュースの description は捨てる
    title = strip_redundant_source(raw_title, src)
    if is_redundant_snippet(snippet, title):
        snippet = ""
    return {
        "id": item_id(raw_title),
        "title": title,
        "link": link,
        "source": src,
        "snippet": snippet[:300],
        "ts": ts.astimezone(timezone.utc).isoformat(),
        "type": item_type,
        "summary": "",
    }


def title_matches(title: str, keywords) -> bool:
    lowered = title.lower()
    return any(k.lower() in lowered for k in keywords)


# ----------------------------------------------------------------- 収集本体

def collect_items(config: dict) -> dict[str, list[dict]]:
    """カテゴリID -> アイテムリスト(重複除去前)を返す。"""
    by_category: dict[str, list[dict]] = {}

    def add(cat_id: str, item: dict | None):
        if item is not None:
            by_category.setdefault(cat_id, []).append(item)

    for cat in config.get("categories", []):
        cat_id = cat["id"]
        for query in cat.get("news_queries", []) or []:
            parsed = fetch_feed(google_news_url(query))
            if parsed is None:
                continue
            for entry in parsed.entries:
                add(cat_id, normalize_entry(entry, "", "news"))
        for feed in cat.get("feeds", []) or []:
            parsed = fetch_feed(feed["url"])
            if parsed is None:
                continue
            ftype = feed.get("type", "news")
            for entry in parsed.entries:
                item = normalize_entry(entry, feed.get("source", ""), ftype)
                if item and feed.get("title_filter") and not title_matches(item["title"], feed["title_filter"]):
                    continue
                add(cat_id, item)
        for x_url in cat.get("x_rss", []) or []:
            parsed = fetch_feed(x_url)
            if parsed is None:
                continue
            for entry in parsed.entries:
                add(cat_id, normalize_entry(entry, cat.get("name", "") + " 公式X", "x"))

    # 汎用ソース: routing でカテゴリ振り分け
    routing = config.get("routing", []) or []
    fallback_id = (config.get("fallback_category") or {}).get("id", "other")
    for src in config.get("shared_sources", []) or []:
        parsed = fetch_feed(src["url"])
        if parsed is None:
            continue
        stype = src.get("type", "news")
        for entry in parsed.entries:
            item = normalize_entry(entry, src.get("source", ""), stype)
            if item is None:
                continue
            if src.get("title_filter") and not title_matches(item["title"], src["title_filter"]):
                continue
            target = fallback_id
            for rule in routing:
                if title_matches(item["title"], rule.get("keywords", [])):
                    target = rule["category"]
                    break
            add(target, item)

    return by_category


def dedupe_and_trim(items: list[dict], max_age_days: int, max_items: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    seen: dict[str, dict] = {}
    for item in items:
        if datetime.fromisoformat(item["ts"]) < cutoff:
            continue
        prev = seen.get(item["id"])
        if prev is None:
            seen[item["id"]] = item
        elif not prev["snippet"] and item["snippet"]:
            # snippet を持つ方を残す(タイトル・リンクは先勝ち)
            prev["snippet"] = item["snippet"]
    result = sorted(seen.values(), key=lambda x: x["ts"], reverse=True)
    return result[:max_items]


# ----------------------------------------------------------------- AI要約

def load_previous_summaries() -> dict[str, str]:
    try:
        data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    summaries = {}
    for items in data.get("categories", {}).values():
        for item in items:
            if item.get("summary"):
                summaries[item["id"]] = item["summary"]
    return summaries


def summarize_batch(api_key: str, batch: list[dict]) -> dict[str, str]:
    lines = []
    for item in batch:
        lines.append(f'- id: {item["id"]}\n  タイトル: {item["title"]}\n  本文抜粋: {item["snippet"][:200]}')
    prompt = (
        "以下のポケモン関連記事を、それぞれ日本語1文・50字以内で要約してください。\n"
        "発売日・締切・価格などの具体情報があれば優先して含めてください。\n"
        '出力は {"記事id": "要約", ...} 形式のJSONのみ。コードブロック記号や説明文は不要です。\n\n'
        + "\n".join(lines)
    )
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": ANTHROPIC_MODEL,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    text = "".join(
        block.get("text", "") for block in resp.json().get("content", []) if block.get("type") == "text"
    ).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    parsed = json.loads(text)
    return {k: str(v) for k, v in parsed.items() if isinstance(k, str)}


def apply_summaries(by_category: dict[str, list[dict]]):
    previous = load_previous_summaries()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    pending = []
    for items in by_category.values():
        for item in items:
            if item["id"] in previous:
                item["summary"] = previous[item["id"]]
            elif api_key:
                pending.append(item)

    if not api_key:
        log.info("ANTHROPIC_API_KEY 未設定のためAI要約をスキップします")
        return
    if not pending:
        log.info("新着なし: 要約は全件 data.json から引き継ぎました")
        return

    log.info("AI要約対象: %d 件", len(pending))
    for i in range(0, len(pending), SUMMARY_BATCH_SIZE):
        batch = pending[i : i + SUMMARY_BATCH_SIZE]
        try:
            summaries = summarize_batch(api_key, batch)
        except Exception as exc:  # noqa: BLE001 - バッチ失敗はスキップして続行
            log.warning("要約バッチ失敗のためスキップ: %s", exc)
            continue
        for item in batch:
            if summaries.get(item["id"]):
                item["summary"] = summaries[item["id"]]


# ----------------------------------------------------------------- HTML生成

def fmt_jst(iso_ts: str) -> str:
    return datetime.fromisoformat(iso_ts).astimezone(JST).strftime("%m/%d %H:%M")


def render_card(item: dict, color: str, now_utc: datetime) -> str:
    e = html.escape
    is_new = (now_utc - datetime.fromisoformat(item["ts"])) <= timedelta(hours=24)
    badges = ""
    if is_new:
        badges += '<span class="badge badge-new">NEW</span>'
    if item["type"] == "video":
        badges += '<span class="badge badge-video">&#9654; 動画</span>'
    elif item["type"] == "x":
        badges += '<span class="badge badge-video">X</span>'

    summary_html = ""
    if item.get("summary"):
        summary_html = (
            '<div class="summary"><span class="badge badge-ai">AI要約</span>'
            f'<span class="summary-text">{e(item["summary"])}</span></div>'
        )
    elif item.get("snippet"):
        summary_html = (
            '<div class="summary">'
            f'<span class="summary-text">{e(item["snippet"][:SNIPPET_FALLBACK_LEN])}</span></div>'
        )

    source_html = f'<span class="source">{e(item["source"])}</span>' if item["source"] else ""
    return (
        f'<article class="card" style="border-left-color:{e(color)}">'
        f'<div class="card-meta">{badges}<span class="time">{fmt_jst(item["ts"])}</span>{source_html}</div>'
        f'<a class="card-title" href="{e(item["link"])}" target="_blank" rel="noopener">{e(item["title"])}</a>'
        f"{summary_html}"
        "</article>"
    )


def render_html(config: dict, by_category: dict[str, list[dict]]) -> str:
    e = html.escape
    now_utc = datetime.now(timezone.utc)
    settings = config.get("settings", {}) or {}
    site_title = settings.get("site_title", "ポケ情報ボード")
    updated = now_utc.astimezone(JST).strftime("%Y/%m/%d %H:%M")

    cats = list(config.get("categories", []))
    fallback = config.get("fallback_category") or {}
    if fallback and by_category.get(fallback.get("id")):
        cats.append(fallback)

    tabs = []
    panels = []
    for i, cat in enumerate(cats):
        cat_id = cat["id"]
        items = by_category.get(cat_id, [])
        active = " active" if i == 0 else ""
        tabs.append(
            f'<button class="tab{active}" data-target="panel-{e(cat_id)}" '
            f'style="--cat:{e(cat.get("color", COLORS["main"]))}">'
            f'{e(cat["name"])}<span class="count">{len(items)}</span></button>'
        )
        cards = "".join(render_card(item, cat.get("color", COLORS["main"]), now_utc) for item in items)
        if not cards:
            cards = '<p class="empty">まだ記事がありません</p>'
        panels.append(f'<section class="panel{active}" id="panel-{e(cat_id)}">{cards}</section>')

    c = COLORS
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(site_title)}</title>
<style>
:root {{
  --main: {c['main']}; --bg: {c['bg']}; --line: {c['line']};
  --ink: {c['ink']}; --white: {c['white']}; --new: {c['new']};
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Yu Gothic", Meiryo, sans-serif;
  background: var(--bg); color: var(--ink);
  max-width: 600px; margin: 0 auto; padding-bottom: 40px;
}}
header {{
  background: var(--main); color: var(--white);
  padding: 16px 16px 12px; border-radius: 0 0 14px 14px;
}}
header h1 {{ font-size: 20px; letter-spacing: 1px; }}
header .updated {{ font-size: 11px; margin-top: 4px; opacity: .95; }}
.tabs {{
  display: flex; gap: 8px; overflow-x: auto; padding: 12px 12px 4px;
  -webkit-overflow-scrolling: touch; scrollbar-width: none;
}}
.tabs::-webkit-scrollbar {{ display: none; }}
.tab {{
  flex: 0 0 auto; border: 1px solid var(--line); background: var(--white);
  color: var(--ink); border-radius: 999px; padding: 7px 14px;
  font-size: 13px; cursor: pointer; display: flex; align-items: center; gap: 6px;
}}
.tab .count {{
  background: var(--cat); color: var(--white); border-radius: 999px;
  font-size: 11px; padding: 1px 7px; min-width: 20px; text-align: center;
}}
.tab.active {{ background: var(--ink); color: var(--white); border-color: var(--ink); }}
.panel {{ display: none; padding: 10px 12px; }}
.panel.active {{ display: block; }}
.card {{
  background: var(--white); border: 1px solid var(--line);
  border-left: 5px solid var(--main); border-radius: 12px;
  padding: 12px 14px; margin-bottom: 10px;
}}
.card-meta {{
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 11px; margin-bottom: 6px;
}}
.time {{ color: var(--ink); opacity: .65; }}
.source {{ color: var(--ink); opacity: .65; }}
.badge {{
  border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: bold;
  color: var(--white); line-height: 1.2;
}}
.badge-new {{ background: var(--new); }}
.badge-video {{ background: var(--ink); }}
.badge-ai {{ background: var(--main); flex: 0 0 auto; }}
.card-title {{
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 4;
  overflow: hidden; color: var(--ink); font-size: 14px; font-weight: bold;
  line-height: 1.45; text-decoration: none; word-break: break-word;
}}
.card-title:hover {{ text-decoration: underline; }}
.summary {{
  display: flex; gap: 8px; align-items: flex-start;
  background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
  padding: 8px 10px; margin-top: 8px; font-size: 12px; line-height: 1.5;
}}
.summary-text {{ flex: 1; min-width: 0; word-break: break-word; overflow-wrap: anywhere; }}
.empty {{ text-align: center; font-size: 13px; opacity: .6; padding: 30px 0; }}
footer {{
  text-align: center; font-size: 11px; opacity: .6; padding: 20px 12px 0;
}}
</style>
</head>
<body>
<header>
  <h1>{e(site_title)}</h1>
  <div class="updated">最終更新: {updated} (JST)</div>
</header>
<nav class="tabs">{''.join(tabs)}</nav>
{''.join(panels)}
<footer>6時間ごと自動更新 / feeds.yml で編集可</footer>
<script>
document.querySelectorAll('.tab').forEach(function (tab) {{
  tab.addEventListener('click', function () {{
    document.querySelectorAll('.tab').forEach(function (t) {{ t.classList.remove('active'); }});
    document.querySelectorAll('.panel').forEach(function (p) {{ p.classList.remove('active'); }});
    tab.classList.add('active');
    document.getElementById(tab.dataset.target).classList.add('active');
  }});
}});
</script>
</body>
</html>
"""


# ----------------------------------------------------------------- main

def main() -> int:
    config = yaml.safe_load(FEEDS_YML.read_text(encoding="utf-8"))
    settings = config.get("settings", {}) or {}
    max_items = int(settings.get("max_items_per_category", 30))
    max_age_days = int(settings.get("max_age_days", 21))

    log.info("収集を開始します")
    by_category = collect_items(config)
    for cat_id, items in by_category.items():
        by_category[cat_id] = dedupe_and_trim(items, max_age_days, max_items)
        log.info("カテゴリ %s: %d 件", cat_id, len(by_category[cat_id]))

    apply_summaries(by_category)

    DOCS.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "categories": by_category,
    }
    DATA_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    INDEX_HTML.write_text(render_html(config, by_category), encoding="utf-8")
    log.info("生成完了: %s / %s", INDEX_HTML, DATA_JSON)
    return 0


if __name__ == "__main__":
    sys.exit(main())
