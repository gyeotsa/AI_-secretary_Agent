# JARVIS — 로컬 개인 AI 에이전트

JARVIS는 Windows PC에서 대화, 파일·문서 작업, 웹 조사, 앱 제어, 음성 입출력과
자동화를 하나의 로컬 에이전트로 연결하는 프로젝트입니다. 최종 목표는 단순한
질문 답변기가 아니라 계획하고, 실제로 실행하고, 결과를 검증하고, 실패하면
복구하는 개인 AI 운영 계층입니다.

## 현재 상태

현재 버전은 “기능이 연결된 AI 비서”에서 “증거 기반 Agent Runtime”으로 전환하는
단계입니다. 다음 기능은 실제 코드와 자동 테스트가 있습니다.

- PyQt6 대화 UI, 세션·권한·Workspace·TTS 설정과 역할별 전문가 작업공간
- Ollama 역할별 로컬 모델 라우팅
- 대화·작업 Slot 상태와 후속 질문
- Plugin Registry 기반 Tool·Intent 로딩
- Plugin 입출력 JSON Schema 검증, timeout·재시도·취소 정책, 상태·인증 진단 UI
- 유형·출처·정정·민감정보 정책을 갖춘 장기 Memory와 근거 인용형 RAG
- 대화에서 명시적 선호·프로젝트 결정·정정을 추출해 Knowledge Memory와 Vector RAG에 자동 축적
- 실제 페이지 방문·교차검증·주장별 인용·Prompt Injection 격리를 갖춘 Research Agent
- 파일·프로젝트 생성, 파일 내용 수정과 실제 경로·해시 검증
- Excel, Word, PowerPoint, PDF, HWPX 파일 처리
- 문서 전문가 작업공간과 Photoshop 전문가 작업공간·Windows COM 연결 진단
- 학습용 참고 시안과 제작용 사진을 분리한 시안 제작 전문가 작업공간
- Git, Windows 앱 탐색·실행·창 제어
- Playwright 페이지 조회와 DDGS 웹 검색
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

다음 항목은 파일이나 클래스가 존재해도 Codex 수준의 완성 기능으로 보지 않습니다.

- 장기 작업을 끝까지 수행하는 공통 Task Orchestrator
- 저장소 전체를 이해하는 Coding Agent
- 모든 Tool에 적용되는 타입 기반 결과·증거 계약
- Planner → 실행 → 검증 → 복구 → 재계획의 안정적인 폐쇄 루프
- Workspace 자동 복원·목록·별칭·프로젝트별 설정
- `.gitignore` 기반 초기/증분 인덱싱, 언어·프레임워크·테스트 명령 감지
- 프로젝트별 Memory/RAG 격리와 Git 브랜치·dirty UI
- 새 프로젝트 템플릿·가상환경·Git 초기화
- 실행 가능한 Plan DAG와 독립 단계 병렬 실행
- 오류 서명·재시도 예산·관찰 기반 재계획·사람 승인 체크포인트
- Conversation·Planning·Execution·Response 서비스 책임 분리
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

상단의 `S` 버튼에서 전문가 작업공간을 선택할 수 있습니다. 채팅이나 음성으로
`문서 작업모드 실행해줘`, `포토샵 전문가 작업공간 열어줘`라고 말해도 같은 창이
열립니다. 문서 공간은 Office 파일·직접 텍스트 편집·검증 결과를 함께 표시하고,
Photoshop 공간은 이미지 미리보기와 설치된 Photoshop의 COM 연결 상태 및 문서 열기를
제공합니다. Photoshop이 설치되지 않은 PC에서는 연결 실패를 명시하며 다른 작업공간과
메인 대화는 계속 동작합니다.

