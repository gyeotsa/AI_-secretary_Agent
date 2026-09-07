"""Displayed-coordinate/pixel contracts after whole-preview effects.

The real edit/save orchestration runs against synthetic raster leaves. A solid
coloured ink box deliberately stands in for glyphs: these tests measure where
the existing text layer moves, not font rasterisation or any model's quality.
No real model, account, desktop action, original portrait or global store is used.
"""
from copy import deepcopy
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
import pytest

from core.mockup_preview_effects import apply_preview_effects, undo_preview_geometry
from core.mockup_scene import ScenePlanError, scene_diff_fields
from test_mockup_review_commit_contract import rig, _plan, _render


def _scene(*, aspect_ratio=1.0, offcentre=False):
    plan = _plan()
    plan["canvas"]["aspect_ratio"] = aspect_ratio
    plan["texts"][0].update({"x": .30, "y": .32, "width": .16, "height": .08,
                              "color": "#ff0000", "font_size": .04})
    if offcentre:
        plan["assets"][0].update({"x": .08, "y": .06, "width": .32, "height": .32})
        plan["texts"] = []
    return plan


def _raster(_index, plan, width, height):
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for asset in plan.get("assets", []):
        box = (round(asset["x"] * width), round(asset["y"] * height),
               round((asset["x"] + asset["width"]) * width) - 1,
               round((asset["y"] + asset["height"]) * height) - 1)
        if asset["shape"] == "ellipse":
            draw.ellipse(box, fill=(150, 150, 150, 255))
        else:
            draw.rectangle(box, fill=(150, 150, 150, 255))
    for text in plan.get("texts", []):
        box = (round(text["x"] * width), round(text["y"] * height),
               round((text["x"] + text["width"]) * width) - 1,
               round((text["y"] + text["height"]) * height) - 1)
        draw.rectangle(box, fill=text["color"])
    return image


def _start(rig, **kwargs):
    rig.image_factory = _raster
    return _render(rig, _scene(**kwargs), instruction="합성 시안 배치를 유지해줘",
                   preview_only=True, visible_copy="" if kwargs.get("offcentre") else "KEEP")


def _red_center(path):
    with Image.open(path) as image:
        red, green, blue, alpha = image.convert("RGBA").split()
        mask = ImageChops.multiply(red.point(lambda p: 255 if p >= 220 else 0),
                                   green.point(lambda p: 255 if p <= 40 else 0))
        mask = ImageChops.multiply(mask, blue.point(lambda p: 255 if p <= 40 else 0))
        mask = ImageChops.multiply(mask, alpha)
        box = mask.getbbox()
        assert box is not None, "Expected a visible red text layer in the rendered pixels"
        return ((box[0] + box[2]) / 2 / image.width,
                (box[1] + box[3]) / 2 / image.height)


def _same_pixels(first, second):
    assert first.size == second.size
    assert all(channel.getbbox() is None for channel in
               ImageChops.difference(first.convert("RGBA"), second.convert("RGBA")).split())


def _model_failure(*_args, **_kwargs):
    raise ScenePlanError("Synthetic planner unavailable; exercise the real local fallback")


@pytest.mark.parametrize("operation,axis,word", [
    ("flip_horizontal", 0, "오른쪽"), ("flip_vertical", 1, "아래쪽"),
    ("rotate_left", 0, "오른쪽"), ("rotate_right", 0, "오른쪽"),
])
def test_local_fallback_moves_text_in_displayed_direction(rig, operation, axis, word):
    state = _start(rig)
    displayed = rig.runtime.transform_preview(state["output"], operation, metadata=state)
    before = _red_center(displayed["output"])
    rig.runtime._request_scene_edit_patch = _model_failure
    result = rig.runtime.edit_preview(displayed, f"문구 위치를 {word}으로 이동해줘")
    after = _red_center(result["output"])
    assert after[axis] > before[axis] + .01, (
        f"'{word}' moved {before} -> {after} in the displayed image after {operation}")
    assert abs(after[1 - axis] - before[1 - axis]) < .002, "The other screen axis was not requested"
    assert result["preview_effects"] == displayed["preview_effects"]


@pytest.mark.parametrize("operation,delta", [("rotate_left", .10), ("rotate_right", -.10)])
def test_correct_inverse_screen_motion_is_not_rejected_as_wrong_scene_axis(rig, operation, delta):
    state = _start(rig, aspect_ratio=1.5)
    displayed = rig.runtime.transform_preview(state["output"], operation, metadata=state)
    before = _red_center(displayed["output"])

    def correct_patch(_profile, _paths, plan, copy, _instruction, **_kwargs):
        revised = deepcopy(plan)
        revised["texts"][0]["y"] += delta  # Screen-right after a quarter turn.
        return revised, copy, scene_diff_fields(plan, revised)

    rig.runtime._request_scene_edit_patch = correct_patch
    result = rig.runtime.edit_preview(displayed, "문구를 오른쪽으로 조금 이동해줘")
    after = _red_center(result["output"])
    assert after[0] == pytest.approx(before[0] + .10, abs=.002)
    assert after[1] == pytest.approx(before[1], abs=.002)


