#!/usr/bin/env python3
"""Generate the public static archive of daily IT security digests.

Reads YYYY-MM-DD_zh.md and YYYY-MM-DD_ja.txt from the digest output
directory and writes a complete site into dist/. Idempotent: dist/ is
cleared and rebuilt from scratch on every run.

URL map:
  /                  latest edition, Japanese (default)
  /zh/               latest edition, Chinese
  /YYYY-MM-DD/ja.html  permalink, Japanese (also used by feed.xml)
  /YYYY-MM-DD/zh.html  permalink, Chinese

Usage:
  python3 build.py
  python3 build.py --input /path/to/out --output /path/to/dist
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

SITE_TITLE_JA = "日次セキュリティ情報"
SITE_TITLE_ZH = "每日 IT 安全摘要"
SITE_TITLE = f"{SITE_TITLE_JA} / {SITE_TITLE_ZH}"
BASE_URL = "https://secnews-list-gcu.pages.dev"
FOOTER_LINE = "本ページは自動収集した情報をもとに作成しています。"
DISCLAIMER_KEEP = (
    "各プロジェクトの状況に応じて、対応要否をご判断ください。",
    "本メールは自動収集した情報をもとに作成しています。",
)

WEEKDAY_JA = "月火水木金土日"
WEEKDAY_ZH = "一二三四五六日"

# Lines that belong in the internal Chinese brief, not on a public page.
ZH_INTERNAL_PREFIXES = ("来源：", "来源:", "來源：", "來源:")
# The Japanese mailing-list subscribe/unsubscribe footer.
JA_OMIT_MARKERS = ("配信の登録・解除", "Web版（過去の配信")

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})$")
ITEM_RE = re.compile(r"^(\d+)\.\s+(.*)$")
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
INLINE_URL_RE = re.compile(r"(https?://[^\s<>\]）)」]+)", re.IGNORECASE)
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
CODE_RE = re.compile(r"`([^`]+)`")
SUBJECT_CRITICAL_RE = re.compile(r"緊急\s*(\d+)\s*件")
SUBJECT_HIGH_RE = re.compile(r"重要\s*(\d+)\s*件")
SUBJECT_INFO_RE = re.compile(r"参考情報?\s*(\d+)\s*件")
SECTION_RE = re.compile(
    r"^■\s*(?:(🔴|🟠|⚪)\s*)?(緊急|重要|参考情報)(?:（([^）]+)）)?"
)

SECTION_CLASS = {
    "緊急": "critical",
    "重要": "high",
    "参考情報": "info",
}


def weekday_ja(date: dt.date) -> str:
    return WEEKDAY_JA[date.weekday()]


def weekday_zh(date: dt.date) -> str:
    return WEEKDAY_ZH[date.weekday()]


def format_date_ja(date: dt.date) -> str:
    return f"{date.strftime('%Y/%m/%d')}（{weekday_ja(date)}）"


def format_date_zh(date: dt.date) -> str:
    return f"{date.strftime('%Y-%m-%d')}（{weekday_zh(date)}）"


def zh_heading_date(zh_text: str, date: dt.date) -> str:
    """Keep the weekday character used in the source title when present."""
    first = zh_text.strip().splitlines()[0] if zh_text.strip() else ""
    m = re.search(r"(\d{4}-\d{2}-\d{2}（.?）)", first)
    if m:
        return m.group(1)
    return format_date_zh(date)


def attr(value: str) -> str:
    return html.escape(value, quote=True)


def is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def link_tag(url: str, label: str | None = None, css: str = "") -> str:
    if not is_http_url(url):
        return html.escape(label or url)
    text = label if label is not None else url
    cls = f' class="{css}"' if css else ""
    return (
        f'<a href="{attr(url)}" target="_blank" rel="noopener noreferrer"{cls}>'
        f"{text}</a>"
    )


def render_inline(text: str, autolink: bool = False) -> str:
    """Escape text and apply a small Markdown-inline subset."""
    tokens: list[str] = []

    def stash(fragment: str) -> str:
        tokens.append(fragment)
        return f"\x00{len(tokens) - 1}\x00"

    def restore(s: str) -> str:
        def inject(match: re.Match[str]) -> str:
            return tokens[int(match.group(1))]

        return re.sub(r"\x00(\d+)\x00", inject, s)

    working = text

    def replace_md_link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if not is_http_url(url):
            return html.escape(match.group(0))
        inner = render_inline(label, autolink=False)
        return stash(link_tag(url, inner))

    working = MD_LINK_RE.sub(replace_md_link, working)

    if autolink:
        def replace_url(match: re.Match[str]) -> str:
            url = match.group(1).rstrip("。．、,)")
            # Put trailing punctuation back outside the link if we stripped it.
            trailing = match.group(1)[len(url):]
            if not is_http_url(url):
                return html.escape(match.group(0))
            return stash(link_tag(url) + html.escape(trailing))

        working = INLINE_URL_RE.sub(replace_url, working)

    def replace_code(match: re.Match[str]) -> str:
        return stash(f"<code>{html.escape(match.group(1))}</code>")

    working = CODE_RE.sub(replace_code, working)

    def replace_bold(match: re.Match[str]) -> str:
        return stash(f"<strong>{html.escape(match.group(1))}</strong>")

    working = BOLD_RE.sub(replace_bold, working)

    return restore(html.escape(working))


def should_omit_ja_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return any(marker in stripped for marker in JA_OMIT_MARKERS)


def should_omit_zh_line(line: str) -> bool:
    stripped = line.strip()
    return any(stripped.startswith(prefix) for prefix in ZH_INTERNAL_PREFIXES)


def parse_counts(subject: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if m := SUBJECT_CRITICAL_RE.search(subject):
        counts["critical"] = int(m.group(1))
    if m := SUBJECT_HIGH_RE.search(subject):
        counts["high"] = int(m.group(1))
    if m := SUBJECT_INFO_RE.search(subject):
        counts["info"] = int(m.group(1))
    return counts


def counts_html(counts: dict[str, int], lang: str = "ja") -> str:
    """Compact severity counts for the date picker; labels follow the page language."""
    ui = UI[lang]
    labels = {
        "critical": ui["critical"],
        "high": ui["high"],
        "info": ui["info_short"],
    }
    unit = ui["unit"]
    parts: list[str] = []
    for key in ("critical", "high", "info"):
        if key not in counts:
            continue
        parts.append(
            f'<span class="count {key}">{html.escape(labels[key])} {counts[key]}{html.escape(unit)}</span>'
        )
    if not parts:
        return ""
    return '<span class="counts">' + '<span class="dot" aria-hidden="true">・</span>'.join(parts) + "</span>"


# ---------------------------------------------------------------------------
# Japanese email → HTML
# ---------------------------------------------------------------------------

def parse_ja(text: str) -> dict:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # First line is the subject; then a blank line; then the body.
    subject = raw_lines[0].strip() if raw_lines else ""
    lines = [ln.rstrip() for ln in raw_lines[1:]]
    while lines and not lines[0].strip():
        lines.pop(0)
    # Drop the mailing-list subscribe/unsubscribe footer (and leftover blanks).
    cleaned: list[str] = []
    for ln in lines:
        if should_omit_ja_line(ln):
            continue
        cleaned.append(ln)
    # Trim trailing empty lines.
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    intro: list[str] = []
    sections: list[dict] = []
    disclaimers: list[str] = []
    current_section: dict | None = None
    current_item: dict | None = None

    def flush_item() -> None:
        nonlocal current_item
        if current_section is not None and current_item is not None:
            current_section["items"].append(current_item)
        current_item = None

    def flush_section() -> None:
        nonlocal current_section
        flush_item()
        if current_section is not None:
            sections.append(current_section)
        current_section = None

    i = 0
    n = len(cleaned)
    # Intro, until the first section header or a disclaimer.
    while i < n:
        ln = cleaned[i]
        if SECTION_RE.match(ln.strip()):
            break
        if ln.strip() in DISCLAIMER_KEEP:
            break
        intro.append(ln)
        i += 1
    while intro and not intro[-1].strip():
        intro.pop()

    while i < n:
        ln = cleaned[i]
        stripped = ln.strip()
        if not stripped:
            i += 1
            continue
        if stripped in DISCLAIMER_KEEP:
            flush_section()
            disclaimers.append(stripped)
            i += 1
            continue
        sec = SECTION_RE.match(stripped)
        if sec:
            flush_section()
            name = sec.group(2)
            current_section = {
                "name": name,
                "level": SECTION_CLASS.get(name, "info"),
                "full": stripped,
                "items": [],
            }
            i += 1
            continue
        item = ITEM_RE.match(stripped)
        if item and current_section is not None:
            flush_item()
            number = int(item.group(1))
            rest = item.group(2).strip()
            title = ""
            body_lines: list[str] = []
            # **Title** on its own line, or a title line with no sentence end.
            bold_only = re.fullmatch(r"\*\*(.+?)\*\*", rest)
            if bold_only:
                title = bold_only.group(1).strip()
            elif "。" not in rest and "．" not in rest:
                title = rest
            else:
                body_lines.append(rest)
            current_item = {
                "number": number,
                "title": title,
                "body_lines": body_lines,
                "urls": [],
            }
            i += 1
            continue
        if current_item is not None and URL_RE.match(stripped):
            url = stripped
            # Guard against a trailing full stop stuck to the URL.
            url = url.rstrip("。．")
            if is_http_url(url):
                current_item["urls"].append(url)
            i += 1
            continue
        if current_item is not None:
            current_item["body_lines"].append(stripped)
            i += 1
            continue
        # Unclassified leftover: keep with intro-like notes.
        intro.append(ln)
        i += 1

    flush_section()
    return {
        "subject": subject,
        "intro": intro,
        "sections": sections,
        "disclaimers": disclaimers,
        "counts": parse_counts(subject),
    }


ZH_PERSONAL_MARKERS = ("你", "提醒过", "提醒可能", "Jacky")


def strip_personal_sentences(line: str) -> str:
    """Drop reader-specific asides (e.g. "你在用 Notion…") from public pages."""
    if not any(m in line for m in ZH_PERSONAL_MARKERS):
        return line
    pieces = re.split(r"(?<=[。！？])", line)
    kept = [pc for pc in pieces if not (any(m in pc for m in ZH_PERSONAL_MARKERS) and "](" not in pc)]
    return "".join(kept).rstrip()


# ---------------------------------------------------------------------------
# Shared item model + one article template for ja and zh
# ---------------------------------------------------------------------------

CVE_RE = re.compile(r"CVE-\d{4}-\d+", re.IGNORECASE)
LEVELS = ("critical", "high", "info")
UI = {
    "ja": {
        "critical": "緊急",
        "high": "重要",
        "info": "参考情報",
        "info_short": "参考",
        "source": "ソース",
        "unit": "件",
        "stack": "関連スタック",
    },
    "zh": {
        "critical": "紧急",
        "high": "重要",
        "info": "参考",
        "info_short": "参考",
        "source": "来源",
        "unit": "条",
        "stack": "相关技术栈",
    },
}


def display_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return url
    if host.startswith("www."):
        host = host[4:]
    return host or url


def norm_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return url.rstrip("/").lower()
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


def extract_cves(*texts: str) -> set[str]:
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in CVE_RE.findall(text):
            found.add(match.upper())
    return found


def collect_urls(*texts: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        for _, url in MD_LINK_RE.findall(text):
            if url not in seen and is_http_url(url):
                seen.add(url)
                urls.append(url)
        for raw in INLINE_URL_RE.findall(text):
            url = raw.rstrip("。．、,)")
            if url not in seen and is_http_url(url):
                seen.add(url)
                urls.append(url)
    return urls


def make_sources(urls: list[str]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for url in urls:
        key = norm_url(url)
        if key in seen:
            continue
        seen.add(key)
        out.append({"url": url, "domain": display_domain(url)})
    return out


def split_ja_title(text: str) -> tuple[str, str]:
    """Turn a single JA paragraph into (title, explanation)."""
    text = text.strip()
    if "について" in text:
        i = text.index("について")
        title = text[:i].rstrip("、，, ")
        expl = text[i + len("について"):].lstrip("、，, ")
        if title:
            return title, expl
    match = re.match(r"(.+?[。．])(.*)$", text, re.S)
    if match and match.group(2).strip():
        return match.group(1).rstrip("。． "), match.group(2).strip()
    return text, ""


def ja_structured_sections(parsed: dict) -> tuple[list[dict], str]:
    period = ""
    for ln in parsed.get("intro") or []:
        stripped = ln.strip()
        if stripped.startswith("対象期間"):
            period = stripped
            break
    sections: list[dict] = []
    for sec in parsed.get("sections") or []:
        items: list[dict] = []
        for raw in sec["items"]:
            title = (raw.get("title") or "").strip()
            expl = " ".join(s.strip() for s in (raw.get("body_lines") or []) if s.strip())
            if not title and expl:
                title, expl = split_ja_title(expl)
            urls = list(raw.get("urls") or [])
            for extra in collect_urls(title, expl):
                if extra not in urls:
                    urls.append(extra)
            items.append({
                "orig": raw.get("number"),
                "severity": sec["level"],
                "title": title,
                "explanation": expl,
                "sources": make_sources(urls),
                "urls": {norm_url(u) for u in urls},
                "cves": extract_cves(title, expl, " ".join(urls)),
                "stack": False,
            })
        if items:
            sections.append({"level": sec["level"], "name": sec.get("name", ""), "items": items})
    return sections, period


def split_zh_item(rest: str) -> tuple[str, str, list[str]]:
    urls = collect_urls(rest)
    match = re.match(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)\s*(?:(?:—|–|－|-)\s*)?(.*)$",
        rest,
        re.S,
    )
    if match:
        title = match.group(1).strip()
        expl = re.sub(r"^(?:—|–|－)\s*", "", (match.group(3) or "").strip())
        primary = match.group(2)
        urls = [primary] + [u for u in urls if u != primary]
        return title, expl, urls
    return rest, "", urls


def parse_zh_content(text: str) -> tuple[list[dict], str, list[str]]:
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [strip_personal_sentences(ln.rstrip()) for ln in raw_lines]
    period = ""
    notes: list[str] = []
    items: list[dict] = []
    i = 0
    n = len(lines)
    if i < n and lines[i].strip():
        i += 1
    if i < n and lines[i].strip().startswith("覆盖时段"):
        period = lines[i].strip()
        i += 1
    buf: list[str] = []

    def flush_notes() -> None:
        if buf:
            notes.append(" ".join(buf))
            buf.clear()

    while i < n:
        stripped = lines[i].strip()
        if should_omit_zh_line(stripped):
            i += 1
            continue
        if not stripped:
            flush_notes()
            i += 1
            continue
        item_match = ITEM_RE.match(stripped)
        if item_match:
            flush_notes()
            orig = int(item_match.group(1))
            rest = item_match.group(2)
            i += 1
            while i < n:
                nxt = lines[i].strip()
                if (not nxt or ITEM_RE.match(nxt) or should_omit_zh_line(nxt)
                        or nxt.startswith("未列入统计")):
                    break
                rest += " " + nxt
                i += 1
            rest = rest.strip()
            stack = rest.startswith("🔴")
            if stack:
                rest = rest[1:].lstrip()
            title, expl, urls = split_zh_item(rest)
            items.append({
                "orig": orig,
                "title": title,
                "explanation": expl,
                "sources": make_sources(urls),
                "urls": {norm_url(u) for u in urls},
                "cves": extract_cves(title, expl, " ".join(urls)),
                "stack": stack,
                "severity": None,
            })
            continue
        buf.append(stripped)
        i += 1
    flush_notes()
    return items, period, notes


def align_zh_sections(ja_sections: list[dict], zh_items: list[dict]) -> list[dict]:
    """Map zh items onto ja severity/order by unique URL or CVE; extras go to 参考."""
    ja_flat: list[tuple[str, dict]] = []
    for sec in ja_sections:
        for item in sec["items"]:
            ja_flat.append((sec["level"], item))

    url_freq: dict[str, int] = {}
    for _, item in ja_flat:
        for url in item["urls"]:
            url_freq[url] = url_freq.get(url, 0) + 1

    def score(zh: dict, ja: dict) -> int:
        shared = zh["urls"] & ja["urls"]
        unique = {url for url in shared if url_freq.get(url, 0) == 1}
        cves = zh["cves"] & ja["cves"]
        return 10 * len(unique) + 8 * len(cves) + 2 * len(shared)

    pairs: list[tuple[int, int, int]] = []
    for zi, zh in enumerate(zh_items):
        for ji, (_, ja) in enumerate(ja_flat):
            s = score(zh, ja)
            if s >= 8:
                pairs.append((s, zi, ji))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_z: set[int] = set()
    used_j: set[int] = set()
    j_to_z: dict[int, int] = {}
    for _, zi, ji in pairs:
        if zi in used_z or ji in used_j:
            continue
        used_z.add(zi)
        used_j.add(ji)
        j_to_z[ji] = zi

    grouped: dict[str, list[dict]] = {level: [] for level in LEVELS}
    for ji, (level, _) in enumerate(ja_flat):
        zi = j_to_z.get(ji)
        if zi is None:
            continue
        item = dict(zh_items[zi])
        item["severity"] = level
        grouped[level].append(item)
    extras = [zh_items[i] for i in range(len(zh_items)) if i not in used_z]
    for zh in extras:
        item = dict(zh)
        item["severity"] = "info"
        grouped["info"].append(item)

    return [{"level": level, "items": grouped[level]} for level in LEVELS if grouped[level]]


def mapping_report(date: dt.date, ja_sections: list[dict], zh_items: list[dict], zh_sections: list[dict]) -> str:
    ja_titles = [it["title"] for sec in ja_sections for it in sec["items"]]
    matched = sum(len(sec["items"]) for sec in zh_sections if sec["level"] != "info")
    info_sec = next((s for s in zh_sections if s["level"] == "info"), None)
    # extras are info items whose orig numbers weren't required; just count zh vs ja
    ja_n = sum(len(s["items"]) for s in ja_sections)
    zh_n = len(zh_items)
    return f"{date.isoformat()}: ja items {ja_n}, zh items {zh_n}, zh sections {[ (s['level'], len(s['items'])) for s in zh_sections ]}"


def section_counts(sections: list[dict]) -> dict[str, int]:
    return {sec["level"]: len(sec["items"]) for sec in sections}


def render_card(item: dict, number: int, ui: dict) -> str:
    title = item["title"] or (item["explanation"][:80] if item["explanation"] else "")
    expl = item["explanation"]
    sources = item["sources"]
    primary = sources[0]["url"] if sources else ""
    badge = ui[item["severity"]]
    stack = ""
    if item.get("stack"):
        stack = f'<span class="stack-flag" title="{attr(ui["stack"])}">🔴</span>'
    if primary and is_http_url(primary):
        title_html = f"<h3>{link_tag(primary, render_inline(title), css='card-title-link')}</h3>"
    else:
        title_html = f"<h3>{render_inline(title)}</h3>"
    expl_html = f'<p class="expl">{render_inline(expl)}</p>' if expl else ""
    src_html = ""
    if sources:
        domain_count: dict[str, int] = {}
        for src in sources:
            domain_count[src["domain"]] = domain_count.get(src["domain"], 0) + 1

        def source_label(src: dict) -> str:
            if domain_count[src["domain"]] <= 1:
                return src["domain"]
            tail = urlparse(src["url"]).path.rstrip("/").split("/")[-1]
            return f"{src['domain']}/{tail}" if tail else src["domain"]

        links = ' <span class="src-sep" aria-hidden="true">·</span> '.join(
            link_tag(src["url"], html.escape(source_label(src)), css="src-link")
            for src in sources
        )
        src_html = (
            f'<p class="sources"><span class="src-label">{html.escape(ui["source"])}:</span> '
            f"{links}</p>"
        )
    return (
        f'<li class="card" value="{number}">\n'
        f'  <div class="card-meta">'
        f'<span class="num">{number}</span>'
        f'<span class="badge {item["severity"]}">{html.escape(badge)}</span>'
        f"{stack}</div>\n"
        f"  {title_html}\n"
        f"  {expl_html}\n"
        f"  {src_html}\n"
        f"</li>"
    )


def render_article(view: dict, lang: str) -> str:
    ui = UI["ja" if lang == "ja" else "zh"]
    parts = ["<article>"]
    parts.append('<header class="summary">')
    parts.append(f'<h1>{html.escape(view["heading"])}</h1>')
    chips: list[str] = []
    counts = section_counts(view["sections"])
    for level in LEVELS:
        if level not in counts:
            continue
        short = ui[level] if level != "info" else ui["info_short"]
        chips.append(
            f'<a class="count-chip {level}" href="#sev-{level}">'
            f'<span class="badge {level}">{html.escape(short)}</span>'
            f" {counts[level]}{ui['unit']}</a>"
        )
    if chips:
        parts.append('<p class="count-row">' + "".join(chips) + "</p>")
    if view.get("period"):
        parts.append(f'<p class="period">{render_inline(view["period"])}</p>')
    parts.append("</header>")

    number = 1
    for sec in view["sections"]:
        level = sec["level"]
        name = ui[level]
        count = len(sec["items"])
        parts.append(
            f'<section class="sev {level}" id="sev-{level}" tabindex="-1" aria-label="{attr(name)}">'
        )
        parts.append(
            f'<h2 class="sev-heading"><span class="badge {level}">{html.escape(name)}</span>'
            f'<span class="sev-count">{count}{ui["unit"]}</span></h2>'
        )
        parts.append(f'<ol class="cards" start="{number}">')
        for item in sec["items"]:
            parts.append(render_card(item, number, ui))
            number += 1
        parts.append("</ol>")
        parts.append("</section>")

    if view.get("notes"):
        parts.append('<div class="notes">')
        for para in view["notes"]:
            if para.strip():
                parts.append(f"<p>{render_inline(para)}</p>")
        parts.append("</div>")
    parts.append("</article>")
    return "\n".join(parts)


def build_view(rec: dict, lang: str) -> dict:
    date = rec["date"]
    ja_sections, period_ja = ja_structured_sections(rec["parsed_ja"])
    if lang == "ja":
        return {
            "heading": format_date_ja(date),
            "period": period_ja,
            "sections": ja_sections,
            "notes": [],
        }
    zh_items, period_zh, notes = parse_zh_content(rec["zh"])
    zh_sections = align_zh_sections(ja_sections, zh_items)
    print("  " + mapping_report(date, ja_sections, zh_items, zh_sections), flush=True)
    return {
        "heading": zh_heading_date(rec["zh"], date),
        "period": period_zh or period_ja,
        "sections": zh_sections,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------

def edition_href(date: dt.date, lang: str, latest: dt.date) -> str:
    """Pretty URL for an edition. The latest day maps to / or /zh/."""
    if date == latest:
        return "/" if lang == "ja" else "/zh/"
    return f"/{date.isoformat()}/{lang}.html"


def permalink_href(date: dt.date, lang: str) -> str:
    return f"/{date.isoformat()}/{lang}.html"


def date_label(rec: dict, lang: str) -> str:
    if lang == "ja":
        return format_date_ja(rec["date"])
    return zh_heading_date(rec["zh"], rec["date"])


def header_nav(
    *,
    date: dt.date,
    lang: str,
    editions: list[dict],
    prev_date: dt.date | None,
    next_date: dt.date | None,
) -> str:
    latest = editions[0]["date"]
    current = next(rec for rec in editions if rec["date"] == date)
    if lang == "ja":
        current_lang, current_label = "ja", "日本語"
        other_lang, other_label = "zh-CN", "中文"
        picker_label = "日付を選ぶ"
        prev_label, next_label = "前の日", "次の日"
        lang_label = "言語"
    else:
        current_lang, current_label = "zh-CN", "中文"
        other_lang, other_label = "ja", "日本語"
        picker_label = "选择日期"
        prev_label, next_label = "前一天", "后一天"
        lang_label = "语言"

    parts: list[str] = []
    parts.append('<div class="header-tools">')
    parts.append(f'<nav class="date-nav" aria-label="{attr(picker_label)}">')

    if prev_date:
        href = edition_href(prev_date, lang, latest)
        label = prev_date.isoformat()
        parts.append(
            f'<a class="pager-link prev" href="{attr(href)}" rel="prev" '
            f'title="{attr(label)}" aria-label="{attr(prev_label + " " + label)}">←</a>'
        )
    else:
        parts.append(
            f'<span class="pager-link prev disabled" aria-disabled="true" '
            f'aria-label="{attr(prev_label)}">←</span>'
        )

    parts.append('<details class="date-picker">')
    summary_label = date_label(current, lang)
    summary_counts = counts_html(current["parsed_ja"]["counts"], lang)
    parts.append(
        f'<summary aria-label="{attr(picker_label + " " + summary_label)}">'
        f'<span class="date-summary-label">{html.escape(summary_label)}</span>'
        f"{summary_counts}"
        f"</summary>"
    )
    parts.append('<div class="date-menu">')
    parts.append("<ul>")
    for i, rec in enumerate(editions):
        href = edition_href(rec["date"], lang, latest)
        is_current = rec["date"] == date
        is_latest = i == 0
        label = date_label(rec, lang)
        counts = counts_html(rec["parsed_ja"]["counts"], lang)
        badge = '<span class="badge-latest">最新</span>' if is_latest else ""
        current_attr = ' aria-current="page"' if is_current else ""
        cls = " is-current" if is_current else ""
        parts.append(
            f'<li><a href="{attr(href)}"{current_attr} class="date-item{cls}">'
            f'<span class="date-item-label">{html.escape(label)} {badge}</span>'
            f"{counts}</a></li>"
        )
    parts.append("</ul>")
    parts.append("</div>")
    parts.append("</details>")

    if next_date:
        href = edition_href(next_date, lang, latest)
        label = next_date.isoformat()
        parts.append(
            f'<a class="pager-link next" href="{attr(href)}" rel="next" '
            f'title="{attr(label)}" aria-label="{attr(next_label + " " + label)}">→</a>'
        )
    else:
        parts.append(
            f'<span class="pager-link next disabled" aria-disabled="true" '
            f'aria-label="{attr(next_label)}">→</span>'
        )
    parts.append("</nav>")

    other_href = edition_href(date, "zh" if lang == "ja" else "ja", latest)
    parts.append(f'<nav class="lang-toggle" aria-label="{attr(lang_label)}">')
    if lang == "ja":
        parts.append(
            f'<span class="current" lang="{current_lang}" aria-current="page">{current_label}</span>'
        )
        parts.append(
            f'<a href="{attr(other_href)}" lang="{other_lang}" hreflang="{other_lang}">{other_label}</a>'
        )
    else:
        parts.append(
            f'<a href="{attr(other_href)}" lang="{other_lang}" hreflang="{other_lang}">{other_label}</a>'
        )
        parts.append(
            f'<span class="current" lang="{current_lang}" aria-current="page">{current_label}</span>'
        )
    parts.append("</nav>")

    if lang == "ja":
        theme_to_light = "ライトモードに切り替え"
        theme_to_dark = "ダークモードに切り替え"
    else:
        theme_to_light = "切换到浅色模式"
        theme_to_dark = "切换到深色模式"
    parts.append(
        f'<button type="button" class="theme-toggle" data-theme-toggle hidden '
        f'aria-label="{attr(theme_to_dark)}" '
        f'data-label-light="{attr(theme_to_light)}" '
        f'data-label-dark="{attr(theme_to_dark)}">'
        f'<span class="theme-icon" aria-hidden="true">☾</span>'
        f"</button>"
    )
    parts.append("</div>")
    return "\n".join(parts)


def wrap_page(
    *,
    title: str,
    lang: str,
    body: str,
    path: str,
    description: str = "",
    date: dt.date,
    editions: list[dict],
    prev_date: dt.date | None,
    next_date: dt.date | None,
) -> str:
    desc = description or SITE_TITLE
    canonical = BASE_URL + path
    if path in ("/", "/zh/"):
        hreflang = (
            f'  <link rel="alternate" hreflang="ja" href="{attr(BASE_URL + "/")}">\n'
            f'  <link rel="alternate" hreflang="zh-CN" href="{attr(BASE_URL + "/zh/")}">\n'
            f'  <link rel="alternate" hreflang="x-default" href="{attr(BASE_URL + "/")}">\n'
        )
    else:
        day = date.isoformat()
        hreflang = (
            f'  <link rel="alternate" hreflang="ja" href="{attr(BASE_URL + "/" + day + "/ja.html")}">\n'
            f'  <link rel="alternate" hreflang="zh-CN" href="{attr(BASE_URL + "/" + day + "/zh.html")}">\n'
        )
    home = "/" if lang == "ja" else "/zh/"
    title_cls = "lang-ja" if lang == "ja" else "lang-zh"
    tools = header_nav(
        date=date,
        lang=lang,
        editions=editions,
        prev_date=prev_date,
        next_date=next_date,
    )
    if lang == "ja":
        footer_lines = (
            "各プロジェクトの状況に応じて、対応要否をご判断ください。",
            FOOTER_LINE,
        )
    else:
        footer_lines = (
            "请根据各项目的情况，自行判断是否需要应对。",
            "本页根据自动收集的信息整理。",
        )
    footer_html = "\n      ".join(f"<p>{html.escape(line)}</p>" for line in footer_lines)
    return f"""<!DOCTYPE html>
