"""Lazy subject segmentation, face grounding and visual-safe-area evidence."""
from __future__ import annotations

import gc
from pathlib import Path

from PIL import Image, ImageFilter


class SubjectAnalysisRuntime:
    BIREFNET_ID = "ZhengPeng7/BiRefNet"

    def __init__(self, model_dir: str | Path = "data/models/birefnet"):
        self.model_dir = Path(model_dir).resolve()
        self._model = None

    def status(self) -> dict:
        return {"backend": "birefnet" if (self.model_dir / "config.json").is_file() else "opencv-grabcut",
                "birefnet_ready": (self.model_dir / "config.json").is_file(),
                "model_dir": str(self.model_dir)}

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
        """Return an alpha mask; BiRefNet when prepared, conservative GrabCut otherwise."""
        resolved = Path(path).expanduser().resolve()
        if (self.model_dir / "config.json").is_file():
            try:
                import torch
                from torchvision import transforms
                from transformers import AutoModelForImageSegmentation
                if self._model is None:
                    self._model = AutoModelForImageSegmentation.from_pretrained(
                        str(self.model_dir), trust_remote_code=True, local_files_only=True).eval()
                    if torch.cuda.is_available(): self._model.to("cuda")
                with Image.open(resolved) as source: image = source.convert("RGB")
                tensor = transforms.Compose([transforms.Resize((1024, 1024)), transforms.ToTensor(),
                                             transforms.Normalize([.485,.456,.406],[.229,.224,.225])])(image).unsqueeze(0)
                if torch.cuda.is_available(): tensor = tensor.cuda()
                with torch.inference_mode(): prediction = self._model(tensor)[-1].sigmoid()[0, 0]
                mask = Image.fromarray((prediction.float().cpu().numpy() * 255).astype("uint8"))
                return mask.resize(image.size, Image.Resampling.LANCZOS)
            except Exception:
                pass
        try:
            import cv2, numpy as np
            image = cv2.imdecode(np.fromfile(str(resolved), dtype=np.uint8), cv2.IMREAD_COLOR)
            mask = np.zeros(image.shape[:2], np.uint8)
            bg, fg = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
            h, w = image.shape[:2]
            cv2.grabCut(image, mask, (max(1,w//20), max(1,h//20), max(1,w*9//10), max(1,h*9//10)), bg, fg, 5, cv2.GC_INIT_WITH_RECT)
            alpha = ((mask == 1) | (mask == 3)).astype("uint8") * 255
            return Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(1.2))
        except Exception:
            with Image.open(resolved) as source:
                return Image.new("L", source.size, 255)

    def release(self):
        self._model = None
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available(): torch.cuda.empty_cache()
        except Exception: pass
