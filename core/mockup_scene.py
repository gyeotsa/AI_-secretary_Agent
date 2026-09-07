"""Model-authored, pattern-agnostic scene plans for reference-driven mockups."""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy

from jsonschema import Draft202012Validator

from core.mockup_pipeline_policy import subject_background_action, subject_background_edits


class ScenePlanError(ValueError):
    pass


COLOR_WORDS = {
    "빨간색": "#e5484d", "빨강": "#e5484d", "레드": "#e5484d",
    "파란색": "#2878d0", "파랑": "#2878d0", "블루": "#2878d0",
    "검은색": "#111111", "검정": "#111111", "흰색": "#ffffff", "하얀색": "#ffffff",
    "초록색": "#38a169", "초록": "#38a169", "노란색": "#f2c94c", "노랑": "#f2c94c",
    "보라색": "#8b4cc2", "보라": "#8b4cc2", "분홍색": "#ec6f9e", "분홍": "#ec6f9e",
}


TEXT_SPAN_JSON_SCHEMA = {
    "type": "object",
    "required": ["content"],
    "properties": {
        "content": {"type": "string"},
        "color": {"type": "string"},
        "font_family": {"type": "string"},
        "font_size": {"type": "number"},
        "font_weight": {"enum": ["normal", "bold"]},
    },
}


TEXT_SPAN_PATCH_JSON_SCHEMA = {
    "type": "object",
    "required": ["index"],
    "properties": {
        # Span wording is immutable in a style patch. Copy changes go through
        # visible_copy, so a local model cannot silently rename one run while
        # claiming that it merely changed a colour or font.
        "index": {"type": "integer"},
        "color": {"type": "string"},
        "font_family": {"type": "string"},
        "font_size": {"type": "number"},
        "font_weight": {"enum": ["normal", "bold"]},
    },
}


def requested_font_size_pixels(instruction: str) -> int | None:
    """One numeric typography contract shared by enforcement and validation.

    Read the size property, not an unrelated image dimension or quoted copy.
    Grammatical particles and the pixel-unit spelling do not change its value.
    """
    from core.utterance_scope import mask_quoted_payloads
    text = mask_quoted_payloads(str(instruction or ""))
    match = re.search(
        r"(?:(?:글자|글씨|문구|폰트)\s*)?(?:크기|사이즈)(?:를|는|만|도|로)?\s*[:=]?\s*"
        r"(\d{1,3})\s*(?:픽셀|px)(?![a-z])", text, re.I,
    )
    return int(match.group(1)) if match else None


def parse_explicit_colored_copy(instruction: str) -> tuple[str, list[dict]]:
    """Extract exact user-authored copy/color pairs without LLM correction.

    Example: ``정지언은 빨간색, 테스트는 파란색`` becomes one copy
    string with two independently colored spans.  The spelling is preserved.
    """
    value = " ".join(str(instruction or "").replace("\n", " ").split())
    color_words = "|".join(sorted(map(re.escape, COLOR_WORDS), key=len, reverse=True))
    pattern = re.compile(
        rf"(?:문구(?:는|를)?\s*)?[\"'“”]?([가-힣ㄱ-ㅎㅏ-ㅣA-Za-z0-9_.-]+)[\"'“”]?\s*(?:은|는|을|를)?\s*({color_words}|#[0-9a-fA-F]{{6}})"
    )
    spans = []
    for match in pattern.finditer(value):
        word = match.group(1).strip()
        quoted = match.start(1) > 0 and value[match.start(1) - 1] in "\"'“”"
        suffix_particle = value[match.end(1):match.start(2)].strip(" \"'“”")
        # A colour adjective following an arbitrary word is not new copy.
        # Require a literal or a subject/object particle; e.g. the conjunction
        # in "더 크게 하고 파란색으로" must never become the text "하고".
        if not quoted and not (re.search(r"(?:은|는|을|를)$", word) or
                               suffix_particle in {"은", "는", "을", "를"}):
            continue
        content = word if quoted else re.sub(r"(?:은|는|을|를)$", "", word)
        if content in {"문구", "글자", "텍스트", "색상"}:
            continue
        color_word = match.group(2)
        span = {"content": content, "color": COLOR_WORDS.get(color_word, color_word.lower())}
        # Typography may be specified independently for each coloured phrase,
        # e.g. `'정지원'은 빨간색 맑은 고딕, '테스트'는 파란색 궁서체`.
        clause_end = re.search(r"[,;\n.]", value[match.end():])
        suffix = value[match.end():match.end() + (clause_end.start() if clause_end else len(value))]
        suffix = re.sub(
            r"\s*(?:으로|로)?\s*(?:해\s*줘|해주세요|설정해줘|적용해줘|써줘|작성해줘).*$",
            "", suffix,
        ).strip(" '\"“”")
        if suffix.endswith("체") and len(suffix) > 1:
            suffix = suffix[:-1].strip()
        if suffix and len(suffix) <= 80 and not any(
            word in suffix for word in ("문구", "색상", "위치", "크기", "하단", "상단")
        ):
            span["font_family"] = suffix
        spans.append(span)
    if not spans:
        return "", []
    return " ".join(span["content"] for span in spans), spans