<html lang="{attr(lang if lang == "ja" else "zh-CN")}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <meta name="description" content="{attr(desc)}">
  <meta name="color-scheme" content="light dark">
  <link rel="canonical" href="{attr(canonical)}">
{hreflang}  <script src="/theme.js"></script>
  <link rel="stylesheet" href="/style.css">
  <link rel="alternate" type="application/atom+xml" title="{attr(SITE_TITLE)}" href="/feed.xml">
</head>
<body>
  <header class="site-header">
    <div class="inner header-bar">
      <a class="site-title {title_cls}" href="{attr(home)}">
        <span class="ja">{html.escape(SITE_TITLE_JA)}</span>
        <span class="sep" aria-hidden="true">/</span>
        <span class="zh" lang="zh-CN">{html.escape(SITE_TITLE_ZH)}</span>
      </a>
      {tools}
    </div>
  </header>
  <main class="inner">
    {body}
  </main>
  <footer class="site-footer">
    <div class="inner">
      {footer_html}
      <div class="subscribe-slot" id="subscribe" aria-hidden="true"></div>
    </div>
  </footer>
</body>
</html>
"""


CSS = r"""
/* Purpose-driven briefing layout.
   Tokens are named for domain purpose (severity, scan hierarchy, measure),
   not decoration. No external fonts or third-party scripts. */
