"""Lazy subject segmentation, face grounding and visual-safe-area evidence."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import gc
import importlib.util
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter


@dataclass
class SegmentationResult:
    """A mask plus auditable structural checks, not a semantic-quality claim."""

    status: str
    backend: str = ""
    mask: Image.Image | None = field(default=None, repr=False)
    changed: bool = False
    quality: dict = field(default_factory=dict)
    attempts: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def success(self) -> bool:
        return self.status in {"succeeded", "fallback"} and self.mask is not None and self.changed

    def evidence(self) -> dict:
        return deepcopy({
            "status": self.status, "success": self.success, "backend": self.backend,
            "changed": self.changed, "fallback": self.status == "fallback",
            "quality": self.quality, "attempts": self.attempts, "error": self.error,
            "mask_size": list(self.mask.size) if self.mask is not None else None,
        })


class SegmentationError(RuntimeError):
    def __init__(self, result: SegmentationResult):
        super().__init__(result.error or "피사체 분리에 실패했습니다.")
        self.evidence = result.evidence()


class _InvalidSegmentationMask(ValueError):
    def __init__(self, message: str, *, code: str, quality: dict | None = None):
        super().__init__(message)
        self.code = code
        self.quality = quality or {}


class SubjectAnalysisRuntime:
    BIREFNET_ID = "ZhengPeng7/BiRefNet"

    def __init__(self, model_dir: str | Path = "data/models/birefnet"):
        self.model_dir = Path(model_dir).resolve()
        self._model = None
        self._last_segmentation: dict | None = None

    def status(self) -> dict:
        readiness = self._birefnet_readiness()
        grabcut_ready = all(self._dependency_available(name) for name in ("cv2", "numpy"))
        return {"backend": "birefnet" if readiness["ready"] else
                           "opencv-grabcut" if grabcut_ready else "unavailable",
                "birefnet_ready": readiness["ready"], "birefnet_readiness": readiness,
                "grabcut_ready": grabcut_ready,
                "model_dir": str(self.model_dir),
                "last_segmentation": deepcopy(self._last_segmentation)}

    @staticmethod
    def _dependency_available(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError, AttributeError):
            return False

    def _local_model_file(self, filename: str) -> bool:
        try:
            target = (self.model_dir / filename).resolve()
            target.relative_to(self.model_dir)
            return target.is_file() and target.stat().st_size > 0
        except (OSError, ValueError, RuntimeError):
            return False

    def _birefnet_readiness(self) -> dict:
        """Check local prerequisites without importing/loading model code.

        Passing this check means an inference attempt is possible, not that the
        weight contents have been loaded or that segmentation quality passed.
        """
        issues = []
        config = None
        try:
            with (self.model_dir / "config.json").open(encoding="utf-8") as stream:
                config = json.load(stream)
            if not isinstance(config, dict) or not config:
                raise ValueError("empty or non-object config")
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            issues.append(f"로컬 모델 설정이 없거나 올바르지 않습니다: {type(exc).__name__}")

        has_weights = any(self._local_model_file(name) for name in ("model.safetensors", "pytorch_model.bin"))
        if not has_weights:
            for filename in ("model.safetensors.index.json", "pytorch_model.bin.index.json"):
                if not self._local_model_file(filename):
                    continue
                try:
                    with (self.model_dir / filename).open(encoding="utf-8") as stream:
                        index = json.load(stream)
                    weight_map = index.get("weight_map") if isinstance(index, dict) else None
                    if (isinstance(weight_map, dict) and weight_map
                            and all(isinstance(name, str) and self._local_model_file(name)
                                    for name in weight_map.values())):
                        has_weights = True
                        break
                except (OSError, ValueError, TypeError, RecursionError):
                    continue
        if not has_weights:
            issues.append("로컬 모델 가중치가 없거나 샤드 다운로드가 불완전합니다.")

        if isinstance(config, dict):
            auto_map = config.get("auto_map", {})
            if not isinstance(auto_map, dict):
                issues.append("로컬 모델 auto_map 설정이 올바르지 않습니다.")
            else:
                if not auto_map.get("AutoModelForImageSegmentation") and not config.get("model_type"):
                    issues.append("분리 모델 클래스 또는 model_type 설정이 없습니다.")
                for key in ("AutoConfig", "AutoModelForImageSegmentation"):
                    reference = auto_map.get(key)
                    if reference is None:
                        continue
                    module, separator, _class = reference.rpartition(".") if isinstance(reference, str) else ("", "", "")
                    valid = (separator and module and _class.isidentifier()
                             and all(part.isidentifier() for part in module.split(".")))
                    if not valid or not self._local_model_file(module.replace(".", "/") + ".py"):
                        issues.append(f"{key}에 필요한 로컬 코드가 없거나 외부 경로를 참조합니다.")
        missing = [name for name in ("torch", "torchvision", "transformers", "numpy")
                   if not self._dependency_available(name)]
        if missing:
            issues.append("설치되지 않은 추론 의존성: " + ", ".join(missing))
        return {"ready": not issues, "scope": "local_prerequisites_only",
                "weights_present": has_weights, "issues": issues}

    @staticmethod
    def _face_boxes(path: Path) -> list[tuple[int, int, int, int]]:
        try:
            import cv2, numpy as np
            data = np.fromfile(str(path), dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            root = Path(cv2.data.haarcascades)
            candidates = []
            for name in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml",
                         "haarcascade_profileface.xml"):
                cascade = cv2.CascadeClassifier(str(root / name))
                candidates.extend(tuple(map(int, item)) for item in
                                  cascade.detectMultiScale(gray, 1.05, 3, minSize=(28, 28)))
                if "profile" in name:
                    flipped = cv2.flip(gray, 1)
                    for x, y, w, h in cascade.detectMultiScale(flipped, 1.05, 3, minSize=(28, 28)):
                        candidates.append((gray.shape[1] - int(x) - int(w), int(y), int(w), int(h)))
            if candidates:
                # Remove near-duplicate detections while preferring the largest.
                unique = []
                for box in sorted(candidates, key=lambda item: item[2] * item[3], reverse=True):
                    cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
                    if not any(abs(cx-(old[0]+old[2]/2)) < max(box[2], old[2])*.3 and
                               abs(cy-(old[1]+old[3]/2)) < max(box[3], old[3])*.3 for old in unique):
                        unique.append(box)
                return unique
            # Last-resort eye-pair grounding is substantially safer than a
            # blind center crop for selfie/profile photographs.
            eye = cv2.CascadeClassifier(str(root / "haarcascade_eye_tree_eyeglasses.xml"))
            eyes = sorted((tuple(map(int, item)) for item in
                           eye.detectMultiScale(gray, 1.05, 3, minSize=(18, 12))),
                          key=lambda item: item[2] * item[3], reverse=True)[:6]
            best = None
            for first in eyes:
                for second in eyes:
                    if first == second:
                        continue
                    x1, y1 = first[0]+first[2]/2, first[1]+first[3]/2
                    x2, y2 = second[0]+second[2]/2, second[1]+second[3]/2
                    distance = abs(x2-x1)
                    if distance > max(first[2], second[2])*1.2 and abs(y2-y1) < distance*.45:
                        score = distance
                        if best is None or score > best[0]:
                            best = (score, min(x1,x2), (y1+y2)/2, max(x1,x2))
            if best:
                _, left, eye_y, right = best
                face_w = min(gray.shape[1], max(64, int((right-left)*2.35)))
                face_h = min(gray.shape[0], int(face_w*1.18))
                return [(max(0, int((left+right)/2-face_w/2)), max(0, int(eye_y-face_h*.38)),
                         face_w, face_h)]
            return []
        except Exception:
            return []

    def analyze(self, path: str | Path) -> dict:
        resolved = Path(path).expanduser().resolve()
        with Image.open(resolved) as source:
            width, height = source.size
        faces = self._face_boxes(resolved)
        normalized_faces = [{"x": x/width, "y": y/height, "width": w/width, "height": h/height,
                             "center_x": (x+w/2)/width, "center_y": (y+h/2)/height}
                            for x, y, w, h in faces]
        return {"path": str(resolved), "width": width, "height": height,
                "faces": normalized_faces, "primary_face": max(normalized_faces, key=lambda box: box["width"]*box["height"], default=None),
                "safe_text_regions": self._safe_regions(normalized_faces), "segmentation": self.status()}

    @staticmethod
    def _safe_regions(faces: list[dict]) -> list[dict]:
        candidates = [
            {"name": "top", "x": .08, "y": .04, "width": .84, "height": .18},
            {"name": "bottom", "x": .08, "y": .76, "width": .84, "height": .20},
            {"name": "left", "x": .04, "y": .24, "width": .28, "height": .48},
            {"name": "right", "x": .68, "y": .24, "width": .28, "height": .48},
        ]
        def overlap(a, b):
            return max(0, min(a["x"]+a["width"], b["x"]+b["width"])-max(a["x"], b["x"])) * \
                   max(0, min(a["y"]+a["height"], b["y"]+b["height"])-max(a["y"], b["y"]))
        return [{**item, "face_overlap": round(sum(overlap(item, face) for face in faces), 4)}
                for item in sorted(candidates, key=lambda item: sum(overlap(item, face) for face in faces))]

    def segment(self, path: str | Path) -> Image.Image:
        """Return a validated alpha mask, or raise instead of fabricating success.

        The compatibility return type remains PIL.Image. Backend, fallback and
        validation evidence are attached under ``mask.info['segmentation']``;
        callers needing explicit outcomes can use ``segment_with_evidence``.
        """
        result = self.segment_with_evidence(path)
        if not result.success:
            raise SegmentationError(result)
        result.mask.info["segmentation"] = result.evidence()
        return result.mask

    def segment_with_evidence(self, path: str | Path) -> SegmentationResult:
        """Attempt local backends without hiding unavailable or invalid outputs.

        Validation proves a nontrivial alpha change with both subject and
        background retained. It does not claim the mask matches a human's
        intended subject; GrabCut is explicitly an approximate fallback.
        """
        attempts = []
        try:
            resolved = Path(path).expanduser().resolve()
            with Image.open(resolved) as source:
                image = source.convert("RGB")
                original_alpha = source.convert("RGBA").getchannel("A")
        except Exception as exc:
            result = SegmentationResult(
                "failed", error=f"피사체 분리 원본을 읽지 못했습니다: {type(exc).__name__}: {exc}",
            )
            self._last_segmentation = result.evidence()
            return result

        backends = []
        readiness = self._birefnet_readiness()
        if readiness["ready"]:
            backends.append(("birefnet", self._segment_birefnet))
        else:
            attempts.append({"backend": "birefnet", "status": "unavailable",
                             "error": "; ".join(readiness["issues"]), "readiness": readiness})
        backends.append(("opencv-grabcut", self._segment_grabcut))
        for backend, run in backends:
            try:
                candidate = run(image.copy())
                mask, quality = self._validate_mask(candidate, original_alpha)
                attempts.append({"backend": backend, "status": "succeeded", "quality": quality})
                result = SegmentationResult(
                    "succeeded" if backend == "birefnet" else "fallback",
                    backend=backend, mask=mask, changed=True, quality=quality, attempts=attempts,
                )
                self._last_segmentation = result.evidence()
                return result
            except _InvalidSegmentationMask as exc:
                attempts.append({"backend": backend, "status": "rejected", "code": exc.code,
                                 "error": str(exc), "quality": exc.quality})
            except Exception as exc:
                attempts.append({"backend": backend, "status": "failed",
                                 "error": f"{type(exc).__name__}: {exc}"})
            if backend == "birefnet":
                # A failed/invalid neural inference must not leave a broken
                # model resident while the CPU fallback attempts recovery.
                try:
                    self.release()
                except Exception as exc:
                    attempts[-1]["release_error"] = f"{type(exc).__name__}: {exc}"

        unchanged = any(attempt.get("code") == "unchanged" for attempt in attempts)
        reason = "; ".join(f"{item['backend']}: {item.get('error', item['status'])}" for item in attempts)
        result = SegmentationResult(
            "unchanged" if unchanged else "failed", attempts=attempts,
            error="피사체 분리에 유효한 변경 마스크를 만들지 못했습니다. " + reason,
        )
        self._last_segmentation = result.evidence()
        return result

    @staticmethod
    def _validate_mask(mask: Image.Image, original_alpha: Image.Image) -> tuple[Image.Image, dict]:
        if not isinstance(mask, Image.Image) or mask.mode not in {"1", "L"}:
            raise _InvalidSegmentationMask("분리 결과가 단일 채널 알파 마스크가 아닙니다.", code="invalid_mask")
        if mask.size != original_alpha.size:
            raise _InvalidSegmentationMask("분리 마스크와 원본 이미지 크기가 다릅니다.", code="size_mismatch")
        mask = mask.convert("L")
        histogram = mask.histogram()
        total = mask.width * mask.height
        background = sum(histogram[:17]) / total
        foreground = sum(histogram[33:]) / total
        low, high = mask.getextrema()
        quality = {"alpha_min": low, "alpha_max": high,
                   "background_fraction": background, "foreground_fraction": foreground}
        if foreground < .01:
            raise _InvalidSegmentationMask("분리 마스크에 피사체가 남지 않았습니다.", code="empty_foreground", quality=quality)
        if background < .01:
            raise _InvalidSegmentationMask("분리 마스크가 배경을 제거하지 않았습니다.", code="no_background_removed", quality=quality)
        if high - low < 32:
            raise _InvalidSegmentationMask("분리 마스크에 유효한 전경·배경 구분이 없습니다.", code="flat_mask", quality=quality)
        # Intersect with input transparency; never resurrect transparent pixels
        # or double-attenuate an existing partially transparent subject edge.
        result = ImageChops.darker(mask, original_alpha)
        retained = sum(result.histogram()[33:]) / total
        changed = sum(ImageChops.subtract(original_alpha, result).histogram()[17:]) / total
        quality.update({"retained_foreground_fraction": retained, "changed_fraction": changed,
                        "validation_scope": "alpha_structure_only"})
        if retained < .01:
            raise _InvalidSegmentationMask("원본 투명도와 결합한 후 피사체가 남지 않았습니다.", code="empty_foreground", quality=quality)
        if changed < .005:
            raise _InvalidSegmentationMask("분리 결과가 원본 알파와 실질적으로 동일합니다.", code="unchanged", quality=quality)
        return result, quality

    def _segment_birefnet(self, image: Image.Image) -> Image.Image:
        import numpy as np
        import torch
        from torchvision import transforms
        from transformers import AutoModelForImageSegmentation

        if self._model is None:
            self._model = AutoModelForImageSegmentation.from_pretrained(
                str(self.model_dir), trust_remote_code=True, local_files_only=True).eval()
            if torch.cuda.is_available():
                self._model.to("cuda")
        tensor = transforms.Compose([transforms.Resize((1024, 1024)), transforms.ToTensor(),
                                     transforms.Normalize([.485, .456, .406], [.229, .224, .225])])(image).unsqueeze(0)
        if torch.cuda.is_available():
            tensor = tensor.cuda()
        with torch.inference_mode():
            prediction = self._model(tensor)[-1].sigmoid()[0, 0]
        alpha = prediction.float().cpu().numpy()
        if alpha.ndim != 2 or not np.isfinite(alpha).all():
            raise ValueError("BiRefNet이 유한한 2차원 알파 예측을 반환하지 않았습니다.")
        mask = Image.fromarray((np.clip(alpha, 0, 1) * 255).astype("uint8"))
        return mask.resize(image.size, Image.Resampling.LANCZOS)

    @staticmethod
    def _segment_grabcut(image: Image.Image) -> Image.Image:
        import cv2
        import numpy as np

        width, height = image.size
        margin_x, margin_y = max(1, width // 20), max(1, height // 20)
        rect = (margin_x, margin_y, width - margin_x * 2, height - margin_y * 2)
        if rect[2] <= 0 or rect[3] <= 0:
            raise ValueError("GrabCut으로 분리하기에 이미지가 너무 작습니다.")
        pixels = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        labels = np.zeros((height, width), np.uint8)
        bg, fg = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
        cv2.grabCut(pixels, labels, rect, bg, fg, 5, cv2.GC_INIT_WITH_RECT)
        alpha = ((labels == 1) | (labels == 3)).astype("uint8") * 255
        return Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(1.2))

    def release(self):
        self._model = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available(): torch.cuda.empty_cache()
        except Exception: pass
