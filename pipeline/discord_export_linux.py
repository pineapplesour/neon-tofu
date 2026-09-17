#!/usr/bin/env python3
"""Linux-native Discord export using a persisted browser session."""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cloakbrowser import launch


KST = timezone(timedelta(hours=9))
SEP = "=" * 62
DEFAULT_GUILD_ID = "1365049273389027388"
DEFAULT_GUILD_NAME = "Dev Mode"
CHANNEL_GUILD_IDS = {
    "1365049274068631644": DEFAULT_GUILD_ID,
    "1496892059351515258": "1492191712108613812",
}
CHANNEL_GUILD_NAMES = {
    "1365049274068631644": DEFAULT_GUILD_NAME,
    "1496892059351515258": "RAGtag",
}
DEFAULT_SCROLLS_PER_SESSION = 4
DISCORD_READY_SELECTORS = (
    '[data-list-id="guildsnav"]',
    'nav[aria-label="Servers"]',
    'nav[aria-label="서버"]',
    '[class*="guilds"]',
)
SAVED_ACCOUNT_LOGIN_SELECTORS = (
    'button:has-text("로그인")',
    'button:has-text("Log In")',
    'button:has-text("Login")',
    '[role="button"]:has-text("로그인")',
    '[role="button"]:has-text("Log In")',
    '[role="button"]:has-text("Login")',
)
LOGIN_READY_SELECTOR = ", ".join((*SAVED_ACCOUNT_LOGIN_SELECTORS, 'input[name="email"]'))

MARKERS = {
    "{Attachments}",
    "{Reactions}",
    "{Embed}",
    "{Stickers}",
    "{Forwarded Message}",
}
INLINE_URL_RE = re.compile(
    r"https://(?:"
    r"cdn\.discordapp\.com/attachments/"
    r"|images-ext-1\.discordapp\.net/external/"
    r"|cdn\.discordapp\.com/stickers/"
    r"|media\.discordapp\.net/attachments/"
    r")\S+"
)


@dataclass(frozen=True)
class BrowserSessionResult:
    oldest_time: datetime
    oldest_snowflake: int | None
    scroll_attempts: int


def parse_kst(value: str) -> datetime:
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)


def snowflake_to_datetime(snowflake: int) -> datetime:
    timestamp_ms = (snowflake >> 22) + 1420070400000
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).astimezone(KST)


def get_login_credentials(environ: dict[str, str] | None = None) -> tuple[str, str] | None:
    environ = os.environ if environ is None else environ
    email = environ.get("DISCORD_EMAIL", "").strip()
    password = environ.get("DISCORD_PASSWORD", "").strip()
    if email and password:
        return email, password
    return None