:root {
  --font-sans: "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic UI", "Yu Gothic",
               "Noto Sans JP", "PingFang SC", "Hiragino Sans GB", "Noto Sans SC",
               "Microsoft YaHei", "Segoe UI", system-ui, sans-serif;
  --font-mono: "SFMono-Regular", "Menlo", "Consolas", "Liberation Mono", ui-monospace, monospace;

  --fs-kicker: 0.75rem;
  --fs-meta: 0.8rem;
  --fs-body: 17px;
  --fs-title: 1.14rem;
  --fs-display: 1.7rem;
  --lh-body: 1.8;
  --lh-title: 1.45;
  --lh-display: 1.22;

  --s-1: 0.25rem;
  --s-2: 0.5rem;
  --s-3: 0.75rem;
  --s-4: 1rem;
  --s-5: 1.5rem;
  --s-6: 2rem;
  --s-7: 3rem;
  --measure: 45rem;

  --bg: #f2f3f4;
  --bg-paper: #ffffff;
  --bg-mute: #e8eaec;
  --text: #161616;
  --text-secondary: #3a3d40;
  --text-meta: #5d6166;
  --rule: #d5d7da;
  --ink: #111213;
  --link: #0b4f8a;
  --link-hover: #083862;
  --accent: #1a1d21;

  --sev-critical: #b42318;
  --sev-critical-soft: #f8e8e6;
  --sev-high: #b54708;
  --sev-high-soft: #f8edd9;
  --sev-info: #3f4a55;
  --sev-info-soft: #e8ecf0;
  --focus: #0b4f8a;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #121416;
    --bg-paper: #1c1f24;
    --bg-mute: #262a31;
    --text: #eeeae4;
    --text-secondary: #cfc9c0;
    --text-meta: #a39c93;
    --rule: #343a42;
    --ink: #f3efe8;
    --link: #8ec8f6;
    --link-hover: #c5e3fb;
    --accent: #e8eef4;
    --sev-critical: #f0a098;
    --sev-critical-soft: #3a1d1b;
    --sev-high: #f0be86;
    --sev-high-soft: #3a2a16;
    --sev-info: #c5d0db;
    --sev-info-soft: #1e252c;
    --focus: #8ec8f6;
    color-scheme: dark;
  }
}
[data-theme="dark"] {
  --bg: #121416;
  --bg-paper: #1c1f24;
  --bg-mute: #262a31;
  --text: #eeeae4;
  --text-secondary: #cfc9c0;
  --text-meta: #a39c93;
  --rule: #343a42;
  --ink: #f3efe8;
  --link: #8ec8f6;
  --link-hover: #c5e3fb;
  --accent: #e8eef4;
  --sev-critical: #f0a098;
  --sev-critical-soft: #3a1d1b;
  --sev-high: #f0be86;
  --sev-high-soft: #3a2a16;
  --sev-info: #c5d0db;
  --sev-info-soft: #1e252c;
  --focus: #8ec8f6;
  color-scheme: dark;
}
[data-theme="light"] {
  color-scheme: light;
}

