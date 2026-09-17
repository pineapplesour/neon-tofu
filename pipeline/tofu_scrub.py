#!/usr/bin/env python3
"""네온 두부 - 요약문에서 출처(Discord) 언급을 제거한다.

기존 프롬프트에 "디스코드 대화내용, devmode 용어 쓰지 말라"는 지시가 이미 있었지만
출력에 그대로 새는 사례가 확인되어(2026-09-17 프로덕션 daily_summary 본문) 후처리로 확실히 지운다.

원칙
- 문장을 지우지 않고 표현만 바꾼다(정보 손실 최소화).
- 치환 후 남은 단독 토큰은 안전망(safety net)으로 한 번 더 정리한다.
"""
from __future__ import annotations

import re

# 대체어: 사이트 독자가 이해할 수 있는 중립 표현
REPLACEMENT = "AI 커뮤니티"

# 순서 중요: 긴 구절 -> 짧은 토큰
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"디스코드\s*(?:대화|채팅)\s*(?:내용|기록|로그)?\s*(?:에서|의|를|은|는|이|가)?"), REPLACEMENT + "에서"),
    (re.compile(r"디스코드\s*(?:서버|채널|방)\s*(?:에서|의|를|은|는|이|가)?"), REPLACEMENT + "에서"),
    (re.compile(r"디스코드\s*(?:대화|채팅)(?:방|창)?"), REPLACEMENT),
    (re.compile(r"디스코드|디코|Discord|discord"), REPLACEMENT),
    (re.compile(r"\bdev\s*mode\b|devmode|데브\s*모드", re.I), "현장"),
    (re.compile(r"대화\s*(?:내용|기록)에서\s*오간"), "커뮤니티에서 오간"),
]

# 안전망: 위 규칙을 모두 통과하고도 남는 토큰
_SAFETY = re.compile(r"디스코드|디코|Discord|discord|devmode|dev\s*mode", re.I)

# 어색해지는 중복 표현 정리
_TIDY = [
    (re.compile(REPLACEMENT + r"\s*" + REPLACEMENT), REPLACEMENT),
    (re.compile(REPLACEMENT + r"에서에서"), REPLACEMENT + "에서"),
    (re.compile(r"\s{2,}"), " "),
    (re.compile(r"\s+([,.·)\]}])"), r"\1"),
]


def scrub_discord_mentions(text: str) -> str:
    """요약문에서 Discord 출처 언급을 제거/치환한 문자열을 돌려준다."""
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    out = _SAFETY.sub(REPLACEMENT, out)
    for pattern, repl in _TIDY:
        out = pattern.sub(repl, out)
    return out


def find_discord_mentions(text: str) -> list[str]:
    """남아 있는 언급 토큰을 찾는다(검증용). 비어 있으면 통과."""
    if not text:
        return []
    return sorted({m.group(0) for m in _SAFETY.finditer(text)})
