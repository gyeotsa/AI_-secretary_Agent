# 아니스 Computer Use

아니스의 도구 목록에 `computer_use_run`과 `computer_use_status`가 자동 등록됩니다.
브라우저와 Windows 앱의 화면을 관찰하고, 한 번에 한 행동을 수행한 다음 화면을 다시 읽습니다.
기존 Plugin Registry, 취소 토큰, 권한 UI, Windows UI Automation 및 GPU 큐를 사용합니다.

## 사용

앱을 다시 시작한 뒤 실제 웹 주소 또는 열려 있는 창 제목으로 요청합니다.

- `브라우저에서 직접 https://example.org 검색창에 아니스를 입력해줘`
- `"제목 없음 - 메모장" 창에서 직접 아니스를 입력해줘`
- `컴퓨터 유즈 상태 알려줘`

Windows 창은 먼저 열어 두어야 합니다. 제목이 모호하면 조작하지 않습니다.
`windows_list_handles`로 정확한 제목을 확인할 수 있습니다.

브라우저 도구 입력 예시:

```json
{
  "goal": "검색창에 아니스를 입력하고 검색한 뒤 결과를 확인해줘",
  "backend": "browser",
  "url": "https://example.org",
  "profile": "default",
  "max_steps": 20,
  "timeout_seconds": 300
}
```

Windows 도구 입력 예시:

```json
{
  "goal": "본문에 안녕하세요를 입력해줘",
  "backend": "windows",
  "window_title": "제목 없음 - 메모장",
  "max_steps": 10
}
```

클릭·입력·키 입력·뒤로 이동은 **대상과 실행 내용을 보여 주는 일회성 승인**을 거칩니다.
이 승인은 영구 허용 설정과 별개이며 저장되지 않습니다. 거절·취소·승인 시간 초과 시 후속 행동을
실행하지 않습니다. 작업 도중 채팅의 기존 취소 기능으로 중단할 수 있습니다.

## 실행 환경

- 기존 `OLLAMA_BASE_URL`과 전용 `OLLAMA_COMPUTER_USE_MODEL`을 사용합니다.
  기본 모델은 `qwen2.5vl:7b`이며 `computer_use` 역할은 temperature 0을 사용합니다.
  대화·일반 이미지 분석 모델 설정은 변경하지 않습니다.
- 일반 대화가 클라우드 모델을 사용하더라도 화면 이미지는 설정된 Ollama endpoint로 보냅니다.
  endpoint를 원격 서버로 설정했다면 이미지 역시 해당 서버에 전달됩니다.
- 기존 requirements의 Playwright·Pillow·pywinauto·jsonschema를 재사용합니다.
  Chromium이 없다면 가상환경에서 `python -m playwright install chromium`이 필요합니다.
  선택한 모델이 없다면 Ollama에 먼저 설치해야 합니다. 실행 중 자동 설치하지 않습니다.
- `computer_use_status`는 설치된 Python 모듈과 설정을 조회합니다. 모델 다운로드·서버 연결·
  Chromium 실행 가능 여부까지 검증했다는 의미는 아닙니다.
- 브라우저는 작업 전용 창을 열고 종료 시 닫습니다. 기존 사용자의 Chrome 탭에 연결하지 않습니다.
  로그인 상태는 DB 디렉터리 아래 `browser_profiles/computer_use/<profile>`에 유지됩니다.
  로그인·CAPTCHA가 필요하면 사용자 개입 필요 상태를 반환합니다.
- Windows는 하나의 창 Handle과 프로세스에 고정됩니다. 다른 창으로의 자동 확장은 하지 않습니다.

## 구조와 실행 계약

`관찰 → 제한된 JSON 행동 선택 → 대상 재검증 → 일회성 승인 → 재검증 → 실행 → 관찰`

| 위치 | 역할 |
| --- | --- |
| `core/computer_use.py` | 행동 스키마, 비전 모델 호출, 반복 제어, 취소·시간·행동 한도, 완료 검증 |
| `core/computer_use_backends.py` | Playwright DOM 요소와 Windows UIA 요소에 대한 관찰·입력 |
| `plugins/computer_use.py` | 자연어 의도·도구 등록, 권한, 작업 단독 실행, 최종 증거 저장 |
| `core/permission.py`, `main_qt.py`, `ui/main_window.py` | 일회성 승인과 요청별 응답 연결 |

모델 응답 스키마에는 **현재 관찰한 요소 ID와 지원하는 행동만** 포함됩니다. 모델이 생성한
JavaScript·Python·셸 명령을 실행하는 경로는 없습니다. DOM 요소나 UIA identity가 바뀌면 이전
승인으로 실행하지 않고 다시 관찰합니다. Windows에서는 입력 직전 포커스와 프로세스도 확인합니다.
값은 화면/모델 입력에서 짧게 표시되지만, DOM/UIA 입력값 **전체의 SHA-256**을 별도 호스트
상태에 보관해 승인 전후 다시 비교합니다. 같은 표시 prefix 뒤의 내용만 바뀌어도 조작하지 않습니다.
전체 값·그 해시·네이티브 identity는 모델 payload와 결과 trace에 보내지 않습니다.
이것이 화면 이미지나 표시된 텍스트 전체의 비밀 제거를 보장하는 것은 아닙니다.

브라우저는 클릭, 텍스트 교체, 드롭다운 선택, 제한된 키 입력, 스크롤, 뒤로 가기, 작업 내 탭 전환을
지원합니다. Windows는 UIA 활성화·텍스트 교체, 제한된 키 입력과 스크롤을 지원합니다. 텍스트
교체와 드롭다운 선택은 실제 값을 다시 읽어 검증합니다.

