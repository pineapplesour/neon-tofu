# 네온 두부 (Neon Tofu)

**AI 최전선 소식을 하루 두 번, 요약 한 장으로.** 조간 08:00 · 석간 20:00 (KST)

기사 목록 없이 **회차 요약과 지난 요약 목록만** 남긴 사이트입니다. 수집은 기존 First Light AI
파이프라인을 그대로 쓰고, 발행물만 요약으로 바꿨습니다.

## 구조

```
docs/                 발행되는 사이트 (GitHub Pages)
  index.html          최신 호 + 지난 요약 목록
  e/<date>-<slot>.html 회차 개별 페이지
  editions.json       목록 데이터
data/editions/        회차 요약 원본 (JSON)
data/raw/             수집 원문 (git 제외 — 개인 대화 포함)
pipeline/             실행 파이프라인
  run_tofu.py         오케스트레이터 (수집→요약→사이트→발행)
  run_hourly.py       기존 First Light AI 로직 (수정 없이 재사용)
  discord_export_linux.py  기존 수집기 (수정 없이 재사용)
  tofu_scrub.py       요약문에서 출처(Discord) 언급 제거
site/build_site.py    사이트 빌더 (마크다운 → HTML)
chat/                 아카이브에 묻는 채팅 (pi + Gemini Flash Lite)
scripts/run_tofu_slot.sh  슬롯 러너
```

## 파이프라인

```
[collect]  Discord(12시간 창) 수집 → data/raw/ 에 원문 보존
[publish]  원문 → LLM 요약(디코 언급 제거) → docs/ 생성 → git push
```

- 기존 4중 중복제거(scan / dedup_cluster / cross_existing_dedup / classify)는 **쓰지 않습니다.**
  요약은 원문에서 직접 생성되고 사이트에 기사가 필요 없기 때문입니다.
  (그 단계의 반복 호출이 API 오류를 오래 끌던 원인이라 함께 제거됩니다.)
- LLM 호출은 **벽시계 예산**(기본 480초) + **429 키 회로차단**으로 제한합니다.

```bash
./scripts/run_tofu_slot.sh collect morning     # 발행 전 미리 수집
./scripts/run_tofu_slot.sh publish morning     # 정시 발행 (--publish 로 push)
```

## 채팅

```bash
python3 chat/server.py --gen-password   # 비밀번호 생성(1회)
python3 chat/server.py --port 8790      # 기동 (127.0.0.1)
```

- 하네스: `pi` (`@earendil-works/pi-coding-agent`), 모델: `gemini-3.5-flash-lite`
- 도구: `read, ls, grep, find` 만 — **명령 실행/쓰기 불가**
- 비밀번호 1회 입력 → HMAC 서명 쿠키(30일)로 재입력 없음
- 세션별 대화 기억 (`chat/transcripts/`, `chat/sessions/` — git 제외)

## 백업

이전 First Light AI 프로덕션은 개편 전 그대로 백업해 두었습니다.
`/srv/first-light/checkpoints/20260917-neon-tofu-pre-migration/`
