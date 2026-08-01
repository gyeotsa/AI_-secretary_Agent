# JARVIS Agent Runtime 개선 로드맵

기준일: 2026-07-27

## 목표

JARVIS의 목표는 기능이 많은 챗봇이 아니라 다음 순환을 신뢰성 있게 수행하는
개인 AI 에이전트입니다.

```text
이해 → 계획 → 권한 → 실행 → 관찰 → 검증 → 복구/재계획 → 기억 → 보고
```

모델이 자연스러운 문장을 만드는 것과 실제 작업이 완료되는 것은 별개입니다.
이 로드맵은 모델 성능보다 실행 구조, 문맥 유지, 검증 가능성, 안전성과 제품 품질을
우선합니다.

## 현재 판정

| 영역 | 현재 수준 | 판정 |
|---|---|---|
| 대화 UI·세션·음성 | 실제 사용 가능, 장치별 편차 존재 | 부분 완료 |
| Plugin Registry | 동적 Tool·Intent 로딩 | 기반 완료 |
| Intent/Slot | 선언형 계약+패턴, 표현 확장에 민감 | 개선 중 |
| 파일·문서 작업 | 주요 형식 생성·읽기와 일부 실검증 | 부분 완료 |
| 웹 조사 | DDGS 검색·출처·후속 문맥 | 1차 완료 |
| Coding | 단일 파일 생성·수정·문법 검증 | 초기 단계 |
| Planner/Executor | 구성 요소 존재, 책임 과대·문자열 계약 잔존 | 재설계 필요 |
| Verify/Recover | 일부 Tool별 검증·복구 | 부분 완료 |
| Workspace/Indexer | 수동 선택·기초 인덱서 | 연결 보강 필요 |
| RAG/Memory | 로컬 벡터 검색·복수 메모리 DB | 정책 보강 필요 |
| Automation/Observer | 알람·스케줄·감시 기반 | 장시간 검증 필요 |
| OAuth/Cloud | 로컬 초안·ICS 중심 | 미완료 |
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

- [x] `domain/action/target/constraints/reference/confidence` 표준 의도 타입
- [ ] Plugin별 입력·출력·부작용·권한·검증 계약
- [ ] 낮은 신뢰도에서만 최소 확인 질문
- [ ] 시간 민감 질문의 웹 검색 자동 라우팅
- [ ] 대화, 조회, 실행, 변경, 외부 전송을 분리
- [ ] Intent 충돌 점수와 설명 가능한 라우팅 로그
- [ ] 실제 사용자 발화 기반 라우팅 회귀 데이터셋

수락 기준:

- 새 표현을 추가할 때 Executor 도메인 분기를 수정하지 않습니다.
- 잘못된 도구 선택이 실행 전에 계약 검사에서 차단됩니다.

## P3 — 범용 Coding Agent

목표: 단일 파일 재작성에서 저장소 단위 개발 에이전트로 발전합니다.

- [ ] 저장소 구조·README·의존성·Git 상태 사전 분석
- [ ] 관련 심볼과 파일 검색
- [ ] 변경 계획과 영향 범위 작성
- [ ] 전체 파일 재생성 대신 최소 patch 적용
- [ ] 인코딩·줄바꿈·사용자 변경 보존
- [ ] 언어별 문법·formatter·lint·type check
- [ ] 관련 테스트 자동 선택·실행
- [ ] 실패 로그 분석 후 수정·재검증
- [ ] 최종 diff 자체 검토
- [ ] 원자적 적용과 실패 롤백
- [ ] 새 기능 테스트 생성
- [ ] 빌드·실행 결과와 Artifact 보고

수락 기준:

- 여러 파일 변경 요청을 계획부터 테스트까지 끝냅니다.
- 테스트 실패 상태에서 “완료”라고 말하지 않습니다.

## P4 — Workspace·Project Intelligence

- [ ] 마지막 Workspace 자동 복원
- [ ] Workspace 목록·별칭·프로젝트별 설정
- [ ] 선택 직후 자동 인덱싱
- [ ] `.gitignore` 준수와 대형 폴더 제외
- [ ] 파일 변경 감지 기반 증분 인덱싱
- [ ] 언어·프레임워크·테스트 명령 감지
- [ ] 프로젝트별 Memory/RAG Namespace
- [ ] 현재 Git 브랜치·dirty 상태 UI
- [ ] 새 프로젝트 템플릿·가상환경·Git 초기화

## P5 — Planner·Executor·Recovery 재설계

