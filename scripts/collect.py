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
        "guide": False,
        "summary": "",
    }


def title_matches(title: str, keywords) -> bool:
    lowered = title.lower()
    return any(k.lower() in lowered for k in keywords)


def is_guide_item(item: dict, guide_filter: dict) -> bool:
    """攻略Wiki・買取価格・通販ページなど、新着ニュースではない記事か判定する。

    公式チャンネルの動画やXは常に新着扱いのままにする。
    """
    if item["type"] != "news":
        return False
    src = item["source"].lower()
    if any(s.lower() in src for s in guide_filter.get("sources", []) or []):
        return True
    return title_matches(item["title"], guide_filter.get("keywords", []) or [])


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

WEEKDAYS_JA = "月火水木金土日"


def to_jst(iso_ts: str) -> datetime:
    return datetime.fromisoformat(iso_ts).astimezone(JST)


def fmt_time(iso_ts: str) -> str:
    return to_jst(iso_ts).strftime("%H:%M")


def fmt_day_label(day: datetime, today: datetime) -> str:
    delta = (today.date() - day.date()).days
    if delta == 0:
        return "今日"
    if delta == 1:
        return "昨日"
    return f"{day.month}月{day.day}日({WEEKDAYS_JA[day.weekday()]})"


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

    if item.get("guide"):
        badges += '<span class="badge badge-guide">攻略・まとめ</span>'

    source_html = f'<span class="source">{e(item["source"])}</span>' if item["source"] else ""
    cls = "card guide" if item.get("guide") else "card"
    return (
        f'<article class="{cls}" data-id="{e(item["id"])}" data-ts="{e(item["ts"])}" '
        f'data-type="{e(item["type"])}" style="border-left-color:{e(color)}">'
        f'<div class="card-meta">{badges}<span class="time">{fmt_time(item["ts"])}</span>{source_html}'
        f"{ACTIONS_HTML}</div>"
        f'<a class="card-title" href="{e(item["link"])}" target="_blank" rel="noopener">{e(item["title"])}</a>'
        f"{summary_html}"
        "</article>"
    )


# ☆=お気に入り / ✕=アーカイブ。アーカイブ表示中は ✕ が「戻す」になる。
ACTIONS_HTML = (
    '<span class="actions">'
    '<button class="act act-fav" type="button" title="お気に入り" aria-label="お気に入りに追加">&#9734;</button>'
    '<button class="act act-arc" type="button" title="アーカイブ" aria-label="アーカイブする">&#10005;</button>'
    "</span>"
)


