# JARVIS Agent Runtime 개선 로드맵

기준일: 2026-08-27

## 목표

JARVIS의 목표는 기능이 많은 챗봇이 아니라 다음 순환을 신뢰성 있게 수행하는
개인 AI 에이전트입니다.

```text
이해 → 계획 → 권한 → 실행 → 관찰 → 검증 → 복구/재계획 → 기억 → 보고
```

모델이 자연스러운 문장을 만드는 것과 실제 작업이 완료되는 것은 별개입니다.
이 로드맵은 모델 성능보다 실행 구조, 문맥 유지, 검증 가능성, 안전성과 제품 품질을
우선합니다.

## 2026-08-27 수락 갱신

P0~P12의 파일 존재 여부가 아니라 현재 통합 실행 계약을 다시 검사했습니다. 요청 귀속은
`TurnEnvelope`, 계획은 단일 Tool 단계와 의존성 DAG, 실행은 typed 결과·execution ID·
취소·idempotency, 완료는 Evidence·Artifact 검증으로 연결됩니다. 전문가 작업공간도 같은
계약을 사용하며 실제 DOCX 생성·재열기 수락을 통과했습니다. 기본 LLM은 Ollama이고 선택형
Anthropic 경로의 미설정 키나 공급자 오류는 정상 답변으로 처리하지 않습니다.

자동 기준선은 `591 passed, 4 deselected`이며 `compileall`, `pip check`,
`git diff --check`, Qt offscreen `JarvisApp` 기동 smoke를 통과했습니다. 남은 항목은 자동
검증을 더 쌓는 것과 별개로 실제 물리 장치·OAuth 계정·외부 앱 전달·Office COM 환경에서의
제품 수락입니다.

## 현재 판정

| 영역 | 현재 수준 | 판정 |
|---|---|---|
| 대화 UI·세션·음성 | Turn 귀속·동시성 자동 수락, 물리 장치 품질은 환경별 편차 | 자동 수락·실장치 대기 |
| Plugin Registry | 동적 Tool·Intent, 7축 상태, 실행·취소·재시도 정책 | 자동 수락 |
| Intent/Slot | 선언형 계약, 후속 문맥, 인용 원문 보존, 모호성 질문 | 자동 수락·표현 회귀 지속 |
| 파일·문서 작업 | 주요 형식 생성·재열기·내용·Evidence 검증 | 로컬 수락·COM 대기 |
| 웹 조사 | 실제 방문·최신성·출처·교차검증 | 자동 수락·사이트별 E2E 지속 |
| Coding | 저장소 분석·최소 patch·lint/test·diff·rollback | 실행 폐쇄루프 수락 |
| Planner/Executor | 단일 Tool 단계·DAG·typed 오류·인스턴스 격리 | 자동 수락 |
| Verify/Recover | typed Evidence·Artifact·범위 제한 복구 | 자동 수락·외부 E2E 지속 |
| Workspace/Indexer | 복원·세션 격리·증분 인덱싱 | 자동 수락 |
| RAG/Memory | namespace 격리·증분 반영·사용 추적·통합 잠금 | 자동 수락·품질 관찰 지속 |
| Automation/Observer | 알람·스케줄·감시 기반 | 장시간 검증 필요 |
| OAuth/Cloud | DPAPI OAuth·승인 원장·Drive/OneDrive/Notion·Slack/Teams 어댑터 | 자격증명별 실환경 검증 필요 |
| 배포 | 대형 오프라인 설치본 존재 | 경량화 예정 |

## 27개 상위 개선 방향

아래 27개는 모델 성능을 제외하고 JARVIS와 Codex의 동작 차이를 줄이기 위해
합의한 제품·아키텍처 개선 방향입니다. P0~P12는 이 목록을 대체하는 것이 아니라
실제로 구현하기 위한 우선순위 묶음입니다.

| 번호 | 개선 방향 | 구현 Phase |
|---:|---|---|
| 1 | 목표·작업 상태 관리 | P1, P5 |
| 2 | 장기 대화 문맥 유지 | P1 |
| 3 | 후속 발화와 새 요청 구분 | P1, P2 |
| 4 | 의미 기반 Intent 분석 | P2 |
| 5 | Capability Router | P2, P6 |
| 6 | 자연스러운 최소 확인 질문 | P1, P2 |
| 7 | 구조화된 작업 계획 | P5 |
| 8 | 실행 결과 기반 재계획 | P5 |
| 9 | 타입 기반 Tool 실행 결과 | P0, P6 |
| 10 | Artifact와 증거 기반 검증 | P0 |
| 11 | 실패 복구 및 재시도 | P5 |
| 12 | 허위 완료 응답 차단 | P0 |
| 13 | Plugin Registry 단일화 | P6 |
| 14 | 권한·보안 정책 강화 | P6, P12 |
| 15 | Workspace 자동 인식과 복원 | P4 |
| 16 | 프로젝트 증분 인덱싱 | P4 |
| 17 | 저장소 단위 Coding Agent | P3 |
| 18 | 메모리 계층 통합 | P1, P7 |
| 19 | RAG 검색 품질과 출처 관리 | P7 |
| 20 | 실시간 웹 조사와 교차 검증 | P8 |
| 21 | Office·HWP 문서 자동화 품질 | P9 |
| 22 | Cloud·메일·캘린더 연결 | P9 |
| 23 | Windows·외부 프로그램 제어 | P10 |
| 24 | Vision·OCR·멀티모달 처리 | P10 |
| 25 | STT·호출어·음성 입력 안정화 | P10 |
| 26 | TTS·끼어들기·음성 출력 개선 | P10 |
| 27 | Scheduler·선제 행동·관측성·테스트·배포 제품화 | P11, P12 |

완료율은 이 표의 문구나 파일 존재 여부가 아니라 각 Phase의 수락 기준과 실제
종단 테스트 결과로 계산합니다.

## P0 — 진실한 실행 시스템

목표: 실제 실행 없이 완료했다고 말할 수 없게 합니다.

진행 기록:

- 2026-07-27: `ToolRunResult`, `ToolRunStatus`, `Evidence`, `Artifact` 공통 타입을
  추가하고 Executor의 Plugin Intent 경로와 자율 Tool 실행 검증 경로에 1차 연결.
  레거시 Tool 반환값은 아직 문자열이므로 순차 이전이 필요합니다.
