#!/usr/bin/env python3
"""First Light AI — Discord-only incremental pipeline.

매 실행:
1) 기존 articles.json(schema v2) 로드 (v1이면 migrate)
2) since = state.last_run_at → Discord export
3) 채팅을 50k chars 청크로 꽉 채워 분할
4) 청크별 LLM 호출 → [no] 또는 [yes]+<data>...
   (프롬프트 입력: 최근 20개 기사 '제목만' + 이 청크 원문)
5) 청크 결과 merge + 제목 Jaccard 중복 제거
6) 새 기사 있으면 분류 호출 1회
   - 규칙: TOP=1(또는 0), MAIN≤6, SIDE 무제한
   - 입력: 규칙 + 최근 3h 결정 로그 + 현재 기사(placement 포함) + 새 기사
   - 검증 실패 → 무한 재시도
7) articles.json 저장 + build_gist.py 호출 + GitHub Pages 공개 산출물 푸시
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time, threading, random
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).parent
ARTICLES_PATH = ROOT / "docs" / "articles.json"
PAGES_ARTICLES_PATH = ROOT / "articles.json"
EXPORTS_ARTICLES_DIR = ROOT / "exports" / "articles"
JOURNAL_NAME = "First Light AI"
GIT_USER_NAME = "pineapplesour"
GIT_USER_EMAIL = "59020461+pineapplesour@users.noreply.github.com"
DAILY_SUMMARY_FALLBACK_TITLE = "오늘의 AI 업데이트"
MODEL = "gemini-3.5-flash-lite"
ENDPOINT_TPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
KST = timezone(timedelta(hours=9))
CHANNEL_ID = "1365049274068631644"
DEVMODE_CHANNEL_ID = CHANNEL_ID

CHUNK_MAX_CHARS = 40_000
MIN_SCAN_SPLIT_CHARS = 10_000
SCAN_GEMMA_MAX_ATTEMPTS = 10
CLASSIFY_GEMMA_MAX_ATTEMPTS = 2
MODEL_FOCUS_LOOKBACK_HOURS = 24
MODEL_FOCUS_RECENT_HOURS = 12
MODEL_FOCUS_DIRECT_MAX_CHARS = 2_000_000
MODEL_FOCUS_MIN_SPLIT_CHARS = 10_000
MODEL_FOCUS_GEMMA_MAX_ATTEMPTS = 10
MODEL_FOCUS_REUSE_TOLERANCE = timedelta(minutes=10)
MODEL_FOCUS_HEADLINE = "일일 알짜배기"
MODEL_FOCUS_KIND = "model_focus"
MODEL_FOCUS_MODELS = ("gemini-3.6-flash", "gemini-3.5-flash-lite")
SAMPLING_DEPRECATED_MODELS = frozenset(MODEL_FOCUS_MODELS)
CROSS_DEDUP_EXISTING_BATCH_SIZE = 75
CLASSIFY_ACTIVE_LIMIT = 75
MAX_MAIN = 6
FRONT_PAGE_SLOTS = 1 + MAX_MAIN
DECISION_LOG_HOURS = 3
TITLES_FOR_DEDUP = 20
KEY_MIN_GAP_S = 3.0
MERGE_ROUNDS = 3
ACTIVE_POOL_LIMIT = 200  # 이 이하면 existing+new 전체 merge, 이상이면 retrieval mode

LOG = lambda m: print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def is_runtime_filesystem_error(exc: BaseException) -> bool:
    text = str(exc)
    lower = text.lower()
    if "tls ca certificate bundle" in lower or "cacert.pem" in lower:
        return True
    if str(ROOT) in text and any(marker in lower for marker in ("permission denied", "no such file", "invalid path")):
        return True
    return False


def default_publish_branch() -> str:
    explicit = os.environ.get("FIRST_LIGHT_PUBLISH_BRANCH") or os.environ.get("GITHUB_REF_NAME")
    if explicit:
        return explicit
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = result.stdout.strip()
        if result.returncode == 0 and branch:
            return branch
    except Exception:
        pass
    return "dev"


PUBLISH_BRANCH = default_publish_branch()


# ── Keys ────────────────────────────────────────────────────────

class KeyScheduler:
    def __init__(self, keys):
        self.keys = list(keys)
        self.last_used = {k: 0.0 for k in keys}
        self.lock = threading.Lock()
        self._idx = 0
    def acquire(self):
        with self.lock:
            now = time.time()
            best, bw = None, float("inf")
            for i in range(len(self.keys)):
                k = self.keys[(self._idx + i) % len(self.keys)]
                w = max(0, self.last_used[k] + KEY_MIN_GAP_S - now)
                if w < bw: bw, best = w, k
                if w == 0: break
            if bw > 0: time.sleep(bw)
            self.last_used[best] = time.time()
            self._idx = (self.keys.index(best) + 1) % len(self.keys)
            return best

def load_keys():
    candidates = []
    if os.environ.get("GEMINI_KEYS_CONFIG"):
        candidates.append(Path(os.environ["GEMINI_KEYS_CONFIG"]))
    candidates.extend([
        Path("/srv/first-light/secrets/gemini_keys.env"),
        Path.home() / ".config" / "legal_evidence_rag" / "keys.env",
    ])
    tried = []
    for p in candidates:
        if p in tried:
            continue
        tried.append(p)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                keys = [k.strip() for k in line.split("=", 1)[1].split(",") if k.strip()]
                if keys:
                    return keys
    raise RuntimeError("No API keys found")

def default_thinking_level(model: str) -> str:
    lowered = model.lower()
    if "gemma" in lowered or "flash-lite" in lowered:
        return "minimal"
    if model == "gemini-3.6-flash":
        return "medium"
    return "high"


def call_gemma(
    prompt,
    sched,
    max_tok=8192,
    temp=0.5,
    json_mode=False,
    max_attempts=20,
    model=None,
    thinking_level=None,
):
    selected_model = model or MODEL
    endpoint = ENDPOINT_TPL.format(model=selected_model)
    gen_cfg = {"maxOutputTokens": max_tok}
    if temp is not None and selected_model not in SAMPLING_DEPRECATED_MODELS:
        gen_cfg["temperature"] = temp
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
    gen_cfg["thinkingConfig"] = {
        "thinkingLevel": thinking_level or default_thinking_level(selected_model)
    }
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    for attempt in range(max_attempts):
        key = sched.acquire()
        try:
            r = requests.post(f"{endpoint}?key={key}", json=body, timeout=240)
        except Exception as e:
            if is_runtime_filesystem_error(e):
                raise RuntimeError(f"runtime filesystem unavailable; encrypted home may be locked: {e}") from e
            LOG(f"  net err: {e}"); time.sleep(30); continue
        if r.status_code == 429 or r.status_code >= 500:
            LOG(f"  {r.status_code} backoff")
            time.sleep(60 - (time.time() % 60) + 0.5 + random.random())
            continue
        if not r.ok:
            err_text = r.text[:150]
            LOG(f"  err {r.status_code}: {err_text}")
            lowered = r.text.lower()
            if r.status_code == 400 and (
                "input token count exceeds" in lowered
                or "maximum number of tokens" in lowered
            ):
                raise RuntimeError(f"input token count exceeds model limit: {err_text}")
            time.sleep(10); continue
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            # Gemma는 thought=True 블록을 먼저 반환 — 실제 답변 part만 골라냄
            for p in parts:
                if p.get("thought"): continue
                t = p.get("text", "")
                if t: return t
            # 모든 part가 thought거나 빈 경우
            time.sleep(3); continue
        except Exception:
            time.sleep(3); continue
    raise RuntimeError(f"API failed after {max_attempts} attempts")


# ── Parsers ─────────────────────────────────────────────────────

MODEL_NAME_RE = re.compile(
    r"(?:\b(?:GPT|ChatGPT|Claude|Opus|Sonnet|Haiku|Gemini|Gemma|DeepSeek|Qwen|Kimi|Grok|Llama|Mistral|Mixtral|Sora|Veo|Imagen)\b|"
    r"제미니|클로드|딥시크|그록|라마)",
    re.I,
)
MODEL_LAUNCH_RE = re.compile(
    r"(출시|공개|발표|런칭|배포|릴리스|등장|나왔다|"
    r"released|launched|announced|unveiled|introduced|rolled\s*out|available)",
    re.I,
)
OFFICIAL_SOURCE_RE = re.compile(
    r"(blog\.google|ai\.google\.dev|developers\.googleblog\.com|openai\.com/(?:blog|index)|"
    r"anthropic\.com/(?:news|research)|deepmind\.google|model\s*card|release\s*notes|changelog|"
    r"공식\s*(?:블로그|문서|API|모델\s*카드|릴리스\s*노트|발표문)|"
    r"(?:모델\s*카드|릴리스\s*노트|API\s*문서|개발자\s*문서))",
    re.I,
)


def _is_unsourced_model_launch_claim(headline: str, body: str) -> bool:
    text = f"{headline}\n{body}"
    if not MODEL_NAME_RE.search(text):
        return False
    if not MODEL_LAUNCH_RE.search(text):
        return False
    return not OFFICIAL_SOURCE_RE.search(text)


def _soften_launch_headline(headline: str) -> str:
    softened = re.sub(r"(공식\s*)?(출시|공개|발표)\s*소식", r"\2 주장", headline)
    softened = re.sub(r"(공식\s*)?(출시|공개|발표)(?=$|\s|,)", r"\2 주장", softened)
    if not softened.startswith(("미확인:", "루머:")):
        softened = f"미확인: {softened}"
    return softened


def _soften_launch_body(body: str) -> str:
    softened = body
    replacements = [
        (r"출시했다는 소식", "출시했다는 주장이"),
        (r"공개했다는 소식", "공개했다는 주장이"),
        (r"발표했다는 소식", "발표했다는 주장이"),
        (r"출시했습니다", "출시했다는 주장이 나왔습니다"),
        (r"공개했습니다", "공개했다는 주장이 나왔습니다"),
        (r"발표했습니다", "발표했다는 주장이 나왔습니다"),
        (r"출시했다", "출시했다는 주장이 나왔다"),
        (r"공개했다", "공개했다는 주장이 나왔다"),
        (r"발표했다", "발표했다는 주장이 나왔다"),
    ]
    for pattern, repl in replacements:
        softened = re.sub(pattern, repl, softened)
    prefix = "공식 출처가 확인되지 않은 채팅 기반 주장으로, 농담이나 루머일 가능성이 있어 미확인 소식으로 분류합니다. "
    if not softened.startswith(prefix):
        softened = prefix + softened
    return softened


def sanitize_scan_article(article: dict) -> dict:
    sanitized = dict(article)
    headline = str(sanitized.get("headline", "")).strip()
    body = str(sanitized.get("body", "")).strip()
    if _is_unsourced_model_launch_claim(headline, body):
        sanitized["headline"] = _soften_launch_headline(headline)
        sanitized["body"] = _soften_launch_body(body)
        sanitized["category"] = "rumor"
        sanitized["trust"] = "low"
    return sanitized


PRODUCT_STORY_PATTERNS = {
    "gpt-image-2": re.compile(
        r"(?:\bGPT[-\s]?Image[-\s]?2\b|ChatGPT\s+Images\s+2(?:\.0)?|"
        r"\bImages\s+2\.0\b|\bImage\s+2\b)",
        re.I,
    ),
}
PRODUCT_FOLLOWUP_RE = re.compile(
    r"(리더보드|벤치마크|성능|점수|기반\s*모델|메타데이터|C2PA|시스템\s*카드|"
    r"안전|검열|거부|가격|토큰|스냅샷|레이트\s*리밋|Heavy\s*Thinking|Thinking\s*모드|"
    r"leaderboard|benchmark|system\s*card|metadata|snapshot|rate\s*limit|pricing|safety)",
    re.I,
)


def _article_text(article: dict) -> str:
    return f"{article.get('headline', '')}\n{article.get('body', '')}"


def _product_story_keys(article: dict) -> set[str]:
    text = _article_text(article)
    return {key for key, pattern in PRODUCT_STORY_PATTERNS.items() if pattern.search(text)}


def _is_product_release_coverage(article: dict) -> bool:
    text = _article_text(article)
    if _is_low_trust_rumor(article):
        return False
    return bool(MODEL_LAUNCH_RE.search(text))


def _is_distinct_product_followup(article: dict) -> bool:
    return bool(PRODUCT_FOLLOWUP_RE.search(_article_text(article)))


def apply_product_story_guard(new_articles: list, existing_articles: list) -> tuple[list, list]:
    """Drop repeated product release stories, but keep clearly new follow-up details."""
    existing_release_keys = set()
    for article in existing_articles:
        if _is_product_release_coverage(article):
            existing_release_keys.update(_product_story_keys(article))

    if not existing_release_keys:
        return list(new_articles), []

    kept, dropped = [], []
    for article in new_articles:
        keys = _product_story_keys(article)
        has_existing_release = bool(keys & existing_release_keys)
        is_release_claim = bool(MODEL_LAUNCH_RE.search(_article_text(article)))
        is_followup = _is_distinct_product_followup(article)
        if has_existing_release and is_release_claim and not is_followup:
            dropped.append(article["id"])
            continue

        item = dict(article)
        if has_existing_release and not item.get("headline", "").startswith("후속: "):
            item["headline"] = f"후속: {item.get('headline', '')}"
        kept.append(item)
    return kept, dropped


def parse_envelope(text):
    w = re.search(r'<\s*data\s*>([\s\S]*?)<\s*/\s*data\s*>', text, re.I)
    if not w: raise ValueError("no <data> wrapper")
    return w.group(1)

def parse_chunk_articles(text):
    """Parse priming-continuation: prepend '{"articles":' and try to find balanced JSON.
    Also try raw parse if whole text is JSON."""
    s = text.strip()
    s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s, flags=re.M).strip()

    # Strategy 1: the response starts with a '[' or '"articles"' etc — try prepending
    # Gemma was primed with '{"articles":' so it should continue with array or value
    candidates = []
    if s and s[0] in '[':
        candidates.append('{"articles":' + s + '}')
        candidates.append('{"articles":' + s.rstrip(',}] ').rstrip() + ']}')
    # Strategy 2: find balanced JSON object anywhere
    start = s.find('{')
    end = s.rfind('}')
    if start != -1 and end > start:
        candidates.append(s[start:end+1])
    # Strategy 3: find articles array in text
    m = re.search(r'"articles"\s*:\s*(\[[\s\S]*?\])', s)
    if m:
        candidates.append('{"articles":' + m.group(1) + '}')

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            # try trimming trailing junk
            try:
                obj = json.loads(re.sub(r',\s*([}\]])', r'\1', cand))
            except Exception:
                continue
        arts = obj.get("articles") if isinstance(obj, dict) else None
        if isinstance(arts, list):
            out = []
            for a in arts:
                if not isinstance(a, dict): continue
                hl = str(a.get("headline", "")).strip().strip('"\'')
                bd = str(a.get("body", "")).strip().strip('"\'')
                if len(hl) <= 4 or len(bd) <= 40: continue
                # 보수적 default: 태그 누락/잘못된 값이면 rumor/low
                cat = str(a.get("category", "")).strip().lower()
                if cat not in ("news", "rumor"): cat = "rumor"
                trust = str(a.get("trust", "")).strip().lower()
                if trust not in ("high", "low"): trust = "low"
                out.append(sanitize_scan_article({"headline": hl, "body": bd, "category": cat, "trust": trust}))
            return out
    return []

def parse_placement_json(text):
    """Parse {"top": "id"|null, "main": [ids], "side": [ids]}"""
    s = text.strip()
    s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s, flags=re.M).strip()
    start = s.find('{'); end = s.rfind('}')
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    obj = json.loads(s[start:end+1])
    def as_list(v):
        if v is None or v == "": return []
        if isinstance(v, str): return [v.strip()] if v.strip() else []
        if isinstance(v, list): return [str(x).strip() for x in v if str(x).strip()]
        raise ValueError(f"bad type: {type(v)}")
    return {"top": as_list(obj.get("top")), "main": as_list(obj.get("main")), "side": as_list(obj.get("side"))}


# ── State ───────────────────────────────────────────────────────

def new_id(now: datetime, n: int) -> str:
    return f"art-{now.strftime('%Y%m%d%H%M')}-{n:02d}"

def load_state():
    if not ARTICLES_PATH.exists():
        return {
            "schema_version": 2, "last_run_at": None,
            "generated_at": datetime.now(KST).isoformat(),
            "journal": JOURNAL_NAME, "model": MODEL, "articles": [], "decision_log": [],
        }
    data = json.loads(ARTICLES_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") == 2:
        return data
    # migrate v1 → v2
    gen_at = data.get("generated_at") or datetime.now(KST).isoformat()
    period_end = (data.get("period") or {}).get("end")
    articles = []
    for a in data.get("articles", []):
        articles.append({
            "id": a.get("id") or f"legacy-{len(articles)+1:03d}",
            "headline": a["headline"],
            "body": a["body"],
            "created_at": gen_at,
            "placement": None,  # 분류 단계에서 채움
            "placed_at": gen_at,
        })
    return {
        "schema_version": 2,
        "last_run_at": period_end,  # 1회차 since = period.end
        "generated_at": gen_at,
        "journal": JOURNAL_NAME,
        "model": MODEL,
        "articles": articles,
        "decision_log": [],
    }

def save_state(state):
    bak = ARTICLES_PATH.with_suffix(".json.bak")
    if ARTICLES_PATH.exists():
        bak.write_text(ARTICLES_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    ARTICLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTICLES_PATH.write_text(payload, encoding="utf-8")
    PAGES_ARTICLES_PATH.write_text(payload, encoding="utf-8")

def git_relative(path: Path) -> str:
    return str(Path(path).resolve().relative_to(ROOT.resolve()))


def is_non_fast_forward_push(stderr: str) -> bool:
    message = str(stderr or "").lower()
    return "non-fast-forward" in message or "fetch first" in message


def publish_public_artifacts_from_remote_branch(rels: list[str], date_key: str) -> bool:
    """Publish from origin when the long-lived runtime checkout has diverged."""
    fetch = subprocess.run(
        ["git", "fetch", "origin", PUBLISH_BRANCH],
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed: {fetch.stderr.strip()}")

    with tempfile.TemporaryDirectory(prefix="signal-pages-publish-") as td:
        worktree = Path(td) / "worktree"
        add_worktree = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), f"origin/{PUBLISH_BRANCH}"],
            capture_output=True,
            text=True,
        )
        if add_worktree.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {add_worktree.stderr.strip()}")
        try:
            for rel in rels:
                source = ROOT / rel
                target = worktree / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

            add = subprocess.run(
                ["git", "-C", str(worktree), "add", "--", *rels],
                capture_output=True,
                text=True,
            )
            if add.returncode != 0:
                raise RuntimeError(f"isolated git add failed: {add.stderr.strip()}")
            diff = subprocess.run(
                ["git", "-C", str(worktree), "diff", "--cached", "--quiet", "--", *rels],
                capture_output=True,
                text=True,
            )
            if diff.returncode == 0:
                return False
            if diff.returncode != 1:
                raise RuntimeError(f"isolated git diff failed: {diff.stderr.strip()}")

            commit = subprocess.run(
                [
                    "git", "-C", str(worktree),
                    "-c", f"user.name={GIT_USER_NAME}",
                    "-c", f"user.email={GIT_USER_EMAIL}",
                    "commit", "-m", f"chore: publish {JOURNAL_NAME} {date_key}", "--", *rels,
                ],
                capture_output=True,
                text=True,
            )
            if commit.returncode != 0:
                raise RuntimeError(f"isolated git commit failed: {commit.stderr.strip()}")
            push = subprocess.run(
                ["git", "-C", str(worktree), "push", "origin", f"HEAD:{PUBLISH_BRANCH}"],
                capture_output=True,
                text=True,
            )
            if push.returncode != 0:
                raise RuntimeError(f"isolated git push failed: {push.stderr.strip()}")
            return True
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                capture_output=True,
                text=True,
            )

def publish_public_artifacts(paths: list[Path], run_at: datetime) -> bool:
    """Commit and push only public artifacts needed by GitHub Pages."""
    rels = []
    for path in paths:
        p = Path(path)
        if not p.exists():
            continue
        try:
            rel = git_relative(p)
        except ValueError:
            continue
        if rel not in rels:
            rels.append(rel)
    if not rels:
        return False

    add = subprocess.run(["git", "add", "--", *rels], capture_output=True, text=True)
    if add.returncode != 0:
        raise RuntimeError(f"git add failed: {add.stderr.strip()}")

    diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--", *rels], capture_output=True, text=True)
    if diff.returncode == 0:
        return False
    if diff.returncode != 1:
        raise RuntimeError(f"git diff failed: {diff.stderr.strip()}")

    date_key = run_at.astimezone(KST).date().isoformat()
    commit = subprocess.run(
        [
            "git",
            "-c", f"user.name={GIT_USER_NAME}",
            "-c", f"user.email={GIT_USER_EMAIL}",
            "commit", "-m", f"chore: publish {JOURNAL_NAME} {date_key}", "--", *rels,
        ],
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"git commit failed: {commit.stderr.strip()}")

    push = subprocess.run(["git", "push", "origin", f"HEAD:{PUBLISH_BRANCH}"], capture_output=True, text=True)
    if push.returncode != 0:
        if is_non_fast_forward_push(push.stderr):
            LOG("pages publish branch diverged; retrying from an isolated origin worktree")
            return publish_public_artifacts_from_remote_branch(rels, date_key)
        raise RuntimeError(f"git push failed: {push.stderr.strip()}")
    return True

def publish_after_run(export_path: Path, run_at: datetime) -> None:
    try:
        changed = publish_public_artifacts([ARTICLES_PATH, PAGES_ARTICLES_PATH, export_path], run_at)
        LOG("pages publish pushed" if changed else "pages publish skipped (no changes)")
    except Exception as e:
        LOG(f"pages publish failed: {e}")

def fallback_daily_summary_title(new_articles: list) -> str:
    if not new_articles:
        return "새 업데이트 없이 이어지는 하루"
    first = str(new_articles[0].get("headline") or "").strip()
    if first:
        return first[:42].rstrip(" .,…") + ("…" if len(first) > 42 else "")
    return DAILY_SUMMARY_FALLBACK_TITLE

def build_daily_summary_payload(body: str, new_articles: list, run_at: datetime, title: str | None = None) -> dict:
    date_key = run_at.astimezone(KST).date().isoformat()
    return {
        "schema_version": 1,
        "title": str(title or fallback_daily_summary_title(new_articles)).strip() or DAILY_SUMMARY_FALLBACK_TITLE,
        "date": date_key,
        "generated_at": run_at.isoformat(),
        "article_count": len(new_articles),
        "body": str(body or "").strip(),
    }

def fallback_daily_summary_body(new_articles: list) -> str:
    if not new_articles:
        return "오늘 새로 확인된 업데이트는 없습니다. 기존 기사와 아카이브는 그대로 유지됩니다."
    parts = []
    for a in new_articles[:10]:
        tag = "찌라시" if a.get("category") == "rumor" else "뉴스"
        trust = "낮은 신뢰" if a.get("trust") == "low" else "확인된 흐름"
        parts.append(f"{a.get('headline', '').strip()}({tag}, {trust})")
    if len(new_articles) > 10:
        parts.append(f"그 외 {len(new_articles) - 10}건")
    return "오늘 업데이트는 " + ", ".join(parts) + "을 중심으로 정리됩니다."

def prompt_daily_summary(new_articles: list) -> str:
    if not new_articles:
        return ""
    lines = []
    for i, a in enumerate(new_articles, 1):
        body = (a.get("body") or "").strip()
        lines.append(
            f"### {i}. {a.get('headline','')}\n"
            f"- category: {a.get('category','news')}\n"
            f"- trust: {a.get('trust','high')}\n"
            f"- body:\n{body}"
        )
    return f"""역할: First Light AI 데일리 에디터.
