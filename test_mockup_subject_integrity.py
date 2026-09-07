"""Synthetic-only subject segmentation contracts; no downloads or user photos."""
import builtins
import json
import sys
from types import SimpleNamespace

from PIL import Image, ImageChops, ImageDraw
import pytest

from core.mockup_subject_runtime import SegmentationError, SubjectAnalysisRuntime


def _source(tmp_path, *, mode="RGB", size=(80, 100)):
    path = tmp_path / "synthetic-subject.png"
    source = Image.new(mode, size, "white")
    ImageDraw.Draw(source).rectangle((25, 20, 55, 85), fill="navy")
    source.save(path)
    return path


def test_backend_failures_never_become_an_opaque_success(tmp_path, monkeypatch):
    source = _source(tmp_path)
    models = tmp_path / "model"
    models.mkdir()
    (models / "config.json").write_text("{}", encoding="utf-8")
    runtime = SubjectAnalysisRuntime(models)
    real_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name.split(".", 1)[0] in {"torch", "torchvision", "transformers", "cv2"}:
            raise ImportError("fixture backend unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    with pytest.raises(RuntimeError, match="분리"):
        runtime.segment(source)


def _mask(size=(80, 100)):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rectangle((20, 10, 60, 90), fill=255)
    return mask


def _runtime(tmp_path, monkeypatch, *, ready=True):
    runtime = SubjectAnalysisRuntime(tmp_path / "models")
    monkeypatch.setattr(runtime, "_birefnet_readiness", lambda: {
        "ready": ready, "scope": "fixture", "issues": [] if ready else ["fixture model absent"],
    })
    releases = []
    monkeypatch.setattr(runtime, "release", lambda: releases.append(True))
    return runtime, releases


def _fail(_image):
    raise RuntimeError("fixture inference failure")


def test_success_has_measured_alpha_change_and_does_not_run_fallback(tmp_path, monkeypatch):
    source = _source(tmp_path)
    runtime, releases = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_segment_birefnet", lambda image: _mask(image.size))
    fallback_calls = []
    monkeypatch.setattr(runtime, "_segment_grabcut", lambda image: fallback_calls.append(True))
    result = runtime.segment_with_evidence(source)
    assert result.success is True
    assert result.status == "succeeded"
    assert result.backend == "birefnet"
    assert result.mask.mode == "L"
    assert result.mask.size == (80, 100)
    assert result.quality["changed_fraction"] > .1
    assert result.quality["validation_scope"] == "alpha_structure_only"
    assert fallback_calls == releases == []
    assert json.loads(json.dumps(result.evidence()))["success"] is True


@pytest.mark.parametrize("ready", [True, False])
def test_fallback_keeps_the_primary_failure_or_unavailability_visible(tmp_path, monkeypatch, ready):
    source = _source(tmp_path)
    runtime, releases = _runtime(tmp_path, monkeypatch, ready=ready)
    monkeypatch.setattr(runtime, "_segment_birefnet", _fail)
    monkeypatch.setattr(runtime, "_segment_grabcut", lambda image: _mask(image.size))
    mask = runtime.segment(source)
    evidence = mask.info["segmentation"]
    assert evidence["success"] is True
    assert evidence["status"] == "fallback"
    assert evidence["fallback"] is True
    assert evidence["backend"] == "opencv-grabcut"
    assert evidence["attempts"][0]["status"] == ("failed" if ready else "unavailable")
    assert "fixture" in evidence["attempts"][0]["error"]
    assert evidence["attempts"][1]["status"] == "succeeded"
    assert releases == ([True] if ready else [])


def test_failure_has_no_fake_mask_and_propagates_backend_evidence(tmp_path, monkeypatch):
    source = _source(tmp_path)
    runtime, releases = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_segment_birefnet", _fail)
    monkeypatch.setattr(runtime, "_segment_grabcut", _fail)
    result = runtime.segment_with_evidence(source)
    assert result.status == "failed"
    assert result.success is False
    assert result.mask is None
    assert result.changed is False
    assert [item["status"] for item in result.attempts] == ["failed", "failed"]
    with pytest.raises(SegmentationError) as caught:
        runtime.segment(source)
    assert caught.value.evidence["mask_size"] is None
    assert caught.value.evidence["success"] is False
    assert "fixture inference failure" in str(caught.value)
    assert len(releases) == 2


@pytest.mark.parametrize("mask_factory", [
    lambda size: Image.new("L", size, 0),
    lambda size: Image.new("L", size, 1),
    lambda size: Image.new("L", size, 128),
    lambda size: Image.new("L", size, 254),
    lambda size: Image.new("L", size, 255),
    lambda size: Image.new("RGB", size, "white"),
    lambda size: Image.new("L", (size[0] - 1, size[1]), 128),
    lambda size: None,
])
def test_invalid_or_trivial_masks_are_rejected_for_every_backend(tmp_path, monkeypatch, mask_factory):
    source = _source(tmp_path)
    runtime, _ = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_segment_birefnet", lambda image: mask_factory(image.size))
    monkeypatch.setattr(runtime, "_segment_grabcut", lambda image: mask_factory(image.size))
    result = runtime.segment_with_evidence(source)
    assert result.success is False
    assert result.mask is None
    assert all(item["status"] == "rejected" for item in result.attempts)


@pytest.mark.parametrize("base, pixel", [(0, 255), (255, 0)])
def test_a_single_changed_pixel_does_not_qualify_as_segmentation(tmp_path, monkeypatch, base, pixel):
    source = _source(tmp_path)
    runtime, _ = _runtime(tmp_path, monkeypatch)

    def speck(image):
        mask = Image.new("L", image.size, base)
        mask.putpixel((40, 50), pixel)
        return mask

    monkeypatch.setattr(runtime, "_segment_birefnet", speck)
    monkeypatch.setattr(runtime, "_segment_grabcut", speck)
    assert runtime.segment_with_evidence(source).success is False


def test_rejected_model_output_can_fall_back_but_is_not_reported_as_model_success(tmp_path, monkeypatch):
    source = _source(tmp_path)
    runtime, releases = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_segment_birefnet", lambda image: Image.new("L", image.size, 255))
    monkeypatch.setattr(runtime, "_segment_grabcut", lambda image: _mask(image.size))
    result = runtime.segment_with_evidence(source)
    assert result.status == "fallback"
    assert result.attempts[0]["status"] == "rejected"
    assert result.attempts[0]["code"] == "no_background_removed"
    assert releases == [True]


def test_existing_identical_alpha_is_an_explicit_no_change_not_success(tmp_path, monkeypatch):
    source = _source(tmp_path, mode="RGBA")
    original_mask = _mask()
    with Image.open(source) as original:
        rgba = original.convert("RGBA")
    rgba.putalpha(original_mask)
    rgba.save(source)
    runtime, _ = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_segment_birefnet", lambda image: original_mask.copy())
    monkeypatch.setattr(runtime, "_segment_grabcut", lambda image: original_mask.copy())
    result = runtime.segment_with_evidence(source)
    assert result.status == "unchanged"
    assert result.success is False
    assert result.mask is None
    assert all(item["code"] == "unchanged" for item in result.attempts)
    with pytest.raises(SegmentationError):
        runtime.segment(source)


def test_segmentation_preserves_existing_transparency_and_soft_alpha(tmp_path, monkeypatch):
    source = _source(tmp_path, mode="RGBA")
    existing = Image.new("L", (80, 100), 255)
    ImageDraw.Draw(existing).rectangle((0, 0, 30, 99), fill=0)
    existing.putpixel((40, 50), 128)
    with Image.open(source) as original:
        rgba = original.convert("RGBA")
    rgba.putalpha(existing)
    rgba.save(source)
    runtime, _ = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_segment_birefnet", lambda image: _mask(image.size))
    result = runtime.segment_with_evidence(source)
    assert result.success is True
    assert result.mask.getpixel((25, 50)) == 0
    assert result.mask.getpixel((40, 50)) == 128
    assert ImageChops.subtract(result.mask, existing).getbbox() is None


def test_disjoint_mask_and_input_alpha_must_not_erase_the_subject(tmp_path, monkeypatch):
    source = _source(tmp_path, mode="RGBA")
    existing = Image.new("L", (80, 100), 0)
    ImageDraw.Draw(existing).rectangle((0, 0, 15, 99), fill=255)
    with Image.open(source) as original:
        rgba = original.convert("RGBA")
    rgba.putalpha(existing)
    rgba.save(source)
    runtime, _ = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_segment_birefnet", lambda image: _mask(image.size))
    monkeypatch.setattr(runtime, "_segment_grabcut", lambda image: _mask(image.size))
    result = runtime.segment_with_evidence(source)
    assert result.success is False
    assert all(item["code"] == "empty_foreground" for item in result.attempts)


def test_returned_evidence_cannot_mutate_the_runtime_last_result(tmp_path, monkeypatch):
    source = _source(tmp_path)
    runtime, _ = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_segment_birefnet", lambda image: _mask(image.size))
    mask = runtime.segment(source)
    mask.info["segmentation"]["status"] = "failed"
    mask.info["segmentation"]["attempts"].clear()
    snapshot = runtime.status()["last_segmentation"]
    assert snapshot["status"] == "succeeded"
    assert snapshot["attempts"]
    snapshot["attempts"].clear()
    assert runtime.status()["last_segmentation"]["attempts"]


@pytest.mark.parametrize("image_case", ["missing", "corrupt"])
def test_invalid_source_produces_no_mask_and_never_runs_models(tmp_path, monkeypatch, image_case):
    source = tmp_path / "bad.png"
    if image_case == "corrupt":
        source.write_bytes(b"not an image")
    runtime, _ = _runtime(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(runtime, "_segment_birefnet", lambda image: calls.append(True))
    result = runtime.segment_with_evidence(source)
    assert result.status == "failed"
    assert result.mask is None
    assert result.evidence()["success"] is False
    assert calls == []


def _model_files(tmp_path, monkeypatch):
    directory = tmp_path / "fixture-model"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({
        "auto_map": {"AutoConfig": "fixture_config.Config", "AutoModelForImageSegmentation": "fixture_model.Model"},
    }), encoding="utf-8")
    # These are only readiness fixtures, never imported or loaded.
    (directory / "fixture_config.py").write_text("# fixture", encoding="utf-8")
    (directory / "fixture_model.py").write_text("# fixture", encoding="utf-8")
    (directory / "model.safetensors").write_bytes(b"fixture metadata check only")
    runtime = SubjectAnalysisRuntime(directory)
    monkeypatch.setattr(runtime, "_dependency_available", lambda name: True)
    return runtime, directory


def test_config_file_alone_is_not_a_prepared_segmentation_model(tmp_path, monkeypatch):
    directory = tmp_path / "fixture-model"
    directory.mkdir()
    (directory / "config.json").write_text("{}", encoding="utf-8")
    runtime = SubjectAnalysisRuntime(directory)
    monkeypatch.setattr(runtime, "_dependency_available", lambda name: True)
    status = runtime.status()
    assert status["birefnet_ready"] is False
    assert status["backend"] == "opencv-grabcut"
    assert status["birefnet_readiness"]["issues"]


@pytest.mark.parametrize("broken", ["config", "weights", "empty_weights", "model_code", "config_code", "dependency"])
def test_incomplete_installation_is_explained_without_loading_any_model(tmp_path, monkeypatch, broken):
    runtime, directory = _model_files(tmp_path, monkeypatch)
    if broken == "config":
        (directory / "config.json").write_text("{", encoding="utf-8")
    elif broken == "weights":
        (directory / "model.safetensors").unlink()
    elif broken == "empty_weights":
        (directory / "model.safetensors").write_bytes(b"")
    elif broken == "model_code":
        (directory / "fixture_model.py").unlink()
    elif broken == "config_code":
        (directory / "fixture_config.py").unlink()
    else:
        monkeypatch.setattr(runtime, "_dependency_available", lambda name: name != "torchvision")
    assert runtime.status()["birefnet_ready"] is False
    assert runtime.status()["birefnet_readiness"]["issues"]


def test_local_prerequisites_are_not_claimed_to_be_loaded_or_quality_validated(tmp_path, monkeypatch):
    runtime, _ = _model_files(tmp_path, monkeypatch)
    status = runtime.status()
    assert status["birefnet_ready"] is True
    assert status["birefnet_readiness"]["scope"] == "local_prerequisites_only"
    assert status["last_segmentation"] is None
    assert runtime._model is None


@pytest.mark.parametrize("missing_shard", [True, False])
def test_every_weight_shard_must_be_local_and_nonempty(tmp_path, monkeypatch, missing_shard):
    runtime, directory = _model_files(tmp_path, monkeypatch)
    (directory / "model.safetensors").unlink()
    (directory / "model.safetensors.index.json").write_text(json.dumps({
        "weight_map": {"layer.0": "model-1.safetensors", "layer.1": "model-2.safetensors"},
    }), encoding="utf-8")
    (directory / "model-1.safetensors").write_bytes(b"fixture shard")
    if not missing_shard:
        (directory / "model-2.safetensors").write_bytes(b"fixture shard")
    assert runtime.status()["birefnet_ready"] is not missing_shard


def test_readiness_rejects_remote_code_reference_and_path_escape(tmp_path, monkeypatch):
    runtime, directory = _model_files(tmp_path, monkeypatch)
    (directory / "config.json").write_text(json.dumps({
        "auto_map": {"AutoModelForImageSegmentation": "remote/repository--fixture_model.Model"},
    }), encoding="utf-8")
    assert runtime.status()["birefnet_ready"] is False
    (directory / "config.json").write_text(json.dumps({"model_type": "fixture"}), encoding="utf-8")
    (directory / "model.safetensors").unlink()
    (tmp_path / "outside.safetensors").write_bytes(b"outside fixture")
    (directory / "model.safetensors.index.json").write_text(json.dumps({
        "weight_map": {"layer.0": "../outside.safetensors"},
    }), encoding="utf-8")
    assert runtime.status()["birefnet_ready"] is False


@pytest.mark.parametrize("size", [(1, 1), (2, 20), (20, 2)])
def test_grabcut_rejects_tiny_images_without_constructing_an_invalid_rectangle(monkeypatch, size):
    calls = []
    fake_cv2 = SimpleNamespace(grabCut=lambda *args: calls.append(args))
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)
    with pytest.raises(ValueError, match="너무 작"):
        SubjectAnalysisRuntime._segment_grabcut(Image.new("RGB", size, "white"))
    assert calls == []