- 2026-07-27: 전용 검증기가 없는 Tool을 자동 성공시키던 기본 분기를 제거하고
  `unverified`로 판정하도록 변경. 미검증 실행은 사용자 응답과 Task 상태에서도
  완료로 확정하지 않습니다.
- 2026-07-27: Plugin SDK와 Registry가 전환 기간 동안 `str | ToolRunResult`를
  함께 지원하도록 반환 경계를 확장. 파일시스템의 프로젝트·파일 생성과 파일
  수정 Tool은 실제 경로·크기·해시 Evidence와 Artifact를 담은 `ToolRunResult`를
  직접 반환하는 첫 Plugin으로 이전했습니다.
- 2026-07-27: Calendar와 Excel·Word·PowerPoint·PDF·HWPX 생성 Tool을 직접
  typed 결과로 이전. 저장 직후 각 라이브러리로 파일을 다시 열어 ICS 이벤트,
  XLSX 시트·셀, DOCX 문단, PPTX 슬라이드, PDF 페이지·텍스트, HWPX 텍스트
  구조를 검증합니다. Evidence가 없는 직접 성공 결과는 Executor가
  `unverified`로 강등합니다.
- 2026-07-27: Browser 검색·페이지·스크린샷과 Weather 조회를 typed 결과로 이전.
  웹 검색은 공급자·검색 시각·출처 URL, 페이지는 최종 URL·HTTP 상태·본문 해시,
  날씨는 Open-Meteo 조회 시각·관측 시각·좌표·위치 정밀도를 Evidence로 보존합니다.
  관측 시각이나 현재 기온이 없는 날씨 응답은 성공으로 보고하지 않습니다.
- 2026-07-27: Git·Windows Control·Mail·Alarm을 typed 결과로 이전. Git은
  저장소·브랜치·명령 전후 HEAD와 출력 해시, Windows는 프로세스·창·별칭 상태,
  Mail은 RFC 822 재파싱 또는 SMTP 수신자 접수, Alarm은 DB 작업 ID와 Scheduler
  메모리 등록 상태를 Evidence로 보존합니다. UAC와 바로가기 실행처럼 실제 프로세스
  시작을 확인할 수 없는 요청은 `unverified`로 유지합니다.
- 2026-07-27: System Tools의 시간·날짜·Plugin Registry·문자열 반복을 typed
  결과로 이전. `ToolExecutor`에 공통 레거시 어댑터를 추가해 모든 중앙 실행 결과가
  `ToolRunResult`로 나오도록 했습니다. 기존 Verifier가 실제 검증할 수 있는 결과만
  성공하며 나머지 레거시 문자열은 `unverified`, 권한·파라미터 오류는 typed
  failure로 변환됩니다.
- 2026-07-27: 내장 파일 읽기·디렉터리 목록·Workspace·Project Indexer·Semantic
  Memory·RAG Tool을 직접 typed 결과로 이전. 파일 해시·Workspace 파일 수·인덱스
  루트와 검색 건수·Memory 저장 후 재조회·RAG Chunk와 출처를 Evidence로 보존합니다.
  ProjectIndexer가 참조하지만 WorkspaceManager에 없던 `current_workspace` 연결
  속성도 복구했습니다.
- 2026-07-27: 내장 파일 쓰기·폴더 생성/삭제·명령 실행과 마이크/카메라 조회·캡처,
  자동화 작업/엔진 Tool을 직접 typed 결과로 이전. 저장 바이트·삭제 후 부재·프로세스
  종료 코드·장치 열거·캡처 이미지 해시·Scheduler DB 레코드·실행 스레드를 성공
  증거로 사용합니다. 존재하지 않는 자동화 작업 ID의 활성화·삭제가 성공으로
  표시되던 오류도 차단했습니다.
- 2026-07-27: 프로필·환경설정·Knowledge Graph·Multi-Agent·STT·감지 제어·
  Scheduler 호환 API와 구형 웹 검색·Vision·PDF 추출·Excel 내장 Tool을 직접
  typed 결과로 이전. DB 재조회·관계/작업 상태·전사 해시·스레드·출처 URL·입력
  이미지/PDF·통합문서 재열기를 Evidence로 사용합니다. 최종 답변 사실성을 검증할
  수 없는 Multi-Agent와 실제 스트림 개방 신호가 없는 감지 시작은 `unverified`로
  유지합니다. PDF 전체 페이지 추출이 같은 페이지를 잘못 참조하던 오류도 수정했습니다.
- 2026-07-27: GUI가 직접 사용하는 `speak_text()` 문자열 API는 유지하되 Tool
  Registry에는 `speak_text_result()` typed 경계를 연결했습니다. 모든 중앙 Tool
  실행은 권한 거부를 포함해 입력·상태·Evidence·Artifact·지연 시간을 Action
  Journal에 기록하며 비밀번호·토큰·API 키는 마스킹합니다. 일반 대화 LLM이
  실행하지 않은 외부 작업을 완료형으로 주장하면 출력 경계에서 차단합니다.
  `PresentedResponse`로 원문 기술 로그와 화면·음성 본문도 명시적으로 분리했습니다.
- 2026-07-27: Recovery가 typed 결과를 문자열 Verifier로 다시 판정하고 복구
  결과를 무조건 성공으로 승격하던 경로를 제거했습니다. 재시도도
  `ToolRunResult.succeeded`만 신뢰합니다. `partial`을 1급 상태로 추가하고 전체
  실행 종료를 `completed/partial/failed/cancelled`로 분류해 재시도 횟수와
  완료·실패 단계 수를 `ExecutionOutcome`과 사용자 응답에 전달합니다.
  전체 회귀 `175 passed, 4 deselected`로 P0 수락 기준을 완료했습니다.

- [x] 모든 중앙 Tool 결과를 `ToolRunResult` 타입으로 통일
- [x] 중앙 ToolExecutor 반환 경계의 `status`, `output`, `error`, `artifacts`, `evidence`, `timing` 통일
- [x] 문자열의 `오류:` 포함 여부에 의존하는 성공 판정 제거
- [x] 검증기가 없는 실행형 Tool은 `unverified` 처리
- [x] 일반 대화 경로의 실행 완료 주장 차단
- [x] Action Journal에 Tool 입력·결과·검증 증거·지연 시간 연결
- [x] UI 표시, TTS, 기술 로그 Presenter 분리
- [x] 취소·부분 완료·재시도 상태를 사용자에게 정확히 표시