def get_storage_state_path(environ: dict[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get("DISCORD_STORAGE_STATE", "/srv/first-light/state/discord_storage_state.json"))


def wait_for_discord_ui(page, timeout: int = 10000) -> None:
    page.wait_for_selector(", ".join(DISCORD_READY_SELECTORS), timeout=timeout)


def scrub_discord_storage_state(storage_state_path: Path) -> None:
    try:
        state = json.loads(storage_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(state, dict):
        return

    changed = False
    for origin in state.get("origins", []):
        if not isinstance(origin, dict) or origin.get("origin") != "https://discord.com":
            continue
        local_storage = origin.get("localStorage")
        if not isinstance(local_storage, list):
            continue
        filtered = [
            item
            for item in local_storage
            if not (isinstance(item, dict) and item.get("name") == "token")
        ]
        if len(filtered) != len(local_storage):
            origin["localStorage"] = filtered
            changed = True

    if changed:
        tmp_path = storage_state_path.with_name(f"{storage_state_path.name}.tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(storage_state_path)


def save_storage_state(context, storage_state_path: Path) -> None:
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(storage_state_path))
    scrub_discord_storage_state(storage_state_path)
    try:
        storage_state_path.chmod(0o600)
    except OSError:
        pass


def login_with_saved_account(page) -> bool:
    for selector in SAVED_ACCOUNT_LOGIN_SELECTORS:
        try:
            button = page.locator(selector).first
            if button.count() == 0:
                continue
            print("[browser] Using saved Discord account chooser.", file=sys.stderr)
            button.click(timeout=5000)
            wait_for_discord_ui(page, timeout=45000)
            return True
        except Exception as exc:
            print(f"[browser] Saved account login attempt failed for {selector}: {exc}", file=sys.stderr)
    return False


def login_with_password(page, email: str, password: str) -> None:
    print("[browser] Logging in with operator email/password fallback.", file=sys.stderr)
    page.goto("https://discord.com/login", wait_until="domcontentloaded")
    time.sleep(random.uniform(2.0, 4.0))
    try:
        page.wait_for_selector(LOGIN_READY_SELECTOR, timeout=45000)
    except Exception as exc:
        print(f"[browser] Login options did not become visible before fallback: {exc}", file=sys.stderr)
    if login_with_saved_account(page):
        return

    page.fill('input[name="email"]', email)
    time.sleep(random.uniform(0.5, 1.5))
    page.fill('input[name="password"]', password)
    time.sleep(random.uniform(0.5, 1.5))
    page.click('button[type="submit"]')
    wait_for_discord_ui(page, timeout=45000)


def ensure_logged_in(page, context, credentials: tuple[str, str] | None, storage_state_path: Path) -> None:
    print("[browser] Checking persisted Discord session.", file=sys.stderr)
    try:
        page.goto("https://discord.com/channels/@me", wait_until="domcontentloaded")
        wait_for_discord_ui(page, timeout=8000)
        print("[browser] Persisted Discord session is valid.", file=sys.stderr)
        return
    except Exception as exc:
        print(f"[browser] Persisted Discord session check failed, trying fallback login: {exc}", file=sys.stderr)

    if credentials:
        try:
            login_with_password(page, credentials[0], credentials[1])
            save_storage_state(context, storage_state_path)
            return
        except Exception as exc:
            print(f"[browser] Password login failed: {exc}", file=sys.stderr)

    page.screenshot(path="/tmp/discord_debug.png", full_page=True)
    raise RuntimeError("Discord login failed: persisted session and password login are unavailable")


def fmt_kst(dt: datetime) -> str:
    dt = dt.astimezone(KST)
    period = "오전" if dt.hour < 12 else "오후"
    hour = dt.hour % 12 or 12
    return f"{dt.year}. {dt.month}. {dt.day}. {period} {hour}:{dt.minute:02d}"


def build_channel_url(guild_id: str, channel_id: str, anchor_snowflake: int | None = None) -> str:
    url = f"https://discord.com/channels/{guild_id}/{channel_id}"
    if anchor_snowflake is not None:
        return f"{url}/{anchor_snowflake}"
    return url


def collect_visible_messages(page, messages_data: dict[int, dict[str, object]]) -> datetime:
    oldest_time = datetime.now(KST)
    locators = page.locator('li[class*="messageListItem"]').all()
    for loc in locators:
        try:
            content_el = loc.locator('div[id^="message-content-"]').last
            if content_el.count() == 0:
                continue

            msg_id_str = content_el.get_attribute("id", timeout=1000)
            if not msg_id_str:
                continue

            snowflake = int(msg_id_str.split("-")[-1])
            msg_time = snowflake_to_datetime(snowflake)

            author_el = loc.locator('span[class*="username"]').first
            author = author_el.inner_text(timeout=1000) if author_el.count() > 0 else "Unknown"
            content = content_el.inner_text(timeout=1000)

            if content:
                messages_data[snowflake] = {
                    "time": msg_time,
                    "author": author,
                    "content": content,
                }
                if msg_time < oldest_time:
                    oldest_time = msg_time
        except Exception:
            continue
    return oldest_time


def normalize_authors(messages_data: dict[int, dict[str, object]]) -> None:
    current_author = "Unknown"
    for snowflake in sorted(messages_data.keys()):
        author = str(messages_data[snowflake]["author"])
        if author != "Unknown":
            current_author = author
        else:
            messages_data[snowflake]["author"] = current_author


def oldest_collected_message(messages_data: dict[int, dict[str, object]]) -> tuple[int | None, datetime]:
    oldest_snowflake = None
    oldest_time = datetime.now(KST)
    for snowflake, msg in messages_data.items():
        msg_time = msg.get("time")
        if not isinstance(msg_time, datetime):
            continue
        if oldest_snowflake is None or msg_time < oldest_time:
            oldest_snowflake = snowflake
            oldest_time = msg_time
    return oldest_snowflake, oldest_time


def write_export(
    guild_name: str,
    channel_id: str,
    after_kst: datetime,
    out_txt: Path,
    messages_data: dict[int, dict[str, object]],
) -> None:
    normalize_authors(messages_data)
    out_lines = [
        SEP,
        f"Guild: {guild_name}",
        f"Channel: {channel_id}",
        f"After: {fmt_kst(after_kst)}",
        f"Before: {fmt_kst(datetime.now(KST))}",
        SEP,
        "",
    ]

    written = 0
    for snowflake in sorted(messages_data.keys()):
        msg = messages_data[snowflake]
        msg_time = msg["time"]
        if not isinstance(msg_time, datetime) or msg_time < after_kst:
            continue
        out_lines.append(f"[{fmt_kst(msg_time)}] {msg['author']}")
        out_lines.append(str(msg["content"]))
        out_lines.append("")
        written += 1

    out_txt.write_text(clean_export_text("\n".join(out_lines)), encoding="utf-8")
    print(f"[browser] Wrote {written} messages to {out_txt}", file=sys.stderr)


def clean_export_text(text: str) -> str:
    cleaned = text
    for marker in MARKERS:
        cleaned = cleaned.replace(marker, "")
    return INLINE_URL_RE.sub("", cleaned)


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def scroll_message_list(page, delta_y: int) -> None:
    scrolled = page.evaluate(
        """
        (deltaY) => {
            const message = document.querySelector('li[class*="messageListItem"]');
            const scroller = message?.closest('[class*="managedReactiveScroller"]');
            if (!scroller) return false;
            scroller.scrollBy(0, deltaY);
            return true;
        }
        """,
        delta_y,
    )
    if not scrolled:
        raise RuntimeError("Discord message scroller was not found")


def run_browser_scroll_session(
    guild_id: str,
    channel_id: str,
    after_kst: datetime,
    messages_data: dict[int, dict[str, object]],
    start_message_id: int | None,
    max_scrolls: int,
) -> BrowserSessionResult:
    anchor = f" from message {start_message_id}" if start_message_id is not None else ""
    print(
        f"[browser] Launching browser for guild {guild_id} channel {channel_id}{anchor} after {after_kst}.",
        file=sys.stderr,
    )
    browser = launch(headless="DISPLAY" not in os.environ)
    storage_state_path = get_storage_state_path()
    credentials = get_login_credentials()
    context_args = {
        "viewport": {"width": random.randint(1200, 1600), "height": random.randint(800, 1000)},
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
    }
    if storage_state_path.exists():
        context_args["storage_state"] = str(storage_state_path)

    context = browser.new_context(**context_args)
    page = context.new_page()
    try:
        ensure_logged_in(page, context, credentials, storage_state_path)
        time.sleep(random.uniform(1.2, 2.8))

        url = build_channel_url(guild_id, channel_id, start_message_id)
        page.goto(url, wait_until="networkidle")
        time.sleep(random.uniform(1.5, 3.0))
        print(f"[browser] Current URL: {page.url}", file=sys.stderr)

        if "login" in page.url:
            page.screenshot(path="/tmp/discord_debug.png", full_page=True)
            raise RuntimeError("Discord returned to login page after session setup")

        page.wait_for_selector('li[class*="messageListItem"]', timeout=30000)
        print("[browser] Chat loaded. Starting human-paced scroll.", file=sys.stderr)
        time.sleep(random.uniform(1.5, 3.0))

        oldest_time = datetime.now(KST)
        scroll_attempts = 0
        major_pause_min = env_float("DISCORD_BROWSER_PAUSE_MIN", 0.25)
        major_pause_max = env_float("DISCORD_BROWSER_PAUSE_MAX", 0.9)

        while oldest_time > after_kst and scroll_attempts < max_scrolls:
            if scroll_attempts % random.randint(2, 5) == 0:
                try:
                    page.mouse.move(
                        random.randint(300, 900),
                        random.randint(200, 700),
                        steps=random.randint(8, 20),
                    )
                    time.sleep(random.uniform(0.1, 0.35))
                except Exception:
                    pass

            oldest_time = collect_visible_messages(page, messages_data)
            oldest_snowflake, collected_oldest_time = oldest_collected_message(messages_data)
            if collected_oldest_time < oldest_time:
                oldest_time = collected_oldest_time
            print(
                "[browser] "
                f"Scrolled {scroll_attempts} times. "
                f"Oldest message: {oldest_time.strftime('%Y-%m-%d %H:%M:%S')}. "
                f"Extracted: {len(messages_data)}",
                file=sys.stderr,
            )
            if oldest_time <= after_kst:
                break

            for _ in range(random.randint(8, 14)):
                scroll_message_list(page, random.randint(-1800, -600))
                time.sleep(random.uniform(0.05, 0.16))
            time.sleep(random.uniform(major_pause_min, major_pause_max))
            scroll_attempts += 1

        oldest_snowflake, oldest_time = oldest_collected_message(messages_data)
        return BrowserSessionResult(oldest_time, oldest_snowflake, scroll_attempts)
    except Exception:
        page.screenshot(path="/tmp/discord_debug.png", full_page=True)
        raise
    finally:
        browser.close()


def run_browser_export(guild_id: str, guild_name: str, channel_id: str, after_kst: datetime, out_txt: Path) -> None:
    messages_data: dict[int, dict[str, object]] = {}
    max_scrolls = env_int("DISCORD_BROWSER_MAX_SCROLLS", 900)
    scrolls_per_session = env_int("DISCORD_BROWSER_SCROLLS_PER_SESSION", DEFAULT_SCROLLS_PER_SESSION)
    if scrolls_per_session <= 0:
        scrolls_per_session = max_scrolls

    total_scrolls = 0
    start_message_id = None
    oldest_time = datetime.now(KST)

    while oldest_time > after_kst and total_scrolls < max_scrolls:
        remaining_scrolls = max_scrolls - total_scrolls
        session_scrolls = min(scrolls_per_session, remaining_scrolls)
        result = run_browser_scroll_session(
            guild_id,
            channel_id,
            after_kst,
            messages_data,
            start_message_id,
            session_scrolls,
        )
        total_scrolls += result.scroll_attempts
        oldest_time = result.oldest_time
        if oldest_time <= after_kst:
            break
        if result.oldest_snowflake is None:
            raise RuntimeError("Browser export could not find a message anchor for the next scroll session")
        if result.oldest_snowflake == start_message_id:
            raise RuntimeError(
                "Browser export made no older-message progress "
                f"from anchor={start_message_id} after {total_scrolls} scrolls"
            )
        start_message_id = result.oldest_snowflake
        print(
            "[browser] "
            f"Restarting from oldest message {start_message_id}; "
            f"oldest={oldest_time.isoformat()} total_scrolls={total_scrolls}.",
            file=sys.stderr,
        )
        time.sleep(random.uniform(1.5, 3.5))

    if oldest_time > after_kst:
        raise RuntimeError(
            "Browser export did not reach requested start time "
            f"after {total_scrolls} scrolls; oldest={oldest_time.isoformat()} target={after_kst.isoformat()}"
        )
    write_export(guild_name, channel_id, after_kst, out_txt, messages_data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--guild", default=None)
    parser.add_argument("--guild-name", default=None)
    parser.add_argument("--after-kst", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    guild_id = args.guild or CHANNEL_GUILD_IDS.get(args.channel, DEFAULT_GUILD_ID)
    guild_name = args.guild_name or CHANNEL_GUILD_NAMES.get(args.channel, DEFAULT_GUILD_NAME)
    after_kst = parse_kst(args.after_kst)
    now_kst = datetime.now(KST)
    out_path = Path(args.out) if args.out else Path(f"/tmp/signal_chat_{now_kst.strftime('%Y%m%d_%H%M%S')}.txt")

    run_browser_export(guild_id, guild_name, args.channel, after_kst, out_path)
    print(f"final_file={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
