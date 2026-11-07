#!/usr/bin/env python3
"""Render the tutorial and Mermaid SVGs; optional documentation tools only."""
import argparse
import asyncio
import hashlib
import html
from pathlib import Path
import re

import markdown
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
CSS = """
:root{color-scheme:light;--bg:#f3f5f4;--paper:#fff;--ink:#1e3034;--muted:#52666b;--line:#d6dfdf;--accent:#006b67;--code:#edf3f3}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);background:var(--bg);font:16px/1.85 "Noto Sans CJK TC","Microsoft JhengHei",system-ui,sans-serif}
.layout{display:grid;grid-template-columns:230px minmax(0,940px);gap:46px;max-width:1290px;margin:auto;padding:38px 30px 90px}
nav{position:sticky;top:24px;align-self:start;max-height:calc(100vh - 48px);overflow:auto;font-size:.84rem;line-height:1.65}nav strong{display:block;border-top:3px solid var(--accent);padding:13px 7px 10px;font-size:.95rem}nav ul{padding:0;list-style:none}nav a{display:block;padding:6px 7px;text-decoration:none;color:var(--muted);border-radius:4px}nav a:hover{background:var(--line);color:var(--ink)}
main{min-width:0;overflow-wrap:anywhere}h1{font-size:2.35rem;letter-spacing:-.025em;line-height:1.35;margin:12px 0 24px;max-width:25ch}h2{font-size:1.6rem;line-height:1.45;margin:70px 0 22px;padding:20px 0 0;border-top:2px solid var(--line)}h3{font-size:1.16rem;line-height:1.55;margin:35px 0 12px}h4{font-size:1rem;margin-top:24px}p,li{max-width:85ch}p{margin:15px 0}li{margin:5px 0}li p{margin:8px 0}a{color:var(--accent);text-underline-offset:3px}a[id]{scroll-margin-top:24px}a:focus-visible,summary:focus-visible{outline:3px solid var(--accent);outline-offset:4px}
.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.13em;font-size:.75rem;font-weight:700;margin:0 0 10px}.document-meta{font-size:.8rem;color:var(--muted);padding-bottom:18px;border-bottom:1px solid var(--line)}
code,pre{font-family:"Cascadia Mono",Consolas,"DejaVu Sans Mono",monospace}code{font-size:.88em;background:var(--code);padding:.1em .28em;border-radius:3px}pre{background:#142a30;color:#e4f2ee;border:1px solid #2c454c;border-radius:8px;padding:20px 22px;overflow:auto;font-size:.82rem;line-height:1.75;tab-size:4;max-width:100%}pre code{font-size:inherit;color:inherit;background:none;padding:0;white-space:pre;overflow-wrap:normal}
blockquote{margin:22px 0;background:#e7f1ed;border-left:4px solid var(--accent);padding:4px 18px;border-radius:0 6px 6px 0}blockquote p{margin:10px 0}.table-wrap{overflow-x:auto;margin:22px 0;border:1px solid var(--line);border-radius:7px;background:var(--paper)}table{border-collapse:collapse;width:100%;font-size:.86rem;line-height:1.75}th,td{text-align:left;vertical-align:top;padding:12px 14px;border-bottom:1px solid var(--line);min-width:90px}th{background:#e7eeee;font-weight:650}tr:last-child td{border-bottom:0}td code{overflow-wrap:anywhere}
figure.diagram{margin:26px 0;padding:18px 12px 10px;background:white;border:1px solid var(--line);border-radius:8px}.diagram-scroll{overflow-x:auto;padding:6px 0 12px}.diagram svg{display:block;margin:auto;height:auto}.diagram figcaption{font-size:.8rem;color:var(--muted);line-height:1.6;margin:7px 8px 2px}details{background:var(--paper);border:1px solid var(--line);border-radius:7px;margin:16px 0;padding:4px 18px}summary{cursor:pointer;font-weight:650;padding:11px 0;line-height:1.65}details[open]{border-left:4px solid var(--accent)}details p{font-size:.95rem}footer{font-size:.8rem;color:var(--muted);border-top:1px solid var(--line);margin-top:60px;padding-top:18px}
@media(max-width:1000px){.layout{grid-template-columns:190px minmax(0,1fr);gap:24px;padding:24px 20px}h1{font-size:2rem}}@media(max-width:760px){.layout{display:block;padding:20px 16px 60px}nav{position:static;max-height:250px;overflow:auto;border:1px solid var(--line);padding:8px 12px;background:var(--paper);margin-bottom:28px}nav strong{padding-top:8px}h1{font-size:1.8rem}h2{font-size:1.4rem;margin-top:52px}pre{padding:16px;font-size:.77rem}th,td{padding:10px;min-width:115px}.diagram{padding:12px 4px!important}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}@media print{body{background:#fff;font-size:10pt}.layout{display:block;max-width:none;padding:0}nav,.eyebrow{display:none}h1{font-size:25pt}h2{font-size:17pt;margin-top:30px}h3{font-size:12pt}h2,h3,summary{break-after:avoid}pre{color:#142a30;background:#f1f4f4;white-space:pre-wrap;overflow-wrap:anywhere;font-size:8pt}pre code{white-space:pre-wrap;overflow-wrap:anywhere}.table-wrap,.diagram-scroll{overflow:visible}table{font-size:8pt}th,td{min-width:0}.diagram svg{max-width:100%!important}details::details-content{content-visibility:visible;display:block}a{color:inherit}}
"""


