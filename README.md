# JARVIS — 로컬 개인 AI 에이전트

JARVIS는 Windows PC에서 대화, 파일·문서 작업, 웹 조사, 앱 제어, 음성 입출력과
자동화를 하나의 로컬 에이전트로 연결하는 프로젝트입니다. 최종 목표는 단순한
질문 답변기가 아니라 계획하고, 실제로 실행하고, 결과를 검증하고, 실패하면
복구하는 개인 AI 운영 계층입니다.

## 화면 테마와 창 크기 조절

Chrome에서 저장한 계정으로 웹사이트에 로그인하려면 [Chrome 로그인 안내](BROWSER_LOGIN.md)를 참고하세요.

질문에 맞는 공개 자료 수집과 출처 기반 분석은 [정보 조사 안내](RESEARCH.md)를 참고하세요.

화면을 직접 조작하는 브라우저·Windows 작업은 [Computer Use 사용 안내](COMPUTER_USE.md)를 참고하세요.

메인 화면 오른쪽 위의 **라이트 모드 / 다크 모드** 버튼으로 전체 앱의 테마를
전환합니다. 설정·권한·음성·대화·작업·메일·리마인더·지식 그래프·
Command Center·첫 실행 안내 창에 함께 적용되며, 열린 창과 작성 중인 내용은 유지됩니다.
선택은 Windows 사용자 설정(`JARVIS/ANIS`, `appearance/theme`)에 저장되어 다음 실행에도
복원됩니다.

메인 창의 네 가장자리와 네 모서리를 드래그해 크기를 조절할 수 있습니다.
최대화·미니 모드에서는 크기 조절이 비활성화되고 일반 창으로 돌아오면 다시 활성화됩니다.
보조 창은 Windows 기본 테두리로 크기를 조절합니다.

## 2026-08-27 통합 실행 수락 상태

이번 수락 주기에서는 기능 수를 늘리는 것보다 **요청이 어느 세션·작업·Workspace에
속하는지**, **실제로 어떤 도구가 실행됐는지**, **결과를 무엇으로 검증했는지**가 끝까지
보존되는지를 우선했습니다.

- 고정 호칭·문장 길이·응답 템플릿을 강제하던 System Prompt를 런타임 사용자 설정과
  대화 목적에 따라 구성하도록 변경했습니다. 짧은 질문은 간결하게, 분석 요청은 필요한
  근거와 설명을 생략하지 않고 답합니다.
- 각 요청은 `TurnEnvelope`로 세션, Task, Workspace, 원문, 라우팅 근거를 고정합니다.
  비동기 응답 순서가 바뀌어도 다른 세션이나 이전 요청의 문맥으로 결과를 기록하지 않습니다.
- Planner는 한 단계에 하나의 Tool만 배정하고, 유효한 의존성 DAG와 인용된 사용자 원문을
  보존합니다. Executor·전문가 팀은 인스턴스별 상태와 실행 잠금을 사용합니다.
- 공급자 timeout·연결 실패·빈 응답은 `ModelCallError`로 전달됩니다. 오류 문자열을 정상
  모델 답변이나 작업 성공으로 학습·표시하지 않습니다.
- Plugin 실행에는 execution ID, 취소, idempotency와 부작용 정책이 적용됩니다. 읽기 전용
  Tool만 안전 조건에서 재시도하고 파일 변경·외부 전송 같은 부작용 Tool은 자동 재시도하지
  않습니다.
- Plugin 상태는 등록, 의존성 설치, 연결, 인증, 계약 검증, 런타임 준비, E2E 검증을 서로
  다른 사실로 표시합니다. 등록 로그만으로 연결 완료라고 보고하지 않습니다.
- 전문가 팀은 계획·역할·Tool 범위·Evidence·Artifact 계약을 검사합니다. 실제 Word 생성
  수락에서는 Plugin을 통해 DOCX를 만들고 다시 열어 제목과 본문이 정확히 저장됐음을
  확인했습니다.

