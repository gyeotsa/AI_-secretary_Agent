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
                                "zoom", "focal_x", "focal_y", "rotation", "z"],
                   "properties": {"index": {"type": "integer"}, "x": {"type": "number"},
                                  "y": {"type": "number"}, "width": {"type": "number"},
                                  "height": {"type": "number"},
                                  "shape": {"enum": ["rectangle", "rounded", "ellipse"]},
                                  "fit": {"enum": ["cover", "contain"]},
                                  "zoom": {"type": "number"},
                                  "focal_x": {"type": "number"}, "focal_y": {"type": "number"},
                                  "rotation": {"type": "number"}, "z": {"type": "integer"}}}},
        "decorations": {"type": "array", "items": {"type": "object"}},
        "texts": {"type": "array", "items": {"type": "object",
                  "required": ["content", "x", "y", "width", "height", "font_size", "color",
                               "background", "align", "padding", "z"],
                  "properties": {"content": {"type": "string"}, "x": {"type": "number"},
                                 "y": {"type": "number"}, "width": {"type": "number"},
                                 "height": {"type": "number"}, "font_size": {"type": "number"},
                                 "font_family": {"type": "string"},
                                 "font_weight": {"enum": ["normal", "bold"]},
                                 "color": {"type": "string"}, "background": {"type": "string"},
                                 "align": {"enum": ["left", "center", "right"]},
                                 "padding": {"type": "number"}, "z": {"type": "integer"}}}},
        "rationale": {"type": "string"},
    },
}


SCENE_EDIT_PATCH_JSON_SCHEMA = {
    "type": "object",
    "required": ["intent_summary", "canvas", "assets", "texts", "replace_decorations",
                 "decorations", "visible_copy", "remove_visible_copy", "success_criteria"],
    "properties": {
        "intent_summary": {"type": "string"},
        "canvas": {"type": "object", "properties": {
            "aspect_ratio": {"type": "number"}, "background": {"type": "string"}}},
        "assets": {"type": "array", "items": {"type": "object", "required": ["index"],
            "properties": {"index": {"type": "integer"}, "x": {"type": "number"},
                "y": {"type": "number"}, "width": {"type": "number"},
                "height": {"type": "number"},
                "shape": {"enum": ["rectangle", "rounded", "ellipse"]},
                "fit": {"enum": ["cover", "contain"]}, "zoom": {"type": "number"},
                "focal_x": {"type": "number"},
                "focal_y": {"type": "number"}, "rotation": {"type": "number"},
                "z": {"type": "integer"}}}},
        "texts": {"type": "array", "items": {"type": "object",
            "required": ["index", "action"], "properties": {
                "index": {"type": "integer"}, "action": {"enum": ["update", "remove", "add"]},
                "x": {"type": "number"}, "y": {"type": "number"},
                "width": {"type": "number"}, "height": {"type": "number"},
                "font_size": {"type": "number"}, "color": {"type": "string"},
                "font_family": {"type": "string"},
                "font_weight": {"enum": ["normal", "bold"]},
                "background": {"type": "string"}, "align": {"enum": ["left", "center", "right"]},
                "padding": {"type": "number"}, "z": {"type": "integer"}}}},
        "replace_decorations": {"type": "boolean"},
        "decorations": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "action": {"enum": ["add", "update", "remove"]},
            "type": {"enum": ["rectangle", "ellipse", "line"]},
            "x": {"type": "number"}, "y": {"type": "number"},
            "width": {"type": "number"}, "height": {"type": "number"},
            "fill": {"type": "string"}, "stroke": {"type": "string"},
            "stroke_width": {"type": "number"}, "dash": {"type": "boolean"},
            "dash_length": {"type": "number"}, "gap_length": {"type": "number"},
            "z": {"type": "integer"},
        }}},
        "visible_copy": {"type": "string"},
        "remove_visible_copy": {"type": "boolean"},
        "success_criteria": {"type": "array", "items": {"type": "string"}},
    },
}