* { box-sizing: border-box; }
html { color-scheme: light dark; scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) {
  html { scroll-behavior: auto; }
}
body {
  margin: 0;
  font-family: var(--font-sans);
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  color: var(--text);
  background: var(--bg);
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--link); text-decoration-thickness: 1px; text-underline-offset: 0.16em; }
a:hover { color: var(--link-hover); }
a:focus-visible, button:focus-visible, summary:focus-visible, section:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}

.inner {
  width: min(var(--measure), calc(100% - 2.25rem));
  margin: 0 auto;
}

/* Masthead: navigation only, not the briefing itself */
.site-header {
  background: var(--bg-paper);
  border-bottom: 1px solid var(--rule);
}
.header-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--s-2) var(--s-4);
  flex-wrap: wrap;
  padding: 0.55rem 0;
}
.site-title {
  color: var(--accent);
  text-decoration: none;
  font-weight: 700;
  font-size: var(--fs-kicker);
  letter-spacing: 0.08em;
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem 0.5rem;
  align-items: baseline;
  min-width: 0;
  flex: 1 1 auto;
}
.site-title:hover { color: var(--link-hover); }
.site-title .sep { color: var(--text-meta); font-weight: 400; letter-spacing: 0; opacity: 0.8; }
.site-title .zh { letter-spacing: 0.06em; }

