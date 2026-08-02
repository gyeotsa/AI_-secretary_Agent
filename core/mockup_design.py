"""Reference-style learning and deterministic local mockup rendering."""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from core.vision_runtime import VisionRuntime


SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


@dataclass
class MockupStyleProfile:
    profile_id: str
    name: str
    reference_paths: list[str]
    reference_hashes: list[str]
    palette: list[str]
    median_aspect_ratio: float
    orientation: str
    vision_analysis: str
    generation_prompt: str = ""
    created_at: float = field(default_factory=time.time)


class MockupDesignRuntime:
    """Keeps measured visual traits separate from unverified Vision interpretation."""

    def __init__(self, profile_dir: str | Path = "data/mockup_styles", vision=None,
                 generation_backend=None):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.vision = vision
        if generation_backend is None:
            from core.mockup_generation import IPAdapterGenerationBackend
            generation_backend = IPAdapterGenerationBackend()
        self.generation_backend = generation_backend

    @staticmethod
    def _validate_images(paths) -> list[Path]:
        images = []
        for value in paths:
            path = Path(value).expanduser().resolve()
            if not path.is_file() or path.suffix.casefold() not in SUPPORTED_IMAGES:
                raise ValueError(f"지원하는 이미지 파일이 아닙니다: {path}")
            images.append(path)
        if not images:
            raise ValueError("이미지를 한 장 이상 추가해 주세요.")
        return images

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _palette(paths: list[Path], colors: int = 6) -> list[str]:
        samples = []
        for path in paths:
            with Image.open(path) as image:
                rgb = image.convert("RGB")
                rgb.thumbnail((320, 320))
                samples.append(rgb.copy())
        width = max(image.width for image in samples)
        height = sum(image.height for image in samples)
        atlas = Image.new("RGB", (width, height), "white")
        y = 0
        for image in samples:
            atlas.paste(image, (0, y)); y += image.height
        quantized = atlas.quantize(colors=colors, method=Image.Quantize.MEDIANCUT).convert("RGB")
        ranked = sorted(quantized.getcolors(quantized.width * quantized.height) or [], reverse=True)
        return ["#%02x%02x%02x" % color for _count, color in ranked[:colors]]

    def learn_style(self, reference_paths, *, name: str = "새 시안 스타일") -> MockupStyleProfile:
        paths = self._validate_images(reference_paths)
        ratios = []
        for path in paths:
            with Image.open(path) as image:
                ratios.append(image.width / max(1, image.height))
        ratio = float(median(ratios))
        orientation = "landscape" if ratio > 1.08 else "portrait" if ratio < 0.92 else "square"
        analysis = ""
        try:
            vision = self.vision or VisionRuntime()
            result = vision.analyze(
                [str(path) for path in paths[:12]],
                "이 이미지들은 같은 계열의 시안 참고 자료입니다. 공통 레이아웃 구조, 이미지 배치, "
                "타이포그래피 위계, 여백, 색상 분위기, 테두리와 장식 요소를 한국어로 분석하세요. "
                "사진 속 인물이나 제품의 정체가 아니라 재사용 가능한 디자인 규칙에 집중하세요. "
                "마지막 줄에는 이미지 생성 모델이 사용할 짧은 영문 스타일 프롬프트를 "
                "GENERATION_PROMPT_EN: 뒤에 작성하세요. 문자·로고·인물 정체는 포함하지 마세요.",
                mode="general",
            )
            analysis = str(result.get("analysis", ""))
        except Exception as exc:
            analysis = f"Vision 정성 분석을 수행하지 못했습니다: {exc}"
        prompt_match = __import__("re").search(r"GENERATION_PROMPT_EN\s*:\s*(.+)", analysis, __import__("re").IGNORECASE)
        generation_prompt = (prompt_match.group(1).strip() if prompt_match else
                             f"{orientation} editorial layout, palette {', '.join(self._palette(paths))}, clean spacing")
        palette = self._palette(paths)
        profile = MockupStyleProfile(
            profile_id=uuid.uuid4().hex,
            name=" ".join(str(name).split()) or "새 시안 스타일",
            reference_paths=[str(path) for path in paths],
            reference_hashes=[self._sha256(path) for path in paths],
            palette=palette, median_aspect_ratio=ratio,
            orientation=orientation, vision_analysis=analysis,
            generation_prompt=generation_prompt,
        )
        target = self.profile_dir / f"{profile.profile_id}.json"
        target.write_text(json.dumps(asdict(profile), ensure_ascii=False, indent=2), encoding="utf-8")
        return profile

    def load_profile(self, profile_id: str) -> MockupStyleProfile:
        path = self.profile_dir / f"{profile_id}.json"
        if not path.is_file():
            raise ValueError(f"시안 스타일 프로필이 없습니다: {profile_id}")
        return MockupStyleProfile(**json.loads(path.read_text(encoding="utf-8")))

    def list_profiles(self) -> list[MockupStyleProfile]:
        profiles = []
        for path in sorted(self.profile_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try: profiles.append(MockupStyleProfile(**json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError, json.JSONDecodeError): continue
        return profiles

    def generation_status(self) -> dict:
        return self.generation_backend.status()

    def prepare_generation_models(self, progress=None) -> dict:
        return self.generation_backend.prepare(progress)

    @staticmethod
    def _font(size: int, bold: bool = False):
        candidates = [
            Path("C:/Windows/Fonts/malgunbd.ttf" if bold else "C:/Windows/Fonts/malgun.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        ]
        for path in candidates:
            if path.is_file(): return ImageFont.truetype(str(path), size)
        return ImageFont.load_default()

    @staticmethod
    def _hex(value: str, fallback=(15, 23, 42)):
        try:
            value = value.lstrip("#")
            return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))
        except Exception:
            return fallback

    def render(self, profile_id: str, production_paths, *, instruction: str = "",
               output_dir: str | Path = "data/mockup_outputs", basename: str = "mockup",
               backend: str = "auto", seed: int = 42) -> dict:
        profile = self.load_profile(profile_id)
        paths = self._validate_images(production_paths)
        ratio = max(0.55, min(1.9, profile.median_aspect_ratio))
        if ratio >= 1:
            width, height = 1600, max(900, int(1600 / ratio))
        else:
            width, height = max(900, int(1600 * ratio)), 1600
        palette = [self._hex(value) for value in profile.palette] or [(15, 23, 42), (34, 211, 238)]
        use_generative = backend == "generative" or (
            backend == "auto" and self.generation_backend.status().get("ready", False)
        )
        generation_error = ""
        if use_generative:
            try:
                background = self.generation_backend.generate_background(
                    reference_paths=profile.reference_paths,
                    prompt=f"{profile.generation_prompt}. {instruction}",
                    orientation=profile.orientation, seed=seed,
                )
                background = ImageOps.fit(background, (width, height), method=Image.Resampling.LANCZOS)
                renderer_name = "sd15-ip-adapter-plus+preserved-photo-layout-v1"
                tint_alpha = 75
            except Exception as exc:
                if backend == "generative":
                    raise
                generation_error = str(exc)
                use_generative = False
        if not use_generative:
            with Image.open(paths[0]) as background_source:
                background = ImageOps.fit(background_source.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)
            background = background.filter(ImageFilter.GaussianBlur(max(12, width // 55)))
            renderer_name = "pillow-layout-v1"
            tint_alpha = 185
        tint = Image.new("RGBA", (width, height), (*palette[0], tint_alpha))
        canvas = Image.alpha_composite(background.convert("RGBA"), tint)
        draw = ImageDraw.Draw(canvas, "RGBA")
        margin = int(min(width, height) * 0.065)
        title_height = int(height * 0.18)
        draw.rounded_rectangle(
            (margin, margin, width - margin, height - margin), radius=34,
            fill=(*palette[0], 120), outline=(*palette[min(1, len(palette)-1)], 230), width=4,
        )
        title = (instruction.strip().splitlines()[0] if instruction.strip() else profile.name)[:70]
        draw.text((margin * 1.45, margin * 1.35), title, font=self._font(max(30, width // 28), True), fill=(255, 255, 255, 245))
        if instruction.strip() and len(instruction.strip().splitlines()) > 1:
            subtitle = " ".join(instruction.strip().splitlines()[1:])[:120]
            draw.text((margin * 1.45, margin * 1.35 + width // 22), subtitle,
                      font=self._font(max(18, width // 50)), fill=(225, 235, 245, 220))
        content_top, content_bottom = margin + title_height, height - margin * 2
        count = len(paths)
        columns = 1 if count == 1 else 2 if count <= 4 else 3
        rows = math.ceil(count / columns)
        gap = max(18, margin // 3)
        cell_w = (width - margin * 3 - gap * (columns - 1)) // columns
        cell_h = (content_bottom - content_top - gap * (rows - 1)) // rows
        for index, path in enumerate(paths):
            row, column = divmod(index, columns)
            x = int(margin * 1.5 + column * (cell_w + gap))
            y = int(content_top + row * (cell_h + gap))
            with Image.open(path) as source:
                card = ImageOps.fit(source.convert("RGB"), (cell_w, cell_h), method=Image.Resampling.LANCZOS)
            mask = Image.new("L", (cell_w, cell_h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, cell_w, cell_h), radius=28, fill=255)
            shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle((x + 12, y + 14, x + cell_w + 12, y + cell_h + 14), radius=28, fill=(0, 0, 0, 105))
            canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(14)))
            canvas.paste(card.convert("RGBA"), (x, y), mask)
            ImageDraw.Draw(canvas).rounded_rectangle((x, y, x + cell_w, y + cell_h), radius=28,
                                                     outline=(*palette[min(1, len(palette)-1)], 235), width=4)
        output_root = Path(output_dir).expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(character for character in basename if character.isalnum() or character in "-_ ").strip() or "mockup"
        output_path = output_root / f"{safe_name}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        canvas.convert("RGB").save(output_path, format="PNG", optimize=True)
        metadata = {
            "output": str(output_path), "profile_id": profile.profile_id,
            "references": profile.reference_hashes, "production_inputs": [self._sha256(path) for path in paths],
            "width": width, "height": height, "renderer": renderer_name,
            "generation_backend": "generative" if use_generative else "local",
            "seed": int(seed),
            "generation_fallback_reason": generation_error,
            "note": ("IP-Adapter 참고 스타일 배경과 원본 사진 보존 합성을 적용한 결과"
                     if use_generative else "정량 스타일 특성과 로컬 합성 레이아웃을 적용한 결과"),
        }
        output_path.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata
