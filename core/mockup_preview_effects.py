"""Replayable whole-preview edits, independent of model-authored scene layers.

The order is intentional: a brightness adjustment followed by a flip is not
recomputed from an old bitmap when the next AI edit re-renders the scene.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy

from PIL import Image, ImageEnhance, ImageOps

from core.mockup_document import scene_digest
from core.utterance_scope import mask_quoted_payloads


ADJUSTMENTS = ("brightness", "contrast", "saturation", "sharpness")
TRANSFORMS = ("rotate_left", "rotate_right", "flip_horizontal", "flip_vertical")


def normalized_adjustments(values: dict) -> dict[str, float]:
    if not isinstance(values, dict) or set(values) - set(ADJUSTMENTS):
        raise ValueError("이미지 조정에는 밝기·대비·채도·선명도만 사용할 수 있습니다.")
    result = {}
    for name in ADJUSTMENTS:
        value = values.get(name, 1.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} 조정값은 유한한 숫자여야 합니다.")
        if not 0 <= value <= 2:
            raise ValueError(f"{name} 조정값은 0부터 2까지여야 합니다.")
        result[name] = float(value)
    return result


def normalize_preview_effects(effects) -> list[dict]:
    if effects is None:
        return []
    if not isinstance(effects, list) or len(effects) > 128:
        raise ValueError("미리보기 편집 이력이 손상되었거나 지원 길이를 초과했습니다.")
    result = []
    for effect in effects:
        if not isinstance(effect, dict):
            raise ValueError("미리보기 편집 이력 항목은 객체여야 합니다.")
        operation = effect.get("operation")
        if operation in TRANSFORMS and set(effect) == {"operation"}:
            result.append({"operation": operation})
        elif operation == "adjust" and set(effect) == {"operation", "values"}:
            values = normalized_adjustments(effect["values"])
            if any(value != 1.0 for value in values.values()):
                result.append({"operation": "adjust", "values": values})
        else:
            raise ValueError("지원하지 않거나 잘못된 미리보기 편집 이력입니다.")
    return result


def apply_preview_effects(image: Image.Image, effects) -> Image.Image:
    """Apply validated edits without mutating pixels or multiplying alpha."""
    result = image.copy()
    for effect in normalize_preview_effects(effects):
        operation = effect["operation"]
        if operation == "rotate_left":
            result = result.transpose(Image.Transpose.ROTATE_90)
        elif operation == "rotate_right":
            result = result.transpose(Image.Transpose.ROTATE_270)
        elif operation == "flip_horizontal":
            result = ImageOps.mirror(result)
        elif operation == "flip_vertical":
            result = ImageOps.flip(result)
        else:
            alpha = result.convert("RGBA").getchannel("A")
            rgb = result.convert("RGB")
            for name, factory in (("brightness", ImageEnhance.Brightness),
                                  ("contrast", ImageEnhance.Contrast),
                                  ("saturation", ImageEnhance.Color),
                                  ("sharpness", ImageEnhance.Sharpness)):
                rgb = factory(rgb).enhance(effect["values"][name])
            if "A" in result.getbands() or "transparency" in result.info:
                rgb.putalpha(alpha)
            result = rgb
    return result


def undo_preview_geometry(image: Image.Image, effects) -> Image.Image:
    """Return pixels to scene coordinates for geometry-based verification.

    Photometric adjustments are deliberately retained: geometry contracts use
    alpha/placement, and attempting to numerically invert enhancements would
    introduce rounding errors. Only the exact D4 transforms are reversed.
    """
    result = image.copy()
    inverse = {"rotate_left": "rotate_right", "rotate_right": "rotate_left",
               "flip_horizontal": "flip_horizontal", "flip_vertical": "flip_vertical"}
    for effect in reversed(normalize_preview_effects(effects)):
        operation = effect["operation"]
        if operation not in inverse:
            continue
        result = apply_preview_effects(result, [{"operation": inverse[operation]}])
    return result


_DIRECTIONS = re.compile(
    r"(?<![가-힣A-Za-z])(?:오른쪽|우측|왼쪽|좌측|위쪽|윗쪽|상단|위로|아래쪽|아래로|하단|아래|밑)"
    r"(?=(?:으로|로|에|의|을|를|은|는|만|에서|$|[\s,.!?]))"
)


def instruction_in_source_coordinates(instruction: str, effects) -> str:
    """Compile screen-relative direction words into pre-effect scene axes.

    Whole-preview rotations/flips change what “right” means. The layer graph
    remains in source coordinates, so deterministic fallbacks and validators
    need this derived instruction. Quoted copy is never rewritten.
    """
    normalized = normalize_preview_effects(effects)
    matrix = (1, 0, 0, 1)  # display vector = M * source vector
    for effect in normalized:
        a, b, c, d = matrix
        operation = effect["operation"]
        if operation == "rotate_left":
            matrix = (c, d, -a, -b)
        elif operation == "rotate_right":
            matrix = (-c, -d, a, b)
        elif operation == "flip_horizontal":
            matrix = (-a, -b, c, d)
        elif operation == "flip_vertical":
            matrix = (a, b, -c, -d)
    if matrix == (1, 0, 0, 1):
        return str(instruction or "")
    vectors = {
        "오른쪽": (1, 0), "우측": (1, 0), "왼쪽": (-1, 0), "좌측": (-1, 0),
        "위쪽": (0, -1), "윗쪽": (0, -1), "상단": (0, -1), "위로": (0, -1),
        "아래쪽": (0, 1), "아래로": (0, 1), "하단": (0, 1), "아래": (0, 1), "밑": (0, 1),
    }
    canonical = {(1, 0): "오른쪽", (-1, 0): "왼쪽", (0, -1): "위쪽", (0, 1): "아래쪽"}
    masked = mask_quoted_payloads(str(instruction or ""))
    replacements = []
    a, b, c, d = matrix
    for match in _DIRECTIONS.finditer(masked):
        display_x, display_y = vectors[match.group(0)]
        source_vector = (a * display_x + c * display_y,
                         b * display_x + d * display_y)  # orthogonal inverse M^T
        replacement = canonical[source_vector]
        if match.group(0) in {"위로", "아래로"}:
            replacement += "으로"
        replacements.append((match.start(), match.end(), replacement))
    result = str(instruction or "")
    for start, end, replacement in reversed(replacements):
        result = result[:start] + replacement + result[end:]
    return result


def preview_state_digest(metadata: dict) -> str:
    plan = metadata.get("scene_plan")
    payload = {"scene": scene_digest(plan) if isinstance(plan, dict) else None,
               "effects": normalize_preview_effects(metadata.get("preview_effects")),
               "output_sha256": str(metadata.get("output_sha256", "")),
               "editable_svg_sha256": str(metadata.get("editable_svg_sha256", ""))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def assert_preview_state(metadata: dict) -> None:
    expected = metadata.get("preview_state_digest")
    if expected and expected != preview_state_digest(metadata):
        raise ValueError("현재 미리보기와 비파괴 편집 이력이 다릅니다. 최신 결과를 다시 선택해 주세요.")


def with_preview_state(metadata: dict) -> dict:
    result = deepcopy(metadata)
    result["preview_effects"] = normalize_preview_effects(result.get("preview_effects"))
    result["preview_state_digest"] = preview_state_digest(result)
    return result
