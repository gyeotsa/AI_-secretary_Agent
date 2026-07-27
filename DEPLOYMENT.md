# JARVIS Windows 오프라인 배포

> 현재 `release/installer`는 2026-07-26 당시 소스 상태의 오프라인 빌드입니다.
> 이후 역할별 모델, GPT-SoVITS, 실제 웹 검색, 대화·파일·알람 개선이 추가되었으므로
> 최신 소스를 배포하려면 설치본을 다시 빌드해야 합니다. 향후 기본 배포 방식은
> 경량 Bootstrap 설치 후 첫 실행에서 선택한 모델을 체크섬 검증과 함께 내려받는
> 구조로 전환할 예정입니다.

`packaging/build_release.ps1`은 다음 항목을 완전한 오프라인 설치 묶음으로 조립한다.

- PyInstaller로 고정한 Python 3.12 런타임과 프로젝트 라이브러리
- BGE-M3 RAG 임베딩 모델
- faster-whisper large-v3와 OpenAI Whisper medium fallback 모델
- Ollama 실행 파일과 빌드 시점에 `packaging` 설정으로 선택된 로컬 모델
- Playwright 브라우저 런타임
- JARVIS 앱/작업 표시줄/바로가기 아이콘

## 빌드

빌드 PC에 Inno Setup 6이 설치되어 있어야 한다.

```powershell
winget install --id JRSoftware.InnoSetup -e
./packaging/build_release.ps1
```

결과물은 `release/installer` 폴더의 `JARVIS-Setup.exe`와 자동 인식되는 `.bin` 조각들이다.
20GB가 넘는 포함 데이터를 단일 Windows EXE로 만들면 4GB 실행 파일 한계 때문에 실행되지 않으므로
분할 설치 형식을 사용한다. 배포 시 `installer` 폴더 전체를 전달하고 사용자는 `JARVIS-Setup.exe`
하나만 실행하면 된다. 설치 시 사용자 관리자 권한 없이
`%LOCALAPPDATA%\Programs\JARVIS`에 설치하고 바탕화면과 시작 메뉴 바로가기를 만든다.
약 25GB 이상의 여유 공간을 권장한다.

## 외부 의존 기능

오프라인 설치 파일은 이 프로젝트가 재배포할 수 있는 로컬 런타임과 모델을 포함한다.
다음 항목은 제3자 소프트웨어 라이선스, 사용자 계정 또는 실제 장치가 필요하므로 설치 파일만으로
자동 활성화할 수 없다.

- HWP/Office COM 실시간 제어: 한컴오피스 또는 Microsoft Office 정품 설치 필요
- Gmail·Google Calendar·Outlook 등: 각 사용자의 OAuth 로그인과 동의 필요
- Edge 온라인 TTS, 날씨, 웹 검색: 인터넷 연결 필요
- 마이크·카메라: 해당 PC 장치와 Windows 개인정보 권한 필요

파일 기반 Word/Excel/PowerPoint/PDF 생성, 로컬 LLM, RAG, STT와 Windows 기본 기능은 포함된
런타임을 사용한다. 배포 전에는 별도의 깨끗한 Windows 사용자 계정 또는 VM에서 설치·실행·제거를
최종 검증해야 한다.
