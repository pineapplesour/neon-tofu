#!/usr/bin/env python3
"""네온 두부 채팅 서버.

- 하네스: `pi` (경량 코딩 에이전트) — 모델은 Gemini Flash Lite
- 도구: read / ls / grep / find 만 허용 (bash, edit, write 차단 → 명령 실행 불가)
- 스트리밍: pi `--mode json` 이벤트를 읽어 NDJSON 으로 브라우저에 흘려보낸다
- 기억: pi 세션(--session-id) + 서버측 대화 기록(JSONL)
- 인증: 비밀번호 1회 입력 → HMAC 서명 쿠키(기본 30일) 로 재입력 없음

의존성: 파이썬 표준 라이브러리만 사용.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
CHAT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = CHAT_DIR / "config.json"
SESSION_DIR = CHAT_DIR / "sessions"
TRANSCRIPT_DIR = CHAT_DIR / "transcripts"
STATIC_DIR = CHAT_DIR / "static"
CORPUS_DIR = PROJECT / "corpus"

COOKIE_NAME = "tofu_auth"
COOKIE_DAYS = 30
PI_BIN = os.environ.get("TOFU_PI_BIN", "pi")
PI_MODEL = os.environ.get("TOFU_PI_MODEL", "gemini-3.5-flash-lite")
PI_PROVIDER = os.environ.get("TOFU_PI_PROVIDER", "google")
PI_TOOLS = os.environ.get("TOFU_PI_TOOLS", "read,ls,grep,find")
PI_TIMEOUT = int(os.environ.get("TOFU_CHAT_TIMEOUT", "180"))

SYSTEM_PROMPT = """너는 '네온 두부(NEON TOFU)'의 아카이브 해설자다.

- 근거는 오직 현재 작업 디렉터리의 자료(요약 회차와 수집 원문)뿐이다.
- 먼저 파일을 읽고 확인한 뒤 답한다. 추측이면 "자료에는 없습니다"라고 말한다.
- 답변은 한국어, 간결하지만 근거가 되는 날짜·회차·파일명을 함께 밝힌다.
- 출처 표기에서 '디스코드/디코/Discord/devmode/채널' 같은 말은 쓰지 않는다. 필요하면 'AI 커뮤니티'라고 한다.
- 자료에 없는 사실을 만들어내지 않는다. 명령 실행 도구는 없다.
"""


# ── 설정/인증 ──────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt.encode("utf-8"),
                            n=2 ** 14, r=8, p=1, dklen=32).hex()
    return digest, salt


def verify_password(password: str, cfg: dict) -> bool:
    if not cfg.get("password_hash") or not cfg.get("salt"):
        return False
    digest, _ = hash_password(password, cfg["salt"])
    return hmac.compare_digest(digest, cfg["password_hash"])


def sign(value: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def make_cookie(secret: str, days: int = COOKIE_DAYS) -> str:
    exp = int(time.time()) + days * 86400
    token = f"{exp}.{sign(str(exp), secret)}"
    return (f"{COOKIE_NAME}={token}; Path=/; Max-Age={days * 86400}; HttpOnly; SameSite=Lax")


def check_cookie(cookie_header: str | None, secret: str) -> int | None:
    """유효하면 만료 epoch, 아니면 None."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name != COOKIE_NAME:
            continue
        exp_s, _, sig = value.partition(".")
        if not exp_s.isdigit():
            return None
        if not hmac.compare_digest(sig, sign(exp_s, secret)):
            return None
        exp = int(exp_s)
        return exp if exp > time.time() else None
    return None


