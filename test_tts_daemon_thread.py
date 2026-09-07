"""Daemon-thread behavior test plus an explicit live TTS diagnostic."""

from __future__ import annotations

import argparse
import threading
from collections.abc import Callable
from typing import Any


def run_in_daemon_thread(
    speaker: Callable[[str], Any], text: str, *, timeout: float = 10.0
) -> tuple[Any, bool]:
    """Run an injected speaker and propagate worker errors to the caller."""
    state: dict[str, Any] = {}

    def worker() -> None:
        try:
            state["result"] = speaker(text)
        except BaseException as exc:
            state["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError("TTS 데몬 스레드가 제한 시간 안에 종료되지 않았습니다.")
    if "error" in state:
        raise RuntimeError("TTS 데몬 스레드 실행에 실패했습니다.") from state["error"]
    return state.get("result"), thread.daemon


def test_daemon_runner_is_joined_without_live_audio() -> None:
    calls: list[str] = []

    def fake_speaker(text: str) -> str:
        calls.append(text)
        return "ok"

    result, is_daemon = run_in_daemon_thread(fake_speaker, "테스트")
    assert result == "ok"
    assert is_daemon is True
    assert calls == ["테스트"]


def run_live_tts_diagnostic(text: str, *, timeout: float = 120.0) -> object:
    from core.tools import get_tool_executor

    result, _ = run_in_daemon_thread(
        get_tool_executor().speak_text, text, timeout=timeout
    )
    print(f"실제 TTS 결과: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="실제 TTS 데몬 스레드 수동 진단")
    parser.add_argument(
        "--live",
        action="store_true",
        help="실제 음성을 재생합니다. 이 옵션 없이는 재생하지 않습니다.",
    )
    parser.add_argument(
        "--text", default="데몬 스레드에서 테스트하는 메시지입니다, 보스."
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("실제 음성 재생에는 --live를 명시해야 합니다.")
    run_live_tts_diagnostic(args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
