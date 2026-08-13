"""Model-authored, pattern-agnostic scene plans for reference-driven mockups."""
from __future__ import annotations

import json
import re
from copy import deepcopy


class ScenePlanError(ValueError):
    pass


SCENE_PLAN_JSON_SCHEMA = {
    "type": "object",
    "required": ["canvas", "assets", "decorations", "texts", "rationale"],
    "properties": {
        "canvas": {"type": "object", "required": ["aspect_ratio", "background"],
                   "properties": {"aspect_ratio": {"type": "number"},
                                  "background": {"type": "string"}}},
        "assets": {"type": "array", "items": {"type": "object",
                   "required": ["index", "x", "y", "width", "height", "shape", "fit",
                                "focal_x", "focal_y", "rotation", "z"],
                   "properties": {"index": {"type": "integer"}, "x": {"type": "number"},
                                  "y": {"type": "number"}, "width": {"type": "number"},
                                  "height": {"type": "number"},
                                  "shape": {"enum": ["rectangle", "rounded", "ellipse"]},
                                  "fit": {"enum": ["cover", "contain"]},
                                  "focal_x": {"type": "number"}, "focal_y": {"type": "number"},
                                  "rotation": {"type": "number"}, "z": {"type": "integer"}}}},
        "decorations": {"type": "array", "items": {"type": "object"}},
        "texts": {"type": "array", "items": {"type": "object",
                  "required": ["content", "x", "y", "width", "height", "font_size", "color",
                               "background", "align", "padding", "z"],
                  "properties": {"content": {"type": "string"}, "x": {"type": "number"},
                                 "y": {"type": "number"}, "width": {"type": "number"},
                                 "height": {"type": "number"}, "font_size": {"type": "number"},
                                 "color": {"type": "string"}, "background": {"type": "string"},
                                 "align": {"enum": ["left", "center", "right"]},
                                 "padding": {"type": "number"}, "z": {"type": "integer"}}}},
        "rationale": {"type": "string"},
    },
}


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
    ignored = {"rationale", "restored_required_elements", "enforced_measured_evidence",
               "enforced_user_constraints", "quality_review_fallback", "quality_review_rejected",
               "initial_plan_fallback", "edit_plan_fallback"}
    for mapping in (left, right):
        for key in ignored:
            mapping.pop(key, None)
    return left != right


def infer_edit_scopes(instruction: str) -> set[str]:
    """Identify the visual groups explicitly targeted by an edit request."""
    text = " ".join(str(instruction or "").lower().split())
    scopes = set()
    if any(word in text for word in ("문구", "텍스트", "글자", "글씨", "카피", "폰트")):
        scopes.add("texts")
    if any(word in text for word in ("배경", "캔버스")):
        scopes.add("canvas")
    if any(word in text for word in (
        "사진", "이미지", "인물", "사람", "얼굴", "머리", "전신", "상반신",
        "원형", "원 형태", "프레임", "틀", "크롭", "잘리", "잘라",
    )):
        scopes.add("assets")
    if any(word in text for word in ("테두리", "점선", "실선", "장식", "라인")):
        scopes.add("decorations")
    return scopes


def merge_scoped_scene_edit(before: dict, candidate: dict, instruction: str) -> tuple[dict, set[str]]:
    """Use explicit edit targets as a write mask over the previous plan."""
    scopes = infer_edit_scopes(instruction)
    if not scopes:
        return deepcopy(candidate), scopes
    merged = deepcopy(before)
    for key in scopes:
        if key in candidate:
            merged[key] = deepcopy(candidate[key])
    merged["rationale"] = str(candidate.get("rationale", before.get("rationale", "")))
    merged["edit_scopes"] = sorted(scopes)
    return merged, scopes


