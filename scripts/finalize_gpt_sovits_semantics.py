"""Merge GPT-SoVITS semantic shard files into the training TSV format."""

from __future__ import annotations

import argparse
from pathlib import Path


def merge_semantic_shards(output: Path, shards: list[Path]) -> int:
    rows: list[str] = []
    for shard in shards:
        rows.extend(line for line in shard.read_text(encoding="utf-8-sig").splitlines() if line.strip())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "item_name\tsemantic_audio\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("shards", nargs="+", type=Path)
    args = parser.parse_args()
    count = merge_semantic_shards(args.output, args.shards)
    print(f"semantic_rows={count}")


if __name__ == "__main__":
    main()