SCENE_EDIT_VERDICT_JSON_SCHEMA = {
    "type": "object",
    "required": ["fulfilled", "reason", "missing_requirements", "unintended_changes"],
    "properties": {
        "fulfilled": {"type": "boolean"}, "reason": {"type": "string"},
        "missing_requirements": {"type": "array", "items": {"type": "string"}},
        "unintended_changes": {"type": "array", "items": {"type": "string"}},
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


def _fit_normalized_box(item: dict) -> None:
    """Keep a normalized scene box fully inside its canvas."""
    width = min(1.0, max(0.0, float(item.get("width", 0))))
    height = min(1.0, max(0.0, float(item.get("height", 0))))
    item["width"], item["height"] = width, height
    item["x"] = round(min(max(0.0, float(item.get("x", 0))), 1.0 - width), 4)
    item["y"] = round(min(max(0.0, float(item.get("y", 0))), 1.0 - height), 4)


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
        normalized_asset = {
            "index": index,
            "x": _number(item.get("x"), 0, .98, .1), "y": _number(item.get("y"), 0, .98, .1),
            "width": _number(item.get("width"), .05, 1, .8),
            "height": _number(item.get("height"), .05, 1, .8),
            "shape": str(item.get("shape", "rectangle")) if str(item.get("shape")) in
                     {"rectangle", "rounded", "ellipse"} else "rectangle",
            "fit": "contain" if item.get("fit") == "contain" else "cover",
            "zoom": _number(item.get("zoom"), 1, 4, 1),
            "focal_x": _number(item.get("focal_x"), 0, 1, .5),
            "focal_y": _number(item.get("focal_y"), 0, 1, .5),
            "rotation": _number(item.get("rotation"), -30, 30, 0),
            "z": int(_number(item.get("z"), -20, 20, 0)),
        }
        _fit_normalized_box(normalized_asset)
        result["assets"].append(normalized_asset)
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
            "dash": bool(item.get("dash", False)),
            "dash_length": _number(item.get("dash_length"), .002, .2, .025),
            "gap_length": _number(item.get("gap_length"), .002, .2, .018),
            "z": int(_number(item.get("z"), -20, 20, -1)),
        })
    requested_copy = " ".join(str(visible_copy or "").split())[:160]
    if requested_copy:
        matching = [item for item in raw.get("texts", []) if isinstance(item, dict)]
        if not matching:
            raise ScenePlanError("표시 문구가 요청되었지만 AI 설계도에 텍스트 영역이 없습니다.")
        item = matching[0]
        normalized_text = {
            "content": requested_copy,
            "x": _number(item.get("x"), 0, .95, .15), "y": _number(item.get("y"), 0, .95, .72),
            "width": _number(item.get("width"), .1, 1, .7),
            "height": _number(item.get("height"), .04, .5, .16),
            "font_size": _number(item.get("font_size"), .015, .2, .065),
            "font_family": " ".join(str(item.get("font_family", "Malgun Gothic")).split())[:80],
            "font_weight": "normal" if item.get("font_weight") == "normal" else "bold",
            "color": _color(item.get("color"), "#111111"),
            "background": _color(item.get("background")),
            "align": str(item.get("align")) if item.get("align") in {"left", "center", "right"} else "center",
            "padding": _number(item.get("padding"), 0, .1, .018),
            "z": int(_number(item.get("z"), -20, 20, 10)),
        }
        _fit_normalized_box(normalized_text)
        normalized_text["padding"] = min(
            normalized_text["padding"],
            normalized_text["width"] * .2,
            normalized_text["height"] * .2,
        )
        result["texts"] = [normalized_text]
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


def filter_scene_edit_patch(patch: dict, instruction: str) -> tuple[dict, list[str]]:
    """Drop model-authored groups that the user did not request.

    Small local models often return a valid requested asset/canvas delta plus
    an unsolicited text tweak. Rejecting the whole answer loses the useful
    delta. This write mask removes only the unrelated groups; the remaining
    patch still goes through measurable validation afterwards.
    """
    if not isinstance(patch, dict):
        raise ScenePlanError("AI 수정 패치가 객체 형식이 아닙니다.")
    scopes = infer_edit_scopes(instruction)
    if not scopes:
        return deepcopy(patch), []
    result = {
        key: deepcopy(value) for key, value in patch.items()
        if key in {"intent_summary", "success_criteria"}
    }
    removed = []
    group_keys = {
        "canvas": ("canvas",),
        "assets": ("assets",),
        "texts": ("texts", "visible_copy", "remove_visible_copy"),
        "decorations": ("decorations", "replace_decorations"),
    }
    for group, keys in group_keys.items():
        for key in keys:
            if group in scopes:
                if key in patch:
                    result[key] = deepcopy(patch[key])
            elif key in patch:
                value = patch[key]
                meaningful = bool(value) if not isinstance(value, bool) else value
                if meaningful:
                    removed.append(group)
    return result, sorted(set(removed))


