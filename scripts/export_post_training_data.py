"""Export reviewed runtime evidence for offline SFT/DPO preparation."""
from pathlib import Path
import argparse

from core.learning_runtime import get_learning_runtime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/training_exports/latest")
    args = parser.parse_args()
    counts = get_learning_runtime().export_training_data(Path(args.output))
    print(f"SFT={counts['sft']} DPO={counts['dpo']} (검토 후에만 학습에 사용하세요)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
