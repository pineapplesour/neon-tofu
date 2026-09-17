#!/usr/bin/env python3
"""네온 두부 사이트 빌더 (디자인 2안 - White Gallery).

입력: data/editions/*.json  (회차별 요약)
출력: site/out/index.html   (최신 호 + 지난 요약 목록)
      site/out/e/<key>.html (회차 개별 페이지)
      site/out/editions.json (목록 데이터)

외부 CDN/폰트 의존 없음. 마크다운은 서버사이드에서 직접 렌더한다.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime
from pathlib import Path

SLOT_LABEL = {"morning": "조간", "evening": "석간"}


# ── 최소 마크다운 렌더러 ───────────────────────────────────────

def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(r"`([^`]+?)`", r"<code>\1</code>", out)
    out = re.sub(r"\[([^\]]+?)\]\((https?://[^)]+)\)", r'<a href="\2" rel="noopener">\1</a>', out)
    return out


def normalize_markdown(text: str) -> str:
    """LLM이 한 줄로 뭉개서 내려준 마크다운을 줄 단위 구조로 복원한다.

    프로덕션 요약문은 실제로 `... 드립니다. --- ### 1. OpenAI (...) * **A**: ... * **B**: ...`
    처럼 구분선/제목/하위 불릿이 한 줄에 붙어서 온다. 렌더러가 먹도록 먼저 줄을 나눈다.
    """
    if not text:
        return ""
    out = text.replace("\r\n", "\n")
    out = re.sub(r"[ \t]*-{3,}[ \t]*", "\n\n---\n\n", out)
    out = re.sub(r"[ \t]*(#{2,6})[ \t]+", r"\n\n\1 ", out)
    # 줄 중간에 붙은 "* 항목" 을 불릿 줄로 (굵게 표기 ** 는 건드리지 않는다)
    out = re.sub(r"(?<!\*)[ \t]+\*[ \t]+(?!\*)", "\n* ", out)
    out = re.sub(r"(?m)^[ \t]*\*[ \t]+(?!\*)", "* ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def md_to_html(text: str) -> str:
    lines = normalize_markdown(text).split("\n")
    parts: list[str] = []
    buf: list[str] = []
    in_ul = False

    def flush_para() -> None:
        if buf:
            parts.append("<p>" + _inline(" ".join(buf).strip()) + "</p>")
            buf.clear()

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            parts.append("</ul>")
            in_ul = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_para()
            close_ul()
            continue
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush_para()
            close_ul()
            parts.append("<hr>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            close_ul()
            level = min(len(m.group(1)) + 1, 6)
            parts.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            continue
        m = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m:
            flush_para()
            if not in_ul:
                parts.append("<ul>")
                in_ul = True
            parts.append("<li>" + _inline(m.group(1).strip()) + "</li>")
            continue
        m = re.match(r"^\s*(\d+)[.)]\s+(.*)$", line)
        if m:
            flush_para()
            if not in_ul:
                parts.append("<ul class=\"num\">")
                in_ul = True
            parts.append("<li>" + _inline(m.group(2).strip()) + "</li>")
            continue
        buf.append(stripped)

    flush_para()
    close_ul()
    return "\n".join(parts)


# ── 회차 데이터 ────────────────────────────────────────────────

def edition_key(ed: dict) -> str:
    return f"{ed['date']}-{ed['slot']}"


def load_editions(editions_dir: Path) -> list[dict]:
    items = []
    for path in sorted(editions_dir.glob("*.json")):
        try:
            ed = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if ed.get("date") and ed.get("slot") and ed.get("body"):
            ed["_key"] = edition_key(ed)
            items.append(ed)
    items.sort(key=lambda e: (e["date"], e["slot"] == "evening"), reverse=True)
    return items


def fmt_window(ed: dict) -> str:
    ws, we = ed.get("window_start"), ed.get("window_end")
    if not ws or not we:
        return "지난 12시간"
    try:
        a = datetime.fromisoformat(ws).strftime("%m-%d %H:%M")
        b = datetime.fromisoformat(we).strftime("%m-%d %H:%M")
    except ValueError:
        return "지난 12시간"
    return f"수집 구간 {a} → {b} KST"


STYLE = """
  :root{--paper:#fbf9f6;--ink:#16151a;--dim:#8b8679;--rule:#e2ddd2;--neon:#00d9c0}
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);
    font-family:"Pretendard Variable",Pretendard,-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;
    line-height:1.72;letter-spacing:-.012em}
  .wrap{max-width:780px;margin:0 auto;padding:34px 22px 80px}
  .masthead{display:flex;align-items:flex-end;gap:16px;padding-bottom:18px;border-bottom:2px solid var(--ink)}
  .logo{display:grid;grid-template-columns:repeat(2,13px);grid-auto-rows:13px;gap:2px}
  .logo i{background:var(--ink);border-radius:1px}
  .logo i:nth-child(4){background:var(--neon)}
  .name{font-size:26px;font-weight:800;letter-spacing:-.05em;line-height:1}
  .name a{color:inherit;text-decoration:none}
  .name small{display:block;font-size:10.5px;font-weight:600;letter-spacing:.34em;color:var(--dim);margin-top:7px}
  .stamp{margin-left:auto;text-align:right;font-size:11px;color:var(--dim);line-height:1.5}
  .lede{padding:46px 0 14px;text-align:center}
  .edline{font-size:11.5px;letter-spacing:.2em;text-transform:uppercase;color:var(--dim)}
  .edline b{color:var(--ink)}
  h1{font-size:clamp(26px,6.2vw,40px);line-height:1.24;margin:16px 0 14px;letter-spacing:-.05em;font-weight:800}
  .dateline{font-size:12.5px;color:var(--dim);font-variant-numeric:tabular-nums}
  .body{max-width:660px;margin:34px auto 0;font-size:16px}
  .body p{margin:0 0 20px}
  .body>p:first-child::first-letter{float:left;font-size:56px;line-height:.86;font-weight:800;padding:6px 10px 0 0;color:var(--neon)}
  .body h3,.body h4{font-size:16px;margin:30px 0 10px;letter-spacing:-.03em}
  .body h3{border-bottom:1px solid var(--rule);padding-bottom:8px}
  .body ul{margin:0 0 20px;padding-left:20px}
  .body li{margin:0 0 8px}
  .body hr{border:0;border-top:1px solid var(--rule);margin:34px 0}
  .body strong{font-weight:700}
  .body code{background:#f1eee7;padding:1px 5px;border-radius:4px;font-size:14px}
  .rule{height:1px;background:var(--rule);margin:44px 0 26px}
  .secttl{font-size:11px;letter-spacing:.26em;text-transform:uppercase;color:var(--dim);margin:0 0 18px}
  ol.past{list-style:none;margin:0;padding:0;counter-reset:p}
  ol.past li{counter-increment:p;border-bottom:1px solid var(--rule)}
  ol.past li a{display:grid;grid-template-columns:34px 1fr auto;gap:14px;align-items:baseline;
    padding:15px 4px;text-decoration:none;color:var(--ink)}
  ol.past li a::before{content:counter(p,decimal-leading-zero);font-size:11.5px;color:var(--dim);font-variant-numeric:tabular-nums}
  ol.past .t{font-size:15px;font-weight:600;letter-spacing:-.02em}
  ol.past .d{font-size:11.5px;color:var(--dim);font-variant-numeric:tabular-nums;white-space:nowrap}
  ol.past li a:hover .t{color:var(--neon)}
  ol.past li.e .t::after{content:"석간";font-size:10px;font-weight:700;color:#fff;background:var(--ink);
    border-radius:3px;padding:2px 5px;margin-left:8px;vertical-align:2px}
  ol.past li.m .t::after{content:"조간";font-size:10px;font-weight:700;color:var(--neon);border:1px solid var(--neon);
    border-radius:3px;padding:1px 4px;margin-left:8px;vertical-align:2px}
  .back{display:inline-block;margin:0 0 6px;font-size:12px;color:var(--dim);text-decoration:none}
  .back:hover{color:var(--neon)}
  .foot{margin-top:46px;font-size:11px;color:var(--dim);display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
  @media(max-width:520px){
    .wrap{padding:24px 16px 70px}
    ol.past li a{grid-template-columns:28px 1fr;gap:10px}
    ol.past .d{grid-column:2;text-align:left}
    .stamp{display:none}
  }
"""

MASTHEAD = """  <div class="masthead">
    <div class="logo"><i></i><i></i><i></i><i></i></div>
    <div class="name"><a href="__ROOT__index.html">네온 두부</a><small>NEON TOFU</small></div>
    <div class="stamp">하루 두 번 · 08:00 / 20:00<br>요약만 남긴 AI 뉴스</div>
  </div>"""


def _page(title: str, root: str, inner: str) -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
{MASTHEAD.replace("__ROOT__", root)}
{inner}
</div>
</body>
</html>
"""


def _lede(ed: dict, root: str) -> str:
    label = SLOT_LABEL.get(ed["slot"], ed["slot"])
    stamp = ed.get("published_at") or ""
    try:
        dt = datetime.fromisoformat(stamp)
        when = dt.strftime("%Y년 %-m월 %-d일 %H:%M")
    except Exception:
        when = stamp
    return f"""  <div class="lede">
    <div class="edline">최신 호 · <b>{label}</b></div>
    <h1>{html.escape(ed.get('title') or '오늘의 AI 업데이트')}</h1>
    <div class="dateline">{html.escape(when)} · {html.escape(fmt_window(ed))}</div>
  </div>
  <div class="body">
{md_to_html(ed.get('body',''))}
  </div>"""


def _past_list(editions: list[dict], root: str, skip_key: str | None) -> str:
    rows = []
    for ed in editions:
        if skip_key and ed["_key"] == skip_key:
            continue
        cls = "e" if ed["slot"] == "evening" else "m"
        when = ed.get("published_at") or ""
        try:
            d = datetime.fromisoformat(when)
            stamp = d.strftime("%m-%d %H:%M")
        except Exception:
            stamp = f"{ed['date']}"
        rows.append(
            f'    <li class="{cls}"><a href="{root}e/{ed["_key"]}.html">'
            f'<span class="t">{html.escape(ed.get("title") or "요약")}</span>'
            f'<span class="d">{html.escape(stamp)}</span></a></li>'
        )
    if not rows:
        return ""
    return ("  <div class=\"rule\"></div>\n"
            "  <p class=\"secttl\">지난 요약 · Past Editions</p>\n"
            "  <ol class=\"past\">\n" + "\n".join(rows) + "\n  </ol>")


def build(editions_dir: Path, out_dir: Path) -> dict:
    editions = load_editions(editions_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "e").mkdir(parents=True, exist_ok=True)

    if not editions:
        (out_dir / "index.html").write_text(
            _page("네온 두부", "", "  <p style=\"padding:40px 0\">아직 발행된 요약이 없습니다.</p>"),
            encoding="utf-8",
        )
        (out_dir / "editions.json").write_text("[]", encoding="utf-8")
        return {"editions": 0, "latest": None}

    latest = editions[0]
    index_inner = _lede(latest, "") + "\n" + _past_list(editions, "", skip_key=latest["_key"]) + \
        '\n  <div class="foot"><span>네온 두부 / Neon Tofu</span><span>요약 전용 · 기사 목록 없음</span></div>'
    (out_dir / "index.html").write_text(
        _page(f"네온 두부 — {latest.get('title','')}", "", index_inner), encoding="utf-8")

    for ed in editions:
        inner = ('  <a class="back" href="../index.html">← 지난 요약 목록</a>\n'
                 + _lede(ed, "../")
                 + "\n" + _past_list(editions, "../", skip_key=ed["_key"])
                 + '\n  <div class="foot"><span>네온 두부 / Neon Tofu</span>'
                   f'<span>{html.escape(ed["date"])} {SLOT_LABEL.get(ed["slot"], ed["slot"])}</span></div>')
        (out_dir / "e" / f"{ed['_key']}.html").write_text(
            _page(f"네온 두부 — {ed.get('title','')}", "../", inner), encoding="utf-8")

    (out_dir / "editions.json").write_text(
        json.dumps(
            [
                {k: v for k, v in ed.items() if not k.startswith("_") and k != "body"}
                for ed in editions
            ],
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return {"editions": len(editions), "latest": latest["_key"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--editions", default="data/editions")
    ap.add_argument("--out", default="site/out")
    args = ap.parse_args()
    result = build(Path(args.editions), Path(args.out))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
