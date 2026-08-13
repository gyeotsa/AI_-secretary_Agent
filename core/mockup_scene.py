"""Model-authored, pattern-agnostic scene plans for reference-driven mockups."""
from __future__ import annotations

import json
import re
from copy import deepcopy


class ScenePlanError(ValueError):
    pass


def extract_json_object(text: str) -> dict:
    """Extract one JSON object without accepting prose as a successful plan."""
    value = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", value, re.IGNORECASE)
    candidates = [fenced.group(1)] if fenced else []
    start, end = value.find("{"), value.rfind("}")
    if start >= 0 and end > start:
        candidates.append(value[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    raise ScenePlanError("Vision 모델이 유효한 디자인 설계도(JSON)를 반환하지 않았습니다.")


def _number(value, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def _color(value, default="transparent") -> str:
    text = str(value or "").strip()
    if text == "transparent" or re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", text):
        return text
    return default


def normalize_scene_plan(raw: dict, *, asset_count: int, visible_copy: str = "") -> dict:
    """Validate the model plan while keeping visual decisions model-authored."""
    if not isinstance(raw, dict):
        raise ScenePlanError("디자인 설계도가 객체 형식이 아닙니다.")
    canvas = raw.get("canvas") if isinstance(raw.get("canvas"), dict) else {}
    result = {
        "schema_version": 3,
        "canvas": {
            "aspect_ratio": _number(canvas.get("aspect_ratio"), .55, 1.9, 1.0),
            "background": _color(canvas.get("background"), "#ffffff"),
        },
        "assets": [], "decorations": [], "texts": [],
        "rationale": str(raw.get("rationale", "")).strip()[:1200],
    }
    seen = set()
    for item in raw.get("assets", []):
        if not isinstance(item, dict):
            continue
        try: index = int(item.get("index"))
        except (TypeError, ValueError): continue
        if index < 0 or index >= asset_count or index in seen:
            continue
        seen.add(index)
        result["assets"].append({
            "index": index,
            "x": _number(item.get("x"), 0, .98, .1), "y": _number(item.get("y"), 0, .98, .1),
            "width": _number(item.get("width"), .05, 1, .8),
            "height": _number(item.get("height"), .05, 1, .8),
            "shape": str(item.get("shape", "rectangle")) if str(item.get("shape")) in
                     {"rectangle", "rounded", "ellipse"} else "rectangle",
            "fit": "contain" if item.get("fit") == "contain" else "cover",
            "focal_x": _number(item.get("focal_x"), 0, 1, .5),
            "focal_y": _number(item.get("focal_y"), 0, 1, .5),
            "rotation": _number(item.get("rotation"), -30, 30, 0),
            "z": int(_number(item.get("z"), -20, 20, 0)),
        })
    if seen != set(range(asset_count)):
        missing = sorted(set(range(asset_count)) - seen)
        raise ScenePlanError(f"AI 설계도에 제작용 이미지 배치가 누락되었습니다: {missing}")
    for item in raw.get("decorations", []):
        if not isinstance(item, dict) or item.get("type") not in {"rectangle", "ellipse", "line"}:
            continue
        result["decorations"].append({
            "type": item["type"], "x": _number(item.get("x"), 0, 1, 0),
            "y": _number(item.get("y"), 0, 1, 0), "width": _number(item.get("width"), 0, 1, 1),
            "height": _number(item.get("height"), 0, 1, 1),
            "fill": _color(item.get("fill")), "stroke": _color(item.get("stroke")),
            "stroke_width": _number(item.get("stroke_width"), 0, .05, .005),
            "dash": bool(item.get("dash", False)), "z": int(_number(item.get("z"), -20, 20, -1)),
        })
    requested_copy = " ".join(str(visible_copy or "").split())[:160]
    if requested_copy:
        matching = [item for item in raw.get("texts", []) if isinstance(item, dict)]
        if not matching:
            raise ScenePlanError("표시 문구가 요청되었지만 AI 설계도에 텍스트 영역이 없습니다.")
        item = matching[0]
        result["texts"] = [{
            "content": requested_copy,
            "x": _number(item.get("x"), 0, .95, .15), "y": _number(item.get("y"), 0, .95, .72),
            "width": _number(item.get("width"), .1, 1, .7),
            "height": _number(item.get("height"), .04, .5, .16),
            "font_size": _number(item.get("font_size"), .015, .2, .065),
            "color": _color(item.get("color"), "#111111"),
            "background": _color(item.get("background")),
            "align": str(item.get("align")) if item.get("align") in {"left", "center", "right"} else "center",
            "padding": _number(item.get("padding"), 0, .1, .018),
            "z": int(_number(item.get("z"), -20, 20, 10)),
        }]
    return result


def restore_required_elements(raw: dict, baseline: dict, *, asset_count: int,
                              visible_copy: str = "") -> tuple[dict, list[str]]:
    """Restore mandatory source/text elements dropped by a model review.

    The model still authors every visual value. Restoration only copies the last
    valid model-authored element; it never invents a template or placement.
    """
    repaired = deepcopy(raw) if isinstance(raw, dict) else {}
    restored = []
    candidate_assets = repaired.get("assets") if isinstance(repaired.get("assets"), list) else []
    present = set()
    for item in candidate_assets:
        try: present.add(int(item.get("index")))
        except (AttributeError, TypeError, ValueError): continue
    baseline_assets = {int(item["index"]): deepcopy(item) for item in baseline.get("assets", [])}
    for index in range(asset_count):
        if index not in present and index in baseline_assets:
            candidate_assets.append(baseline_assets[index]); restored.append(f"asset:{index}")
    repaired["assets"] = candidate_assets
    requested_copy = " ".join(str(visible_copy or "").split())[:160]
    candidate_texts = repaired.get("texts") if isinstance(repaired.get("texts"), list) else []
    if requested_copy and not any(isinstance(item, dict) for item in candidate_texts):
        if baseline.get("texts"):
            candidate_texts = [deepcopy(baseline["texts"][0])]; restored.append("text")
    repaired["texts"] = candidate_texts
    return repaired, restored


def scene_changed(before: dict, after: dict) -> bool:
    left, right = deepcopy(before), deepcopy(after)
    left.pop("rationale", None); right.pop("rationale", None)
    return left != right


def enforce_measured_style_evidence(plan: dict, style_features: dict) -> tuple[dict, list[str]]:
    """Enforce only high-confidence measurements, never a named visual template."""
    result = deepcopy(plan); enforced = []
    references = style_features.get("references", []) if isinstance(style_features, dict) else []
    ratios = [float(item["aspect_ratio"]) for item in references if isinstance(item, dict) and item.get("aspect_ratio")]
    if len(ratios) >= 2:
        ordered = sorted(ratios); measured = ordered[len(ordered) // 2]
        tolerance = max(.04, measured * .06)
        agreement = sum(abs(value - measured) <= tolerance for value in ratios) / len(ratios)
        proposed = float(result.get("canvas", {}).get("aspect_ratio", measured))
        if agreement >= .75 and abs(proposed - measured) > tolerance:
            result.setdefault("canvas", {})["aspect_ratio"] = round(measured, 4)
            enforced.append("canvas.aspect_ratio")
    consensus = style_features.get("consensus", {}) if isinstance(style_features, dict) else {}
    confidence = float(consensus.get("confidence", 0) or 0)
    evidence = consensus.get("evidence", {}) if isinstance(consensus, dict) else {}
    reference_count = int(evidence.get("reference_count", len(references)) or 0)
    if confidence >= .8 and reference_count >= 3:
        assets = result.get("assets", [])
        learned_scale = float(consensus.get("subject_scale", 0) or 0)
        primary_frame = consensus.get("primary_frame")
        if len(assets) == 1 and learned_scale >= .55:
            asset = assets[0]
            if float(asset.get("width", 0)) * float(asset.get("height", 0)) < learned_scale ** 2 * .7:
                asset.update({"x": round((1 - learned_scale) / 2, 4), "y": .06, "width": learned_scale,
                              "height": min(.88, learned_scale), "fit": "cover"})
                enforced.append("assets[0].learned_subject_occupancy")
            if primary_frame == "circle" and asset.get("shape") != "ellipse":
                asset["shape"] = "ellipse"
                enforced.append("assets[0].learned_primary_frame")
        if consensus.get("text_region") == "lower_overlay":
            for index, item in enumerate(result.get("texts", [])):
                if float(item.get("y", 0)) < .6 or float(item.get("height", 0)) > .22:
                    text_width = min(.82, max(.5, float(item.get("width", .7))))
                    item.update({"x": round((1 - text_width) / 2, 4), "y": .72, "width": text_width,
                                 "height": min(.18, max(.08, float(item.get("height", .12)))),
                                 "font_size": min(.085, max(.035, float(item.get("font_size", .055))))})
                    enforced.append(f"texts[{index}].learned_text_region")
    if enforced:
        result["enforced_measured_evidence"] = enforced
    return result, enforced
