"""Synthetic current-preview continuity tests; no live models or user assets."""
from copy import deepcopy
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
import pytest

from core.mockup_design import MockupDesignRuntime
from core.mockup_preview_effects import (apply_preview_effects, assert_preview_state,
                                        instruction_in_source_coordinates, normalize_preview_effects,
                                        undo_preview_geometry, with_preview_state)
from core.mockup_scene import enforce_explicit_user_constraints, requested_font_size_pixels, scene_diff_fields
from test_mockup_review_commit_contract import rig, _plan, _render


def _assert_same(first, second):
    assert first.size == second.size
    assert all(channel.getbbox() is None for channel in
               ImageChops.difference(first.convert("RGBA"), second.convert("RGBA")).split())


@pytest.fixture
def manual(tmp_path, monkeypatch):
    import core.mockup_design as design
    monkeypatch.setattr(design.tempfile, "gettempdir", lambda: str(tmp_path))
    image = Image.new("RGBA", (30, 20), (25, 60, 90, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((3, 4, 14, 16), fill=(100, 40, 80, 128))
    draw.rectangle((15, 4, 26, 16), fill=(40, 130, 110, 255))
    path = tmp_path / "input.png"
    image.save(path)
    runtime = MockupDesignRuntime.__new__(MockupDesignRuntime)
    state = with_preview_state({"output": str(path), "output_sha256": runtime._sha256(path),
                                "editable_svg": "old-scene.svg", "revision": 2})
    return runtime, path, image, state


@pytest.mark.parametrize("operation", ["rotate_left", "rotate_right", "flip_horizontal", "flip_vertical",
                                        "brightness", "contrast", "saturation", "sharpness"])
def test_manual_operations_preserve_alpha_source_and_replayable_state(manual, operation):
    runtime, source, original, state = manual
    before = deepcopy(state)
    result = runtime.transform_preview(source, operation, .8, metadata=state)
    assert state == before
    assert result["output"] != str(source)
    assert result["editable_svg"] == ""  # Never export a stale untransformed SVG.
    assert result["output_sha256"] == runtime._sha256(Path(result["output"]))
    assert result["adjustment_base_sha256"] == result["output_sha256"]
    assert result["adjustments"] == {}
    assert_preview_state(result)
    with Image.open(source) as unchanged, Image.open(result["output"]) as output:
        _assert_same(original, unchanged)
        _assert_same(output, apply_preview_effects(original, result["preview_effects"]))
        assert {value for _count, value in output.getchannel("A").getcolors()} == {0, 128, 255}


def test_adjustments_use_stable_base_not_accumulating_previous_slider_value(manual):
    runtime, source, original, state = manual
    first = runtime.adjust_preview(source, {"brightness": .6}, metadata=state)
    second = runtime.adjust_preview(first["adjustment_base"], {"brightness": .8}, metadata=first)
    assert len(second["preview_effects"]) == 1
    assert second["adjustment_base_effects"] == []
    with Image.open(second["output"]) as output:
        _assert_same(output, apply_preview_effects(original, second["preview_effects"]))
    reset = runtime.adjust_preview(second["adjustment_base"], {}, metadata=second)
    assert reset["preview_effects"] == []
    with Image.open(reset["output"]) as output:
        _assert_same(original, output)


def test_adjust_rotate_adjust_keeps_order_and_does_not_double_apply(manual):
    runtime, source, original, state = manual
    first = runtime.adjust_preview(source, {"brightness": .6}, metadata=state)
    rotated = runtime.transform_preview(first["output"], "rotate_left", metadata=first)
    adjusted = runtime.adjust_preview(rotated["output"], {"saturation": .4}, metadata=rotated)
    assert [step["operation"] for step in adjusted["preview_effects"]] == ["adjust", "rotate_left", "adjust"]
    with Image.open(adjusted["output"]) as output:
        _assert_same(output, apply_preview_effects(original, adjusted["preview_effects"]))
    assert rotated["adjustments"] == {}
    assert adjusted["adjustment_base_effects"] == rotated["preview_effects"]


@pytest.mark.parametrize("value", [True, "0.8", [], {}, float("nan"), float("inf"), -.1, 2.1])
def test_invalid_adjustment_fails_before_new_preview(manual, value):
    runtime, source, _image, state = manual
    before = set(source.parent.rglob("*.png"))
    with pytest.raises(ValueError):
        runtime.adjust_preview(source, {"brightness": value}, metadata=state)
    assert set(source.parent.rglob("*.png")) == before


@pytest.mark.parametrize("effects", [{}, "rotate_left", [None], [{"operation": "delete"}],
                                     [{"operation": "rotate_left", "unknown": True}],
                                     [{"operation": "adjust", "values": {"brightness": "1"}}]])
def test_effect_history_is_validated_not_silently_ignored(effects):
    with pytest.raises(ValueError):
        normalize_preview_effects(effects)


@pytest.mark.parametrize("tamper", ["pixels", "effects", "source"])
def test_manual_edit_rejects_stale_or_mutated_baseline(manual, tamper, tmp_path):
    runtime, source, original, state = manual
    if tamper == "pixels":
        original.convert("RGB").save(source)
    elif tamper == "effects":
        state["preview_effects"] = [{"operation": "rotate_left"}]
    else:
        source = tmp_path / "other.png"
        original.save(source)
    with pytest.raises(ValueError):
        runtime.transform_preview(source, "flip_horizontal", metadata=state)


@pytest.mark.parametrize("instruction", ["글자 크기만 64픽셀", "글씨 크기를 64픽셀로 줄여줘",
                                     "폰트 크기는 64px로 키워줘", "문구 크기:64PX",
                                     "글자 크기 64픽셀로 더 크게 해줘", "문구 크기도 64픽셀"])
def test_absolute_size_contract_is_shared_across_particles_units_and_relative_verbs(instruction):
    assert requested_font_size_pixels(instruction) == 64
    plan, _ = enforce_explicit_user_constraints(_plan(), instruction)
    assert plan["texts"][0]["font_size"] == .04


def test_size_like_quoted_copy_is_not_a_typography_command():
    assert requested_font_size_pixels("문구를 '글자 크기 64픽셀'로 바꿔줘") is None


def test_ai_edit_receives_active_snapshot_and_replays_manual_effects(rig):
    original = _render(rig, _plan(), preview_only=True)
    adjusted = rig.runtime.adjust_preview(original["output"], {"brightness": .6}, metadata=original)
    manual_state = rig.runtime.transform_preview(adjusted["output"], "rotate_left", metadata=adjusted)
    seen = {}
    def patch(_profile, _paths, plan, copy, _instruction, **kwargs):
        seen.update(kwargs)
        result = deepcopy(plan)
        result["texts"][0]["font_size"] = .04
        return result, copy, scene_diff_fields(plan, result)
    rig.runtime._request_scene_edit_patch = patch
    result = rig.runtime.edit_preview(manual_state, "글자 크기만 64픽셀로 변경해줘")
    assert seen["current_preview_path"] == manual_state["output"]
    assert result["preview_effects"] == manual_state["preview_effects"]
    assert result["adjustments"] == {}
    assert result["scene_plan"]["texts"][0]["font_size"] == .04
    with Image.open(manual_state["output"]) as before, Image.open(result["output"]) as after:
        # This injected raster renderer omits text, isolating effect continuity.
        _assert_same(before, after)
    assert_preview_state(result)


def test_ai_edit_rejects_unrendered_scene_mutation(rig):
    state = _render(rig, _plan(), preview_only=True)
    state["scene_plan"]["texts"][0]["color"] = "#000000"
    with pytest.raises(ValueError):
        rig.runtime.edit_preview(state, "글자 크기만 64픽셀로 변경해줘")


def test_manual_effect_history_and_pixels_can_be_saved_together(rig):
    state = _render(rig, _plan(), preview_only=True)
    changed = rig.runtime.transform_preview(state["output"], "flip_horizontal", metadata=state)
    target = rig.root / "approved" / "manual.png"
    saved = rig.runtime.save_preview(changed["output"], target, changed)
    assert saved["preview_effects"] == [{"operation": "flip_horizontal"}]
    assert saved["editable_svg"] == ""
    assert target.read_bytes() == Path(changed["output"]).read_bytes()
    assert target.with_suffix(".json").is_file()
    assert_preview_state(saved)


def test_failed_bundle_publish_preserves_previous_image_and_no_approval(rig, monkeypatch):
    import core.artifact_transaction as transaction
    state = _render(rig, _plan(), preview_only=True)
    target = rig.root / "approved" / "keep.png"
    target.parent.mkdir()
    Image.new("RGB", (10, 10), "red").save(target)
    old_png = target.read_bytes()
    old_json = b'{"previous":true}'
    target.with_suffix(".json").write_bytes(old_json)
    real_replace = transaction.os.replace
    def fail_json(source, destination):
        if Path(destination) == target.with_suffix(".json"):
            raise OSError("synthetic publish failure")
        return real_replace(source, destination)
    monkeypatch.setattr(transaction.os, "replace", fail_json)
    with pytest.raises(RuntimeError):
        rig.runtime.save_preview(state["output"], target, state)
    assert target.read_bytes() == old_png
    assert target.with_suffix(".json").read_bytes() == old_json
    assert rig.records == []


def test_approval_memory_failure_does_not_misreport_a_completed_save(rig):
    state = _render(rig, _plan(), preview_only=True)
    def fail_memory(*_args, **_kwargs):
        raise OSError("synthetic index unavailable")
    rig.runtime.style_index.add = fail_memory
    saved = rig.runtime.save_preview(state["output"], rig.root / "saved.png", state)
    assert Path(saved["output"]).is_file()
    assert saved["post_save_warnings"]


def test_old_creation_copy_is_not_reapplied_to_a_later_edit(rig):
    original = _plan()
    original["texts"][0]["content"] = "OLD"
    revised = deepcopy(original)
    revised["texts"][0]["content"] = "NEW"
    result = _render(rig, revised, instruction="문구는 OLD는 빨간색",
                     visible_copy="NEW", edit_baseline=original,
                     edit_instruction="문구 내용을 NEW로 변경해줘")
    assert result["visible_copy"] == "NEW"
    assert result["scene_plan"]["texts"][0]["content"] == "NEW"


@pytest.mark.parametrize("operation,directions", [
    ("rotate_left", {"오른쪽": "아래쪽", "왼쪽": "위쪽", "위쪽": "오른쪽", "아래쪽": "왼쪽"}),
    ("rotate_right", {"오른쪽": "위쪽", "왼쪽": "아래쪽", "위쪽": "왼쪽", "아래쪽": "오른쪽"}),
    ("flip_horizontal", {"오른쪽": "왼쪽", "왼쪽": "오른쪽", "위쪽": "위쪽", "아래쪽": "아래쪽"}),
    ("flip_vertical", {"오른쪽": "오른쪽", "왼쪽": "왼쪽", "위쪽": "아래쪽", "아래쪽": "위쪽"}),
])
def test_screen_direction_is_compiled_to_source_axis_for_each_manual_transform(operation, directions):
    effect = [{"operation": operation}]
    for visible, source in directions.items():
        result = instruction_in_source_coordinates(f"문구를 {visible}으로 옮겨줘", effect)
        assert result == f"문구를 {source}으로 옮겨줘"


def test_spatial_instruction_handles_composition_particles_and_quoted_copy():
    effects = [{"operation": "flip_horizontal"}, {"operation": "rotate_left"},
               {"operation": "adjust", "values": {"brightness": 1, "contrast": 1,
                                                     "saturation": 1, "sharpness": 1.2}}]
    result = instruction_in_source_coordinates(
        "문구 '오른쪽 위로'는 그대로 두고 글씨를 오른쪽으로, 사진은 위로 옮겨줘", effects)
    assert "'오른쪽 위로'" in result
    assert "글씨를 아래쪽으로" in result
    assert "사진은 왼쪽으로" in result


@pytest.mark.parametrize("effects", [
    [{"operation": "rotate_left"}], [{"operation": "rotate_right"}],
    [{"operation": "flip_horizontal"}], [{"operation": "flip_vertical"}],
    [{"operation": "rotate_left"}, {"operation": "flip_vertical"},
     {"operation": "rotate_right"}, {"operation": "flip_horizontal"}],
])
def test_undo_preview_geometry_returns_exact_source_pixels(manual, effects):
    _runtime, _path, original, _state = manual
    displayed = apply_preview_effects(original, effects)
    restored = undo_preview_geometry(displayed, effects)
    _assert_same(original, restored)


def test_off_center_circular_asset_uses_its_declared_box_not_canvas_center():
    plan = _plan()
    plan["assets"][0].update({"x": .05, "y": .08, "width": .3, "height": .3})
    image = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((10, 16, 70, 76), fill=(40, 80, 120, 255))
    assert MockupDesignRuntime._render_contract_violations(
        image, plan, "스티커를 원형으로 만들어줘") == []
