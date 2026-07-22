# Level 2 전수 검토 결과 (2026-07-22)

## 판정

기존 문서의 “Level 2 완료” 표기는 실제 동작 검증보다 앞서 있었습니다. 이번 검토에서 1–7단계의 핵심 연결 오류를 수정했고 8단계 플러그인, 9단계 RAG, 10단계 역할 기반 하이브리드 LLM 라우팅까지 구현했습니다. Playwright 브라우저 바이너리, 실제 Anthropic 성공 경로 및 GUI·하드웨어 연동은 별도 실환경 검증이 남아 있습니다.

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

## 검증 결과

- 전체 Python 소스 `compileall`: 통과.
- 로컬 import 정합성 검사: 발견된 KnowledgeUpdater/Scheduler 불일치 수정.
- Tool Registry: 70개/고유 70개, browser/filesystem/git/system_tools 자동 등록 확인.
- 안전성 회귀: 허용 루트와 이름이 비슷한 형제 경로 차단, `&`/`|` 명령 연결 차단, 승인 콜백 없는 파일 쓰기 거부 확인.
- Python 3.12 `.venv` 재생성 및 전체 `requirements.txt` 설치 완료. 기존 환경은 `.venv-python310-broken`으로 보존.
- RAG: `BAAI/bge-m3` 로컬 모델(1024차원), Chroma 문서 추가·한국어 검색·재시작 영속성 테스트 통과.
- 자동 회귀 테스트: Level 2 안전성 4개 + RAG 종단 테스트 1개, 총 5개 통과.

## 남은 우선순위

1. Playwright 설치 및 `playwright install chromium` 후 브라우저 실동작 검증.
2. 실제 Anthropic API 키로 Reasoner 성공 경로의 품질·비용·지연시간 비교.
3. 실제 Ollama, GUI, 마이크·카메라·TTS 종단 검증.
4. mail/calendar는 OAuth 토큰 저장·권한·계정 선택 정책을 먼저 확정한 뒤 확장.
