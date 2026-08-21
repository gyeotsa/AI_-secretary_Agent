"""Approval-gated style adapter dataset preparation.

Four reference images are evidence, not a safe training set. This module makes
real LoRA/post-training an explicit, reproducible phase once enough user-approved
outputs exist, instead of falsely labelling a JSON profile as weight training.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


class StyleTrainingDataset:
    MIN_APPROVED_SAMPLES = 20

    def __init__(self, root: str | Path = "data/mockup_training"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def collect(self, profile_id: str, style_rows: list[dict]) -> dict:
        samples = []
        for row in style_rows:
            path = Path(str(row.get("image_path", "")))
            if row.get("approved") and path.is_file() and row.get("profile_id") == profile_id:
                samples.append({
                    "image": str(path.resolve()),
                    "caption": str((row.get("metadata") or {}).get("instruction", "approved design")),
                    "source": "user_approved_result",
                })
        target = self.root / profile_id
        target.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": 1, "profile_id": profile_id, "created_at": time.time(),
            "sample_count": len(samples), "minimum_required": self.MIN_APPROVED_SAMPLES,
            "ready": len(samples) >= self.MIN_APPROVED_SAMPLES, "samples": samples,
            "recommended_method": "SDXL LoRA",
        }
        (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    def training_command(self, manifest: dict) -> list[str]:
        if not manifest.get("ready"):
            raise ValueError(
                f"과적합을 막기 위해 승인 시안 {self.MIN_APPROVED_SAMPLES}개 이상이 필요합니다. "
                f"현재 {manifest.get('sample_count', 0)}개입니다."
            )
        # Returned as argv for an explicitly launched, separately permissioned job.
        return ["accelerate", "launch", "train_dreambooth_lora_sdxl.py",
                "--dataset_manifest", str(self.root / manifest["profile_id"] / "manifest.json"),
                "--output_dir", str(self.root / manifest["profile_id"] / "lora")]