오늘 새로 업데이트된 청크 처리 결과의 기사 본문 전체를 읽고, 하나의 자세한 데일리 요약글로 다시 쓰세요.

## 작성 규칙
- 오늘 흐름을 대표하는 한국어 제목을 직접 붙일 것.
- 제목은 18~42자, 과장 없이 핵심 축을 압축.
- 한국어 본문 1200~2200자.
- 입력된 각 기사 본문 전체를 근거로 사용하고, 중요한 디테일을 버리지 말 것.
- 기사 목록식 요약이 아니라 하루 흐름을 하나의 긴 글처럼 연결.
- 중요한 축, 루머/낮은 신뢰 항목의 불확실성, 모델·제품·인프라·보안·비즈니스 흐름을 함께 정리.
- 같은 사건이 반복되면 하나로 묶되, 서로 다른 의미나 파급효과는 보존.
- 입력에 없는 사실을 만들지 말 것.
- 출력은 JSON 객체 하나만.

## 스키마
{{"title":"오늘 흐름을 대표하는 한국어 제목","body":"요약 본문"}}

## 오늘 새 기사
{chr(10).join(lines)}

JSON만 출력:"""

def parse_daily_summary_response(text: str) -> dict:
    s = text.strip()
    s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s, flags=re.M).strip()
    start = s.find('{'); end = s.rfind('}')
    if start != -1 and end > start:
        obj = json.loads(s[start:end+1])
        title = str(obj.get("title") or "").strip()
        body = str(obj.get("body") or obj.get("summary") or "").strip()
        return {"title": title, "body": body}
    return {"title": "", "body": s.strip()}

def parse_daily_summary_body(text: str) -> str:
    return parse_daily_summary_response(text)["body"]

def generate_daily_summary(new_articles: list, run_at: datetime, sched) -> dict:
    if not new_articles or sched is None:
        return build_daily_summary_payload(fallback_daily_summary_body(new_articles), new_articles, run_at)
    prompt = prompt_daily_summary(new_articles)
    try:
        raw = call_gemma(prompt, sched, max_tok=4096, temp=0.35, json_mode=True)
        parsed = parse_daily_summary_response(raw)
        title = parsed["title"]
        body = parsed["body"]
        if len(body) < 40:
            body = fallback_daily_summary_body(new_articles)
            title = fallback_daily_summary_title(new_articles)
    except Exception as e:
        LOG(f"daily summary fallback: {e}")
        body = fallback_daily_summary_body(new_articles)
        title = fallback_daily_summary_title(new_articles)
    return build_daily_summary_payload(body, new_articles, run_at, title=title)

def write_daily_new_articles_export(new_articles: list, run_at: datetime, daily_summary: dict | None = None) -> Path:
    """Write the per-day new-article JSON used by downstream local pipelines."""
    EXPORTS_ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    date_key = run_at.astimezone(KST).date().isoformat()
    out_path = EXPORTS_ARTICLES_DIR / f"{date_key}.json"
    if daily_summary is None:
        daily_summary = build_daily_summary_payload(fallback_daily_summary_body(new_articles), new_articles, run_at)
    payload = {
        "schema_version": 1,
        "journal": JOURNAL_NAME,
        "date": date_key,
        "generated_at": run_at.isoformat(),
        "count": len(new_articles),
        "daily_summary": daily_summary,
        "articles": new_articles,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


# ── Discord ─────────────────────────────────────────────────────

def discord_export(since_iso: str, channel_id: str = CHANNEL_ID) -> Path:
    """Export Discord chat through the local browser-session collector."""
    since = datetime.fromisoformat(since_iso).astimezone(KST)
    after_kst = since.strftime("%Y-%m-%d %H:%M:%S")
    LOG(f"[discord] channel {channel_id} --after-kst '{after_kst}'")
    script = ROOT / "discord_export_linux.py"
    cmd = [sys.executable, str(script), "--channel", channel_id, "--after-kst", after_kst]
    LOG(f"  using: {script.name}")
    timeout = int(os.environ.get("DISCORD_EXPORT_TIMEOUT_SECONDS", "3600"))
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    stdout = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
    stderr = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
    if stderr:
        for line in stderr.splitlines():
            LOG(f"  [export-stderr] {line}")
    if r.returncode != 0:
        raise RuntimeError(f"Discord export failed: {stderr[-800:]}")
    for line in stdout.split("\n"):
        if line.startswith("final_file="):
            return Path(line.split("=", 1)[1].strip())
    raise RuntimeError(f"final_file not found:\n{stdout[-400:]}")


def generate_devmode_model_focus_article(now: datetime, sched, source_text: str | None = None) -> dict | None:
    if source_text is not None:
        chat = filter_chat_since(
            source_text,
            (now - timedelta(hours=MODEL_FOCUS_RECENT_HOURS)).isoformat(),
        ).strip()
        LOG(
            f"daily nuggets chat: {len(chat):,} chars "
            f"(latest {MODEL_FOCUS_RECENT_HOURS}h derived from reused 24h source)"
        )
        return generate_model_focus_article(chat, now, sched)

    since = now - timedelta(hours=MODEL_FOCUS_LOOKBACK_HOURS)
    try:
        export_path = discord_export(since.isoformat(), channel_id=DEVMODE_CHANNEL_ID)
        full_chat = read_chat_text(export_path).strip()
        chat = filter_chat_since(
            full_chat,
            (now - timedelta(hours=MODEL_FOCUS_RECENT_HOURS)).isoformat(),
        ).strip()
    except Exception as e:
        LOG(f"daily nuggets export failed: {e}; skipping daily-nuggets article")
        return None
    LOG(f"daily nuggets chat: {len(chat):,} chars (latest {MODEL_FOCUS_RECENT_HOURS}h)")
    return generate_model_focus_article(chat, now, sched)


def parse_kst_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def resolve_run_at(as_of: str | None = None) -> datetime:
    return parse_kst_iso(as_of) if as_of else datetime.now(KST)


def single_export_since_for_run(scan_since_iso: str, now: datetime) -> str:
    scan_since = parse_kst_iso(scan_since_iso)
    model_since = now.astimezone(KST) - timedelta(hours=MODEL_FOCUS_LOOKBACK_HOURS)
    return min(scan_since, model_since).isoformat()


def model_focus_source_from_primary_chat(
    chat: str,
    since_iso: str,
    now: datetime,
    explicit_chat_file: bool = False,
) -> str | None:
    if explicit_chat_file:
        return chat
    try:
        since = parse_kst_iso(since_iso)
    except Exception:
        return None
    required_start = now - timedelta(hours=MODEL_FOCUS_LOOKBACK_HOURS) + MODEL_FOCUS_REUSE_TOLERANCE
    if since <= required_start:
        return chat
    return None


MSG_HEADER_TIME_RE = re.compile(
    r"^\[(\d{4})\. (\d{1,2})\. (\d{1,2})\. (오전|오후) (\d{1,2}):(\d{2})\]"
)


def parse_message_header_time(block: str) -> datetime | None:
    first_line = block.splitlines()[0] if block else ""
    m = MSG_HEADER_TIME_RE.match(first_line)
    if not m:
        return None
    year, month, day, period, hour, minute = m.groups()
    hour_i = int(hour)
    if period == "오전":
        hour_i = 0 if hour_i == 12 else hour_i
    else:
        hour_i = hour_i if hour_i == 12 else hour_i + 12
    return datetime(int(year), int(month), int(day), hour_i, int(minute), tzinfo=KST)


def filter_chat_since(chat: str, since_iso: str) -> str:
    blocks = split_message_blocks(chat)
    if not blocks:
        return chat
    since = parse_kst_iso(since_iso).replace(second=0, microsecond=0)
    kept = []
    for block in blocks:
        msg_time = parse_message_header_time(block)
        if msg_time is None or msg_time >= since:
            kept.append(block)
    return "\n\n".join(kept)


def model_focus_summary_payload(article: dict) -> dict:
    generated_at = article.get("created_at") or article.get("placed_at") or datetime.now(KST).isoformat()
    try:
        run_at = datetime.fromisoformat(generated_at).astimezone(KST)
    except Exception:
        run_at = datetime.now(KST)
    return {
        "schema_version": 1,
        "title": MODEL_FOCUS_HEADLINE,
        "date": run_at.date().isoformat(),
        "generated_at": run_at.isoformat(),
        "body": article.get("body", ""),
    }


def current_model_focus_summary_for_daily(state: dict, run_at: datetime, article_count: int) -> dict | None:
    summary = state.get("model_focus_summary")
    if not isinstance(summary, dict) or not str(summary.get("body") or "").strip():
        return None
    expected_date = run_at.astimezone(KST).date().isoformat()
    if summary.get("date") != expected_date:
        return None
    payload = dict(summary)
    payload["title"] = MODEL_FOCUS_HEADLINE
    payload["article_count"] = article_count
    return payload


def upsert_model_focus_article(state: dict, new_articles: list, article: dict | None) -> list:
    if not article:
        return new_articles
    article_id = article["id"]
    state["articles"] = [a for a in state.get("articles", []) if a.get("id") != article_id]
    kept_new = [a for a in new_articles if a.get("id") != article_id]
    kept_new.append(article)
    state["model_focus_summary"] = model_focus_summary_payload(article)
    LOG(f"model focus article added: {article_id}")
    return kept_new

def read_chat_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    parts = text.split("=" * 62)
    return parts[2] if len(parts) >= 3 else text

MSG_HDR = re.compile(r'(^\[\d{4}\. \d{1,2}\. \d{1,2}\. (?:오전|오후) \d{1,2}:\d{2}\][^\n]*\n)', re.M)


def split_message_blocks(text: str) -> list[str]:
    parts = MSG_HDR.split(text)
    blocks = []
    i = 1
    while i < len(parts):
        hdr = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        block = (hdr + body).strip()
        if block:
            blocks.append(block)
        i += 2
    if blocks:
        return blocks
    return [b.strip() for b in re.split(r"\n{3,}", text) if b.strip()]


def chunk_by_messages(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    blocks = split_message_blocks(text)
    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) > max_chars:
            chunks.append(cur); cur = b
        else:
            cur += b
    if cur.strip(): chunks.append(cur)
    return chunks


def split_chunk_for_retry(chunk: str) -> tuple[str, str]:
    mid = len(chunk) // 2
    separators = []
    before = chunk.rfind("\n\n\n", 0, mid)
    after = chunk.find("\n\n\n", mid)
    if before > 0:
        separators.append((abs(before - mid), before, 3))
    if after > 0:
        separators.append((abs(after - mid), after, 3))
    if separators:
        _, split_at, sep_len = min(separators)
        left = chunk[:split_at].strip()
        right = chunk[split_at + sep_len:].strip()
    else:
        left = chunk[:mid].strip()
        right = chunk[mid:].strip()
    if not left or not right:
        return chunk[:mid].strip(), chunk[mid:].strip()
    return left, right


# ── Prompts ─────────────────────────────────────────────────────

def prompt_scan_chunk(chunk: str, titles: list[str]) -> str:
    titles_block = "\n".join(f"- {t}" for t in titles) or "(없음)"
    return f"""역할: AI 뉴스 에디터. Discord 채팅에서 '새로운' AI 소식을 기사화하고, 각 기사에 category·trust 태그 부여.

