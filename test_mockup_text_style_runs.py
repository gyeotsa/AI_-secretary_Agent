"""Whole-text typography contracts across parent styles and phrase overrides."""
from copy import deepcopy
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

from core.mockup_design import MockupDesignRuntime
from core.mockup_layer_graph import layer_graph_to_svg, scene_plan_to_layer_graph
from core.mockup_scene import (ScenePlanError, apply_scene_edit_patch,
                               enforce_exact_user_copy, enforce_explicit_user_constraints, filter_scene_edit_patch,
                               infer_edit_scopes, normalize_scene_plan,
                               preserve_unrequested_scene_fields, requests_visible_copy_change,
                               validate_patch_against_instruction)
from test_mockup_review_commit_contract import _plan


def _styled_plan(parent_family="Malgun Gothic", parent_color="#e5484d"):
    plan = _plan()
    plan["texts"][0].update({
        "content": "정지원 테스트", "font_family": parent_family, "color": parent_color,
        "spans": [{"content": "정지원", "font_family": "Arial", "color": "#e5484d"},
                  {"content": "테스트", "font_family": "Malgun Gothic", "color": "#2878d0"}],
    })
    return plan


def _enforce_and_protect(plan, instruction):
    revised, _ = enforce_explicit_user_constraints(plan, instruction)
    revised, _ = preserve_unrequested_scene_fields(plan, revised, instruction)
    return normalize_scene_plan(revised, asset_count=1, visible_copy=plan["texts"][0]["content"])


@pytest.mark.parametrize("parent_family", ["Malgun Gothic", "Gungsuh"])
def test_explicit_global_font_overrides_phrase_fonts_even_if_parent_already_matches(parent_family):
    before = _styled_plan(parent_family)
    revised = _enforce_and_protect(before, "문구 글꼴을 'Gungsuh'로 바꿔줘")
    text = revised["texts"][0]
    assert text["font_family"] == "Gungsuh"
    assert all(span.get("font_family", text["font_family"]) == "Gungsuh" for span in text["spans"])
    assert [span["color"] for span in text["spans"]] == ["#e5484d", "#2878d0"]
    assert text["content"] == before["texts"][0]["content"]
    assert [span["content"] for span in text["spans"]] == ["정지원", "테스트"]


@pytest.mark.parametrize("parent_color", ["#e5484d", "#2878d0"])
@pytest.mark.parametrize("instruction", ["문구 색상을 파란색으로 바꿔줘", "글씨를 파란색으로 바꿔줘"])
def test_global_color_changes_runs_without_changing_wording_fonts_or_layout(parent_color, instruction):
    before = _styled_plan(parent_color=parent_color)
    revised = _enforce_and_protect(before, instruction)
    text = revised["texts"][0]
    assert text["color"] == "#2878d0"
    assert [span["color"] for span in text["spans"]] == ["#2878d0", "#2878d0"]
    assert [span["font_family"] for span in text["spans"]] == ["Arial", "Malgun Gothic"]
    for key in ("content", "x", "y", "width", "height", "font_size", "font_weight"):
        assert text[key] == before["texts"][0][key]


def test_hex_global_color_overrides_run_colours_when_parent_already_matches():
    before = _styled_plan(parent_color="#123abc")
    revised = _enforce_and_protect(before, "문구 색상을 #123abc로 바꿔줘")
    assert revised["texts"][0]["color"] == "#123abc"
    assert [span["color"] for span in revised["texts"][0]["spans"]] == ["#123abc", "#123abc"]


@pytest.mark.parametrize("instruction", [
    "테두리를 초록색으로 바꾸고 문구 글꼴을 'Gungsuh'로 변경해줘",
    "문구 글꼴을 'Gungsuh'로 변경해줘. 테두리는 초록색 점선으로 만들어줘",
    "테두리: 초록색; 글씨 크기 64픽셀",
])
def test_another_layer_color_is_not_authorization_to_recolor_text_runs(instruction):
    before = _styled_plan()
    broad = deepcopy(before)
    broad["texts"][0].update({"color": "#38a169", "font_family": "Gungsuh", "font_size": .04})
    broad["texts"][0]["spans"][1].update({"content": "NO", "color": "#38a169"})
    protected, _ = preserve_unrequested_scene_fields(before, broad, instruction)
    assert protected["texts"][0]["color"] == before["texts"][0]["color"]
    assert [span["color"] for span in protected["texts"][0]["spans"]] == ["#e5484d", "#2878d0"]
    assert [span["content"] for span in protected["texts"][0]["spans"]] == ["정지원", "테스트"]


