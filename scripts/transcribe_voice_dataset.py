"""GPT-SoVITS 학습용 WAV 폴더를 faster-whisper로 전사한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import wave

import torch
from faster_whisper import WhisperModel


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_list", type=Path)
    parser.add_argument("--speaker", required=True)
    parser.add_argument("--language", default="ko")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--min-seconds", type=float, default=1.2)
    parser.add_argument("--max-seconds", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "int8_float16" if device == "cuda" else "int8"
    model = WhisperModel(args.model, device=device, compute_type=compute_type)

    accepted: list[str] = []
    rejected: list[dict[str, object]] = []
    paths = sorted(args.input_dir.glob("*.wav"))
    for index, path in enumerate(paths, start=1):
        duration = wav_duration(path)
        if not args.min_seconds <= duration <= args.max_seconds:
            rejected.append({"path": str(path), "reason": "duration", "seconds": duration})
            continue
        segments, _ = model.transcribe(
            str(path),
            language=args.language,
            beam_size=5,
            temperature=0,
            vad_filter=True,
            vad_parameters={
                "min_speech_duration_ms": 180,
                "min_silence_duration_ms": 300,
                "speech_pad_ms": 120,
            },
            condition_on_previous_text=False,
        )
        valid = [
            segment.text.strip()
            for segment in segments
            if segment.text.strip()
            and segment.no_speech_prob < 0.6
            and segment.avg_logprob > -1.2
            and segment.compression_ratio < 2.4
        ]
        text = " ".join(valid).strip()
        if text:
            accepted.append(f"{path.resolve()}|{args.speaker}|{args.language}|{text}")
        else:
            rejected.append({"path": str(path), "reason": "empty_or_low_confidence", "seconds": duration})
        print(f"[{index}/{len(paths)}] {path.name}: {text or 'REJECTED'}")

    args.output_list.parent.mkdir(parents=True, exist_ok=True)
    args.output_list.write_text("\n".join(accepted), encoding="utf-8")
    report_path = args.output_list.with_suffix(".report.json")
    report_path.write_text(
        json.dumps(
            {
                "speaker": args.speaker,
                "language": args.language,
                "model": args.model,
                "device": device,
                "accepted": len(accepted),
                "rejected": rejected,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"accepted={len(accepted)} rejected={len(rejected)} report={report_path}")
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
