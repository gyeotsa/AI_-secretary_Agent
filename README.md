# JARVIS — 로컬 개인 AI 에이전트

JARVIS는 Windows PC에서 대화, 파일·문서 작업, 웹 조사, 앱 제어, 음성 입출력과
자동화를 하나의 로컬 에이전트로 연결하는 프로젝트입니다. 최종 목표는 단순한
질문 답변기가 아니라 계획하고, 실제로 실행하고, 결과를 검증하고, 실패하면
복구하는 개인 AI 운영 계층입니다.

## 현재 상태

현재 버전은 “기능이 연결된 AI 비서”에서 “증거 기반 Agent Runtime”으로 전환하는
단계입니다. 다음 기능은 실제 코드와 자동 테스트가 있습니다.

- PyQt6 대화 UI, 세션·권한·Workspace·TTS 설정
- Ollama 역할별 로컬 모델 라우팅
- 대화·작업 Slot 상태와 후속 질문
- Plugin Registry 기반 Tool·Intent 로딩
- 파일·프로젝트 생성, 파일 내용 수정과 실제 경로·해시 검증
- Excel, Word, PowerPoint, PDF, HWPX 파일 처리
- Git, Windows 앱 탐색·실행·창 제어
- Playwright 페이지 조회와 DDGS 웹 검색
- 출처 URL이 있는 최신 정보 검색과 간결한 답변
- Open-Meteo 실시간 날씨, 로컬 날짜·시간
- ChromaDB와 BGE-M3 기반 RAG
- faster-whisper STT, Windows·Edge·GPT-SoVITS TTS
- 단발 알람, Scheduler, Observer와 선제 알림 기반
- Gemma 3 기반 이미지 분석
- 영구 권한 정책과 실행 감사 기반

다음 항목은 파일이나 클래스가 존재해도 Codex 수준의 완성 기능으로 보지 않습니다.

- 장기 작업을 끝까지 수행하는 공통 Task Orchestrator
- 저장소 전체를 이해하는 Coding Agent
- 모든 Tool에 적용되는 타입 기반 결과·증거 계약
- Planner → 실행 → 검증 → 복구 → 재계획의 안정적인 폐쇄 루프
- Workspace 자동 복원·증분 인덱싱·프로젝트별 메모리
- Gmail, Outlook, Calendar, Drive 등의 실제 OAuth 연결
- 장시간 자동화와 실제 장치의 제품 수준 E2E 검증

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
제외합니다. 마이크, 카메라, 브라우저, OAuth, Office COM, 장시간 Scheduler는 별도
실환경 수락 테스트가 필요합니다.

## 핵심 개발 원칙

1. Tool을 실행하지 않은 응답은 외부 작업이 완료됐다고 주장하지 않습니다.
2. 성공은 문자열이 아니라 실제 결과물과 검증 증거로 판정합니다.
3. Tool과 Intent의 진실 공급원은 Plugin Registry로 통일합니다.
4. 읽기·쓰기·삭제·외부 전송 권한을 분리하고 예외 시 거부합니다.
5. 사용자 파일과 dirty Git 변경을 임의로 덮어쓰거나 삭제하지 않습니다.
6. 화면 응답, TTS 문장, 개발자 로그를 분리합니다.
7. 기능 존재와 실제 E2E 완료를 구분해 문서에 표시합니다.

## 문서

- [AGENT_RUNTIME_ROADMAP.md](AGENT_RUNTIME_ROADMAP.md): Codex급 에이전트로 가기 위한 통합 개선 계획
- [PROJECT_REVIEW.md](PROJECT_REVIEW.md): 현재 구현의 검토 결과와 기술 부채
- [PLUGIN_ROADMAP.md](PLUGIN_ROADMAP.md): Plugin 연결 현황과 확장 순서
- [MODEL_ROUTING.md](MODEL_ROUTING.md): 역할별 모델과 GPU 정책
- [DEPLOYMENT.md](DEPLOYMENT.md): Windows 설치본과 배포 제한
- [Level2 리팩토링 10단계.txt](Level2%20리팩토링%2010단계.txt): 2026-07-22 기반 리팩터링 기록
- [Agent 인수인계.txt](Agent%20인수인계.txt): 작업 이력과 필수 운영 규칙
