#!/usr/bin/env python3
"""네온 두부 (Neon Tofu) — 요약 전용 파이프라인.

기존 First Light AI(`run_hourly.py`)의 수집/요약 로직을 **그대로 재사용**하고 아래만 바꾼다.

  1) 수집 창 12시간
     - MODEL_FOCUS_LOOKBACK_HOURS = 12, since = max(last_run_at, now-12h)
  2) 원문 아카이브 보존 (data/raw/…) — 채팅이 근거로 읽는 자료
  3) 요약문에서 Discord(디코) 언급 제거 (tofu_scrub)
  4) 요약 전용 사이트 생성 + 새 레포(docs/)로 발행
     - 기사 스캔 / dedup_cluster / cross_existing_dedup / classify 단계는 쓰지 않는다
       (요약은 원문에서 직접 생성되므로 사이트에 기사가 필요 없다)
  5) API 예산 + 서킷브레이커 (call_gemma 교체)
     - 시도횟수 대신 벽시계 예산, 분 경계 60초 슬립 제거, 429 키는 해당 실행에서 차단

사용:
  python3 run_tofu.py --phase collect --slot morning
  python3 run_tofu.py --phase publish --slot morning [--publish]
  python3 run_tofu.py --phase all --slot morning --chat-file /tmp/export.txt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent   # 네온 두부 프로젝트 루트
ROOT = Path(__file__).resolve().parent             # pipeline/ (legacy 모듈 위치)
sys.path.insert(0, str(ROOT))

import run_hourly as L  # noqa: E402  (기존 프로덕션 로직 — 수정하지 않고 재사용)
import requests  # noqa: E402
from tofu_scrub import find_discord_mentions, scrub_discord_mentions  # noqa: E402

KST = L.KST
LOG = L.LOG

TOFU_BRAND = "네온 두부"

RAW_DIR = PROJECT / "data" / "raw"
EDITIONS_DIR = PROJECT / "data" / "editions"
SITE_OUT = PROJECT / "docs"
STATE_PATH = PROJECT / "data" / "tofu_state.json"

RECENT_HOURS = int(os.environ.get("TOFU_RECENT_HOURS", "12"))
SLOT_LABEL = {"morning": "조간", "evening": "석간"}
SLOT_HOUR = {"morning": 8, "evening": 20}

# ── LLM 예산/서킷브레이커 ──────────────────────────────────────

LLM_BUDGET_SECONDS = int(os.environ.get("TOFU_LLM_BUDGET_SECONDS", "480"))
LLM_CALL_TIMEOUT = int(os.environ.get("TOFU_LLM_TIMEOUT_SECONDS", "180"))
LLM_MAX_ATTEMPTS = int(os.environ.get("TOFU_LLM_MAX_ATTEMPTS", "6"))
BACKOFFS = (5, 10, 20, 30, 30, 30)

TOFU_INSTRUCTION = """자세히 정리좀, 찌라시 빠짐없이 요약해줘. 이것만 봐도 배부른 알짜배기만 모았습니다라고 시작해줘.

[출력 형식 — 반드시 지킬 것]
- 한 줄에 여러 항목을 붙여 쓰지 말 것. 구분선, 제목, 불릿은 각각 자기 줄에 둔다.
- 섹션 제목은 `## 1. 제목` 형식으로 그 줄에 단독으로 쓴다.
- 항목은 `- 내용` 형식으로 줄바꿈해서 쓴다. (`*` 대신 `-` 사용)
- 문단 사이는 빈 줄 하나로 구분한다.