P0 상태: **완료**. 레거시 문자열은 직접 성공 근거로 사용하지 않으며, 전용 검증기가
없는 결과는 `unverified`입니다. 이후 하위 서비스와 Plugin을 완전 typed API로
정리하는 작업은 P6의 Tool Runtime 단일화에서 계속합니다.

수락 기준:

- 파일·앱·웹·알람·문서 작업이 증거 없이 `succeeded`가 될 수 없습니다.
- 실패한 작업은 최종 응답에서도 실패 또는 부분 완료로 유지됩니다.

## P1 — 대화와 Task State 통합

목표: “그 파일”, “같은 방식”, “이어서”가 작업 상태와 연결됩니다.

진행 기록:

- 2026-07-27: 기존 `pending_requests`, `intent_states`, `recent_intents`,
  Executor Scratchpad에 흩어졌던 실행 상태를 `agent_tasks` 중심으로 연결했다.
  Task는 Workspace, Intent, Slot, 확인 질문, 대화 문맥, 실행 계획, Artifact,
  Evidence, 마지막 Tool, 재시도 횟수, 검증 상태를 영속 저장한다.
- 기존 DB를 삭제하지 않고 필요한 열을 추가하는 SQLite migration을 연결했다.
- Pending과 recent intent 조회를 세션뿐 아니라 현재 Workspace로 격리했다. 작업
  상태/제어 조회도 다른 Workspace의 Task ID를 사용할 수 없다.
- 실행 중 앱이 종료되면 Task를 `interrupted`로 바꾸고 자동 재실행하지 않는다.
  사용자가 작업 목록을 확인한 뒤 명시적으로 재개할 수 있다.
- 확인 응답을 7일 동안 받지 못한 Task는 `expired` 처리하고 pending 행을 제거한다.
- Intent 직접 일치와 후속 Slot 보완의 신뢰도를 Task에 기록한다.
- 허용 상태 전이표와 검증 API를 추가했다. 1차 구현 당시에는 기존 Executor 제어
  경로 전체의 강제 적용과 Task 관리 GUI가 후속 범위로 남아 있었다.
- 2026-08-01: Executor의 시작·완료·취소·수정·일시정지·재개 경로를
  `transition_task()`로 통일했다. 잘못된 상태의 명령은 상태를 훼손하지 않고
  거절한다. 재시작 복구는 새 Task 복제가 아니라 동일 Task를 `queued`로 되돌려
  목표·계획·증거 연결을 유지한다.
- 상단 `☷` 작업 관리 UI를 추가했다. 현재 세션·Workspace의 Task만 조회하며 상태,
  목표, 확인 질문, Intent, 마지막 Tool, 검증 상태와 Artifact/Evidence 수를 표시한다.
  중단 작업 재개, 활성 작업 취소, 종료·만료 기록 삭제를 지원한다.

- [x] Conversation, pending request, recent intent, Scratchpad 상태 통합
- [x] Task별 목표·대상·Slot·계획·Artifact·검증 결과 저장
- [x] 지시 수정, 대상 교체, 취소, 재개를 상태 전이로 처리
- [x] 새 요청과 후속 답변 구분 신뢰도 도입
- [x] 앱 재시작 후 대기 작업 복구 정책
- [x] Workspace·세션별 Task 격리
- [x] 오래된 pending task 만료와 UI 관리

수락 기준:

- 대상과 지시를 여러 발화에 나눠 말해도 다시 묻지 않습니다.
- 다른 주제로 전환하면 이전 대기 작업이 새 요청을 가로채지 않습니다.

검증 결과: 자동 회귀 테스트 `181 passed, 4 deselected`, Qt offscreen Task 관리창
스모크 테스트 통과. P1 대화와 Task State 통합은 **완료**입니다.

## P2 — 의미 기반 Capability Router

목표: 키워드가 아니라 구조화된 의도와 Capability 계약으로 Tool을 선택합니다.

진행 기록:

- 2026-08-01: `IntentSchema`와 `IntentResolution`에 표준 `domain/action/target/
  constraints/reference/confidence` 구조를 추가했다. 기존 Plugin 선언은 intent 이름과
  Slot 이름에서 하위 호환 방식으로 표준 필드를 유도할 수 있다.
- `ToolSchema`에 출력 스키마, 부작용, 검증 필수 여부를 추가하고 Registry가
  `CapabilityContract`를 단일 조회한다. Executor는 Intent Tool 실행 전에 Registry의
  필수 입력 계약을 검사한다.
- 라우터가 hint·action·pattern·의도 설명 유사도와 2순위 점수 차이를 함께 사용해
  신뢰도를 계산하고 상위 대안과 선택 근거를 반환한다. 설명 유사도만으로 새 도메인을
  만들지 않아 “파일” 같은 일반 단어의 오분류를 막는다.
- 현재는 표준 구조와 실행 경계가 마련된 1차 단계다. 모든 Plugin의 부작용·출력·검증
  계약 명시, 낮은 신뢰도 최소 질문, 시간 민감 자동 웹 라우팅, 요청 종류 분리와 실제
  발화 회귀 데이터셋은 후속 P2 작업으로 남아 있다.
- 2026-08-01: Tool 이름·권한 계약에서 `read/execute/change/external_send` 부작용을
  정규화하고 모든 Tool에 공통 typed 출력 스키마와 검증 요구사항을 채웠다. 설치된
  41개 Tool·21개 Intent의 구조와 request type/side effect 일치를 Registry가 전수
  검사하며 Executor도 실행 직전에 불일치를 차단한다.
- Intent 점수가 12% 이내로 충돌하고 정규식의 강한 근거가 없을 때 실행을 보류하고
  상위 두 작업 설명만 제시하는 한 문장 확인 질문을 반환한다. 선택 Intent, 요청 종류,
  confidence, 점수 근거와 대안은 `[Router]` 로그로 남는다.