## 출력 규칙
- JSON 객체 하나만. 마크다운 코드펜스 금지. 첫 문자 `{{`, 마지막 `}}`.

## 스키마
{{"articles":[{{"headline":"제목","body":"본문","category":"news|rumor","trust":"high|low"}}]}}

- **category**:
  - "news" = 공식 발표/모델 카드/API 공개/블로그 등 공식 소스 근거 있는 소식
  - "rumor" = 찌라시/루머/미확인 주장/개인 트윗만/스크린샷 유출/"~인 것 같다"
- **trust**:
  - "high" = 여러 소스 일관, 구체적 근거, 공식 출처 포함
  - "low" = 단일 언급, 추측, 농담일 수도 있음, 애매함

## 규칙
- 새 소식 없으면: {{"articles": []}}
- '이미 다룬 기사 제목'에 있는 소식은 재작성 금지
- **찌라시도 빠짐없이 출력** (category=rumor 로 태그 붙여서)
- 신모델/제품의 "출시·공개·발표" 주장은 공식 블로그·공식 문서·모델 카드·API 문서·릴리스 노트 같은 출처가 본문에 확인될 때만 news/high.
- 공식 출처가 없는 모델 출시설, 농담, 밈, 패러디성 선언은 절대 확정 기사처럼 쓰지 말고 category=rumor, trust=low, 제목에 "미확인" 또는 "루머"를 넣기.
- 본문: 한국어 300~600자. 단정보단 "~라는 소식", "~라고 알려졌다" 같은 전언 형태 선호 (특히 rumor)
- 유저이름·닉네임 언급 금지. 채팅에 실제 나온 내용만.