.header-tools {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.4rem 0.6rem;
  margin-left: auto;
  flex: 0 1 auto;
  flex-wrap: wrap;
}
.date-nav { display: inline-flex; align-items: center; gap: 0.15rem; position: relative; }
.pager-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.85rem;
  height: 1.85rem;
  border-radius: 2px;
  text-decoration: none;
  font-weight: 700;
  color: var(--text);
}
.pager-link:hover { background: var(--bg-mute); color: var(--link-hover); }
.pager-link.disabled { color: var(--text-meta); opacity: 0.4; pointer-events: none; }

.date-picker { position: relative; }
.date-picker summary {
  list-style: none;
  cursor: pointer;
  border: 1px solid var(--rule);
  border-radius: 2px;
  padding: 0.15rem 0.65rem;
  background: var(--bg);
  font-weight: 650;
  font-size: var(--fs-meta);
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  line-height: 1.4;
  user-select: none;
}
.date-picker summary::-webkit-details-marker,
.date-picker summary::marker { display: none; content: ""; }
.date-picker summary::after { content: "▾"; font-size: 0.75em; color: var(--text-meta); }
.date-picker[open] summary::after { content: "▴"; }
.date-picker summary:hover { border-color: var(--link); }
.date-summary-label { white-space: nowrap; }
.date-picker summary .counts { font-weight: 500; }