자동 검증 결과는 Python 3.12 프로젝트 가상환경에서 `615 passed, 4 deselected`이며,
`compileall`, `pip check`, `git diff --check`를 통과했습니다. 마이크 자동 시작과
최초 설정 마법사를 끈 Qt offscreen 환경에서 실제 `JarvisApp`을 생성하고 이벤트 루프까지
정상 종료했습니다. 끊어진 Python 3.10 전용 가상환경 때문에 발생하던 GPT-SoVITS Anis
상태 코드 101은 로컬 Python 3.10.11과 전용 환경을 복구해 해결했고, 실제 모델 준비
17.15초와 준비 후 5.64초 음성 합성 8.87초를 확인했습니다. TTS 설정 창은 준비 중·완료·실패를
표시하고, 실패 로그는 `data/logs/tts`에 남으며 앱 종료 시 소유한 합성 서버를 종료합니다.
이 검증은 물리 스피커에서의 주관적 음질, OAuth 계정,
외부 메시지의 서버 전달, Office COM을 대신하지 않습니다. 다른 PC 배포본은 Python 3.10과
GPT-SoVITS 런타임을 첫 실행 부트스트랩 또는 휴대형 런타임으로 별도 보장해야 합니다.

## 현재 상태

통합 리마인더는 아니스와 연결한 Codex/Claude Code의 로컬 기록을 증분 수집해
계획 단계, 다음 할 일, 완료 근거와 마감 알림을 조회합니다. 앱 시작 직후와 실행 중 1시간마다
자동 수집하며, 진행 근거는 설명·출처·시각으로 정리해 보여줍니다. 사이드바의 작업 및 리마인더 메뉴에서
열 수 있으며 연결 방법과 수집 범위는 [통합 리마인더 안내](CONTINUITY_REMINDER.md)를 참고하세요.

메인 화면은 대화·프로젝트·플러그인을 분리한 탭/사이드바 방식입니다.
대화는 프로젝트 폴더별로 표시하며 폴더를 클릭해 접고 펼칠 수 있습니다. 대화를 우클릭하면
이름 변경·고정/고정 해제·전체 대화 복사·영구 삭제를 사용할 수 있습니다. 이름과 고정은 재실행 후에도
유지됩니다. 영구 삭제는 대화 원문과 원문 보관 사본을 정리하며, 이미 추출된 지식과 별도 작업 기록은 유지합니다.
프로젝트 폴더를 우클릭하면 해당 폴더에 새 채팅을 만들거나 프로젝트 목록에서 폴더를 제거할 수 있습니다.
폴더 제거 시 실제 폴더와 파일은 보존하며 기존 대화는 일반 대화로 옮깁니다.
권한 요청과 추가 질문은 알림음과 함께 화면 오른쪽 아래에 표시됩니다. 허용·거절이나 제시된
선택지를 클릭해 응답할 수 있고, 자유 입력이 필요하면 직접 답변을 사용할 수 있습니다.
닫기는 이번 요청만 취소하며, 취소되거나 다른 대화로 바뀐 작업의 이전 알림은 실행되지 않습니다.
테마 버튼 왼쪽의 마이크 ON/OFF는 음성 입력만 제어합니다. 실제 입력 장치 연결 후 ON으로 표시하며,
OFF 시 연결 대기와 뒤늦은 음성 인식 결과도 취소합니다.
설정 및 도구에서 권한·음성·지식 그래프를 열 수 있습니다. 플러그인 화면의
Google·Microsoft 계정 연결은 PKCE 로그인과 DPAPI 저장을 사용하며, 실제 계정의
동의·갱신·업무 수락은 별도로 확인해야 합니다. [클라우드 지원 범위](CLOUD_INTEGRATION.md),
[MCP 연결·다운로드 경계](MCP_INTEGRATION.md), [전 영역 완료 원장](QA_COMPLETION_LEDGER.md)을
기준으로 구현·자동 검사·실사용 수락을 구분합니다. 앞의 8월 수치는 당시 기록이며 최신
작업 트리의 검사 결과는 [공통 런타임 QA](QA_COMMON_RUNTIME.md)에 있습니다.

현재 버전은 증거 기반 Agent Runtime의 핵심 실행 계약을 자동 회귀로 수락했고,
외부 장치·계정·앱별 실환경 수락을 남겨 둔 단계입니다. 다음 기능은 실제 코드와
자동 테스트가 있습니다.

