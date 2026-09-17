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
# 사이트에 노출하는 회차 표기 — 시각 대신 낮/밤
SLOT_WORD = {"morning": "낮", "evening": "밤"}
PER_PAGE = 5


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
    # 줄 중간에 붙은 불릿을 줄로 분리한다.
    #  - "* **굵은항목**" 처럼 별표 뒤에 바로 굵은 표기가 와도 불릿이므로 반드시 분리해야 한다(이전 버그).
    #  - 굵은 표기 자체(" **text** ")는 별표 뒤에 공백이 없어 매칭되지 않는다.
    out = re.sub(r"(?<!\*)[ \t]+\*[ \t]+", "\n* ", out)
    # "- 불릿" 도 같은 이유로 분리한다. 단어 사이 하이픈(GPT-6, 10-11)은 공백이 없어 매칭되지 않는다.
    out = re.sub(r"(?<![A-Za-z0-9])[ \t]+-[ \t]+(?=\S)", "\n- ", out)
    out = re.sub(r"(?m)^[ \t]*\*[ \t]+", "* ", out)
    # 번호로 시작하는 짧은 줄은 섹션 제목으로 승격 (예: "1. OpenAI (GPT-6 시리즈 / 요금제 개편 / 우회 차단)")
    out = re.sub(
        r"(?m)^(\d{1,2}\.)[ \t]+([^\n]{2,60}?)[ \t]*$",
        lambda m: (f"\n## {m.group(1)} {m.group(2)}\n"
                   if not m.group(2).rstrip().endswith((".", "다.", "요.", ":"))
                   else m.group(0)),
        out,
    )
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