- 2026-08-01: Intent에 `freshness(static/session/live)`와 `requires_sources` 계약을
  추가했다. 전용 실시간 Plugin이 직접 매칭되지 않은 발화만 시간성+질문성을 판정해
  live/source-required 검색 Capability로 보낸다. 따라서 현재 날짜·시간·날씨는 각각
  전용 Plugin을 유지하고 주가·공적 인물·최근 뉴스는 출처 포함 웹 검색으로 향한다.
- 실제 사용 중 발생했던 일반 대화, 시간 민감 질문, 날짜·시간·날씨, 앱 실행, 파일·
  캘린더 생성을 JSON 회귀 데이터셋으로 분리해 새 표현을 코드 분기 없이 추가할 수
  있게 했다.

- [x] `domain/action/target/constraints/reference/confidence` 표준 의도 타입
- [x] Plugin별 입력·출력·부작용·권한·검증 계약
- [x] 낮은 신뢰도에서만 최소 확인 질문
- [x] 시간 민감 질문의 웹 검색 자동 라우팅
- [x] 대화, 조회, 실행, 변경, 외부 전송을 분리
- [x] Intent 충돌 점수와 설명 가능한 라우팅 로그
- [x] 실제 사용자 발화 기반 라우팅 회귀 데이터셋

수락 기준:

- 새 표현을 추가할 때 Executor 도메인 분기를 수정하지 않습니다.
- 잘못된 도구 선택이 실행 전에 계약 검사에서 차단됩니다.

검증 결과: 전체 `198 passed, 4 deselected`. P2 의미 기반 Capability Router는
**완료**입니다.

## P3 — 범용 Coding Agent

목표: 단일 파일 재작성에서 저장소 단위 개발 에이전트로 발전합니다.

진행 기록:

- 2026-08-01: `CodingAgent`와 `CodingPlugin`을 추가했다. Workspace의 파일 구조,
  README, 의존성 선언, Git 브랜치·dirty 상태를 사전 분석하며 가상환경·빌드·IDE
  폴더를 제외한다.
- `FileEdit(old_text/new_text/expected_sha256)` 기반 최소 치환만 허용한다. 기준 문자열은
  정확히 한 번 일치해야 하며 변경 전 해시가 달라지면 사용자 변경 충돌로 중단한다.
  UTF-8/BOM/CP949 인코딩과 기존 주변 내용을 보존한다.
- 여러 파일을 임시 파일+`os.replace`로 원자 적용하고 기본 Python `py_compile` 또는
  명시된 인자 배열 검증을 실행한다. 하나라도 실패하면 모든 파일을 원본 bytes로
  롤백하며 완료 상태를 반환하지 않는다.
- unified diff, 검증 명령 결과, 변경 파일 Artifact와 diff hash Evidence를 typed
  Tool 결과로 반환한다. 아직 LLM 변경 계획·관련 심볼 검색·언어별 lint/type/test
  자동 선택과 실패 로그 기반 재수정 루프는 남아 있다.
- 2026-08-01: CodingAgent가 임시 ProjectIndexer DB로 Python AST 심볼과 파일명·본문을
  검색해 `CodingPlan(request/related_files/related_symbols/impact_scope/validation)`을
  생성한다. 사용자 저장소에는 인덱스 DB를 남기지 않는다.
- 변경 파일명과 대응하는 `test_<stem>.py`/`<stem>_test.py`를 자동 영향 범위에 넣고
  Python `py_compile` 후 관련 pytest를 실행한다. JavaScript는 Node가 있을 때
  `node --check`를 선택한다. 관련 테스트 실패 시 원자적 롤백됨을 검증했다.
- 2026-08-01: manifest와 설치된 실행기를 함께 확인해 Ruff check/format, Mypy,
  npm format/lint/typecheck/test/build, Cargo check/test, Go test를 안전한 인자 배열로
  선택한다. 설정이나 실행기가 없으면 존재하지 않는 검증을 성공으로 가장하지 않는다.
- diff가 비었거나 2,000줄 최소 patch 한도를 넘거나 병합 충돌 표식을 추가하면 저장
  전에 자체 검토에서 거절한다. 검증 실패는 전체 롤백 후 오류 로그를 repair callback에
  전달하며 최대 5회 이내의 새 최소 patch만 재적용·재검증한다.
- 2026-08-01: `coding.change_repository` Intent와 `coding_execute_request` Tool로
  자연어 요청→구조화 계획→Coding LLM JSON edits→Transaction 실행을 연결했다.
  모델은 파일을 직접 쓰거나 전체 원문을 반환할 수 없고 exact edit만 제안한다.
- 기존 파일 수정과 새 테스트 파일 생성을 하나의 원자 transaction으로 처리한다.
  `change_kind=feature`인데 테스트 edit가 없으면 저장 전에 거절한다. 검증 실패 로그는
  Coding 모델에 전달되어 제한 횟수 내 새 최소 edits를 제안하고 같은 검증을 다시 거친다.
  코드 대상+개발 동작 정규식 계약으로 “테스트 실행”이 Windows 앱 실행으로 잘못
  라우팅되는 충돌도 제거했다.

- [x] 저장소 구조·README·의존성·Git 상태 사전 분석
- [x] 관련 심볼과 파일 검색
- [x] 변경 계획과 영향 범위 작성
- [x] 전체 파일 재생성 대신 최소 patch 적용
- [x] 인코딩·줄바꿈·사용자 변경 보존
- [x] 언어별 문법·formatter·lint·type check
- [x] 관련 테스트 자동 선택·실행
- [x] 실패 로그 분석 후 수정·재검증
- [x] 최종 diff 자체 검토
- [x] 원자적 적용과 실패 롤백
- [x] 새 기능 테스트 생성
- [x] 빌드·실행 결과와 Artifact 보고

수락 기준:

- 여러 파일 변경 요청을 계획부터 테스트까지 끝냅니다.
- 테스트 실패 상태에서 “완료”라고 말하지 않습니다.

검증 결과: 자연어 새 기능 요청이 제품 코드와 테스트 파일을 함께 생성하고 관련
pytest까지 통과하는 E2E 및 전체 `212 passed, 4 deselected`. P3 범용 Coding Agent는
**완료**입니다.

## P4 — Workspace·Project Intelligence

