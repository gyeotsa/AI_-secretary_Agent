"""Small manual live TTS diagnostic; import and collection are inert."""

from __future__ import annotations

import argparse


def run_simple_tts_diagnostic(text: str) -> object:
    from core.tools import get_tool_executor

    result = get_tool_executor().speak_text(text)
    print(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="실제 TTS 단일 문장 수동 진단")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--text", default="안녕하세요, 사장님. 테스트 음성입니다.")
    args = parser.parse_args()
    if not args.live:
        parser.error("실제 음성 재생에는 --live를 명시해야 합니다.")
    run_simple_tts_diagnostic(args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
