# Level 2 전수 검토 결과 (2026-07-22)

## 2026-07-27 재평가

실제 GUI 대화를 반복 검증한 결과, “Level 2 완료”는 구성 요소 존재 기준으로는
맞지만 제품 수준 Agent Runtime 완료를 뜻하지 않습니다. 다음 문제가 실제로
재현되고 일부는 이번 주기에 보완됐습니다.

- Tool을 호출하지 않은 LLM이 파일·프로젝트·알람·검색 작업을 완료했다고 주장
- 대기 작업의 대상·지시 Slot이 후속 발화에서 유실되거나 오래된 작업에 고착
- 최신 정보 질문이 웹 검색 없이 로컬 모델 기억으로 답변
- 파일 생성과 내용 수정을 같은 Intent로 처리해 기존 파일을 빈 내용으로 덮어쓸 위험
- Workspace 선택이 자동 복원·Indexer·권한 범위와 완전히 연결되지 않음
- Tool별 Verifier가 없는 경우 문자열 오류 탐지만으로 성공 판정
- Coding 기능이 저장소 단위 patch·lint·test·diff 루프가 아닌 단일 파일 생성 중심

최근 보완:

- 프로젝트·파일 생성과 코드 수정 Intent 분리, 실제 파일·해시·Python 문법 검증
- 상대 시간 알람을 실제 Scheduler와 UI/TTS callback에 연결
- 실시간 날짜·시간·Open-Meteo 날씨 조회
- DDGS 실제 웹 검색, 출처 URL 검증, 공식 출처 우선순위와 간결한 답변
- 두 발화 이상으로 나뉜 대상·지시 Slot 누적
- 역할별 conversation/reasoning/code/vision 모델 라우팅

현재 최우선 기술 부채는 문자열 기반 Tool 결과를 타입 기반 결과·증거 계약으로
교체하는 일입니다. 전체 개선 순서와 수락 기준은
[`AGENT_RUNTIME_ROADMAP.md`](AGENT_RUNTIME_ROADMAP.md)를 따릅니다.