- PyQt6 대화 UI와 세션·권한·프로젝트 폴더·TTS 설정
- Ollama 역할별 로컬 모델 라우팅
- 대화·작업 Slot 상태와 후속 질문
- Plugin Registry 기반 Tool·Intent 로딩
- 명시 Intent가 없어도 Registry 설명과 요청 문맥이 일치하면 해당 Tool만 노출하는 범용 실행 경로
- Plugin 입출력 JSON Schema 검증, timeout·재시도·취소 정책, 상태·인증 진단 UI
- 유형·출처·정정·민감정보 정책을 갖춘 장기 Memory와 근거 인용형 RAG
- 대화에서 명시적 선호·프로젝트 결정·정정을 추출해 Knowledge Memory와 Vector RAG에 자동 축적
- 실제 페이지 방문·교차검증·주장별 인용·Prompt Injection 격리를 갖춘 Research Agent
- 파일·프로젝트 생성, 파일 내용 수정과 실제 경로·해시 검증
- Excel, Word, PowerPoint, PDF, HWPX 파일 처리
- Photoshop 도구와 Windows COM 연결 진단
- Git, Windows 앱 탐색·실행·창 제어
- Playwright 페이지 조회와 DDGS 웹 검색, 공급자 카탈로그 기반 사이트 검색
- YouTube 검색 결과 해석과 기본 브라우저 재생 페이지 열기(실제 소리 재생 여부는 브라우저 정책에 따름)
- Yahoo Finance 실제 거래 데이터 기반 종목·시장 분석(수익률·이동평균·변동성·최대낙폭·거래량·밸류에이션)
- 카카오톡 대화방 목록과 메시지 읽기
- 출처 URL이 있는 최신 정보 검색과 간결한 답변
- Open-Meteo 실시간 날씨, 로컬 날짜·시간
- ChromaDB와 BGE-M3 기반 RAG
- faster-whisper STT, Windows·Edge·GPT-SoVITS TTS
- 단발 알람, Scheduler, Observer와 선제 알림 기반
- Gemma 3 기반 이미지 분석
- 화면 영역·다중 이미지·영상 프레임 분석과 Windows UI Automation
- STT·Vision 중앙 GPU 큐, TTS 에코 제거·음성 끼어들기·장치 자동 복구
- 집중·회의·전체화면·방해 금지 기반 선제 알림 보류·digest와 Scheduler 복원
- 영구 권한 정책과 실행 감사 기반
- Plugin Registry 기반 자신의 역량 조회와 실제 Python·GPU·Ollama·Plugin·권한·Git 상태 진단
- 별도 `self_modify` 승인, 보호 영역 차단, 최소 patch·테스트·diff·rollback을 갖춘 자체 코드·UI 수정

다음 항목은 코드 경로가 있어도 현재 PC의 외부 조건까지 충족해야 제품 수준 완료로 봅니다.

- Gmail, Outlook, Calendar, Drive 등의 실제 OAuth 연결
- 장시간 자동화와 실제 장치의 제품 수준 E2E 검증
- 브라우저 자동재생 정책을 포함한 사이트별 로그인·재생·구매·게시 E2E
- 위험 Tool을 별도 worker process에 격리한 OS 수준 hard timeout·메모리 제한

상세 개선 계획은 [AGENT_RUNTIME_ROADMAP.md](AGENT_RUNTIME_ROADMAP.md), 플러그인
현황은 [PLUGIN_ROADMAP.md](PLUGIN_ROADMAP.md), 모델 구성은
[MODEL_ROUTING.md](MODEL_ROUTING.md)를 확인하세요.

## 실행 환경

- Windows 10/11
- Python 3.12
- NVIDIA GPU 권장(현재 RTX 4060 Laptop 8GB 검증)
- Ollama
- 인터넷 기능을 사용할 경우 네트워크 연결

