"""Manual pyttsx3 device/voice diagnostic; never runs during import."""

from __future__ import annotations

import argparse


def run_pyttsx3_diagnostic(text: str) -> dict:
    import pyttsx3

    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    selected_voice = None
    for voice in voices:
        print(f"음성: {voice.name}, 언어: {voice.languages}")
        if selected_voice is None and (
            "ko" in str(voice.languages).lower()
            or "korean" in voice.name.lower()
        ):
            selected_voice = voice

    if selected_voice is not None:
        engine.setProperty("voice", selected_voice.id)
        print(f"한국어 음성 설정: {selected_voice.name}")
    else:
        print("한국어 음성을 찾을 수 없어 기본 음성을 사용합니다.")

    engine.say(text)
    engine.runAndWait()
    print("음성 재생 완료!")
    return {
        "voice_count": len(voices),
        "selected_voice": selected_voice.name if selected_voice else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="실제 pyttsx3 음성 장치 수동 진단")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--text", default="안녕하세요, 보스! 자비스 테스트입니다.")
    args = parser.parse_args()
    if not args.live:
        parser.error("실제 음성 재생에는 --live를 명시해야 합니다.")
    run_pyttsx3_diagnostic(args.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
