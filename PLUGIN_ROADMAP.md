# JARVIS 업무 자동화 플러그인 로드맵

상태 표기: 설치·연결·인증·검증은 서로 다른 상태다. 앱 상단 `P` 진단 UI에서
각 상태와 누락 의존성·지원 OS·인증 요구 사항을 독립적으로 확인한다.

## Sprint 1 — 무료 로컬 문서 및 Windows 기반

| 영역 | 방식 | 비용 | 상태 |
|---|---|---:|---|
| Excel | openpyxl, 앱 독립 파일 처리 | 무료 | 연결됨 |
| Word | python-docx, 앱 독립 DOCX 처리 | 무료 | 연결됨 |
| PowerPoint | python-pptx, 앱 독립 PPTX 처리 | 무료 | 연결됨 |
| PDF | PyMuPDF 생성·추출 | 무료 | 연결됨 |
| HWPX | python-hwpx 순수 Python 처리 | 무료 | 연결됨 |
| HWP/HWPX 라이브 제어 | 패키지 보존 편집 + 설치 시 COM 어댑터 | 무료 라이브러리, 한컴 라이선스 별도 | 구현됨·현재 COM 미연결 |
| Windows 앱 | App Paths/PATH 검색, 실행, 창 포커스 | 무료 | 연결됨 |
| MS Office 라이브 제어 | 보존 편집·PDF 시각 QA·pywin32 COM 어댑터 | 무료 라이브러리, Office 라이선스 별도 | 구현됨·현재 COM 미연결 |

## Sprint 2 — 웹·검색·미디어

- DDGS 무료 웹 검색·출처 URL 검증·후속 검색 문맥 — 1차 연결됨
- Playwright 공개 페이지 본문·스크린샷·SSRF 방어 — 1차 연결됨
- 검색 결과 페이지 실제 방문·게시/수정일 확인·공식 출처 우선·주장별 교차 인용 — 연결됨
- Playwright persistent profile·Cookie·다운로드·취소 — 연결됨
- PDF 본문·HTML 표·동적 페이지와 Prompt Injection 격리 — 연결됨
- TTL Research Cache와 강제 재검색 — 연결됨
- 업로드·결제·게시 같은 외부 반영 Browser Action — 대기
- 선택적 Tavily 공급자
- YouTube 자막 수집 및 요약
- 브라우저 작업의 로그인 세션과 전송/구매 승인 분리

## Sprint 3 — 개발 자동화

- Workspace 내 프로젝트·파일 생성, 단일 파일 코드 작성·문법·해시 검증 — 초기 연결
- 저장소 분석·최소 patch·lint/test·diff·rollback Coding Agent — 최우선 개발
- VS Code/PyCharm/Visual Studio CLI·진단 어댑터
- GitHub CLI 기반 Issue·PR·CI 연결
- 제한된 작업 디렉터리와 명령 allowlist 기반 코드 실행
- Docker는 설치된 경우에만 선택적 격리 실행기로 사용

## Sprint 4 — 계정·클라우드

- Google Gmail/Calendar OAuth PKCE·DPAPI Token Vault — 구현됨, 실계정 검증 대기
- Microsoft Outlook Mail/Calendar Graph OAuth PKCE — 구현됨, 실계정 검증 대기
- Google Drive·OneDrive 원격 ID 카탈로그 동기화 — 구현됨, 실계정 검증 대기
- Notion 메타데이터 카탈로그 동기화 — 구현됨, Integration Token 검증 대기
- 조회·초안·승인·외부 반영 Tool과 Permission 분리 — 구현됨

## Sprint 5 — 협업·개인비서

