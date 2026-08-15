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
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from core.vision_runtime import VisionRuntime
from core.mockup_scene import (SCENE_PLAN_JSON_SCHEMA, SCENE_EDIT_PATCH_JSON_SCHEMA,
                               SCENE_EDIT_VERDICT_JSON_SCHEMA, ScenePlanError,
                               apply_scene_edit_patch, build_evidence_fallback_plan, extract_json_object,
                               normalize_scene_plan, enforce_explicit_user_constraints,
                               enforce_exact_user_copy,
                               enforce_measured_style_evidence, infer_edit_scopes, merge_scoped_scene_edit,
                               parse_explicit_colored_copy,
                               filter_scene_edit_patch,
                               restore_required_elements, scene_changed, validate_patch_against_instruction)
from core.mockup_layer_graph import (scene_plan_to_layer_graph, validate_layer_graph,
                                     layer_graph_to_svg, render_svg_with_qt)
from core.mockup_style_index import VisualStyleIndex
from core.mockup_subject_runtime import SubjectAnalysisRuntime
from core.mockup_post_training import StyleTrainingDataset
from config import Config
from core.specialist_team import TeamRun


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
                 generation_backend=None, scene_planner=None, style_index=None,
                 enable_visual_review: bool | None = None, subject_runtime=None, team_runtime=None):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.vision = vision
        self.scene_planner = scene_planner
        self.style_index = style_index or VisualStyleIndex(self.profile_dir / "visual_style.db")
        self.subject_runtime = subject_runtime or SubjectAnalysisRuntime()
        self.training_dataset = StyleTrainingDataset()
        self.team_runtime = team_runtime
        # Injected test/custom vision adapters keep their established contract.
        self.enable_visual_review = (Config.MOCKUP_VISUAL_REVIEW if enable_visual_review is None and vision is None
                                     else bool(enable_visual_review))
        if generation_backend is None:
            from core.mockup_generation import IPAdapterGenerationBackend
            generation_backend = IPAdapterGenerationBackend()
        self.generation_backend = generation_backend
        from core.mockup_generation import SDXLGenerationBackend
        self.sdxl_backend = SDXLGenerationBackend()

    def _get_scene_planner(self):
        """Use a text reasoning model for JSON planning, separate from image observation."""
        if self.scene_planner is not None:
            return self.scene_planner
        # Injected vision doubles used by tests and custom adapters keep the
        # legacy single-adapter contract unless a planner is explicitly given.
        if self.vision is not None:
            return None
        # Scene JSON must remain local and schema-constrained even when the
        # application's global provider is hybrid. HybridLLMClient exposes a
        # conversational surface, while OllamaClient exposes format/schema.
        from core.llm import OllamaClient
        self.scene_planner = OllamaClient("reasoning")
        return self.scene_planner

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
        for reference in paths:
            self.style_index.add(profile.profile_id, reference, kind="reference",
                                 metadata={"profile_name": profile.name})
        return profile

    def similar_styles(self, image_path: str | Path, *, top_k: int = 5) -> list[dict]:
        """Search reference/approved images using image-native features, never text RAG."""
        return self.style_index.search(image_path, top_k=top_k)

    def prepare_style_training_dataset(self, profile_id: str) -> dict:
        """Prepare a real post-training manifest only from approved outputs."""
        self.load_profile(profile_id)
        return self.training_dataset.collect(profile_id, self.style_index.items(profile_id, approved_only=True))

    @staticmethod
    def _scene_contract_violations(plan: dict, instruction: str, visible_copy: str) -> list[str]:
        """Validate explicit user requirements from editable layer data.

        Vision is useful for aesthetic advice but unreliable for exact font,
        colour and geometry claims. Those are authoritative in the scene graph.
        """
        violations = []
        text_layers = plan.get("texts", [])
        copy, required_spans = parse_explicit_colored_copy(instruction)
        if visible_copy and not any(str(item.get("content", "")).strip() == visible_copy.strip()
                                    for item in text_layers):
            violations.append("정확한 표시 문구가 장면 레이어에 없습니다.")
        if required_spans:
            actual_spans = [span for item in text_layers for span in item.get("spans", [])]
            for required in required_spans:
                actual = next((item for item in actual_spans
                               if str(item.get("content", "")).strip() == required["content"]), None)
                if not actual:
                    violations.append(f"'{required['content']}' 문구 구간이 없습니다.")
                    continue
                if str(actual.get("color", "")).casefold() != str(required["color"]).casefold():
                    violations.append(f"'{required['content']}' 문구 색상이 요청과 다릅니다.")
                requested_font = re.sub(r"[\s_-]|체$", "", str(required.get("font_family", "")).casefold())
                actual_font = re.sub(r"[\s_-]|체$", "", str(actual.get("font_family", "")).casefold())
                aliases = {"맑은고딕": "malgungothic", "궁서": "gungsuh", "고딕": "gothic"}
                requested_font, actual_font = aliases.get(requested_font, requested_font), aliases.get(actual_font, actual_font)
                if requested_font and requested_font not in actual_font and actual_font not in requested_font:
                    violations.append(f"'{required['content']}' 문구 글꼴이 요청과 다릅니다.")
        assets = plan.get("assets", [])
        if any(word in instruction for word in ("원형", "동그랗", "원 모양")):
            if not assets or any(item.get("shape") != "ellipse" for item in assets):
                violations.append("원형 스티커 프레임이 적용되지 않았습니다.")
            if "스티커" in instruction and plan.get("canvas", {}).get("background") != "transparent":
                violations.append("원형 스티커 바깥 영역이 투명하지 않습니다.")
        if "얼굴만" in instruction and (not assets or float(assets[0].get("zoom", 1)) <= 1.5):
            violations.append("얼굴 중심 확대 크롭이 적용되지 않았습니다.")
        if ("안쪽" in instruction and "하단" in instruction and assets and text_layers):
            asset, text = assets[0], text_layers[0]
            inside = (float(text.get("x", 0)) >= float(asset.get("x", 0)) and
                      float(text.get("x", 0)) + float(text.get("width", 0)) <= float(asset.get("x", 0)) + float(asset.get("width", 0)) and
                      float(text.get("y", 0)) >= float(asset.get("y", 0)) + float(asset.get("height", 0)) * .55 and
                      float(text.get("y", 0)) + float(text.get("height", 0)) <= float(asset.get("y", 0)) + float(asset.get("height", 0)))
            if not inside:
                violations.append("문구가 스티커 안쪽 하단에 배치되지 않았습니다.")
        return violations

    def _review_rendered_image(self, output_path: Path, profile: MockupStyleProfile,
                               instruction: str, visible_copy: str, scene_plan: dict) -> dict:
        schema = {
            "type": "object", "required": ["passed", "score", "violations", "correction_instruction"],
            "properties": {
                "passed": {"type": "boolean"}, "score": {"type": "number"},
                "violations": {"type": "array", "items": {"type": "string"}},
                "correction_instruction": {"type": "string"},
            },
        }
        vision = VisionRuntime()
        try:
            result = vision.analyze([str(output_path)], (
                "렌더링된 결과를 실제 이미지로 검수하세요. 얼굴/핵심 피사체 잘림, 문구 가독성, "
                "겹침, 안전 여백, 대비와 사용자 지시 충족 여부를 판정하세요. 사용자가 시선을 지시하지 "
                "않았다면 피사체 시선 방향을 결함으로 판단하지 마세요. 사용자가 문구를 사진 안쪽 하단에 "
                "요청했다면 하단 피사체와의 일부 중첩은 의도된 구성이며 눈·코·입을 실제로 가릴 때만 지적하세요. "
                "정확한 문구 색상·글꼴·좌표는 이미지에서 추측하지 말고 아래 장면 레이어를 기준으로 판단하세요. "
                f"사용자 지시: {instruction or '없음'} / 정확한 표시 문구: {visible_copy or '없음'} / "
                f"장면 레이어: {json.dumps(scene_plan, ensure_ascii=False)[:5000]} / "
                f"학습 스타일 구조: {json.dumps(profile.design_recipe, ensure_ascii=False)[:3000]}"
            ), mode="general", json_schema=schema)
            raw = extract_json_object(result.get("analysis", ""))
            visual_advice = [str(item)[:300] for item in raw.get("violations", [])[:8] if str(item).strip()]
            violations = self._scene_contract_violations(scene_plan, instruction, visible_copy)
            return {
                # Only explicit, machine-verifiable contract failures block a
                # preview. Vision's aesthetic observations remain advisory.
                "passed": not violations,
                "score": max(0.0, min(1.0, float(raw.get("score", 0)))),
                "violations": violations,
                "visual_advice": visual_advice,
                "correction_instruction": (str(raw.get("correction_instruction", ""))[:1200]
                                           if violations else ""),
            }
        except Exception as exc:
            return {"passed": False, "score": 0.0, "violations": [f"시각 검수 실패: {exc}"],
                    "correction_instruction": "", "review_error": True}
        finally:
            vision.release_model()

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
        result = self.generation_backend.status()
        result["sdxl"] = self.sdxl_backend.status()
        return result

    def prepare_generation_models(self, progress=None) -> dict:
        return self.generation_backend.prepare(progress)

    def prepare_sdxl_model(self, progress=None) -> dict:
        return self.sdxl_backend.prepare(progress)

    @staticmethod
    def _font(size: int, bold: bool = False, family: str = ""):
        normalized = re.sub(r"[^a-z0-9가-힣]", "", str(family).casefold())
        known = {
            "malgungothic": "malgun", "맑은고딕": "malgun", "segoeui": "segoeui",
            "arial": "arial", "timesnewroman": "times", "consolas": "consola",
        }
        stem = known.get(normalized, normalized)
        font_root = Path("C:/Windows/Fonts")
        requested = []
        # Windows stores localized family names separately from font filenames
        # (for example 휴먼둥근헤드라인 -> HMKMRHD.TTF). Filename globbing
        # therefore silently fell back to Malgun Gothic even though the UI and
        # scene JSON showed a different family.
        if normalized:
            try:
                import winreg
                registry_roots = (
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
                    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
                )
                matches = []
                for hive, key_name in registry_roots:
                    try:
                        with winreg.OpenKey(hive, key_name) as key:
                            index = 0
                            while True:
                                try:
                                    display_name, filename, _ = winreg.EnumValue(key, index)
                                except OSError:
                                    break
                                index += 1
                                display_normalized = re.sub(
                                    r"[^a-z0-9가-힣]", "", display_name.casefold()
                                ).replace("truetype", "")
                                if normalized in display_normalized or display_normalized in normalized:
                                    path = Path(str(filename))
                                    if not path.is_absolute():
                                        path = font_root / path
                                    if path.is_file():
                                        score = int(bool(bold) == any(
                                            token in display_name.casefold()
                                            for token in ("bold", "굵게", " bd", " b ")
                                        ))
                                        matches.append((score, path))
                    except OSError:
                        continue
                requested.extend(path for _, path in sorted(matches, key=lambda item: -item[0]))
            except (ImportError, OSError):
                pass
        if stem:
            requested.extend(sorted(
                font_root.glob(f"{stem}*.*"),
                key=lambda path: (bold and "bd" not in path.stem.casefold(), len(path.name)),
            ))
        candidates = [
            *requested,
            Path("C:/Windows/Fonts/malgunbd.ttf" if bold else "C:/Windows/Fonts/malgun.ttf"),
            Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        ]
        for path in candidates:
            if path.is_file(): return ImageFont.truetype(str(path), size)
        return ImageFont.load_default()

    @staticmethod
    def _detect_primary_face(path: Path):
        """Return the largest detected face as normalized center/size."""
        try:
            import cv2
            import numpy as np
            # cv2.imread on Windows cannot reliably open Korean paths.
            image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                return None
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            classifier = cv2.CascadeClassifier(
                str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
            )
            faces = classifier.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=5,
                                                  minSize=(32, 32))
            if len(faces) == 0:
                return None
            x, y, width, height = max(faces, key=lambda face: int(face[2]) * int(face[3]))
            image_h, image_w = gray.shape[:2]
            return {
                "center_x": (x + width / 2) / image_w,
                "center_y": (y + height / 2) / image_h,
                "width": width / image_w,
                "height": height / image_h,
                "source_ratio": image_w / max(1, image_h),
            }
        except Exception:
            return None

    def _enforce_detected_subject_visibility(self, plan: dict, paths: list[Path],
                                             instruction: str) -> tuple[dict, list[str]]:
        """Ground face framing requests in source pixels instead of LLM guesses."""
        text = " ".join(str(instruction or "").casefold().split())
        if not any(word in text for word in ("얼굴", "머리", "안면")):
            return plan, []
        face_only = any(word in text for word in ("얼굴만", "얼굴 위주", "얼굴 중심", "얼굴 크게"))
        fully_visible = any(word in text for word in (
            "모두 보", "전부 보", "전체 보", "안 잘리", "잘리지 않", "머리까지",
            "얼굴 보이", "얼굴이 보이", "얼굴을 보이",
        ))
        if not face_only and not fully_visible:
            return plan, []
        result, changed = deepcopy(plan), []
        for asset in result.get("assets", []):
            index = int(asset.get("index", -1))
            if not 0 <= index < len(paths):
                continue
            face = self._detect_primary_face(paths[index])
            if not face:
                continue
            asset["focal_x"] = round(face["center_x"], 4)
            asset["focal_y"] = round(face["center_y"], 4)
            changed.extend([f"assets[{index}].focal_x", f"assets[{index}].focal_y"])
            if fully_visible and not face_only:
                asset["fit"] = "contain"
                asset["zoom"] = 1
                changed.extend([f"assets[{index}].fit", f"assets[{index}].zoom"])
            else:
                # Make the face prominent while retaining a 35% safety margin
                # around the detected box. The cover crop remains centered on
                # the measured face, so eyes/hair cannot drift outside the frame.
                frame_ratio = float(asset.get("width", 1)) / max(.001, float(asset.get("height", 1)))
                visible_height_at_zoom_1 = min(1.0, face["source_ratio"] / max(.001, frame_ratio))
                safe_zoom = visible_height_at_zoom_1 / max(.05, face["height"] * 1.35)
                asset["fit"] = "cover"
                asset["zoom"] = round(max(1.0, min(4.0, safe_zoom)), 4)
                changed.extend([f"assets[{index}].fit", f"assets[{index}].zoom"])
        return result, list(dict.fromkeys(changed))

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
                                 edit_state: dict | None = None,
                                 size: int = 1600) -> tuple[Image.Image, list[dict], str]:
        """Preserve source photos while applying the learned circular sticker grammar."""
        edit_state = dict(edit_state or {})
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
                base_color = self._hex(edit_state.get("background_color", ""), self._accent_palette(palette))
                base = Image.new("RGBA", hero.size, (*base_color, 255))
                base.alpha_composite(hero); hero = base
            canvas.paste(hero, (margin, margin), Image.composite(circle_mask, Image.new("L", circle_mask.size), hero.getchannel("A")))
            plan.append({"index": 0, "shape": "circle", "role": "hero", "x": margin / size,
                         "y": margin / size, "width": diameter / size, "height": diameter / size,
                         "fit": "cover", "rotation": 0})
        else:
            base_color = self._hex(edit_state.get("background_color", ""), self._accent_palette(palette))
            background = Image.new("RGBA", (diameter, diameter), (*base_color, 255))
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
        border_color = (*self._hex(edit_state.get("border_color", ""), (255, 255, 255)), 245)
        if edit_state.get("border_style") == "solid":
            draw.ellipse(box, outline=border_color, width=max(7, size // 130))
        else:
            dash_count = 36
            for index in range(dash_count):
                start = index * 360 / dash_count + 1.5
                draw.arc(box, start=start, end=start + 5.8, fill=border_color, width=max(7, size // 130))

        visible_copy = " ".join(str(edit_state.get("visible_copy", visible_copy) or "").split())[:120] or self._visible_copy(instruction)
        if edit_state.get("remove_copy"):
            visible_copy = ""
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

    def _legacy_render(self, profile_id: str, production_paths, *, instruction: str = "",
               visible_copy: str = "",
               edit_state: dict | None = None,
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
                paths, palette, instruction, visible_copy, edit_state, width
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
                "production_sources": [str(path) for path in paths],
                "edit_state": dict(edit_state or {}),
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
            "visible_copy": visible_copy.strip(),
            "production_sources": [str(path) for path in paths],
            "edit_state": dict(edit_state or {}),
            "preview_only": bool(preview_only),
            "generation_fallback_reason": generation_error,
            "note": ("IP-Adapter 참고 스타일 배경과 원본 사진 보존 합성을 적용한 결과"
                     if use_generative else "정량 스타일 특성과 로컬 합성 레이아웃을 적용한 결과"),
        }
        if not preview_only:
            output_path.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata

    def _request_scene_plan(self, profile: MockupStyleProfile, paths: list[Path], instruction: str,
                            visible_copy: str, previous_plan: dict | None = None,
                            edit_instruction: str = "", guidance_paths=None,
                            memory_context: str = "") -> dict:
        vision = self.vision or VisionRuntime()
        source_context = (
            f"학습된 스타일 분석:\n{profile.vision_analysis}\n\n"
            f"학습 데이터의 측정 특징:\n{json.dumps(profile.style_features, ensure_ascii=False)}\n"
            f"사용자 제작 지시: {instruction or '없음'}\n"
            f"이미지에 실제 표시할 문구: {visible_copy or '없음'}\n"
        )
        if previous_plan is None:
            task = "참고 이미지들의 공통 디자인 언어를 학습해 제작용 이미지로 새로운 시안을 설계하세요."
        else:
            task = (
                "기존 설계도를 사용자의 수정 명령에 맞게 수정하세요. 명령하지 않은 피사체 정체성, "
                "구도, 문구는 보존하세요. 기존 설계도:\n"
                f"{json.dumps(previous_plan, ensure_ascii=False)}\n수정 명령: {edit_instruction}"
            )
        prompt = f"""{task}
{source_context}
첫 {len(profile.reference_paths[:8])}장은 서로 독립된 학습용 시안이고, 마지막 {len(paths)}장은 제작용 원본입니다.
학습용 픽셀이나 인물을 결과에 복사하지 말고 형태, 공간, 계층, 색, 타이포그래피 관계만 추론하세요.
원형 스티커 같은 미리 정해진 템플릿을 가정하지 마세요. 사용자 명령이 학습 스타일보다 우선합니다.
제작용 이미지는 모두 정확히 한 번씩 assets에 포함하고 얼굴이나 핵심 피사체가 잘리지 않도록 focal_x/focal_y를 정하세요.
표시 문구가 '없음'이면 texts는 빈 배열이어야 합니다. 지시문 자체를 이미지 문구로 쓰지 마세요.
설명 없이 JSON 객체 하나만 반환하세요. 아래는 값 예시가 아니라 필드의 형식 정의입니다. 좌표와 속성은 반드시 실제 이미지를 분석해 새로 결정하세요.
- canvas: aspect_ratio(number 0.55~1.9), background(hex color)
- assets: 제작 이미지마다 index, x, y, width, height(모두 정규화 좌표), shape(rectangle/rounded/ellipse), fit(cover/contain), zoom(1~4), focal_x, focal_y, rotation, z
- decorations: 필요할 때만 type(rectangle/ellipse/line), x, y, width, height, fill, stroke, stroke_width, dash, z
- texts: 표시 문구가 있을 때만 content, x, y, width, height, font_size, font_family, font_weight(normal/bold), color, background, align, padding, z
- rationale: 어떤 참고 이미지의 어떤 공통 특징과 사용자 지시가 각 결정의 근거인지 구체적으로 작성
최상위 키는 canvas, assets, decorations, texts, rationale 다섯 개만 사용하세요."""
        operation = (f"기존 설계도: {json.dumps(previous_plan, ensure_ascii=False)}\n"
                     f"이번 수정 명령: {edit_instruction}\n요청한 부분만 변경하세요."
                     if previous_plan is not None else
                     "참고 이미지의 공통 디자인 문법으로 새 설계도를 작성하세요.")
        prompt = f"""당신은 이미지 편집 장면 설계자입니다.
{operation}
앞의 {len(profile.reference_paths[:8])}장은 스타일 참고 이미지이고 마지막 {len(paths)}장은 결과에 배치할 제작 원본입니다.
측정된 스타일 특성: {json.dumps(profile.style_features, ensure_ascii=False)}
사용자 제작 지시: {instruction or '없음'}
실제 표시 문구: {visible_copy or '없음'}
제작 원본 index 0부터 {len(paths) - 1}까지 각각 정확히 한 번 assets에 포함하세요.
x와 y는 중심이 아니라 왼쪽 위 좌표이며 x+width와 y+height는 1 이하여야 합니다.
얼굴·머리·사용자가 보존하라고 한 신체가 잘리면 안 됩니다. 전체 보존 요청에는 fit=contain을 사용하세요.
사용자 지시는 학습 스타일보다 우선합니다. 사용자가 문구를 중앙에 요청하면 하단 배치를 강제하지 마세요.
참고 이미지의 픽셀이나 인물을 결과에 복제하지 마세요. 표시 문구가 없으면 texts는 빈 배열입니다.
설명이나 Markdown 없이 canvas, assets, decorations, texts, rationale 키를 가진 JSON 객체 하나만 반환하세요."""
        guidance = self._validate_images(guidance_paths or []) if guidance_paths else []
        evidence_paths = [*profile.reference_paths[:8], *[str(path) for path in paths[:4]],
                          *[str(path) for path in guidance[:4]]]
        planner = self._get_scene_planner()
        visual_observation = ""
        if planner is not None:
            observation_prompt = f"""이미지를 설계하지 말고 관찰 사실만 기록하세요.
앞의 {len(profile.reference_paths[:8])}장은 스타일 참고, 다음 {len(paths)}장은 제작 원본, 마지막 {len(guidance)}장은 사용자의 설명 스케치 또는 수정 참고 이미지입니다.
참고 이미지에서 반복되는 프레임·여백·문구 영역·피사체 크기를 요약하고, 제작 원본마다 사람이나 물체의 위치와 안전하게 자를 수 있는 얼굴·상반신·전신 범위를 설명하세요.
설명 스케치는 최종 픽셀로 복사하지 말고 화살표·박스·대략적인 배치 관계를 의도로 해석하세요.
사용자 지시: {instruction or '없음'}"""
            try:
                visual_observation = str(vision.analyze(
                    evidence_paths, observation_prompt, mode="general"
                ).get("analysis", ""))[:12000]
            except Exception as exc:
                visual_observation = f"이미지 관찰 실패: {exc}"
            finally:
                release = getattr(vision, "release_model", None)
                if callable(release): release()

        def request_valid_plan(request_prompt: str, stage: str, baseline: dict | None = None) -> dict:
            last_error, last_answer = None, ""
            current_prompt = request_prompt
            for attempt in range(3):
                if planner is not None:
                    last_answer = str(planner.chat_structured([
                        {"role": "system", "content": (
                            "당신은 이미지 관찰 결과와 사용자 지시를 검증 가능한 장면 JSON으로 "
                            "변환하는 레이아웃 설계자입니다. 이미지에 없는 사실을 만들지 마세요.")},
                        {"role": "user", "content": (
                            f"{current_prompt}\n\nVision 관찰 결과:\n{visual_observation}"
                            f"\n\n작업공간의 승인된 과거 기억:\n{memory_context or '없음'}")},
                    ], json_schema=SCENE_PLAN_JSON_SCHEMA))
                else:
                    try:
                        response = vision.analyze(evidence_paths, current_prompt, mode="general",
                                                  json_schema=SCENE_PLAN_JSON_SCHEMA)
                    except TypeError:  # compatibility with injected/custom vision adapters
                        response = vision.analyze(evidence_paths, current_prompt, mode="general")
                    last_answer = str(response.get("analysis", ""))
                try:
                    raw = extract_json_object(last_answer)
                    restored = []
                    if baseline is not None:
                        raw, restored = restore_required_elements(
                            raw, baseline, asset_count=len(paths), visible_copy=visible_copy,
                        )
                    normalized = normalize_scene_plan(raw, asset_count=len(paths), visible_copy=visible_copy)
                    if restored:
                        normalized["restored_required_elements"] = restored
                    return normalized
                except ScenePlanError as exc:
                    last_error = exc
                    current_prompt = f"""이전 {stage} 응답이 검증에 실패했습니다.
검증 오류: {exc}
이전 응답: {last_answer}
제작용 이미지 인덱스는 0부터 {len(paths) - 1}까지이며 모두 assets에 정확히 한 번 포함해야 합니다.
표시 문구가 있으면 texts에 해당 문구의 영역을 반드시 포함하고, 없으면 texts를 빈 배열로 두세요.
원래 요청의 디자인 판단은 유지하되 오류만 교정하여 JSON 객체 하나만 다시 반환하세요."""
            raise ScenePlanError(f"AI가 {stage} 설계도를 3회 교정했지만 유효하게 만들지 못했습니다: {last_error}")

        plan = previous_plan or {}

        review_prompt = f"""이미지 레이아웃을 검토하고 전체 JSON 설계도만 반환하세요.
설계도: {json.dumps(plan, ensure_ascii=False)}
사용자 지시: {instruction or '없음'}
이번 수정 명령: {edit_instruction or '없음'}
표시 문구: {visible_copy or '없음'}
얼굴·머리·요청 신체의 잘림, 제작 이미지 누락, 왼쪽 위 좌표 범위, 문구 위치와 가독성, 수정 명령의 실제 반영을 검사하세요. 사용자 지시가 학습 스타일보다 우선합니다."""
        try:
            plan = request_valid_plan(prompt, "초기")
        except ScenePlanError:
            if previous_plan is not None:
                plan, explicit = enforce_explicit_user_constraints(previous_plan, edit_instruction)
                if not explicit or not scene_changed(previous_plan, plan):
                    raise
                plan["edit_plan_fallback"] = "Vision 설계 실패 후 명확한 사용자 제약만 적용했습니다."
            else:
                plan = build_evidence_fallback_plan(
                    profile.style_features, asset_count=len(paths), visible_copy=visible_copy,
                )
        if previous_plan is not None:
            plan, _ = enforce_explicit_user_constraints(plan, edit_instruction)
        if previous_plan is not None and not scene_changed(previous_plan, plan):
            for revision_attempt in range(2):
                retry_prompt = f"""사용자의 수정 명령을 반영하는 편집 설계도에서 실제 변경점이 발견되지 않았습니다.
수정 명령: {edit_instruction}
현재 설계도: {json.dumps(previous_plan, ensure_ascii=False)}
직전 무효 응답: {json.dumps(plan, ensure_ascii=False)}
사용자가 지정한 대상과 속성을 찾아 최소 한 가지 이상의 관련 필드를 실제로 변경하세요.
요청하지 않은 피사체·텍스트·구조는 보존하고, 설명 없이 변경된 전체 JSON 설계도만 반환하세요."""
                plan = request_valid_plan(retry_prompt, f"수정 반영 재시도 {revision_attempt + 1}", baseline=previous_plan)
                if scene_changed(previous_plan, plan):
                    break
            if not scene_changed(previous_plan, plan):
                raise ScenePlanError("수정 명령의 대상과 변경 속성을 식별하지 못했습니다.")
        review_prompt = f"""당신은 상업 디자인 아트 디렉터입니다. 참고 이미지, 제작 원본, 사용자 지시와 아래 1차 설계도를 비교해 결함을 교정하세요.
1차 설계도: {json.dumps(plan, ensure_ascii=False)}
사용자 지시: {instruction or '없음'}
표시 문구: {visible_copy or '없음'}
검사 항목: 참고 자료의 반복되는 캔버스 비율과 시각 문법, 피사체 크기와 정체성 보존, 얼굴·머리·몸의 의도치 않은 잘림, 빈 공간의 균형, 문구와 얼굴의 충돌, 문구 가독성, 사용자 수정 명령의 실제 반영.
미리 정한 원형·카드 템플릿을 적용하지 말고 보이는 참고 자료를 근거로 판단하세요. cover는 의도적인 크롭일 때만 쓰고 전체 보존 요청에는 contain을 쓰세요.
결함을 고친 최종 설계도를 JSON 객체 하나로만 반환하세요. 최상위 키는 canvas, assets, decorations, texts, rationale입니다."""
        try:
            reviewed = request_valid_plan(review_prompt, "품질 검토", baseline=plan)
        except ScenePlanError as exc:
            reviewed = deepcopy(plan)
            reviewed["quality_review_fallback"] = str(exc)
        # A review may improve a revision, but it must not erase the user's edit.
        if previous_plan is not None and not scene_changed(previous_plan, reviewed):
            reviewed = plan
            reviewed["quality_review_rejected"] = "사용자 수정 사항을 되돌려 1차 수정 설계를 유지했습니다."
        reviewed, _ = enforce_measured_style_evidence(reviewed, profile.style_features)
        reviewed, _ = enforce_explicit_user_constraints(
            reviewed, " ".join(part for part in (instruction, edit_instruction) if part)
        )
        if previous_plan is not None:
            reviewed, _ = merge_scoped_scene_edit(previous_plan, reviewed, edit_instruction)
            reviewed, _ = enforce_explicit_user_constraints(reviewed, edit_instruction)
        return reviewed

    @staticmethod
    def _rgba(value: str, fallback=(0, 0, 0, 0)):
        text = str(value or "")
        if text == "transparent": return (0, 0, 0, 0)
        try:
            raw = text.lstrip("#")
            if len(raw) == 6: raw += "ff"
            return tuple(int(raw[index:index + 2], 16) for index in (0, 2, 4, 6))
        except (TypeError, ValueError):
            return fallback

    def _render_scene_plan(self, plan: dict, paths: list[Path], *, background=None,
                           long_edge: int = 1600) -> Image.Image:
        ratio = float(plan["canvas"]["aspect_ratio"])
        width, height = ((long_edge, max(900, int(long_edge / ratio))) if ratio >= 1 else
                         (max(900, int(long_edge * ratio)), long_edge))
        if background is None:
            canvas = Image.new("RGBA", (width, height), self._rgba(plan["canvas"]["background"], (255, 255, 255, 255)))
        else:
            canvas = ImageOps.fit(background.convert("RGBA"), (width, height), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(canvas, "RGBA")

        def box(item):
            x, y = int(item["x"] * width), int(item["y"] * height)
            w, h = max(1, int(item["width"] * width)), max(1, int(item["height"] * height))
            return x, y, min(width, x + w), min(height, y + h)

        for item in sorted((d for d in plan["decorations"] if d["z"] < 0), key=lambda d: d["z"]):
            self._draw_scene_decoration(draw, item, box(item), width)
        for item in sorted(plan["assets"], key=lambda asset: asset["z"]):
            x1, y1, x2, y2 = box(item); cell = (max(1, x2-x1), max(1, y2-y1))
            with Image.open(paths[item["index"]]) as opened:
                source = opened.convert("RGBA")
                alpha_bbox = source.getchannel("A").getbbox()
                if alpha_bbox and alpha_bbox != (0, 0, source.width, source.height):
                    source = source.crop(alpha_bbox)
                zoom = max(1.0, float(item.get("zoom", 1)))
                if zoom > 1:
                    crop_width = max(1, int(source.width / zoom))
                    crop_height = max(1, int(source.height / zoom))
                    center_x = int(float(item.get("focal_x", .5)) * source.width)
                    center_y = int(float(item.get("focal_y", .5)) * source.height)
                    left = min(max(0, center_x - crop_width // 2), source.width - crop_width)
                    top = min(max(0, center_y - crop_height // 2), source.height - crop_height)
                    source = source.crop((left, top, left + crop_width, top + crop_height))
                if item["fit"] == "contain":
                    placed = ImageOps.contain(source, cell, Image.Resampling.LANCZOS)
                    layer = Image.new("RGBA", cell, (0, 0, 0, 0))
                    layer.alpha_composite(placed, ((cell[0]-placed.width)//2, (cell[1]-placed.height)//2))
                else:
                    centering = ((.5, .5) if zoom > 1 else
                                 (item["focal_x"], item["focal_y"]))
                    layer = ImageOps.fit(source, cell, Image.Resampling.LANCZOS,
                                         centering=centering)
            if item["rotation"]:
                layer = layer.rotate(item["rotation"], Image.Resampling.BICUBIC, expand=False)
            mask = layer.getchannel("A")
            if item["shape"] in {"rounded", "ellipse"}:
                shape_mask = Image.new("L", cell, 0); shape_draw = ImageDraw.Draw(shape_mask)
                if item["shape"] == "ellipse": shape_draw.ellipse((0, 0, cell[0]-1, cell[1]-1), fill=255)
                else: shape_draw.rounded_rectangle((0, 0, cell[0]-1, cell[1]-1), radius=max(8, min(cell)//12), fill=255)
                mask = Image.composite(mask, Image.new("L", cell, 0), shape_mask)
            canvas.paste(layer, (x1, y1), mask); draw = ImageDraw.Draw(canvas, "RGBA")
        for item in sorted((d for d in plan["decorations"] if d["z"] >= 0), key=lambda d: d["z"]):
            self._draw_scene_decoration(draw, item, box(item), width)
        for item in sorted(plan["texts"], key=lambda text: text["z"]):
            self._draw_scene_text(draw, item, box(item), width, height)
        return canvas

    def _draw_scene_decoration(self, draw, item, bounds, width):
        fill, stroke = self._rgba(item["fill"]), self._rgba(item["stroke"])
        stroke_width = max(1, int(item["stroke_width"] * width))
        if item.get("dash") and item["type"] == "ellipse":
            dash_length = max(2.0, float(item.get("dash_length", .025)) * 360)
            gap_length = max(1.0, float(item.get("gap_length", .018)) * 360)
            angle = 0.0
            while angle < 360:
                draw.arc(bounds, start=angle, end=min(360, angle + dash_length),
                         fill=stroke, width=stroke_width)
                angle += dash_length + gap_length
            return
        if item.get("dash") and item["type"] == "line":
            import math
            x1, y1, x2, y2 = bounds
            distance = max(1.0, math.hypot(x2 - x1, y2 - y1))
            dash = max(2.0, float(item.get("dash_length", .025)) * width)
            gap = max(1.0, float(item.get("gap_length", .018)) * width)
            cursor = 0.0
            while cursor < distance:
                end = min(distance, cursor + dash)
                draw.line((x1 + (x2-x1) * cursor/distance, y1 + (y2-y1) * cursor/distance,
                           x1 + (x2-x1) * end/distance, y1 + (y2-y1) * end/distance),
                          fill=stroke, width=stroke_width)
                cursor += dash + gap
            return
        if item["type"] == "line":
            draw.line((bounds[0], bounds[1], bounds[2], bounds[3]), fill=stroke, width=stroke_width)
        elif item["type"] == "ellipse":
            draw.ellipse(bounds, fill=fill, outline=stroke, width=stroke_width)
        else:
            draw.rectangle(bounds, fill=fill, outline=stroke, width=stroke_width)

    def _draw_scene_text(self, draw, item, bounds, width, height):
        x1, y1, x2, y2 = bounds
        background = self._rgba(item["background"])
        if background[3]: draw.rectangle(bounds, fill=background)
        padding = int(item["padding"] * min(width, height)); x1 += padding; y1 += padding; x2 -= padding; y2 -= padding
        font_size = max(14, int(item["font_size"] * min(width, height)))
        content = item["content"]
        bold = item.get("font_weight", "bold") == "bold"
        family = str(item.get("font_family", ""))
        font = self._font(font_size, bold=bold, family=family)
        while font_size > 14:
            font = self._font(font_size, bold=bold, family=family); measured = draw.textbbox((0, 0), content, font=font)
            if measured[2] - measured[0] <= max(1, x2-x1) and measured[3] - measured[1] <= max(1, y2-y1): break
            font_size -= 2
        measured = draw.textbbox((0, 0), content, font=font, spacing=max(1, int(font_size * float(item.get("line_height", 1.2)))))
        text_w, text_h = measured[2]-measured[0], measured[3]-measured[1]
        x = x1 if item["align"] == "left" else x2-text_w if item["align"] == "right" else x1+(x2-x1-text_w)//2
        y = y1 + (y2-y1-text_h)//2 - measured[1]
        fill = self._rgba(item["color"], (17, 17, 17, 255))
        stroke_fill = self._rgba(item.get("stroke", "transparent"))
        stroke_width = max(0, int(float(item.get("stroke_width", 0)) * min(width, height)))
        shadow = item.get("shadow") if isinstance(item.get("shadow"), dict) else {}
        if shadow:
            offset_x = int(float(shadow.get("offset_x", .006)) * width)
            offset_y = int(float(shadow.get("offset_y", .006)) * height)
            draw.multiline_text((x + offset_x, y + offset_y), content, font=font,
                                fill=self._rgba(shadow.get("color", "#00000066")),
                                spacing=max(1, int(font_size * float(item.get("line_height", 1.2)))))
        path = item.get("path") if isinstance(item.get("path"), dict) else None
        letter_spacing = int(float(item.get("letter_spacing", 0)) * min(width, height))
        if path and path.get("type") == "arc" and content:
            radius = max(font_size, int(float(path.get("radius", .25)) * min(width, height)))
            center_x, center_y = x1 + (x2 - x1) // 2, y1 + (y2 - y1) // 2
            start = math.radians(float(path.get("start_angle", 200)))
            end = math.radians(float(path.get("end_angle", 340)))
            step = (end - start) / max(1, len(content) - 1)
            for index, character in enumerate(content):
                angle = start + step * index
                cx = center_x + math.cos(angle) * radius
                cy = center_y + math.sin(angle) * radius
                draw.text((cx, cy), character, font=font, anchor="mm", fill=fill,
                          stroke_width=stroke_width, stroke_fill=stroke_fill)
        elif letter_spacing and "\n" not in content:
            widths = [draw.textlength(character, font=font) for character in content]
            total = sum(widths) + letter_spacing * max(0, len(content) - 1)
            cursor = x1 if item["align"] == "left" else x2-total if item["align"] == "right" else x1+(x2-x1-total)/2
            for character, char_width in zip(content, widths):
                draw.text((cursor, y), character, font=font, fill=fill,
                          stroke_width=stroke_width, stroke_fill=stroke_fill)
                cursor += char_width + letter_spacing
        else:
            draw.multiline_text((x, y), content, font=font, fill=fill,
                                spacing=max(1, int(font_size * float(item.get("line_height", 1.2)))),
                                align=item.get("align", "center"), stroke_width=stroke_width,
                                stroke_fill=stroke_fill)

    def render(self, profile_id: str, production_paths, *, instruction: str = "",
               visible_copy: str = "", edit_state: dict | None = None,
               output_dir: str | Path = "data/mockup_outputs", basename: str = "mockup",
               backend: str = "auto", seed: int = 42, preview_only: bool = False,
               scene_plan: dict | None = None, guidance_paths=None,
               memory_context: str = "") -> dict:
        """Render exclusively from a model-authored scene plan, never a named template."""
        profile = self.load_profile(profile_id); paths = self._validate_images(production_paths)
        team_run = TeamRun("mockup", instruction, {
            "memory_context": memory_context, "references": list(profile.reference_paths),
            "production_assets": [str(path) for path in paths],
        })
        if self.team_runtime:
            self.team_runtime.execute_role(team_run, "style_analyst", lambda _run: {
                "recipe": profile.design_recipe, "features": profile.style_features,
                "similar_styles": self.similar_styles(paths[0], top_k=3),
            }, release=self.style_index.release)
            subject_evidence = self.team_runtime.execute_role(
                team_run, "subject_specialist",
                lambda _run: [self.subject_runtime.analyze(path) for path in paths],
                release=self.subject_runtime.release,
            )
        else:
            subject_evidence = [self.subject_runtime.analyze(path) for path in paths]
        planner_memory = "\n".join(part for part in (
            memory_context,
            "피사체·얼굴·안전영역 계측: " + json.dumps(subject_evidence, ensure_ascii=False),
        ) if part)
        plan_factory = lambda _run: (scene_plan or self._request_scene_plan(
            profile, paths, instruction, visible_copy,
            guidance_paths=guidance_paths, memory_context=planner_memory,
        ))
        plan = (self.team_runtime.execute_role(team_run, "design_director", plan_factory)
                if self.team_runtime else plan_factory(team_run))
        plan, visible_copy, exact_copy_fields = enforce_exact_user_copy(plan, instruction, visible_copy)
        if exact_copy_fields:
            plan["exact_copy_fields"] = exact_copy_fields
        # IP-Adapter can reproduce people/text from reference sheets. Automatic
        # mode therefore uses the model-authored vector/raster scene only.
        use_generative = backend in {"generative", "generative_sdxl"}
        background, generation_error = None, ""
        if use_generative:
            try:
                selected_generator = self.sdxl_backend if backend == "generative_sdxl" else self.generation_backend
                background = selected_generator.generate_background(
                    reference_paths=profile.reference_paths,
                    prompt=f"{profile.generation_prompt}. {instruction}. {plan.get('rationale', '')}",
                    orientation=profile.orientation, seed=seed)
            except Exception as exc:
                if backend in {"generative", "generative_sdxl"}: raise
                generation_error, use_generative = str(exc), False
        plan, grounded_fields = self._enforce_detected_subject_visibility(plan, paths, instruction)
        if grounded_fields:
            plan["source_grounded_fields"] = grounded_fields
        def render_current(current_plan):
            graph = scene_plan_to_layer_graph(current_plan)
            validate_layer_graph(graph, asset_count=len(paths))
            # A generated pixel background cannot be represented as a reusable
            # source layer yet, so that explicit backend keeps the compatibility renderer.
            if background is not None:
                return self._render_scene_plan(current_plan, paths, background=background), graph, "pillow-generative-composite-v1", ""
            ratio = float(current_plan["canvas"]["aspect_ratio"])
            svg_width, svg_height = ((1600, max(900, int(1600 / ratio))) if ratio >= 1 else
                                     (max(900, int(1600 * ratio)), 1600))
            svg = layer_graph_to_svg(graph, paths, svg_width, svg_height)
            try:
                return render_svg_with_qt(svg, svg_width, svg_height), graph, "qt-svg-layer-graph-v1", svg
            except Exception:
                return self._render_scene_plan(current_plan, paths), graph, "pillow-layer-graph-fallback-v1", svg

        rendered = (self.team_runtime.execute_role(team_run, "renderer", lambda _run: render_current(plan))
                    if self.team_runtime else render_current(plan))
        canvas, layer_graph, renderer_name, editable_svg = rendered
        quality_verdict = {"passed": True, "score": 1.0, "violations": [], "skipped": True}
        correction_history = []
        if self.enable_visual_review:
            review_root = Path("data/runtime/mockup_reviews").resolve()
            review_root.mkdir(parents=True, exist_ok=True)
            for correction_index in range(Config.MOCKUP_MAX_CORRECTIONS + 1):
                review_path = review_root / f"review_{uuid.uuid4().hex}.png"
                canvas.convert("RGB").save(review_path, "PNG")
                quality_verdict = self._review_rendered_image(
                    review_path, profile, instruction, visible_copy, plan
                )
                if self.team_runtime and correction_index == 0:
                    team_run.artifacts["rendered_image"] = str(review_path)
                    self.team_runtime.execute_role(team_run, "visual_critic", lambda _run: quality_verdict)
                review_path.unlink(missing_ok=True)
                quality_verdict["skipped"] = False
                if quality_verdict.get("passed") or quality_verdict.get("review_error") or correction_index >= Config.MOCKUP_MAX_CORRECTIONS:
                    break
                correction = str(quality_verdict.get("correction_instruction", "")).strip()
                if not correction:
                    break
                try:
                    revised, _copy, fields = self._request_scene_edit_patch(
                        profile, paths, plan, visible_copy, correction,
                        memory_context="시각 품질 검수 자동 교정",
                    )
                    if not scene_changed(plan, revised):
                        break
                    plan = revised
                    correction_history.append({"instruction": correction, "fields": fields})
                    if self.team_runtime:
                        team_run.artifacts["layer_graph"] = scene_plan_to_layer_graph(plan)
                        self.team_runtime.execute_role(team_run, "corrector", lambda _run: team_run.artifacts["layer_graph"])
                    canvas, layer_graph, renderer_name, editable_svg = render_current(plan)
                except Exception as exc:
                    correction_history.append({"instruction": correction, "error": str(exc)})
                    break
        if quality_verdict.get("review_error"):
            raise ScenePlanError("렌더링 결과를 시각적으로 검수하지 못해 미리보기를 제공하지 않았습니다: " +
                                 "; ".join(quality_verdict.get("violations", [])))
        if self.enable_visual_review and not quality_verdict.get("passed"):
            raise ScenePlanError(
                "렌더링 결과가 시각 품질 검수를 통과하지 못해 미리보기를 제공하지 않았습니다: "
                + "; ".join(quality_verdict.get("violations", []))
            )
        output_root = (Path(tempfile.gettempdir()) / "jarvis_mockup_previews" if preview_only else Path(output_dir).expanduser().resolve())
        output_root.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c for c in basename if c.isalnum() or c in "-_ ").strip() or "mockup"
        suffix = uuid.uuid4().hex if preview_only else time.strftime('%Y%m%d_%H%M%S')
        output_path = output_root / f"{safe_name}_{suffix}.png"
        output_mode = "RGBA" if plan.get("canvas", {}).get("background") == "transparent" else "RGB"
        canvas.convert(output_mode).save(output_path, "PNG", optimize=True)
        svg_path = output_path.with_suffix(".svg")
        if editable_svg:
            svg_path.write_text(editable_svg, encoding="utf-8")
        metadata = {
            "output": str(output_path), "profile_id": profile.profile_id, "references": profile.reference_hashes,
            "production_inputs": [self._sha256(path) for path in paths], "production_sources": [str(path) for path in paths],
            "width": canvas.width, "height": canvas.height, "renderer": "ai-scene-plan-renderer-v3",
            "render_engine": renderer_name,
            "editable_svg": str(svg_path) if editable_svg else "",
            "generation_backend": "generative" if use_generative else "model_planned_local",
            "reference_pixels_sent_to_generator": bool(use_generative),
            "seed": int(seed), "scene_plan": plan, "composition_plan": plan["assets"],
            "instruction": instruction.strip(), "visible_copy": visible_copy.strip(), "preview_only": bool(preview_only),
            "guidance_sources": [str(Path(path).resolve()) for path in (guidance_paths or [])],
            "generation_fallback_reason": generation_error, "style_recipe": profile.design_recipe,
            "layer_graph": layer_graph, "quality_verdict": quality_verdict,
            "automatic_corrections": correction_history,
            "subject_evidence": subject_evidence,
            "team_events": team_run.events,
        }
        if not preview_only: output_path.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
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
        editable_svg = Path(str(result.get("editable_svg", ""))) if result.get("editable_svg") else None
        if editable_svg and editable_svg.is_file():
            target_svg = target.with_suffix(".svg")
            shutil.copy2(editable_svg, target_svg)
            result["editable_svg"] = str(target_svg)
        result.update({"output": str(target), "preview_only": False, "saved_from_preview": str(source)})
        profile_id = str(result.get("profile_id", ""))
        if profile_id:
            self.style_index.add(profile_id, target, kind="approved_result", approved=True,
                                 metadata={"instruction": result.get("edit_instruction") or result.get("instruction", ""),
                                           "quality_verdict": result.get("quality_verdict", {})})
        target.with_suffix(".json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    @staticmethod
    def _merge_structured_edit(current: dict, instruction: str) -> tuple[dict, list[str]]:
        state, applied = dict(current or {}), []
        value = " ".join(str(instruction).split())
        colors = {
            "빨간": "#e5484d", "레드": "#e5484d", "파란": "#2878d0", "블루": "#2878d0",
            "보라": "#8b4cc2", "퍼플": "#8b4cc2", "민트": "#8cecd2", "초록": "#38a169",
            "노란": "#f2c94c", "옐로": "#f2c94c", "검정": "#111111", "블랙": "#111111",
            "흰색": "#ffffff", "화이트": "#ffffff", "분홍": "#ec6f9e", "핑크": "#ec6f9e",
        }
        color_mentions = [(match.start(), match.group(0))
                          for match in re.finditer(r"#[0-9a-fA-F]{6}\b", value)]
        color_mentions.extend((value.index(name), color) for name, color in colors.items() if name in value)
        for position, requested_color in sorted(color_mentions):
            prefix = value[:position]
            background_at = max(prefix.rfind("배경"), prefix.rfind("바탕"))
            border_at = max(prefix.rfind("테두리"), prefix.rfind("점선"), prefix.rfind("라인"))
            target = "border_color" if border_at > background_at else "background_color"
            state[target] = requested_color; applied.append(target)
        if re.search(r"실선", value): state["border_style"] = "solid"; applied.append("border_style")
        elif re.search(r"점선", value): state["border_style"] = "dashed"; applied.append("border_style")
        copy_match = re.search(r"(?:문구|텍스트|글자|카피)(?:를|는|은)?\s*[\"“']?(.+?)[\"”']?\s*(?:로\s*)?(?:바꿔|변경|수정|넣어|써|적어)", value)
        if copy_match:
            state["visible_copy"] = copy_match.group(1).strip(" '\"“”")
            state.pop("remove_copy", None)
            applied.append("visible_copy")
        if re.search(r"(?:문구|텍스트|글자|카피).{0,8}(?:없애|삭제|빼줘)", value):
            state["remove_copy"] = True; applied.append("remove_copy")
        return state, list(dict.fromkeys(applied))

    def edit_preview(self, metadata: dict, instruction: str, *, seed: int = 42,
                     guidance_paths=None, memory_context: str = "") -> dict:
        """Let the model revise the editable scene plan, then re-render from sources."""
        if not isinstance(metadata, dict) or not instruction.strip():
            raise ValueError("수정할 미리보기 정보와 구체적인 수정 지시가 필요합니다.")
        profile_id = str(metadata.get("profile_id", ""))
        sources = metadata.get("production_sources") or []
        if not profile_id or not sources:
            raise ValueError("이전 방식으로 만든 결과에는 원본 연결 정보가 없습니다. 원본 사진으로 시안을 한 번 다시 만들어 주세요.")
        previous_plan = metadata.get("scene_plan")
        if not isinstance(previous_plan, dict):
            raise ValueError("이전 결과에 AI 디자인 설계도가 없습니다. 새 파이프라인으로 시안을 다시 생성해 주세요.")
        profile = self.load_profile(profile_id)
        paths = self._validate_images(sources)
        # A repeated, measurable request is a successful idempotent operation,
        # not a model failure.  Check it before asking the model to invent a
        # delta; otherwise a correct no-op patch is retried three times and is
        # eventually reported as an error.
        explicit_plan, explicit_fields = enforce_explicit_user_constraints(previous_plan, instruction)
        explicit_plan, grounded_fields = self._enforce_detected_subject_visibility(
            explicit_plan, paths, instruction
        )
        recognized_noop = bool(explicit_fields or grounded_fields or re.search(
            r"글꼴(?:을|은)?\s*['\"][^'\"]+['\"]|(?:글자\s*)?크기(?:를|는)?\s*\d{1,3}\s*픽셀",
            instruction,
            re.I,
        ))
        # JSON equality is not proof of visual success. Re-render and run the
        # image critic instead of returning the previous bitmap here.
        if recognized_noop and not scene_changed(previous_plan, explicit_plan):
            # Re-render the plan and run the real-image critic. This preserves
            # idempotent UX without trusting stale JSON or an old bitmap.
            verification = self.render(
                profile_id, sources, instruction=str(metadata.get("instruction", "")),
                visible_copy=str(metadata.get("visible_copy", "")), scene_plan=explicit_plan,
                backend="auto", seed=seed, preview_only=True,
                guidance_paths=guidance_paths, memory_context=memory_context,
            )
            # The extra render exists only to prove the current visual output
            # still satisfies the request. Keep the user's active preview and
            # revision stable instead of silently replacing it with a duplicate.
            verified_output = Path(str(verification.get("output", "")))
            for artifact in (
                verified_output,
                verified_output.with_suffix(".json"),
                verified_output.with_suffix(".svg"),
            ):
                try:
                    if artifact.is_file():
                        artifact.unlink()
                except OSError:
                    pass
            result = deepcopy(metadata)
            result.update({
                "renderer": "verified-idempotent-edit-v1",
                "edit_instruction": instruction.strip(),
                "applied_edit_fields": [],
                "already_satisfied": True,
                "revision": int(metadata.get("revision", 0)),
                "quality_verdict": verification.get("quality_verdict", {}),
            })
            return result
        try:
            revised_plan, revised_copy, patch_fields = self._request_scene_edit_patch(
                profile, paths, previous_plan, str(metadata.get("visible_copy", "")), instruction.strip(),
                guidance_paths=guidance_paths, memory_context=memory_context,
            )
            renderer = "ai-scene-patch-v5"
        except ScenePlanError as model_error:
            revised_plan, patch_fields = enforce_explicit_user_constraints(previous_plan, instruction)
            revised_plan, grounded_fields = self._enforce_detected_subject_visibility(
                revised_plan, paths, instruction
            )
            patch_fields = list(dict.fromkeys([*patch_fields, *grounded_fields]))
            if not scene_changed(previous_plan, revised_plan):
                if not patch_fields:
                    raise model_error
                revised_copy = str(metadata.get("visible_copy", ""))
                renderer = "structured-scene-patch-v4"
            validate_patch_against_instruction(
                instruction, patch_fields, before=previous_plan, after=revised_plan,
            )
            revised_copy = str(metadata.get("visible_copy", ""))
            renderer = "structured-scene-patch-v4"
        revised_plan, grounded_fields = self._enforce_detected_subject_visibility(
            revised_plan, paths, instruction
        )
        patch_fields = list(dict.fromkeys([*patch_fields, *grounded_fields]))
        revised_plan, revised_copy, exact_fields = enforce_exact_user_copy(
            revised_plan, instruction, revised_copy
        )
        patch_fields = list(dict.fromkeys([*patch_fields, *exact_fields]))
        result = self.render(
            profile_id, sources, instruction=str(metadata.get("instruction", "")),
            visible_copy=revised_copy, scene_plan=revised_plan,
            backend="auto", seed=seed, preview_only=True,
            guidance_paths=guidance_paths, memory_context=memory_context,
        )
        result.update({"renderer": renderer,
                       "edit_instruction": instruction.strip(), "applied_edit_fields": patch_fields,
                       "revision": int(metadata.get("revision", 0)) + 1})
        return result

    def _request_scene_edit_patch(self, profile: MockupStyleProfile, paths: list[Path],
                                  previous_plan: dict, visible_copy: str,
                                  instruction: str, guidance_paths=None,
                                  memory_context: str = "") -> tuple[dict, str, list[str]]:
        """Ask the vision model for a delta and verify that the delta changes the scene."""
        vision = self.vision or VisionRuntime()
        planner = self._get_scene_planner()
        guidance = self._validate_images(guidance_paths or []) if guidance_paths else []
        evidence_paths = [*profile.reference_paths[:8], *[str(path) for path in paths[:4]],
                          *[str(path) for path in guidance[:4]]]
        guidance_observation = "첨부 없음"
        if guidance:
            try:
                guidance_observation = str(vision.analyze(
                    [str(path) for path in guidance[:4]],
                    "수정 참고사진과 설명 스케치에서 화살표, 박스, 강조 영역, 원하는 배치 관계만 관찰하세요. 최종 픽셀로 복사하지 마세요.",
                    mode="general",
                ).get("analysis", ""))[:6000]
            finally:
                release = getattr(vision, "release_model", None)
                if callable(release): release()
        allowed_scopes = sorted(infer_edit_scopes(instruction))
        allowed_scope_text = ", ".join(allowed_scopes) if allowed_scopes else "명령에서 직접 지칭한 대상만"
        last_error = ""
        previous_answer = ""
        for attempt in range(3):
            correction = (f"\n이전 패치 검증 오류: {last_error}\n이전 응답: {previous_answer}\n"
                          "오류 원인만 교정하고 실제로 달라지는 필드만 다시 작성하세요."
                          if last_error else "")
            prompt = f"""당신은 비파괴 이미지 편집 명령 해석기입니다.
사용자 수정 명령을 기존 전체 설계도가 아니라 최소 변경 패치로 변환하세요.
사용자 명령: {instruction}
현재 표시 문구: {visible_copy or '없음'}
현재 설계도: {json.dumps(previous_plan, ensure_ascii=False)}
학습된 스타일 근거: {json.dumps(profile.style_features, ensure_ascii=False)}
첨부된 수정 참고 이미지/스케치 수: {len(guidance)}. 스케치는 픽셀 복사가 아니라 위치·화살표·영역 의도로 해석하세요.
첨부 시각자료 관찰: {guidance_observation}
작업공간의 승인된 과거 기억: {memory_context or '없음'}

규칙:
1. 사용자가 명시한 대상과 그 요청을 수행하는 데 필수적인 필드만 패치하세요.
2. 요청하지 않은 텍스트 크기·위치·색, 사진 배치, 배경, 장식은 패치에 넣지 마세요.
3. assets와 texts의 index는 현재 설계도의 index입니다. 변경하지 않는 요소는 배열에서 생략하세요.
4. 문구 내용 변경은 visible_copy에 새 문구를 쓰고, 삭제는 remove_visible_copy=true로 지정하세요.
5. decorations 전체를 바꿀 때만 replace_decorations=true로 지정하세요. 장식 하나를 추가·수정·삭제할 때는 replace_decorations=false이고 decorations 항목에 action(add/update/remove)과 index를 넣으세요. 점선은 dash=true, 점 길이와 간격은 dash_length/gap_length로 지정하세요.
5-1. '사진/프레임 안쪽' 같은 상대 위치는 해당 asset의 x/y/width/height를 기준으로 여백을 빼서 decoration 좌표를 계산하세요. 참고 이미지나 스케치의 선·박스·화살표는 이 장식 또는 배치 좌표로 변환하세요.
6. success_criteria에는 결과 이미지에서 확인 가능한 완료 조건을 구체적으로 쓰세요.
7. 값이 현재와 같은 패치는 실패입니다. 명령을 실제 시각 변화로 변환하세요.
8. 이번 명령에서 허용된 변경 그룹은 [{allowed_scope_text}]입니다. 이 밖의 그룹은 빈 배열/빈 객체로 두세요.
9. 방향·크기·색상 표현은 같은 절에서 가장 가까운 대상 명사에만 연결하세요. 예를 들어 '스티커 오른쪽이 잘림'은 텍스트 오른쪽 이동이 아닙니다.
10. 사진·인물·피사체 자체의 확대/축소는 assets[index].zoom으로, 사진 내부 초점 이동은 focal_x/focal_y로 표현하세요.
11. 스티커·프레임·사진 영역 자체의 크기/위치 요청에만 width/height/x/y를 사용하세요. 원형 프레임은 width와 height를 같은 비율로 유지하세요.
{correction}
JSON Schema에 맞는 객체만 반환하세요."""
            try:
                if planner is not None:
                    previous_answer = str(planner.chat_structured([
                        {"role": "system", "content": (
                            "당신은 기존 장면 JSON을 보존하면서 사용자 지시를 최소 변경 패치로 "
                            "컴파일하는 편집 계획기입니다.")},
                        {"role": "user", "content": prompt},
                    ], json_schema=SCENE_EDIT_PATCH_JSON_SCHEMA))
                else:
                    response = vision.analyze(evidence_paths, prompt, mode="general",
                                              json_schema=SCENE_EDIT_PATCH_JSON_SCHEMA)
                    previous_answer = str(response.get("analysis", ""))
            except TypeError:
                response = vision.analyze(evidence_paths, prompt, mode="general")
                previous_answer = str(response.get("analysis", ""))
            try:
                patch = extract_json_object(previous_answer)
                patch, removed_groups = filter_scene_edit_patch(patch, instruction)
                revised, revised_copy, fields = apply_scene_edit_patch(
                    previous_plan, patch, asset_count=len(paths), visible_copy=visible_copy,
                )
                validate_patch_against_instruction(
                    instruction, fields, before=previous_plan, after=revised,
                )
                verdict_prompt = f"""당신은 이미지 편집 결과 의미 검증기입니다.
사용자 명령: {instruction}
수정 전 설계도: {json.dumps(previous_plan, ensure_ascii=False)}
적용된 패치: {json.dumps(patch, ensure_ascii=False)}
수정 후 설계도: {json.dumps(revised, ensure_ascii=False)}
실제 변경 필드: {json.dumps(fields, ensure_ascii=False)}
사용자 명령의 모든 요구가 수정 후 수치와 속성에 실제 반영됐는지 검사하세요.
명령하지 않은 요소가 바뀌었거나, 관련 없는 미세 변경만 있고 핵심 요구가 빠졌다면 fulfilled=false입니다.
JSON Schema에 맞는 판정만 반환하세요."""
                if planner is not None:
                    verdict_answer = planner.chat_structured([
                        {"role": "system", "content": "장면 패치의 의미 충족 여부만 JSON으로 판정하세요."},
                        {"role": "user", "content": verdict_prompt},
                    ], json_schema=SCENE_EDIT_VERDICT_JSON_SCHEMA)
                else:
                    try:
                        verdict_response = vision.analyze(evidence_paths, verdict_prompt, mode="general",
                                                          json_schema=SCENE_EDIT_VERDICT_JSON_SCHEMA)
                    except TypeError:
                        verdict_response = vision.analyze(evidence_paths, verdict_prompt, mode="general")
                    verdict_answer = str(verdict_response.get("analysis", ""))
                verdict = extract_json_object(str(verdict_answer))
                if not bool(verdict.get("fulfilled")) or verdict.get("missing_requirements") or verdict.get("unintended_changes"):
                    reason = str(verdict.get("reason", "요구 충족 실패"))
                    missing = ", ".join(map(str, verdict.get("missing_requirements", [])))
                    unintended = ", ".join(map(str, verdict.get("unintended_changes", [])))
                    raise ScenePlanError(f"의미 검증 실패: {reason}; 누락={missing or '없음'}; 의도 밖 변경={unintended or '없음'}")
                revised["edit_semantic_verdict"] = str(verdict.get("reason", "검증 통과"))[:500]
                if removed_groups:
                    revised["filtered_unrequested_groups"] = removed_groups
                return revised, revised_copy, fields
            except ScenePlanError as exc:
                last_error = str(exc)
        raise ScenePlanError(f"AI가 수정 명령을 3회 해석했지만 유효한 변경 패치를 만들지 못했습니다: {last_error}")

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

    def adjust_preview(self, base_path: str | Path, adjustments: dict[str, float]) -> dict:
        """Apply all four controls once to a stable base, avoiding cumulative degradation."""
        source = Path(base_path).expanduser().resolve()
        if not source.is_file():
            raise ValueError("조정할 미리보기 원본이 없습니다.")
        from PIL import ImageEnhance
        values = {name: max(0.0, min(2.0, float(adjustments.get(name, 1.0))))
                  for name in ("brightness", "contrast", "saturation", "sharpness")}
        with Image.open(source) as opened:
            image = opened.convert("RGB")
            image = ImageEnhance.Brightness(image).enhance(values["brightness"])
            image = ImageEnhance.Contrast(image).enhance(values["contrast"])
            image = ImageEnhance.Color(image).enhance(values["saturation"])
            image = ImageEnhance.Sharpness(image).enhance(values["sharpness"])
        preview_root = Path(tempfile.gettempdir()) / "jarvis_mockup_previews"
        preview_root.mkdir(parents=True, exist_ok=True)
        target = preview_root / f"adjusted_{uuid.uuid4().hex}.png"
        image.save(target, "PNG", optimize=True)
        return {"output": str(target), "preview_only": True, "operation": "combined_adjustment",
                "renderer": "pillow-live-adjustment-v2", "adjustment_base": str(source),
                "adjustments": values, "width": image.width, "height": image.height}