## 개발 실행

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-cuda.txt
.\.venv\Scripts\python.exe main_qt.py
```

기본 LLM 공급자는 Ollama입니다. Anthropic 경로는 선택적 하이브리드 확장을 위해
존재하며 API 키 없이도 로컬 경로가 동작합니다. `.env.example`을 참고해 `.env`를
구성하되 토큰이나 비밀번호를 Git에 커밋하지 마세요.

전문가 작업공간 목록과 전용 작업창은 제거했습니다. 문서·개발·리서치·Photoshop 작업은
메인 대화의 기존 도구를 통해 사용할 수 있습니다. 지식 그래프는 **설정 및 도구 → 지식 그래프**에서
열 수 있으며 기존 기억 데이터와 그래프 탐색 기능을 유지합니다.
시안 제작·이미지 생성 기능과 전용 모델은 제거했습니다. 화면 인식·음성 인식·RAG 모델은 유지합니다.

웹 실행은 공급자 설정 파일에서 확장됩니다. 예를 들어 `유튜브에서 고양이 영상을 검색해줘`는
YouTube 검색 결과 페이지를 열고, `아이유 좋은날 틀어줘`는 `yt-dlp`로 첫 결과 URL을 확인한 뒤
기본 브라우저에 자동재생 URL을 전달합니다. URL 전달 성공과 실제 페이지 로드·소리 재생 성공은
구분해 기록하므로 브라우저가 자동재생을 막았을 때 재생 완료라고 거짓 보고하지 않습니다.

`삼성전자 주식 분석해줄래?` 같은 요청은 브라우저 검색으로 보내지 않고 `finance` Plugin이
회사 별칭을 실제 종목 코드로 해석한 뒤 Yahoo Finance의 일별 거래 데이터를 조회합니다. 최근
수익률, 20·60일 이동평균, 연환산 변동성, 최대 낙폭, 기간 내 가격 위치, 거래량과 제공 가능한
밸류에이션을 계산해 출처·조회 시각과 함께 답합니다. 이 결과는 과거 데이터 기반 정량 분석이며
기업 공시·뉴스의 정성 분석이나 매수·매도 추천으로 표시하지 않습니다.

카카오톡은 대화방 목록과 메시지 읽기를 지원합니다. 카카오톡 메시지 전송 기능은 제거했습니다.

승인 대기 작업이 하나뿐이면 사용자는 내부 작업 번호를 읽지 않고 `승인`이라고만 답해도 됩니다.
대기 작업이 여러 개면 시스템은 임의로 고르지 않고 작업 번호를 요청합니다. 승인 발화 자체는 일반
대화 LLM에 전달하지 않으며, 실제 Tool 실행 증거가 생긴 뒤에만 실행 결과를 표시합니다.

`너의 현재 상태를 분석해줘`, `어떤 작업을 할 수 있어?`로 Anis 자신의 실제 런타임과 Registry
역량을 확인할 수 있습니다. `너의 UI를 ... 수정해줘` 같은 자체 변경은 일반 Workspace 코딩과
분리된 프로젝트 경계에서 실행되며 `self_modify`, 파일 읽기·쓰기, 명령 실행 권한을 모두 요구합니다.
`.git`, 사용자 데이터·기억·모델·배포물·임시 폴더와 비밀정보·권한 우회 요청은 수정 대상에서
제외됩니다. 변경은 최소 patch 후 관련 테스트를 통과해야 유지되고 실패하면 원복됩니다. 실행 중인
UI의 코드가 바뀐 경우 앱 재시작 뒤 새 코드가 적용됩니다.

대화 전체를 무조건 학습하지는 않습니다. `앞으로`, `항상`, `선호`, `기억해`,
`우리 프로젝트는`처럼 장기성이 명시된 사용자 발화만 기억 후보로 분류하며, 신뢰도·중복·
정정·민감정보 검사를 통과한 내용만 Knowledge Memory와 RAG에 함께 저장합니다. 다음 요청은
현재 Workspace와 global 기억에서 관련 항목만 검색합니다. 잡담과 일회성 명령은 축적하지
않고, 저장된 사용자 설정이 현재 발화와 충돌하면 현재 발화를 우선합니다.
대화 중에는 경량 이벤트만 저장하고 장기 기억 추출·임베딩은 누적 임계치, 15분 유휴 시간,
세션 전환 때 배치 처리합니다. 승인된 작업 결과와 여러 세션에서 반복된 행동만 별도로
승격하며, Obsidian 문서는 내용 해시가 바뀐 파일만 증분·역동기화합니다. RAG는 검색된
문서와 실제 프롬프트에 포함된 문서를 구분해 사용량을 추적합니다.

## 주요 디렉터리

```text
AI_-secretary_Agent/
├─ main_qt.py                 # GUI 진입점
├─ config.py                  # 설정
├─ core/
│  ├─ executor.py             # 현행 실행 오케스트레이션
│  ├─ intent_router.py        # Plugin Intent/Slot 라우팅
│  ├─ dialogue_state.py       # 대기 작업과 후속 대화 상태
│  ├─ plugin.py               # Plugin SDK와 Registry
│  ├─ verifier.py             # Tool별 결과 검증
│  ├─ recovery.py             # 실패 복구
│  ├─ scheduler.py            # 예약·알람 실행
│  ├─ rag.py                  # 지식 검색
│  └─ runtime/                # Event Bus와 Action Journal
├─ plugins/                   # 실제 실행 Capability
├─ ui/                        # PyQt6 UI
├─ data/                      # 사용자·런타임 데이터
├─ scripts/                   # 학습·배포 보조 스크립트
└─ packaging/                 # Windows 배포 구성
```

## 테스트

안전한 기본 회귀 테스트:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

현재 기본 모음은 네트워크·실제 장치·외부 계정이 필요한 integration 테스트를
제외합니다. 마이크, 브라우저, OAuth, Office COM, 장시간 Scheduler는 별도
실환경 수락 테스트가 필요합니다.

P9부터 Office 문서는 기존 패키지 서식을 보존하는 원자적 치환과 PDF 렌더링 QA를
지원합니다. Google/Microsoft OAuth 토큰은 Windows DPAPI로 암호화하며 Gmail,
Calendar, Outlook, Slack, Teams의 외부 반영은 반드시 로컬 초안 생성과 권한 승인 뒤
실행됩니다. Drive·OneDrive·Notion은 원격 ID 기반 카탈로그로 동기화됩니다. 실제 계정
연결에 필요한 값은 `.env.example`을 참고하되 비밀값은 `.env`에만 저장해야 합니다.

P10의 Windows 제어는 API·CLI·COM·UI Automation을 우선하며 좌표 클릭은 별도 승인
fallback입니다. 화면·Vision Tool은 캡처 권한을 요구하고, STT와 Vision은 공통 VRAM
예산을 사용합니다. `GPU_VRAM_BUDGET_MB`를 비워 두면 감지된 CUDA VRAM의 82%를
사용합니다.

P11 선제 알림은 중요도와 발생 이유를 원장에 기록합니다. 집중·회의·전체화면·방해 금지
중에는 critical 보안 알림을 제외하고 보류하며, 상태가 해제되면 묶어서 알려줍니다.
자비스가 먼저 제안한 작업은 승인하더라도 즉시 실행되지 않으며 실제 Tool 권한을 별도로
통과해야 합니다. Scheduler는 heartbeat와 활성 작업을 DB에 저장해 시작·절전 복귀 시
다시 구성합니다.

## 핵심 개발 원칙

1. Tool을 실행하지 않은 응답은 외부 작업이 완료됐다고 주장하지 않습니다.
2. 성공은 문자열이 아니라 실제 결과물과 검증 증거로 판정합니다.
3. Tool과 Intent의 진실 공급원은 Plugin Registry로 통일합니다.
4. 읽기·쓰기·삭제·외부 전송 권한을 분리하고 예외 시 거부합니다.
5. 사용자 파일과 dirty Git 변경을 임의로 덮어쓰거나 삭제하지 않습니다.
6. 화면 응답, TTS 문장, 개발자 로그를 분리합니다.
7. 기능 존재와 실제 E2E 완료를 구분해 문서에 표시합니다.

## 평가·학습 준비 Runtime

모든 대화 턴은 개인정보를 마스킹한 trajectory로 기록되며 라우팅, 제한된 Tool Loadout,
도구 실행 결과와 검증 상태가 하나의 실행 ID로 연결됩니다. 사용자 피드백은 즉시 모델을
바꾸지 않고 검토 대기 데이터로 저장됩니다. `python scripts/export_post_training_data.py`로
SFT·DPO·Verifier-RL 후보를 내보낼 수 있지만 자동 학습과 자동 배포는 금지되어 있습니다.

RAG는 BM25 성격의 어휘 검색과 벡터 검색을 독립 수행한 뒤 RRF로 병합하고, 선택적으로
Cross-Encoder로 재정렬합니다. 신뢰도가 기준보다 낮으면 문서 내용을 사실로 단정하지
않고 확인 또는 실시간 검색으로 전환합니다.

## 문서

- [AGENT_RUNTIME_ROADMAP.md](AGENT_RUNTIME_ROADMAP.md): Codex급 에이전트로 가기 위한 통합 개선 계획
- [PROJECT_REVIEW.md](PROJECT_REVIEW.md): 현재 구현의 검토 결과와 기술 부채
- [PLUGIN_ROADMAP.md](PLUGIN_ROADMAP.md): Plugin 연결 현황과 확장 순서
- [MODEL_ROUTING.md](MODEL_ROUTING.md): 역할별 모델과 GPU 정책
- [POST_TRAINING.md](POST_TRAINING.md): 평가 데이터, QLoRA·DPO·Verifier-RL 진입 기준
- [OBSIDIAN_KNOWLEDGE.md](OBSIDIAN_KNOWLEDGE.md): Obsidian Vault 기억 계층과 링크·RAG 구조
- Knowledge Graph: 설정 및 도구의 지식 그래프 버튼으로 Vault 연결 구조를 시각적으로 탐색
- Knowledge Graph는 노드 반발력·링크 장력 기반으로 움직이며 드래그, 관계 강조, 확대·이동, 일시정지와 재배치를 지원
- [DEPLOYMENT.md](DEPLOYMENT.md): Windows 설치본과 배포 제한
- [Level2 리팩토링 10단계.txt](Level2%20리팩토링%2010단계.txt): 2026-07-22 기반 리팩터링 기록
- [Agent 인수인계.txt](Agent%20인수인계.txt): 작업 이력과 필수 운영 규칙
## 통합 에이전트 런타임과 Command Center (2026-08-21)

Anis의 일반 채팅과 전문 도구 실행은 동일한 실행 계약을 사용합니다. 실제 작업은
`TaskContract`에 목표, 입력, 성공 조건, 허용 Tool, 산출물, 증거와 실패 사유를 기록하며,
Supervisor가 RAM·VRAM·동시 실행 수·실행 시간을 검사합니다. Tool 실행과 산출물 검증이 없는
작업은 완료로 승격되지 않습니다. 실행 중 취소 요청도 계약에 영속화되어 다음 단계 진입 전에
중단됩니다.

메인 창과 `Command Center`의 `BRAIN MAP`에서는 실제 화면으로 연결되는 2.5D 뇌·에이전트
궤도를 제공합니다. 시스템 화면은 원근감이 있는 타원 궤도에 배치되며 클릭,
마우스 드래그와 휠로 탐색할 수 있습니다. 각 에이전트는 고정된 모양과 전문 색상을
사용하고 실행 상태만 맥동·연결 흐름으로 표현해 역할과 상태가 섞이지 않게 했습니다.

`Command Center`에서는 `BRAIN MAP`과 다음 11개 운영 영역을 한 곳에서 확인할 수 있습니다.

- 작업/계약, 전문가 팀, Plan DAG, 모델/자원
- 워크플로/Observer, 승인/행동, 권한/플러그인
- 산출물/증거, 진단, 품질, 이벤트

`config/workflows.json`에는 Morning Brief를 포함한 재개 가능한 워크플로가 있으며 승인 경계의
정확한 단계와 안전하게 마스킹된 상태를 저장합니다. Morning Brief는 시스템 시각, Open-Meteo,
연결된 Google/Microsoft Calendar, 대화 작업, 승인 대기, 플러그인과 Scheduler 상태만 사용합니다.
연결되지 않은 계정의 일정이나 날씨를 추측해 만들지 않습니다.

첫 실행 진단은 프로젝트 Python, Ollama와 필수 모델, 실제 CUDA 텐서 연산, RAM/VRAM, 작업공간,
Plugin Registry, Tool 왕복, Scheduler soak를 검사합니다. 마이크·스피커의 실제 입출력은
사용자가 라이브 진단을 선택했을 때만 실행합니다.

2026-08-21 수락 결과는 다음과 같습니다.

- 프로젝트 `.venv` Python 3.12에서 `main_qt.py` 실제 기동 성공
- Ollama 필수 모델 0개 누락, RTX 4060 Laptop에서 CUDA 텐서 연산 성공
- Scheduler 1,000회 soak 실패 0건, Tool 흐름과 마이크/출력 장치 열거 성공
- 전체 안전 회귀 테스트 `454 passed, 4 deselected`
- `compileall`, `pip check`, `git diff --check` 통과

현재 미수락 항목은 구현 누락이 아니라 외부 조건입니다. 선택된 작업공간이 없으면 진단 경고가
남고, Google/Microsoft/Slack/Notion/SMTP 자격증명이 없으면 해당 연결은 준비되지 않은 상태로
표시됩니다. 실제 TTS 재생은 장치·소음 영향을 주므로 사용자 수락 세션에서 최종 확인해야 합니다.