def parse_requested_visible_copy(instruction: str) -> str:
    """Extract an unquoted replacement value without mistaking style for copy.

    Korean users commonly say ``문구를 테스트로 바꾸고 빨간색으로 해줘``.
    The previous boolean heuristic saw the colour word and classified the
    entire utterance as a style-only edit, causing the write barrier to restore
    the old wording.  This parser accepts the grammatical replacement slot and
    rejects known typography/layout values occupying that same slot.
    """
    text = " ".join(str(instruction or "").replace("\n", " ").split())
    target = r"(?:문구|텍스트|글자|글씨|카피|내용)"
    action = r"(?:바꿔|바꾸|변경|수정|교체)"
    match = re.search(
        rf"{target}(?:\s*내용)?(?:은|는|을|를)?\s+(.{{1,160}}?)\s*(?:으)?로\s*{action}",
        text, re.I,
    )
    if not match:
        return ""
    raw_candidate = match.group(1).strip()
    candidate = raw_candidate.strip(" \t'\"“”‘’")
    if not candidate:
        return ""
    style_tokens = {
        *COLOR_WORDS, "색", "색상", "색깔", "컬러", "글꼴", "폰트", "서체",
        "고딕", "고딕체", "궁서", "궁서체", "명조", "명조체", "굵게", "볼드",
        "왼쪽", "오른쪽", "위", "아래", "상단", "하단", "중앙", "가운데",
        "크게", "작게", "투명", "불투명",
    }
    prefix = text[match.start():match.start(1)]
    explicit_content = "내용" in prefix
    quoted = (len(raw_candidate) >= 2 and raw_candidate[0] in "'\"“‘"
              and raw_candidate[-1] in "'\"”’")
    compact = re.sub(r"\s+", "", candidate).casefold()
    normalized_tokens = {re.sub(r"\s+", "", token).casefold() for token in style_tokens}
    # Without an explicit content marker or quotes, a grammatical ``...로
    # 바꿔`` slot can also contain a property value: ``글씨 크기를 64픽셀로``
    # and ``문구 글꼴을 궁서로`` are style edits, not replacement copy.
    contains_style_value = (
        any(token and token in compact for token in normalized_tokens)
        or bool(re.search(r"#[0-9a-fA-F]{3,8}(?![0-9a-fA-F])", candidate))
        or bool(re.search(r"\d{1,4}\s*(?:px|픽셀|배)", candidate, re.I))
    )
    if not quoted and not explicit_content and contains_style_value:
        return ""
    return " ".join(candidate.split())[:160]


def _named_text_fragment_matches(item: dict, instruction: str) -> dict[int, re.Match]:
    """Return explicitly named run indexes and their nearest mention."""
    result = {}
    text = str(instruction or "")
    for index, span in enumerate(item.get("spans", [])):
        if not isinstance(span, dict):
            continue
        content = str(span.get("content", "")).strip()
        if not content:
            continue
        literal = re.escape(content)
        match = re.search(
            rf"(?:['\"“‘]\s*{literal}\s*['\"”’]|(?<![\w가-힣ㄱ-ㅎㅏ-ㅣ]){literal})"
            rf"\s*(?:은|는|만|의|을|를)?",
            text,
        )
        if match:
            result[index] = match
    return result


def _requested_span_style_updates(item: dict, instruction: str) -> dict[int, dict]:
    """Compile explicit phrase-level style requests into run updates.

    Each phrase is limited to the clause following its own mention. This keeps
    ``'A'는 빨간색, 'B'는 파란색`` from leaking B's style into A and gives the
    deterministic fallback the same semantics as the structured patch path.
    """
    text = " ".join(str(instruction or "").replace("\n", " ").split())
    mentions = _named_text_fragment_matches(item, text)
    if not mentions:
        return {}
    ordered_starts = sorted((match.start(), index) for index, match in mentions.items())
    updates = {}
    for position, (_start, index) in enumerate(ordered_starts):
        match = mentions[index]
        end = ordered_starts[position + 1][0] if position + 1 < len(ordered_starts) else len(text)
        clause = text[match.end():end]
        delimiter = re.search(
            r"[,;.!?\n]|\s+그리고\s+|"
            r"\s+(?:바꾸고|변경하고|수정하고|설정하고|적용하고|만들고|해\s*주고|하고)\s+",
            clause,
        )
        if delimiter:
            clause = clause[:delimiter.start()]
        requested = {}
        color_word = next((word for word in sorted(COLOR_WORDS, key=len, reverse=True)
                           if word in clause), None)
        hex_color = re.search(r"#[0-9a-fA-F]{6}(?![0-9a-fA-F])", clause)
        if hex_color:
            requested["color"] = hex_color.group(0).lower()
        elif color_word:
            requested["color"] = COLOR_WORDS[color_word]
        quoted_family = re.search(
            r"(?:글꼴|폰트|서체)(?:은|는|을|를)?\s*['\"]([^'\"]+)['\"]", clause, re.I,
        )
        if quoted_family:
            requested["font_family"] = quoted_family.group(1).strip()[:80]
        else:
            common_family = re.search(
                r"(맑은\s*고딕|궁서(?:체)?|굴림(?:체)?|돋움(?:체)?|바탕(?:체)?|명조(?:체)?)",
                clause, re.I,
            )
            if common_family:
                requested["font_family"] = " ".join(common_family.group(1).split())[:80]
        pixel_size = requested_font_size_pixels(clause)
        if pixel_size is not None:
            requested["font_size"] = max(.015, min(.2, pixel_size / 1600))
        elif any(word in clause for word in ("더 크게", "크게", "키워", "확대")):
            base_size = float(item.get("spans", [])[index].get(
                "font_size", item.get("font_size", .055)
            ))
            requested["font_size"] = round(min(.2, max(.015, base_size * 1.5)), 4)
        elif any(word in clause for word in ("더 작게", "작게", "줄여", "축소")):
            base_size = float(item.get("spans", [])[index].get(
                "font_size", item.get("font_size", .055)
            ))
            requested["font_size"] = round(max(.015, base_size * .65), 4)
        if re.search(r"굵게|굵은|볼드|bold|진하게", clause, re.I):
            requested["font_weight"] = "bold"
        elif re.search(r"굵기(?:는|를)?\s*보통|보통\s*굵기|normal", clause, re.I):
            requested["font_weight"] = "normal"
        if requested:
            updates[index] = requested
    return updates