## 이미 다룬 기사 제목 (최근 {TITLES_FOR_DEDUP}개)
{titles_block}

## Discord 채팅 (이번 청크)
{chunk}

JSON만 출력:"""


MODEL_FOCUS_INSTRUCTION = """자세히 정리좀, 찌라시 빠짐없이 디스코드 대화내용', 'devmode' 이런 용어는 쓰지말고 요약해줘 이것만 봐도 배부른 알짜배기만
모았습니다라고 시작해줘."""


def prompt_model_focus_article(source_text: str, source_label: str = "최신 12시간 자료") -> str:
    return f"""{MODEL_FOCUS_INSTRUCTION}

{source_text}"""


def prompt_model_focus_merge(part_summaries: list[str]) -> str:
    parts = "\n\n".join(
        f"[부분 {i}]\n{summary.strip()}"
        for i, summary in enumerate(part_summaries, 1)
        if summary.strip()
    )
    return f"""{MODEL_FOCUS_INSTRUCTION}

{parts}"""


def parse_model_focus_response(text: str) -> str:
    s = text.strip()
    s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s, flags=re.M).strip()
    start = s.find('{'); end = s.rfind('}')
    if s.startswith("{") and s.endswith("}") and start != -1 and end > start:
        try:
            obj = json.loads(s[start:end+1])
        except json.JSONDecodeError:
            return s
        body = obj.get("body") or obj.get("summary") or obj.get("article") or ""
        return str(body).strip()
    return s


def call_model_focus_summary(prompt: str, sched) -> str:
    failures = []
    for model in MODEL_FOCUS_MODELS:
        try:
            raw = call_gemma(
                prompt,
                sched,
                max_tok=8192,
                temp=None,
                json_mode=False,
                max_attempts=MODEL_FOCUS_GEMMA_MAX_ATTEMPTS,
                model=model,
            )
            body = parse_model_focus_response(raw)
            if body.strip():
                return body
            failures.append(f"{model}: empty response")
        except Exception as e:
            failures.append(f"{model}: {e}")
            LOG(f"daily nuggets model fallback: {model} failed: {e}")
    raise RuntimeError("all daily-nuggets models failed: " + " | ".join(failures))


def merge_model_focus_summaries(part_summaries: list[str], sched) -> str:
    summaries = [s.strip() for s in part_summaries if s and s.strip()]
    if not summaries:
        return ""
    if len(summaries) == 1:
        return summaries[0]
    try:
        body = call_model_focus_summary(prompt_model_focus_merge(summaries), sched)
        if len(body) >= 40:
            return body
        LOG("model focus merge fallback: response too short; joining partial summaries")
    except Exception as e:
        LOG(f"model focus merge fallback: {e}; joining partial summaries")
    return "\n\n".join(summaries)


def summarize_model_focus_source(
    source_text: str,
    sched,
    source_label: str = "최신 12시간 자료",
    min_chars: int = MODEL_FOCUS_MIN_SPLIT_CHARS,
    min_body_chars: int = 40,
    direct_max_chars: int = MODEL_FOCUS_DIRECT_MAX_CHARS,
) -> str:
    source = source_text.strip()
    if not source or sched is None:
        return ""
    if len(source) <= direct_max_chars:
        try:
            body = call_model_focus_summary(prompt_model_focus_article(source, source_label), sched)
            if len(body) >= min_body_chars:
                return body
            LOG(f"model focus fallback: response too short for {source_label}")
        except Exception as e:
            LOG(f"model focus fallback: {e}; splitting {source_label}")
    else:
        LOG(
            f"model focus pre-split: {source_label} is {len(source):,} chars "
            f"(direct limit {direct_max_chars:,})"
        )
    if len(source) <= min_chars:
        raise RuntimeError(
            f"daily-nuggets fragment failed after all model/key attempts: "
            f"{source_label} ({len(source):,} chars)"
        )
    left, right = split_chunk_for_retry(source)
    part_summaries = []
    for idx, part in enumerate((left, right), 1):
        label = f"{source_label} / 부분 {idx}"
        summary = summarize_model_focus_source(
            part,
            sched,
            label,
            min_chars=min_chars,
            min_body_chars=16,
            direct_max_chars=direct_max_chars,
        )
        if summary:
            part_summaries.append(summary)
    return merge_model_focus_summaries(part_summaries, sched)


def generate_model_focus_article(
    chat: str,
    now: datetime,
    sched,
    min_chars: int = MODEL_FOCUS_MIN_SPLIT_CHARS,
) -> dict | None:
    source = chat.strip()
    if not source or sched is None:
        return None
    try:
        body = summarize_model_focus_source(source, sched, min_chars=min_chars)
    except Exception as e:
        LOG(f"daily nuggets generation failed without dropping fragments: {e}")
        return None
    if len(body) < 40:
        LOG("model focus fallback: response too short; skipping model-focus article")
        return None
    date_key = now.astimezone(KST).strftime("%Y%m%d")
    return {
        "id": f"model-focus-{date_key}",
        "headline": MODEL_FOCUS_HEADLINE,
        "body": body,
        "category": "news",
        "trust": "high",
        "kind": MODEL_FOCUS_KIND,
        "created_at": now.isoformat(),
        "placement": None,
        "placed_at": now.isoformat(),
    }


def prompt_classify(active: list, new: list, decision_log: list) -> tuple[str, dict]:
    # Build short ID map (1, 2, 3, ...) for prompt clarity
    ordered = active + new
    short2real = {str(i + 1): a["id"] for i, a in enumerate(ordered)}
    real2short = {v: k for k, v in short2real.items()}

    def art_block(a, is_new=False):
        if is_new:
            tag = "[NEW]"
        else:
            tag = f"[{a['placement'] or 'unplaced'} · placed_at={a['placed_at']}]"
        body_brief = (a['body'] or '')[:180].replace('\n', ' ')
        return f"[id={real2short[a['id']]}] {tag} {a['headline']} — {body_brief}"

    log_lines = []
    for entry in decision_log:
        placements = entry.get("placements", {})
        summary = ", ".join(f"{short2real_get(short2real, iid)}→{p}" for iid, p in placements.items())
        log_lines.append(f"- {entry['run_at']} (new={entry.get('new_count',0)})")
    log_block = "\n".join(log_lines) or "(없음)"

    old_block = "\n\n".join(art_block(a) for a in active) or "(없음)"
    new_block = "\n\n".join(art_block(a, is_new=True) for a in new) or "(없음)"

    prompt = f"""당신은 AI 뉴스 편집자입니다. 아래 모든 활성 기사를 TOP/MAIN/SIDE 중 하나로 분류하세요.

