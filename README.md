# 🤖 자비스 (Jarvis) - 개인 AI 비서 프로젝트

영화 아이언맨의 자비스처럼, 모든 일을 보조해주는 개인 AI 비서를 만드는 프로젝트입니다.

## 🚀 빠른 시작

### 1. 환경 설정

```powershell
# 가상 환경 생성
python -m venv venv

# 가상 환경 활성화 (Windows)
.\venv\Scripts\activate

# 패키지 설치
pip install -r requirements.txt
```

### 2. API 키 설정

`.env.template` 파일을 복사하여 `.env` 파일을 만들고, Anthropic API 키를 입력하세요:

```powershell
Copy-Item .env.template .env
```

`.env` 파일을 열고 `ANTHROPIC_API_KEY` 에 실제 키를 입력합니다.

### 3. 실행

```powershell
python main.py
```

## 📋 로드맵

### Phase 1: 기반 구축 ✅ (현재)
- CLI 기반 대화 인터페이스
- 대화 기록 저장 (SQLite)
- 기본 안전장치 (Harness Layer)

### Phase 2: 핵심 기능
- Tool Use (파일 읽기/쓰기, 명령어 실행, 웹 검색)
- 개인화 메모리 (사용자 프로필)

### Phase 3: 확장 기능
- RAG (지식 기반 검색)
- 음성 인터페이스 (STT/TTS)
- 스케줄러
- 하드웨어 확장 (마이크/카메라, 제스처 인식)

### Phase 4: 고도화
- 자율 에이전트 (ReAct 패턴)
- MCP (Model Context Protocol) 지원
- 멀티모달 (이미지 분석)
- 플러그인 시스템

## 📂 프로젝트 구조

```
Jarvis/
├── main.py              # 진입점
├── config.py            # 설정 관리
├── requirements.txt     # 의존성
├── .env.template        # 환경변수 템플릿
├── core/
│   ├── __init__.py
│   ├── llm.py          # LLM 래퍼
│   ├── memory.py       # 대화 저장
│   └── harness.py      # Harness Engineering (안전장치)
├── data/               # 데이터 저장
└── plugins/            # 플러그인 디렉토리
```

## 🔗 참고 자료

- [Anthropic API 문서](https://docs.anthropic.com)
- [Model Context Protocol](https://modelcontextprotocol.io)
- [Harness Engineering 연구 (arXiv:2605.13357)](https://arxiv.org/html/2605.13357v1)