첫 전환 작업으로 `core/tool_result.py`에 상태·검증 증거·Artifact·소요 시간을 담는
공통 계약을 추가했고, Executor의 Plugin Intent 실행과 자율 Tool 검증 경로가
`ToolRunResult.succeeded`를 기준으로 완료 여부를 판단하도록 연결했습니다. 주요
실행 Tool은 직접 typed 결과로 이전했지만 일부 레거시 내장 Tool이 남아 있어 P0는
진행 중입니다.
전용 검증기가 없는 Tool을 일반 성공으로 처리하던 fallback은 제거했으며, 이제
해당 실행은 `unverified` 상태로 남고 사용자에게도 완료로 보고되지 않습니다.
Plugin SDK는 `str | ToolRunResult` 점진 이전을 지원하고, 파일시스템의 생성·수정
Tool은 실제 경로·파일 크기·변경 전후 해시를 Evidence와 Artifact로 직접 반환합니다.
ReAct·Multi-Agent·Scratchpad에는 이중 반환 형식의 호환 경계를 연결했습니다.
Calendar와 Office 5종 생성 Tool도 typed 결과로 이전했으며, 저장 파일을 형식별
라이브러리로 다시 열어 내부 구조를 확인합니다. typed 성공이라도 Evidence가 없으면
Executor가 `unverified`로 강등합니다.
Browser 검색·페이지·스크린샷과 Weather 조회도 typed 결과로 이전했습니다. 검색
출처 URL·조회 시각, HTTP 최종 URL·상태·본문 해시, 날씨 공급자·좌표·관측 시각을
Evidence로 남기며 불완전한 실시간 응답은 실패 처리합니다.
Git·Windows Control·Mail·Alarm도 typed 결과로 이전했습니다. Git HEAD와 브랜치,
Windows PID·창·별칭 상태, EML 구조·SMTP 접수, Scheduler 작업 ID를 검증하며
UAC·바로가기처럼 완료 확인이 불가능한 요청은 `unverified`로 보고합니다.
System Tools도 로컬 시계·타임존과 Plugin Registry 스냅샷 Evidence를 반환합니다.
중앙 ToolExecutor는 이제 Plugin과 레거시 내장 Tool 모두를 `ToolRunResult`로
정규화하며, 전용 검증기가 없는 레거시 결과를 자동 성공시키지 않습니다.
파일 읽기·디렉터리 목록·Workspace·Project Indexer·Semantic Memory·RAG 내장
Tool도 직접 typed 결과로 이전했습니다. 파일 해시, 프로젝트 루트·검색 건수,
메모리 영속성, RAG Chunk·출처를 확인하며 Workspace와 Indexer 사이의 누락된
`current_workspace` 연결도 수정했습니다.
파일 쓰기·폴더 생성/삭제·명령 실행, 마이크·카메라 목록과 캡처, 자동화 작업과
엔진 상태도 직접 typed 결과로 이전했습니다. 저장 바이트와 해시, 삭제 후 부재,
프로세스 종료 코드, 실제 장치 목록과 캡처 이미지, Scheduler DB와 실행 스레드를
재확인하며 존재하지 않는 자동화 작업 ID는 실패 처리합니다.
프로필·Knowledge Graph·Multi-Agent·STT/감지·Scheduler 호환 API와 구형 웹 검색·
Vision·PDF·Excel 내장 Tool도 직접 typed 결과로 이전했습니다. 검증할 수 없는
모델 해석과 장치 스트림 준비 전 상태는 성공으로 승격하지 않습니다. PDF 전체 추출의
페이지 인덱스 오류와 중복 `add_document` 정의도 제거했습니다.
TTS는 GUI 문자열 호환 API와 Registry typed API를 분리했습니다. 중앙 Tool 결과는
권한 거부까지 Action Journal에 입력·상태·Evidence·Artifact·지연 시간으로 기록되고
자격 증명은 마스킹됩니다. 일반 대화의 실행하지 않은 완료 주장도 차단하며,
Presenter는 기술 로그와 화면·음성 본문을 별도 채널로 제공합니다.
Recovery도 typed 상태만 직접 사용하도록 전환해 정상 결과 본문의 “오류” 단어 때문에
실패하는 문제와 미검증 복구 결과의 성공 승격을 제거했습니다. 전체 실행은
`completed`, `partial`, `failed`, `cancelled`를 구분하고 재시도 횟수와 단계 수를
최종 응답에 표시합니다. 이에 따라 P0 진실한 실행 시스템은 완료로 판정합니다.

P1 첫 구현으로 대화·pending·recent intent·Scratchpad 실행 결과를 SQLite의
통합 `agent_tasks` 상태에 연결했습니다. Task별 Workspace, Intent/Slot, 확인 질문,
대화 문맥, 계획, Artifact/Evidence, 마지막 Tool, 재시도와 검증 상태가 재시작 후에도
복구됩니다. Workspace·세션 간 pending/recent intent/작업 제어가 격리되고, 7일이
지난 확인 대기는 만료됩니다. 상태 전이 검증 API와 문맥 신뢰도도 추가했습니다.
Executor의 수정·취소·정지·재개 경로도 허용 상태 전이 API를 강제 사용합니다.
재시작 작업은 동일 Task ID로 대기열에 복구되며, 상단 Task 관리 UI에서 현재
세션·Workspace 작업의 상세 상태를 확인하고 재개·취소·종료 기록 삭제를 수행할 수
있습니다. 이에 따라 P1 대화와 Task State 통합은 완료로 판정합니다.

현재 기본 자동 회귀 테스트 기준선은 `184 passed, 4 deselected`입니다. 이는
네트워크·OAuth·실제 장치·Office COM·장시간 자동화를 모두 보증하는 수치는 아닙니다.

P2 첫 구현으로 표준 Intent의 domain/action/target/constraints/reference/confidence와
Registry 소유 CapabilityContract를 추가했습니다. 라우팅 결과는 선택 근거와 상위
대안을 제공하고, Executor는 실행 전에 Registry 필수 입력 계약을 검사합니다. 기존
Plugin은 하위 호환되며, 이 1차 시점에는 개별 Tool 계약 정규화와 낮은 신뢰도 확인
질문 정책이 후속 범위로 남아 있었습니다.