async def render(mermaid_js):
    source = (ROOT / "cxl-mm-tutorial.md").read_text(encoding="utf-8")
    digest = hashlib.sha256(source.encode()).hexdigest()
    pattern = re.compile(r"^```mermaid\n(.*?)^```[ \t]*$", re.M | re.S)
    diagrams = []
    async with async_playwright() as client:
        browser = await client.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content('<html lang="zh-Hant"><body></body></html>')
        await page.add_script_tag(path=str(mermaid_js))
        await page.evaluate("""mermaid.initialize({startOnLoad:false,securityLevel:'strict',
          theme:'base',fontFamily:'Droid Sans Fallback, Microsoft JhengHei, sans-serif',
          deterministicIds:true,deterministicIDSeed:'cxl-mm-tutorial',
          flowchart:{htmlLabels:false,useMaxWidth:false},
          themeVariables:{primaryColor:'#e6f2ed',primaryBorderColor:'#47857d',
            primaryTextColor:'#183b3b',lineColor:'#52746f',secondaryColor:'#edf2f5',
            tertiaryColor:'#fff8e8',fontSize:'16px'}})""")
        for index, match in enumerate(pattern.finditer(source), 1):
            chart = match.group(1).strip()
            heading = re.findall(r"^###? (.+)$", source[:match.start()], re.M)[-1]
            if "accTitle:" not in chart:
                first, rest = chart.split("\n", 1)
                chart = first + f"\naccTitle: {heading}\naccDescr: {heading}的流程與條件，詳細解釋見圖前後文字。\n" + rest
            svg = await page.evaluate("""async ({id, chart}) =>
              (await mermaid.render(id, chart)).svg""", {"id": f"diagram{index}", "chart": chart})
            if "<script" in svg.lower():
                raise ValueError("Unexpected script in rendered SVG")
            # Mermaid reuses node/edge IDs across graphs; namespace references too.
            ids = re.findall(r'(?<![-:\w])id="([^"]+)"', svg)
            names = {old: f"chart{index}-{old}" for old in ids}
            svg = re.sub(r'(?<![-:\w])id="([^"]+)"',
                         lambda m: f'id="{names[m[1]]}"', svg)
            refs = re.compile(r'#(' + '|'.join(re.escape(x) for x in
                              sorted(names, key=len, reverse=True)) + r')(?![-\w])')
            svg = refs.sub(lambda m: '#' + names[m[1]], svg)
            svg = re.sub(r'(aria-labelledby|aria-describedby)="([^"]+)"',
                         lambda m: m[1] + '="' + ' '.join(names.get(x, x) for x in
                                                         m[2].split()) + '"', svg)
            width = float(re.search(r'viewBox="[\d. -]+ ([\d.]+) [\d.]+"', svg)[1])
            svg = re.sub(r'(<svg\b[^>]*?) style="[^"]*"', r'\1', svg, count=1)
            svg = re.sub(r'<svg\b', f'<svg style="width:{max(260, min(width, 900)):.0f}px"', svg, count=1)
            diagrams.append(f'<figure class="diagram"><div class="diagram-scroll">{svg}</div>'
                            f'<figcaption>圖 {index} · {html.escape(heading)}</figcaption></figure>')
        await browser.close()
    iterator = iter(diagrams)
    text = pattern.sub(lambda _: next(iterator), source).replace('<details>', '<details markdown="1">')
    body = markdown.markdown(text, extensions=["fenced_code", "tables", "md_in_html"])
    toc = re.search(r'<h2>目錄</h2>\s*(<ul>.*?</ul>)', body, re.S)
    if not toc:
        raise ValueError("Tutorial contents list missing")
    nav = toc[1]
    body = body[:toc.start()] + body[toc.end():]
    body = re.sub(r"<table>.*?</table>", lambda m: '<div class="table-wrap">' + m[0] + '</div>', body, flags=re.S)
    output = f'''<!doctype html>
<html lang="zh-Hant-TW"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="source-sha256" content="{digest}"><title>CXL Memory Tiering Lab｜從建置到核心追蹤</title>
<style>{CSS}</style></head><body><div class="layout"><nav aria-label="章節目錄"><strong>CXL / Linux MM</strong>{nav}</nav>
<main><p class="eyebrow">CXL Memory Tiering Lab / Technical Guide</p><p class="document-meta">閱讀版本 · 來源：cxl-mm-tutorial.md · M0–M4 / G0–G5</p>{body}
<footer>本文件由 Markdown 產生，圖表已內嵌；閱讀不需網路或 JavaScript。</footer></main></div></body></html>
'''
    (ROOT / "cxl-mm-tutorial.html").write_text(output, encoding="utf-8")
    print(f"Rendered {len(diagrams)} diagrams; source SHA256 {digest}; {len(output.encode())} bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mermaid-js", type=Path, required=True, help="Local mermaid.min.js; no runtime CDN")
    args = parser.parse_args()
    asyncio.run(render(args.mermaid_js.resolve(strict=True)))