.date-menu {
  position: absolute;
  top: calc(100% + 0.35rem);
  right: 0;
  min-width: max(16.5rem, 100%);
  width: max(18rem, 100%);
  max-width: min(22rem, calc(100vw - 1.5rem));
  max-height: min(70vh, 28rem);
  overflow: auto;
  background: var(--bg-paper);
  border: 1px solid var(--rule);
  z-index: 30;
  padding: 0.3rem;
}
.date-menu ul { list-style: none; margin: 0; padding: 0; }
.date-item {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.15rem 0.7rem;
  padding: 0.4rem 0.55rem;
  text-decoration: none;
  color: var(--text);
  font-size: 0.88rem;
  line-height: 1.5;
}
.date-item:hover { background: var(--bg-mute); color: var(--text); }
.date-item.is-current { background: var(--sev-info-soft); font-weight: 650; }
.date-item-label { display: inline-flex; flex-wrap: wrap; align-items: baseline; gap: 0.35rem; }
.date-item .counts { font-size: 0.8rem; }
.badge-latest {
  display: inline-block;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  color: var(--text);
  background: var(--bg-mute);
  border-radius: 2px;
  padding: 0.08rem 0.4rem;
}
.counts { color: var(--text-meta); font-size: 0.84rem; }
.count.critical { color: var(--sev-critical); font-weight: 650; }
.count.high { color: var(--sev-high); font-weight: 650; }
.count.info { color: var(--sev-info); }
.dot { margin: 0 0.1rem; color: var(--text-meta); }

.lang-toggle {
  display: inline-flex;
  border: 1px solid var(--rule);
  overflow: hidden;
  font-size: var(--fs-meta);
  background: var(--bg-paper);
  flex: 0 0 auto;
}
.lang-toggle a,
.lang-toggle .current {
  padding: 0.15rem 0.7rem;
  text-decoration: none;
  line-height: 1.4;
}
.lang-toggle .current {
  background: var(--accent);
  color: var(--bg-paper);
  font-weight: 700;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .lang-toggle .current { color: #121416; background: var(--accent); }
}
[data-theme="dark"] .lang-toggle .current { color: #121416; background: var(--accent); }

.theme-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.85rem;
  height: 1.85rem;
  padding: 0;
  border: 1px solid var(--rule);
  border-radius: 2px;
  background: var(--bg-paper);
  color: var(--text);
  cursor: pointer;
  font-size: 0.95rem;
  line-height: 1;
  flex: 0 0 auto;
}
.theme-toggle:hover { background: var(--bg-mute); color: var(--link-hover); }
.theme-toggle[hidden] { display: none; }
.theme-icon { pointer-events: none; }

/* Briefing body */
main { padding: var(--s-5) 0 var(--s-7); }

.summary {
  margin: 0 0 var(--s-4);
  padding: 0;
  border-bottom: 0;
}
.summary h1 {
  margin: 0 0 var(--s-3);
  font-size: var(--fs-display);
  line-height: var(--lh-display);
  font-weight: 750;
  letter-spacing: 0.01em;
  color: var(--ink);
}
.count-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--s-2) var(--s-3);
  margin: 0 0 var(--s-2);
  align-items: center;
}
.count-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.95rem;
  color: var(--text);
  font-weight: 700;
  text-decoration: none;
  border-bottom: 1px solid transparent;
  padding-bottom: 0.05rem;
}
.count-chip:hover {
  color: var(--link);
  border-bottom-color: var(--link);
}

.period {
  margin: 0;
  color: var(--text-meta);
  font-size: var(--fs-meta);
  line-height: 1.6;
}