- [x] 마지막 Workspace 자동 복원
- [x] Workspace 목록·별칭·프로젝트별 설정
- [x] 선택 직후 자동 인덱싱
- [x] `.gitignore` 준수와 대형 폴더 제외
- [x] 파일 변경 감지 기반 증분 인덱싱
- [x] 언어·프레임워크·테스트 명령 감지
- [x] 프로젝트별 Memory/RAG Namespace
- [x] 현재 Git 브랜치·dirty 상태 UI
- [x] 새 프로젝트 템플릿·가상환경·Git 초기화

완료 기준: Workspace 카탈로그는 `data/workspaces.json`에 원자적으로 저장되며 앱 singleton만
마지막 선택을 자동 복원한다. 선택·복원 직후와 5초 주기로 `.gitignore` 및 공통 대형 생성
폴더를 제외한 증분 인덱싱을 수행한다. Memory 세션과 RAG 문서는 경로 해시 namespace로
격리된다. 상단 UI는 Git 브랜치와 dirty 표시를 갱신한다. Workspace Plugin은 목록·별칭·
프로젝트 프로필과 `empty`/`python-basic`/`python-cli` 템플릿, 선택적 `.venv`·`git init`을
제공하며 실패 시 새 프로젝트 생성 전체를 롤백한다.

## P5 — Planner·Executor·Recovery 재설계

- [x] `executor.py`의 대화·계획·실행·응답 책임 분리
- [x] 실행 가능한 Plan DAG
- [x] 단계별 사전 조건·예상 Artifact·검증 방법
- [x] 독립 단계 병렬 실행
- [x] 관찰 결과 기반 재계획
- [x] 오류 서명·시도 전략·재시도 예산 저장
- [x] 동일 실패 반복 차단
- [x] 복구 후 같은 검증기 재실행
- [x] 사람 승인이 필요한 중단점 명시

완료 기준: `Executor`는 호환 facade이며 일반 대화는 `ConversationService`, 계획 생성은
`PlanningService`, DAG 실행·병렬화·복구는 `PlanCoordinator`, 종료 상태 문구는
`ResponseComposer`가 담당한다. 각 `PlanStep`은 의존성·사전조건·Tool 입력·예상 Artifact·
검증법·승인 이유·재시도 예산과 전략을 보유한다. Plan/attempt/error signature는 SQLite에
저장되며 동일 서명 2회, 단계 예산, 전체 재계획 2회 중 먼저 도달한 경계에서 중단한다.
모든 복구 시도는 최초 실행과 같은 검증 callback을 거친다. 외부 전송·삭제·push 권한 단계는
`awaiting_approval`로 영속화되고 승인된 단계만 재개할 수 있다.

## P6 — Tool Runtime과 Plugin 완전 단일화

- [x] `core/tools.py` 레거시 Tool을 Plugin으로 이전
- [x] 중복 이름·Schema 충돌을 시작 시 차단
- [x] JSON Schema 입력과 출력 모두 검증
- [x] Plugin 버전·의존성·지원 OS·취소 가능성 선언
- [x] 동기/비동기·Timeout·재시도 정책
- [x] Plugin 상태·인증·진단 UI
- [x] 연결됨/설치됨/인증됨/검증됨 상태 분리

진행 기록 (2026-08-02):

- 모든 공개 Tool 스키마 조회와 실행을 `PluginRegistry` 단일 경계로 통일했다.
  기존 내장 구현은 `legacy_runtime` Plugin이 제공하며 더 이상 Executor의 우회 분기로
  실행되지 않는다.
- Plugin/Tool/Intent 중복과 잘못된 JSON Schema는 등록 시 예외로 시작을 중단한다.
  입력과 출력은 Draft 2020-12 JSON Schema로 검증한다.
- Tool 계약에 실행 모드, timeout, 재시도 횟수, 취소 가능성을 추가했다. 동기·비동기
  반환을 같은 worker 경계에서 처리하고 timeout·취소·재시도 예산을 강제한다.
- Plugin은 버전, Python 의존성, 지원 OS, 인증 방식과 연결/인증 진단을 선언한다.
  상단 `P` UI에서 설치됨·연결됨·인증됨·검증됨을 독립적으로 확인할 수 있다.
- P6 전용 계약 테스트와 전체 회귀 `212 passed, 4 deselected`를 통과했다.

P6 상태: **완료**. 다음 단계는 P7 Memory·RAG·Knowledge 통합이다.

## P7 — Memory·RAG·Knowledge

- [x] 단기 대화/Task/프로젝트/선호/사실/사례 메모리 분리
- [x] 사실·추측·출처·기록 시각 저장
- [x] 사용자 정정과 모순 해결
- [x] 민감 정보 자동 기억 금지
- [x] 문서 유형별 구조 보존 Chunking
- [x] 삭제·수정 문서 인덱스 동기화
- [x] Metadata filter와 reranking
- [x] 답변 주장과 근거 Chunk 연결
- [x] 웹 정보의 만료·재검증 정책

진행 기록 (2026-08-02):

- 대화와 Task의 기존 전용 저장소는 유지하면서 프로젝트·선호·사실·사례를 typed
  `KnowledgeRecord`로 분리했다. 각 기록은 Workspace, subject/predicate, 사실성,
  confidence, 출처, 기록 시각, 만료 시각과 metadata를 보유한다.
- 모순 기록은 조용히 덮어쓰지 않고 `conflicts_with` 관계로 함께 보존한다. 사용자 정정은
  기존 활성 기록을 `superseded`로 전환하고 새 기록과 연결한다.
- 자격증명·토큰·개인식별번호 패턴은 자동 장기 기억 단계에서 차단한다.
- Markdown heading, JSON top-level, CSV header/row, 코드 symbol 경계를 보존하는 Chunking을
  추가했다. 모든 Chunk는 안정 ID, section, line range와 원본 해시를 가진다.
- 검색 전 로컬 원본 변경·삭제를 자동 동기화한다. Metadata filter 뒤 lexical/vector
  점수와 기록 시각으로 reranking하며 결과마다 citation 객체를 반환한다.
- 답변 Context에 근거 Chunk ID와 출처·section·line을 포함하고, 문서 기반 주장은 근거 ID를
  표시하도록 응답 계약을 연결했다.
- 웹 문서는 주제별 TTL을 적용한다. 만료 정보는 기본 검색에서 제외되고 명시적 재검증 Tool만
  TTL을 갱신한다.
