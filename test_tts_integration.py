"""Manual live TTS integration diagnostic; import is silent."""

from __future__ import annotations

import argparse


def run_tts_integration_diagnostic(text: str) -> object:
    from core.tools import get_tool_executor

    print(f"테스트 텍스트: {text}")
    result = get_tool_executor().speak_text(text)
    print(f"결과: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="실제 ToolExecutor TTS 수동 진단")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--text", default="일상적으로는 맑음으로 예보되는군요, 보스.")
    args = parser.parse_args()
    if not args.live:
        parser.error("실제 음성 재생에는 --live를 명시해야 합니다.")
    run_tts_integration_diagnostic(args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