def build_evidence_fallback_plan(style_features: dict, *, asset_count: int,
                                 visible_copy: str = "") -> dict:
    """Build a usable neutral plan only from measured profile evidence.

    This is an availability fallback for a model that returned no parseable
    scene JSON. It does not select a named design template.
    """
    features = style_features if isinstance(style_features, dict) else {}
    consensus = features.get("consensus", {}) if isinstance(features.get("consensus"), dict) else {}
    references = features.get("references", []) if isinstance(features.get("references"), list) else []
    ratios = [float(item["aspect_ratio"]) for item in references
              if isinstance(item, dict) and item.get("aspect_ratio")]
    ratio = sorted(ratios)[len(ratios) // 2] if ratios else 1.0
    scale = max(.5, min(.88, float(consensus.get("subject_scale", .72) or .72)))
    shape = "ellipse" if consensus.get("primary_frame") == "circle" else "rounded"
    assets = []
    if asset_count == 1:
        assets.append({"index": 0, "x": round((1 - scale) / 2, 4), "y": .06,
                       "width": scale, "height": scale, "shape": shape, "fit": "contain",
                       "focal_x": .5, "focal_y": .42, "rotation": 0, "z": 1})
    else:
        columns = 2 if asset_count <= 4 else 3
        rows = (asset_count + columns - 1) // columns
        width = .84 / columns; height = min(.68 / rows, width)
        for index in range(asset_count):
            column, row = index % columns, index // columns
            assets.append({"index": index, "x": .08 + column * (.84 / columns),
                           "y": .06 + row * (.7 / rows), "width": width - .025,
                           "height": height - .025, "shape": shape, "fit": "contain",
                           "focal_x": .5, "focal_y": .45, "rotation": 0, "z": index + 1})
    texts = []
    copy = " ".join(str(visible_copy or "").split())[:160]
    if copy:
        texts.append({"content": copy, "x": .15, "y": .76, "width": .7, "height": .15,
                      "font_size": .06, "color": "#111111", "background": "#ffffffcc",
                      "align": "center", "padding": .018, "z": 10})
    raw = {"canvas": {"aspect_ratio": ratio, "background": "#ffffff"}, "assets": assets,
           "decorations": [], "texts": texts,
           "rationale": "Vision JSON 실패 시 학습 자료의 측정값으로 구성한 복구 설계도"}
    normalized = normalize_scene_plan(raw, asset_count=asset_count, visible_copy=copy)
    normalized["initial_plan_fallback"] = True
    normalized, _ = enforce_measured_style_evidence(normalized, features)
    return normalized


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
            area = float(asset.get("width", 0)) * float(asset.get("height", 0))
            if area < learned_scale ** 2 * .7 or area > learned_scale ** 2 * 1.3:
                asset.update({"x": round((1 - learned_scale) / 2, 4), "y": .06, "width": learned_scale,
                              "height": min(.88, learned_scale)})
                enforced.append("assets[0].learned_subject_occupancy")
            if primary_frame == "circle" and asset.get("shape") != "ellipse":
                asset["shape"] = "ellipse"
                enforced.append("assets[0].learned_primary_frame")
        # Text placement is not forced from the learned profile because an
        # explicit user placement request must take priority.
    if enforced:
        result["enforced_measured_evidence"] = enforced
    return result, enforced


def enforce_explicit_user_constraints(plan: dict, instruction: str) -> tuple[dict, list[str]]:
    """Apply only unambiguous, domain-wide layout constraints from user wording."""
    result = deepcopy(plan); applied = []
    text = " ".join(str(instruction or "").lower().split())
    subject_words = ("얼굴", "머리", "인물", "사람", "전신", "상반신")
    visibility_words = ("전부", "모두", "전체", "안 잘리", "안잘리", "보이", "나오", "포함")
    preserve_subject = any(word in text for word in subject_words) and any(
        word in text for word in visibility_words
    )
    if preserve_subject:
        for index, asset in enumerate(result.get("assets", [])):
            if asset.get("fit") != "contain":
                asset["fit"] = "contain"; applied.append(f"assets[{index}].fit=contain")
            asset["focal_x"], asset["focal_y"] = .5, .5
    circular_frame = any(word in text for word in ("원형", "원 형태", "동그랗"))
    if circular_frame:
        for index, asset in enumerate(result.get("assets", [])):
            width, height = float(asset.get("width", .8)), float(asset.get("height", .8))
            side = min(width, height)
            center_x = float(asset.get("x", 0)) + width / 2
            center_y = float(asset.get("y", 0)) + height / 2
            asset.update({"shape": "ellipse", "width": side, "height": side,
                          "x": round(max(0, min(1 - side, center_x - side / 2)), 4),
                          "y": round(max(0, min(1 - side, center_y - side / 2)), 4)})
            if any(word in text for word in ("원 형태가 아니", "원형으로", "동그랗게")):
                asset["fit"] = "cover"
            applied.append(f"assets[{index}].circular_frame")
    center_copy = any(word in text for word in ("문구", "텍스트", "글자", "글씨")) and any(
        word in text for word in ("정중앙", "가운데", "중앙")
    )
    if center_copy:
        for index, item in enumerate(result.get("texts", [])):
            width, height = float(item.get("width", .7)), float(item.get("height", .15))
            frame = result.get("assets", [{}])[0] if result.get("assets") else {}
            frame_x, frame_y = float(frame.get("x", 0)), float(frame.get("y", 0))
            frame_w, frame_h = float(frame.get("width", 1)), float(frame.get("height", 1))
            item["x"] = round(frame_x + (frame_w - width) / 2, 4)
            item["y"] = round(frame_y + (frame_h - height) / 2, 4)
            item["background"] = "transparent"
            if float(item.get("font_size", 0)) < .05:
                item["font_size"] = .055
            if width < .35:
                item["width"] = .5
                item["x"] = round(frame_x + (frame_w - .5) / 2, 4)
            applied.append(f"texts[{index}].primary_frame_center")
    color_map = {"파란": "#2878d0", "파랑": "#2878d0", "빨간": "#e5484d", "빨강": "#e5484d",
                 "검정": "#111111", "검은": "#111111", "흰색": "#ffffff", "하얀": "#ffffff",
                 "초록": "#38a169", "노란": "#f2c94c", "보라": "#8b4cc2"}
    copy_targeted = any(word in text for word in ("문구", "텍스트", "글자", "글씨"))
    if copy_targeted:
        requested = next((color for word, color in color_map.items() if word in text), None)
        for index, item in enumerate(result.get("texts", [])):
            if requested and item.get("color") != requested:
                item["color"] = requested; applied.append(f"texts[{index}].color")
            if any(word in text for word in ("더 크게", "크게", "키워", "키워줘")):
                old = float(item.get("font_size", .055))
                item["font_size"] = round(min(.16, max(.055, old * 1.5)), 4)
                item["width"] = min(.85, max(float(item.get("width", .5)), .5))
                item["height"] = min(.24, max(float(item.get("height", .12)), .12))
                applied.append(f"texts[{index}].size")
            half_size = any(word in text for word in ("절반", "반으로")) and any(
                word in text for word in ("줄여", "작게", "축소")
            )
            if half_size:
                item["font_size"] = round(max(.015, float(item.get("font_size", .055)) * .5), 4)
                applied.append(f"texts[{index}].size_half")
    if applied:
        result["enforced_user_constraints"] = applied
    return result, applied