.badge {
  display: inline-block;
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  line-height: 1.2;
  padding: 0.16em 0.48em;
  border-radius: 2px;
  vertical-align: middle;
}
.badge.critical { background: var(--sev-critical); color: #fff; }
.badge.high { background: var(--sev-high); color: #fff; }
.badge.info { background: var(--sev-info); color: #fff; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .badge.critical { background: #8f332d; color: #ffe9e7; }
  :root:not([data-theme="light"]) .badge.high { background: #8a5318; color: #fff4e4; }
  :root:not([data-theme="light"]) .badge.info { background: #4a5560; color: #eef3f7; }
}
[data-theme="dark"] .badge.critical { background: #8f332d; color: #ffe9e7; }
[data-theme="dark"] .badge.high { background: #8a5318; color: #fff4e4; }
[data-theme="dark"] .badge.info { background: #4a5560; color: #eef3f7; }

section.sev { margin: 0 0 var(--s-6); }
section.sev:focus { outline: none; }
section.sev:focus-visible { outline: 2px solid var(--focus); outline-offset: 4px; }
.sev-heading {
  display: flex;
  align-items: center;
  gap: var(--s-2);
  margin: 0 0 var(--s-3);
  padding: var(--s-2) 0 0;
  border-top: 1px dashed var(--rule);
  font-size: 1rem;
  font-weight: 700;
}
section.sev.critical .sev-heading { border-top-color: var(--sev-critical); }
section.sev.high .sev-heading { border-top-color: var(--sev-high); }
section.sev.info .sev-heading { border-top-color: var(--sev-info); }
.sev-count { color: var(--text-meta); font-weight: 600; font-size: 0.9rem; }

ol.cards {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0;
  background: var(--bg-paper);
  border: 1px solid var(--rule);
  border-left-width: 1px;
  border-left-style: dashed;
}
section.sev.critical ol.cards { border-left-color: var(--sev-critical); }
section.sev.high ol.cards { border-left-color: var(--sev-high); }
section.sev.info ol.cards { border-left-color: var(--sev-info); }

.card {
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--rule);
  border-radius: 0;
  box-shadow: none;
  padding: 1.05rem 1.15rem 1.1rem;
}
.card:last-child { border-bottom: 0; }

.card-meta {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  margin: 0 0 0.35rem;
}
.card-meta .num {
  font-variant-numeric: tabular-nums;
  font-weight: 700;
  font-size: var(--fs-kicker);
  color: var(--text-meta);
  min-width: 1.25em;
}
.stack-flag { font-size: 0.85rem; margin-left: 0.1rem; }

.card h3 {
  margin: 0;
  font-size: var(--fs-title);
  font-weight: 700;
  line-height: var(--lh-title);
  color: var(--ink);
}
.card-title-link {
  color: var(--ink);
  text-decoration: underline;
  text-decoration-color: var(--rule);
  text-decoration-thickness: 1px;
  text-underline-offset: 0.18em;
}
.card-title-link:hover {
  color: var(--link);
  text-decoration-color: var(--link);
}

.expl {
  margin: 0.4rem 0 0;
  color: var(--text-secondary);
  font-size: 16.5px;
  font-weight: 400;
  line-height: var(--lh-body);
  overflow-wrap: break-word;
}

.sources {
  margin: 0.6rem 0 0;
  font-size: var(--fs-kicker);
  line-height: 1.5;
  color: var(--text-meta);
}
.src-label { margin-right: 0.25rem; }
.src-link {
  color: var(--text-meta);
  text-decoration: none;
  border-bottom: 1px solid var(--rule);
}
.src-link:hover { color: var(--link); border-bottom-color: var(--link); }
.src-sep { margin: 0 0.15rem; color: var(--rule); }

.notes {
  margin: var(--s-6) 0 0;
  padding: var(--s-4);
  background: var(--bg-mute);
  color: var(--text-meta);
  font-size: 0.88rem;
  line-height: 1.7;
}
.notes p { margin: 0.4rem 0; }
.notes p:first-child { margin-top: 0; }
.notes p:last-child { margin-bottom: 0; }

code {
  font-family: var(--font-mono);
  font-size: 0.86em;
  background: var(--bg-mute);
  border-radius: 2px;
  padding: 0.05em 0.35em;
  word-break: break-all;
}

.site-footer {
  border-top: 1px solid var(--rule);
  padding: var(--s-5) 0 var(--s-7);
  color: var(--text-meta);
  font-size: var(--fs-meta);
  line-height: 1.7;
  background: var(--bg);
}
.site-footer p { margin: 0.15rem 0; }

/* Reserved for a future subscribe/unsubscribe form. No placeholder copy. */
.subscribe-slot {
  min-height: 3.25rem;
  margin-top: var(--s-5);
}

@media (max-width: 640px) {
  .site-title.lang-ja .sep,
  .site-title.lang-ja .zh,
  .site-title.lang-zh .sep,
  .site-title.lang-zh .ja { display: none; }
  .date-picker summary .counts { display: none; }
  /* Keep the date list fully inside the viewport; do not clip against the picker. */
  .header-tools { position: relative; width: 100%; }
  .date-picker,
  .date-nav { position: static; }
  .date-menu {
    left: 0;
    right: 0;
    width: 100%;
    min-width: 0;
    max-width: none;
  }
}

@media (max-width: 560px) {
  body { font-size: 16.5px; }
  .expl { font-size: 16px; }
  .summary h1 { font-size: 1.4rem; }
  .inner { width: min(var(--measure), calc(100% - 1.5rem)); }
  .card { padding: 0.95rem 0.85rem 1rem; }
  .header-tools { width: 100%; }
  .summary { margin-bottom: var(--s-4); padding-bottom: var(--s-3); }
  section.sev { margin-bottom: var(--s-5); }
  .sev-heading { padding-top: var(--s-3); }
}

@media print {
  .site-header, .site-footer { position: static; }
  .header-tools, .subscribe-slot { display: none; }
  body { background: white; color: black; }
  ol.cards { border-color: #ccc; }
  a { color: black; }
  .src-link::after { content: " (" attr(href) ")"; font-size: 0.85em; }
}
"""

HEADERS = """\
/*
  Content-Security-Policy: default-src 'self'
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  X-Frame-Options: DENY
"""

THEME_JS = r"""
(function () {
  var KEY = "theme";

  function saved() {
    try {
      var value = localStorage.getItem(KEY);
      if (value === "dark" || value === "light") return value;
    } catch (err) {}
    return null;
  }

  function systemTheme() {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function resolved() {
    return saved() || systemTheme();
  }

  function apply(theme, persist) {
    document.documentElement.setAttribute("data-theme", theme);
    if (persist) {
      try { localStorage.setItem(KEY, theme); } catch (err) {}
    }
  }

  var initial = saved();
  if (initial) apply(initial, false);

  function sync(theme) {
    var buttons = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      btn.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
      var icon = btn.querySelector(".theme-icon");
      if (icon) icon.textContent = theme === "dark" ? "\u2600" : "\u263E";
      var label = btn.getAttribute(theme === "dark" ? "data-label-light" : "data-label-dark");
      if (label) btn.setAttribute("aria-label", label);
    }
  }

  function onToggle() {
    var next = resolved() === "dark" ? "light" : "dark";
    apply(next, true);
    sync(next);
  }

  function bind() {
    var theme = resolved();
    var buttons = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      btn.hidden = false;
      btn.addEventListener("click", onToggle);
    }
    sync(theme);
    try {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        if (!saved()) sync(resolved());
      });
    } catch (err) {}
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
"""


def discover(input_dir: Path) -> list[dict]:
    editions: dict[str, dict] = {}
    for path in sorted(input_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith("_raw.jsonl") or name.endswith("_raw.err") or "_raw." in name:
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_(zh\.md|ja\.txt)$", name)
        if not m:
            continue
        day, kind = m.group(1), m.group(2)
        try:
            date = dt.date.fromisoformat(day)
        except ValueError:
            continue
        rec = editions.setdefault(day, {"date": date, "ja": None, "zh": None})
        text = path.read_text(encoding="utf-8")
        if kind == "ja.txt":
            rec["ja"] = text
            rec["ja_path"] = path
        else:
            rec["zh"] = text
            rec["zh_path"] = path
    missing = []
    result = []
    for day, rec in sorted(editions.items(), reverse=True):
        if rec["ja"] is None or rec["zh"] is None:
            missing.append(day)
            continue
        rec["parsed_ja"] = parse_ja(rec["ja"])
        result.append(rec)
    if missing:
        print(
            "warning: skipping incomplete editions (need both _ja.txt and _zh.md): "
            + ", ".join(missing),
            file=sys.stderr,
        )
    return result


def atom_rfc3339(date: dt.date, hour: int = 9) -> str:
    # Digests go out around 09:00 JST.
    aware = dt.datetime(date.year, date.month, date.day, hour, 0, 0,
                        tzinfo=dt.timezone(dt.timedelta(hours=9)))
    return aware.isoformat()


def build_feed(editions: list[dict]) -> str:
    latest = editions[0]["date"] if editions else dt.date.today()
    updated = atom_rfc3339(latest)
    entries: list[str] = []
    for rec in editions:
        date = rec["date"]
        subject = rec["parsed_ja"]["subject"]
        ja_url = f"{BASE_URL}/{date.isoformat()}/ja.html"
        zh_url = f"{BASE_URL}/{date.isoformat()}/zh.html"
        summary = html.escape(subject or f"{SITE_TITLE_JA} {format_date_ja(date)}")
        entry_id = f"{BASE_URL}/{date.isoformat()}/"
        entries.append(
            f"""  <entry>
    <title>{html.escape(subject or date.isoformat())}</title>
    <id>{attr(entry_id)}</id>
    <link rel="alternate" hreflang="ja" href="{attr(ja_url)}"/>
    <link rel="alternate" hreflang="zh-CN" href="{attr(zh_url)}"/>
    <updated>{updated if date == latest else atom_rfc3339(date)}</updated>
    <published>{atom_rfc3339(date)}</published>
    <summary>{summary}</summary>
  </entry>"""
        )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>{html.escape(SITE_TITLE)}</title>
  <subtitle>公開の日次セキュリティ情報アーカイブ</subtitle>
  <link href="{attr(BASE_URL + "/")}" rel="alternate" type="text/html"/>
  <link href="{attr(BASE_URL + "/feed.xml")}" rel="self" type="application/atom+xml"/>
  <id>{attr(BASE_URL + "/")}</id>
  <updated>{updated}</updated>
  <author><name>GCU</name></author>
{chr(10).join(entries)}
</feed>
"""


def neighbors(date: dt.date, chronological: list[dict]) -> tuple[dt.date | None, dt.date | None]:
    index_by_day = {rec["date"]: i for i, rec in enumerate(chronological)}
    i = index_by_day[date]
    prev_date = chronological[i - 1]["date"] if i > 0 else None
    next_date = chronological[i + 1]["date"] if i + 1 < len(chronological) else None
    return prev_date, next_date


def render_edition_page(
    rec: dict,
    lang: str,
    editions: list[dict],
    chronological: list[dict],
    path: str,
) -> str:
    date = rec["date"]
    prev_date, next_date = neighbors(date, chronological)
    view = build_view(rec, "ja" if lang == "ja" else "zh")
    body = render_article(view, "ja" if lang == "ja" else "zh")
    if lang == "ja":
        title = rec["parsed_ja"]["subject"] or f"{SITE_TITLE_JA} {format_date_ja(date)}"
        html_lang = "ja"
        description = rec["parsed_ja"]["subject"] or SITE_TITLE_JA
    else:
        heading = view["heading"]
        title = f"{SITE_TITLE_ZH} · {heading}"
        html_lang = "zh-CN"
        description = title
    return wrap_page(
        title=title,
        lang=html_lang if html_lang == "ja" else "zh",
        body=body,
        path=path,
        description=description,
        date=date,
        editions=editions,
        prev_date=prev_date,
        next_date=next_date,
    )


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    path.write_text(content, encoding="utf-8")


def build(input_dir: Path, output_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        raise SystemExit(f"input directory not found: {input_dir}")

    editions = discover(input_dir)
    if not editions:
        raise SystemExit(f"no complete digest editions found in {input_dir}")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    written: list[Path] = []

    def remember(path: Path) -> Path:
        written.append(path)
        return path

    write_file(remember(output_dir / "style.css"), CSS)
    write_file(remember(output_dir / "theme.js"), THEME_JS)
    write_file(remember(output_dir / "_headers"), HEADERS)
    write_file(remember(output_dir / "feed.xml"), build_feed(editions))

    # editions are newest-first; prev = older, next = newer.
    chronological = list(reversed(editions))
    latest = editions[0]

    write_file(
        remember(output_dir / "index.html"),
        render_edition_page(latest, "ja", editions, chronological, "/"),
    )
    write_file(
        remember(output_dir / "zh" / "index.html"),
        render_edition_page(latest, "zh", editions, chronological, "/zh/"),
    )

    for rec in editions:
        day = rec["date"].isoformat()
        write_file(
            remember(output_dir / day / "ja.html"),
            render_edition_page(rec, "ja", editions, chronological, f"/{day}/ja.html"),
        )
        write_file(
            remember(output_dir / day / "zh.html"),
            render_edition_page(rec, "zh", editions, chronological, f"/{day}/zh.html"),
        )

    return written


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    default_input = Path("/workspace/secnews/out")
    if not default_input.is_dir():
        alt = here.parent / "secnews" / "out"
        if alt.is_dir():
            default_input = alt

    parser = argparse.ArgumentParser(description="Build the secnews public archive site.")
    parser.add_argument("--input", type=Path, default=default_input, help="digest output directory")
    parser.add_argument("--output", type=Path, default=here / "dist", help="site output directory")
    args = parser.parse_args(argv)

    written = build(args.input.resolve(), args.output.resolve())
    print(f"built {len(written)} files into {args.output.resolve()}")
    for path in written:
        rel = path.relative_to(args.output.resolve())
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