`시안 제작 전문가 작업공간 열어줘`로 Photoshop과 독립된 시안 제작 창을 열 수 있습니다.
학습용 영역에는 완성된 참고 시안 여러 장을, 제작용 영역에는 새 결과에 사용할 원본 사진을
각각 추가합니다. 학습 단계는 이미지 해시, 대표 색상, 화면 비율, 반복 기하 구조와 Vision 보조 분석을
재사용 가능한 프로필로 저장합니다. 제작 단계는 선택한 프로필과 제작용 사진만 사용해 PNG를
만들고 같은 이름의 JSON에 입력·스타일 provenance를 기록합니다. 제작 방식은 `자동`,
`생성형(SD1.5 + IP-Adapter Plus)`, `빠른 로컬 합성` 중 선택합니다. 생성형 모델은 최초 한 번
`생성형 모델 준비` 버튼으로 앱 전용 폴더에 내려받으며, 이후에는 네트워크 없이 실행됩니다.
생성형 경로는 참고 시안으로 스타일 배경을 만들고 제작용 원본 사진을 그 위에 별도로 합성해
제품·인물 사진의 픽셀을 임의로 다시 그리지 않습니다. 모델이 없거나 CUDA를 쓸 수 없으면
자동 모드는 Photoshop/CorelDRAW가 필요 없는 Pillow 합성기로 안전하게 돌아갑니다.
원형 스티커처럼 반복 구조가 명확한 스타일은 생성형 모델보다 구조 렌더러를 우선해 큰 중앙 원,
사진 마스크, 점선 링과 문구 영역을 그대로 재현합니다. 제작 지시는 화면에 출력되지 않으며 별도의
`표시할 문구` 입력란에 쓴 내용만 이미지에 들어갑니다.
AI 수정도 완성 이미지를 재생성하지 않고 원본 제작 사진에서 다시 렌더링합니다. 네 가지 보정
슬라이더는 50%를 원본으로 하며 이동하는 즉시 비파괴 미리보기에 반영됩니다.

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
제외합니다. 마이크, 카메라, 브라우저, OAuth, Office COM, 장시간 Scheduler는 별도
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
- Knowledge Graph 작업공간: 전문가 목록 또는 “지식 그래프 열어줘”로 Vault 연결 구조를 시각적으로 탐색
- Knowledge Graph는 노드 반발력·링크 장력 기반으로 움직이며 드래그, 관계 강조, 확대·이동, 일시정지와 재배치를 지원
- 시안 제작 전문가는 특정 원형·카드 템플릿 대신 학습용 참고 이미지와 사용자 지시로 AI 장면 설계도를 생성하며, 후속 수정도 설계도 변경으로 검증한 뒤 원본 제작 이미지를 다시 렌더링
- 시안 스타일 선택 상태를 별도 카드로 명확히 표시하며, AI 설계도에서 제작 이미지가 누락되면 최대 3회 자동 교정 후 렌더링
- [DEPLOYMENT.md](DEPLOYMENT.md): Windows 설치본과 배포 제한
- [Level2 리팩토링 10단계.txt](Level2%20리팩토링%2010단계.txt): 2026-07-22 기반 리팩터링 기록
- [Agent 인수인계.txt](Agent%20인수인계.txt): 작업 이력과 필수 운영 규칙
# 시안 제작 전문가 v2 아키텍처

시안 작업공간은 단일 모델이 모든 판단을 수행하지 않습니다. 기억 큐레이터, Qwen2.5-VL 기반
스타일 분석가, 피사체·크롭 전문가, Qwen 설계 디렉터, Qt/SVG 레이어 렌더러, 렌더링 결과
검수자와 제약 교정자가 순차 실행됩니다. 무거운 모델은 역할 종료 후 해제하며, 승인한 결과만
이미지 스타일 인덱스와 작업공간 기억에 들어갑니다.

- 참고 이미지는 Vision 계측과 이미지 전용 임베딩으로 검색합니다. 텍스트용 BGE-M3를 이미지
  임베딩이라고 부르지 않습니다.
- 편집 원본은 버전형 레이어 그래프로 유지하고 PNG와 함께 편집 가능한 SVG를 생성합니다.
- 각 미리보기는 canonical scene digest, revision, parent digest를 가진 디자인 문서입니다. AI 수정은
  현재 선택된 리비전을 기준으로만 수행되어 실행 취소·다시 실행 뒤 도착한 오래된 결과를 덮어쓰지 않습니다.
- 얼굴·피사체 안전 영역을 설계 입력에 넣고 BiRefNet이 준비되지 않은 환경은 OpenCV 기반
  보수적 폴백을 사용합니다.
- 글자 자간·행간·윤곽선·그림자·곡선 경로, 장식 점선, 투명도와 블렌드 속성을 레이어 스키마가
  표현합니다. 곡선 문구는 SVG `textPath`로 저장되어 PNG 픽셀에 구워진 텍스트가 아닙니다.
- 실제 렌더링 PNG를 Vision 모델이 다시 검수하고 최대 2회만 최소 변경 교정을 수행합니다.
- 원형 알파·빈 출력처럼 수치로 판정할 수 있는 계약은 Vision 응답보다 실제 렌더링 픽셀을 우선해
  검사합니다. 텍스트·도형 수정은 SVG 경로, 누끼는 BiRefNet/GrabCut 경로, 새로운 픽셀 생성은
  명시적으로 필요한 경우에만 확산 모델 경로로 라우팅합니다.
- 실제 가중치 학습은 네 장의 참고 이미지를 곧바로 과적합시키지 않습니다. 승인 시안 20개가
  모이면 SDXL LoRA 학습 manifest를 만들 수 있는 승인 기반 post-training 경로를 제공합니다.
