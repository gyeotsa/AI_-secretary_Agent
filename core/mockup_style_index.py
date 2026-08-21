"""Persistent visual-style embeddings and approved-design preference memory."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat


class VisualStyleIndex:
    """Image-native similarity index with a zero-download feature fallback.

    When a local CLIP model exists it is used through transformers. Otherwise a
    deterministic visual descriptor (colour, luminance, edges and composition)
    keeps the feature available without pretending that text BGE embeddings are
    image embeddings.
    """

    MODEL_ID = "openai/clip-vit-base-patch32"

    def __init__(self, db_path: str | Path = "data/mockup_styles/visual_style.db",
                 model_dir: str | Path = "data/models/clip-vit-base-patch32"):
        self.db_path = Path(db_path)
        self.model_dir = Path(model_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._model = None
        self._processor = None
        self._init_db()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _flat_vector(value) -> list[float]:
        """Flatten tensor/legacy JSON dimensions into one numeric embedding."""
        result: list[float] = []

        def visit(item):
            if isinstance(item, (list, tuple)):
                for child in item:
                    visit(child)
                return
            try:
                result.append(float(item))
            except (TypeError, ValueError):
                return

        visit(value)
        return result

    def _init_db(self):
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS visual_styles(
                item_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, image_path TEXT NOT NULL,
                kind TEXT NOT NULL, backend TEXT NOT NULL, vector_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_visual_styles_profile ON visual_styles(profile_id)")

    @staticmethod
    def _fallback_vector(path: Path) -> list[float]:
        with Image.open(path) as source:
            image = source.convert("RGB")
            image.thumbnail((256, 256))
            channels = image.split()
            values: list[float] = []
            for channel in channels:
                hist = channel.histogram()
                total = max(1, sum(hist))
                values.extend(sum(hist[start:start + 32]) / total for start in range(0, 256, 32))
            gray = image.convert("L")
            edges = gray.filter(ImageFilter.FIND_EDGES)
            values.extend([
                ImageStat.Stat(gray).mean[0] / 255.0,
                ImageStat.Stat(gray).stddev[0] / 128.0,
                ImageStat.Stat(edges).mean[0] / 255.0,
                image.width / max(1, image.height),
            ])
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]

    def _clip_vector(self, path: Path) -> list[float] | None:
        if not (self.model_dir / "config.json").is_file():
            return None
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor
            if self._model is None:
                self._processor = CLIPProcessor.from_pretrained(str(self.model_dir), local_files_only=True)
                self._model = CLIPModel.from_pretrained(str(self.model_dir), local_files_only=True)
                self._model.eval()
            with Image.open(path) as source:
                inputs = self._processor(images=source.convert("RGB"), return_tensors="pt")
            with torch.inference_mode():
                vector = self._model.get_image_features(**inputs).float().reshape(-1)
                vector = vector / vector.norm().clamp_min(1e-12)
            return self._flat_vector(vector.cpu().tolist())
        except Exception:
            return None

    def embed(self, path: str | Path) -> tuple[list[float], str]:
        resolved = Path(path).expanduser().resolve()
        vector = self._clip_vector(resolved)
        return (self._flat_vector(vector), "clip-vit-base-patch32") if vector is not None else (self._fallback_vector(resolved), "visual-descriptor-v1")

    def add(self, profile_id: str, image_path: str | Path, *, kind: str = "reference",
            approved: bool = False, metadata: dict | None = None) -> str:
        path = Path(image_path).expanduser().resolve()
        vector, backend = self.embed(path)
        digest = hashlib.sha256(f"{profile_id}|{kind}|{path}|{path.stat().st_mtime_ns}".encode()).hexdigest()[:24]
        with self._lock, self._connect() as db:
            db.execute("""INSERT OR REPLACE INTO visual_styles
                (item_id,profile_id,image_path,kind,backend,vector_json,metadata_json,approved,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (
                digest, profile_id, str(path), kind, backend, json.dumps(vector),
                json.dumps(metadata or {}, ensure_ascii=False), int(approved), time.time()))
        return digest

    def search(self, image_path: str | Path, *, top_k: int = 5, approved_only: bool = False) -> list[dict]:
        query, _backend = self.embed(image_path)
        sql = "SELECT * FROM visual_styles" + (" WHERE approved=1" if approved_only else "")
        with self._lock, self._connect() as db:
            rows = db.execute(sql).fetchall()
        scored = []
        for row in rows:
            # Older CLIP entries could be persisted as [[...]]. Flatten them
            # at read time so existing style profiles remain immediately usable.
            vector = self._flat_vector(json.loads(row["vector_json"]))
            if len(vector) != len(query):
                continue
            score = sum(a * b for a, b in zip(query, vector))
            scored.append({**dict(row), "score": round(float(score), 6)})
        return sorted(scored, key=lambda item: item["score"], reverse=True)[:max(1, top_k)]

    def items(self, profile_id: str, *, approved_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM visual_styles WHERE profile_id=?"
        params: list = [profile_id]
        if approved_only:
            sql += " AND approved=1"
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            item.pop("vector_json", None)
            result.append(item)
        return result

    def release(self):
        self._model = self._processor = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
