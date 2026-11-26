# 설계 초안 v2: 프로젝트·산출물 테이블 (오피스 확장 전제)

상태: **확정 (2026-11-26)** — 마이그레이션 v2, `projects`·`deliverables`·`project_resources`, 오피스 선언, 고아 점검까지 구현됨 (M2a). 화면·API는 M2b. 계획은 `plan-task-pipeline.md`, 결정 이유는 `project-notes.md`에 쌓는다.
v1(2026-11-26)은 `tasks`·`task_questions`처럼 이름과 열이 자소서에 묶여 있어서 폐기하고, 아래로 다시 설계한다.

## 왜 다시 설계했나

앞으로 **자소서(취업) 오피스** 다음에 **코드 오피스**, **기획 오피스**가 이어진다. 그러면 DB 뼈대는 도메인을 몰라야 하고,
도메인마다 다른 것(입력 항목, 산출물 종류, 단계, 가드)은 **코드에 선언**하고 DB에는 JSON으로 담는다.
이미 워크플로가 `input_fields`·`prompt_roles`를 선언하는 방식과 같은 방향이다.

| 오피스 | 프로젝트(의뢰 한 건) | 산출물 | 단계 | 가드 |
|---|---|---|---|---|
| 취업 (지금) | "A사 데이터 엔지니어 지원" | 자소서 문항 답변 N개 | 직무 분석 → 회사 조사 → 매칭 → 문항별 작성 루프 → 일관성 → 최종 | 근거 일치, 글자 수 |
| 코드 (나중) | "로그인 API 추가" | 변경 묶음(패치) N개 | 요구 분석 → 계획 → 구현 루프(작성↔리뷰+테스트) → 통합 | 테스트 통과, 린트 |
| 기획 (나중) | "신규 서비스 기획서" | 문서 섹션 N개 | 조사 → 목차 → 섹션 작성 루프 → 통합 검토 | 출처, 분량 |

**DB에 도메인 단어(회사, 문항, 글자 수)를 넣지 않는다.** 그건 오피스 선언(코드)과 `brief`·`spec` JSON 안에만 있다.

## 이름 (검토 필요)

기존 이름과 겹치지 않아야 한다. `task`는 큐의 `job`, 실행(`run`)과 헷갈려서 쓰지 않는다.

| 개념 | 제안 | 설명 |
|---|---|---|
| 도메인 한 갈래 | **office** (`career`, `code`, `planning`) | 앞으로 오피스가 여러 개. 컬럼 값으로만 존재하고 처리 방식은 코드에 선언 |
| 의뢰 한 건 | **project** (프로젝트) | 사용자가 맡기는 단위. 산출물 여러 개를 가짐 |
| 맡긴 결과물 하나 | **deliverable** (산출물) | 자소서 문항 하나 / 코드 변경 하나 / 기획 섹션 하나. 작성 루프(run) 하나에 대응 |
| 작성 방식 | **workflow** (기존) | 산출물 하나를 만드는 루프 코드 (`cover_letter` 등) |

참고: 게이트웨이도 `X-Project-Id`라는 "project" 개념을 쓰지만 별개이고 설정 이름도 `H_CHAT_PROJECT_ID`로 구분된다.
대안 이름은 `commission`(의뢰, 겹침 없음)이 있다.

## 원칙 (기존 유지)

1. 편집 가능한 **정의는 행**, 실행은 **시작 시점의 스냅샷을 담은 이벤트**.
2. 실행 기록(`events`)은 추가 전용이 단일 진실원. 상태·비용은 거기서 파생한다.
3. AI는 확정하지 않는다. 사람이 승인한 결과만 산출물의 최종본이 된다.
4. **외래키는 쓰지 않는다.** 삭제는 한 함수가 한 트랜잭션으로, 고아 행은 점검 함수와 테스트로 막는다.

## 관계