P2 2차에서 모든 Plugin Tool 계약을 `read/execute/change/external_send` 부작용과
공통 typed 출력 스키마로 정규화했습니다. Registry는 41개 Tool·21개 Intent의
계약 완전성과 요청 종류/부작용 일치를 전수 검사하고 Executor는 실행 전 불일치를
차단합니다. 유사 Intent가 근접 점수로 충돌할 때는 실행하지 않고 상위 두 후보를
한 문장으로 확인하며 선택 근거와 대안을 로그에 남깁니다. 현재 기준선은
`187 passed, 4 deselected`입니다.

P2 마지막 단계에서 Intent freshness/source 계약과 일반화된 시간 민감도 판정을
연결했습니다. 전용 현재 정보 Plugin이 우선하고 그 밖의 현재·최근 정보 질문만 실제
웹 검색으로 라우팅됩니다. 실제 사용자형 발화를 JSON 데이터셋으로 분리했으며 전체
기준선은 `198 passed, 4 deselected`입니다. P2 Capability Router는 완료입니다.

P3 첫 구현으로 저장소 분석과 원자적 최소 patch를 담당하는 Coding Transaction
엔진을 추가했습니다. 전체 파일 생성 응답을 즉시 덮어쓰지 않고 exact replacement와
변경 전 SHA-256을 요구하며, 다중 파일 적용 후 검증 실패 시 원본 bytes로 롤백합니다.
Coding Plugin은 저장소 snapshot과 변경 diff·검증 결과를 typed Evidence/Artifact로
반환합니다. 현재 기준선은 `204 passed, 4 deselected`이며 계획 생성·심볼 영향 분석·
언어별 검증 자동 선택·실패 재수정 루프는 P3 후속 범위입니다.

P3 2차에서 ProjectIndexer의 Python AST 심볼·파일명·본문 검색을 임시 인덱스로
CodingAgent에 연결했습니다. 구조화 CodingPlan은 관련 파일·심볼·영향 범위·검증
명령을 보존합니다. 변경 파일과 대응하는 pytest를 자동 선택해 py_compile 이후
실행하고 실패 시 전체 transaction을 롤백합니다. JavaScript는 사용 가능한 Node의
`--check`를 선택합니다. 현재 기준선은 `206 passed, 4 deselected`입니다.

P3 3차에서 pyproject/package/Cargo/go manifest와 실제 설치 실행기를 기준으로
formatter·lint·type·test·build 검증 전략을 자동 구성합니다. 모든 명령은 shell 없는
인자 배열이며, 실패 시 원본 롤백 후 구조화 오류를 다음 최소 patch에 전달하는 최대
5회의 복구 인터페이스를 제공합니다. 빈 diff·과대 patch·병합 충돌 표식은 저장 전
자체 검토에서 차단합니다. 현재 기준선은 `209 passed, 4 deselected`입니다.

P3 마지막 단계에서 자연어 저장소 변경 Intent를 CodingPlan과 JSON minimal-edit 계약에
연결했습니다. Coding 모델은 직접 파일을 쓰지 않으며 기존 파일 exact replacement와
명시적 새 파일 생성만 제안합니다. 새 기능은 테스트 edit가 없으면 적용되지 않고,
검증 실패 로그를 모델에 다시 제공해 제한된 재제안·동일 검증을 수행합니다. 자연어
요청으로 제품 코드와 새 테스트를 함께 적용해 pytest까지 통과하는 E2E를 포함해
`212 passed, 4 deselected`이며 P3를 완료로 판정합니다.

## 판정

기존 문서의 “Level 2 완료” 표기는 실제 동작 검증보다 앞서 있었습니다. 이번 검토에서 1–7단계의 핵심 연결 오류를 수정했고 8단계 플러그인과 Chromium 종단 검증, 9단계 RAG, 10단계 역할 기반 하이브리드 LLM 라우팅까지 구현했습니다. 실제 Anthropic 성공 경로 및 GUI·하드웨어 연동은 별도 실환경 검증이 남아 있습니다.

## 주요 수정