[출처 표기 금지 — 반드시 지킬 것]
- '디스코드', '디코', 'Discord', 'devmode', 'Dev Mode', '서버', '채널', '대화 내용' 같은 말을 절대 쓰지 말 것.
- 출처를 굳이 밝혀야 하면 'AI 커뮤니티'라고만 쓸 것.
- 첫 문장도 출처 언급 없이 곧바로 내용 요약으로 시작할 것.
"""


class BudgetExceeded(RuntimeError):
    pass


_BUDGET: dict[str, Any] = {"started": None, "dead": set(), "calls": 0}


def _deadline_left() -> float | None:
    if _BUDGET["started"] is None:
        _BUDGET["started"] = time.time()
    return LLM_BUDGET_SECONDS - (time.time() - _BUDGET["started"])


LLM_MAX_OUTPUT_TOKENS = 32768  # MAX_TOKENS 재시도 상한 (thinking 토큰 포함)


def call_gemma_budgeted(prompt, sched, max_tok=8192, temp=0.5, json_mode=False,
                        max_attempts=None, model=None, thinking_level=None):
    """legacy call_gemma 대체 — 벽시계 예산 + 429 키 서킷브레이커."""
    selected_model = model or L.MODEL
    endpoint = L.ENDPOINT_TPL.format(model=selected_model)
    gen_cfg: dict[str, Any] = {"maxOutputTokens": max_tok}
    if temp is not None and selected_model not in L.SAMPLING_DEPRECATED_MODELS:
        gen_cfg["temperature"] = temp
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
    gen_cfg["thinkingConfig"] = {
        "thinkingLevel": thinking_level or L.default_thinking_level(selected_model)
    }
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen_cfg}

    attempts = min(max_attempts or LLM_MAX_ATTEMPTS, LLM_MAX_ATTEMPTS)
    for attempt in range(attempts):
        left = _deadline_left()
        if left is not None and left <= 0:
            raise BudgetExceeded(
                f"LLM 예산 {LLM_BUDGET_SECONDS}s 초과 — 남은 호출 포기({selected_model})"
            )
        key = sched.acquire()
        if key in _BUDGET["dead"]:
            time.sleep(0.2)
            continue
        _BUDGET["calls"] += 1
        try:
            r = requests.post(f"{endpoint}?key={key}", json=body, timeout=LLM_CALL_TIMEOUT)
        except Exception as e:
            if L.is_runtime_filesystem_error(e):
                raise RuntimeError(f"runtime filesystem unavailable: {e}") from e
            LOG(f"  net err: {e}")
            time.sleep(5)
            continue
        if r.status_code == 429:
            _BUDGET["dead"].add(key)
            LOG(f"  429 → 키 회로차단({len(_BUDGET['dead'])}개 차단), 즉시 다음 키로")
            if len(_BUDGET["dead"]) >= len(sched.keys):
                raise RuntimeError("모든 키가 429 — 쿼터 소진, 즉시 중단")
            continue
        if r.status_code >= 500:
            wait = BACKOFFS[min(attempt, len(BACKOFFS) - 1)]
            LOG(f"  {r.status_code} backoff {wait}s")
            time.sleep(wait)
            continue
        if not r.ok:
            LOG(f"  err {r.status_code}: {r.text[:150]}")
            lowered = r.text.lower()
            if r.status_code == 400 and (
                "input token count exceeds" in lowered or "maximum number of tokens" in lowered
            ):
                raise RuntimeError(f"input token count exceeds model limit: {r.text[:150]}")
            time.sleep(BACKOFFS[min(attempt, len(BACKOFFS) - 1)])
            continue
        try:
            data = r.json()
            cand = data["candidates"][0]
            parts = cand.get("content", {}).get("parts", [])
        except Exception:
            time.sleep(2)
            continue
        # 텍스트 part 가 여러 개로 나뉘어 올 수 있다 — 첫 part 만 쓰면 뒷부분이 잘린다
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        finish = cand.get("finishReason", "")
        usage = data.get("usageMetadata", {})
        LOG(f"  [{selected_model}] finish={finish or '?'} "
            f"thoughts={usage.get('thoughtsTokenCount', 0)} out={usage.get('candidatesTokenCount', 0)} "
            f"max={gen_cfg['maxOutputTokens']} chars={len(text)}")
        if finish and finish != "STOP":
            # 잘린 응답은 성공으로 치지 않는다 (2026-09-26 조간 '3. Google (' 절단 사고)
            if finish == "MAX_TOKENS":
                gen_cfg["maxOutputTokens"] = min(gen_cfg["maxOutputTokens"] * 2, LLM_MAX_OUTPUT_TOKENS)
                LOG(f"  MAX_TOKENS → maxOutputTokens {gen_cfg['maxOutputTokens']} 로 재시도")
            else:
                LOG(f"  비정상 종료({finish}) → 재시도")
            time.sleep(2)
            continue
        if text:
            return text
        time.sleep(2)
        continue
    raise RuntimeError(f"API failed after {attempts} attempts ({selected_model})")


def apply_overrides() -> None:
    """legacy 모듈 전역을 네온 두부 규칙으로 바꾼다(파일은 그대로 둔다)."""
    L.call_gemma = call_gemma_budgeted
    L.MODEL_FOCUS_INSTRUCTION = TOFU_INSTRUCTION
    L.MODEL_FOCUS_LOOKBACK_HOURS = RECENT_HOURS
    L.MODEL_FOCUS_RECENT_HOURS = RECENT_HOURS
    L.MODEL_FOCUS_DIRECT_MAX_CHARS = 400_000
    L.JOURNAL_NAME = TOFU_BRAND


# ── 상태/유틸 ──────────────────────────────────────────────────

def load_tofu_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last_collect_at": None, "last_publish_at": None, "editions": []}


def save_tofu_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_slot(now: datetime, forced: str | None) -> str:
    if forced in SLOT_LABEL:
        return forced
    return "morning" if now.hour < 14 else "evening"


# ── 1단계: 수집 + 원문 보존 ────────────────────────────────────

def collect(*, slot: str, now: datetime, chat_file: str | None) -> dict:
    state = load_tofu_state()
    since = now - timedelta(hours=RECENT_HOURS)
    if chat_file:
        export_path = Path(chat_file)
        LOG(f"[collect] 기존 export 재사용: {export_path}")
    else:
        export_path = Path(L.discord_export(since.isoformat()))
        LOG(f"[collect] export 완료: {export_path}")

    raw_text = L.read_chat_text(export_path).strip()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d_%H%M")
    raw_path = RAW_DIR / f"{stamp}_{slot}.txt"
    raw_path.write_text(raw_text, encoding="utf-8")
    digest = sha256_text(raw_text)
    manifest = RAW_DIR / "manifest.jsonl"
    with manifest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "file": raw_path.name,
            "slot": slot,
            "collected_at": now.isoformat(),
            "window_start": since.isoformat(),
            "chars": len(raw_text),
            "sha256": digest,
        }, ensure_ascii=False) + "\n")
    state["last_collect_at"] = now.isoformat()
    save_tofu_state(state)
    LOG(f"[collect] 원문 보존 → {raw_path} ({len(raw_text):,}자, sha256 {digest[:12]}…)")
    return {"raw_path": str(raw_path), "chars": len(raw_text), "sha256": digest}


def latest_raw_for_slot(slot: str) -> Path | None:
    if not RAW_DIR.exists():
        return None
    files = sorted(RAW_DIR.glob(f"*_{slot}.txt"))
    return files[-1] if files else None


def raw_window(raw_path: Path, fallback_end: datetime) -> tuple[datetime, datetime]:
    """보존된 원문의 실제 수집 구간을 manifest 에서 읽는다."""
    manifest = RAW_DIR / "manifest.jsonl"
    if manifest.exists():
        for line in reversed(manifest.read_text(encoding="utf-8").splitlines()):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("file") == raw_path.name:
                try:
                    start = datetime.fromisoformat(entry["window_start"])
                    end = datetime.fromisoformat(entry.get("collected_at") or entry["window_start"])
                    return start, end
                except (KeyError, ValueError):
                    break
    return fallback_end - timedelta(hours=RECENT_HOURS), fallback_end


# ── 2단계: 요약 생성 + 사이트 발행 ─────────────────────────────

def stub_summary(chat: str, now: datetime, slot: str) -> str:
    """API 없이 오케스트레이션을 검증하기 위한 오프라인 요약 스텁."""
    head = chat.strip().split("\n")[0][:80]
    return (
        "이것만 봐도 배부른 알짜배기만 모았습니다.\n\n"
        f"AI 커뮤니티에서 오간 주요 소식을 {RECENT_HOURS}시간 범위로 정리했습니다.\n\n"
        "---\n\n### 1. 검증용 스텁 요약\n"
        f"* **원문 첫 줄**: {head}\n"
        f"* **수집 구간**: {(now - timedelta(hours=RECENT_HOURS)).strftime('%m-%d %H:%M')} → {now.strftime('%m-%d %H:%M')} KST\n"
    )


GREETING_RE = re.compile(r"알짜배기만 모았습니다|배부른 알짜배기")


def heuristic_title(body: str) -> str:
    """폴백 제목: 본문에서 가장 먼저 나오는 굵은 항목 → 없으면 첫 문장."""
    m = re.search(r"\*\*\s*([^*\n]{6,70}?)\s*\*\*", body)
    if m:
        return m.group(1).strip().rstrip(":.：")[:60]
    cleaned = GREETING_RE.sub("", body).replace("#", " ")
    for chunk in re.split(r"[.\n]|(?<=다)\s", cleaned):
        chunk = chunk.strip(" *-·")
        if len(chunk) >= 8:
            return chunk[:60]
    return "오늘의 AI 업데이트"


def derive_title(body: str, sched, stub: bool) -> tuple[str, str]:
    """회차 제목을 정한다. 반환: (제목, 방법)."""
    if stub or sched is None:
        return heuristic_title(body), "heuristic"
    prompt = (
        "다음은 AI 뉴스 요약문이다. 이 요약 전체를 대표하는 한국어 제목을 하나 만들어라.\n"
        "규칙: 18~42자, 과장 없이 핵심 축 압축, 따옴표/이모지/마침표 없이, 제목만 출력.\n"
        "출력은 JSON 객체 하나: {\"title\":\"...\"}\n\n"
        f"[요약문]\n{body[:6000]}\n\nJSON만 출력:"
    )
    try:
        raw = call_gemma_budgeted(prompt, sched, max_tok=200, temp=0.3, json_mode=True)
        s = raw.strip()
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            title = str(json.loads(s[start:end + 1]).get("title") or "").strip()
        else:
            title = s.strip().strip('"')
        title = re.sub(r"[\"'`*#]", "", title).strip().rstrip(".")
        if 8 <= len(title) <= 80:
            return title, "llm"
        LOG(f"[publish] 제목 생성 결과가 부적합({title!r}) → 폴백")
    except Exception as e:
        LOG(f"[publish] 제목 생성 실패({e}) → 폴백")
    return heuristic_title(body), "heuristic"


def make_edition(*, slot: str, now: datetime, chat: str, sched, stub: bool,
                 window: tuple[datetime, datetime] | None = None) -> dict:
    window_start, window_end = window or (now - timedelta(hours=RECENT_HOURS), now)
    if stub:
        body = stub_summary(chat, now, slot)
    else:
        body = L.summarize_model_focus_source(
            chat, sched, source_label=f"최신 {RECENT_HOURS}시간 자료"
        )
    if not body or len(body.strip()) < 40:
        raise RuntimeError("요약 생성 실패(빈 본문)")

    body = scrub_discord_mentions(body)
    leftovers = find_discord_mentions(body)
    if leftovers:
        LOG(f"[publish] 경고: 디코 토큰 잔여 {leftovers}")

    if stub:
        title, title_method = heuristic_title(body), "heuristic"
    else:
        title, title_method = derive_title(body, sched, stub)
    title = title[:60].rstrip(" .,…") or "오늘의 AI 업데이트"

    edition = {
        "date": now.date().isoformat(),
        "slot": slot,
        "slot_label": SLOT_LABEL.get(slot, slot),
        "published_at": now.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "title": title,
        "body": body,
        "chars": len(body),
        "source_chars": len(chat),
        "title_method": title_method,
        "brand": TOFU_BRAND,
    }
    EDITIONS_DIR.mkdir(parents=True, exist_ok=True)
    out = EDITIONS_DIR / f"{edition['date']}_{slot}.json"
    out.write_text(json.dumps(edition, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG(f"[publish] 회차 저장 → {out} ({edition['chars']:,}자, 제목: {title[:30]})")
    return edition


def build_site() -> dict:
    r = subprocess.run(
        [sys.executable, str(ROOT.parent / "site" / "build_site.py"),
         "--editions", str(EDITIONS_DIR), "--out", str(SITE_OUT)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"사이트 빌드 실패: {r.stderr[-500:]}")
    LOG(f"[publish] 사이트 빌드 → {SITE_OUT} {r.stdout.strip()}")
    return json.loads(r.stdout.strip() or "{}")


def git_publish(paths: list[Path], message: str) -> bool:
    rels = []
    for p in paths:
        p = Path(p)
        if p.exists():
            rels.append(str(p.relative_to(PROJECT)))
    if not rels:
        return False
    subprocess.run(["git", "add", "--", *rels], cwd=PROJECT, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--", *rels], cwd=PROJECT)
    if diff.returncode == 0:
        LOG("[publish] 변경 없음 — 커밋 생략")
        return False
    subprocess.run(
        ["git", "-c", f"user.name={L.GIT_USER_NAME}", "-c", f"user.email={L.GIT_USER_EMAIL}",
         "commit", "-m", message, "--", *rels],
        cwd=PROJECT, check=True,
    )
    push = subprocess.run(["git", "push", "origin", "HEAD"], cwd=PROJECT, capture_output=True, text=True)
    if push.returncode != 0:
        raise RuntimeError(f"git push 실패: {push.stderr.strip()[-400:]}")
    LOG("[publish] git push 완료")
    return True


def publish(*, slot: str, now: datetime, chat_file: str | None, sched,
            stub: bool, do_publish: bool, force: bool = False) -> dict:
    # 슬롯 게이트: 같은 회차(날짜+슬롯)가 이미 있으면 재발행하지 않는다
    existing = EDITIONS_DIR / f"{now.date().isoformat()}_{slot}.json"
    if existing.exists() and not force:
        LOG(f"[publish] 게이트: {existing.name} 이미 발행됨 — 건너뜀")
        return {"edition": existing.stem, "skipped": True, "reason": "already-published"}

    if chat_file:
        raw_path = Path(chat_file)
    else:
        raw_path = latest_raw_for_slot(slot)
        if raw_path is None:
            raise RuntimeError(f"[publish] {slot} 슬롯 원문이 없습니다. 먼저 --phase collect 실행 필요")
    chat = L.read_chat_text(raw_path).strip()
    LOG(f"[publish] 원문 {raw_path} ({len(chat):,}자) 사용")
    if not chat:
        raise RuntimeError("[publish] 원문이 비어 있습니다")

    edition = make_edition(
        slot=slot, now=now, chat=chat, sched=sched, stub=stub,
        window=raw_window(Path(raw_path), now) if chat_file is None else None,
    )
    build_site()

    published = False
    if do_publish:
        published = git_publish(
            [SITE_OUT / "index.html", SITE_OUT / "editions.json", SITE_OUT / "e", EDITIONS_DIR],
            f"chore: publish {TOFU_BRAND} {edition['date']} {SLOT_LABEL.get(slot, slot)}",
        )
    state = load_tofu_state()
    state["last_publish_at"] = now.isoformat()
    state.setdefault("editions", []).append(
        {k: v for k, v in edition.items() if k != "body"}
    )
    state["editions"] = state["editions"][-200:]
    save_tofu_state(state)
    return {"edition": edition["date"] + " " + slot, "published": published}


# ── CLI ────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["collect", "publish", "all"], default="all")
    ap.add_argument("--slot", choices=["morning", "evening", "auto"], default="auto")
    ap.add_argument("--as-of", help="고정 실행 시각(ISO, KST)")
    ap.add_argument("--chat-file", help="기존 export 재사용")
    ap.add_argument("--publish", action="store_true", help="git push 까지 수행")
    ap.add_argument("--stub", action="store_true", help="LLM 없이 스텁 요약으로 검증")
    ap.add_argument("--force", action="store_true", help="같은 슬롯이어도 다시 발행")
    ap.add_argument("--hours", type=int, help="수집 창(시간) — 기본 12, 테스트용으로 줄일 수 있음")
    args = ap.parse_args()

    if args.hours:
        global RECENT_HOURS
        RECENT_HOURS = args.hours
    apply_overrides()
    now = L.resolve_run_at(args.as_of)
    slot = resolve_slot(now, None if args.slot == "auto" else args.slot)
    LOG(f"[tofu] phase={args.phase} slot={slot}({SLOT_LABEL[slot]}) now={now.isoformat()}")

    sched = None
    if not args.stub:
        sched = L.KeyScheduler(L.load_keys())
        LOG(f"[tofu] keys={len(sched.keys)}  예산={LLM_BUDGET_SECONDS}s")

    t0 = time.time()
    result: dict[str, Any] = {"slot": slot}
    try:
        if args.phase in ("collect", "all"):
            result["collect"] = collect(slot=slot, now=now, chat_file=args.chat_file)
        if args.phase in ("publish", "all"):
            result["publish"] = publish(
                slot=slot, now=now, chat_file=args.chat_file, sched=sched,
                stub=args.stub, do_publish=args.publish, force=args.force,
            )
    except BudgetExceeded as e:
        LOG(f"[tofu] 예산 초과로 중단: {e}")
        result["error"] = str(e)
        print(json.dumps(result, ensure_ascii=False, default=str))
        sys.exit(3)
    except Exception as e:
        LOG(f"[tofu] 실패: {e}")
        result["error"] = str(e)
        print(json.dumps(result, ensure_ascii=False, default=str))
        sys.exit(1)

    result["elapsed_seconds"] = round(time.time() - t0, 1)
    if not args.stub:
        result["llm_calls"] = _BUDGET["calls"]
        result["keys_blocked"] = len(_BUDGET["dead"])
    print(json.dumps(result, ensure_ascii=False, default=str))
    LOG(f"[tofu] 완료 {result['elapsed_seconds']}s")


if __name__ == "__main__":
    main()