```
projects 1 ─── N deliverables         (산출물: 종류·제목·종류별 spec JSON·승인된 최종본)
projects N ─── N resources            (project_resources: 역할 evidence | brief | reference)
resources.owner_project_id            ('' = 내 라이브러리, 값이 있으면 그 프로젝트 전용: 직무 설명, 회사 조사)
projects 1 ─── N ProjectRun           (events: 시작 때마다 한 스트림)
ProjectRun 1 ─── N 산출물별 run        (run_started.project_run, deliverable_id 로 연결, 비용 합산)
```

## DDL (v2)

```sql
CREATE TABLE projects (
    id          TEXT PRIMARY KEY,
    office      TEXT NOT NULL,                       -- 'career' 지금, 'code'·'planning' 나중. 처리 방식은 코드에 선언
    title       TEXT NOT NULL,                       -- 목록 표시용 (취업: "회사 · 직무")
    brief       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(brief)),   -- 오피스별 입력 (취업: company, role, source_url)
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX projects_by_office ON projects (office, updated_at);

CREATE TABLE deliverables (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    position      INTEGER NOT NULL,                  -- 화면 순서 (UNIQUE 아님)
    kind          TEXT NOT NULL,                     -- 오피스가 선언한 종류 (취업: answer / 코드: change / 기획: section)
    title         TEXT NOT NULL,                     -- 취업: 문항 원문 / 코드: 변경 제목 / 기획: 섹션 제목
    spec          TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(spec)),    -- 종류별 명세
                                                     --   answer: {max_chars, count_mode, qtype, guidance}
    final_content TEXT,                              -- NULL = 아직 승인 전. 승인되면 채워지고 사람이 이후 고칠 수 있음
    final_meta    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(final_meta)),  -- 승인 시점에 코드가 잰 값 (answer: chars)
    final_run_id  TEXT NOT NULL DEFAULT '',          -- 승인된 작성 루프 run
    final_at      TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX deliverables_by_project ON deliverables (project_id, position);

CREATE TABLE project_resources (                      -- 프로젝트에 붙인 자료
    project_id  TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'evidence',    -- evidence(근거) | brief(직무 설명·요구사항) | reference(참고). 오피스가 선언
    added_at    TEXT NOT NULL,
    PRIMARY KEY (project_id, resource_id)
) WITHOUT ROWID;
CREATE INDEX project_resources_by_resource ON project_resources (resource_id);   -- "이 리소스를 쓰는 프로젝트" (삭제 거부용)

ALTER TABLE resources ADD COLUMN meta TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta));
    -- research 리소스면 {"sources": [{"url", "title", "retrieved_at"}], "query": "…", "searched_at": "…"}
ALTER TABLE resources ADD COLUMN owner_project_id TEXT NOT NULL DEFAULT '';
    -- '' = 내 라이브러리. 값이 있으면 그 프로젝트 전용(라이브러리 목록에 안 보임, 프로젝트와 함께 삭제). "라이브러리에 저장"하면 ''로 바뀜
-- resources.source_type 에 'research' 추가 (CHECK 없음, 앱이 검증)

CREATE INDEX events_project_id  ON events (json_extract(data, '$.project_id'))  WHERE type = 'run_started';   -- ProjectRun 찾기
CREATE INDEX events_project_run ON events (json_extract(data, '$.project_run')) WHERE type = 'run_started';   -- 산출물별 run 찾기
```

### 직무 설명을 열이 아니라 리소스로 둔 이유
v1은 `jd_text`·`jd_review_status`·`jd_convert_run_id`를 `tasks`에 넣었다. 하지만 "붙여넣기/이미지 변환 + 검수 전 → 후"는 이미 리소스가 하는 일이다.
직무 설명을 `owner_project_id`가 있는 리소스(역할 `brief`)로 두면 변환·검수·화면을 그대로 재사용하고, 코드 오피스의 "요구사항 문서",
기획 오피스의 "참고 자료"도 같은 구조로 들어간다. 회사 조사 결과(`research`)도 같다: 프로젝트 전용으로 만들어 두고, 다음 지원에 쓰려면 라이브러리로 올린다.

## 오피스 선언 (코드, DB 아님)