def apply_scene_edit_patch(before: dict, patch: dict, *, asset_count: int,
                           visible_copy: str) -> tuple[dict, str, list[str]]:
    """Apply a model-authored delta without allowing an implicit full-plan rewrite."""
    if not isinstance(patch, dict):
        raise ScenePlanError("AI 수정 패치가 객체 형식이 아닙니다.")
    raw = deepcopy(before); changed = []
    canvas_patch = patch.get("canvas") if isinstance(patch.get("canvas"), dict) else {}
    for key in ("aspect_ratio", "background"):
        if key in canvas_patch and raw.setdefault("canvas", {}).get(key) != canvas_patch[key]:
            raw["canvas"][key] = canvas_patch[key]; changed.append(f"canvas.{key}")
    assets = {int(item.get("index", -1)): item for item in raw.get("assets", []) if isinstance(item, dict)}
    for update in patch.get("assets", []):
        if not isinstance(update, dict): continue
        try: index = int(update.get("index"))
        except (TypeError, ValueError): continue
        target = assets.get(index)
        if target is None: continue
        for key in ("x", "y", "width", "height", "shape", "fit", "zoom", "focal_x", "focal_y", "rotation", "z"):
            if key in update and target.get(key) != update[key]:
                target[key] = update[key]; changed.append(f"assets[{index}].{key}")
    texts = [deepcopy(item) for item in raw.get("texts", []) if isinstance(item, dict)]
    for update in patch.get("texts", []):
        if not isinstance(update, dict): continue
        try: index = int(update.get("index", -1))
        except (TypeError, ValueError): continue
        action = update.get("action")
        if action == "remove" and 0 <= index < len(texts):
            texts.pop(index); changed.append(f"texts[{index}].remove"); continue
        if action == "add":
            base = {"content": str(patch.get("visible_copy") or visible_copy), "x": .15, "y": .72,
                    "width": .7, "height": .15, "font_size": .06, "font_family": "Malgun Gothic",
                    "font_weight": "bold", "color": "#111111",
                    "background": "transparent", "align": "center", "padding": .018, "z": 10}
            base.update({key: value for key, value in update.items() if key not in {"index", "action"}})
            texts.append(base); changed.append("texts.add"); continue
        if action == "update" and 0 <= index < len(texts):
            for key in ("x", "y", "width", "height", "font_size", "font_family", "font_weight", "color", "background", "align", "padding", "z"):
                if key in update and texts[index].get(key) != update[key]:
                    texts[index][key] = update[key]; changed.append(f"texts[{index}].{key}")
    raw["texts"] = texts
    if bool(patch.get("replace_decorations")):
        replacement = patch.get("decorations") if isinstance(patch.get("decorations"), list) else []
        if raw.get("decorations", []) != replacement:
            raw["decorations"] = deepcopy(replacement); changed.append("decorations.replace")
    elif isinstance(patch.get("decorations"), list):
        decorations = deepcopy(raw.get("decorations", []))
        for update in patch["decorations"]:
            if not isinstance(update, dict):
                continue
            action = str(update.get("action", "update"))
            try: index = int(update.get("index", len(decorations)))
            except (TypeError, ValueError): index = len(decorations)
            if action == "remove" and 0 <= index < len(decorations):
                decorations.pop(index); changed.append(f"decorations[{index}].remove")
                continue
            allowed = ("type", "x", "y", "width", "height", "fill", "stroke",
                       "stroke_width", "dash", "dash_length", "gap_length", "z")
            if action == "add":
                item = {"type": "ellipse", "x": .1, "y": .1, "width": .8, "height": .8,
                        "fill": "transparent", "stroke": "#ffffff", "stroke_width": .006,
                        "dash": False, "dash_length": .025, "gap_length": .018, "z": 5}
                item.update({key: update[key] for key in allowed if key in update})
                decorations.append(item); changed.append(f"decorations[{len(decorations)-1}].add")
            elif 0 <= index < len(decorations):
                for key in allowed:
                    if key in update and decorations[index].get(key) != update[key]:
                        decorations[index][key] = update[key]
                        changed.append(f"decorations[{index}].{key}")
        raw["decorations"] = decorations
    next_copy = str(visible_copy or "")
    if bool(patch.get("remove_visible_copy")):
        next_copy = ""; raw["texts"] = []; changed.append("visible_copy.remove")
    elif str(patch.get("visible_copy", "")).strip():
        proposed = " ".join(str(patch["visible_copy"]).split())[:160]
        if proposed != next_copy:
            next_copy = proposed; changed.append("visible_copy")
    normalized = normalize_scene_plan(raw, asset_count=asset_count, visible_copy=next_copy)
    if not changed or not scene_changed(before, normalized):
        raise ScenePlanError("AI 수정 패치에 실제 시각 변경이 없습니다.")
    normalized["edit_patch_summary"] = str(patch.get("intent_summary", ""))[:500]
    normalized["edit_patch_fields"] = list(dict.fromkeys(changed))
    normalized["edit_success_criteria"] = [str(item)[:240] for item in patch.get("success_criteria", [])[:8]]
    return normalized, next_copy, list(dict.fromkeys(changed))