- [ ] `executor.py`의 대화·계획·실행·응답 책임 분리
- [ ] 실행 가능한 Plan DAG
- [ ] 단계별 사전 조건·예상 Artifact·검증 방법
- [ ] 독립 단계 병렬 실행
- [ ] 관찰 결과 기반 재계획
- [ ] 오류 서명·시도 전략·재시도 예산 저장
- [ ] 동일 실패 반복 차단
- [ ] 복구 후 같은 검증기 재실행
- [ ] 사람 승인이 필요한 중단점 명시

## P6 — Tool Runtime과 Plugin 완전 단일화

- [ ] `core/tools.py` 레거시 Tool을 Plugin으로 이전
- [ ] 중복 이름·Schema 충돌을 시작 시 차단
- [ ] JSON Schema 입력과 출력 모두 검증
- [ ] Plugin 버전·의존성·지원 OS·취소 가능성 선언
- [ ] 동기/비동기·Timeout·재시도 정책
- [ ] Plugin 상태·인증·진단 UI
- [ ] 연결됨/설치됨/인증됨/검증됨 상태 분리

## P7 — Memory·RAG·Knowledge

- [ ] 단기 대화/Task/프로젝트/선호/사실/사례 메모리 분리
- [ ] 사실·추측·출처·기록 시각 저장
- [ ] 사용자 정정과 모순 해결
- [ ] 민감 정보 자동 기억 금지
- [ ] 문서 유형별 구조 보존 Chunking
- [ ] 삭제·수정 문서 인덱스 동기화
- [ ] Metadata filter와 reranking
- [ ] 답변 주장과 근거 Chunk 연결
- [ ] 웹 정보의 만료·재검증 정책

## P8 — Research·Browser Agent

- [ ] 검색 결과 페이지 실제 방문과 본문 추출
- [ ] 게시·수정 날짜 확인
- [ ] 공식 출처 탐지와 우선순위
- [ ] 다중 출처 교차 검증과 상충 정보 표시
- [ ] 주장별 인용
- [ ] 로그인 세션·쿠키·다운로드
- [ ] PDF·표·동적 페이지 처리
- [ ] Prompt Injection과 웹 지시 격리
- [ ] 검색 Cache와 만료

## P9 — Office·Cloud·Communication

- [ ] 기존 서식·템플릿 보존형 Office 편집
- [ ] 렌더링 기반 시각 QA
- [ ] HWP·Office COM 라이브 어댑터
- [ ] Gmail/Google Calendar OAuth
- [ ] Outlook/Graph OAuth
- [ ] Drive/OneDrive/Notion 동기화
- [ ] 초안·승인·외부 반영 Tool 분리
- [ ] 원격 Message/Event ID 검증
- [ ] Slack/Teams 조회·요약·승인 후 전송

## P10 — OS·Multimodal·Voice

- [ ] API/CLI/COM/UI Automation 우선, 좌표 클릭은 최후 수단
- [ ] 창 Handle·접근성 트리·포커스 검증
- [ ] 화면 선택 영역·OCR·표·차트 이해
- [ ] 다중 이미지·영상 프레임 문맥
- [ ] Wake word 전용 감지와 STT 후보 재평가
- [ ] TTS 에코 제거·사용자 끼어들기·재생 취소
- [ ] GPU 중앙 큐와 VRAM 예산
- [ ] 장치 변경·절전 복귀 자동 복구

## P11 — Automation·Proactive Policy

- [ ] 집중 모드·회의·전체화면·방해 금지
- [ ] 중요도·중복·보류·묶음 알림
- [ ] 알림 근거와 “왜 알려줬는지” 제공
- [ ] 장시간 Scheduler soak test
- [ ] 재부팅·절전 후 작업 복원
- [ ] 선제 제안과 실제 실행 권한 분리

## P12 — Security·Observability·Productization

- [ ] 폴더·앱·도메인·계정별 권한
- [ ] 한 번/세션/항상 허용 정책
- [ ] Credential Manager 기반 비밀 저장
- [ ] 로그 개인정보·토큰 마스킹
- [ ] 구조화 JSON Trace와 회전 로그
- [ ] Task/Tool/검증 ID 연결
- [ ] 모델·GPU·지연·재시도 지표
- [ ] 진단 보고서와 안전 모드
- [ ] 실제 장치·네트워크·OAuth E2E
- [ ] 경량 Bootstrap·모델 선택 다운로드
- [ ] 업데이트·DB migration·rollback

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