def render_days(items: list[dict], color: str, now_utc: datetime) -> str:
    """記事を日付ごとの見出しでまとめる。"""
    today = now_utc.astimezone(JST)
    groups: list[tuple[datetime, list[dict]]] = []
    for item in items:
        day = to_jst(item["ts"])
        if not groups or groups[-1][0].date() != day.date():
            groups.append((day, []))
        groups[-1][1].append(item)

    out = []
    for day, day_items in groups:
        cards = "".join(render_card(i, color, now_utc) for i in day_items)
        out.append(
            '<div class="day">'
            f'<h2 class="day-label">{html.escape(fmt_day_label(day, today))}</h2>'
            f"{cards}</div>"
        )
    return "".join(out)


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

    # お気に入りタブは常に先頭。中身はブラウザの保存内容から JS で描画する
    tabs = [
        '<button class="tab" data-target="panel-fav" data-fav-tab="1" '
        f'style="--cat:{COLORS["main"]}">&#9733; お気に入り<span class="count">0</span></button>'
    ]
    panels = ['<section class="panel" id="panel-fav"></section>']

    for i, cat in enumerate(cats):
        cat_id = cat["id"]
        items = by_category.get(cat_id, [])
        active = " active" if i == 0 else ""
        tabs.append(
            f'<button class="tab{active}" data-target="panel-{e(cat_id)}" '
            f'style="--cat:{e(cat.get("color", COLORS["main"]))}">'
            f'{e(cat["name"])}<span class="count">{len(items)}</span></button>'
        )
        body = render_days(items, cat.get("color", COLORS["main"]), now_utc)
        panels.append(f'<section class="panel{active}" id="panel-{e(cat_id)}">{body}</section>')

    guide_total = sum(1 for items in by_category.values() for it in items if it.get("guide"))

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
.toolbar {{ display: flex; flex-wrap: wrap; gap: 6px 16px; padding: 9px 14px 0; }}
.toolbar label {{
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--ink); opacity: .8; cursor: pointer;
}}
.toolbar input {{ accent-color: var(--main); width: 15px; height: 15px; }}
.panel {{ display: none; padding: 6px 12px 10px; }}
.panel.active {{ display: block; }}
.day-label {{
  font-size: 12px; font-weight: bold; color: var(--ink); opacity: .55;
  margin: 14px 2px 7px; letter-spacing: .5px;
}}
.day:first-child .day-label {{ margin-top: 6px; }}
.card.guide {{ display: none; }}
body.show-guide .card.guide {{ display: block; }}
.card {{
  background: var(--white); border: 1px solid var(--line);
  border-left: 5px solid var(--main); border-radius: 12px;
  padding: 12px 14px; margin-bottom: 10px;
}}
.card-meta {{
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-size: 11px; margin-bottom: 6px;
}}
.actions {{ margin-left: auto; display: flex; gap: 2px; flex: 0 0 auto; }}
.act {{
  border: 0; background: none; cursor: pointer; line-height: 1;
  color: var(--ink); opacity: .4; font-size: 16px;
  /* 指で押しやすいよう余白で当たり判定を広げる(見た目は変えない) */
  padding: 8px; margin: -6px 0; border-radius: 6px; font-family: inherit;
}}
.act:hover {{ opacity: .9; background: var(--bg); }}
.act-fav.on {{ opacity: 1; color: var(--main); }}
.card.archived .act-arc {{ font-size: 11px; opacity: .75; }}
.time {{ color: var(--ink); opacity: .65; }}
.source {{ color: var(--ink); opacity: .65; }}
.badge {{
  border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: bold;
  color: var(--white); line-height: 1.2;
}}
.badge-new {{ background: var(--new); }}
.badge-video {{ background: var(--ink); }}
.badge-ai {{ background: var(--main); flex: 0 0 auto; }}
.badge-guide {{ background: var(--white); color: var(--ink); border: 1px solid var(--line); opacity: .8; }}
.card-title {{
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3;
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
<div class="toolbar">
  <label><input type="checkbox" id="showGuide"> 攻略・まとめも表示（{guide_total}）</label>
  <label><input type="checkbox" id="showArchive"> アーカイブを見る（<span id="arcCount">0</span>）</label>
</div>
{''.join(panels)}
<footer>6時間ごと自動更新 / feeds.yml で編集可</footer>
<script>
(function () {{
  var FAV_KEY = 'pokeboard.favs.v1';
  var ARC_KEY = 'pokeboard.arc.v1';
  var ARC_TTL_MS = 60 * 86400000;   // アーカイブ記録の保持期間
  var DAY_MS = 86400000;
  var WD = ['日', '月', '火', '水', '木', '金', '土'];

  function load(k) {{
    try {{ return JSON.parse(localStorage.getItem(k)) || {{}}; }} catch (e) {{ return {{}}; }}
  }}
  function save(k, v) {{
    try {{ localStorage.setItem(k, JSON.stringify(v)); }} catch (e) {{}}
  }}
  function esc(s) {{
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {{
      return {{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }}[c];
    }});
  }}
  // 閲覧者のタイムズーンに関係なくJSTで表示する
  function jst(ts) {{ return new Date(Date.parse(ts) + 9 * 3600000); }}
  function fmtTime(ts) {{
    var d = jst(ts), h = d.getUTCHours(), m = d.getUTCMinutes();
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
  }}
  function dayKey(ts) {{ return jst(ts).toISOString().slice(0, 10); }}
  function dayLabel(ts) {{
    var d = jst(ts), today = jst(new Date().toISOString());
    var diff = Math.round((Date.parse(dayKey(new Date().toISOString())) - Date.parse(dayKey(ts))) / DAY_MS);
    if (diff === 0) return '今日';
    if (diff === 1) return '昨日';
    return (d.getUTCMonth() + 1) + '月' + d.getUTCDate() + '日(' + WD[d.getUTCDay()] + ')';
  }}

  var favs = load(FAV_KEY);
  var arc = load(ARC_KEY);

  // 記録が無制限に増えないよう、古いアーカイブは忘れる
  var cutoff = Date.now() - ARC_TTL_MS, pruned = false;
  Object.keys(arc).forEach(function (id) {{
    if (!(arc[id] > cutoff)) {{ delete arc[id]; pruned = true; }}
  }});
  if (pruned) save(ARC_KEY, arc);

  var showGuide = document.getElementById('showGuide');
  var showArchive = document.getElementById('showArchive');
  var arcCount = document.getElementById('arcCount');
  var favPanel = document.getElementById('panel-fav');

  // カードのDOMから保存用のデータを取り出す(記事が配信から消えても残せるように)
  function readCard(card) {{
    var a = card.querySelector('.card-title');
    var sum = card.querySelector('.summary-text');
    var src = card.querySelector('.source');
    return {{
      id: card.dataset.id, ts: card.dataset.ts, type: card.dataset.type,
      color: card.style.borderLeftColor,
      title: a ? a.textContent : '', link: a ? a.href : '',
      source: src ? src.textContent : '',
      summary: sum ? sum.textContent : '',
      ai: !!card.querySelector('.badge-ai'),
      addedAt: Date.now()
    }};
  }}

  function cardHTML(it, archived) {{
    var b = '';
    if (Date.now() - Date.parse(it.ts) <= DAY_MS) b += '<span class="badge badge-new">NEW</span>';
    if (it.type === 'video') b += '<span class="badge badge-video">&#9654; 動画</span>';
    else if (it.type === 'x') b += '<span class="badge badge-video">X</span>';
    var sum = '';
    if (it.summary) {{
      sum = '<div class="summary">' + (it.ai ? '<span class="badge badge-ai">AI要約</span>' : '') +
        '<span class="summary-text">' + esc(it.summary) + '</span></div>';
    }}
    var src = it.source ? '<span class="source">' + esc(it.source) + '</span>' : '';
    return '<article class="card' + (archived ? ' archived' : '') + '" data-id="' + esc(it.id) +
      '" data-ts="' + esc(it.ts) + '" data-type="' + esc(it.type) +
      '" style="border-left-color:' + esc(it.color || '{c['main']}') + '">' +
      '<div class="card-meta">' + b + '<span class="time">' + fmtTime(it.ts) + '</span>' + src +
      '<span class="actions">' +
      '<button class="act act-fav on" type="button" title="お気に入りを外す" aria-label="お気に入りを外す">&#9733;</button>' +
      '<button class="act act-arc" type="button" title="' + (archived ? '元に戻す' : 'アーカイブ') + '">' +
      (archived ? '戻す' : '&#10005;') + '</button></span></div>' +
      '<a class="card-title" href="' + esc(it.link) + '" target="_blank" rel="noopener">' +
      esc(it.title) + '</a>' + sum + '</article>';
  }}

  function renderFavPanel() {{
    var arcMode = showArchive.checked;
    var list = Object.keys(favs).map(function (id) {{ return favs[id]; }})
      .filter(function (it) {{ return arcMode ? !!arc[it.id] : !arc[it.id]; }})
      .sort(function (a, b) {{ return Date.parse(b.ts) - Date.parse(a.ts); }});

    if (!list.length) {{
      favPanel.innerHTML = '<p class="empty">' + (arcMode
        ? 'アーカイブしたお気に入りはありません'
        : 'お気に入りはまだありません。記事の &#9734; を押すと追加されます。') + '</p>';
      return list.length;
    }}
    var out = '', prev = null;
    list.forEach(function (it) {{
      var k = dayKey(it.ts);
      if (k !== prev) {{
        if (prev !== null) out += '</div>';
        out += '<div class="day"><h2 class="day-label">' + esc(dayLabel(it.ts)) + '</h2>';
        prev = k;
      }}
      out += cardHTML(it, arcMode);
    }});
    favPanel.innerHTML = out + '</div>';
    return list.length;
  }}

  function refresh() {{
    var arcMode = showArchive.checked;
    var guideOn = showGuide.checked;
    document.body.classList.toggle('show-guide', guideOn);

    document.querySelectorAll('.panel:not(#panel-fav)').forEach(function (panel) {{
      var shown = 0;
      panel.querySelectorAll('.card').forEach(function (card) {{
        var archived = !!arc[card.dataset.id];
        var visible = arcMode ? archived
          : (!archived && (guideOn || !card.classList.contains('guide')));
        card.style.display = visible ? '' : 'none';
        card.classList.toggle('archived', archived);
        var fav = card.querySelector('.act-fav');
        var isFav = !!favs[card.dataset.id];
        fav.classList.toggle('on', isFav);
        fav.innerHTML = isFav ? '&#9733;' : '&#9734;';
        fav.title = isFav ? 'お気に入りを外す' : 'お気に入り';
        var ab = card.querySelector('.act-arc');
        ab.innerHTML = archived ? '戻す' : '&#10005;';
        ab.title = archived ? '元に戻す' : 'アーカイブ';
        if (visible) shown++;
      }});
      panel.querySelectorAll('.day').forEach(function (day) {{
        var any = Array.prototype.some.call(day.querySelectorAll('.card'), function (c) {{
          return c.style.display !== 'none';
        }});
        day.style.display = any ? '' : 'none';
      }});
      var msg = panel.querySelector('.empty');
      if (shown === 0) {{
        if (!msg) {{ msg = document.createElement('p'); msg.className = 'empty'; panel.appendChild(msg); }}
        msg.textContent = arcMode ? 'このカテゴリにアーカイブした記事はありません'
          : (guideOn ? '記事がありません'
            : '新着ニュースはありません。上の「攻略・まとめも表示」で表示できます。');
        msg.hidden = false;
      }} else if (msg) {{
        msg.hidden = true;
      }}
      var tab = document.querySelector('.tab[data-target="' + panel.id + '"]');
      if (tab) tab.querySelector('.count').textContent = shown;
    }});

    var favShown = renderFavPanel();
    document.querySelector('.tab[data-fav-tab]').querySelector('.count').textContent = favShown;
    arcCount.textContent = Object.keys(arc).length;
  }}

  document.querySelectorAll('.tab').forEach(function (tab) {{
    tab.addEventListener('click', function () {{
      document.querySelectorAll('.tab').forEach(function (t) {{ t.classList.remove('active'); }});
      document.querySelectorAll('.panel').forEach(function (p) {{ p.classList.remove('active'); }});
      tab.classList.add('active');
      document.getElementById(tab.dataset.target).classList.add('active');
    }});
  }});

  document.addEventListener('click', function (ev) {{
    var btn = ev.target.closest('.act');
    if (!btn) return;
    var card = btn.closest('.card');
    var id = card.dataset.id;
    if (btn.classList.contains('act-fav')) {{
      if (favs[id]) delete favs[id]; else favs[id] = readCard(card);
      save(FAV_KEY, favs);
    }} else {{
      if (arc[id]) delete arc[id]; else arc[id] = Date.now();
      save(ARC_KEY, arc);
    }}
    refresh();
  }});

  showGuide.addEventListener('change', refresh);
  showArchive.addEventListener('change', refresh);
  refresh();
}})();
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
    guide_filter = config.get("guide_filter", {}) or {}
    by_category = collect_items(config)
    for cat_id, items in by_category.items():
        trimmed = dedupe_and_trim(items, max_age_days, max_items)
        for item in trimmed:
            item["guide"] = is_guide_item(item, guide_filter)
        by_category[cat_id] = trimmed
        n_guide = sum(1 for i in trimmed if i["guide"])
        log.info("カテゴリ %s: %d 件 (うち攻略・まとめ %d 件)", cat_id, len(trimmed), n_guide)

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