- 전체 회귀 `212 passed, 4 deselected`를 통과했다.

P7 상태: **완료**. 다음 단계는 P8 Research·Browser Agent다.

## P8 — Research·Browser Agent

- [x] 검색 결과 페이지 실제 방문과 본문 추출
- [x] 게시·수정 날짜 확인
- [x] 공식 출처 탐지와 우선순위
- [x] 다중 출처 교차 검증과 상충 정보 표시
- [x] 주장별 인용
- [x] 로그인 세션·쿠키·다운로드
- [x] PDF·표·동적 페이지 처리
- [x] Prompt Injection과 웹 지시 격리
- [x] 검색 Cache와 만료

진행 기록 (2026-08-02):

- `ResearchAgent`가 DDGS 결과의 스니펫을 답으로 사용하지 않고 후보 URL을 Playwright로
  실제 방문한다. HTTP 성공과 최종 URL을 확인한 페이지 본문만 Research Source가 된다.
- meta, JSON-LD, `time[datetime]`, Last-Modified에서 게시·수정 날짜를 추출한다.
  정부·교육 도메인과 공식/보도자료 표식을 점수화해 공식 출처를 우선 정렬한다.
- 여러 본문의 유사 주장을 묶어 Source ID를 인용하고 숫자 값이 다른 주장은 conflicts에
  출처별 원문을 보존한다. 사용자 응답도 `[S1]` 형식의 주장별 인용과 상충 경고를 유지한다.
- 이름이 정규화된 Playwright persistent profile로 로그인 Cookie·세션을 재사용한다.
  다운로드는 허용 경로에만 저장하고 크기·SHA-256을 검증한다.
- 렌더링 완료 대기, HTML table 구조 추출, PyMuPDF 기반 PDF 본문 추출을 지원한다.
- 웹 본문의 시스템 지시·이전 명령 무시·비밀 노출·도구 실행 패턴은 분석 전에 격리하고
  Source에 경고를 남긴다. 레거시 본문·검색 스니펫 경로에도 같은 방어를 적용했다.
- SQLite Research Cache는 query·source 수·profile 단위 키와 TTL을 사용한다.
  `force_refresh`만 캐시를 우회하며 만료 레코드는 조회 시 제거한다.
- Playwright Chromium 설치·headless 실행을 확인했고 전체 회귀
  `212 passed, 4 deselected`를 통과했다.

P8 상태: **완료**. 다음 단계는 P9 Office·Cloud·Communication이다.

## P9 — Office·Cloud·Communication

- [x] 기존 서식·템플릿 보존형 Office 편집
- [x] 렌더링 기반 시각 QA
- [x] HWP·Office COM 라이브 어댑터
- [x] Gmail/Google Calendar OAuth
- [x] Outlook/Graph OAuth
- [x] Drive/OneDrive/Notion 동기화
- [x] 초안·승인·외부 반영 Tool 분리
- [x] 원격 Message/Event ID 검증
- [x] Slack/Teams 조회·요약·승인 후 전송

2026-08-02 구현 상태:

- OOXML/HWPX 패키지의 style·theme·media·layout·header 등 보호 파트를 해시 비교하고,
  임시 복사본 편집 후 원자 교체한다. DOCX/XLSX/PPTX/HWPX 텍스트 치환과 PDF 페이지
  렌더·비어 있지 않은 페이지 검증을 제공한다.
- Word/Excel/PowerPoint/HWP COM은 설치 흔적이 아니라 Registry의 LocalServer 실행 파일을
  확인한 뒤에만 연결한다. 현재 개발 PC에는 실제 COM class factory가 없어 어댑터는
  `available=false`이며, 설치된 Office/HWP 환경에서 별도 실환경 수락 검증이 필요하다.
- Google/Microsoft Authorization Code + PKCE와 refresh를 구현하고 토큰은 Windows DPAPI로
  암호화한다. 외부 전송은 로컬 draft 원장과 `external_send` 승인을 통과해야 한다.
- Gmail·Google Calendar·Outlook·Teams·Slack 적용 후 Message/Event ID를 다시 조회한다.
  Drive·OneDrive·Notion 원격 ID 카탈로그와 Slack·Teams 근거 요약을 제공한다.
- 자격증명이 없는 자동 테스트에서는 모의 HTTP 계약·DPAPI 왕복·승인 경계·ID 재조회를
  검증했다. 실제 계정 생성·동의와 외부 전송은 사용자의 자격증명 및 명시 승인 전까지
  수행하지 않았다.
- 전체 회귀 테스트 `212 passed, 4 deselected`를 통과했다.

P9 상태: **런타임 구현 완료 / 실계정·설치형 COM 수락 테스트 대기**. 다음 단계는 P10이다.

## P10 — OS·Multimodal·Voice

- [x] API/CLI/COM/UI Automation 우선, 좌표 클릭은 최후 수단
- [x] 창 Handle·접근성 트리·포커스 검증
- [x] 화면 선택 영역·OCR·표·차트 이해
- [x] 다중 이미지·영상 프레임 문맥
- [x] Wake word 전용 감지와 STT 후보 재평가
- [x] TTS 에코 제거·사용자 끼어들기·재생 취소
- [x] GPU 중앙 큐와 VRAM 예산
- [x] 장치 변경·절전 복귀 자동 복구

2026-08-02 구현 상태:

- Windows 자동화 우선순위를 API → CLI → COM → UI Automation → 좌표 fallback으로
  고정했다. 좌표 클릭은 별도 `coordinate_control` 확인 권한과 이유를 요구하며 결과는
  검증 불가 상태로 유지한다. 창 Handle·PID·foreground와 UIA Control tree를 조회한다.
- 화면 전체/선택 영역 PNG 캡처, 이미지 1~12개 순서 보존 분석, 영상 균등 프레임 샘플링을
  지원한다. OCR·표·차트 모드는 로컬 Vision 모델이 입력 해시와 함께 근거를 반환한다.
- 호출어 후보는 음향 점수·무음 확률·Registry 어휘·첫 단어 유사도로 재평가한다.
  TTS 출력 참조 신호의 투영 성분을 마이크 입력에서 제거한 뒤 지속 근접 발화를 판정해
  재생을 취소한다.