- Tool Registry: 플러그인 자동 로딩을 멱등화하고 `get_tools_schema()`에 동적 반영. 70개 스키마 이름의 중복 없음 확인.
- Permission: `ToolExecutor.execute_tool()`을 중앙 권한 관문으로 만들어 Executor, ReAct, Multi-Agent 등 모든 호출 경로에 동일 적용. 권한 검사 예외는 거부(fail closed).
- Executor: native tool calling 사용, 검증/복구 실패 작업의 고착 방지, 실패 상태·Reflection 기록, 잘못된 목표 완료 fallback 수정.
- Security: 경로 `startswith` 우회 제거, Windows 명령 파싱 수정, `shell=False`, 셸 연결·리다이렉션 차단, 실행 파일 allowlist 적용.
- Reflection/Recovery: LLM 응답 `eval()` 제거, 복구 결과를 ToolVerifier로 재검증, 실패 fallback을 성공으로 오판하던 흐름 제거.
- Compatibility: `multi_agent.py`의 LLM/Context/Scratchpad API 불일치, Scheduler의 존재하지 않는 `chat`/`SYSTEM_PROMPT`, KnowledgeUpdater의 존재하지 않는 profile/RAG API를 수정.
- Package loading: `core/__init__.py`의 eager import를 제거해 독립 모듈이 불필요한 LLM 의존성 때문에 로드 실패하지 않도록 수정.
- Step 8: workspace 한정 filesystem 탐색, 인자 배열 기반 Git, 선택적 Playwright browser 플러그인 추가.
- Step 10: Tool Reasoner만 Claude 우선으로 라우팅하고 API 키·API 장애 시 Ollama로 자동 fallback. 일반 응답과 실행·메모리·RAG는 로컬 유지.
- Browser: Playwright/Chromium 설치, 공개 페이지 본문·스크린샷 성공. DNS/redirect/subresource SSRF와 screenshot 경로·복합 권한 검증 추가.

## 검증 결과

- 전체 Python 소스 `compileall`: 통과.
- 로컬 import 정합성 검사: 발견된 KnowledgeUpdater/Scheduler 불일치 수정.
- Tool Registry: 70개/고유 70개, browser/filesystem/git/system_tools 자동 등록 확인.
- 안전성 회귀: 허용 루트와 이름이 비슷한 형제 경로 차단, `&`/`|` 명령 연결 차단, 승인 콜백 없는 파일 쓰기 거부 확인.
- Python 3.12 `.venv` 재생성 및 전체 `requirements.txt` 설치 완료. 기존 환경은 `.venv-python310-broken`으로 보존.
- RAG: `BAAI/bge-m3` 로컬 모델(1024차원), Chroma 문서 추가·한국어 검색·재시작 영속성 테스트 통과.
- 자동 회귀 테스트: Level 2 안전성 4개 + RAG 종단 테스트 1개, 총 5개 통과.

## 남은 우선순위

1. 타입 기반 ToolRunResult와 검증 증거 계약.
2. Conversation/Pending/Intent/Artifact를 통합한 Task State.
3. 저장소 단위 Coding Agent와 patch→test→diff 루프.
4. Workspace·Project Intelligence 완료. 다음 구현 우선순위는 P5 Planner·Executor·Recovery 재설계.

P5·P6·P7·P8·P9·P10·P11 런타임 구현을 완료했으며 다음 구현 우선순위는 P12 Security·Observability·Evaluation·Deployment입니다.

## 2026-08-02 P8 완료

- 검색 결과 후보를 Playwright로 실제 방문하고 렌더링된 본문과 표를 추출합니다.
- 게시·수정일은 meta·JSON-LD·time·HTTP header에서 확인하고 공식 출처를 우선합니다.
- 유사 주장을 다중 출처로 묶어 Source ID를 부여하며 수치 상충은 별도 conflict로 노출합니다.
- Browser persistent profile로 로그인 Cookie를 보존하고 다운로드 결과의 크기·해시를 검증합니다.
- PDF 본문, HTML table, 동적 페이지 network-idle 처리를 지원합니다.
- Prompt Injection 성격의 웹 문장은 분석 Context에서 격리하고 원 Source에 경고를 남깁니다.
- Research Cache는 TTL 만료와 강제 새로고침 정책을 지원합니다.
- Playwright Chromium headless 실행 및 전체 `212 passed, 4 deselected`를 확인했습니다.

## 2026-08-02 P9 런타임 구현 완료

- OOXML/HWPX 보호 파트를 비교하는 원자적 템플릿 치환과 PDF 렌더링 시각 QA를 추가했습니다.
- Office/HWP COM은 실제 LocalServer 등록을 확인하는 라이브 어댑터로 구현했습니다. 현재 PC는
  COM class factory 미등록 상태여서 연결 불가를 성공으로 가장하지 않습니다.
- Google/Microsoft OAuth PKCE·refresh와 Windows DPAPI 토큰 Vault를 구현했습니다.
- Gmail·Calendar·Outlook·Slack·Teams 외부 반영은 로컬 초안과 승인 Tool로 분리했고,
  적용 뒤 원격 Message/Event ID를 재조회합니다.
- Drive·OneDrive·Notion 메타데이터는 원격 ID 기반 SQLite 카탈로그에 동기화합니다.
- P9 집중 테스트 46개가 통과했습니다. 실제 Google/Microsoft/Slack/Notion 계정 호출과
  Office/HWP COM E2E는 자격증명·설치 환경이 준비된 뒤 별도 수락 테스트가 필요합니다.

## 2026-08-02 P10 런타임 구현 완료

- Windows Handle·PID·foreground 검증과 UI Automation 접근성 트리를 추가했습니다.
  좌표 클릭은 별도 확인 권한을 받는 최후 fallback이며 완료로 자동 승격하지 않습니다.
- 화면 선택 영역과 다중 이미지, 영상 시간축 프레임을 로컬 Vision 모델에 전달하고 입력
  해시·프레임 시각을 Evidence로 보존합니다. OCR·표·차트 분석 모드를 제공합니다.
- Wake word 전사 후보 재평가, TTS 참조 기반 에코 제거, 지속 근접 발화 끼어들기와 즉시
  재생 취소를 연결했습니다.
- STT·Vision 중앙 GPU 큐와 VRAM 예산, 마이크 stream의 장치 변경·절전 복귀 재연결을
  구현했습니다. 기본 pytest 수집에 P4~P10 안전 회귀를 편입해 전체 기준선은
  `280 passed, 4 deselected`입니다.
- 실제 데스크톱 화면·UIA·스피커/마이크 acoustic 환경은 Jarvis GUI 사용자 세션에서
  별도 수락 테스트가 필요합니다.

## 2026-08-02 P11 런타임 구현 완료

- 집중·회의·전체화면·방해 금지 상태와 중요도 기반 알림 보류·중복 억제·digest를 구현했습니다.
- 모든 선제 알림은 판단 이유, 이벤트 ID, 출처 Evidence를 SQLite 원장에 남깁니다.
- 선제 행동 proposal 승인과 실제 Tool 실행을 분리해 승인 자체가 외부 행동을 일으키지 않습니다.
- Scheduler heartbeat, 중복 실행 lease, 시작 시 DB 복원, 절전 공백 감지 후 재로드를 추가했습니다.
- 100,000 tick 가속 soak와 실제 heartbeat·절전 공백 주입을 통과했습니다. 전체 회귀는
  `289 passed, 4 deselected`이며 며칠 단위 wall-clock/실제 재부팅은 배포 수락에서 관찰해야 합니다.

## 2026-08-02 P7 완료

- Conversation, Task, Project, Preference, Fact, Case를 별도 의미 유형으로 관리합니다.
- 장기 기록에 사실·추측·사용자 진술, 출처, confidence, 기록·만료 시각을 저장합니다.
- 모순은 충돌 관계로 보존하고 사용자 정정으로만 이전 활성 기록을 폐기합니다.
- 자격증명과 고위험 개인식별정보는 자동 기억하지 않습니다.
- Markdown·JSON·CSV·코드 구조를 보존한 Chunk와 원본 해시 기반 수정·삭제 동기화를
  구현했습니다.
- Metadata filter·reranking 결과는 안정 Chunk ID와 source·section·line citation을
  포함하며 최종 응답 Context와 연결됩니다.
- 웹 정보는 weather·traffic·finance·news·software·general 유형별 TTL을 적용하고,
  만료된 근거는 재검증 전까지 기본 검색과 답변에서 제외합니다.
- 전체 자동화 회귀 결과는 `212 passed, 4 deselected`입니다.

## 2026-08-02 P6 완료