@pytest.mark.parametrize("operation,word,source_axis,wrong_delta,screen_axis", [
    ("flip_horizontal", "오른쪽", "x", .10, 0),
    ("flip_vertical", "아래쪽", "y", .10, 1),
    ("rotate_left", "오른쪽", "y", -.10, 0),
    ("rotate_right", "오른쪽", "y", .10, 0),
])
def test_incorrect_inverse_motion_cannot_be_accepted_as_requested_screen_direction(
        rig, operation, word, source_axis, wrong_delta, screen_axis):
    state = _start(rig)
    displayed = rig.runtime.transform_preview(state["output"], operation, metadata=state)
    before = _red_center(displayed["output"])

    def incorrect_patch(_profile, _paths, plan, copy, _instruction, **_kwargs):
        revised = deepcopy(plan)
        revised["texts"][0][source_axis] += wrong_delta
        return revised, copy, scene_diff_fields(plan, revised)

    rig.runtime._request_scene_edit_patch = incorrect_patch
    try:
        result = rig.runtime.edit_preview(displayed, f"문구를 {word}으로 조금 이동해줘")
    except ScenePlanError:
        return  # Rejecting an incorrect model patch is a valid fail-closed outcome.
    after = _red_center(result["output"])
    assert after[screen_axis] > before[screen_axis], (
        f"A patch moving visibly opposite to {word} was reported as successful after {operation}")


@pytest.mark.parametrize("operation,axis,delta", [
    (None, "x", .10), ("flip_horizontal", "x", -.10), ("flip_vertical", "y", -.10),
])
def test_unrotated_and_mirrored_valid_motion_controls(rig, operation, axis, delta):
    state = _start(rig)
    displayed = rig.runtime.transform_preview(state["output"], operation, metadata=state) if operation else state
    before = _red_center(displayed["output"])

    def correct_patch(_profile, _paths, plan, copy, _instruction, **_kwargs):
        revised = deepcopy(plan)
        revised["texts"][0][axis] += delta
        return revised, copy, scene_diff_fields(plan, revised)

    rig.runtime._request_scene_edit_patch = correct_patch
    screen_axis = 1 if axis == "y" else 0
    instruction = "문구를 아래쪽으로 조금 이동해줘" if screen_axis else "문구를 오른쪽으로 조금 이동해줘"
    result = rig.runtime.edit_preview(displayed, instruction)
    after = _red_center(result["output"])
    assert after[screen_axis] == pytest.approx(before[screen_axis] + .10, abs=.002)
    assert after[1 - screen_axis] == pytest.approx(before[1 - screen_axis], abs=.002)


@pytest.mark.parametrize("operation", ["rotate_left", "rotate_right", "flip_horizontal", "flip_vertical"])
def test_offcentre_circular_pixels_do_not_require_opaque_canvas_centre(rig, operation):
    plan = _scene(offcentre=True)
    original = _raster(0, plan, 1600, 1600)
    displayed = apply_preview_effects(original, [{"operation": operation}])
    # The requested small circle is valid and simply does not cross the canvas
    # centre. Checking (width/2,height/2) instead of its frame is a false failure.
    # The low-level renderer contract consumes source-coordinate pixels.  The
    # production render/save paths first undo whole-preview geometry, so audit
    # that exact boundary instead of deliberately mixing display pixels with a
    # source-coordinate scene plan.
    source_pixels = undo_preview_geometry(displayed, [{"operation": operation}])
    violations = rig.runtime._render_contract_violations(
        source_pixels, plan, "스티커를 원형으로 만들어줘")
    assert violations == [], f"Valid off-centre circle rejected after {operation}: {violations}"


@pytest.mark.parametrize("effects", [
    ["rotate_left", "flip_horizontal"], ["flip_vertical", "rotate_right"],
])
def test_asymmetric_non_square_manual_pixels_survive_final_save_control(rig, effects):
    state = _start(rig, aspect_ratio=1.5)
    current = state
    for operation in effects:
        current = rig.runtime.transform_preview(current["output"], operation, metadata=current)
    saved = rig.runtime.save_preview(current["output"], rig.root / "approved" / "rotated.png", current)
    with Image.open(state["output"]) as original, Image.open(saved["output"]) as output:
        _same_pixels(output, apply_preview_effects(original, current["preview_effects"]))
        assert output.size == (saved["width"], saved["height"])
    assert saved["preview_effects"] == current["preview_effects"]
    assert saved["editable_svg"] == ""


def test_requested_colour_cannot_be_reported_successful_as_grayscale(rig):
    rig.image_factory = _raster
    plan = _scene()
    plan["texts"][0]["color"] = "#0000ff"
    state = _render(rig, plan, instruction="합성 시안", preview_only=True)
    displayed = rig.runtime.adjust_preview(state["output"], {"saturation": 0}, metadata=state)

    def colour_patch(_profile, _paths, plan, copy, _instruction, **_kwargs):
        revised = deepcopy(plan)
        revised["texts"][0]["color"] = "#ff0000"
        return revised, copy, scene_diff_fields(plan, revised)

    rig.runtime._request_scene_edit_patch = colour_patch
    try:
        result = rig.runtime.edit_preview(displayed, "글자 색상을 #ff0000으로 바꿔줘")
    except ScenePlanError:
        return  # An explicit conflicting-effect failure is safer than false success.
    _red_center(result["output"])


