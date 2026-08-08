"""Reference-style learning and deterministic local mockup rendering."""
from __future__ import annotations

import hashlib
import json
import time
import shutil
import tempfile
import uuid
import math
import re
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
    design_recipe: dict = field(default_factory=dict)
    style_features: dict = field(default_factory=dict)
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
        measurements = [self._measure_reference(path) for path in paths]
        recipe = self._consensus_recipe(measurements, ratio, orientation)
        analysis = ""
        try:
            vision = self.vision or VisionRuntime()
            result = vision.analyze(
                [str(path) for path in paths[:12]],
                "각 이미지는 서로 다른 참고 시안입니다. 여러 장을 한 장의 콜라주로 해석하지 마세요. "
                "인물의 정체가 아니라 모든 이미지에 반복되는 디자인 문법만 찾으세요. 이미지별 관찰과 공통점을 "
                "분리하고, 캔버스 형태·주 피사체 마스크·테두리·문구 위치·사진 점유율을 설명하세요. "
                f"컴퓨터 비전 계측 결과는 {json.dumps(measurements, ensure_ascii=False)} 입니다. 계측과 충돌하는 "
                "추측은 하지 마세요. 마지막 줄에는 문자·로고·고유 인물을 제외한 영문 생성 프롬프트를 "
                "`GENERATION_PROMPT_EN:` 뒤에 작성하세요.", mode="general")
            analysis = str(result.get("analysis", ""))
        except Exception as exc:
            analysis = f"Vision 정성 분석을 수행하지 못했습니다: {exc}"
        structural_summary = self._recipe_summary(recipe)
        analysis = f"[구조 계측 · 렌더링 기준]\n{structural_summary}\n\n[Vision 보조 분석]\n{analysis}"
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
            design_recipe={
                "analysis": analysis,
                "priority": ["user_instruction", "production_asset_identity", "reference_design_language"],
                "aspect_ratio": ratio,
                "palette": palette,
                **recipe,
            },
            style_features={"references": measurements, "consensus": recipe},
        )
        target = self.profile_dir / f"{profile.profile_id}.json"
        target.write_text(json.dumps(asdict(profile), ensure_ascii=False, indent=2), encoding="utf-8")
        return profile

    def load_profile(self, profile_id: str) -> MockupStyleProfile:
        path = self.profile_dir / f"{profile_id}.json"
        if not path.is_file():
            raise ValueError(f"시안 스타일 프로필이 없습니다: {profile_id}")
        profile = MockupStyleProfile(**json.loads(path.read_text(encoding="utf-8")))
        if (not profile.design_recipe.get("layout_family")
                or not profile.design_recipe.get("structural_summary")):
            existing = [Path(value) for value in profile.reference_paths if Path(value).is_file()]
            if existing:
                measurements = [self._measure_reference(item) for item in existing]
                recipe = self._consensus_recipe(
                    measurements, profile.median_aspect_ratio, profile.orientation
                )
                profile.design_recipe.update(recipe)
                if "[구조 계측 · 렌더링 기준]" not in profile.vision_analysis:
                    profile.vision_analysis = (
                        f"[구조 계측 · 렌더링 기준]\n{self._recipe_summary(recipe)}\n\n"
                        f"[이전 Vision 분석 · 구조와 충돌하면 사용하지 않음]\n{profile.vision_analysis}"
                    )
                profile.style_features = {"references": measurements, "consensus": recipe}
                path.write_text(json.dumps(asdict(profile), ensure_ascii=False, indent=2), encoding="utf-8")
        return profile

    def list_profiles(self) -> list[MockupStyleProfile]:
        profiles = []
        for path in sorted(self.profile_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try: profiles.append(MockupStyleProfile(**json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError, json.JSONDecodeError): continue
        return profiles

    @staticmethod
    def _measure_reference(path: Path) -> dict:
        """Measure reusable geometry. This deliberately ignores people and readable copy."""
        with Image.open(path) as source:
            image = source.convert("RGB")
            width, height = image.size
        circle = None
        try:
            import cv2
            import numpy as np
            array = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2GRAY)
            array = cv2.medianBlur(array, 7)
            minimum = min(width, height)
            found = cv2.HoughCircles(
                array, cv2.HOUGH_GRADIENT, dp=1.2, minDist=max(40, minimum // 3),
                param1=100, param2=45, minRadius=int(minimum * .32), maxRadius=int(minimum * .52),
            )
            if found is not None:
                candidates = sorted(found[0], key=lambda value: value[2], reverse=True)
                x, y, radius = candidates[0]
                centered = abs(float(x) / width - .5) < .12 and abs(float(y) / height - .5) < .12
                if centered:
                    circle = {"cx": round(float(x) / width, 3), "cy": round(float(y) / height, 3),
                              "radius": round(float(radius) / minimum, 3)}
        except (ImportError, OSError, ValueError):
            circle = None
        return {
            "width": width, "height": height, "aspect_ratio": round(width / max(1, height), 4),
            "square": abs(width / max(1, height) - 1.0) <= .08,
            "large_center_circle": circle is not None, "circle": circle,
        }

    @staticmethod
    def _consensus_recipe(measurements: list[dict], ratio: float, orientation: str) -> dict:
        total = max(1, len(measurements))
        circle_votes = sum(bool(item.get("large_center_circle")) for item in measurements)
        square_votes = sum(bool(item.get("square")) for item in measurements)
        circular = circle_votes / total >= .6 and square_votes / total >= .6
        radii = [item["circle"]["radius"] for item in measurements if item.get("circle")]
        recipe = {
            "schema_version": 2,
            "layout_family": "circular_sticker" if circular else "editorial_composite",
            "canvas_shape": "square" if square_votes / total >= .6 else orientation,
            "primary_frame": "circle" if circular else "freeform",
            "primary_frame_radius": round(float(median(radii)), 3) if radii else None,
            "subject_strategy": "single_hero_or_cluster",
            "subject_scale": .78 if circular else .62,
            "border_style": "dashed_inner_ring" if circular else "subtle_frame",
            "text_policy": "explicit_copy_only",
            "text_region": "lower_overlay" if circular else "layout_defined",
            "reference_pixels_allowed_in_output": False,
            "confidence": round(max(circle_votes, square_votes) / total, 3),
            "evidence": {"reference_count": total, "circle_votes": circle_votes,
                         "square_votes": square_votes},
        }
        recipe["structural_summary"] = MockupDesignRuntime._recipe_summary(recipe)
        return recipe

    @staticmethod
    def _recipe_summary(recipe: dict) -> str:
        evidence = recipe.get("evidence", {})
        if recipe.get("layout_family") == "circular_sticker":
            return (
                f"참고 {evidence.get('reference_count', 0)}장 중 원형 구조 "
                f"{evidence.get('circle_votes', 0)}장·정사각 캔버스 {evidence.get('square_votes', 0)}장. "
                "큰 중앙 원 안에 제작 사진을 배치하고, 안쪽 점선 링과 하단 문구 영역을 사용한다. "
                "참고 이미지 픽셀은 결과 배경으로 재사용하지 않으며 표시 문구는 별도 입력된 경우에만 넣는다."
            )
        return (
            f"참고 {evidence.get('reference_count', 0)}장의 공통 비율과 색상을 사용하는 편집 구성. "
            "제작 사진의 정체성을 보존하고 참고 이미지 픽셀은 결과에 복사하지 않는다."
        )

    def _composition_plan(self, profile, paths: list[Path], instruction: str) -> list[dict]:
        """Ask the vision model for normalized placements; use a safe editorial fallback."""
        try:
            vision = self.vision or VisionRuntime()
            response = vision.analyze(
                [*profile.reference_paths[:8], *[str(path) for path in paths[:8]]],
                "앞쪽 이미지들은 참고 시안이고 뒤쪽 이미지들은 실제 제작 소재입니다. 참고 시안의 디자인 문법을 "
                "재해석하되 제작 소재의 정체와 비율을 보존하세요. 사용자 지시가 최우선입니다: "
                f"{instruction or '추가 지시 없음'}. 제작 소재 {len(paths)}개 각각의 배치를 JSON 배열로만 반환하세요. "
                "각 객체는 index, x, y, width, height(모두 0~1), fit(contain 또는 cover), rotation(-15~15)을 가집니다. "
                "지시문 자체를 이미지 글자로 배치하지 마세요.", mode="general")
            raw = str(response.get("analysis", ""))
            match = __import__("re").search(r"\[[\s\S]*\]", raw)
            data = json.loads(match.group(0)) if match else []
            plan = []
            for item in data:
                index = int(item.get("index", -1))
                if 0 <= index < len(paths):
                    plan.append({
                        "index": index,
                        "x": max(0.0, min(.9, float(item.get("x", 0)))),
                        "y": max(0.0, min(.9, float(item.get("y", 0)))),
                        "width": max(.12, min(1.0, float(item.get("width", .5)))),
                        "height": max(.12, min(1.0, float(item.get("height", .5)))),
                        "fit": "contain" if item.get("fit") == "contain" else "cover",
                        "rotation": max(-15.0, min(15.0, float(item.get("rotation", 0)))),
                    })
            if {item["index"] for item in plan} == set(range(len(paths))):
                return plan
        except Exception:
            pass
        # Balanced fallback, not a fixed text/banner template: one hero plus
        # progressively smaller supporting assets with deliberate overlap.
        if len(paths) == 1:
            return [{"index": 0, "x": .10, "y": .10, "width": .80, "height": .80,
                     "fit": "contain", "rotation": 0}]
        plan = [{"index": 0, "x": .06, "y": .10, "width": .58, "height": .80,
                 "fit": "contain", "rotation": -2}]
        supporting = len(paths) - 1
        for offset, index in enumerate(range(1, len(paths))):
            height = .72 / supporting
            plan.append({"index": index, "x": .58, "y": .10 + offset * height,
                         "width": .36, "height": min(.34, height + .04),
                         "fit": "contain", "rotation": 2 if offset % 2 == 0 else -2})
        return plan

    def delete_profile(self, profile_id: str) -> bool:
        """Delete one saved style profile without touching its source images."""
        normalized = str(profile_id or "").strip()
        if not normalized or any(ch not in "0123456789abcdefABCDEF" for ch in normalized):
            raise ValueError("올바르지 않은 스타일 프로필 ID입니다.")
        target = (self.profile_dir / f"{normalized}.json").resolve()
        if target.parent != self.profile_dir.resolve():
            raise ValueError("스타일 프로필 경로가 올바르지 않습니다.")
        if not target.is_file():
            return False
        target.unlink()
        return True

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

    @staticmethod
    def _visible_copy(instruction: str) -> str:
        """Only render copy that the user explicitly labels as visible text."""
        value = str(instruction or "").strip()
        patterns = (
            r"(?:문구|텍스트|글자|카피)\s*[:：]\s*([^\n]+)",
            r"[\"“](.+?)[\"”]\s*(?:라고|이라는?)\s*(?:써|적어|넣어|표시)",
        )
        for pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if match:
                return " ".join(match.group(1).split())[:120]
        return ""

    @staticmethod
    def _accent_palette(palette: list[tuple[int, int, int]]) -> tuple[int, int, int]:
        candidates = [color for color in palette if sum(color) < 705]
        return max(candidates or palette or [(70, 160, 220)],
                   key=lambda color: max(color) - min(color))

    def _render_circular_sticker(self, paths: list[Path], palette, instruction: str,
                                 visible_copy: str = "",
                                 size: int = 1600) -> tuple[Image.Image, list[dict], str]:
        """Preserve source photos while applying the learned circular sticker grammar."""
        canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        margin = int(size * .018); diameter = size - margin * 2
        circle_mask = Image.new("L", (diameter, diameter), 0)
        ImageDraw.Draw(circle_mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
        plan = []
        if len(paths) == 1:
            with Image.open(paths[0]) as source:
                hero = ImageOps.fit(source.convert("RGBA"), (diameter, diameter),
                                    method=Image.Resampling.LANCZOS, centering=(.5, .45))
            if hero.getchannel("A").getextrema()[0] < 255:
                base = Image.new("RGBA", hero.size, (*self._accent_palette(palette), 255))
                base.alpha_composite(hero); hero = base
            canvas.paste(hero, (margin, margin), Image.composite(circle_mask, Image.new("L", circle_mask.size), hero.getchannel("A")))
            plan.append({"index": 0, "shape": "circle", "role": "hero", "x": margin / size,
                         "y": margin / size, "width": diameter / size, "height": diameter / size,
                         "fit": "cover", "rotation": 0})
        else:
            background = Image.new("RGBA", (diameter, diameter), (*self._accent_palette(palette), 255))
            canvas.paste(background, (margin, margin), circle_mask)
            count = len(paths); orbit = diameter * .24
            item_diameter = int(diameter * min(.48, .76 / math.sqrt(count)))
            for index, path in enumerate(paths):
                angle = -math.pi / 2 + 2 * math.pi * index / count
                cx = size / 2 + math.cos(angle) * orbit
                cy = size / 2 + math.sin(angle) * orbit * .82
                x, y = int(cx - item_diameter / 2), int(cy - item_diameter / 2)
                with Image.open(path) as source:
                    portrait = ImageOps.fit(source.convert("RGBA"), (item_diameter, item_diameter),
                                            method=Image.Resampling.LANCZOS, centering=(.5, .42))
                mask = Image.new("L", portrait.size, 0); ImageDraw.Draw(mask).ellipse(
                    (0, 0, item_diameter - 1, item_diameter - 1), fill=255)
                canvas.paste(portrait, (x, y), mask)
                ImageDraw.Draw(canvas).ellipse((x, y, x + item_diameter, y + item_diameter),
                                               outline=(255, 255, 255, 235), width=max(5, size // 180))
                plan.append({"index": index, "shape": "circle", "role": "cluster_subject",
                             "x": x / size, "y": y / size, "width": item_diameter / size,
                             "height": item_diameter / size, "fit": "cover", "rotation": 0})

        draw = ImageDraw.Draw(canvas, "RGBA")
        inset = int(size * .066); box = (inset, inset, size - inset, size - inset)
        dash_count = 36
        for index in range(dash_count):
            start = index * 360 / dash_count + 1.5
            draw.arc(box, start=start, end=start + 5.8, fill=(255, 255, 255, 245), width=max(7, size // 130))

        visible_copy = " ".join(str(visible_copy or "").split())[:120] or self._visible_copy(instruction)
        if visible_copy:
            lines = [part.strip() for part in re.split(r"[|/]", visible_copy) if part.strip()]
            if len(lines) == 1 and len(lines[0]) > 15:
                words, built = lines[0].split(), []
                current = ""
                for word in words:
                    candidate = f"{current} {word}".strip()
                    if len(candidate) > 15 and current:
                        built.append(current); current = word
                    else: current = candidate
                if current: built.append(current)
                lines = built[:3]
            band_top = int(size * (.66 if len(lines) <= 2 else .60))
            draw.rectangle((margin, band_top, size - margin, int(size * .89)), fill=(255, 255, 255, 205))
            available_w = int(size * .78); available_h = int(size * .22)
            font_size = int(size * .075)
            while font_size > 34:
                font = self._font(font_size, bold=True)
                boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=2) for line in lines]
                if max(box[2] for box in boxes) <= available_w and len(lines) * font_size * 1.16 <= available_h:
                    break
                font_size -= 4
            total_h = int(len(lines) * font_size * 1.14); y = band_top + (int(size * .89) - band_top - total_h) // 2
            accent = self._accent_palette(palette)
            for index, line in enumerate(lines):
                box_text = draw.textbbox((0, 0), line, font=font, stroke_width=2)
                x = (size - (box_text[2] - box_text[0])) // 2
                color = (*accent, 255) if index == len(lines) - 1 and len(lines) > 1 else (15, 15, 18, 255)
                draw.text((x, y), line, font=font, fill=color, stroke_width=max(1, size // 500),
                          stroke_fill=(255, 255, 255, 230))
                y += int(font_size * 1.14)
        return canvas, plan, visible_copy

    def render(self, profile_id: str, production_paths, *, instruction: str = "",
               visible_copy: str = "",
               output_dir: str | Path = "data/mockup_outputs", basename: str = "mockup",
               backend: str = "auto", seed: int = 42, preview_only: bool = False) -> dict:
        profile = self.load_profile(profile_id)
        paths = self._validate_images(production_paths)
        ratio = max(0.55, min(1.9, profile.median_aspect_ratio))
        if ratio >= 1:
            width, height = 1600, max(900, int(1600 / ratio))
        else:
            width, height = max(900, int(1600 * ratio)), 1600
        palette = [self._hex(value) for value in profile.palette] or [(15, 23, 42), (34, 211, 238)]
        if profile.design_recipe.get("layout_family") == "circular_sticker":
            width = height = 1600
            canvas, plan, visible_copy = self._render_circular_sticker(
                paths, palette, instruction, visible_copy, width
            )
            output_root = (Path(tempfile.gettempdir()) / "jarvis_mockup_previews" if preview_only
                           else Path(output_dir).expanduser().resolve())
            output_root.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(character for character in basename if character.isalnum() or character in "-_ ").strip() or "mockup"
            suffix = uuid.uuid4().hex if preview_only else time.strftime('%Y%m%d_%H%M%S')
            output_path = output_root / f"{safe_name}_{suffix}.png"
            canvas.convert("RGB").save(output_path, format="PNG", optimize=True)
            metadata = {
                "output": str(output_path), "profile_id": profile.profile_id,
                "references": profile.reference_hashes,
                "production_inputs": [self._sha256(path) for path in paths],
                "width": width, "height": height, "renderer": "structured-circular-sticker-v2",
                "generation_backend": "structured_local", "seed": int(seed),
                "composition_plan": plan, "instruction": instruction.strip(),
                "visible_copy": visible_copy, "preview_only": bool(preview_only),
                "generation_fallback_reason": "", "style_recipe": profile.design_recipe,
                "quality_checks": {
                    "reference_pixels_reused": False, "production_assets_present": len(plan) == len(paths),
                    "layout_family_matched": True, "unrequested_text_rendered": False,
                },
                "note": "참고 시안에서 계측한 원형 스티커 문법과 원본 제작 사진을 적용한 결과",
            }
            if not preview_only:
                output_path.with_suffix(".json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return metadata
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
        draw.rounded_rectangle(
            (margin, margin, width - margin, height - margin), radius=34,
            fill=(*palette[0], 120), outline=(*palette[min(1, len(palette)-1)], 230), width=4,
        )
        # `instruction` is a design directive, never implicit visible copy. Text is
        # only painted by an explicit text-element editor operation.
        plan = self._composition_plan(profile, paths, instruction)
        inner_x, inner_y = margin, margin
        inner_w, inner_h = width - margin * 2, height - margin * 2
        for placement in plan:
            path = paths[placement["index"]]
            cell_w = max(80, int(inner_w * placement["width"]))
            cell_h = max(80, int(inner_h * placement["height"]))
            x = min(width - margin - cell_w, int(inner_x + inner_w * placement["x"]))
            y = min(height - margin - cell_h, int(inner_y + inner_h * placement["y"]))
            with Image.open(path) as source:
                source = source.convert("RGBA")
                if placement["fit"] == "contain":
                    card = ImageOps.contain(source, (cell_w, cell_h), method=Image.Resampling.LANCZOS)
                    transparent = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
                    transparent.paste(card, ((cell_w-card.width)//2, (cell_h-card.height)//2), card)
                    card = transparent
                else:
                    card = ImageOps.fit(source, (cell_w, cell_h), method=Image.Resampling.LANCZOS)
                if placement["rotation"]:
                    card = card.rotate(placement["rotation"], expand=False, resample=Image.Resampling.BICUBIC)
            mask = Image.new("L", (cell_w, cell_h), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, cell_w, cell_h), radius=28, fill=255)
            if card.mode == "RGBA":
                mask = Image.composite(mask, Image.new("L", mask.size, 0), card.getchannel("A"))
            shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            ImageDraw.Draw(shadow).rounded_rectangle((x + 12, y + 14, x + cell_w + 12, y + cell_h + 14), radius=28, fill=(0, 0, 0, 105))
            canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(14)))
            canvas.paste(card.convert("RGBA"), (x, y), mask)
            ImageDraw.Draw(canvas).rounded_rectangle((x, y, x + cell_w, y + cell_h), radius=28,
                                                     outline=(*palette[min(1, len(palette)-1)], 235), width=4)
        output_root = (Path(tempfile.gettempdir()) / "jarvis_mockup_previews" if preview_only
                       else Path(output_dir).expanduser().resolve())
        output_root.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(character for character in basename if character.isalnum() or character in "-_ ").strip() or "mockup"
        suffix = uuid.uuid4().hex if preview_only else time.strftime('%Y%m%d_%H%M%S')
        output_path = output_root / f"{safe_name}_{suffix}.png"
        canvas.convert("RGB").save(output_path, format="PNG", optimize=True)
        metadata = {
            "output": str(output_path), "profile_id": profile.profile_id,
            "references": profile.reference_hashes, "production_inputs": [self._sha256(path) for path in paths],
            "width": width, "height": height, "renderer": renderer_name,
            "generation_backend": "generative" if use_generative else "local",
            "seed": int(seed),
            "composition_plan": plan,
            "instruction": instruction.strip(),
            "preview_only": bool(preview_only),
            "generation_fallback_reason": generation_error,
            "note": ("IP-Adapter 참고 스타일 배경과 원본 사진 보존 합성을 적용한 결과"
                     if use_generative else "정량 스타일 특성과 로컬 합성 레이아웃을 적용한 결과"),
        }
        if not preview_only:
            output_path.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata

    def save_preview(self, preview_path: str | Path, destination: str | Path,
                     metadata: dict | None = None) -> dict:
        source = Path(preview_path).expanduser().resolve()
        preview_root = (Path(tempfile.gettempdir()) / "jarvis_mockup_previews").resolve()
        if source.parent != preview_root or not source.is_file():
            raise ValueError("자비스가 생성한 임시 미리보기만 저장할 수 있습니다.")
        target = Path(destination).expanduser().resolve()
        if target.suffix.casefold() != ".png":
            target = target.with_suffix(".png")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        result = dict(metadata or {})
        result.update({"output": str(target), "preview_only": False, "saved_from_preview": str(source)})
        target.with_suffix(".json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    def edit_preview(self, preview_path: str | Path, instruction: str, *, seed: int = 42) -> dict:
        """Regenerate an existing draft with a natural-language edit instruction."""
        source = Path(preview_path).expanduser().resolve()
        if not source.is_file() or not instruction.strip():
            raise ValueError("수정할 미리보기와 구체적인 수정 지시가 필요합니다.")
        if not self.generation_backend.status().get("ready", False):
            raise RuntimeError("AI 프롬프트 수정에는 생성형 시안 모델 준비가 필요합니다.")
        with Image.open(source) as original:
            size = original.size
            orientation = "landscape" if size[0] > size[1] else "portrait" if size[0] < size[1] else "square"
        edited = self.generation_backend.generate_background(
            reference_paths=[str(source)],
            prompt=("Edit the supplied design while preserving its composition and recognizable subjects. "
                    "The following user instruction has highest priority: " + instruction.strip()),
            orientation=orientation, seed=seed,
        )
        edited = ImageOps.fit(edited, size, method=Image.Resampling.LANCZOS)
        preview_root = Path(tempfile.gettempdir()) / "jarvis_mockup_previews"
        preview_root.mkdir(parents=True, exist_ok=True)
        target = preview_root / f"edited_{uuid.uuid4().hex}.png"
        edited.save(target, "PNG", optimize=True)
        return {"output": str(target), "preview_only": True, "instruction": instruction.strip(),
                "renderer": "ai-image-edit", "width": size[0], "height": size[1]}

    def transform_preview(self, preview_path: str | Path, operation: str, value: float = 1.0) -> dict:
        """Apply a non-destructive manual operation and return a new temporary draft."""
        source = Path(preview_path).expanduser().resolve()
        if not source.is_file():
            raise ValueError("수정할 미리보기가 없습니다.")
        from PIL import ImageEnhance
        with Image.open(source) as opened:
            image = opened.convert("RGB")
            if operation == "rotate_left": image = image.rotate(90, expand=True)
            elif operation == "rotate_right": image = image.rotate(-90, expand=True)
            elif operation == "flip_horizontal": image = ImageOps.mirror(image)
            elif operation == "flip_vertical": image = ImageOps.flip(image)
            elif operation == "brightness": image = ImageEnhance.Brightness(image).enhance(float(value))
            elif operation == "contrast": image = ImageEnhance.Contrast(image).enhance(float(value))
            elif operation == "saturation": image = ImageEnhance.Color(image).enhance(float(value))
            elif operation == "sharpness": image = ImageEnhance.Sharpness(image).enhance(float(value))
            else: raise ValueError(f"지원하지 않는 편집 작업입니다: {operation}")
        preview_root = Path(tempfile.gettempdir()) / "jarvis_mockup_previews"
        preview_root.mkdir(parents=True, exist_ok=True)
        target = preview_root / f"edited_{uuid.uuid4().hex}.png"
        image.save(target, "PNG", optimize=True)
        return {"output": str(target), "preview_only": True, "operation": operation,
                "renderer": "pillow-nondestructive-editor",
                "width": image.width, "height": image.height}
