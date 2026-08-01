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
Plugin은 하위 호환되지만 개별 Tool의 부작용·출력 스키마를 명시적으로 채우는 이전과
낮은 신뢰도 확인 질문 정책은 남아 있습니다.

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
4. Workspace 자동 복원·증분 Project Indexer.
5. 실제 Ollama, GUI, 마이크·카메라·TTS·Scheduler 장시간 종단 검증.
6. mail/calendar OAuth 토큰 저장·권한·계정 선택 정책.