def validate_patch_against_instruction(instruction: str, changed_fields: list[str],
                                       before: dict | None = None,
                                       after: dict | None = None) -> None:
    """Programmatically validate measurable parts of an edit request."""
    text = " ".join(str(instruction or "").lower().split())
    scopes = infer_edit_scopes(text)
    groups = {field.split(".", 1)[0].split("[", 1)[0] for field in changed_fields}
    if scopes:
        unexpected = groups - scopes - {"visible_copy"}
        if unexpected:
            raise ScenePlanError(f"명령하지 않은 영역을 변경했습니다: {sorted(unexpected)}")
    text_target = "texts" in scopes
    if text_target and any(word in text for word in (
        "옮겨", "이동", "위치", "왼쪽", "오른쪽", "위로", "아래로", "아래쪽", "밑",
        "상단", "하단", "중앙", "가운데",
    )):
        required_axes = set()
        if any(word in text for word in ("왼쪽", "오른쪽", "좌측", "우측")): required_axes.add("x")
        if any(word in text for word in ("위쪽", "아래", "아래쪽", "밑", "상단", "하단", "위로")): required_axes.add("y")
        if any(word in text for word in ("중앙", "가운데")): required_axes.update({"x", "y"})
        changed_axes = {field.rsplit(".", 1)[-1] for field in changed_fields
                        if field.startswith("texts[") and field.rsplit(".", 1)[-1] in {"x", "y"}}
        position_satisfied = False
        if after and after.get("texts") and any(word in text for word in ("중앙", "가운데")):
            item = after["texts"][0]
            frame = after.get("assets", [{}])[0] if after.get("assets") else {
                "x": 0, "y": 0, "width": 1, "height": 1,
            }
            text_center = (float(item.get("x", 0)) + float(item.get("width", 0)) / 2,
                           float(item.get("y", 0)) + float(item.get("height", 0)) / 2)
            frame_center = (float(frame.get("x", 0)) + float(frame.get("width", 1)) / 2,
                            float(frame.get("y", 0)) + float(frame.get("height", 1)) / 2)
            position_satisfied = (abs(text_center[0] - frame_center[0]) <= .03 and
                                  abs(text_center[1] - frame_center[1]) <= .03)
        if after and after.get("texts") and not position_satisfied:
            item = after["texts"][0]
            frame = after.get("assets", [{}])[0] if after.get("assets") else {
                "x": 0, "y": 0, "width": 1, "height": 1,
            }
            left_gap = float(item.get("x", 0)) - float(frame.get("x", 0))
            right_gap = (float(frame.get("x", 0)) + float(frame.get("width", 1)) -
                         float(item.get("x", 0)) - float(item.get("width", 0)))
            top_gap = float(item.get("y", 0)) - float(frame.get("y", 0))
            bottom_gap = (float(frame.get("y", 0)) + float(frame.get("height", 1)) -
                          float(item.get("y", 0)) - float(item.get("height", 0)))
            horizontal_ok = (not any(word in text for word in ("왼쪽", "좌측", "오른쪽", "우측")) or
                             (any(word in text for word in ("왼쪽", "좌측")) and left_gap <= .06) or
                             (any(word in text for word in ("오른쪽", "우측")) and right_gap <= .06))
            vertical_ok = (not any(word in text for word in
                                   ("위쪽", "위로", "상단", "아래", "아래쪽", "밑", "하단")) or
                           (any(word in text for word in ("위쪽", "위로", "상단")) and top_gap <= .06) or
                           (any(word in text for word in ("아래", "아래쪽", "밑", "하단")) and bottom_gap <= .06))
            position_satisfied = horizontal_ok and vertical_ok
        if required_axes and not required_axes.issubset(changed_axes) and not position_satisfied:
            raise ScenePlanError(f"텍스트 위치 요청의 좌표 변경이 부족합니다: 필요={sorted(required_axes)}")
        if not required_axes and not changed_axes:
            raise ScenePlanError("텍스트 위치 요청인데 x/y 위치 변경이 없습니다.")
    requested_colors = {"파란": "#2878d0", "파랑": "#2878d0", "빨간": "#e5484d",
                        "빨강": "#e5484d", "검정": "#111111", "검은": "#111111",
                        "흰색": "#ffffff", "하얀": "#ffffff", "초록": "#38a169",
                        "노란": "#f2c94c", "보라": "#8b4cc2"}
    requested_color = next((color for word, color in requested_colors.items() if word in text), None)
    color_changed = any(field.startswith("texts[") and field.endswith(".color")
                        for field in changed_fields)
    color_satisfied = bool(after and requested_color and after.get("texts") and
                           after["texts"][0].get("color") == requested_color)
    if text_target and requested_color and not color_changed and not color_satisfied:
        raise ScenePlanError("텍스트 색상 요청이 수정 후 결과에도 반영되지 않았습니다.")
    text_background_removal = text_target and any(word in text for word in (
        "텍스트박스", "텍스트 박스", "글씨 뒤 배경", "문구 뒤 배경", "글자 뒤 배경", "배경 없이",
    )) and any(word in text for word in ("없애", "제거", "투명", "빼줘", "지워", "없이"))
    background_changed = any(field.startswith("texts[") and field.endswith(".background")
                             for field in changed_fields)
    background_satisfied = bool(after and after.get("texts") and
                                after["texts"][0].get("background") == "transparent")
    if text_background_removal and not background_changed and not background_satisfied:
        raise ScenePlanError("텍스트 배경 제거 요청이 수정 후 결과에도 반영되지 않았습니다.")
    if text_target and any(word in text for word in ("크기", "크게", "작게", "줄여", "키워", "확대", "축소")) \
            and not any(field.startswith("texts[") and field.rsplit(".", 1)[-1] in
                        {"font_size", "width", "height"} for field in changed_fields):
        raise ScenePlanError("텍스트 크기 요청인데 크기 관련 변경이 없습니다.")
    pixel_match = re.search(r"(?:글자\s*)?크기(?:를|는)?\s*(\d{1,3})\s*픽셀", text)
    if text_target and pixel_match and after and after.get("texts"):
        expected = max(.015, min(.2, int(pixel_match.group(1)) / 1600))
        actual = float(after["texts"][0].get("font_size", 0))
        if abs(actual - expected) > .001:
            raise ScenePlanError(
                f"글자 크기 요청이 실제 캔버스 기준과 다릅니다: 요청={pixel_match.group(1)}px, "
                f"설계값={round(actual * 1600)}px"
            )
    if "canvas" in scopes and any(word in text for word in ("배경색", "바탕색")) \
            and "canvas.background" not in changed_fields:
        raise ScenePlanError("배경색 요청인데 canvas.background 변경이 없습니다.")
    if "decorations" in scopes:
        decorations = after.get("decorations", []) if after else []
        if not decorations:
            raise ScenePlanError("장식 요청인데 수정 후 장식 요소가 없습니다.")
        target = decorations[-1]
        if "점선" in text and not bool(target.get("dash")):
            raise ScenePlanError("점선 요청인데 장식의 dash가 활성화되지 않았습니다.")
        if "실선" in text and bool(target.get("dash")):
            raise ScenePlanError("실선 요청인데 장식이 점선으로 남아 있습니다.")
        if requested_color and target.get("stroke") != requested_color:
            raise ScenePlanError("테두리 색상 요청이 장식의 stroke에 반영되지 않았습니다.")
        if any(word in text for word in ("안쪽", "내부", "안에")) and after.get("assets"):
            frame = after["assets"][0]
            inside = (float(target.get("x", 0)) > float(frame.get("x", 0)) and
                      float(target.get("y", 0)) > float(frame.get("y", 0)) and
                      float(target.get("x", 0)) + float(target.get("width", 0)) <
                      float(frame.get("x", 0)) + float(frame.get("width", 0)) and
                      float(target.get("y", 0)) + float(target.get("height", 0)) <
                      float(frame.get("y", 0)) + float(frame.get("height", 0)))
            if not inside:
                raise ScenePlanError("안쪽 테두리 요청인데 장식이 사진 프레임 내부에 배치되지 않았습니다.")
    shape_satisfied = bool(after and after.get("assets") and
                           all(item.get("shape") == "ellipse" for item in after["assets"]))
    if "assets" in scopes and any(word in text for word in ("원형", "원 형태", "동그랗")) \
            and not any(field.startswith("assets[") and field.endswith(".shape")
                        for field in changed_fields) and not shape_satisfied:
        raise ScenePlanError("원형 프레임 요청인데 shape 변경이 없습니다.")
    photo_target = any(word in text for word in ("사진", "이미지", "인물", "피사체", "사람"))
    frame_target = any(word in text for word in ("스티커", "프레임", "영역", "틀"))
    if "assets" in scopes and photo_target and not frame_target:
        if any(word in text for word in ("확대", "축소", "크게", "작게", "줌")) and not any(
                field.startswith("assets[") and field.endswith(".zoom") for field in changed_fields):
            raise ScenePlanError("사진 자체의 확대·축소 요청은 프레임 크기가 아니라 zoom 변경이 필요합니다.")
        if any(word in text for word in ("왼쪽", "오른쪽", "위로", "아래로")) and not any(
                field.startswith("assets[") and field.rsplit(".", 1)[-1] in {"focal_x", "focal_y"}
                for field in changed_fields):
            raise ScenePlanError("사진 내부 이동 요청은 프레임 좌표가 아니라 focal 좌표 변경이 필요합니다.")
    if after and before:
        before_assets = {int(item.get("index", -1)): item for item in before.get("assets", [])}
        for item in after.get("assets", []):
            prior = before_assets.get(int(item.get("index", -1)))
            if not prior or prior.get("shape") != "ellipse":
                continue
            before_ratio = float(prior.get("width", 1)) / max(.001, float(prior.get("height", 1)))
            after_ratio = float(item.get("width", 1)) / max(.001, float(item.get("height", 1)))
            if abs(before_ratio - 1) <= .03 and abs(after_ratio - 1) > .03 and not any(
                    word in text for word in ("타원", "가로로", "세로로")):
                raise ScenePlanError("원형 프레임의 종횡비가 명령 없이 타원으로 변했습니다.")


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
                       "width": scale, "height": scale, "shape": shape, "fit": "contain", "zoom": 1,
                       "focal_x": .5, "focal_y": .42, "rotation": 0, "z": 1})
    else:
        columns = 2 if asset_count <= 4 else 3
        rows = (asset_count + columns - 1) // columns
        width = .84 / columns; height = min(.68 / rows, width)
        for index in range(asset_count):
            column, row = index % columns, index // columns
            assets.append({"index": index, "x": .08 + column * (.84 / columns),
                           "y": .06 + row * (.7 / rows), "width": width - .025,
                           "height": height - .025, "shape": shape, "fit": "contain", "zoom": 1,
                           "focal_x": .5, "focal_y": .45, "rotation": 0, "z": index + 1})
    texts = []
    copy = " ".join(str(visible_copy or "").split())[:160]
    if copy:
        texts.append({"content": copy, "x": .15, "y": .76, "width": .7, "height": .15,
                      "font_size": .06, "font_weight": "bold", "color": "#111111", "background": "#ffffffcc",
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
        if result.get("texts") and consensus.get("text_region") == "lower_overlay":
            frame = result.get("assets", [{}])[0]
            frame_x, frame_y = float(frame.get("x", 0)), float(frame.get("y", 0))
            frame_w, frame_h = float(frame.get("width", 1)), float(frame.get("height", 1))
            for index, item in enumerate(result["texts"]):
                width, height = float(item.get("width", .7)), float(item.get("height", .15))
                target_x = round(frame_x + (frame_w - width) / 2, 4)
                target_y = round(max(frame_y, frame_y + frame_h - height - .035), 4)
                if item.get("x") != target_x:
                    item["x"] = target_x; enforced.append(f"texts[{index}].learned_center")
                if item.get("y") != target_y:
                    item["y"] = target_y; enforced.append(f"texts[{index}].learned_lower_overlay")
                item["align"] = "center"
    if enforced:
        result["enforced_measured_evidence"] = enforced
    return result, enforced


def enforce_explicit_user_constraints(plan: dict, instruction: str) -> tuple[dict, list[str]]:
    """Apply only unambiguous, domain-wide layout constraints from user wording."""
    result = deepcopy(plan); applied = []
    raw_text = " ".join(str(instruction or "").split())
    text = raw_text.lower()
    clauses = [part.strip() for part in re.split(r"[.!?\n]+|,\s*|\s+그리고\s+|고\s+", text) if part.strip()]
    bound_clauses, active_target = [], None
    text_nouns = ("문구", "텍스트", "글자", "글씨", "카피", "폰트")
    asset_nouns = ("스티커", "사진", "이미지", "인물", "사람", "얼굴", "머리", "프레임",
                   "원형", "원 형태", "모양", "형태")
    for clause in clauses:
        if any(word in clause for word in text_nouns): active_target = "text"
        elif any(word in clause for word in asset_nouns): active_target = "asset"
        bound_clauses.append((active_target, clause))
    text_request = " ".join(clause for target, clause in bound_clauses if target == "text")
    asset_request = " ".join(clause for target, clause in bound_clauses if target == "asset")
    subject_words = ("얼굴", "머리", "인물", "사람", "전신", "상반신")
    visibility_words = ("전부", "모두", "전체", "안 잘리", "안잘리", "전부 보이", "모두 보이", "전체 보이")
    preserve_subject = any(word in text for word in subject_words) and any(
        word in text for word in visibility_words
    )
    if preserve_subject:
        for index, asset in enumerate(result.get("assets", [])):
            if asset.get("fit") != "contain":
                asset["fit"] = "contain"; applied.append(f"assets[{index}].fit")
            asset["focal_x"], asset["focal_y"] = .5, .5
    framing_request = asset_request
    if "상반신" in framing_request and any(word in framing_request for word in ("위로", "까지만", "만 나오", "위주")):
        for index, asset in enumerate(result.get("assets", [])):
            asset["fit"] = "cover"
            asset["zoom"] = max(1.55, float(asset.get("zoom", 1)))
            asset["focal_x"], asset["focal_y"] = .5, .3
            applied.extend([f"assets[{index}].fit", f"assets[{index}].zoom", f"assets[{index}].focal_x",
                            f"assets[{index}].focal_y"])
    circular_frame = any(word in asset_request for word in ("원형", "원 형태", "동그랗"))
    if circular_frame:
        for index, asset in enumerate(result.get("assets", [])):
            width, height = float(asset.get("width", .8)), float(asset.get("height", .8))
            side = min(width, height)
            center_x = float(asset.get("x", 0)) + width / 2
            center_y = float(asset.get("y", 0)) + height / 2
            asset.update({"shape": "ellipse", "width": side, "height": side,
                          "x": round(max(0, min(1 - side, center_x - side / 2)), 4),
                          "y": round(max(0, min(1 - side, center_y - side / 2)), 4)})
            if any(word in asset_request for word in ("원 형태가 아니", "원형으로", "동그랗게")):
                asset["fit"] = "cover"
            applied.extend([f"assets[{index}].shape", f"assets[{index}].width",
                            f"assets[{index}].height", f"assets[{index}].x", f"assets[{index}].y"])
            if asset.get("fit") == "cover": applied.append(f"assets[{index}].fit")
    center_copy = any(word in text_request for word in ("정중앙", "가운데", "중앙"))
    if center_copy:
        for index, item in enumerate(result.get("texts", [])):
            width, height = float(item.get("width", .7)), float(item.get("height", .15))
            frame = result.get("assets", [{}])[0] if result.get("assets") else {}
            frame_x, frame_y = float(frame.get("x", 0)), float(frame.get("y", 0))
            frame_w, frame_h = float(frame.get("width", 1)), float(frame.get("height", 1))
            item["x"] = round(frame_x + (frame_w - width) / 2, 4)
            item["y"] = round(frame_y + (frame_h - height) / 2, 4)
            item["background"] = "transparent"
            item["align"] = "center"
            item["padding"] = min(.025, float(item.get("padding", .018)))
            if float(item.get("font_size", 0)) < .05:
                item["font_size"] = .055
            if width < .35:
                item["width"] = .5
                item["x"] = round(frame_x + (frame_w - .5) / 2, 4)
            applied.extend([f"texts[{index}].x", f"texts[{index}].y",
                            f"texts[{index}].background"])
    copy_position = bool(text_request)
    horizontal = ("right" if any(word in text_request for word in ("오른쪽", "우측")) else
                  "left" if any(word in text_request for word in ("왼쪽", "좌측")) else None)
    vertical = ("bottom" if any(word in text_request for word in ("아래", "아래쪽", "밑", "하단")) else
                "top" if any(word in text_request for word in ("위쪽", "상단", "위로")) else None)
    if copy_position and (horizontal or vertical):
        frame = result.get("assets", [{}])[0] if result.get("assets") else {}
        frame_x, frame_y = float(frame.get("x", 0)), float(frame.get("y", 0))
        frame_w, frame_h = float(frame.get("width", 1)), float(frame.get("height", 1))
        margin = .025
        for index, item in enumerate(result.get("texts", [])):
            width, height = float(item.get("width", .7)), float(item.get("height", .15))
            if horizontal in {"right", "left"}:
                width = min(width, max(.22, frame_w * .48)); item["width"] = round(width, 4)
                item["align"] = horizontal
            if horizontal == "right": item["x"] = round(max(0, frame_x + frame_w - width - margin), 4)
            elif horizontal == "left": item["x"] = round(min(1 - width, frame_x + margin), 4)
            if vertical == "bottom": item["y"] = round(max(0, frame_y + frame_h - height - margin), 4)
            elif vertical == "top": item["y"] = round(min(1 - height, frame_y + margin), 4)
            if horizontal:
                applied.extend([f"texts[{index}].x", f"texts[{index}].width", f"texts[{index}].align"])
            if vertical: applied.append(f"texts[{index}].y")
    color_map = {"파란": "#2878d0", "파랑": "#2878d0", "빨간": "#e5484d", "빨강": "#e5484d",
                 "검정": "#111111", "검은": "#111111", "흰색": "#ffffff", "하얀": "#ffffff",
                 "초록": "#38a169", "노란": "#f2c94c", "보라": "#8b4cc2"}
    copy_targeted = bool(text_request)
    if copy_targeted:
        requested = next((color for word, color in color_map.items() if word in text_request), None)
        for index, item in enumerate(result.get("texts", [])):
            family_match = re.search(r"글꼴(?:을|은)?\s*['\"]([^'\"]+)['\"]", raw_text, re.I)
            if family_match and item.get("font_family") != family_match.group(1).strip():
                item["font_family"] = family_match.group(1).strip()[:80]
                applied.append(f"texts[{index}].font_family")
            pixel_match = re.search(r"(?:글자\s*)?크기(?:를|는)?\s*(\d{1,3})\s*픽셀", text_request)
            if pixel_match:
                normalized_size = max(.015, min(.2, int(pixel_match.group(1)) / 1600))
                if item.get("font_size") != normalized_size:
                    item["font_size"] = normalized_size; applied.append(f"texts[{index}].font_size")
            if "굵기는 보통" in text_request and item.get("font_weight") != "normal":
                item["font_weight"] = "normal"; applied.append(f"texts[{index}].font_weight")
            elif any(word in text_request for word in ("굵게", "볼드", "bold")) and item.get("font_weight") != "bold":
                item["font_weight"] = "bold"; applied.append(f"texts[{index}].font_weight")
            if requested and item.get("color") != requested:
                item["color"] = requested; applied.append(f"texts[{index}].color")
            background_targeted = any(word in text_request for word in
                                      ("텍스트박스", "텍스트 박스", "글씨 뒤 배경", "문구 뒤 배경",
                                       "글자 뒤 배경", "배경 없이", "배경을 투명", "배경 투명"))
            remove_background = any(word in text_request for word in
                                    ("없애", "제거", "투명", "빼줘", "지워", "없이"))
            if background_targeted and remove_background and item.get("background") != "transparent":
                item["background"] = "transparent"
                applied.append(f"texts[{index}].background")
            if any(word in text_request for word in ("더 크게", "크게", "키워", "키워줘")):
                old = float(item.get("font_size", .055))
                item["font_size"] = round(min(.16, max(.055, old * 1.5)), 4)
                item["width"] = min(.85, max(float(item.get("width", .5)), .5))
                item["height"] = min(.24, max(float(item.get("height", .12)), .12))
                applied.extend([f"texts[{index}].font_size", f"texts[{index}].width",
                                f"texts[{index}].height"])
            half_size = any(word in text for word in ("절반", "반으로")) and any(
                word in text for word in ("줄여", "작게", "축소")
            )
            if half_size:
                item["font_size"] = round(max(.015, float(item.get("font_size", .055)) * .5), 4)
                applied.append(f"texts[{index}].font_size")
            if not half_size and ("너무 크" in text_request or any(
                    word in text_request for word in ("작게", "줄여", "축소"))):
                item["font_size"] = round(max(.025, float(item.get("font_size", .055)) * .65), 4)
                item["width"] = min(float(item.get("width", .7)), .72)
                item["height"] = min(float(item.get("height", .15)), .2)
                item["padding"] = min(.025, float(item.get("padding", .018)))
                applied.extend([f"texts[{index}].font_size", f"texts[{index}].width",
                                f"texts[{index}].height", f"texts[{index}].padding"])
            if any(word in text_request for word in ("볼드", "굵게", "굵은", "진하게")):
                item["font_weight"] = "bold"; applied.append(f"texts[{index}].font_weight")
            if any(word in text_request for word in ("안에", "내부", "영역 안")):
                frame = result.get("assets", [{}])[0] if result.get("assets") else {}
                frame_x, frame_y = float(frame.get("x", 0)), float(frame.get("y", 0))
                frame_w, frame_h = float(frame.get("width", 1)), float(frame.get("height", 1))
                margin = min(.035, frame_w * .04, frame_h * .04)
                item["width"] = round(min(float(item.get("width", .7)), max(.1, frame_w - margin * 2)), 4)
                item["height"] = round(min(float(item.get("height", .15)), max(.04, frame_h - margin * 2)), 4)
                item["x"] = round(min(max(float(item.get("x", 0)), frame_x + margin),
                                      frame_x + frame_w - item["width"] - margin), 4)
                item["y"] = round(min(max(float(item.get("y", 0)), frame_y + margin),
                                      frame_y + frame_h - item["height"] - margin), 4)
                item["padding"] = min(float(item.get("padding", .018)),
                                      item["width"] * .08, item["height"] * .08)
                applied.extend([f"texts[{index}].x", f"texts[{index}].y",
                                f"texts[{index}].width", f"texts[{index}].height",
                                f"texts[{index}].padding"])
            _fit_normalized_box(item)
    decoration_request = any(word in text for word in ("테두리", "점선", "실선", "라인", "장식"))
    if decoration_request and result.get("assets"):
        color_map = {"하얀": "#ffffff", "흰색": "#ffffff", "흰": "#ffffff",
                     "검정": "#111111", "검은": "#111111", "파란": "#2878d0",
                     "빨간": "#e5484d", "초록": "#38a169", "노란": "#f2c94c"}
        stroke = next((value for word, value in color_map.items() if word in text), "#ffffff")
        dashed = "점선" in text
        asset = result["assets"][0]
        inset = .025 if any(word in text for word in ("안쪽", "내부", "안에")) else 0
        x = round(float(asset.get("x", 0)) + inset, 4)
        y = round(float(asset.get("y", 0)) + inset, 4)
        width = round(max(.02, float(asset.get("width", 1)) - inset * 2), 4)
        height = round(max(.02, float(asset.get("height", 1)) - inset * 2), 4)
        shape = "ellipse" if asset.get("shape") == "ellipse" or any(
            word in text for word in ("원형", "동그란", "원 모양")
        ) else "rectangle"
        desired = {"type": shape, "x": x, "y": y, "width": width, "height": height,
                   "fill": "transparent", "stroke": stroke, "stroke_width": .006,
                   "dash": dashed, "dash_length": .025, "gap_length": .018,
                   "z": max(1, int(asset.get("z", 0)) + 1)}
        candidates = result.setdefault("decorations", [])
        matching_index = next((index for index, item in enumerate(candidates)
                               if item.get("type") == shape and item.get("fill") == "transparent"), None)
        if matching_index is None:
            candidates.append(desired)
            applied.append(f"decorations[{len(candidates)-1}].add")
        else:
            for key, value in desired.items():
                if candidates[matching_index].get(key) != value:
                    candidates[matching_index][key] = value
                    applied.append(f"decorations[{matching_index}].{key}")
    if applied:
        result["enforced_user_constraints"] = applied
    return result, applied