@pytest.mark.parametrize("instruction", ["문구 크기만 64픽셀", "문구를 아래로 내려줘", "문구 행간을 늘려줘"])
def test_non_font_or_color_edits_preserve_all_run_styles(instruction):
    before = _styled_plan()
    revised = deepcopy(before)
    revised["texts"][0]["spans"] = [{"content": "BAD", "color": "#38a169"}]
    revised["texts"][0]["font_family"] = "Gungsuh"
    revised["texts"][0]["color"] = "#38a169"
    protected, _ = preserve_unrequested_scene_fields(before, revised, instruction)
    assert protected["texts"][0]["spans"] == before["texts"][0]["spans"]
    assert protected["texts"][0]["color"] == before["texts"][0]["color"]
    assert protected["texts"][0]["font_family"] == before["texts"][0]["font_family"]


@pytest.mark.parametrize("change", ["remove", "add"])
def test_typography_cannot_change_text_layer_count(change):
    before = _styled_plan()
    revised = deepcopy(before)
    revised["texts"] = [] if change == "remove" else revised["texts"] * 2
    protected, _ = preserve_unrequested_scene_fields(before, revised, "글꼴을 궁서로 바꿔줘")
    assert protected["texts"] == before["texts"]


@pytest.mark.parametrize("instruction,expected", [
    ("글씨를 빨간색으로 바꿔줘", False),
    ("문구를 파란색으로 변경해줘", False),
    ("문구를 #123abc로 바꿔줘", False),
    ("문구를 궁서체로 바꿔줘", False),
    ("글씨 크기를 64픽셀로 변경해줘", False),
    ("문구를 '빨간색'으로 바꿔줘", True),
    ("문구 내용을 '테두리 초록색'으로 바꿔줘", True),
    ("문구 내용을 새 제목으로 바꿔줘", True),
])
def test_style_values_are_not_literal_copy_unless_requested_as_content(instruction, expected):
    assert requests_visible_copy_change(instruction) is expected


def test_unquoted_copy_and_style_change_replaces_wording_without_stale_spans():
    before = _styled_plan(parent_color="#111111")
    instruction = "문구를 새 제목으로 바꾸고 빨간색으로 해줘"
    revised, copy, copy_fields = enforce_exact_user_copy(
        before, instruction, before["texts"][0]["content"],
    )
    revised, style_fields = enforce_explicit_user_constraints(revised, instruction)

    assert copy == "새 제목"
    assert revised["texts"][0]["content"] == "새 제목"
    assert "spans" not in revised["texts"][0]
    assert revised["texts"][0]["color"] == "#e5484d"
    assert "texts[0].content" in copy_fields
    assert "texts[0].color" in style_fields


def test_svg_receives_the_requested_family_for_every_preserved_phrase(tmp_path):
    before = _styled_plan()
    revised = _enforce_and_protect(before, "문구 글꼴을 'Gungsuh'로 바꿔줘")
    source = tmp_path / "synthetic.png"
    Image.new("RGB", (12, 12), "gray").save(source)
    svg = layer_graph_to_svg(scene_plan_to_layer_graph(revised), [source], 800, 800)
    nodes = ET.fromstring(svg).findall(".//{http://www.w3.org/2000/svg}text")
    assert len(nodes) == 2
    assert [node.text for node in nodes] == ["정지원", " 테스트"]
    assert all("Gungsuh" in node.attrib["font-family"] for node in nodes)
    assert [node.attrib["fill"] for node in nodes] == ["#e5484d", "#2878d0"]


def test_quoted_phrase_style_patch_changes_only_the_named_span():
    before = _styled_plan()
    instruction = "'정지원'만 초록색으로 바꿔줘"
    assert infer_edit_scopes(instruction) == {"texts"}
    filtered, removed = filter_scene_edit_patch({
        "texts": [{
            "index": 0,
            "action": "update",
            "spans": [{"index": 0, "color": "#38a169"}],
        }],
    }, instruction)
    assert removed == []
    revised, next_copy, changed = apply_scene_edit_patch(
        before, filtered, asset_count=1, visible_copy="정지원 테스트",
    )
    protected, _ = preserve_unrequested_scene_fields(before, revised, instruction)
    validate_patch_against_instruction(instruction, changed, before, protected)

    text = protected["texts"][0]
    assert next_copy == "정지원 테스트"
    assert text["content"] == before["texts"][0]["content"]
    assert {key: text[key] for key in ("font_family", "font_size", "font_weight", "color")} == {
        key: before["texts"][0][key]
        for key in ("font_family", "font_size", "font_weight", "color")
    }
    assert text["spans"][0]["color"] == "#38a169"
    assert text["spans"][1] == before["texts"][0]["spans"][1]


