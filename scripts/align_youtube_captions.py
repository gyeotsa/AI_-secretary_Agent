"""YouTube json3 자막을 샘플 오프셋 기반 WAV 데이터셋에 정렬한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


OFFSETS = re.compile(r"_(\d{10})_(\d{10})\.wav$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_list", type=Path)
    parser.add_argument("caption_json", type=Path)
    parser.add_argument("output_list", type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--sample-rate", type=int, default=32000)
    parser.add_argument("--only-source", action="store_true")
    return parser.parse_args()


def clean_caption(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def main() -> int:
    args = parse_args()
    payload = json.loads(args.caption_json.read_text(encoding="utf-8"))
    captions: list[tuple[float, float, str]] = []
    for event in payload.get("events", []):
        text = clean_caption("".join(segment.get("utf8", "") for segment in event.get("segs", [])))
        if not text:
            continue
        start = event.get("tStartMs", 0) / 1000
        end = start + event.get("dDurationMs", 0) / 1000
        captions.append((start, end, text))

    aligned: list[str] = []
    for line in args.input_list.read_text(encoding="utf-8").splitlines():
        path, speaker, language, original_text = line.split("|", 3)
        if args.source_id not in Path(path).name:
            if not args.only_source:
                aligned.append(line)
            continue
        match = OFFSETS.search(Path(path).name)
        if not match:
            continue
        start = int(match.group(1)) / args.sample_rate
        end = int(match.group(2)) / args.sample_rate
        overlapping = [
            text
            for caption_start, caption_end, text in captions
            if min(end, caption_end) - max(start, caption_start) > 0.15
        ]
        text = clean_caption(" ".join(dict.fromkeys(overlapping))) or original_text
        aligned.append(f"{path}|{speaker}|{language}|{text}")

    args.output_list.parent.mkdir(parents=True, exist_ok=True)
    args.output_list.write_text("\n".join(aligned), encoding="utf-8")
    print(f"aligned={len(aligned)} output={args.output_list}")
    return 0 if aligned else 1


if __name__ == "__main__":
    raise SystemExit(main())