def test_grabcut_keeps_rgb_channel_order_and_builds_a_bounded_rectangle(monkeypatch):
    calls = []

    def convert(pixels, code):
        assert code == "rgb-to-bgr"
        assert tuple(pixels[0, 0]) == (255, 0, 0)
        return pixels[:, :, ::-1]

    def run(pixels, labels, rect, bg, fg, iterations, mode):
        calls.append(rect)
        assert tuple(pixels[0, 0]) == (0, 0, 255)
        x, y, width, height = rect
        assert x > 0 and y > 0
        assert x + width < pixels.shape[1] and y + height < pixels.shape[0]
        labels[10:90, 20:60] = 3

    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(
        cvtColor=convert, COLOR_RGB2BGR="rgb-to-bgr", grabCut=run, GC_INIT_WITH_RECT=0,
    ))
    mask = SubjectAnalysisRuntime._segment_grabcut(Image.new("RGB", (80, 100), "red"))
    assert mask.mode == "L"
    assert mask.size == (80, 100)
    assert mask.getextrema() == (0, 255)
    assert len(calls) == 1


def test_low_contrast_soft_masks_do_not_fake_foreground_background_separation(tmp_path, monkeypatch):
    source = _source(tmp_path)
    runtime, _ = _runtime(tmp_path, monkeypatch)

    def low_contrast(image):
        mask = Image.new("L", image.size, 16)
        ImageDraw.Draw(mask).rectangle((20, 10, 60, 90), fill=40)
        return mask

    monkeypatch.setattr(runtime, "_segment_birefnet", low_contrast)
    monkeypatch.setattr(runtime, "_segment_grabcut", low_contrast)
    result = runtime.segment_with_evidence(source)
    assert result.success is False
    assert all(item["code"] == "flat_mask" for item in result.attempts)


def test_cleanup_failure_is_reported_without_hiding_original_backend_failure(tmp_path, monkeypatch):
    source = _source(tmp_path)
    runtime, _ = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "_segment_birefnet", _fail)
    monkeypatch.setattr(runtime, "_segment_grabcut", lambda image: _mask(image.size))

    def fail_release():
        raise RuntimeError("fixture release failure")

    monkeypatch.setattr(runtime, "release", fail_release)
    result = runtime.segment_with_evidence(source)
    assert result.status == "fallback"
    assert "fixture inference failure" in result.attempts[0]["error"]
    assert "fixture release failure" in result.attempts[0]["release_error"]
