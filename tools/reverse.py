#!/usr/bin/env python3
"""Rebuild out/ source files from live site HTML (+ sent mail for ja)."""
import html, re, sys, os
from pathlib import Path

LIVE = Path('/workspace/secnews/live-backup-2026-11-17')
MAIL = LIVE / 'mail'
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else '/workspace/secnews/rebuild/out')
OUT.mkdir(parents=True, exist_ok=True)

A_RE = re.compile(r'<a href="([^"]*)"[^>]*>(.*?)</a>', re.S)

def rev_inline(s: str) -> str:
    s = A_RE.sub(lambda m: f'[{rev_inline(m.group(2))}]({html.unescape(m.group(1))})', s)
    s = re.sub(r'<code>(.*?)</code>', lambda m: '`' + m.group(1) + '`', s, flags=re.S)
    s = re.sub(r'<strong>(.*?)</strong>', lambda m: '**' + m.group(1) + '**', s, flags=re.S)
    return html.unescape(s)

def parse_page(path: Path):
    t = path.read_text(encoding='utf-8')
    title = html.unescape(re.search(r'<title>(.*?)</title>', t, re.S).group(1))
    art = t[t.index('<article>'):t.index('</article>')]
    h1 = html.unescape(re.search(r'<h1>(.*?)</h1>', art).group(1))
    pm = re.search(r'<p class="period">(.*?)</p>', art, re.S)
    period = rev_inline(pm.group(1)) if pm else ''
    sections = []
    for sm in re.finditer(r'<section class="sev (\w+)".*?</section>', art, re.S):
        level = sm.group(1)
        items = []
        for cm in re.finditer(r'<li class="card".*?</li>', sm.group(0), re.S):
            c = cm.group(0)
            stack = 'stack-flag' in c
            h3 = re.search(r'<h3>(.*?)</h3>', c, re.S).group(1)
            am = re.fullmatch(r'<a href="([^"]*)"[^>]*class="card-title-link">(.*)</a>', h3, re.S)
            if am:
                primary, tit = html.unescape(am.group(1)), rev_inline(am.group(2))
            else:
                primary, tit = '', rev_inline(h3)
            em = re.search(r'<p class="expl">(.*?)</p>', c, re.S)
            expl = rev_inline(em.group(1)) if em else ''
            srcs = [html.unescape(u) for u in re.findall(r'<a href="([^"]*)"[^>]*class="src-link"', c)]
            items.append(dict(stack=stack, title=tit, expl=expl, primary=primary, sources=srcs))
        sections.append((level, items))
    notes = []
    nm = re.search(r'<div class="notes">(.*?)</div>', art, re.S)
    if nm:
        notes = [rev_inline(p) for p in re.findall(r'<p>(.*?)</p>', nm.group(1), re.S)]
    return dict(title=title, h1=h1, period=period, sections=sections, notes=notes)

SEC_HEAD = {'critical': '■ 🔴 緊急（Critical）', 'high': '■ 🟠 重要（High）', 'info': '■ ⚪ 参考情報'}

def ja_from_html(p) -> str:
    lines = [p['title'], '', '各位', '', '本日のセキュリティ情報をお送りします。']
    if p['period']:
        lines.append(p['period'])
    lines.append('凡例：🔴 緊急（Critical）／🟠 重要（High）／⚪ 参考情報')
    n = 1
    for level, items in p['sections']:
        lines += ['', SEC_HEAD[level]]
        for it in items:
            lines.append('')
            t = it['title']
            if '。' in t or '．' in t:
                t = f'**{t}**'
            lines.append(f'{n}. {t}')
            if it['expl']:
                lines.append(it['expl'])
            lines += it['sources']
            n += 1
    lines += ['', '各プロジェクトの状況に応じて、対応要否をご判断ください。',
              '本メールは自動収集した情報をもとに作成しています。', '',
              'Web版（過去の配信も閲覧可）：https://secnews-list-gcu.pages.dev/']
    return '\n'.join(lines) + '\n'

def zh_from_html(p, ja_period: str) -> str:
    lines = [f'# 每日 IT 安全摘要 {p["h1"]}']
    if p['period'] and p['period'] != ja_period:
        lines.append(p['period'])
    lines.append('')
    n = 1
    for level, items in p['sections']:
        for it in items:
            flag = '🔴 ' if it['stack'] else ''
            if it['primary']:
                body = f'[{it["title"]}]({it["primary"]})'
                if it['expl']:
                    body += f' — {it["expl"]}'
            else:
                body = it['title']
            # sources that are not visible in the text must be appended
            extra = [u for u in it['sources'] if u not in body]
            if extra:
                body += ' ' + ' '.join(f'[{u}]({u})' for u in extra)
                print(f'  note: {p["h1"]} item {n} extra sources appended {extra}')
            lines.append(f'{n}. {flag}{body}')
            n += 1
    for note in p['notes']:
        lines += ['', note]
    return '\n'.join(lines) + '\n'

dates = sorted(d.name for d in LIVE.iterdir() if re.fullmatch(r'\d{4}-\d\d-\d\d', d.name))
for day in dates:
    ja_p = parse_page(LIVE / day / 'ja.html')
    zh_p = parse_page(LIVE / day / 'zh.html')
    mail = MAIL / f'{day}_ja.txt'
    if mail.exists():
        ja_txt = mail.read_text(encoding='utf-8').replace('\r\n', '\n')
        src = 'mail'
        live_items = [it for _, its in ja_p['sections'] for it in its]
        fixed = []
        for ln in ja_txt.split('\n'):
            m = re.match(r'^(\d+)\.\s+(.*)$', ln)
            if m:
                k = int(m.group(1)) - 1
                rest = m.group(2).strip()
                if 0 <= k < len(live_items):
                    lt, le = live_items[k]['title'], live_items[k]['expl']
                    if lt and rest != lt and rest.startswith(lt + ' ') and rest[len(lt):].strip() == le:
                        print(f'  split {day} item {k+1}: mail merged title+body into one line')
                        fixed += [f'{m.group(1)}. {lt}', le]
                        continue
            fixed.append(ln)
        ja_txt = '\n'.join(fixed)
    else:
        ja_txt = ja_from_html(ja_p)
        src = 'live-html'
    (OUT / f'{day}_ja.txt').write_text(ja_txt, encoding='utf-8')
    (OUT / f'{day}_zh.md').write_text(zh_from_html(zh_p, ja_p['period']), encoding='utf-8')
    print(day, 'ja from', src)