- `get_tools_schema()`와 `execute_tool()`은 초기화된 Plugin Registry만 사용합니다.
  `core/tools.py`의 내장 구현은 `legacy_runtime` Plugin 계약으로 편입했습니다.
- Plugin·Tool·Intent 이름 충돌과 잘못된 입출력 Schema를 시작 시 차단하고,
  모든 실행 입력·출력을 JSON Schema Draft 2020-12로 검증합니다.
- 동기·비동기 실행, Tool별 timeout·재시도·취소 정책을 중앙 worker runtime에서
  처리합니다. 실패는 typed `ToolRunResult`로 반환됩니다.
- 버전·의존성·지원 OS·인증 방식·취소 가능성을 Plugin/Tool 계약에 추가했습니다.
- 상단 Plugin 진단 UI는 설치·연결·인증·검증 상태를 서로 구분해 표시합니다.
- 전체 자동화 회귀 결과는 `212 passed, 4 deselected`입니다.

## 2026-08-01 P5 완료

- 설명 목록 수준의 계획을 cycle·누락 의존성을 거부하는 실행 가능한 Plan DAG로 교체했습니다.
- 단계별 사전조건, Tool 입력, 예상 Artifact, 검증 방법, 승인 이유, 재시도 예산·전략을
  계획 및 통합 Task 상태에 저장합니다.
- 동시에 준비된 독립 단계는 제한된 worker pool에서 병렬 실행하고 의존 단계는 완료를 기다립니다.
- 실제 실패 관찰을 포함해 계획을 revision 단위로 다시 만들며 완료 단계는 반복하지 않습니다.
- 오류 유형과 정규화된 서명을 attempt별로 저장하고 같은 실패 2회·단계 예산·전체 재계획
  2회의 경계로 무한 반복을 차단합니다.
- 복구 실행 결과는 최초 단계와 같은 verifier callback으로 다시 검증합니다.
- 외부 전송·삭제·push 계약은 승인 이유와 함께 `awaiting_approval`로 영속화되며 승인된
  단계만 재개합니다.
- Executor는 호환 facade로 유지하고 Conversation, Planning, Execution/Recovery, Response
  책임을 독립 서비스로 분리했습니다.
- 전체 자동화 회귀 결과는 `212 passed, 4 deselected`입니다.

## 2026-08-01 P4 완료

- 마지막 Workspace를 영속 카탈로그에서 자동 복원하고 목록·별칭·프로젝트별 설정을 관리합니다.
- 선택 직후 전체 상태를 동기화하며 이후에는 `.gitignore`와 대형 생성 폴더를 제외한 파일의
  추가·수정·삭제만 증분 인덱싱합니다.
- 확장자와 manifest에서 언어·프레임워크·사용 가능한 테스트/검증 명령을 감지합니다.
- Conversation Memory와 RAG 문서는 Workspace 경로 해시 namespace로 분리합니다.
- 상단 Workspace 표시에 현재 Git 브랜치와 dirty 상태를 표시하고 주기적으로 갱신합니다.
- Workspace Plugin이 프로젝트 목록·별칭·프로필과 템플릿 기반 프로젝트 생성을 제공합니다.
  `.venv`와 Git 저장소를 선택적으로 초기화하며 중간 실패 시 새 프로젝트 폴더를 롤백합니다.
- 자동화 회귀 결과는 `212 passed, 4 deselected`입니다.
5. 실제 Ollama, GUI, 마이크·카메라·TTS·Scheduler 장시간 종단 검증.
6. mail/calendar OAuth 토큰 저장·권한·계정 선택 정책.

## 2026-08-21 통합 에이전트 운영 계층 검토

이번 구현은 UI 카드만 추가한 작업이 아니다. Executor의 실제 Tool 경계에 Task Contract를 연결하고,
성공 조건·산출물·검증 Evidence가 충족되지 않으면 완료 상태가 될 수 없도록 실행 의미를 바꿨다.
Supervisor는 시스템의 현재 RAM/VRAM과 실행 중인 전문가 수를 기준으로 admission을 결정하며,
사용자 취소 요청은 메모리 플래그가 아니라 계약 저장소에 남는다.

