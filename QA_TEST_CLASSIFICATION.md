# 루트 진단 스크립트 분류

이 문서는 `pytest.ini`의 명시적 수집 목록에서 빠져 있던 레거시 `test_*.py`
12개를 자동 회귀 테스트와 수동 진단으로 구분한다. 파일명이 `test_`로
시작하더라도 실제 장치·네트워크·사용자 데이터를 만지는 진단은 pytest 테스트가
아니다. 모든 수동 진단은 이제 import/수집 시 아무 작업도 하지 않고 명시적인
`main()` 실행에서만 동작한다.

## 자동 수집 권장

| 파일 | 자동 검증 범위 | 외부 부작용 |
|---|---|---|
| `test_main_fix.py` | 필수 `Config` 속성 접근 가능 여부 | 없음 |
| `test_tts_daemon_thread.py` | 가짜 speaker를 주입한 데몬 스레드 종료·결과 전달 | 없음(실제 TTS는 CLI `--live`에서만 실행) |

`pytest.ini`의 `python_files`에는 위 두 파일만 추가했다. 나머지 레거시 진단은
파일명이 `test_`로 시작해도 자동 수집하지 않는다.

## 수동 진단 전용

| 파일 | 분류 | 자동 수집하지 않는 이유 | 명시적 실행 예시 |
|---|---|---|---|
| `test_camera.py` | 실제 하드웨어 | 카메라 점유·프레임 캡처 | `python -X utf8 test_camera.py` |
| `test_full_flow.py` | 라이브 통합 | LLM 호출; 메모리 저장과 음성은 선택적 부작용 | `python -X utf8 test_full_flow.py --prompt "안녕"` |
| `test_gpu.py` | 환경 진단 | 로컬 CUDA/드라이버 상태 조회, 무거운 torch 초기화 | `python -X utf8 test_gpu.py` |
| `test_ollama.py` | 라이브 네트워크 | Ollama GET/POST 호출 | `python -X utf8 test_ollama.py` |
| `test_simple_llm.py` | 라이브 네트워크/모델 | 실제 LLM 한 턴 호출 | `python -X utf8 test_simple_llm.py` |
| `test_tools.py` | 파일 생성 | 폴더와 Excel 파일 생성 | `python -X utf8 test_tools.py --output-dir <새-전용-경로>` |
| `test_tts_integration.py` | 실제 오디오 | 음성 장치 재생 | `python -X utf8 test_tts_integration.py --live` |
| `test_tts_simple.py` | 실제 오디오 | 음성 장치 재생 | `python -X utf8 test_tts_simple.py --live` |
| `test_tts.py` | 실제 오디오/드라이버 | pyttsx3 엔진과 음성 장치 사용 | `python -X utf8 test_tts.py --live` |
| `test_ui_input.py` | 수동 GUI | 창 표시와 사용자 입력이 필요 | `python -X utf8 test_ui_input.py` |

`test_full_flow.py`는 기본적으로 대화를 저장하지 않고 음성을 재생하지 않는다.
실제 저장은 `--persist-memory`, 실제 음성은 `--speak`를 각각 명시해야 한다.
`test_tools.py`는 기존 사용자 파일을 덮어쓰지 않도록 존재하지 않는 출력 경로만
허용한다. TTS 수동 진단은 실수로 실행해도 재생되지 않도록 `--live`가 필수다.

## 판정 원칙

- 자동 회귀 테스트는 오프라인·결정적이며 사용자 데이터와 장치를 변경하지 않는다.
- 실제 서버, GPU/카메라, 스피커, 대화 메모리, GUI 입력을 검증하는 작업은 수동
  수락 진단으로 보고 자동 통과 수치에 포함하지 않는다.
- 수동 진단의 성공은 해당 PC에서 실행한 시점의 증거일 뿐 다른 환경의 기능 완료를
  보장하지 않는다.
