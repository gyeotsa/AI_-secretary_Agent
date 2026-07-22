# JARVIS 업무 자동화 플러그인 로드맵

상태 표기: `연결됨`은 Registry·Permission·검증·테스트까지 완료된 기능만 의미한다.

## Sprint 1 — 무료 로컬 문서 및 Windows 기반

| 영역 | 방식 | 비용 | 상태 |
|---|---|---:|---|
| Excel | openpyxl, 앱 독립 파일 처리 | 무료 | 연결됨 |
| Word | python-docx, 앱 독립 DOCX 처리 | 무료 | 연결됨 |
| PowerPoint | python-pptx, 앱 독립 PPTX 처리 | 무료 | 연결됨 |
| PDF | PyMuPDF 생성·추출 | 무료 | 연결됨 |
| HWPX | python-hwpx 순수 Python 처리 | 무료 | 연결됨 |
| HWP/HWPX 라이브 제어 | 한컴 설치 시 pyhwpx/COM 어댑터 | 무료 라이브러리, 한컴 라이선스 별도 | 대기 |
| Windows 앱 | App Paths/PATH 검색, 실행, 창 포커스 | 무료 | 연결됨 |
| MS Office 라이브 제어 | pywin32 COM 어댑터 | 무료 라이브러리, Office 라이선스 별도 | 대기 |

## Sprint 2 — 웹·검색·미디어

- 기존 Playwright 세션·다운로드·업로드·취소 고도화
- DuckDuckGo 무료 검색, 선택적 Tavily 공급자
- YouTube 자막 수집 및 요약
- 브라우저 작업의 로그인 세션과 전송/구매 승인 분리

## Sprint 3 — 개발 자동화

- VS Code/PyCharm/Visual Studio CLI·진단 어댑터
- GitHub CLI 기반 Issue·PR·CI 연결
- 제한된 작업 디렉터리와 명령 allowlist 기반 코드 실행
- Docker는 설치된 경우에만 선택적 격리 실행기로 사용

## Sprint 4 — 계정·클라우드

- Gmail 또는 Outlook Mail OAuth
- Google Calendar 또는 Outlook Calendar OAuth
- Google Drive 또는 OneDrive/SharePoint
- Notion, Box 및 RAG 문서 동기화
- 조회·초안·승인·외부 반영을 별도 Tool과 Permission으로 분리

## Sprint 5 — 협업·개인비서

- Slack/Teams 메시지 검색·요약·승인 후 전송
- 연락처·할 일·리마인더·지도/교통
- Windows UI Automation 우선, 화면 좌표 기반 pyautogui는 최후 fallback
- 카카오톡 등 공식 자동화 API가 없는 앱은 오작동·약관 위험을 별도 표시

## 설계 원칙

- OAuth 자격증명이나 대상 프로그램이 없으면 `연결됨`으로 표시하지 않는다.
- 로컬 파일 방식과 실행 중인 앱의 COM/UI 제어 방식을 별도 Tool로 유지한다.
- 외부 전송·삭제·앱 실행·UI 조작은 Permission 승인을 거친다.
- Plugin Registry가 Tool과 Intent의 단일 진실 공급원이며 Executor에 도메인 분기를 추가하지 않는다.
- 모든 실행형 플러그인은 Tool 등록만으로 완료 처리하지 않고 자연어 Intent 우회 방지와 실제 종단 실행을 함께 검증한다.
- Windows 앱은 제한된 자동 검색과 머신 로컬 카탈로그를 사용하며 WinError 740은 `runas` UAC 요청으로 전환한다. 경로 수동 등록은 자동 탐색이 실패하거나 동일 앱 후보가 여러 개일 때만 필요하다.