전문가 작업공간은 이름만 다른 대화창이 아니라 공통 Planner, 실제 Executor, Reviewer와 namespace별
RAG를 사용한다. RTX 4060 Laptop 8GB 조건에서는 복수 무거운 모델을 동시 상주시킨다는 비현실적인
완료 표기를 하지 않고 역할별 순차 실행과 모델 해제를 기본으로 한다.

지속 워크플로는 단계·산출물·승인 경계를 저장한다. Morning Brief는 실제 시각, Open-Meteo,
연결된 Calendar, 대화 작업과 런타임 상태를 수집하며, 선택형 공급자가 미연결이면 그 사실을 결과에
남긴다. 비밀값은 저장 전 마스킹한다.

Command Center는 작업 계약, 팀, DAG, 모델/자원, Observer, 승인, 권한/Plugin, Artifact/Evidence,
진단, 품질, 이벤트를 11개 탭으로 노출한다. 작업 취소·워크플로 실행·진단 실행은 해당 런타임에
실제로 연결되어 있다. 첫 실행 마법사는 실패한 검사를 숨기지 않고 재실행 또는 Command Center
열기를 제공한다.

실제 수락 결과:

- `.venv` Python 3.12로 `main_qt.py` 기동 성공
- Ollama 9개 모델 확인, 필수 모델 누락 없음
- RTX 4060 Laptop에서 실제 CUDA 텐서 연산 성공
- Scheduler 1,000회 soak와 Tool 왕복 실패 없음
- MediaPipe 0.10.35 HandLandmarker 모델 생성·종료 성공(카메라는 동의 없이 열지 않음)
- `444 passed, 4 deselected`, `pip check` 충돌 0개

정직하게 남은 위험은 세 가지다. 외부 OAuth/SMTP 계정은 자격증명이 없어 E2E 미수락이고, 실제
카메라·스피커 출력은 사용자의 라이브 동의가 필요하며, 임의 Tool 프로세스를 OS 수준에서 강제
종료하는 격리는 아직 범용 샌드박스가 아니다. 현재 Supervisor는 실행 전 admission과 단계 사이
취소, 반환 후 시간 초과 판정을 보장한다. 장기적으로는 위험 Tool을 별도 worker process로 옮겨
hard timeout과 메모리 제한을 적용하는 것이 다음 강화 지점이다.

## 2026-08-21 수행 범위·자체 권한 확장 검토

수행 범위가 좁았던 핵심 원인은 Plugin 수가 아니라 선언형 Intent를 찾지 못한 모든 요청을 Planner
전에 일반 대화로 종료한 실행 경계였다. Registry Tool 설명 기반의 제한된 loadout을 추가해 새
Capability가 중앙 하드코딩 없이 자연어 요청 후보가 되도록 변경했다. 사회적 대화와 Registry
근거가 약한 요청은 여전히 Tool 경로에 들어가지 않는다.

브라우저 플러그인에는 공급자 카탈로그 기반 사이트 검색, URL 열기, `yt-dlp` 기반 YouTube 첫
검색 결과 재생 페이지 열기를 추가했다. 실제 공개 YouTube 검색으로 동영상 URL과 autoplay
파라미터를 확인했다. 다만 기본 브라우저에 URL을 전달한 사실만 검증하며 브라우저 정책이 막을 수
있는 오디오 재생까지 성공했다고 표시하지 않는다.

자체 분석과 변경은 별도 `agent_self` 플러그인으로 구현했다. 상태 보고는 실제 저장소 dirty 상태,
Plugin 계약/의존성/인증, Permission, RAM, CUDA GPU와 Ollama 모델을 조회한다. 자체 변경은
`self_modify` 영구 정책 UI에 자동 노출되며 매번 중앙 Permission Manager를 통과한다. Coding Agent는
경로 관련도를 점수화해 문서 내용보다 실제 UI/소스 경로를 우선하고 최소 patch·테스트·diff·rollback을
적용한다. 실행 중인 프로세스를 즉시 hot reload하거나 권한·보안·비밀 영역을 스스로 바꾸는 권한은
부여하지 않았다.

검증은 집중 43개, 전체 `450 passed, 4 deselected`, Python compile 성공이다. 실환경에서 등록
31개/검증 29개/기술 결함 0개, 선택형 계정 미설정 2개, CUDA RTX 4060, Ollama 9개 모델과 새
Registry Tool 7개 노출을 확인했다.