좌표 클릭과 Windows 전역 텍스트 입력은 `allow_coordinates: true`가 있어야 후보에 포함됩니다.
이때도 별도 일회성 좌표 승인을 요청하고, 입력 직전 창 위치와 화면 픽셀이 동일해야 실행합니다.
좌표는 브라우저 viewport 또는 대상 Windows 창의 왼쪽 위를 원점으로 합니다.
Windows 전역 텍스트 입력은 관찰 당시 유일하게 포커스된 안정적인 UIA 요소를 고정하고,
승인 후 및 실제 Unicode 입력 직전 같은 요소·포커스·전경 창인지 확인합니다. 픽셀 일치만으로
입력 대상이 같다고 판단하지 않으며 비밀번호 요소나 신원을 확인할 수 없는 대상은 거부합니다.

브라우저 문서 이동은 시작 URL의 출처로 제한됩니다. 필요한 추가 출처는 `allowed_origins`에
`https://example.org` 형태로 명시합니다. 기존 공개 URL 검증을 하위 요청·리디렉션과 WebSocket에
적용합니다. 사설 주소, 파일 업로드, 자동 다운로드, 기본 대화상자의 자동 수락은 지원하지 않습니다.

모델이 완료를 제안하면 새 화면을 별도 검증 요청으로 평가하고, 근거 문구가 검증 후 재관찰한
화면에도 존재해야 성공합니다. 두 관찰의 context·요소 상태·호스트 전용 전체 값 해시와
포커스 identity도 일치해야 합니다. 같은 근거 문구가 남아 있어도 checkbox나 입력값 등 최종
상태가 달라지면 성공이 아닙니다. 화면에 드러나지 않는 서버 내부 상태나 순수 시각적 결과는
미검증으로 남을 수 있습니다. 모델의 의미 판단 자체가 정확성을 보장하지는 않습니다.

같은 상태에서 같은 행동을 반복하거나 한도를 초과하면 중단합니다. 실행 중 예외가 발생하면
부작용이 이미 발생했을 가능성을 보존하고 자동 재시도하지 않습니다. 기본 한도는 20행동·300초,
최대 40행동·600초입니다. 실행 중인 네이티브 호출을 되돌릴 수는 없으며 취소는 이후 행동을
막습니다. 같은 프로세스에서는 화면 조작 작업을 하나씩 실행합니다.

최종 스크린샷과 근거는 DB 디렉터리 아래 `computer_use/<run-id>/final.png`, `trace.json`에
저장됩니다. 중간 스크린샷은 메모리에만 유지합니다. 최종 화면에는 작업 내용이 포함될 수 있습니다.
기본 `data/computer_use/`는 Git에서 제외되며 자동 보존 기간은 설정하지 않았습니다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pytest test_computer_use.py -q
.\.venv\Scripts\python.exe -m pytest test_computer_use.py -q -m integration
```

실제 설정된 Ollama 모델까지 연결하려면:

```powershell
$env:ANIS_COMPUTER_USE_LIVE_MODEL = '1'
.\.venv\Scripts\python.exe -m pytest test_computer_use.py -q -m integration
```

브라우저 통합 테스트는 네트워크를 차단한 임시 페이지에서 수행합니다. 개인 계정이나 업무 앱을
수정하지 않습니다. 일반 테스트에는 승인 후 취소·시간 초과, DOM/UIA 변경, 결과 오판, 반복 행동,
조건부 입력 계약, 일회성 승인과 늦은 승인 응답 격리가 포함됩니다.

Windows 실제 입력 테스트는 별도의 테스트 창을 만들고 그 창만 조작합니다.
대화형 Windows 세션에서 다음처럼 실행합니다.

```powershell
$env:ANIS_COMPUTER_USE_NATIVE_TEST = '1'
.\.venv\Scripts\python.exe -m pytest test_computer_use.py -q -m integration -k owned_fixture
```

과거 기록 — 2026-09-27 검증: 관련 회귀 테스트 181개와 실제 Chromium + 로컬 `qwen2.5vl:7b`
통합 테스트가 통과했습니다. Windows UIA의 대상·값·포커스 재검증은 모의 테스트로 확인했습니다.
현재 실행 환경에서는 Windows가 테스트 창의 전경 활성화를 허용하지 않아 실제 네이티브 입력
검증은 완료하지 못했습니다. 이를 우회하기 위해 포커스 검증을 약화하지 않았습니다.

2026-10-04 보강은 전체 값의 숨은 suffix 변경, 승인 도중 및 입력 직전 포커스 이동,
완료 검증 후 checkbox/전체 값 변경의 회귀 테스트를 추가했습니다. 같은 날
`test_computer_use.py`, `test_kimi_integration.py`, `test_plugin_hub_tabs.py`, `test_ui_theme.py`의
집중 안전성 점검은 **103 passed, 3 deselected (27.30초)**였습니다. 이 합산은 Computer Use
단독 통과 수가 아니며, 현재 Windows 네이티브 어댑터의 실제 입력 수락이나 실제 업무 앱 완료를
입증하지 않습니다. 위 과거 Chromium/모델 기록도 현재 모든 어댑터의 재검증으로 확대하지 않습니다.

구현 참고: [Ollama 비전 구조화 출력](https://docs.ollama.com/capabilities/structured-outputs),
[Playwright 요소 핸들](https://playwright.dev/python/docs/api/class-elementhandle).
관찰 시점의 DOM 요소를 고정하기 위해 핸들을 사용하고 변경·분리된 요소는 재탐색합니다.