- STT와 Vision은 process-wide GPU admission queue를 공유하며 STT 우선순위와 VRAM
  예산을 적용한다. 예산은 CUDA VRAM의 82% 또는 `GPU_VRAM_BUDGET_MB`로 지정한다.
- 입력 장치가 변경되거나 절전 복귀 뒤 stream이 끊기면 bounded exponential backoff로
  최대 5회 재연결한다. 사용자가 직접 감지를 중지한 경우에는 재연결하지 않는다.
- P10 집중 검증 38개, 확장된 전체 회귀 `280 passed, 4 deselected`를 통과했다.
  Codex 실행 세션은 대화형 Windows 화면 접근이 없어 실제 화면 캡처가 차단됐으며,
  Jarvis GUI 사용자 세션에서 화면·UIA·끼어들기·장치 복귀 실환경 수락 테스트가 필요하다.

P10 상태: **런타임 구현 완료 / 대화형 장치·화면 수락 테스트 대기**. 다음 단계는 P11이다.

## P11 — Automation·Proactive Policy

- [x] 집중 모드·회의·전체화면·방해 금지
- [x] 중요도·중복·보류·묶음 알림
- [x] 알림 근거와 “왜 알려줬는지” 제공
- [x] 장시간 Scheduler soak test
- [x] 재부팅·절전 후 작업 복원
- [x] 선제 제안과 실제 실행 권한 분리

2026-08-02 구현 상태:

- 집중 모드·회의·전체화면·방해 금지 상태를 사용자별 JSON에 원자 저장한다. 전체화면은
  foreground 창과 monitor rect로 자동 감지하며 나머지 상태는 명시 설정한다.
- 알림은 severity·dedupe key·이유·이벤트 ID·출처 Evidence와 함께 SQLite 원장에 남긴다.
  비중요 알림은 방해 상태에서 보류되고 상태 해제 이벤트 뒤 중요도순 digest로 묶인다.
  security critical 알림만 방해 금지를 우회한다.
- 선제 행동은 proposal과 approval만 저장한다. 승인 Tool은 원래 대상 Tool을 실행하지
  않으며 실제 실행은 Plugin Registry의 별도 Tool 호출과 원래 Permission을 다시 거쳐야 한다.
- Scheduler는 heartbeat·마지막 복원·활성 작업·실행 lease를 기록한다. 시작 시 활성 작업을
  DB에서 복원하고 10초 이상의 tick 공백을 절전/중단으로 감지해 다시 로드한다.
- 100,000 tick 가속 soak, 실제 2.2초 heartbeat 시작/중지, 20초 절전 공백 주입 복원을
  검증했다. P11 관련 33개와 전체 `289 passed, 4 deselected`를 통과했다.
  며칠 단위 실제 wall-clock soak와 Windows 재부팅 E2E는 배포 수락 환경에서 계속 관찰해야 한다.

P11 상태: **정책·복원 런타임 구현 완료 / 장기 wall-clock 운영 수락 관찰 대기**. 다음 단계는 P12이다.

## P12 — Security·Observability·Productization

- [x] 폴더·앱·도메인·계정별 권한
- [x] 한 번/세션/항상 허용 정책
- [x] Credential Manager 기반 비밀 저장
- [x] 로그 개인정보·토큰 마스킹
- [x] 구조화 JSON Trace와 회전 로그
- [x] Task/Tool/검증 ID 연결
- [x] 모델·GPU·지연·재시도 지표 수집 기반
- [x] 진단 보고서와 안전 모드
- [ ] 실제 장치·네트워크·OAuth E2E
- [x] 경량 Bootstrap·모델 선택 다운로드
- [x] 업데이트·DB migration·rollback

진행 기록:

- 2026-08-02: 폴더·앱·도메인·계정 범위와 1회 소비·세션·영구 수명을 지원하는
  권한 저장소를 중앙 Tool 검사에 연결했다. Windows Credential Manager는 평문 fallback
  없이 실패 폐쇄하며 관리 Tool은 비밀 원문을 반환하지 않는다.
- 회전 JSONL Trace에 Task·Tool call·verification ID와 지연·결과를 연결한다. 토큰,
  Authorization, Cookie, 이메일, 전화번호는 공통 Redactor를 거친다. 안전 모드는 중앙
  경계에서 읽기 이외 작업을 차단하고 진단 보고서는 DB quick check와 메트릭을 포함한다.
- 선택형 Bootstrap은 역할별 asset의 SHA-256을 검증한 후 원자 설치한다. Update는 checksum,
  ZIP 경로 탈출 방지, backup·rollback을 제공하며 DB migration은 version 원장을 사용한다.
- 장치·네트워크·OAuth 프로브는 `passed/ready/skipped/not_run`을 구분한다. 현재 자동 환경에
  OAuth 자격증명과 명시적 외부 네트워크 수락 실행이 없어 해당 항목은 성공으로 표기하지 않았다.

P12 상태: **보안·관측성·제품화 런타임 구현 완료 / 실제 네트워크·OAuth 배포 수락 대기**.

2026-08-02 실제 GUI 재수락:

- `main_qt.py` 실제 이벤트 루프에서 typed Tool 결과가 Presenter에 그대로 전달되어 날짜
  응답이 150초 동안 멈추는 결함을 발견하고, 중앙 표시 경계에서 검증 envelope와 도메인
  payload를 분리했다. 날짜와 Open-Meteo 서울 날씨가 GUI에 정상 표시됐다.
- `안녕, 오늘 기분은 어때?`를 시간 민감 웹 검색으로 오인하던 Router를 수정했다. 사회적
  대화는 명시적 조사 요청이 없으면 conversation에 남고, “웹에서 찾아줘”는 live intent를 유지한다.
- Windows Credential Manager 임시 secret 저장·조회·삭제, 실제 HTTPS(약 1.2초), 카메라
  프레임 기반 열거 2개, Anis TTS와 AudioProcessor 재생, Qt 패키징 smoke를 통과했다.
- Scheduler 100,000 tick soak는 실패 0건, Action Journal DB quick check는 `ok`, 전체
  회귀는 `303 passed, 4 deselected`다. 실제 OAuth는 자격증명이 없어 여전히 미수락이다.

## 모델 정책