- Slack/Teams 메시지 조회·근거 요약·승인 후 전송·원격 ID 재검증 — 구현됨, 실계정 검증 대기
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
- Windows 앱 검색 소스에는 사용자별 LOCALAPPDATA 설치, 사용자/공용 시작 메뉴 바로가기와 최종 C 드라이브 전체 검색 fallback도 포함한다.
- 사용자 앱 별칭은 자연어로 추가·목록 조회·삭제할 수 있으며 머신 로컬 설정으로 기본 별칭과 분리해 보존한다.
- Tool 등록만 된 상태, 실제 실행 가능 상태, 인증 완료 상태, E2E 검증 완료 상태를 구분한다.
- 모든 실행 결과는 향후 공통 `ToolRunResult`의 Artifact와 Evidence로 보고하며 문자열의 성공 문구만으로 완료하지 않는다.
- Plugin SDK와 Registry는 현재 `str | ToolRunResult` 점진 이전 경계를 지원한다.
  파일시스템 생성·수정 Tool은 첫 직접 반환 전환을 완료했으며, 나머지 Plugin은
  전용 검증기 추가와 함께 순차 이전한다.
- Calendar와 Excel·Word·PowerPoint·PDF·HWPX 생성 Tool은 직접 typed 결과 전환을
  완료했다. 각 생성 파일은 저장 후 동일 형식 라이브러리로 재열기 검증한다.
- Browser 검색·페이지·스크린샷과 Open-Meteo Weather 조회는 직접 typed 결과
  전환을 완료했다. 검색 출처, HTTP 응답, 조회·관측 시각과 위치 좌표를 Evidence로
  보존한다.
- Git·Windows Control·Mail·Alarm은 직접 typed 결과 전환을 완료했다. Git 저장소
  상태, Windows 프로세스·창·별칭, RFC 822/SMTP 접수, Scheduler 작업 등록을
  실제 상태에서 확인하며 확인 불가능한 실행 요청은 `unverified`로 구분한다.
- System Tools는 로컬 시간·날짜·Plugin Registry 상태를 typed Evidence로 반환한다.
  중앙 ToolExecutor의 레거시 어댑터는 아직 직접 이전되지 않은 내장 Tool 문자열을
  검증 후 `succeeded`·`failed`·`unverified`로 정규화한다.
- 내장 파일 읽기·목록, Workspace·Project Indexer, Semantic Memory·RAG Tool은
  직접 typed 결과 전환을 완료했다. 실제 경로·해시·인덱스·영속 저장·검색 출처를
  Evidence와 Artifact로 보존한다.
- 내장 파일 쓰기·폴더 생성/삭제·명령 실행, 마이크·카메라 조회/캡처, 자동화
  작업/엔진도 직접 typed 결과 전환을 완료했다. 저장 후 내용·프로세스 종료 코드·
  장치 열거·이미지 파일·Scheduler DB와 스레드 상태를 실제 Evidence로 사용한다.
- 프로필·환경설정·Knowledge Graph·Multi-Agent·STT/감지·Scheduler 호환 API와
  구형 웹 검색·Vision·PDF·Excel 내장 Tool도 직접 typed 결과 전환을 완료했다.
  모델 출력이나 장치 스트림처럼 아직 완결 증거가 없는 결과는 `unverified`로 남긴다.
- TTS는 GUI 문자열 호환과 Registry typed 결과를 분리했다. 중앙 Tool 실행은
  Action Journal에 상태·Evidence·Artifact·지연 시간을 기록하고, 일반 대화가
  Tool 없이 외부 작업 완료를 주장하는 응답은 사용자 경계에서 차단한다.
- Recovery는 typed Tool 상태만 신뢰하며 미검증 결과를 성공으로 승격하지 않는다.
  전체 실행 종료는 완료·부분 완료·실패·취소로 구분되고 재시도 정보까지 사용자에게
  전달된다. P6에서 레거시 구현도 `legacy_runtime` Plugin으로 편입되어 스키마 조회와
  실행이 Registry를 우회할 수 없다. 입출력 JSON Schema, 이름 충돌 차단, timeout,
  재시도, 취소와 진단 상태 계약도 중앙에서 강제한다.
