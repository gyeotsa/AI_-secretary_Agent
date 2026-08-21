from core.response_presenter import present_channels, present_response, requests_technical_details


RAW_LAUNCH = (
    r"프로그램 실행 성공: C:\Program Files\WindowsApps\Microsoft.WindowsNotepad_11.2605.34.0_x64__8wekyb3d8bbwe\Notepad\Notepad.exe "
    "(PID: 15152)"
)


def test_program_path_and_pid_are_hidden_by_default():
    assert present_response(RAW_LAUNCH, "메모장 실행해줘") == "프로그램을 실행했습니다, 보스."


def test_program_details_are_preserved_when_explicitly_requested():
    assert present_response(RAW_LAUNCH, "실행하고 경로와 PID도 알려줘") == RAW_LAUNCH


def test_pid_is_removed_from_other_conversational_results():
    assert present_response("작업을 시작했습니다. (PID: 321)") == "작업을 시작했습니다."


def test_detail_intent_is_case_insensitive():
    assert requests_technical_details("PID 포함해서 자세히 알려줘") is True


def test_presenter_keeps_technical_log_separate_from_screen_and_speech():
    channels = present_channels(RAW_LAUNCH, "메모장 실행해줘")
    assert channels.technical_text == RAW_LAUNCH
    assert channels.screen_text == "프로그램을 실행했습니다, 보스."
    assert channels.speech_text == channels.screen_text
