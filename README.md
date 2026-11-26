# AI Office

에이전트 루프를 **운영(AI Ops)** 하고, 결과는 **오피스**에서 사람이 최종 승인하는 도구.
첫 워크로드는 자기소개서 작성↔리뷰 루프.

| 화면 | 대상 | 보여주는 것 |
|---|---|---|
| **AI Ops** `/ops` | 개발자 | 상단 고정 지표(토큰/비용/예산/라운드/점수/상태), 프롬프트·응답 타임라인, 설정 스냅샷, 점수 추이, diff |
| **AI Office** `/office` | 결정권자 | 에이전트가 일하는 모습(픽셀 오피스) + 최종 결과물·점수·종료 사유·토큰/비용 + 승인/수정 요청/반려. **AI Ops 팝업**으로 상세 확인 |

핵심 원칙
- **run 기록(SQLite `events` 테이블, 추가 전용)이 단일 원본.** Ops, Office 모두 같은 기록에서 파생됨. 설정 스냅샷, 프롬프트·응답 전문, 호출별 토큰/비용이 남아 검증·재현 가능. `UPDATE`/`DELETE`는 DB 트리거가 거부함
- **AI는 확정하지 못함.** 루프가 끝나면 `awaiting_approval`에서 멈추고 사람의 결정(승인/수정 요청/반려)만 run을 닫음
- **계측은 우회 불가.** 워크플로는 `MeteredLLM`만 받으므로 모든 LLM 호출이 자동으로 기록·과금 계산되고 예산 상한이 강제됨
- **Office는 요약만, 전문은 팝업에서.** `/api/office/*`는 결과·점수·결정·사용량 합계(토큰/비용/예산)·"누가 일하는 중"만 반환 (프롬프트·응답 전문은 없음). 작업 중에는 중간 점수/초안은 숨기고 지출만 표시. 전문·설정·비교·새 실행은 Office 상단 **AI Ops** 팝업(같은 Ops 콘솔)에서

## 실행

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Windows (macOS/Linux: .venv/bin/python)
.venv/Scripts/python -m pytest -q                            # 테스트
.venv/Scripts/python -m uvicorn aiops.api.app:app --host 127.0.0.1 --port 8765
```

- Office: http://127.0.0.1:8765/office (`/` 도 여기로 이동)
- Ops: http://127.0.0.1:8765/ops (`/ops#<run_id>` 딥링크). 실행 이력에서 run 2개를 체크하고 **비교**(지표·항목별 점수·설정 차이·점수 추이·결과 diff), **같은 설정으로 다시 실행**, **폼으로 불러오기**(입력·설정을 폼에 채워 바꿔 실행)로 재현/비교. 새 실행 폼에서 **루브릭(항목·가중치·설명)** 을 편집할 수 있음
- Ops 화면의 "새 실행"에서 실행하면 Office 가 가장 최근 작업 중인 run 을 자동으로 따라감

## 단계 승인 모드

"새 실행" 폼의 **단계 승인 모드**(또는 설정 `step_mode: true`)를 켜면 라운드(작성+리뷰+채점) 하나가 끝날 때마다 `승인 대기`로 멈춤. 사람은 **계속**(다음 라운드, 추가 피드백은 선택), **승인**(지금 초안 확정), **반려**를 고름. 계속하면 작성자는 직전 리뷰어 피드백과 사람 코멘트를 함께 받음. 기준 점수를 통과하거나 예산이 소진되면 일반 게이트(승인/수정 요청/반려)로 바뀌고, 이 모드에서는 라운드 상한이 적용되지 않음(사람이 멈출 시점을 정함). 비용이 큰 실제 LLM 루프를 라운드마다 확인하며 돌릴 때 유용함. 연속 운영과 함께 쓰면 라운드 게이트도 "승인 대기"로 계산됨.

## 연속 운영 (멈춤 없이 도는 루프)

Ops 의 **연속 운영(큐)** 카드: 작업(직무/문항/초안)을 여러 건 붙여넣어(`---` 로 구분) 큐에 쌓고 **▶ 시작**하면, 에이전트가 끝나는 대로 다음 작업을 자동 시작함. 한 run 이 끝나도 **확정은 사람만** 하므로 run 은 `승인 대기`에 쌓이고, 사람은 Office 에서 나중에 비동기로 결정함(승인 대기 때문에 에이전트가 멈추지 않음). LLM·루브릭·예산·통과 점수는 "새 실행" 폼의 설정이 모든 작업에 적용됨.

안전장치(멈춤 없이 돌기 때문에 필수):

| 장치 | 동작 |
|---|---|
| 이번 운영 예산 | 시작할 때마다 새 예산. 이번 운영에서 시작한 run 들이 다 쓰면 자동 정지 (새 작업 시작 시점에 확인, 진행 중 run 은 자체 run 예산 안에서 끝남) |
| 승인 대기 상한 | 사람의 결정을 기다리는 run 이 N건이면 새 작업 시작 보류 → 사람이 결정하면 저절로 재개 |
| 연속 실패 | run 이 연속 3회 실패하면 자동 정지 (게이트웨이 장애 등으로 큐를 태우지 않도록) |
| 재시작 | 서버를 재시작하면 항상 **정지** 상태로 시작 (큐는 보존). 도중에 죽어 "실행 중"으로 남은 run 은 실패로 정리 |
| 사전 검증 | 큐에 추가할 때 전체 작업의 설정(루브릭, 모델, 환경변수)을 먼저 검증. 하나라도 잘못이면 전부 거부 |
| 한도 우회 방지 | Office 의 ▶ 재개는 **수동 일시정지 후 같은 세션을 이어서** 할 때만 가능. 예산 소진/연속 실패로 멈춘 뒤에는 Ops 에서 새 예산을 입력해야 함 |

