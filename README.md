# AI Office

에이전트 루프를 **운영(AI Ops)** 하고, 결과는 **오피스**에서 사람이 최종 승인하는 도구.
첫 워크로드는 자기소개서 작성↔리뷰 루프.

| 화면 | 대상 | 보여주는 것 |
|---|---|---|
| **AI Ops** `/ops` | 개발자 | 상단 고정 지표(토큰/비용/예산/라운드/점수/상태), 프롬프트·응답 타임라인, 설정 스냅샷, 점수 추이, diff |
| **AI Office** `/office` | 결정권자 | 에이전트가 일하는 모습(픽셀 오피스) + 최종 결과물·점수·종료 사유 + 승인/수정 요청/반려 |

핵심 원칙
- **run 기록(JSONL)이 단일 원본.** Ops, Office 모두 같은 기록에서 파생됨. 설정 스냅샷, 프롬프트·응답 전문, 호출별 토큰/비용이 남아 검증·재현 가능
- **AI는 확정하지 못함.** 루프가 끝나면 `awaiting_approval`에서 멈추고 사람의 결정(승인/수정 요청/반려)만 run을 닫음
- **계측은 우회 불가.** 워크플로는 `MeteredLLM`만 받으므로 모든 LLM 호출이 자동으로 기록·과금 계산되고 예산 상한이 강제됨
- **Office는 과정을 모름.** `/api/office/*`는 결과·점수·결정·"누가 일하는 중"만 반환 (프롬프트, 토큰, 비용 없음). 작업 중에는 중간 점수/초안도 숨김

## 실행

```bash
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Windows (macOS/Linux: .venv/bin/python)
.venv/Scripts/python -m pytest -q                            # 테스트
.venv/Scripts/python -m uvicorn aiops.api.app:app --host 127.0.0.1 --port 8765
```

- Office: http://127.0.0.1:8765/office (`/` 도 여기로 이동)
- Ops: http://127.0.0.1:8765/ops (`/ops#<run_id>` 딥링크)
- Ops 화면의 "새 실행"에서 실행하면 Office 가 가장 최근 작업 중인 run 을 자동으로 따라감

## 구조

```
backend/aiops/
  core/       store(JSONL 기록), metered(LLM 계측), engine(수명주기+승인 게이트), summary, config
  llm/        LLMClient 인터페이스, FakeLLM(결정적 점수 시나리오)
  workflows/  cover_letter (LangGraph write → review → decide 루프). 워크플로 추가 = 파일 추가
  office/     Office 전용 뷰(결과/점수/결정, 애니메이션 이벤트)
  api/        FastAPI + SSE, static/{ops,office}.html (빌드 없는 단일 HTML)
design/       목업 (Office 캔버스의 원본)
```

Office 캔버스는 `design/mockup.html`의 코드를 그대로 재사용하고, 설정/이벤트/UI 부분만 실제 run 이벤트로 교체함.
새 워크플로의 agent 이름은 `office.html`의 `ROLES`에 추가하면 이름/자리/팀이 지정되고, 없으면 "예비 자리"에 자동 배치됨.

## 상태

- 완료: 코어, 자소서 워크플로(가짜 LLM), Ops 콘솔, Office(캔버스 + 승인), 테스트 25개
- 예정: 실제 LLM(Claude API, 실제 usage), 루브릭 편집 UI, run 비교, 단계 승인 모드, 두 번째 워크플로
- `llm.mode=real` 은 아직 미구현(명확한 에러). 가짜 LLM의 토큰은 글자 수 기반 추정이며 비용은 설정 단가 기준 추정치

## 데이터 주의

실행 기록(`backend/runs/`)에는 입력한 자기소개서 원문이 들어가므로 **git 에서 제외**되어 있음. 커밋하지 말 것.