def test_unquoted_known_phrase_is_resolved_from_current_scene_before_patch_masking():
    before = _styled_plan()
    instruction = "정지원만 초록색으로 바꾸고 테두리는 흰색 점선으로 해줘"
    patch = {
        "texts": [{
            "index": 0, "action": "update",
            "spans": [{"index": 0, "color": "#38a169"}],
        }],
        "decorations": [{
            "index": 0, "action": "add", "type": "ellipse",
            "stroke": "#ffffff", "dash": True,
        }],
    }
    filtered, removed = filter_scene_edit_patch(patch, instruction, before=before)
    assert removed == []
    assert filtered["texts"][0]["spans"] == [{"index": 0, "color": "#38a169"}]
    assert filtered["decorations"][0]["stroke"] == "#ffffff"

    revised, _copy, changed = apply_scene_edit_patch(
        before, filtered, asset_count=1, visible_copy="정지원 테스트",
    )
    protected, scopes = preserve_unrequested_scene_fields(before, revised, instruction)
    validate_patch_against_instruction(instruction, changed, before, protected)

    assert scopes == {"texts", "decorations"}
    assert protected["texts"][0]["spans"][0]["color"] == "#38a169"
    assert protected["texts"][0]["spans"][1] == before["texts"][0]["spans"][1]
    assert protected["decorations"][0]["stroke"] == "#ffffff"


def test_deterministic_fallback_honors_quoted_phrase_without_generic_text_noun():
    before = _styled_plan()
    revised = _enforce_and_protect(before, "'정지원'만 초록색으로 바꿔줘")
    assert revised["texts"][0]["spans"][0]["color"] == "#38a169"
    assert revised["texts"][0]["spans"][1] == before["texts"][0]["spans"][1]
    assert revised["texts"][0]["color"] == before["texts"][0]["color"]


def test_phrase_style_write_barrier_restores_parent_layout_and_other_spans():
    before = _styled_plan()
    broad = deepcopy(before)
    broad["texts"][0].update({
        "x": .02, "font_family": "Gungsuh", "color": "#38a169",
    })
    broad["texts"][0]["spans"][0].update({"color": "#38a169"})
    broad["texts"][0]["spans"][1].update({"color": "#38a169", "font_family": "Arial"})
    protected, _ = preserve_unrequested_scene_fields(
        before, broad, "'정지원'만 초록색으로 바꿔줘",
    )
    assert protected["texts"][0]["x"] == before["texts"][0]["x"]
    assert protected["texts"][0]["font_family"] == before["texts"][0]["font_family"]
    assert protected["texts"][0]["color"] == before["texts"][0]["color"]
    assert protected["texts"][0]["spans"][0]["color"] == "#38a169"
    assert protected["texts"][0]["spans"][1] == before["texts"][0]["spans"][1]


@pytest.mark.parametrize("collateral", ["parent", "other_span", "wording"])
def test_phrase_style_validation_rejects_proxy_or_collateral_changes(collateral):
    before = _styled_plan()
    after = deepcopy(before)
    after["texts"][0]["spans"][0]["color"] = "#38a169"
    changed = ["texts[0].spans[0].color"]
    if collateral == "parent":
        after["texts"][0]["color"] = "#38a169"
        changed.append("texts[0].color")
    elif collateral == "other_span":
        after["texts"][0]["spans"][1]["color"] = "#38a169"
        changed.append("texts[0].spans[1].color")
    else:
        after["texts"][0]["spans"][1]["content"] = "다른 말"
        changed.append("texts[0].spans[1].content")
    with pytest.raises(ScenePlanError, match="부분 문구 스타일 요청"):
        validate_patch_against_instruction(
            "'정지원'만 초록색으로 바꿔줘", changed, before, after,
        )


def test_pillow_fallback_renders_each_span_with_its_own_style(monkeypatch):
    plan = _styled_plan()
    plan.update({"assets": [], "decorations": []})
    plan["canvas"].update({"aspect_ratio": 1, "background": "#ffffff"})
    plan["texts"][0].update({
        "x": .08, "y": .28, "width": .84, "height": .4,
        "font_size": .12, "background": "transparent", "padding": 0,
        "spans": [
            {"content": "정지원", "font_family": "Arial", "font_size": .09,
             "font_weight": "normal", "color": "#e5484d"},
            {"content": "테스트", "font_family": "Gungsuh", "font_size": .16,
             "font_weight": "bold", "color": "#2878d0"},
        ],
    })
    calls = []
    original = MockupDesignRuntime._font

    def recording_font(size, bold=False, family=""):
        calls.append((size, bold, family))
        return original(size, bold=bold, family=family)

    monkeypatch.setattr(MockupDesignRuntime, "_font", staticmethod(recording_font))
    runtime = MockupDesignRuntime.__new__(MockupDesignRuntime)
    rendered = runtime._render_scene_plan(plan, [], long_edge=600)
    rgb = rendered.convert("RGB")
    pixels = [rgb.getpixel((x, y)) for y in range(rgb.height) for x in range(rgb.width)]

    assert any(red > 180 and red > blue * 1.25 for red, _green, blue in pixels)
    assert any(blue > 140 and blue > red * 1.25 for red, _green, blue in pixels)
    assert any(not bold and family == "Arial" for _size, bold, family in calls)
    assert any(bold and family == "Gungsuh" for _size, bold, family in calls)
    assert max(size for size, _bold, family in calls if family == "Gungsuh") > max(
        size for size, _bold, family in calls if family == "Arial"
    )