@pytest.mark.parametrize("property_name,requested,expected", [
    ("font_family", "문구 전체 글꼴을 'Gungsuh'로 바꿔줘", "Gungsuh"),
    ("color", "문구 전체 색상을 파란색으로 바꿔줘", "#2878d0"),
])
def test_global_text_style_edit_reaches_every_visible_span(rig, property_name, requested, expected):
    rig.image_factory = _raster
    plan = _scene()
    plan["texts"][0].update({
        "content": "정지원 테스트", "font_family": "Malgun Gothic", "color": "#e5484d",
        "spans": [
            {"content": "정지원", "font_family": "Arial", "color": "#e5484d"},
            {"content": "테스트", "font_family": "Malgun Gothic", "color": "#2878d0"},
        ],
    })
    state = _render(rig, plan, instruction="합성 시안", visible_copy="정지원 테스트",
                    preview_only=True)

    def parent_only_patch(_profile, _paths, current, copy, _instruction, **_kwargs):
        revised = deepcopy(current)
        revised["texts"][0][property_name] = expected
        return revised, copy, scene_diff_fields(current, revised)

    rig.runtime._request_scene_edit_patch = parent_only_patch
    result = rig.runtime.edit_preview(state, requested)
    text = result["scene_plan"]["texts"][0]
    assert text[property_name] == expected
    assert all(span.get(property_name, text[property_name]) == expected
               for span in text["spans"]), (
        "A parent style changed in JSON while one or more rendered tspan overrides stayed stale")
    assert [span["content"] for span in text["spans"]] == ["정지원", "테스트"]


def test_visual_noop_claim_repairs_pixels_that_do_not_match_the_current_rerender(rig):
    state = _start(rig)

    def visually_changed(_index, plan, width, height):
        base = _raster(_index, plan, width, height)
        # Keep the same valid geometry while making the visible raster differ.
        return base.point(lambda value: round(value * .8))

    rig.image_factory = visually_changed
    result = rig.runtime.edit_preview(state, "스티커를 원형으로 만들어줘")
    assert result["already_satisfied"] is False
    assert result["renderer"] == "verified-preview-repair-v1"
    assert result["output"] != state["output"]


def test_true_visual_noop_keeps_the_selected_preview_revision(rig):
    state = _start(rig)
    result = rig.runtime.edit_preview(state, "스티커를 원형으로 만들어줘")
    assert result["already_satisfied"] is True
    assert result["output"] == state["output"]
    assert result["revision"] == state["revision"]


def test_current_adjusted_pixels_cannot_be_used_with_an_older_base_effect_chain(rig):
    state = _start(rig)
    first = rig.runtime.adjust_preview(state["output"], {"brightness": .5}, metadata=state)
    try:
        # This currently passes the shared manual-source validator because it is
        # the displayed output, but its pixels and adjustment_base_effects differ.
        second = rig.runtime.adjust_preview(first["output"], {"brightness": .8}, metadata=first)
    except ValueError:
        return  # Rejecting a non-base slider input is valid.
    with Image.open(state["output"]) as original, Image.open(second["output"]) as output:
        _same_pixels(output, apply_preview_effects(original, second["preview_effects"]))


def test_mutated_valid_svg_is_not_published_as_the_verified_current_preview(rig):
    state = _start(rig)
    svg = Path(state["editable_svg"])
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1600">'
                   '<rect width="1600" height="1600" fill="purple"/></svg>', encoding="utf-8")
    destination = rig.root / "approved" / "current.png"
    with pytest.raises((ValueError, ScenePlanError)):
        rig.runtime.save_preview(state["output"], destination, state)
    assert not destination.exists()
    assert rig.records == []


def test_save_bundle_failure_leaves_every_previous_approved_artifact_unchanged(rig, monkeypatch):
    state = _start(rig)
    destination = rig.root / "approved" / "current.png"
    destination.parent.mkdir(parents=True)
    previous = {
        destination: b"previous-png",
        destination.with_suffix(".svg"): b"previous-svg",
        destination.with_suffix(".json"): b"previous-json",
    }
    for path, payload in previous.items():
        path.write_bytes(payload)

    def fail_before_publication(bundle):
        assert {target.suffix for target in bundle.values()} == {".png", ".svg", ".json"}
        raise RuntimeError("synthetic bundle commit outage")

    monkeypatch.setattr("core.artifact_transaction.commit_artifact_bundle", fail_before_publication)
    with pytest.raises(RuntimeError, match="synthetic bundle commit outage"):
        rig.runtime.save_preview(state["output"], destination, state)
    assert {path: path.read_bytes() for path in previous} == previous
    assert rig.records == []