def date_label(ed: dict) -> str:
    """'2026년 9월 17일 밤' 형식 (조간=낮, 석간=밤)."""
    raw = str(ed.get("date") or "")[:10]
    try:
        d = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    word = SLOT_WORD.get(str(ed.get("slot")), "")
    return f"{d.year}년 {d.month}월 {d.day}일 {word}".strip()


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
  .nav{display:flex;gap:10px;align-items:center;margin:6px 0 0;font-size:12.5px}
  .nav a{color:var(--dim);text-decoration:none;border:1px solid var(--rule);border-radius:999px;padding:5px 12px;background:#fff}
  .nav a:hover{border-color:var(--neon);color:var(--ink)}
  .nav a.on{background:var(--ink);color:#fff;border-color:var(--ink)}
  .cta{margin:40px 0 0;text-align:center}
  .cta a{display:inline-block;font-size:14px;font-weight:700;color:var(--ink);text-decoration:none;
    border:1.5px solid var(--ink);border-radius:999px;padding:12px 26px}
  .cta a:hover{background:var(--ink);color:#fff}
  .pager{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin:26px 0 0}
  .pager button{font:inherit;font-size:13px;min-width:36px;padding:7px 10px;cursor:pointer;background:#fff;
    color:var(--ink);border:1px solid var(--rule);border-radius:9px;font-variant-numeric:tabular-nums}
  .pager button:hover{border-color:var(--neon)}
  .pager button.on{background:var(--ink);color:#fff;border-color:var(--ink);font-weight:700}
  .pagehead{text-align:left;padding:38px 0 6px}
  .pagehead h1{margin:0;font-size:clamp(22px,4.6vw,30px)}
  .pagehead .sub{margin-top:8px;font-size:12.5px;color:var(--dim)}
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
    <div class="stamp">하루 두 번 · 낮(08시) / 밤(20시)<br>요약만 남긴 AI 뉴스</div>
  </div>"""


def _page(title: str, root: str, inner: str, script: str = "") -> str:
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
{script}
</body>
</html>
"""


def _lede(ed: dict, root: str, latest: bool = True) -> str:
    word = SLOT_WORD.get(ed["slot"], "")
    badge = f"최신 호 · <b>{word}</b>" if latest else f"<b>{word}</b>"
    return f"""  <div class="lede">
    <div class="edline">{badge}</div>
    <h1>{html.escape(ed.get('title') or '오늘의 AI 업데이트')}</h1>
    <div class="dateline">{html.escape(date_label(ed))}</div>
  </div>
  <div class="body">
{md_to_html(ed.get('body',''))}
  </div>"""


def _nav(root: str, here: str) -> str:
    """here: 'latest' | 'list' | 'edition'"""
    items = []
    items.append(f'<a class="{"on" if here == "latest" else ""}" href="{root}index.html">최신 호</a>')
    items.append(f'<a class="{"on" if here == "list" else ""}" href="{root}list.html">목록 보기</a>')
    return '  <nav class="nav">' + "".join(items) + "</nav>"


def _list_items(editions: list[dict], root: str) -> str:
    """전체 목록을 한 번에 렌더하고, 페이지 나누기는 클라이언트 스크립트가 담당한다."""
    rows = []
    for i, ed in enumerate(editions):
        rows.append(
            f'    <li data-i="{i}"><a href="{root}e/{ed["_key"]}.html">'
            f'<span class="t">{html.escape(ed.get("title") or "요약")}</span>'
            f'<span class="d">{html.escape(date_label(ed))}</span></a></li>'
        )
    if not rows:
        return ""
    return ("  <ol class=\"past\" id=\"past\">\n" + "\n".join(rows) + "\n  </ol>\n"
            "  <nav class=\"pager\" id=\"pager\" hidden></nav>")


PAGER_JS = """<script>
(function () {
  var PER = __PER__;
  var ol = document.getElementById('past');
  var pager = document.getElementById('pager');
  if (!ol || !pager) return;
  var items = Array.prototype.slice.call(ol.children);
  if (items.length <= PER) return;
  var pages = Math.ceil(items.length / PER);
  var cur = 0;
  function render() {
    items.forEach(function (li, i) { li.hidden = Math.floor(i / PER) !== cur; });
    Array.prototype.forEach.call(pager.children, function (b, i) {
      b.className = (i === cur) ? 'on' : '';
      b.setAttribute('aria-current', i === cur ? 'page' : 'false');
    });
    if (history.replaceState) history.replaceState(null, '', '#p' + (cur + 1));
  }
  for (var i = 0; i < pages; i++) {
    (function (i) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = String(i + 1);
      b.addEventListener('click', function () { cur = i; render(); });
      pager.appendChild(b);
    })(i);
  }
  pager.hidden = false;
  var m = location.hash.match(/^#p(\\d+)$/);
  if (m) { var n = parseInt(m[1], 10) - 1; if (n >= 0 && n < pages) cur = n; }
  render();
})();
</script>""".replace("__PER__", str(PER_PAGE))


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

    # 1) 최신 호 (표지) — 요약만. 목록은 여기에 붙이지 않고 전용 페이지로 보낸다.
    index_inner = (
        _lede(latest, "", latest=True)
        + '\n  <div class="cta"><a href="list.html">지난 요약 목록 보기</a></div>'
        + '\n  <div class="foot"><span>네온 두부 / Neon Tofu</span>'
          f'<span>{html.escape(date_label(latest))} · 최신 호</span></div>'
    )
    (out_dir / "index.html").write_text(
        _page(f"네온 두부 — {latest.get('title','')}", "", index_inner), encoding="utf-8")

    # 2) 목록 페이지 — 5개씩, 페이지 번호로 같은 화면에서 목록만 넘어간다.
    list_inner = (
        _nav("", "list")
        + '\n  <div class="pagehead"><h1>지난 요약</h1>'
          f'<div class="sub">전체 {len(editions)}회차 · 5개씩 보기</div></div>'
        + "\n  <div class=\"rule\"></div>\n"
        + _list_items(editions, "")
        + '\n  <div class="foot"><span>네온 두부 / Neon Tofu</span><span>목록</span></div>'
    )
    (out_dir / "list.html").write_text(
        _page("네온 두부 — 지난 요약", "", list_inner, script=PAGER_JS), encoding="utf-8")

    # 3) 회차 개별 페이지 — 상단은 '최신 호' / '목록 보기' 링크만.
    for ed in editions:
        inner = (_nav("../", "edition") + "\n"
                 + _lede(ed, "../", latest=False)
                 + '\n  <div class="foot"><span>네온 두부 / Neon Tofu</span>'
                   f'<span>{html.escape(date_label(ed))}</span></div>')
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