## 규칙
- TOP: 최대 1개 (또는 0개). 세계적·기술적 돌파구 급만.
- MAIN: 최대 {MAX_MAIN}개. 중요하지만 TOP은 아닌 것.
- SIDE: 무제한. 그 외 전부.
- **모든 활성 기사가 정확히 한 분류를 받아야 함** (기존 + 새 기사 합쳐서).
- 이번 배치 새 기사는 프론트 후보로 우선 검토. 교체가 애매하면 새 기사 쪽을 더 위에 둔다.
- 새 기사가 7개 미만이면 남는 TOP/MAIN 칸은 기존 중요 기사로 채운다.
- 시간별 잡스러운 소식이 큰 발표(신모델 공개, 메이저 업데이트 등)를 밀어내지 않도록 보수적 판단.
- placed_at이 오래되고 새 기사가 명백히 더 중대하면 교체 가능.

## 최근 {DECISION_LOG_HOURS}h 결정 로그 (일관성 참고용)
{log_block}

## 현재 활성 기사
{old_block}

## 이번 배치 새 기사
{new_block}

## 출력 (JSON만, 다른 텍스트 절대 금지)
{{"top": "id" 또는 null, "main": ["id", ...], "side": ["id", ...]}}

- 각 id는 위 목록의 [id=N] 값(문자열 "1", "2"...).
- top이 없으면 null.
- main은 최대 {MAX_MAIN}개.
- 모든 활성 기사(현재+새 기사)가 top/main/side 중 한 곳에만 정확히 1번 나와야 함.

응답은 `{{`로 시작해서 `}}`로 끝. 다른 설명 금지:"""
    return prompt, short2real


def select_active_for_classification(active: list, limit: int = CLASSIFY_ACTIVE_LIMIT) -> list:
    """Keep the current front page and the newest archive items inside the LLM prompt budget."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(active) <= limit:
        return list(active)
    front = [a for a in active if a.get("placement") in {"top", "main"}]
    front_ids = {a["id"] for a in front}
    archive = sorted(
        (a for a in active if a["id"] not in front_ids),
        key=_created_at_sort_value,
        reverse=True,
    )
    return (front + archive)[:max(limit, len(front))]


def short2real_get(m, k):
    return m.get(k, k)


# ── Validation ──────────────────────────────────────────────────

def validate_placement(p: dict, valid_shorts: set) -> str | None:
    """관대한 검증 + 자동 정리: top>main>side 우선순위로 중복 제거. missing은 side로 기본 배치."""
    # 중복 자동 해결: 같은 id가 여러 그룹에 있으면 top > main > side 우선
    seen = set()
    for k in ("top", "main", "side"):
        p[k] = [x for x in p[k] if not (x in seen or seen.add(x))]
    # top ≤1 강제 (초과분은 main으로)
    if len(p["top"]) > 1:
        overflow = p["top"][1:]
        p["top"] = p["top"][:1]
        p["main"] = list(dict.fromkeys(p["main"] + overflow))
    # main ≤MAX_MAIN 강제 (초과분은 side로)
    if len(p["main"]) > MAX_MAIN:
        overflow = p["main"][MAX_MAIN:]
        p["main"] = p["main"][:MAX_MAIN]
        p["side"] = list(dict.fromkeys(p["side"] + overflow))
    for k in ("top", "main", "side"):
        p[k] = [i for i in p[k] if i in valid_shorts]
    all_ids = p["top"] + p["main"] + p["side"]
    # missing 자동 side로 추가
    missing = valid_shorts - set(all_ids)
    if missing:
        p["side"] = p["side"] + sorted(missing)
    return None


def _created_at_sort_value(article: dict) -> float:
    try:
        return datetime.fromisoformat(article.get("created_at", "")).timestamp()
    except Exception:
        return 0.0


def _is_low_trust_rumor(article: dict) -> bool:
    return article.get("category") == "rumor" and article.get("trust") == "low"


def _is_model_focus_article(article: dict) -> bool:
    return article.get("kind") == MODEL_FOCUS_KIND or str(article.get("id", "")).startswith("model-focus-")


def prioritize_new_articles_for_front_page(all_articles: list, new_articles: list, placement_map: dict) -> tuple[dict, list]:
    """Force the front page to show fresh updates first while preserving archive dates."""
    if not new_articles:
        return placement_map, all_articles

    original_index = {a["id"]: i for i, a in enumerate(all_articles)}
    new_ids = {a["id"] for a in new_articles}
    placement_rank = {"top": 0, "main": 1, "side": 2}
    model_focus_articles = sorted(
        [a for a in all_articles if _is_model_focus_article(a)],
        key=lambda a: (-_created_at_sort_value(a), original_index.get(a["id"], 10**9)),
    )
    front_model_focus = model_focus_articles[0] if model_focus_articles else None
    model_focus_ids = {a["id"] for a in model_focus_articles}

    def editorial_rank(article):
        return (
            placement_rank.get(placement_map.get(article["id"], "side"), 2),
            original_index.get(article["id"], 10**9),
        )

    def front_rank(article):
        is_new = article["id"] in new_ids
        weak = _is_low_trust_rumor(article)
        if is_new and not weak:
            bucket = 0
        elif not is_new and not weak:
            bucket = 1
        elif is_new:
            bucket = 2
        else:
            bucket = 3
        return (bucket, *editorial_rank(article))

    normal_candidates = [a for a in all_articles if a["id"] not in model_focus_ids]
    model_focus_slot_count = 1 if front_model_focus else 0
    normal_front = sorted(normal_candidates, key=front_rank)[:FRONT_PAGE_SLOTS - model_focus_slot_count]
    if front_model_focus:
        if normal_front:
            front = [normal_front[0], front_model_focus] + normal_front[1:]
        else:
            front = [front_model_focus]
    else:
        front = normal_front
    front_ids = {a["id"] for a in front}

    final_map = {a["id"]: "side" for a in all_articles}
    if front_model_focus and not normal_front:
        final_map[front_model_focus["id"]] = "main"
    elif front:
        final_map[front[0]["id"]] = "top"
        for article in front[1:FRONT_PAGE_SLOTS]:
            final_map[article["id"]] = "main"

    side = [a for a in all_articles if a["id"] not in front_ids]
    side.sort(key=lambda a: (-_created_at_sort_value(a), original_index.get(a["id"], 10**9)))
    return final_map, front + side


# ── Main ────────────────────────────────────────────────────────

def title_norm(t: str) -> str:
    return re.sub(r'\W+', '', t.lower())

def title_tokens(t: str) -> set:
    """Normalize title to set of tokens (≥2 chars, lowercased)."""
    s = re.sub(r'[^\w가-힣]+', ' ', t.lower())
    return {w for w in s.split() if len(w) >= 2}

def title_similarity(a: str, b: str) -> float:
    """Jaccard of token sets."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb: return 0.0
    return len(ta & tb) / len(ta | tb)

def dedup_articles(articles: list, threshold: float = 0.4) -> list:
    """Greedy dedup: keep longest-body article per cluster."""
    out = []
    for a in sorted(articles, key=lambda x: -len(x.get("body", ""))):
        if any(title_similarity(a["headline"], b["headline"]) >= threshold for b in out):
            continue
        out.append(a)
    return out


# ── LLM-based dedup cluster (body 재작성 없음) ───────────────────

DEDUP_CLUSTER_PROMPT = """아래 기사들 중 **정확히 같은 사실·같은 이벤트**를 전달하는 경우에만 중복으로 판정.

