"""Adversarial synthetic QA for the mockup review -> publication boundary.

No real models, original portraits, desktop actions, or global memory writes.
The scene/commit orchestration is real; renderer and critic leaves are injected
so a fault cannot be hidden by a lucky VLM verdict or renderer invocation.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw
import pytest

import core.mockup_design as design
from core.mockup_design import MockupDesignRuntime, MockupStyleProfile
from core.mockup_scene import ScenePlanError, normalize_scene_plan, scene_diff_fields


CIRCLE_REQUEST = "스티커를 원형으로 만들어줘"


def _plan(*, with_text=True, background="transparent"):
    return normalize_scene_plan({
        "canvas": {"aspect_ratio": 1.0, "background": background},
        "assets": [{"index": 0, "x": .1, "y": .1, "width": .8, "height": .8,
                    "shape": "ellipse", "fit": "cover", "zoom": 1.0,
                    "focal_x": .5, "focal_y": .5, "rotation": 0, "z": 0}],
        "texts": ([{"content": "KEEP", "x": .2, "y": .72, "width": .6, "height": .1,
                    "font_size": .05, "font_family": "Malgun Gothic", "font_weight": "bold",
                    "color": "#e5484d", "background": "transparent", "align": "center",
                    "padding": .01, "z": 10}] if with_text else []),
        "decorations": [], "rationale": "synthetic QA scene",
    }, asset_count=1, visible_copy="KEEP" if with_text else "")


def _circle_image(width=1600, height=1600):
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((width * .1, height * .1, width * .9, height * .9),
                                 fill=(35, 80, 125, 255))
    return image


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # Production review files use a relative directory; contain it in this test.
    monkeypatch.chdir(tmp_path)
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(design.tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.setattr(design.Config, "MOCKUP_MAX_CORRECTIONS", 1)
    source = tmp_path / "synthetic-source.png"
    Image.new("RGB", (64, 64), "#23507d").save(source)
    profile = MockupStyleProfile("qa", "Synthetic", [], [], ["#23507d"], 1.0, "square", "no model")
    records = []
    runtime = MockupDesignRuntime.__new__(MockupDesignRuntime)
    runtime.enable_visual_review = True
    runtime.team_runtime = None
    runtime.subject_runtime = SimpleNamespace(status=lambda: {"birefnet_ready": False},
                                             analyze=lambda _path: {"backend": "synthetic"}, release=lambda: None)
    runtime.style_index = SimpleNamespace(add=lambda *args, **kwargs: records.append(("style", args, kwargs)))
    runtime.load_profile = lambda _profile_id: profile
    runtime._enforce_detected_subject_visibility = lambda plan, _paths, _instruction: (deepcopy(plan), [])
    runtime._review_rendered_image = lambda *_args: {"passed": True, "score": 1.0, "violations": []}
    state = SimpleNamespace(runtime=runtime, source=source, root=tmp_path, records=records,
                            rendered=[], reviews=[], image_factory=lambda _i, _plan, w, h: _circle_image(w, h))

    def serialize(graph, _paths, width, height):
        state.current_plan = deepcopy(graph["compatibility_scene_plan"])
        return '<svg xmlns="http://www.w3.org/2000/svg"/>'

    def renderer(_svg, width, height):
        plan = deepcopy(state.current_plan)
        index = len(state.rendered)
        state.rendered.append(plan)
        return state.image_factory(index, plan, width, height)

    monkeypatch.setattr(design, "layer_graph_to_svg", serialize)
    monkeypatch.setattr(design, "render_svg_with_qt", renderer)
    monkeypatch.setattr("core.quality_metrics.get_quality_metric_store", lambda: SimpleNamespace(
        record=lambda *args, **kwargs: records.append(("metric", args, kwargs))))
    return state


def _render(rig, plan, *, instruction=CIRCLE_REQUEST, visible_copy="KEEP", preview_only=False, **kwargs):
    return rig.runtime.render("qa", [str(rig.source)], scene_plan=deepcopy(plan), instruction=instruction,
                              visible_copy=visible_copy, backend="local", output_dir=rig.root / "outputs",
                              preview_only=preview_only, **kwargs)


def _correction(rig, revised, instruction="사진 내부 초점을 오른쪽으로 옮겨줘", *, second_error=False):
    def review(_path, _profile, original_instruction, visible_copy, plan):
        rig.reviews.append({"instruction": original_instruction, "copy": visible_copy, "plan": deepcopy(plan)})
        if len(rig.reviews) == 1:
            return {"passed": False, "score": .2, "violations": ["synthetic correction required"],
                    "correction_instruction": instruction}
        if second_error:
            return {"passed": False, "status": "unverified", "score": 0.0,
                    "violations": ["synthetic VLM unavailable"], "review_error": True,
                    "correction_instruction": ""}
        return {"passed": True, "score": 1.0, "violations": []}

    def patch(_profile, _paths, previous, _copy, _instruction, **_kwargs):
        return deepcopy(revised), _copy, scene_diff_fields(previous, revised)

    rig.runtime._review_rendered_image = review
    rig.runtime._request_scene_edit_patch = patch


@pytest.mark.parametrize("fault", ["opaque_corners", "fully_transparent"])
def test_corrected_raster_must_pass_alpha_contract_before_publication(rig, fault):
    original = _plan()
    revised = deepcopy(original)
    revised["assets"][0]["focal_x"] = .55
    _correction(rig, revised)
    rig.image_factory = lambda index, _plan, w, h: (
        _circle_image(w, h) if index == 0 else
        Image.new("RGBA", (w, h), (35, 80, 125, 255 if fault == "opaque_corners" else 0)))
    try:
        result = _render(rig, original)
    except ScenePlanError:
        assert not list((rig.root / "outputs").glob("*.png"))
        return
    with Image.open(result["output"]) as published:
        failures = MockupDesignRuntime._render_contract_violations(published, result["scene_plan"], CIRCLE_REQUEST)
    assert not failures, f"A post-correction raster was published without revalidation: {failures}"


def test_latest_circle_edit_not_old_creation_prompt_controls_pixel_contract(rig):
    baseline = _plan(background="#ffffff")
    baseline["assets"][0]["shape"] = "rectangle"
    rig.image_factory = lambda _i, _plan, w, h: Image.new("RGBA", (w, h), (35, 80, 125, 255))
    with pytest.raises(ScenePlanError):
        _render(rig, _plan(), instruction="사진 시안을 제작해줘", edit_baseline=baseline,
                edit_instruction=CIRCLE_REQUEST)
    assert not list((rig.root / "outputs").glob("*.png"))


def test_required_alpha_is_not_lost_in_final_output_mode_conversion(rig):
    rig.runtime.enable_visual_review = False
    try:
        result = _render(rig, _plan(background="#ffffff"))
    except ScenePlanError:
        assert not list((rig.root / "outputs").glob("*.png"))
        return
    with Image.open(result["output"]) as published:
        failures = MockupDesignRuntime._render_contract_violations(published, result["scene_plan"], CIRCLE_REQUEST)
    assert not failures, "RGBA passed in memory but the committed RGB file discarded the required alpha"


@pytest.mark.parametrize("critic_state", ["disabled", "unavailable"])
def test_required_text_layer_is_checked_independently_of_vlm_availability(rig, monkeypatch, critic_state):
    if critic_state == "disabled":
        rig.runtime.enable_visual_review = False
    else:
        class UnavailableVision:
            def analyze(self, *_args, **_kwargs):
                raise RuntimeError("synthetic VLM unavailable")
            def release_model(self):
                pass
        monkeypatch.setattr(design, "VisionRuntime", UnavailableVision)
        rig.runtime._review_rendered_image = MockupDesignRuntime._review_rendered_image.__get__(rig.runtime)
    with pytest.raises(ScenePlanError):
        _render(rig, _plan(with_text=False), visible_copy="KEEP")
    assert not list((rig.root / "outputs").glob("*.png"))


def test_auto_correction_cannot_discard_explicit_copy_then_publish_on_critic_failure(rig):
    original = _plan()
    revised = deepcopy(original)
    revised["texts"] = []
    _correction(rig, revised, instruction="문구를 삭제해줘", second_error=True)
    try:
        result = _render(rig, original, visible_copy="KEEP")
    except ScenePlanError:
        assert not list((rig.root / "outputs").glob("*.png"))
        return
    assert any(text.get("content") == "KEEP" for text in result["scene_plan"].get("texts", [])), (
        "A critic outage turned an explicit text-layer violation into a published unverified preview")


def test_critic_sees_latest_edit_instruction_not_only_creation_prompt(rig):
    baseline = _plan()
    edited = deepcopy(baseline)
    edited["texts"][0]["font_size"] = .04
    latest_request = "글자 크기만 64픽셀로 변경해줘"
    seen = []
    def review(_path, _profile, instruction, _copy, _plan):
        seen.append(instruction)
        return {"passed": True, "score": 1.0, "violations": []}
    rig.runtime._review_rendered_image = review
    _render(rig, edited, instruction=CIRCLE_REQUEST, edit_baseline=baseline, edit_instruction=latest_request)
    assert seen and all(latest_request in instruction for instruction in seen), (
        "The critic received only the original creation request, not the current edit constraints")


@pytest.mark.parametrize("changes", [
    {"font_size": .09, "font_family": "Gungsuh", "color": "#38a169"},
    {"font_size": .09},
    {"font_family": "Gungsuh"},
    {"color": "#38a169"},
], ids=["size_font_color", "size_only", "font_only", "color_only"])
def test_auto_correction_preserves_exact_latest_size_and_unrequested_typography(rig, changes):
    baseline = _plan()
    edited = deepcopy(baseline)
    edited["texts"][0]["font_size"] = .04  # user explicitly asked for 64px on 1600px canvas
    revised = deepcopy(edited)
    revised["texts"][0].update(changes)
    _correction(rig, revised, instruction="문구 글자 크기를 144픽셀로 변경하고 색상을 초록으로 바꾸고 폰트를 궁서로 변경해줘")
    try:
        result = _render(rig, edited, instruction=CIRCLE_REQUEST, edit_baseline=baseline,
                         edit_instruction="글자 크기만 64픽셀로 변경해줘")
    except ScenePlanError:
        assert not list((rig.root / "outputs").glob("*.png"))
        return
    text = result["scene_plan"]["texts"][0]
    assert text["font_size"] == .04, "The critic overrode the latest explicit pixel size"
    assert text["font_family"] == baseline["texts"][0]["font_family"], "Font changed despite a size-only request"
    assert text["color"] == baseline["texts"][0]["color"], "Color changed despite a size-only request"


def test_save_preview_does_not_approve_pixels_that_no_longer_match_circle_contract(rig):
    metadata = _render(rig, _plan(), preview_only=True)
    source = Path(metadata["output"])
    Image.new("RGBA", (1600, 1600), (35, 80, 125, 255)).save(source)
    target = rig.root / "approved" / "changed.png"
    with pytest.raises((ValueError, ScenePlanError)):
        rig.runtime.save_preview(source, target, metadata)
    assert not target.exists()
    assert rig.records == [], "Invalid final pixels must not be promoted into approved style memory/metrics"


def test_save_preview_rejects_metadata_from_another_visible_revision(rig):
    first = _render(rig, _plan(), preview_only=True)
    changed = _plan()
    changed["texts"][0]["color"] = "#2878d0"
    second = _render(rig, changed, preview_only=True)
    assert first["output"] != second["output"]
    target = rig.root / "approved" / "wrong-revision.png"
    with pytest.raises((ValueError, ScenePlanError)):
        rig.runtime.save_preview(first["output"], target, second)
    assert not target.exists()
    assert rig.records == []


def test_edit_preview_failure_cannot_leave_an_unvalidated_published_revision(rig):
    metadata = _render(rig, _plan(), preview_only=True)
    preview_root = Path(metadata["output"]).parent
    before = {path.name: sha256(path.read_bytes()).hexdigest()
              for path in preview_root.iterdir() if path.is_file()}
    edited = deepcopy(metadata["scene_plan"])
    edited["texts"][0]["font_size"] = .04
    revised = deepcopy(edited)
    revised["texts"][0]["font_size"] = .06
    request = "글자 크기를 64픽셀로 변경해줘"
    _correction(rig, revised, instruction="글자 크기를 96픽셀로 변경해줘")

    def patch(_profile, _paths, previous, copy, instruction, **_kwargs):
        candidate = edited if instruction == request else revised
        return deepcopy(candidate), copy, scene_diff_fields(previous, candidate)

    rig.runtime._request_scene_edit_patch = patch
    try:
        result = rig.runtime.edit_preview(metadata, request)
    except ScenePlanError:
        after = {path.name: sha256(path.read_bytes()).hexdigest()
                 for path in preview_root.iterdir() if path.is_file()}
        assert after == before, (
            "edit_preview rejected the final scene only after render had published a new PNG/SVG revision")
    else:
        assert result["scene_plan"]["texts"][0]["font_size"] == .04, (
            "A repaired successful edit must still fulfill the exact 64px request")
        assert Path(result["output"]).is_file()
        assert all(sha256((preview_root / name).read_bytes()).hexdigest() == digest
                   for name, digest in before.items()), "The previous visible revision must remain unchanged"
    assert rig.records == []


def test_initial_invalid_alpha_is_already_rejected_control_case(rig):
    rig.image_factory = lambda _i, _plan, w, h: Image.new("RGBA", (w, h), (35, 80, 125, 255))
    with pytest.raises(ScenePlanError, match="모서리"):
        _render(rig, _plan())
    assert not list((rig.root / "outputs").glob("*.png"))


def test_valid_unchanged_contract_can_still_be_published_control_case(rig):
    result = _render(rig, _plan(), preview_only=True)
    assert Path(result["output"]).is_file()
    assert result["scene_plan"]["texts"][0]["content"] == "KEEP"
    with Image.open(result["output"]) as image:
        assert not MockupDesignRuntime._render_contract_violations(image, result["scene_plan"], CIRCLE_REQUEST)