def _has_quoted_phrase_style_target(instruction: str) -> bool:
    """Recognize a phrase-level style command without a generic text noun.

    Users naturally say ``'정지원'만 초록색으로`` after a mixed-style title
    has already been created. Requiring the words ``문구`` or ``글씨`` makes
    that command disappear from both the patch write-mask and deterministic
    fallback. Quotes provide an unambiguous text-run target; a separate style
    token is still required so quoted replacement copy is not misclassified.
    """
    text = " ".join(str(instruction or "").replace("\n", " ").split())
    if not re.search(r"['\"“‘][^'\"”’]{1,160}['\"”’]", text):
        return False
    color_words = "|".join(sorted(map(re.escape, COLOR_WORDS), key=len, reverse=True))
    return bool(re.search(
        rf"(?:{color_words}|#[0-9a-fA-F]{{6}}(?![0-9a-fA-F])|"
        r"글꼴|폰트|서체|고딕|궁서|명조|굴림|돋움|바탕|"
        r"굵게|굵은|볼드|bold|보통\s*굵기|"
        r"(?:크기|사이즈)(?:를|는|만|도|로)?\s*[:=]?\s*\d{1,3}\s*(?:픽셀|px)|"
        r"더\s*(?:크게|작게)|키워|줄여|확대|축소)",
        text, re.I,
    ))


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
                                  "remove_background": {"type": "boolean"},
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
                                 "letter_spacing": {"type": "number"},
                                 "line_height": {"type": "number"},
                                 "stroke": {"type": "string"},
                                 "stroke_width": {"type": "number"},
                                 "shadow": {"type": "object"},
                                 "path": {"type": ["object", "null"]},
                                 "opacity": {"type": "number"},
                                 "blend_mode": {"type": "string"},
                                 "spans": {"type": "array", "items": TEXT_SPAN_JSON_SCHEMA},
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
                "remove_background": {"type": "boolean"},
                "z": {"type": "integer"}}}},
        "texts": {"type": "array", "items": {"type": "object",
            "required": ["index", "action"], "properties": {
                "index": {"type": "integer"}, "action": {"enum": ["update", "remove", "add"]},
                "x": {"type": "number"}, "y": {"type": "number"},
                "width": {"type": "number"}, "height": {"type": "number"},
                "font_size": {"type": "number"}, "color": {"type": "string"},
                "font_family": {"type": "string"},
                "font_weight": {"enum": ["normal", "bold"]},
                "letter_spacing": {"type": "number"}, "line_height": {"type": "number"},
                "stroke": {"type": "string"}, "stroke_width": {"type": "number"},
                "shadow": {"type": "object"}, "path": {"type": ["object", "null"]},
                "opacity": {"type": "number"}, "blend_mode": {"type": "string"},
                "spans": {"type": "array", "items": TEXT_SPAN_PATCH_JSON_SCHEMA},
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


# The model response requests a complete envelope, while internal callers may
# submit sparse deltas. Both must obey the same property types before equality
# comparisons (Python otherwise equates True/1 and False/0).
_SPARSE_PATCH_SCHEMA = deepcopy(SCENE_EDIT_PATCH_JSON_SCHEMA)
_SPARSE_PATCH_SCHEMA.pop("required", None)
_SPARSE_PATCH_VALIDATOR = Draft202012Validator(_SPARSE_PATCH_SCHEMA)


def _validate_edit_patch_values(patch: dict) -> None:
    errors = sorted(_SPARSE_PATCH_VALIDATOR.iter_errors(patch), key=lambda error: str(list(error.path)))
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.path) or "patch"
        detail = "값은 true/false여야 합니다" if error.validator == "type" and error.validator_value == "boolean" else error.message
        raise ScenePlanError(f"AI 수정 패치 형식 오류 ({location}): {detail}")

    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ScenePlanError("AI 수정 패치의 숫자는 유한한 값이어야 합니다.")
        if isinstance(value, dict):
            for item in value.values():
                finite(item)
        elif isinstance(value, list):
            for item in value:
                finite(item)
    finite(patch)


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
        if "remove_background" in item:
            if not isinstance(item["remove_background"], bool):
                raise ScenePlanError("사진 배경 제거 속성은 true/false여야 합니다.")
            normalized_asset["remove_background"] = item["remove_background"]
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
            "letter_spacing": _number(item.get("letter_spacing"), -.03, .1, 0),
            "line_height": _number(item.get("line_height"), .8, 3.0, 1.2),
            "stroke": _color(item.get("stroke")),
            "stroke_width": _number(item.get("stroke_width"), 0, .03, 0),
            "shadow": item.get("shadow") if isinstance(item.get("shadow"), dict) else {},
            "path": item.get("path") if isinstance(item.get("path"), dict) else None,
            "opacity": _number(item.get("opacity"), 0, 1, 1),
            "blend_mode": str(item.get("blend_mode", "normal")) if str(item.get("blend_mode", "normal")) in
                          {"normal", "multiply", "screen", "overlay"} else "normal",
            "color": _color(item.get("color"), "#111111"),
            "background": _color(item.get("background")),
            "align": str(item.get("align")) if item.get("align") in {"left", "center", "right"} else "center",
            "padding": _number(item.get("padding"), 0, .1, .018),
            "z": int(_number(item.get("z"), -20, 20, 10)),
        }
        spans = item.get("spans") if isinstance(item.get("spans"), list) else []
        normalized_spans = []
        for span in spans:
            if not isinstance(span, dict) or not str(span.get("content", "")).strip():
                continue
            normalized_spans.append({
                "content": str(span["content"]).strip()[:80],
                "color": _color(span.get("color"), normalized_text["color"]),
            })
            family = " ".join(str(span.get("font_family", "")).split())[:80]
            if family:
                normalized_spans[-1]["font_family"] = family
            if "font_size" in span:
                normalized_spans[-1]["font_size"] = _number(
                    span.get("font_size"), .015, .2, normalized_text["font_size"]
                )
            if "font_weight" in span:
                normalized_spans[-1]["font_weight"] = (
                    "normal" if span.get("font_weight") == "normal" else "bold"
                )
        if normalized_spans and " ".join(part["content"] for part in normalized_spans) == requested_copy:
            normalized_text["spans"] = normalized_spans
        _fit_normalized_box(normalized_text)
        normalized_text["padding"] = min(
            normalized_text["padding"],
            normalized_text["width"] * .2,
            normalized_text["height"] * .2,
        )
        result["texts"] = [normalized_text]
    return result


def enforce_exact_user_copy(plan: dict, instruction: str, visible_copy: str) -> tuple[dict, str, list[str]]:
    """Make exact user copy and per-fragment colors a non-negotiable render contract."""
    result = deepcopy(plan)
    explicit_copy, spans = parse_explicit_colored_copy(instruction)
    replacement_copy = parse_requested_visible_copy(instruction)
    copy = explicit_copy or replacement_copy or " ".join(str(visible_copy or "").split())[:160]
    if not copy or not result.get("texts"):
        return result, copy, []
    text = result["texts"][0]
    changed = []
    if text.get("content") != copy:
        text["content"] = copy
        changed.append("texts[0].content")
    if spans and text.get("spans") != spans:
        text["spans"] = spans
        changed.append("texts[0].spans")
    elif replacement_copy and not spans and text.get("spans"):
        # Existing phrase boundaries refer to the old wording. Keeping them
        # would either fail normalization or render stale text in a fallback.
        text.pop("spans", None)
        changed.append("texts[0].spans")
    # A model may propose an enormous normalized size. Keep it as a preference;
    # the SVG renderer performs the final exact fit inside this box.
    if float(text.get("width", 0)) < .45:
        text["width"] = .7
        text["x"] = max(0.0, min(.3, .5 - text["width"] / 2))
        changed.extend(["texts[0].width", "texts[0].x"])
    _fit_normalized_box(text)
    if changed:
        result["enforced_user_constraints"] = list(dict.fromkeys(
            [*result.get("enforced_user_constraints", []), *changed]
        ))
    return result, copy, changed


def requests_visible_copy_change(instruction: str) -> bool:
    """Return whether an edit explicitly asks to alter the displayed wording.

    Colour, font, border and layout clauses may contain noun-like tokens that
    the exact-copy parser can otherwise mistake for new copy.  Copy is mutable
    only when the user names a text target and a content-writing action (or
    supplies a quoted literal as that target).
    """
    text = " ".join(str(instruction or "").replace("\n", " ").split())
    target = r"(?:문구|텍스트|글자|글씨|카피|내용)"
    write_action = r"(?:바꿔|변경|수정|교체|써\s*줘|적어\s*줘|넣어\s*줘|추가|작성|삭제|지워|없애)"
    style_terms = ("글꼴", "폰트", "서체", "색상", "색깔", "크기", "굵기", "위치", "정렬",
                   "자간", "행간", "윤곽선", "그림자", "배경", "고딕", "궁서", "명조")
    has_quoted_copy = bool(re.search(rf"{target}(?:는|를|은|을)?\s*['\"“”]", text, re.I))
    has_unquoted_copy = bool(parse_requested_visible_copy(text))
    # “문구 글꼴을 바꿔” changes typography, not the wording itself.
    has_style_value = any(term in text for term in (*style_terms, *COLOR_WORDS)) or bool(
        re.search(r"#[0-9a-fA-F]{3,8}(?![0-9a-fA-F])", text))
    if has_style_value and "내용" not in text and not has_quoted_copy and not has_unquoted_copy:
        return False
    if re.search(rf"{target}.{{0,60}}{write_action}", text, re.I):
        return True
    if re.search(rf"{write_action}.{{0,30}}{target}", text, re.I):
        return True
    return has_quoted_copy or has_unquoted_copy


def _bound_edit_clauses(instruction: str) -> list[tuple[str | None, str]]:
    """Bind a property to its clause's subject, not every noun in the prompt.

    Quote payloads retain their offsets but cannot supply subjects/separators.
    An explicit new subject ends inheritance from the preceding clause.
    """
    from core.utterance_scope import mask_quoted_payloads
    original = str(instruction or "")
    masked = mask_quoted_payloads(original).casefold()
    separator = re.compile(r"[!?;\n]+|(?<!\d)\.(?!\d)|,\s*|\s+그리고\s+|고\s+")
    subjects = (
        ("text", ("문구", "텍스트", "글자", "글씨", "카피", "폰트", "글꼴", "서체")),
        ("decoration", ("테두리", "점선", "실선", "장식", "라인")),
        ("asset", ("스티커", "사진", "이미지", "인물", "사람", "얼굴", "머리", "프레임",
                   "원형", "원 형태", "모양", "형태")),
        ("canvas", ("캔버스", "배경", "바탕")),
    )
    result, start, active_target = [], 0, None
    bounds = [(match.start(), match.end()) for match in separator.finditer(masked)]
    for end, next_start in [*bounds, (len(original), len(original))]:
        clause, visible = original[start:end].strip(), masked[start:end]
        start = next_start
        if not clause:
            continue
        for target, nouns in subjects:
            if any(noun in visible for noun in nouns):
                active_target = target
                break
        result.append((active_target, clause))
    return result


def _requested_text_properties(instruction: str) -> set[str]:
    """Property-level authorization for explicit typography/layout edits.

    This constrains post-processing as well as the planner. Unknown creative
    requests still use the model's text-group scope, but a concrete size/font
    command does not authorize every other property in that group.
    """
    text = " ".join(clause for target, clause in _bound_edit_clauses(instruction)
                    if target == "text").casefold()
    if not text and _has_quoted_phrase_style_target(instruction):
        text = " ".join(str(instruction or "").split()).casefold()
    groups = (
        (r"글꼴|폰트|서체|고딕|궁서|명조|font", {"font_family"}),
        (r"굵|두껍|볼드|bold|보통", {"font_weight"}),
        (r"크기|픽셀|작게|크게|줄여|줄이|키워|키우|확대|축소|\d\s*배", {"font_size", "width", "height"}),
        (r"색|color|#[0-9a-f]{3,8}", {"color"}),
        (r"배경|바탕|텍스트\s*박스|하얀\s*박스", {"background"}),
        (r"정렬|중앙|가운데|왼쪽|오른쪽", {"align", "x", "y"}),
        (r"위치|배치|옮|이동|올려|내려|아래|하단|상단|위로|좌측|우측", {"x", "y"}),
        (r"맞춰|맞게|안쪽|안에|잘리|잘려|너비|폭|높이", {"x", "y", "width", "height", "padding"}),
        (r"여백|패딩|padding", {"padding"}),
        (r"자간|글자\s*사이", {"letter_spacing"}),
        (r"행간|줄\s*간격", {"line_height"}),
        (r"윤곽|외곽|테두리|stroke", {"stroke", "stroke_width"}),
        (r"그림자|shadow", {"shadow"}),
        (r"투명도|불투명도|opacity", {"opacity"}),
        (r"블렌딩|합성\s*모드|blend", {"blend_mode"}),
        (r"곡선|곡률|휘어|둥글|arc", {"path"}),
        (r"레이어|맨\s*앞|맨\s*뒤|겹침|가리", {"z"}),
    )
    allowed = set()
    for pattern, properties in groups:
        if re.search(pattern, text):
            allowed.update(properties)
    return allowed


def _inherit_text_run_properties(item: dict, properties: set[str]) -> bool:
    """A whole-text style command overrides run styles, never run wording.

    Runs use their own colour/font ahead of the parent. Updating only the
    parent can therefore be a JSON change with no visible effect. Drop a font
    override to inherit the requested family, and update explicit run colours
    (which the legacy normalizer always materializes).
    """
    changed = False
    for span in item.get("spans", []):
        if not isinstance(span, dict):
            continue
        if "font_family" in properties and "font_family" in span:
            span.pop("font_family")
            changed = True
        if "color" in properties and item.get("color") and span.get("color") != item["color"]:
            span["color"] = item["color"]
            changed = True
    return changed


def _names_text_fragment(item: dict, instruction: str) -> bool:
    """Do not expand an explicitly named phrase into a whole-text change."""
    return bool(_named_text_fragment_matches(item, instruction))


def preserve_unrequested_scene_fields(before: dict, candidate: dict,
                                      instruction: str) -> tuple[dict, set[str]]:
    """Apply a final, field-aware write barrier to an edit transaction."""
    result, scopes = merge_scoped_scene_edit(before, candidate, instruction)
    # Alpha has its own per-source write mask, even if another photo property
    # (crop/position) authorizes the assets group. A model cannot remove every
    # background just because one photo was mentioned.
    original_assets = {asset.get("index"): asset for asset in before.get("assets", [])}
    for asset in result.get("assets", []):
        old = original_assets.get(asset.get("index"), {})
        if "remove_background" in old:
            asset["remove_background"] = old["remove_background"]
        else:
            asset.pop("remove_background", None)
    result, _ = enforce_subject_background_constraints(result, instruction)
    if "texts" in scopes and not requests_visible_copy_change(instruction):
        old_texts = before.get("texts", [])
        new_texts = result.get("texts", [])
        allowed = _requested_text_properties(instruction)
        # A typography/position request cannot add or remove wording by
        # replacing the text-list shape. Keep the existing identities/order.
        if len(old_texts) != len(new_texts):
            result["texts"] = deepcopy(old_texts)
            new_texts = result["texts"]
        for index, old in enumerate(old_texts):
            if index >= len(new_texts) or not isinstance(old, dict) or not isinstance(new_texts[index], dict):
                continue
            if "content" in old:
                new_texts[index]["content"] = deepcopy(old["content"])
            else:
                new_texts[index].pop("content", None)
            named_updates = _requested_span_style_updates(old, instruction)
            # A phrase-level style request authorizes only the named run. The
            # parent layer and every other run are immutable even when a local
            # model returns a convenient whole-text proxy change.
            if named_updates:
                for key in set(old) | set(new_texts[index]):
                    if key in {"content", "spans"}:
                        continue
                    if key in old:
                        new_texts[index][key] = deepcopy(old[key])
                    else:
                        new_texts[index].pop(key, None)
            elif allowed:
                for key in set(old) | set(new_texts[index]):
                    if key in allowed or key == "spans":
                        continue
                    if key in old:
                        new_texts[index][key] = deepcopy(old[key])
                    else:
                        new_texts[index].pop(key, None)
            if named_updates:
                old_spans = old.get("spans", []) if isinstance(old.get("spans"), list) else []
                candidate_spans = (new_texts[index].get("spans", [])
                                   if isinstance(new_texts[index].get("spans"), list) else [])
                merged_spans = deepcopy(old_spans)
                run_properties = {"font_family", "font_size", "font_weight", "color"}
                for span_index, requested in named_updates.items():
                    if span_index >= len(merged_spans) or span_index >= len(candidate_spans):
                        continue
                    candidate_span = candidate_spans[span_index]
                    if not isinstance(candidate_span, dict):
                        continue
                    for key in requested:
                        if key in run_properties and key in candidate_span:
                            merged_spans[span_index][key] = deepcopy(candidate_span[key])
                if old_spans:
                    new_texts[index]["spans"] = merged_spans
                else:
                    new_texts[index].pop("spans", None)
                # A phrase-specific request must not make the parent style a
                # proxy success. Non-target runs inherit from that parent.
                for key in run_properties:
                    if key in old:
                        new_texts[index][key] = deepcopy(old[key])
                    else:
                        new_texts[index].pop(key, None)
            else:
                if "spans" in old:
                    new_texts[index]["spans"] = deepcopy(old["spans"])
                else:
                    new_texts[index].pop("spans", None)
                inherited = {key for key in ("font_family", "color")
                             if key in allowed and old.get(key) != new_texts[index].get(key)}
                text_request = " ".join(clause for target, clause in _bound_edit_clauses(instruction)
                                        if target == "text")
                family = re.search(r"글꼴(?:을|은)?\s*['\"]([^'\"]+)['\"]", text_request, re.I)
                if family and family.group(1).strip() == new_texts[index].get("font_family"):
                    inherited.add("font_family")
                if "color" in allowed and (any(word in text_request for word in COLOR_WORDS) or
                                            re.search(r"#[0-9a-fA-F]{6}(?![0-9a-fA-F])", text_request)):
                    inherited.add("color")
                _inherit_text_run_properties(new_texts[index], inherited)
    return result, scopes


def scene_diff_fields(before: dict, after: dict) -> list[str]:
    """Describe actual leaf changes, rather than trusting model patch claims."""
    changed = []

    def walk(left, right, path=""):
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                if key in {"document", "rationale", "edit_scopes", "edit_patch_summary", "edit_patch_fields",
                           "edit_success_criteria", "enforced_user_constraints",
                           "enforced_measured_evidence"}:
                    continue
                walk(left.get(key), right.get(key), f"{path}.{key}" if path else key)
            return
        if isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                walk(left[index] if index < len(left) else None,
                     right[index] if index < len(right) else None, f"{path}[{index}]")
            return
        if left != right:
            changed.append(path)

    walk(before, after)
    return changed


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
    ignored = {"document", "rationale", "restored_required_elements", "enforced_measured_evidence",
               "enforced_user_constraints", "quality_review_fallback", "quality_review_rejected",
               "initial_plan_fallback", "edit_plan_fallback"}
    for mapping in (left, right):
        for key in ignored:
            mapping.pop(key, None)
    return left != right


def requests_circular_shape(value: str) -> bool:
    """Recognize ordinary Korean variants describing a circular result."""
    text = " ".join(str(value or "").lower().split())
    return (any(word in text for word in ("원형", "원 형태", "원 모양", "동그랗", "동그란")) or
            bool(re.search(r"원\s*(?:모양|형태)?\s*(?:으)?로(?:\s|$|[.,!?])", text)))


def infer_edit_scopes(instruction: str) -> set[str]:
    """Identify the visual groups explicitly targeted by an edit request."""
    text = " ".join(str(instruction or "").lower().split())
    scopes = set()
    if any(word in text for word in ("문구", "텍스트", "글자", "글씨", "카피", "폰트", "글꼴", "서체")):
        scopes.add("texts")
    if _has_quoted_phrase_style_target(instruction):
        scopes.add("texts")
    if any(word in text for word in ("배경", "캔버스")):
        scopes.add("canvas")
    if any(word in text for word in (
        "사진", "이미지", "인물", "사람", "얼굴", "머리", "전신", "상반신",
        "원형", "원 형태", "프레임", "틀", "크롭", "잘리", "잘라",
    )):
        scopes.add("assets")
    if requests_circular_shape(text):
        scopes.add("assets")
    # A circular *sticker* changes both the photo/frame mask and the delivered
    # canvas alpha.  Treating it as assets-only makes the safety write mask
    # reject the required transparent canvas as an unsolicited edit.
    if "스티커" in text and requests_circular_shape(text):
        scopes.add("canvas")
    if any(word in text for word in ("테두리", "점선", "실선", "장식", "라인")):
        scopes.add("decorations")
    if subject_background_action(instruction) is not None:
        scopes.add("assets")
        if "캔버스" not in text and not ("스티커" in text and requests_circular_shape(text)):
            scopes.discard("canvas")
    return scopes


def _span_aware_edit_scopes(instruction: str, before: dict | None = None) -> set[str]:
    """Resolve unquoted run names against the current scene before masking.

    Quoted phrases are identifiable without scene state and are handled by
    ``infer_edit_scopes``.  For a natural follow-up such as ``정지원만
    초록색으로`` the existing span list is the only safe source of truth.
    Matching a known run plus a concrete style delta authorizes ``texts``;
    arbitrary unknown words never do.
    """
    scopes = infer_edit_scopes(instruction)
    if "texts" in scopes or not isinstance(before, dict):
        return scopes
    for item in before.get("texts", []):
        if isinstance(item, dict) and _requested_span_style_updates(item, instruction):
            scopes.add("texts")
            break
    return scopes


def merge_scoped_scene_edit(before: dict, candidate: dict, instruction: str) -> tuple[dict, set[str]]:
    """Use explicit edit targets as a write mask over the previous plan."""
    scopes = _span_aware_edit_scopes(instruction, before)
    if not scopes:
        return deepcopy(candidate), scopes
    merged = deepcopy(before)
    for key in scopes:
        if key in candidate:
            merged[key] = deepcopy(candidate[key])
    merged["rationale"] = str(candidate.get("rationale", before.get("rationale", "")))
    merged["edit_scopes"] = sorted(scopes)
    return merged, scopes


def filter_scene_edit_patch(patch: dict, instruction: str,
                            before: dict | None = None) -> tuple[dict, list[str]]:
    """Drop model-authored groups that the user did not request.

    Small local models often return a valid requested asset/canvas delta plus
    an unsolicited text tweak. Rejecting the whole answer loses the useful
    delta. This write mask removes only the unrelated groups; the remaining
    patch still goes through measurable validation afterwards.
    """
    if not isinstance(patch, dict):
        raise ScenePlanError("AI 수정 패치가 객체 형식이 아닙니다.")
    scopes = _span_aware_edit_scopes(instruction, before)
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
    _validate_edit_patch_values(patch)
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
        for key in ("x", "y", "width", "height", "shape", "fit", "zoom", "focal_x", "focal_y", "rotation", "z", "remove_background"):
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
            for key in ("x", "y", "width", "height", "font_size", "font_family", "font_weight",
                        "letter_spacing", "line_height", "stroke", "stroke_width", "shadow", "path",
                        "opacity", "blend_mode", "color", "background", "align", "padding", "z"):
                if key in update and texts[index].get(key) != update[key]:
                    texts[index][key] = update[key]; changed.append(f"texts[{index}].{key}")
            span_updates = update.get("spans") if isinstance(update.get("spans"), list) else []
            target_spans = texts[index].get("spans") if isinstance(texts[index].get("spans"), list) else []
            for span_update in span_updates:
                if not isinstance(span_update, dict):
                    continue
                try:
                    span_index = int(span_update.get("index", -1))
                except (TypeError, ValueError):
                    continue
                if not 0 <= span_index < len(target_spans) or not isinstance(target_spans[span_index], dict):
                    continue
                for key in ("font_family", "font_size", "font_weight", "color"):
                    if key in span_update and target_spans[span_index].get(key) != span_update[key]:
                        target_spans[span_index][key] = span_update[key]
                        changed.append(f"texts[{index}].spans[{span_index}].{key}")
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
    scopes = _span_aware_edit_scopes(text, before)
    groups = {field.split(".", 1)[0].split("[", 1)[0] for field in changed_fields}
    if scopes:
        unexpected = groups - scopes - {"visible_copy"}
        if unexpected:
            raise ScenePlanError(f"명령하지 않은 영역을 변경했습니다: {sorted(unexpected)}")
    text_target = "texts" in scopes
    has_named_run_request = False
    if text_target and before and after:
        # Phrase-level style requests are fulfilled only by the named run.
        # Changing the parent text style is not evidence because explicit run
        # values override it in both the SVG and Pillow renderers.
        for text_index, prior_text in enumerate(before.get("texts", [])):
            requested_runs = _requested_span_style_updates(prior_text, instruction)
            if not requested_runs:
                continue
            has_named_run_request = True
            if text_index >= len(after.get("texts", [])):
                raise ScenePlanError("부분 문구 스타일 요청의 텍스트 레이어가 사라졌습니다.")
            revised_text = after["texts"][text_index]
            run_properties = {"font_family", "font_size", "font_weight", "color"}
            for key in run_properties:
                if prior_text.get(key) != revised_text.get(key):
                    raise ScenePlanError(
                        f"부분 문구 스타일 요청이 부모 텍스트의 {key}까지 변경했습니다."
                    )
            revised_spans = after["texts"][text_index].get("spans", [])
            prior_spans = prior_text.get("spans", [])
            if len(revised_spans) != len(prior_spans):
                raise ScenePlanError("부분 문구 스타일 요청이 span 구성을 변경했습니다.")
            for span_index, requirements in requested_runs.items():
                if span_index >= len(revised_spans) or not isinstance(revised_spans[span_index], dict):
                    raise ScenePlanError("부분 문구 스타일 요청의 대상 span이 결과에 없습니다.")
                actual = revised_spans[span_index]
                for key, expected in requirements.items():
                    if actual.get(key) != expected:
                        name = str(prior_text.get("spans", [])[span_index].get("content", span_index))
                        raise ScenePlanError(
                            f"'{name}' 부분 문구의 {key} 요청이 실제 span에 반영되지 않았습니다."
                        )
            for span_index, prior_span in enumerate(prior_spans):
                if not isinstance(prior_span, dict) or not isinstance(revised_spans[span_index], dict):
                    if prior_span != revised_spans[span_index]:
                        raise ScenePlanError("부분 문구 스타일 요청이 span 구성을 변경했습니다.")
                    continue
                permitted = set(requested_runs.get(span_index, {}))
                for key in set(prior_span) | set(revised_spans[span_index]):
                    if key in permitted:
                        continue
                    if prior_span.get(key) != revised_spans[span_index].get(key):
                        name = str(prior_span.get("content", span_index))
                        raise ScenePlanError(
                            f"부분 문구 스타일 요청이 '{name}' span의 명령하지 않은 {key}까지 변경했습니다."
                        )
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
        # An axis change alone is not proof that a relative movement request
        # was fulfilled.  This is especially important after a whole-preview
        # flip/rotation, where the display instruction has already been mapped
        # back to source coordinates.  Reject a planner patch whose sign is the
        # opposite of the requested source direction.  Static placement such
        # as "오른쪽에 배치" is still judged by the edge-gap check above.
        relative_motion = any(word in text for word in ("옮겨", "이동", "밀어", "당겨", "조금"))
        if relative_motion and before and after:
            expected_signs = {}
            if any(word in text for word in ("오른쪽", "우측")):
                expected_signs["x"] = 1
            elif any(word in text for word in ("왼쪽", "좌측")):
                expected_signs["x"] = -1
            if any(word in text for word in ("아래", "아래쪽", "아래로", "밑", "하단")):
                expected_signs["y"] = 1
            elif any(word in text for word in ("위쪽", "위로", "상단")):
                expected_signs["y"] = -1
            for axis, expected_sign in expected_signs.items():
                deltas = []
                for index, old_item in enumerate(before.get("texts", [])):
                    if index >= len(after.get("texts", [])):
                        continue
                    field = f"texts[{index}].{axis}"
                    if field not in changed_fields:
                        continue
                    delta = float(after["texts"][index].get(axis, 0)) - float(old_item.get(axis, 0))
                    deltas.append(delta)
                if deltas and not any(delta * expected_sign > 1e-6 for delta in deltas):
                    label = "오른쪽/아래쪽" if expected_sign > 0 else "왼쪽/위쪽"
                    raise ScenePlanError(f"텍스트가 요청한 {label} 방향과 반대로 이동했습니다.")
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
    pixel_size = requested_font_size_pixels(text)
    if text_target and pixel_size is not None and after and after.get("texts") and not has_named_run_request:
        expected = max(.015, min(.2, pixel_size / 1600))
        actual = float(after["texts"][0].get("font_size", 0))
        if abs(actual - expected) > .001:
            raise ScenePlanError(
                f"글자 크기 요청이 실제 캔버스 기준과 다릅니다: 요청={pixel_size}px, "
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
    if "assets" in scopes and requests_circular_shape(text) \
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


def enforce_subject_background_constraints(plan: dict, instruction: str) -> tuple[dict, list[str]]:
    """Bind a background operation to explicit production-source indexes."""
    result = deepcopy(plan); applied = []
    edits = subject_background_edits(instruction)
    if not edits:
        return result, applied
    assets = result.get("assets", [])
    try:
        indexes = {int(asset["index"]) for asset in assets}
    except (KeyError, TypeError, ValueError) as exc:
        raise ScenePlanError("배경 처리 대상에 유효한 제작 이미지 번호가 없습니다.") from exc
    for edit in edits:
        requested = set(edit.source_indexes)
        if requested - indexes:
            raise ScenePlanError("배경을 처리할 사진 번호가 제작 이미지 범위를 벗어났습니다.")
        if not requested:
            if len(assets) == 1 or edit.all_sources:
                requested = indexes
            elif assets:
                raise ScenePlanError("어느 사진의 배경을 처리할지 사진 번호 또는 '모든 사진'을 지정해 주세요.")
            else:
                raise ScenePlanError("배경을 처리할 제작 이미지가 없습니다.")
        for position, asset in enumerate(assets):
            if int(asset["index"]) in requested and asset.get("remove_background", False) != edit.remove:
                asset["remove_background"] = edit.remove
                applied.append(f"assets[{position}].remove_background")
    return result, applied


def enforce_explicit_user_constraints(plan: dict, instruction: str) -> tuple[dict, list[str]]:
    """Apply only unambiguous, domain-wide layout constraints from user wording."""
    result, applied = enforce_subject_background_constraints(plan, instruction)
    raw_text = " ".join(str(instruction or "").split())
    text = raw_text.lower()
    bound_clauses = [(target, clause.casefold()) for target, clause in _bound_edit_clauses(instruction)]
    text_request = " ".join(clause for target, clause in bound_clauses if target == "text")
    if not text_request and _has_quoted_phrase_style_target(instruction):
        text_request = raw_text.casefold()
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
    circular_frame = requests_circular_shape(asset_request)
    if circular_frame:
        # "스티커를 원형으로" describes the delivered sticker/canvas, not
        # merely an already-elliptical photo frame. Preserve true alpha outside.
        if "스티커" in asset_request and result.get("canvas", {}).get("background") != "transparent":
            result.setdefault("canvas", {})["background"] = "transparent"
            applied.append("canvas.background")
        for index, asset in enumerate(result.get("assets", [])):
            width, height = float(asset.get("width", .8)), float(asset.get("height", .8))
            side = min(width, height)
            center_x = float(asset.get("x", 0)) + width / 2
            center_y = float(asset.get("y", 0)) + height / 2
            asset.update({"shape": "ellipse", "width": side, "height": side,
                          "x": round(max(0, min(1 - side, center_x - side / 2)), 4),
                          "y": round(max(0, min(1 - side, center_y - side / 2)), 4)})
            # A delivered circular sticker must fill the circle. ``contain``
            # leaves a rectangular photo floating inside an elliptical mask.
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
        hex_color = re.search(r"#[0-9a-f]{6}(?![0-9a-f])", text_request)
        if hex_color:
            requested = hex_color.group(0)
        for index, item in enumerate(result.get("texts", [])):
            named_run_updates = _requested_span_style_updates(item, raw_text)
            family_match = re.search(r"글꼴(?:을|은)?\s*['\"]([^'\"]+)['\"]", raw_text, re.I)
            if not named_run_updates and family_match and item.get("font_family") != family_match.group(1).strip():
                item["font_family"] = family_match.group(1).strip()[:80]
                applied.append(f"texts[{index}].font_family")
            pixel_size = requested_font_size_pixels(text_request)
            if not named_run_updates and pixel_size is not None:
                normalized_size = max(.015, min(.2, pixel_size / 1600))
                if item.get("font_size") != normalized_size:
                    item["font_size"] = normalized_size; applied.append(f"texts[{index}].font_size")
            if not named_run_updates and "굵기는 보통" in text_request and item.get("font_weight") != "normal":
                item["font_weight"] = "normal"; applied.append(f"texts[{index}].font_weight")
            elif (not named_run_updates and any(word in text_request for word in ("굵게", "볼드", "bold"))
                  and item.get("font_weight") != "bold"):
                item["font_weight"] = "bold"; applied.append(f"texts[{index}].font_weight")
            if not named_run_updates and requested and item.get("color") != requested:
                item["color"] = requested; applied.append(f"texts[{index}].color")
            if named_run_updates:
                spans = item.get("spans", []) if isinstance(item.get("spans"), list) else []
                for span_index, updates in named_run_updates.items():
                    if span_index >= len(spans) or not isinstance(spans[span_index], dict):
                        continue
                    for key, value in updates.items():
                        if spans[span_index].get(key) != value:
                            spans[span_index][key] = value
                            applied.append(f"texts[{index}].spans[{span_index}].{key}")
            else:
                inherited = ({"font_family"} if family_match else set()) | ({"color"} if requested else set())
                if _inherit_text_run_properties(item, inherited):
                    applied.append(f"texts[{index}].spans")
            background_targeted = any(word in text_request for word in
                                      ("텍스트박스", "텍스트 박스", "글씨 뒤 배경", "문구 뒤 배경",
                                       "글자 뒤 배경", "배경 없이", "배경을 투명", "배경 투명"))
            remove_background = any(word in text_request for word in
                                    ("없애", "제거", "투명", "빼줘", "지워", "없이"))
            if background_targeted and remove_background and item.get("background") != "transparent":
                item["background"] = "transparent"
                applied.append(f"texts[{index}].background")
            if (not named_run_updates and pixel_size is None
                    and any(word in text_request for word in ("더 크게", "크게", "키워", "키워줘"))):
                old = float(item.get("font_size", .055))
                item["font_size"] = round(min(.16, max(.055, old * 1.5)), 4)
                item["width"] = min(.85, max(float(item.get("width", .5)), .5))
                item["height"] = min(.24, max(float(item.get("height", .12)), .12))
                applied.extend([f"texts[{index}].font_size", f"texts[{index}].width",
                                f"texts[{index}].height"])
            half_size = pixel_size is None and any(word in text_request for word in ("절반", "반으로")) and any(
                word in text_request for word in ("줄여", "작게", "축소")
            )
            if half_size and not named_run_updates:
                item["font_size"] = round(max(.015, float(item.get("font_size", .055)) * .5), 4)
                applied.append(f"texts[{index}].font_size")
            if not named_run_updates and pixel_size is None and not half_size and ("너무 크" in text_request or any(
                    word in text_request for word in ("작게", "줄여", "축소"))):
                item["font_size"] = round(max(.025, float(item.get("font_size", .055)) * .65), 4)
                item["width"] = min(float(item.get("width", .7)), .72)
                item["height"] = min(float(item.get("height", .15)), .2)
                item["padding"] = min(.025, float(item.get("padding", .018)))
                applied.extend([f"texts[{index}].font_size", f"texts[{index}].width",
                                f"texts[{index}].height", f"texts[{index}].padding"])
            if not named_run_updates and any(word in text_request for word in ("볼드", "굵게", "굵은", "진하게")):
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