## 중복 판정 엄격 기준 (모두 일치해야 함)
- 같은 회사·같은 제품·같은 날짜/버전
- 같은 사건·같은 발표·같은 행동
- 본질적으로 같은 뉴스 (표현만 다를 뿐)

## 중복 아님 (drop 하지 말 것)
- 같은 회사 다른 제품 (예: Kimi K2.6 vs Kimi 인프라)
- 같은 제품 다른 측면 (예: Opus 4.7 출시 vs Opus 4.7 해커톤)
- 서로 다른 루머·예측 (예: GPT-5.5 출시설 vs GPT-6 예측)
- 새로운 디테일 추가

각 중복 클러스터에서 가장 구체적인 것 1개만 keep, 나머지는 drop.
**제목/본문 재작성 절대 금지** — 원본 id만 keep/drop 결정.
독립 기사는 출력에 포함하지 마세요.

## 기사 (id | 제목 | 본문 일부)
{articles}

## 출력 (JSON만)
{{"clusters": [{{"keep": "id", "drop": ["id","id"]}}, ...]}}

확신 있는 중복만. 없으면 {{"clusters": []}}"""

def dedup_cluster(candidates, sched):
    """LLM으로 중복 클러스터 찾아 drop. body/headline 재작성 없이 원본 보존.
    Returns: (kept_list, dropped_ids_list)"""
    if len(candidates) <= 1:
        return list(candidates), []
    short_to_full = {c["id"].split("-")[-1]: c["id"] for c in candidates}
    lines = []
    for c in candidates:
        s = c["id"].split("-")[-1]
        body = (c["body"] or "").replace("\n", " ")[:250]
        lines.append(f"{s} | {c['headline']} | {body}")
    prompt = DEDUP_CLUSTER_PROMPT.format(articles="\n".join(lines))
    LOG(f"dedup_cluster: {len(candidates)} candidates, prompt {len(prompt):,} chars")

    # 재시도: 파싱 실패면 최대 3회 재시도. 여전히 실패면 candidates 전부 유지 (포기 없음)
    dropped = set()
    parsed_ok = False
    for attempt in range(1, 4):
        try:
            raw = call_gemma(prompt, sched, max_tok=8192, temp=0.2, json_mode=True)
        except Exception as e:
            LOG(f"  dedup API fail (attempt {attempt}): {e}; 전체 유지")
            break
        s = raw.strip()
        s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s, flags=re.M).strip()
        start = s.find('{'); end = s.rfind('}')
        try:
            if start != -1 and end > start:
                obj = json.loads(s[start:end+1])
                for cluster in obj.get("clusters", []) or []:
                    for did in cluster.get("drop", []) or []:
                        did = str(did).strip().strip('"\'')
                        if not did: continue
                        stripped = did.lstrip('0') or '0'
                        resolved = short_to_full.get(did) or short_to_full.get(stripped)
                        if resolved:
                            dropped.add(resolved)
                parsed_ok = True
                break
        except Exception as e:
            LOG(f"  dedup parse fail (attempt {attempt}): {e}")
    if not parsed_ok:
        LOG(f"  dedup 모든 재시도 실패 — 전체 유지(포기 없음)")

    kept = [c for c in candidates if c["id"] not in dropped]
    LOG(f"  → kept {len(kept)}, dropped {len(dropped)}")
    return kept, sorted(dropped)


CROSS_DEDUP_PROMPT = """기존 활성 기사와 새 후보 기사를 비교해 **new 중 기존과 정확히 같은 사실·이벤트**만 drop.

## drop 해야 함 (엄격)
- 같은 회사·제품·버전·날짜의 같은 뉴스
- 본질적으로 같은 내용 (표현만 다름)

## drop 금지 (keep)
- 다른 루머·예측 (예: 기존 'Opus 4.7 출시'에 대해 새 'Opus 4.7 벤치마크')
- 다른 측면·새 디테일
- 같은 주제지만 다른 이벤트

**제목/본문 재작성 금지** — 원본 id만 drop 결정.

## 기존 활성 기사 (E prefix)
{existing}

## 새 후보 (N prefix)
{new}

## 출력 (JSON만)
{{"drop_new": ["N-id", ...]}}

확신 있는 중복만. 없으면 {{"drop_new": []}}"""

def _cross_existing_dedup_batch(new_candidates, existing_articles, sched, batch_label):
    def fmt_article(prefix, idx, a):
        body = (a.get("body") or "").replace("\n", " ")[:220]
        headline = str(a.get("headline") or "")[:180]
        return f"{prefix}{idx} | {headline} | {body}"
    ex_lines = [fmt_article("E", i+1, a) for i, a in enumerate(existing_articles)]
    nw_lines = [fmt_article("N", i+1, a) for i, a in enumerate(new_candidates)]
    prompt = CROSS_DEDUP_PROMPT.format(existing="\n".join(ex_lines), new="\n".join(nw_lines))
    LOG(
        f"cross_existing_dedup {batch_label}: {len(existing_articles)} existing "
        f"vs {len(new_candidates)} new, prompt {len(prompt):,} chars"
    )

    # 재시도: 파싱 실패면 최대 3회. 여전히 실패면 new 전부 유지(포기 없음)
    drop_new = set()
    parsed_ok = False
    for attempt in range(1, 4):
        try:
            raw = call_gemma(prompt, sched, max_tok=4096, temp=0.2, json_mode=True)
        except Exception as e:
            LOG(f"  cross dedup API fail (attempt {attempt}): {e}; new 전부 유지")
            break
        s = raw.strip()
        s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s, flags=re.M).strip()
        start = s.find('{'); end = s.rfind('}')
        try:
            if start != -1 and end > start:
                obj = json.loads(s[start:end+1])
                for did in obj.get("drop_new", []) or []:
                    did = str(did).strip().strip('"\'').upper()
                    m = re.match(r'N0*(\d+)$', did)
                    if m:
                        idx = int(m.group(1)) - 1
                        if 0 <= idx < len(new_candidates):
                            drop_new.add(new_candidates[idx]["id"])
                parsed_ok = True
                break
        except Exception as e:
            LOG(f"  cross dedup parse fail (attempt {attempt}): {e}")
    if not parsed_ok:
        LOG(f"  cross dedup 모든 재시도 실패 — new 전부 유지(포기 없음)")

    kept = [c for c in new_candidates if c["id"] not in drop_new]
    LOG(f"  → kept {len(kept)} new (dropped {len(drop_new)} in {batch_label})")
    return kept, sorted(drop_new)


def cross_existing_dedup(
    new_candidates,
    existing_articles,
    sched,
    batch_size: int = CROSS_DEDUP_EXISTING_BATCH_SIZE,
):
    """new 중 기존과 '같은 내용'인 것만 drop. 기존 기사는 작은 묶음으로 비교한다."""
    if not new_candidates or not existing_articles:
        return list(new_candidates), []
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    batches = [
        existing_articles[i:i + batch_size]
        for i in range(0, len(existing_articles), batch_size)
    ]
    LOG(
        f"cross_existing_dedup: {len(existing_articles)} existing vs {len(new_candidates)} new "
        f"in {len(batches)} batches"
    )
    remaining = list(new_candidates)
    dropped = []
    for idx, batch in enumerate(batches, 1):
        if not remaining:
            break
        remaining, batch_dropped = _cross_existing_dedup_batch(
            remaining,
            batch,
            sched,
            f"[{idx}/{len(batches)}]",
        )
        dropped.extend(batch_dropped)
    LOG(f"cross_existing_dedup total: kept {len(remaining)}, dropped {len(dropped)}")
    return remaining, sorted(set(dropped))


# ── Merge loop (consolidation + coverage-patch) ─────────────────

MERGE_ROUND1_PROMPT = """당신은 AI 뉴스 편집장입니다. 아래 candidate 기사들을 검토해 '퍼블릭에 나갈 최종 기사 목록'을 작성하세요.

## 지시
- 중복·유사 내용은 병합: 하나의 최종 기사로 합치고 merged_from에 사용한 candidate id들 모두 기록
- 모순되는 내용은 더 최신·구체적인 쪽으로 정리
- **출시 상태 표현은 보수적으로**: candidate들이 '출시됨/출시 임박/루머/추측'이 엇갈리면 더 약한 표현 사용.
  '공식 출시' 또는 '출시됐다'는 **다수의 구체적 근거**(공식 발표, 모델 카드, 가격 명시, 출시일 명시, 여러 출처 일관된 증언)가 있을 때만.
  애매하면 '출시 임박', '출시 정황 포착', '내부 테스트 중', '루머'로 표현. 본문에서도 "~로 알려졌다", "~라는 소식이 전해졌다" 같은 전언 형태 사용.
- 명백히 가치 없거나 사실성 의심되는 건 discard 배열에 id만
- 각 최종 기사는 400~700자 본문 (한국어). 제목 간결·구체.
- candidate 원문에 없는 사실 지어내지 말 것. 병합은 사실의 합집합.
- merged_from에는 candidate id를 문자열로 (예: "98", "114"). 숫자 앞 0 붙이지 말 것.

## 입력 candidate 기사 (id | 제목 | 본문)
{candidates}

## 출력 (JSON만, 다른 텍스트 금지)
{{"final": [{{"headline": "...", "body": "...", "merged_from": ["id",...]}}], "discard": ["id",...]}}
"""

MERGE_PATCH_PROMPT = """이전 round에서 이미 작성된 최종 기사들이 있습니다. 아래 '언급 안 된' candidate 기사들 중 최종 퍼블릭에 추가할 가치가 있는 것만 가려 새 기사로 쓰세요.

## 이미 확정된 최종 기사 (참고용, 수정 금지)
{existing_finals}

## 언급 안 된 candidate 기사들
{unreferenced}

## 지시
- 이미 확정된 기사와 실질적 중복이면 추가 금지 → discard에 id만
- 완전 새 소식·독립 가치 있는 것만 새 final 기사로 (400~700자)
- 새 기사에는 사용한 candidate id들을 merged_from에 기록
- 추가할 게 없으면 final=[] 로 반환
- merged_from은 문자열 id 배열. 숫자 앞 0 금지.
- **출시 상태 표현은 보수적으로**: 근거 약하면 '출시 임박/루머/추측' 사용. '공식 출시'는 다수의 구체적 근거가 있을 때만.