로컬 7B 모델은 장문 계획과 미묘한 Tool 선택에 한계가 있습니다. 그러나 모든 요청을
큰 모델로 바꾸는 대신 대화, 라우팅, 코딩, 문서, Vision, 검증 역할을 분리하고
복잡한 계획·검토에만 더 강한 모델이나 선택적 API를 사용하는 것을 원칙으로 합니다.

## 개발 순서

```text
P0 진실한 실행
→ P1 Task State
→ P2 Capability Router
→ P3 Coding Agent
→ P4 Workspace Intelligence
→ P5 Orchestrator
→ P6 Plugin 단일화
→ P7~P12 영역 확장과 제품화
```

각 Phase는 파일이나 클래스의 존재가 아니라 자동 회귀 테스트와 실제 E2E 수락 기준을
통과했을 때만 완료로 표시합니다.
# 시안 전문가 근본 재설계 (2026-08-15)

구현 완료: 역할별 전문가 팀 오케스트레이션, Qwen2.5-VL 모델 역할, 이미지 전용 스타일 인덱스,
승인 결과 인덱싱, 버전형 레이어 그래프, SVG/Qt 렌더링, 얼굴·안전영역 분석, BiRefNet 지연 로드와
GrabCut 폴백, 고급 타이포 속성, 렌더링 후 Vision 검수, 제한 자동 교정, 승인 기반 LoRA 데이터셋
게이트, 생성 이미지와 정확한 텍스트 레이어 분리.

운영 원칙: RTX 4060 Laptop 8GB에서는 모든 모델을 상주시킬 수 없으므로 Vision → 설계 → 생성 →
검수 순서로 한 번에 하나의 무거운 모델만 활성화한다. SAM2는 Windows 네이티브 설치보다 WSL이
권장되므로 기본 분할기는 BiRefNet으로 정하고, 설치 전에는 OpenCV 폴백을 사용한다.

후속 품질 축적: 승인 결과 20개 이전에는 모델 가중치 학습을 실행하지 않는다. 승인 결과의
배치·타이포·색상과 이미지 임베딩을 먼저 누적하고, 충분한 데이터가 생긴 후에만 SDXL LoRA를
사용한다. FLUX는 8GB VRAM에서 상시 경로가 아니라 선택적 외부/CPU-offload 백엔드로 둔다.

## 통합 운영 Runtime 수락 (2026-08-21)

- [x] 모든 실제 Executor 단계에 영속 `TaskContract`와 성공 조건·Evidence 연결
- [x] Supervisor의 RAM·VRAM·동시 실행·시간 예산과 취소 요청 영속화
- [x] 전문가 작업공간의 Planner → Executor → Reviewer 순차 팀 실행과 공유 RAG
- [x] 승인 경계에서 정확히 중단·재개하는 지속 워크플로
- [x] 실데이터만 사용하는 Morning Brief와 미연결 소스의 명시적 경고
- [x] 첫 실행/수동 실환경 진단과 개인정보가 필요한 장치 검사의 선택적 실행
- [x] 11영역 Command Center와 작업 취소·워크플로 실행·진단 제어
- [x] RAG 검색 후보, 프롬프트 포함, 실제 응답 사용 trace 분리
- [x] 사용자 요청에 따른 카메라 기본 자동 실행·로컬 MediaPipe 제스처 처리. 승인/취소 등 명령 제스처는 별도 opt-in이며 기본 비활성. 장치/권한 실패를 활성 성공으로 표시하지 않는다.

실행 환경에서는 무거운 모델을 동시에 상주시키지 않는다. Supervisor와 GPU Scheduler가 역할별
예산을 확인하고 전문가 팀은 순차 실행한다. 제스처 모델은 별도 `requirements-gesture.txt`로
고정했으며 MediaPipe Tasks API와 `opencv-contrib-python 4.8` 조합을 실제 설치·초기화했다.

수락 증거:

- 실제 `main_qt.py` 이벤트 루프 기동, RAG CUDA, STT CUDA, Automation, 마이크 연결 확인
- 비파괴 core 진단 10개 중 실패 0개: 8 passed, 2 warning
- 경고 2개는 선택 작업공간 없음과 선택형 외부 계정 미구성 상태
- 전체 회귀 `444 passed, 4 deselected`, 의존성 충돌 0개

남은 항목은 외부 수락 조건이다. OAuth/SMTP 자격증명과 실제 계정, 사용자의 카메라·스피커 라이브
검사 동의, 장시간 wall-clock 운영 관찰이 준비되면 Command Center의 진단 탭에서 실행한다.

## 2026-08-21 범용 역량 실행과 자체 개발 경계

- 선언형 Intent가 없는 요청도 실행형 문장이고 Plugin Registry의 Tool 설명과 충분히 일치하면
  관련 Tool만 동적으로 노출한다. 일반 잡담은 기존 Conversation 경로에 남고, Registry 근거가
  없는 요청에는 로컬 모델이 임의 Tool 이름을 만들 수 없다.
- 한국어 조사·어미 때문에 Registry 설명 매칭이 끊기지 않도록 언어 독립 문자 n-gram을 사용한다.
  새 플러그인을 추가하면 Executor의 도메인 분기 코드를 고치지 않아도 capability 설명을 통해
  후보가 될 수 있다.
- 웹 공급자를 코드 분기 대신 `config/web_providers.json`에서 관리한다. 사이트 검색과 미디어
  재생은 Tool 계약·권한·Evidence를 통과하며, OS 브라우저 전달과 실제 페이지/오디오 성공을
  구분한다.
- Anis 자신은 전용 `agent_self` 플러그인으로 저장소·Registry·권한·RAM·GPU·Ollama 상태와
  가능한 Tool을 실제 조회한다. 계정 미설정은 플러그인 코드 결함과 별도로 보고한다.
- 자체 변경은 사용자 Workspace와 분리된 고정 프로젝트 루트, 별도 `self_modify` 승인,
  관련도 기반 파일 선택, 최소 patch, diff 검토, 관련 검증, 실패 롤백으로 실행한다. 사용자
  데이터·기억·모델·Git 내부·비밀·권한 우회는 수정 범위가 아니다.
- 자동 회귀 `450 passed, 4 deselected`. 실환경 Registry 31개 중 29개 검증, 2개는 선택형
  Mail/Cloud 계정 미설정, 기술 결함 0개. CUDA RTX 4060과 Ollama 9개 모델 연결을 확인했다.