def set_password(password: str) -> dict:
    cfg = load_config()
    digest, salt = hash_password(password)
    cfg.update({
        "password_hash": digest,
        "salt": salt,
        "secret": cfg.get("secret") or secrets.token_hex(32),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    save_config(cfg)
    return cfg


# ── pi 실행/스트리밍 ───────────────────────────────────────────

def pi_available() -> bool:
    return shutil.which(PI_BIN) is not None


def build_pi_command(message: str, session_id: str) -> list[str]:
    return [
        PI_BIN,
        "--provider", PI_PROVIDER,
        "--model", PI_MODEL,
        "--tools", PI_TOOLS,
        "--no-extensions", "--no-skills", "--no-prompt-templates",
        "--no-approve",
        "--session-dir", str(SESSION_DIR),
        "--session-id", session_id,
        "--system-prompt", SYSTEM_PROMPT,
        "--mode", "json",
        message,
    ]


def stream_pi(message: str, session_id: str, emit) -> None:
    """pi 를 실행하고 이벤트를 emit(dict) 로 흘려보낸다."""
    env = dict(os.environ)
    env.setdefault("PI_CODING_AGENT_DIR", str(CHAT_DIR / "pi-config"))
    env["PI_TELEMETRY"] = "0"
    cmd = build_pi_command(message, session_id)
    proc = subprocess.Popen(
        cmd, cwd=str(CORPUS_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    started = time.time()
    text_parts: list[str] = []
    try:
        for raw in proc.stdout:  # type: ignore[union-attr]
            if time.time() - started > PI_TIMEOUT:
                proc.kill()
                emit({"t": "error", "message": f"시간 초과({PI_TIMEOUT}s)"})
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type")
            if etype == "message_update":
                inner = ev.get("assistantMessageEvent") or {}
                if inner.get("type") == "text_delta" and inner.get("delta"):
                    text_parts.append(inner["delta"])
                    emit({"t": "delta", "text": inner["delta"]})
            elif etype == "tool_execution_start":
                emit({"t": "tool", "name": ev.get("toolName"), "state": "start"})
            elif etype == "tool_execution_end":
                emit({"t": "tool", "name": ev.get("toolName"), "state": "end",
                      "error": bool(ev.get("isError"))})
            elif etype == "agent_end":
                break
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        err = ""
        if proc.stderr is not None:
            err = proc.stderr.read() or ""
    if proc.returncode not in (0, None) and not text_parts:
        emit({"t": "error", "message": (err.strip()[-400:] or f"pi 종료코드 {proc.returncode}")})
    emit({"t": "done", "text": "".join(text_parts), "elapsed": round(time.time() - started, 1)})


def append_transcript(session_id: str, question: str, answer: str) -> None:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"{session_id}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(),
            "q": question,
            "a": answer,
        }, ensure_ascii=False) + "\n")


def read_transcript(session_id: str, limit: int = 40) -> list[dict]:
    path = TRANSCRIPT_DIR / f"{session_id}.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# ── HTTP ──────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "NeonTofuChat/1.0"

    # ---- helpers ----
    def _cfg(self) -> dict:
        return self.server.cfg  # type: ignore[attr-defined]

    def _authed(self) -> bool:
        return check_cookie(self.headers.get("Cookie"), self._cfg().get("secret", "")) is not None

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: dict, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ---- routes ----
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._json(200, {"ok": True, "pi": pi_available(), "model": PI_MODEL})
            return
        if path in ("/", "/index.html"):
            if not self._authed():
                self._serve_file(STATIC_DIR / "login.html")
            else:
                self._serve_file(STATIC_DIR / "index.html")
            return
        if path == "/logout":
            self._send(200, b'{"ok":true}', "application/json",
                       {"Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            return
        if path == "/api/history":
            if not self._authed():
                self._json(401, {"error": "auth"})
                return
            sid = self.headers.get("X-Tofu-Session") or "default"
            self._json(200, {"rows": read_transcript(safe_session_id(sid))})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/login":
            data = self._body()
            cfg = self._cfg()
            if not cfg.get("password_hash"):
                self._json(500, {"error": "서버에 비밀번호가 설정되지 않았습니다"})
                return
            if not verify_password(str(data.get("password") or ""), cfg):
                time.sleep(0.6)
                self._json(401, {"error": "비밀번호가 맞지 않습니다"})
                return
            self._json(200, {"ok": True}, {"Set-Cookie": make_cookie(cfg["secret"])})
            return
        if path == "/api/chat":
            if not self._authed():
                self._json(401, {"error": "auth"})
                return
            data = self._body()
            message = str(data.get("message") or "").strip()
            if not message:
                self._json(400, {"error": "빈 메시지"})
                return
            sid = safe_session_id(str(data.get("session") or "default"))
            if not pi_available():
                self._json(500, {"error": f"pi 실행 파일을 찾을 수 없습니다({PI_BIN})"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            lock = threading.Lock()
            captured: dict[str, str] = {}

            def emit(obj: dict) -> None:
                if obj.get("t") == "done":
                    captured["text"] = str(obj.get("text") or "")
                line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
                with lock:
                    try:
                        self.wfile.write(line)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            try:
                stream_pi(message, sid, emit)
            except Exception as e:  # pragma: no cover
                emit({"t": "error", "message": str(e)})
                emit({"t": "done", "text": ""})
            if captured.get("text"):
                try:
                    append_transcript(sid, message, captured["text"])
                except Exception as e:  # 기록 실패가 응답을 막지 않게
                    sys.stderr.write(f"[chat] transcript 실패: {e}\n")
            return
        self._json(404, {"error": "not_found"})

    def _serve_file(self, path: Path) -> None:
        if not path.exists():
            self._json(404, {"error": "not_found"})
            return
        ctype = "text/html; charset=utf-8" if path.suffix == ".html" else "text/plain; charset=utf-8"
        self._send(200, path.read_bytes(), ctype)

    def log_message(self, fmt: str, *args) -> None:  # 조용히
        sys.stderr.write("[chat] %s - %s\n" % (self.address_string(), fmt % args))


def safe_session_id(value: str) -> str:
    keep = "".join(ch for ch in value if ch.isalnum() or ch in "-_")[:40]
    return keep or "default"


def ensure_corpus() -> None:
    """요약/원문을 채팅이 읽을 수 있는 corpus/ 로 노출한다."""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    (CORPUS_DIR / "summaries").mkdir(exist_ok=True)
    (CORPUS_DIR / "raw").mkdir(exist_ok=True)
    editions = sorted((PROJECT / "data" / "editions").glob("*.json"))
    for ed in editions:
        (CORPUS_DIR / "summaries" / f"{ed.stem}.md").write_text(
            edition_to_markdown(json.loads(ed.read_text(encoding="utf-8"))), encoding="utf-8")
    raws = sorted((PROJECT / "data" / "raw").glob("*_*.txt"))[-20:]
    for raw in raws:
        target = CORPUS_DIR / "raw" / raw.name
        if not target.exists():
            target.symlink_to(raw)
    (CORPUS_DIR / "README.md").write_text(
        "# 네온 두부 아카이브\n\n"
        "- `summaries/` : 회차별 요약 (파일명 `YYYY-MM-DD-<조간|석간>.md`)\n"
        "- `raw/` : 같은 회차의 수집 원문 텍스트\n\n"
        "질문에 답할 때는 summaries 를 먼저 읽고, 필요하면 raw 에서 원문을 확인한다.\n",
        encoding="utf-8",
    )


def edition_to_markdown(ed: dict) -> str:
    return (
        f"# {ed.get('title','요약')}\n\n"
        f"- 날짜: {ed.get('date')}\n"
        f"- 회차: {ed.get('slot_label')} ({ed.get('slot')})\n"
        f"- 발행: {ed.get('published_at')}\n"
        f"- 수집 구간: {ed.get('window_start')} → {ed.get('window_end')}\n\n"
        f"{ed.get('body','')}\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--set-password", help="비밀번호를 설정하고 종료")
    ap.add_argument("--gen-password", action="store_true", help="임의 비밀번호를 생성해 설정")
    args = ap.parse_args()

    if args.gen_password:
        pw = secrets.token_urlsafe(6)[:8]
        set_password(pw)
        print(json.dumps({"ok": True, "password": pw}, ensure_ascii=False))
        return
    if args.set_password:
        set_password(args.set_password)
        print(json.dumps({"ok": True}, ensure_ascii=False))
        return

    cfg = load_config()
    if not cfg.get("password_hash"):
        print("비밀번호 미설정 — `--gen-password` 또는 `--set-password` 를 먼저 실행하세요.", file=sys.stderr)
        sys.exit(2)
    ensure_corpus()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.cfg = cfg  # type: ignore[attr-defined]
    print(f"neon-tofu chat on http://{args.host}:{args.port} (pi={pi_available()}, model={PI_MODEL})",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