Office 헤더에 연속 운영 상태 칩(`▶ 결정 필요! · 대기 2`)과 ⏸ 일시정지/▶ 재개 버튼이 있음.

## 실제 LLM (H Chat) 연결

키와 주소는 **환경변수로만** 받음 (레포·run 기록에 저장되지 않음). `backend/.env`(git 제외)에 적어도 됨 → `backend/.env.example` 참고.

| 변수 | 필수 | 설명 |
|---|---|---|
| `H_CHAT_API_KEY` | ✔ | API 키 |
| `H_CHAT_BASE_URL` | ✔ | 게이트웨이 기본 주소(`.../api/v3`) 또는 전체 엔드포인트(`.../claude/messages`) 둘 다 가능 |
| `H_CHAT_PROJECT_ID` | | 있으면 `X-Project-Id` 헤더로 전송 |
| `H_CHAT_AUTH_PREFIX` | | 기본 `Bearer `. 401 이 나면 빈 값으로 |

Ops 화면 "새 실행"에서 LLM 을 `H Chat` 으로 선택 → **연결 테스트**(약 20 토큰 1회 호출) → 실행.
작성자/리뷰어 모델을 따로 지정할 수 있음(심사 모델을 다르게 두는 것을 권장). 모델별 단가는 `backend/pricing.json`(USD/100만 토큰)에 확인된 값만 두고 run 설정 스냅샷에 포함됨. 화면의 "단가" 입력으로 run 별 덮어쓰기 가능. 목록에 없는 모델은 기본 단가로 추정하며 설정 카드에 경고 표시. 응답에 비용 필드는 없으므로 **토큰 × 단가**로 계산하고, 게이트웨이 사용량 화면과의 대조는 Ops 의 "모델별" 표(호출 시각은 타임라인)로 함. `claude-sonnet-5`(1272 in / 550 out = $0.008044), `claude-haiku-4-5`(14 / 4 = $0.000034) 는 대시보드 값과 정확히 일치함을 테스트로 고정.

방어 로직: 호출 전 최악 비용(입력 추정 + `max_tokens`)이 예산을 넘으면 **호출하지 않고** 멈춤 · 429/5xx/네트워크 오류는 재시도(횟수는 `attempts` 로 기록) ·
실패한 호출도 `call_failed` 로 프롬프트와 함께 기록 · 응답에 usage 가 없으면 조용히 0 으로 처리하지 않고 실패 · 리뷰어 응답에 설명문이 붙어도 JSON 추출.

### 키 없이 실제 호출 경로 시험하기

```bash
cd backend
.venv/Scripts/python tools/mock_hchat.py --port 8999 --key local-test-key --chatty       # 가짜 게이트웨이 (--fail-every 3, --latency 1 도 가능)
# 다른 터미널
set H_CHAT_API_KEY=local-test-key & set H_CHAT_BASE_URL=http://127.0.0.1:8999/claude/messages    # PowerShell: $env:H_CHAT_API_KEY=...
.venv/Scripts/python -m uvicorn aiops.api.app:app --host 127.0.0.1 --port 8765
```

## 구조

```
backend/aiops/
  core/       db+store(SQLite 단일 파일, 추가 전용 실행 기록·큐), dbio(텍스트 내보내기/가져오기), metered(LLM 계측·예산 가드), engine(수명주기+승인 게이트), summary, config, jsonx, env
  llm/        LLMClient 인터페이스, FakeLLM(결정적 점수 시나리오), HChatClient(실제 호출)
  workflows/  cover_letter (LangGraph write → review → decide 루프). 워크플로 추가 = 파일 추가
  office/     Office 전용 뷰(결과/점수/결정, 애니메이션 이벤트)
  api/        FastAPI + SSE, static/{ops,office}.html (빌드 없는 단일 HTML)
design/       목업 (Office 캔버스의 원본)
```

Office 캔버스는 `design/mockup.html`의 코드를 그대로 재사용하고, 설정/이벤트/UI 부분만 실제 run 이벤트로 교체함.
새 워크플로의 agent 이름은 `office.html`의 `ROLES`에 추가하면 이름/자리/팀이 지정되고, 없으면 "예비 자리"에 자동 배치됨.

## 상태

- 완료: 코어, 자소서 워크플로, Ops 콘솔, Office(캔버스 + 승인), 가짜 LLM, H Chat 클라이언트, 테스트 94개
- H Chat: 실제 게이트웨이로 연결 테스트와 전체 루프 1회 검증함 (`Authorization: Bearer`, 응답 `usage` 사용)
- 예정: 두 번째 워크플로, 통과 건 자동 승인(옵션, 미구현)
- 가짜 LLM의 토큰은 글자 수 기반 추정이며 비용은 설정 단가 기준 추정치

## 데이터 주의

실행 기록·큐가 들어 있는 `backend/data/aiops.db`에는 입력한 자기소개서 원문이 들어가므로 **git 에서 제외**되어 있음. 커밋하지 말 것 (이 저장소는 공개). 옮기거나 백업할 때는 `python -m aiops.dbio export <폴더>` / `import <폴더>`로 텍스트(JSONL)로 내보내고 가져옴 — 이전 파일 방식의 `runs/` 폴더도 그대로 import 됨. 초기화는 서버를 끄고 `backend/data/aiops.db*` 삭제
