"""Synthetic-only integration contracts for non-destructive subject removal.

The renderers are real, but subject inference and scene planning are stubs.  No
model is loaded/downloaded, and every source/output/temp artifact belongs to a
pytest temporary directory.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from PIL import Image, ImageChops, ImageDraw
import pytest

from core.mockup_design import MockupDesignRuntime, MockupStyleProfile
from core.mockup_document import assert_scene_document
from core.mockup_layer_graph import (
    layer_graph_to_svg, render_svg_with_qt, scene_plan_to_layer_graph,
)
from core.mockup_pipeline_policy import route_mockup_request, subject_background_action
from core.mockup_scene import (
    SCENE_EDIT_PATCH_JSON_SCHEMA, SCENE_PLAN_JSON_SCHEMA, ScenePlanError,
    apply_scene_edit_patch, enforce_explicit_user_constraints,
    enforce_subject_background_constraints, normalize_scene_plan,
    preserve_unrequested_scene_fields,
)
from core.mockup_subject_assets import prepared_subject_sources
from core.mockup_subject_runtime import SegmentationResult


BACKGROUND = (0, 64, 192)
SUBJECT = (224, 80, 32)


@pytest.fixture(autouse=True)
def isolated_temporary_files(tmp_path, monkeypatch):
    # edit_preview deliberately writes to gettempdir(), not its output_dir.
    # Keep that behavior under test without using the user's real preview area.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _mask(size=(64, 64), *, foreground=255):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rectangle(
        (size[0] // 4, size[1] // 4, size[0] * 3 // 4 - 1, size[1] * 3 // 4 - 1),
        fill=foreground,
    )
    return mask


def _source(tmp_path, name="source.png", *, alpha=None):
    path = tmp_path / name
    image = Image.new("RGB", (64, 64), BACKGROUND)
    ImageDraw.Draw(image).rectangle((16, 16, 47, 47), fill=SUBJECT)
    if alpha is not None:
        image = image.convert("RGBA")
        image.putalpha(alpha)
    image.save(path, "PNG")
    return path


def _plan(count=1, *, remove=None, text=False, transparent=False):
    raw = {
        "canvas": {"aspect_ratio": 1.0, "background": "transparent" if transparent else "#ffffff"},
        "assets": [{
            "index": index, "x": index / count, "y": 0.0,
            "width": 1 / count, "height": 1.0, "shape": "rectangle",
            "fit": "cover", "zoom": 1.0, "focal_x": .5, "focal_y": .5,
            "rotation": 0, "z": 0,
        } for index in range(count)],
        "decorations": [], "texts": [],
    }
    if remove is not None:
        for asset in raw["assets"]:
            asset["remove_background"] = remove
    if text:
        raw["texts"] = [{
            "content": "테스트", "x": .3, "y": .7, "width": .4, "height": .1,
            "font_family": "Malgun Gothic", "font_size": .045,
            "font_weight": "normal", "color": "#111111", "align": "center",
            "background": "transparent", "padding": .01, "z": 3,
        }]
    return normalize_scene_plan(raw, asset_count=count, visible_copy="테스트" if text else "")


class SubjectStub:
    def __init__(self, result_factory=None):
        self.result_factory = result_factory
        self.calls = []
        self.analysis_calls = []
        self.releases = 0

    def status(self):
        return {"birefnet_ready": False, "grabcut_ready": False, "backend": "synthetic-stub"}

    def analyze(self, path):
        self.analysis_calls.append(Path(path))
        with Image.open(path) as image:
            width, height = image.size
        return {"width": width, "height": height, "faces": [], "safe_text_regions": [],
                "segmentation": self.status()}

    def segment_with_evidence(self, path):
        self.calls.append(Path(path))
        if self.result_factory:
            return self.result_factory(Path(path))
        with Image.open(path) as image:
            mask = _mask(image.size)
        return SegmentationResult(
            "succeeded", "synthetic-stub", mask, True,
            attempts=[{"backend": "synthetic-stub", "status": "succeeded"}],
        )

    def release(self):
        self.releases += 1


def _runtime(monkeypatch, subject):
    # Bypass production constructors that initialize persistent style stores.
    runtime = object.__new__(MockupDesignRuntime)
    runtime.subject_runtime = subject
    runtime.team_runtime = None
    runtime.enable_visual_review = False
    runtime.vision = None
    runtime.scene_planner = None
    runtime.generation_backend = SimpleNamespace(generate_background=lambda **_kwargs: Image.new("RGB", (1600, 1600), "#5a1464"))
    runtime.sdxl_backend = runtime.generation_backend
    profile = MockupStyleProfile(
        profile_id="synthetic-profile", name="synthetic", reference_paths=[],
        reference_hashes=[], palette=["#ffffff"], median_aspect_ratio=1.0,
        orientation="square", vision_analysis="synthetic fixture only",
    )
    monkeypatch.setattr(runtime, "load_profile", lambda _profile_id: profile)
    monkeypatch.setattr(runtime, "_detect_primary_face", lambda _path: None)

    def no_remote_planner(*_args, **_kwargs):
        raise ScenePlanError("synthetic offline planner")

    monkeypatch.setattr(runtime, "_request_scene_edit_patch", no_remote_planner)
    return runtime


def _assert_no_subject_temporaries(tmp_path):
    assert list(tmp_path.glob("anis_mockup_subject_*")) == []


def test_unrequested_subject_processing_is_lazy_and_preserves_source_paths(tmp_path):
    sources = [_source(tmp_path), _source(tmp_path, "other.png")]
    before = [path.read_bytes() for path in sources]
    subject = SubjectStub()
    with prepared_subject_sources(_plan(2), sources, subject) as (effective, evidence):
        assert effective == sources
        assert evidence == []
    assert subject.calls == []
    assert [path.read_bytes() for path in sources] == before
    _assert_no_subject_temporaries(tmp_path)


@pytest.mark.parametrize("status,backend", [("succeeded", "birefnet"), ("fallback", "opencv-grabcut")])
def test_prepared_source_preserves_pixels_and_auditable_fallback(tmp_path, status, backend):
    source = _source(tmp_path)
    original = source.read_bytes()
    result = SegmentationResult(status, backend, _mask(), True,
                                attempts=[{"backend": "birefnet", "status": "failed", "error": "fixture failure"}])
    subject = SubjectStub(lambda _path: result)
    plan = _plan(remove=True)
    untouched_plan = deepcopy(plan)
    with prepared_subject_sources(plan, [source], subject) as (effective, evidence):
        prepared = effective[0]
        assert prepared != source and prepared.is_file()
        with Image.open(prepared) as image:
            assert image.mode == "RGBA" and image.size == (64, 64)
            assert image.getpixel((2, 2))[3] == 0
            assert image.getpixel((32, 32)) == (*SUBJECT, 255)
            with Image.open(source) as original_image:
                assert ImageChops.difference(image.convert("RGB"), original_image.convert("RGB")).getbbox() is None
        assert evidence[0]["source_sha256"] == hashlib.sha256(original).hexdigest()
        assert evidence[0]["mask_sha256"] == hashlib.sha256(_mask().tobytes()).hexdigest()
        assert evidence[0]["fallback"] is (status == "fallback")
        assert evidence[0]["backend"] == backend
        assert evidence[0]["render_input_validation"]["changed_fraction"] > .5
        assert json.loads(json.dumps(evidence))[0]["success"] is True
    assert not prepared.exists()
    assert source.read_bytes() == original and plan == untouched_plan
    assert subject.calls == [source] and subject.releases == 1
    _assert_no_subject_temporaries(tmp_path)


def test_only_requested_source_is_prepared_and_duplicate_layers_reuse_it(tmp_path):
    sources = [_source(tmp_path, "first.png"), _source(tmp_path, "second.png")]
    plan = _plan(2)
    plan["assets"][1]["remove_background"] = True
    plan["assets"].append(deepcopy(plan["assets"][1]))
    subject = SubjectStub()
    with prepared_subject_sources(plan, sources, subject) as (effective, evidence):
        assert effective[0] == sources[0]
        assert effective[1] != sources[1]
        assert [item["source_index"] for item in evidence] == [1]
    assert subject.calls == [sources[1]]
    _assert_no_subject_temporaries(tmp_path)


@pytest.mark.parametrize("invalid", ["true", "false", 0, 1, None, [], {}])
def test_preparation_rejects_non_boolean_background_flags(tmp_path, invalid):
    source = _source(tmp_path)
    plan = _plan()
    plan["assets"][0]["remove_background"] = invalid
    subject = SubjectStub()
    with pytest.raises(ScenePlanError, match="true/false"):
        with prepared_subject_sources(plan, [source], subject):
            pytest.fail("invalid flags must not reach rendering")
    assert subject.calls == []
    _assert_no_subject_temporaries(tmp_path)


@pytest.mark.parametrize("index", [-1, 1, 500])
def test_preparation_rejects_out_of_range_source_indexes(tmp_path, index):
    source = _source(tmp_path)
    plan = _plan(remove=True)
    plan["assets"][0]["index"] = index
    subject = SubjectStub()
    with pytest.raises(ScenePlanError, match="번호"):
        with prepared_subject_sources(plan, [source], subject):
            pytest.fail("invalid index must not reach rendering")
    assert subject.calls == []


@pytest.mark.parametrize("mask_factory", [
    lambda: Image.new("L", (64, 64), 0),
    lambda: Image.new("L", (64, 64), 128),
    lambda: Image.new("L", (64, 64), 255),
    lambda: Image.new("RGB", (64, 64), "white"),
    lambda: _mask((63, 64)),
    lambda: None,
], ids=["empty", "flat", "opaque", "rgb", "wrong-size", "missing"])
def test_adapter_success_claim_cannot_publish_an_invalid_mask(tmp_path, mask_factory):
    source = _source(tmp_path)
    original = source.read_bytes()
    subject = SubjectStub(lambda _path: SegmentationResult("succeeded", "lying-adapter", mask_factory(), True))
    with pytest.raises(ScenePlanError):
        with prepared_subject_sources(_plan(remove=True), [source], subject):
            pytest.fail("invalid masks must not be rendered")
    assert source.read_bytes() == original
    assert subject.releases == 1
    _assert_no_subject_temporaries(tmp_path)


@pytest.mark.parametrize("status", ["failed", "unchanged"])
def test_failure_or_unchanged_segmentation_keeps_original_and_exposes_reason(tmp_path, status):
    source = _source(tmp_path)
    original = source.read_bytes()
    subject = SubjectStub(lambda _path: SegmentationResult(status, "stub", _mask(), False,
                                                         error="fixture rejected segmentation"))
    with pytest.raises(ScenePlanError, match="fixture rejected segmentation"):
        with prepared_subject_sources(_plan(remove=True), [source], subject):
            pytest.fail("a rejected result must not reach rendering")
    assert source.read_bytes() == original
    assert subject.releases == 1
    _assert_no_subject_temporaries(tmp_path)


def test_existing_partial_alpha_is_never_resurrected_or_double_attenuated(tmp_path):
    alpha = Image.new("L", (64, 64), 255)
    alpha.putpixel((32, 32), 128)
    alpha.putpixel((33, 32), 0)
    source = _source(tmp_path, alpha=alpha)
    subject = SubjectStub()
    with prepared_subject_sources(_plan(remove=True), [source], subject) as (effective, _evidence):
        with Image.open(effective[0]) as image:
            assert image.getpixel((32, 32))[3] == 128
            assert image.getpixel((33, 32))[3] == 0
            assert image.getpixel((34, 32))[3] == 255
    assert subject.calls == [source]


def test_unchanged_existing_alpha_preserves_source_without_claiming_subject_success(tmp_path):
    alpha = _mask(foreground=128)
    source = _source(tmp_path, alpha=alpha)
    original = source.read_bytes()
    subject = SubjectStub(lambda _path: SegmentationResult(
        "unchanged", "synthetic-stub", alpha, False, error="추가 분리 미확인",
    ))
    with pytest.raises(ScenePlanError, match="추가 분리 미확인"):
        with prepared_subject_sources(_plan(remove=True), [source], subject):
            pytest.fail("existing alpha is not semantic proof of a successful cutout")
    with Image.open(source) as image:
        assert ImageChops.difference(image.getchannel("A"), alpha).getbbox() is None
    assert source.read_bytes() == original
    assert subject.calls == [source]


def test_transparent_photo_frame_is_not_proof_that_subject_background_was_removed(tmp_path):
    # A 1px transparent photo border is not a subject cutout: blue background
    # still surrounds the orange synthetic subject and must be segmented.
    alpha = Image.new("L", (64, 64), 255)
    ImageDraw.Draw(alpha).rectangle((0, 0, 63, 63), outline=0, width=1)
    source = _source(tmp_path, alpha=alpha)
    subject = SubjectStub()
    with prepared_subject_sources(_plan(remove=True), [source], subject) as (effective, _evidence):
        with Image.open(effective[0]) as image:
            assert image.getpixel((8, 32))[3] == 0
    assert subject.calls == [source]


def test_temporaries_and_runtime_are_released_when_renderer_raises(tmp_path):
    source = _source(tmp_path)
    original = source.read_bytes()
    subject = SubjectStub()
    prepared = None
    with pytest.raises(RuntimeError, match="fixture renderer error"):
        with prepared_subject_sources(_plan(remove=True), [source], subject) as (effective, _evidence):
            prepared = effective[0]
            assert prepared.is_file()
            raise RuntimeError("fixture renderer error")
    assert prepared is not None and not prepared.exists()
    assert source.read_bytes() == original and subject.releases == 1
    _assert_no_subject_temporaries(tmp_path)


def test_later_source_failure_removes_earlier_temporary_cutout(tmp_path):
    sources = [_source(tmp_path, "first.png"), _source(tmp_path, "second.png")]
    original = [path.read_bytes() for path in sources]

    def segment(path):
        if path == sources[1]:
            raise RuntimeError("second source failed")
        return SegmentationResult("succeeded", "stub", _mask(), True)

    subject = SubjectStub(segment)
    with pytest.raises(RuntimeError, match="second source failed"):
        with prepared_subject_sources(_plan(2, remove=True), sources, subject):
            pytest.fail("partial preparation must not reach rendering")
    assert [path.read_bytes() for path in sources] == original
    assert subject.releases == 1
    _assert_no_subject_temporaries(tmp_path)


def test_source_change_during_inference_is_detected_without_publishing(tmp_path):
    source = _source(tmp_path)

    def externally_changed_fixture(path):
        Image.new("RGB", (64, 64), "purple").save(path)
        return SegmentationResult("succeeded", "stub", _mask(), True)

    subject = SubjectStub(externally_changed_fixture)
    with pytest.raises(ScenePlanError, match="원본 이미지가 변경"):
        with prepared_subject_sources(_plan(remove=True), [source], subject):
            pytest.fail("a cutout derived from another source revision must not publish")
    _assert_no_subject_temporaries(tmp_path)


def test_schema_and_normalization_preserve_explicit_boolean_flags():
    assert SCENE_PLAN_JSON_SCHEMA["properties"]["assets"]["items"]["properties"]["remove_background"] == {"type": "boolean"}
    assert SCENE_EDIT_PATCH_JSON_SCHEMA["properties"]["assets"]["items"]["properties"]["remove_background"] == {"type": "boolean"}
    for flag in (True, False):
        plan = _plan(remove=flag)
        normalized = normalize_scene_plan(plan, asset_count=1)
        assert normalized["assets"][0]["remove_background"] is flag
    assert "remove_background" not in _plan()["assets"][0]


@pytest.mark.parametrize("invalid", ["true", "false", 0, 1, None, [], {}])
def test_normalization_and_patch_reject_invalid_background_flag_types(invalid):
    before = _plan()
    malformed = deepcopy(before)
    malformed["assets"][0]["remove_background"] = invalid
    with pytest.raises(ScenePlanError, match="true/false"):
        normalize_scene_plan(malformed, asset_count=1)
    with pytest.raises(ScenePlanError, match="true/false"):
        apply_scene_edit_patch(before, {"canvas": {"background": "#abcdef"},
                                       "assets": [{"index": 0, "remove_background": invalid}]},
                               asset_count=1, visible_copy="")


@pytest.mark.parametrize("before_flag,invalid", [(True, 1), (False, 0)])
def test_boolean_equality_does_not_allow_numeric_flags_through_patch_validation(before_flag, invalid):
    before = _plan(remove=before_flag)
    with pytest.raises(ScenePlanError, match="true/false"):
        apply_scene_edit_patch(before, {"canvas": {"background": "#abcdef"},
                                       "assets": [{"index": 0, "remove_background": invalid}]},
                               asset_count=1, visible_copy="")


def test_remove_restore_patch_round_trip_preserves_other_visual_fields():
    before = _plan(text=True)
    removed, copy, fields = apply_scene_edit_patch(
        before, {"assets": [{"index": 0, "remove_background": True}]},
        asset_count=1, visible_copy="테스트",
    )
    assert copy == "테스트" and fields == ["assets[0].remove_background"]
    assert before["assets"][0].get("remove_background") is None
    restored, _copy, fields = apply_scene_edit_patch(
        removed, {"assets": [{"index": 0, "remove_background": False}]},
        asset_count=1, visible_copy="테스트",
    )
    assert restored["assets"][0]["remove_background"] is False
    assert {key: value for key, value in restored["assets"][0].items() if key != "remove_background"} == before["assets"][0]
    assert restored["texts"] == before["texts"]
    assert fields == ["assets[0].remove_background"]


@pytest.mark.parametrize("instruction", ["문구 글꼴을 '궁서'로 바꿔줘", "스티커를 원형으로 만들어줘", "사진 크기를 조금 줄여줘"])
@pytest.mark.parametrize("before_flag", [None, False, True])
def test_unrelated_edits_cannot_add_drop_or_reverse_background_removal(instruction, before_flag):
    before = _plan(remove=before_flag, text=True)
    candidate, _fields = enforce_explicit_user_constraints(before, instruction)
    candidate["assets"][0]["remove_background"] = before_flag is not True
    preserved, _scopes = preserve_unrequested_scene_fields(before, candidate, instruction)
    assert preserved["assets"][0].get("remove_background") is before_flag


@pytest.mark.parametrize("instruction,expected", [
    ("사진의 배경을 제거해줘", True),
    ("배경을 지워줘", True),
    ("인물만 추출해줘", True),
    ("사진 누끼를 따줘", True),
    ("사진 배경을 투명하게 해줘", True),
    ("사진 배경 제거를 취소해줘", False),
    ("사진 배경을 원래대로 되돌려줘", False),
    ("문구 배경을 제거해줘", None),
    ("캔버스 배경을 투명하게 해줘", None),
    ("사진은 유지하고 문구 배경을 제거해줘", None),
    ("사진 위 문구 배경을 투명하게 해줘", None),
    ("사진 배경을 제거하지 말고 그대로 둬", None),
    ("사진 배경을 투명하게 하지 마", None),
    ("사진 배경을 제거할 필요 없어", None),
    ("사진 배경을 복원하지 마", None),
    ("누끼 없이 원본 그대로 사용해줘", None),
])
def test_background_intent_is_targeted_and_negation_aware(instruction, expected):
    assert subject_background_action(instruction) is expected
    route = route_mockup_request(instruction, segmentation_ready=True)
    assert (route.subject_processing != "none") is (expected is True)


def test_multiple_unselected_photos_require_target_clarification():
    before = _plan(2)
    with pytest.raises(ScenePlanError, match="사진 번호|어느 사진"):
        enforce_subject_background_constraints(before, "배경을 제거해줘")
    assert all("remove_background" not in asset for asset in before["assets"])


@pytest.mark.parametrize("instruction,selected", [
    ("1번 사진 배경을 제거해줘", {0}),
    ("두 번째 사진 배경을 제거해줘", {1}),
    ("모든 사진 배경을 제거해줘", {0, 1}),
])
def test_explicit_photo_selection_applies_without_mutating_input(instruction, selected):
    before = _plan(2)
    after, _fields = enforce_subject_background_constraints(before, instruction)
    assert {asset["index"] for asset in after["assets"] if asset.get("remove_background")} == selected
    assert all("remove_background" not in asset for asset in before["assets"])


def test_invalid_explicit_photo_selection_is_not_silently_retargeted():
    with pytest.raises(ScenePlanError, match="범위"):
        enforce_subject_background_constraints(_plan(2), "3번 사진 배경을 제거해줘")


def test_mixed_per_photo_actions_are_not_replaced_by_the_last_clause():
    before = _plan(2, remove=False)
    before["assets"][1]["remove_background"] = True
    after, _fields = enforce_subject_background_constraints(
        before, "첫 번째 사진 배경을 제거하고 두 번째 사진 배경은 복원해줘",
    )
    assert [asset["remove_background"] for asset in after["assets"]] == [True, False]


def test_explicit_single_photo_edit_blocks_other_photo_removal_changes():
    before = _plan(2, remove=False)
    candidate = deepcopy(before)
    for asset in candidate["assets"]:
        asset["remove_background"] = True
    after, _scopes = preserve_unrequested_scene_fields(
        before, candidate, "첫 번째 사진만 배경을 제거해줘",
    )
    assert [asset["remove_background"] for asset in after["assets"]] == [True, False]


@pytest.mark.parametrize("engine", ["qt", "pillow"])
def test_real_renderers_preserve_subject_alpha_without_auto_cropping(tmp_path, engine):
    source = _source(tmp_path)
    plan = _plan(remove=True, transparent=True)
    subject = SubjectStub(lambda _path: SegmentationResult("succeeded", "stub", _mask(foreground=128), True))
    with prepared_subject_sources(plan, [source], subject) as (effective, _evidence):
        if engine == "qt":
            pytest.importorskip("PyQt6.QtSvg")
            svg = layer_graph_to_svg(scene_plan_to_layer_graph(plan), effective, 128, 128)
            rendered = render_svg_with_qt(svg, 128, 128)
        else:
            runtime = object.__new__(MockupDesignRuntime)
            rendered = runtime._render_scene_plan(plan, effective, long_edge=900)
        width, height = rendered.size
        assert rendered.getpixel((width // 8, height // 2))[3] == 0
        center = rendered.getpixel((width // 2, height // 2))
        assert abs(center[3] - 128) <= 1  # Never alpha-squared to approximately 64.
        assert all(abs(center[index] - SUBJECT[index]) <= 2 for index in range(3))
    _assert_no_subject_temporaries(tmp_path)


@pytest.mark.parametrize("engine", ["qt", "pillow-fallback", "pillow-generated"])
def test_render_pipeline_uses_cutouts_but_persists_only_original_sources(tmp_path, monkeypatch, engine):
    if engine == "qt":
        pytest.importorskip("PyQt6.QtSvg")
    source = _source(tmp_path)
    original = source.read_bytes()
    subject = SubjectStub()
    runtime = _runtime(monkeypatch, subject)
    if engine == "pillow-fallback":
        def fail_qt(*_args):
            raise RuntimeError("fixture Qt unavailable")
        monkeypatch.setattr("core.mockup_design.render_svg_with_qt", fail_qt)
    result = runtime.render(
        "synthetic-profile", [source], instruction="사진 배경을 제거해줘",
        scene_plan=_plan(), output_dir=tmp_path / "output",
        backend="generative" if engine == "pillow-generated" else "auto",
    )
    with Image.open(result["output"]) as rendered:
        assert rendered.getpixel((10, 10)) == ((90, 20, 100) if engine == "pillow-generated" else (255, 255, 255))
        assert rendered.getpixel((800, 800)) == SUBJECT
    assert source.read_bytes() == original
    assert result["production_sources"] == [str(source)]
    assert result["production_inputs"] == [hashlib.sha256(original).hexdigest()]
    assert result["scene_plan"]["assets"][0]["remove_background"] is True
    assert result["subject_processing_evidence"][0]["backend"] == "synthetic-stub"
    assert result["subject_processing_evidence"][0]["changed"] is True
    assert_scene_document(result["scene_plan"], expected_digest=result["scene_digest"])
    assert "anis_mockup_subject_" not in json.dumps(result)
    if engine == "qt":
        svg = Path(result["editable_svg"]).read_text(encoding="utf-8")
        assert "data:image/png;base64," in svg and "anis_mockup_subject_" not in svg
        # The exported SVG must still be renderable after prepared sources vanish.
        if engine == "qt":
            reopened = render_svg_with_qt(svg, 1600, 1600)
            assert reopened.getpixel((10, 10))[:3] == (255, 255, 255)
            assert reopened.getpixel((800, 800))[:3] == SUBJECT
    assert subject.calls == [source]
    _assert_no_subject_temporaries(tmp_path)


def test_render_failure_cannot_publish_a_preview_or_modify_source(tmp_path, monkeypatch):
    source = _source(tmp_path)
    original = source.read_bytes()
    subject = SubjectStub(lambda _path: SegmentationResult("failed", "stub", error="fixture unavailable"))
    runtime = _runtime(monkeypatch, subject)
    destination = tmp_path / "output"
    with pytest.raises(ScenePlanError, match="fixture unavailable"):
        runtime.render("synthetic-profile", [source], instruction="사진 배경을 제거해줘",
                       scene_plan=_plan(), output_dir=destination)
    assert not destination.exists() or not list(destination.iterdir())
    assert source.read_bytes() == original
    _assert_no_subject_temporaries(tmp_path)


def test_source_change_during_render_does_not_publish_mismatched_provenance(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtSvg")
    source = _source(tmp_path)
    subject = SubjectStub()
    runtime = _runtime(monkeypatch, subject)
    original_renderer = render_svg_with_qt

    def changed_after_svg_embedding(svg, width, height):
        rendered = original_renderer(svg, width, height)
        # This is a competing edit to a test-owned fixture, not a production file.
        # The bitmap now describes the old source while a late re-hash sees green.
        Image.new("RGB", (64, 64), "green").save(source)
        return rendered

    monkeypatch.setattr("core.mockup_design.render_svg_with_qt", changed_after_svg_embedding)
    destination = tmp_path / "output"
    with pytest.raises(ScenePlanError, match="원본.*변경|source.*chang"):
        runtime.render("synthetic-profile", [source], instruction="사진 배경을 제거해줘",
                       scene_plan=_plan(), output_dir=destination)
    assert not destination.exists() or not list(destination.iterdir())
    _assert_no_subject_temporaries(tmp_path)


def test_unrequested_model_alpha_flag_is_blocked_before_the_renderer_runs(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtSvg")
    source = _source(tmp_path)
    subject = SubjectStub()
    runtime = _runtime(monkeypatch, subject)
    before = _plan(remove=False, text=True)
    candidate = deepcopy(before)
    candidate["assets"][0]["remove_background"] = True
    candidate["texts"][0]["font_family"] = "궁서"
    result = runtime.render(
        "synthetic-profile", [source], instruction="테스트 시안", visible_copy="테스트",
        scene_plan=candidate, edit_baseline=before,
        edit_instruction="문구 글꼴을 '궁서'로 바꿔줘", output_dir=tmp_path / "output",
    )
    assert subject.calls == []
    assert result["subject_processing_evidence"] == []
    assert result["scene_plan"]["assets"][0]["remove_background"] is False
    with Image.open(result["output"]) as image:
        assert image.getpixel((10, 10)) == BACKGROUND


def test_repeating_verified_subject_removal_is_idempotent_not_a_planner_error(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtSvg")
    source = _source(tmp_path)
    subject = SubjectStub()
    runtime = _runtime(monkeypatch, subject)
    first = runtime.render(
        "synthetic-profile", [source], instruction="사진 배경을 제거해줘",
        scene_plan=_plan(), preview_only=True,
    )
    original_preview = Path(first["output"]).read_bytes()
    repeated = runtime.edit_preview(first, "사진 배경을 제거해줘")
    assert repeated["already_satisfied"] is True
    assert repeated["output"] == first["output"]
    assert repeated["revision"] == first["revision"]
    assert Path(first["output"]).read_bytes() == original_preview
    assert repeated["scene_plan"]["assets"][0]["remove_background"] is True
    _assert_no_subject_temporaries(tmp_path)


def test_font_circle_and_restore_edits_keep_current_preview_state_and_original_source(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtSvg")
    source = _source(tmp_path)
    original = source.read_bytes()
    subject = SubjectStub()
    runtime = _runtime(monkeypatch, subject)
    first = runtime.render(
        "synthetic-profile", [source], instruction="사진 배경을 제거해줘", visible_copy="테스트",
        scene_plan=_plan(text=True), preview_only=True,
    )
    first_snapshot = deepcopy(first)
    font = runtime.edit_preview(first, "문구 글꼴을 '궁서'로 바꿔줘")
    assert font["scene_plan"]["assets"][0]["remove_background"] is True
    assert font["scene_plan"]["texts"][0]["font_family"] == "궁서"
    circle = runtime.edit_preview(font, "스티커를 원형으로 만들어줘")
    assert circle["scene_plan"]["assets"][0]["remove_background"] is True
    assert circle["scene_plan"]["assets"][0]["shape"] == "ellipse"
    assert circle["scene_plan"]["texts"][0]["font_family"] == "궁서"
    calls_before_restore = len(subject.calls)
    restored = runtime.edit_preview(circle, "사진 배경을 복원해줘")
    assert restored["scene_plan"]["assets"][0]["remove_background"] is False
    assert restored["scene_plan"]["assets"][0]["shape"] == "ellipse"
    assert restored["scene_plan"]["texts"][0]["font_family"] == "궁서"
    assert len(subject.calls) == calls_before_restore
    assert restored["production_sources"] == first["production_sources"] == [str(source)]
    assert source.read_bytes() == original and first == first_snapshot
    for metadata in (first, font, circle):
        with Image.open(metadata["output"]) as image:
            colors = image.convert("RGB").getcolors(maxcolors=image.width * image.height)
            assert not any(color == BACKGROUND for _count, color in colors)
    with Image.open(restored["output"]) as image:
        colors = image.convert("RGB").getcolors(maxcolors=image.width * image.height)
        assert any(color == BACKGROUND for _count, color in colors)
    assert restored["revision"] == first["revision"] + 3
    assert restored["subject_processing_evidence"] == []
    # Simulate selecting the older font preview through Undo.  The edit must use
    # that document, not secretly retain the later circle/restore branch.
    undo_branch = runtime.edit_preview(font, "사진 배경을 복원해줘")
    assert undo_branch["scene_plan"]["assets"][0]["shape"] == "rectangle"
    assert undo_branch["scene_plan"]["assets"][0]["remove_background"] is False
    assert undo_branch["scene_plan"]["texts"][0]["font_family"] == "궁서"
    assert undo_branch["revision"] == font["revision"] + 1
    _assert_no_subject_temporaries(tmp_path)