```python
class Office(Protocol):
    key: str                      # "career"
    label: str                    # "취업 오피스"
    brief_fields: list[dict]      # 프로젝트 입력 항목 (취업: company, role, source_url)  ← input_fields와 같은 형식
    resource_roles: list[str]     # ["evidence", "brief", "reference"]
    deliverable_kinds: dict       # {"answer": {"label": "문항", "spec_fields": [...], "workflow": "cover_letter"}}
    stages: list[str]             # 파이프라인 단계 이름
    def check_project(self, project) -> None: ...   # 시작 전 검사 (예: 근거 리소스는 검수 완료만)
```
화면(프로젝트 생성·산출물 입력 폼)은 이 선언에서 만든다. 지금은 취업 오피스 하나만 구현하고, 선언 형식이 코드·기획에도 맞는지는 두 번째 오피스를 붙일 때 검증한다.

## 실행 이벤트 (새 테이블 없음)

ProjectRun = `events`의 한 스트림, workflow `<office>_pipeline` (취업: `career_pipeline`).

| 이벤트 | 내용 |
|---|---|
| `run_started` | `project_id`, 설정(예산·모델), 입력 **스냅샷**: brief, 산출물 목록, 자료 `[{id, name, role, text, sha256}]` |
| `stage_started` / `stage_finished` | `stage`, `output` |
| `gate_opened` / `gate_decision` | 오피스가 정한 확인 지점(취업: 조사 확인, 부족 정보 질문, 최종 승인). 사람이 고친 내용 포함 |
| `llm_call` 등 | 기존과 동일. 프로젝트 예산은 ProjectRun + 산출물 run 비용의 합 |

산출물별 작성은 기존 workflow run이고 `run_started`에 `project_run`, `deliverable_id`를 담는다.
**프로젝트 상태는 저장하지 않는다**: `projects` 행만 있고 ProjectRun이 없으면 `draft`, 이후는 이벤트에서 파생.

## 삭제·무결성

| 대상 | 규칙 |
|---|---|
| 라이브러리 리소스 삭제 | 어떤 프로젝트가 선택 중이면 **거부** (어느 프로젝트인지 알려 줌) |
| 프로젝트 삭제 | `projects`·`deliverables`·`project_resources`·**그 프로젝트 전용 리소스**를 한 트랜잭션으로 삭제. 실행 기록은 남음 |
| 산출물 삭제 | 그 행(최종본 포함)만 삭제 |
| 승인 | `deliverables.final_*`에 쓰고(다시 승인하면 덮어씀), 이력은 이벤트에 남음 |

**고아 행 점검** `integrity_problems(db)`: 없는 프로젝트를 가리키는 산출물·자료 선택·전용 리소스, 없는 리소스를 가리키는 자료 선택,
없는 run을 가리키는 `convert_run_id`·`final_run_id`. 테스트와 `python -m aiops.dbio check`에서 쓴다.

## 스키마 버전

`PRAGMA user_version`과 번호 붙은 마이그레이션 (각각 한 트랜잭션, 실패 시 되돌림, 같은 DB를 다시 열어도 변화 없음).
- **v1** = 지금까지의 구조. 이미 있는 DB는 v1로 간주.
- **v2** = 위 DDL.

## 결정됨 (2026-11-26, 사용자 확인)

1. 이름은 **project / deliverable**, 취업 오피스의 키는 **`career`** (이력서·면접으로 넓힐 여지).
2. 직무 설명·회사 조사는 **프로젝트 전용 리소스**(`owner_project_id`). 필요하면 `promote_resource`로 라이브러리에 올린다.
3. 산출물 최종본은 **행에 두고 사람이 이후 고칠 수 있다**(`set_final` / `edit_final` / `clear_final`, 원래 run은 `final_run_id`에 남음).
4. FK는 쓰지 않는다(`integrity_problems`와 `python -m aiops.dbio check`로 대신). 스키마 버전 관리는 도입했다.
5. `count_mode`(공백 포함/제외)는 `answer` 종류의 spec 안. 바이트 기준은 나중에.

## 아직 안 넣은 것
마감일, 지원 이력(제출 버전). 코드·기획 오피스의 선언 형식이 맞는지는 두 번째 오피스를 붙일 때 검증한다.