## 출력 (JSON만)
{{"final": [{{"headline": "...", "body": "...", "merged_from": ["id",...]}}], "discard": ["id",...]}}
"""

def _merge_cand_block(arts):
    lines = []
    for a in arts:
        s = a["id"].split("-")[-1]
        body = (a["body"] or "").replace('\n', ' ')
        lines.append(f"{s} | {a['headline']} | {body}")
    return "\n".join(lines)

def _merge_finals_block(finals):
    lines = []
    for i, f in enumerate(finals, 1):
        lines.append(f"[F{i}] {f['headline']}\n    {f['body'][:300]}")
    return "\n".join(lines) or "(없음)"

def _merge_tolerant_extract(text):
    """수동 JSON 추출. 긴 body의 일부 이스케이프 오류에 관대."""
    finals = []
    i = 0
    while i < len(text):
        m = re.search(r'"headline"\s*:\s*"', text[i:])
        if not m: break
        headline_pos = i + m.start()
        obj_start = text.rfind('{', 0, headline_pos)
        if obj_start == -1:
            i = headline_pos + 1; continue
        depth = 0; in_str = False; esc = False; end_pos = -1
        for j in range(obj_start, len(text)):
            ch = text[j]
            if esc: esc = False; continue
            if ch == '\\' and in_str: esc = True; continue
            if ch == '"': in_str = not in_str; continue
            if in_str: continue
            if ch == '{': depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0: end_pos = j; break
        if end_pos == -1:
            i = headline_pos + 1; continue
        chunk = text[obj_start:end_pos+1]
        hl_m = re.search(r'"headline"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
        bd_m = re.search(r'"body"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk, re.S)
        mf_m = re.search(r'"merged_from"\s*:\s*\[([^\]]*)\]', chunk, re.S)
        if hl_m and bd_m:
            try:
                hl = json.loads('"' + hl_m.group(1) + '"')
                bd = json.loads('"' + bd_m.group(1) + '"')
            except Exception:
                hl = hl_m.group(1); bd = bd_m.group(1)
            mf = []
            if mf_m:
                mf = [x.strip().strip('"\'') for x in mf_m.group(1).split(',') if x.strip()]
            finals.append({"headline": hl, "body": bd, "merged_from": mf})
        i = end_pos + 1
    discards = []
    dm = re.search(r'"discard"\s*:\s*\[([^\]]*)\]', text, re.S)
    if dm:
        discards = [x.strip().strip('"\'') for x in dm.group(1).split(',') if x.strip()]
    return {"final": finals, "discard": discards}

def _normalize_merge_ids(id_list, short_to_full):
    """관대한 id 매핑: 0-padding, 공백 허용, 이미 full id면 그대로."""
    lookup = {}
    for k, v in short_to_full.items():
        lookup[k] = v
        lookup[k.lstrip('0') or '0'] = v
    out = []
    seen = set()
    for x in id_list or []:
        x = str(x).strip().strip('"\'')
        if not x: continue
        stripped = x.lstrip('0') or '0'
        v = lookup.get(x) or lookup.get(stripped)
        if v and v not in seen: out.append(v); seen.add(v)
        elif x in short_to_full.values() and x not in seen: out.append(x); seen.add(x)
    return out

def _run_merge_round(candidates, existing_finals, sched, round_num, short_to_full):
    if round_num == 1:
        prompt = MERGE_ROUND1_PROMPT.format(candidates=_merge_cand_block(candidates))
    else:
        prompt = MERGE_PATCH_PROMPT.format(
            existing_finals=_merge_finals_block(existing_finals),
            unreferenced=_merge_cand_block(candidates),
        )
    LOG(f"  merge R{round_num}: {len(candidates)} cands, {len(existing_finals)} exist-finals, prompt={len(prompt):,} chars")
    raw = call_gemma(prompt, sched, max_tok=32768, temp=0.3, json_mode=True)
    s = raw.strip()
    s = re.sub(r'^```(?:json)?\s*|\s*```$', '', s, flags=re.M).strip()
    start = s.find('{'); end = s.rfind('}')
    obj = None
    if start != -1 and end > start:
        try:
            obj = json.loads(s[start:end+1])
        except Exception as e:
            LOG(f"    strict parse fail ({e}); trying tolerant")
    if obj is None:
        obj = _merge_tolerant_extract(raw)
    finals = obj.get("final", []) or []
    discards = obj.get("discard", []) or []
    for f in finals:
        f["merged_from"] = _normalize_merge_ids(f.get("merged_from", []), short_to_full)
    discards = _normalize_merge_ids(discards, short_to_full)
    LOG(f"    → {len(finals)} finals, {len(discards)} discards")
    return {"final": finals, "discard": discards}

def merge_candidates(candidates, sched, rounds=MERGE_ROUNDS, preserve_ids=None):
    """Merge+coverage-patch 루프. candidates → final article objects.

    preserve_ids: 이 id들이 merged_from에 나오면 id/created_at/placement/placed_at 유지 (쌓기)."""
    if not candidates:
        return []
    short_to_full = {c["id"].split("-")[-1]: c["id"] for c in candidates}
    by_id = {c["id"]: c for c in candidates}
    preserve = set(preserve_ids or [])
    now = datetime.now(KST)
    LOG(f"merge_candidates: {len(candidates)} inputs ({len(preserve)} preserve), up to {rounds} rounds")

    finals_raw = []
    discards = set()
    mentioned = set()

    r = _run_merge_round(candidates, [], sched, 1, short_to_full)
    finals_raw.extend(r["final"])
    discards |= set(r["discard"])
    for f in r["final"]:
        mentioned |= set(f["merged_from"])

    for rn in range(2, rounds + 1):
        unref = [c for c in candidates if c["id"] not in mentioned and c["id"] not in discards]
        if not unref:
            LOG(f"  all candidates covered by round {rn-1}")
            break
        r = _run_merge_round(unref, finals_raw, sched, rn, short_to_full)
        finals_raw.extend(r["final"])
        discards |= set(r["discard"])
        for f in r["final"]:
            mentioned |= set(f["merged_from"])

    # Convert to article dicts. 기존 id가 merged_from에 있으면 id/created_at/placement/placed_at 유지.
    result = []
    used_existing_ids = set()
    for i, f in enumerate(finals_raw):
        mf = f.get("merged_from", [])
        # 기존 id 중 merged_from에 포함된 것 (아직 다른 final에 쓰이지 않은 것만)
        existing_refs = [x for x in mf if x in preserve and x not in used_existing_ids]
        if existing_refs:
            # 가장 오래된(=원조) 기존 id를 승계
            anchor = min(existing_refs, key=lambda eid: by_id[eid].get("created_at", now.isoformat()))
            used_existing_ids.add(anchor)
            src = by_id[anchor]
            result.append({
                "id": anchor,
                "headline": f["headline"],
                "body": f["body"],
                "created_at": src.get("created_at", now.isoformat()),
                "placement": src.get("placement"),  # 유지 (classify에서 재검토)
                "placed_at": src.get("placed_at", now.isoformat()),
                "merged_from": mf,
            })
        else:
            # 신규 기사
            src_dates = [by_id[mid].get("created_at") for mid in mf if mid in by_id and by_id[mid].get("created_at")]
            created = max([d for d in src_dates if d]) if src_dates else now.isoformat()
            result.append({
                "id": f"art-{now.strftime('%Y%m%d%H%M')}-m{i+1:02d}",
                "headline": f["headline"],
                "body": f["body"],
                "created_at": created,
                "placement": None,
                "placed_at": now.isoformat(),
                "merged_from": mf,
            })
    still_unref = [c["id"] for c in candidates if c["id"] not in mentioned and c["id"] not in discards]
    LOG(f"  final: {len(result)} articles, {len(discards)} discards, {len(still_unref)} default-dropped "
        f"(preserved {len(used_existing_ids)} existing ids)")
    return result


def _scan_chunk_once(chunk: str, titles: list[str], sched, label: str) -> tuple[list, bool]:
    arts = []
    # 재시도: 파싱 결과 0개인데 raw에 'articles' 언급 있으면 garbled 가능성 → 재시도
    for attempt in range(1, 6):
        try:
            raw = call_gemma(
                prompt_scan_chunk(chunk, titles),
                sched,
                temp=0.3,
                json_mode=True,
                max_attempts=SCAN_GEMMA_MAX_ATTEMPTS,
            )
        except Exception as e:
            LOG(f"  {label} scan API fail (attempt {attempt}): {e}")
            return [], False
        arts = parse_chunk_articles(raw)
        if arts:
            return arts, True
        # 빈 응답이지만 legitimately empty인지 check
        if re.search(r'"articles"\s*:\s*\[\s*\]', raw):
            LOG(f"  {label} legitimately empty (attempt {attempt})")
            return [], True
        LOG(f"  {label} attempt {attempt} parse fail, retry")
    return [], False


def _scan_chunk_with_splitting(chunk: str, titles: list[str], sched, label: str, min_chars: int) -> list:
    LOG(f"{label} scan chunk ({len(chunk):,} chars)")
    arts, ok = _scan_chunk_once(chunk, titles, sched, label)
    if ok:
        LOG(f"  {label} → {len(arts)} articles")
        return arts
    if len(chunk) <= min_chars:
        LOG(f"  {label} failed at <= {min_chars:,} chars — dropping this chunk fragment")
        return []
    left, right = split_chunk_for_retry(chunk)
    if not left or not right:
        LOG(f"  {label} split produced empty fragment — dropping this chunk fragment")
        return []
    LOG(f"  {label} failed — splitting into {len(left):,} + {len(right):,} chars")
    return (
        _scan_chunk_with_splitting(left, titles, sched, f"{label}.1", min_chars)
        + _scan_chunk_with_splitting(right, titles, sched, f"{label}.2", min_chars)
    )


def scan_chunks_for_articles(chunks, titles, now, sched, min_chars: int = MIN_SCAN_SPLIT_CHARS):
    new_articles = []
    for i, ch in enumerate(chunks):
        arts = _scan_chunk_with_splitting(ch, titles, sched, f"[{i+1}/{len(chunks)}]", min_chars)
        for a in arts:
            new_articles.append({
                "id": new_id(now, len(new_articles) + 1),
                "headline": a["headline"],
                "body": a["body"],
                "category": a.get("category", "rumor"),
                "trust": a.get("trust", "low"),
                "created_at": now.isoformat(),
                "placement": None,
                "placed_at": now.isoformat(),
            })
    return new_articles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="override since ISO")
    ap.add_argument("--chat-file", help="reuse existing Discord export (skip re-export)")
    ap.add_argument("--as-of", help="fixed KST run boundary; requires a prefiltered --chat-file")
    ap.add_argument("--skip-scan", action="store_true", help="load new articles from cache, skip chunk scan")
    args = ap.parse_args()
    if args.as_of and not args.chat_file:
        ap.error("--as-of requires a prefiltered --chat-file")

    keys = load_keys()
    sched = KeyScheduler(keys)
    LOG(f"loaded {len(keys)} keys")

    state = load_state()
    now = resolve_run_at(args.as_of)

    since_iso = args.since or state.get("last_run_at") or (now - timedelta(hours=72)).isoformat()
    LOG(f"since = {since_iso}")
    export_since_iso = since_iso if args.chat_file else single_export_since_for_run(since_iso, now)
    if export_since_iso != since_iso:
        LOG(f"single export widened for model focus: {since_iso} → {export_since_iso}")

    if args.skip_scan:
        cache_path = ROOT / "data" / "new_articles_cache.json"
        new_articles = json.loads(cache_path.read_text(encoding="utf-8"))
        LOG(f"skip-scan: loaded {len(new_articles)} articles from cache")
        _classify_and_save(state, new_articles, now, sched)
        return

    if args.chat_file:
        export_path = Path(args.chat_file)
        LOG(f"[discord] reusing {export_path}")
    else:
        export_path = discord_export(export_since_iso)
    export_chat = read_chat_text(export_path).strip()
    chat = export_chat
    if not args.chat_file and export_since_iso != since_iso:
        chat = filter_chat_since(export_chat, since_iso).strip()
        LOG(f"scan chat filtered from widened export: {len(export_chat):,} → {len(chat):,} chars")
    LOG(f"chat: {len(chat):,} chars")

    if not chat:
        model_focus_source = model_focus_source_from_primary_chat(
            export_chat,
            export_since_iso,
            now,
            explicit_chat_file=bool(args.chat_file),
        )
        if model_focus_source is None:
            LOG("model focus skipped: primary export does not cover daily window")
            model_focus_article = None
        else:
            model_focus_article = generate_devmode_model_focus_article(now, sched, source_text=model_focus_source)
        if model_focus_article:
            new_articles = upsert_model_focus_article(state, [], model_focus_article)
            _classify_and_save(state, new_articles, now, sched)
            return
        LOG("empty chat — no-op except last_run_at bump")
        state["last_run_at"] = now.isoformat()
        state["generated_at"] = now.isoformat()
        state["journal"] = JOURNAL_NAME
        state["model"] = MODEL
        daily_summary = generate_daily_summary([], now, sched)
        state["daily_summary"] = daily_summary
        save_state(state)
        export_path = write_daily_new_articles_export([], now, daily_summary)
        LOG(f"daily new-article export → {export_path}")
        subprocess.run(["python3", str(ROOT / "build_gist.py")], check=False)
        publish_after_run(export_path, now)
        return

    chunks = chunk_by_messages(chat)
    LOG(f"{len(chunks)} chunks")

    existing_sorted = sorted(state["articles"], key=lambda a: a["created_at"], reverse=True)
    titles = [a["headline"] for a in existing_sorted[:TITLES_FOR_DEDUP]]

    new_articles = scan_chunks_for_articles(chunks, titles, now, sched)

    # inter-chunk dedup: first exact title, then Jaccard ≥ 0.4
    seen = set()
    deduped_exact = []
    for a in new_articles:
        k = title_norm(a["headline"])
        if k in seen: continue
        seen.add(k)
        deduped_exact.append(a)
    new_articles = dedup_articles(deduped_exact, threshold=0.4)
    LOG(f"new articles: {len(deduped_exact)} exact-dedup → {len(new_articles)} jaccard-dedup")

    if new_articles and state.get("articles"):
        before_story_guard = len(new_articles)
        new_articles, story_drops = apply_product_story_guard(new_articles, state["articles"])
        if story_drops:
            LOG(f"product story guard: dropped {len(story_drops)} stale release duplicates")
        if len(new_articles) != before_story_guard:
            LOG(f"  → {len(new_articles)} after product story guard")

    # cache raw deduped (pre-merge) for debug
    cache_path = ROOT / "data" / "new_articles_cache.json"
    cache_path.parent.mkdir(exist_ok=True)
    cache_path.write_text(json.dumps(new_articles, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG(f"cached {len(new_articles)} (pre-merge) → {cache_path}")

    # Step A: intra-batch dedup (new 끼리 중복 제거, body 재작성 X)
    if new_articles:
        new_articles, _ = dedup_cluster(new_articles, sched)
        (ROOT / "data" / "deduped_cache.json").write_text(
            json.dumps(new_articles, ensure_ascii=False, indent=2), encoding="utf-8")

    # Step B: cross-existing dedup (new vs 기존 활성 기사. 같은 내용만 drop)
    if new_articles and state.get("articles"):
        new_articles, _ = cross_existing_dedup(new_articles, state["articles"], sched)

    model_focus_source = model_focus_source_from_primary_chat(
        export_chat,
        export_since_iso,
        now,
        explicit_chat_file=bool(args.chat_file),
    )
    if model_focus_source is None:
        LOG("model focus skipped: primary export does not cover daily window")
        model_focus_article = None
    else:
        model_focus_article = generate_devmode_model_focus_article(now, sched, source_text=model_focus_source)
    new_articles = upsert_model_focus_article(state, new_articles, model_focus_article)

    # Step C: classify (기존 + new 모두 TOP/MAIN/SIDE 배치)
    _classify_and_save(state, new_articles, now, sched)


def _classify_and_save(state, new_articles, now, sched):
    # 주의: placement를 강제 None으로 리셋하지 않음 (merge가 기존 id 승계한 경우 placement 유지)
    if not new_articles:
        LOG("no new articles — keeping placements")
    else:
        recent_log = [
            e for e in state.get("decision_log", [])
            if (now - datetime.fromisoformat(e["run_at"])).total_seconds() < DECISION_LOG_HOURS * 3600
        ]
        LOG(f"classify: {len(state['articles'])} active + {len(new_articles)} new")

        placement_map_real = None
        attempt = 0
        classify_active = select_active_for_classification(state["articles"])
        if len(classify_active) != len(state["articles"]):
            LOG(
                f"classify prompt pool: {len(classify_active)} of "
                f"{len(state['articles'])} active articles"
            )
        while True:
            attempt += 1
            prompt, short2real = prompt_classify(classify_active, new_articles, recent_log)
            valid_shorts = set(short2real.keys())
            raw = ""
            try:
                raw = call_gemma(
                    prompt,
                    sched,
                    max_tok=16384,
                    temp=0.2,
                    json_mode=True,
                    max_attempts=CLASSIFY_GEMMA_MAX_ATTEMPTS,
                )
                p = parse_placement_json(raw)
                err = validate_placement(p, valid_shorts)
                if err:
                    raise ValueError(err)
                placement_map_real = {a["id"]: "side" for a in state["articles"]}
                for iid in p["top"]: placement_map_real[short2real[iid]] = "top"
                for iid in p["main"]: placement_map_real[short2real[iid]] = "main"
                for iid in p["side"]: placement_map_real[short2real[iid]] = "side"
                break
            except Exception as e:
                LOG(f"  classify fail ({attempt}): {e}; retry")
                # dump raw for post-mortem on first few failures
                if raw and attempt <= 3:
                    dbg = ROOT / "data" / f"classify_raw_{attempt}.txt"
                    dbg.write_text(raw, encoding="utf-8")
                if not raw or attempt >= 8:
                    LOG(f"  giving up classify after {attempt} attempts — fallback: all new → side")
                    placement_map_real = {a["id"]: (a.get("placement") or "side") for a in state["articles"]}
                    for a in new_articles:
                        placement_map_real[a["id"]] = "side"
                    break

        placement_map_real, all_articles = prioritize_new_articles_for_front_page(
            state["articles"] + new_articles,
            new_articles,
            placement_map_real,
        )
        for a in all_articles:
            new_p = placement_map_real.get(a["id"], "side")
            if a.get("placement") != new_p:
                if a.get("placement") is not None:
                    a["placed_at"] = now.isoformat()
                a["placement"] = new_p
        state["articles"] = all_articles

        state["decision_log"] = recent_log + [{
            "run_at": now.isoformat(),
            "new_count": len(new_articles),
            "placements": placement_map_real,
        }]
        counts = {k: sum(1 for v in placement_map_real.values() if v == k) for k in ("top","main","side")}
        LOG(f"placed: top={counts['top']} main={counts['main']} side={counts['side']}")

    state["last_run_at"] = now.isoformat()
    state["generated_at"] = now.isoformat()
    state["journal"] = JOURNAL_NAME
    state["model"] = MODEL
    daily_summary = current_model_focus_summary_for_daily(state, now, len(new_articles))
    if daily_summary is None:
        daily_summary = generate_daily_summary(new_articles, now, sched)
    else:
        LOG("daily ribbon uses the current daily-nuggets article")
    state["daily_summary"] = daily_summary
    save_state(state)
    LOG("state saved")
    export_path = write_daily_new_articles_export(new_articles, now, daily_summary)
    LOG(f"daily new-article export → {export_path}")

    r = subprocess.run(["python3", str(ROOT / "build_gist.py")], capture_output=True, text=True)
    if r.returncode == 0:
        LOG(r.stdout.strip().split("\n")[-1])
    else:
        LOG(f"gist push failed: {r.stderr[-300:]}")
    publish_after_run(export_path, now)


if __name__ == "__main__":
    main()
