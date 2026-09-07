"""Offscreen UI contracts for preview snapshots and out-of-order completions."""
from copy import deepcopy
import hashlib
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageChops, ImageDraw
from PyQt6.QtWidgets import QApplication
import pytest

import ui.specialist_workspaces as workspaces


class RuntimeFixture:
    """A deterministic, file-local backend with the preview metadata contract."""
    def __init__(self, root):
        self.root = root
        self.sequence = 0
        self.transform_calls = []
        self.adjust_calls = []
        self.edit_calls = []
        self.render_calls = []
        self.save_calls = []
        self.fail_edits = set()
        self.fail_renders = set()
        self.fail_adjustments = False
        self.prepare_calls = []
        self.fail_preparations = set()
        self.model_status = {
            "ready": False, "base_ready": False, "adapter_ready": False, "cuda": False,
            "sdxl": {"ready": False, "model": "fixture/sdxl", "cuda": True, "vram_mb": 8192},
        }

    def list_profiles(self):
        return []

    def generation_status(self):
        return deepcopy(self.model_status)

    def _prepare(self, backend, progress):
        self.prepare_calls.append(backend)
        if progress:
            progress("fixture preparation")
        if backend in self.fail_preparations:
            raise ValueError(f"fixture preparation failure: {backend}")
        if backend == "generative_sdxl":
            status = {"ready": True, "model": "fixture/sdxl", "cuda": True, "vram_mb": 8192,
                      "inference_verified": True}  # Preparation alone is never inference evidence.
            self.model_status["sdxl"] = deepcopy(status)
        else:
            status = {"ready": True, "base_ready": True, "adapter_ready": True,
                      "image_encoder_ready": True, "cuda": True, "vram_mb": 8192,
                      "base_model": "fixture/sd15"}
            self.model_status.update(deepcopy(status))
        return status

    def prepare_generation_models(self, progress=None):
        return self._prepare("generative", progress)

    def prepare_sdxl_model(self, progress=None):
        return self._prepare("generative_sdxl", progress)

    def result(self, label="preview", **updates):
        self.sequence += 1
        path = self.root / f"{label}-{self.sequence}.png"
        Image.new("RGBA", (64, 48), (self.sequence % 200, 80, 160, 128)).save(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result = {
            "output": str(path), "output_sha256": digest, "width": 64, "height": 48,
            "renderer": "fixture-renderer", "preview_only": True,
            "scene_plan": {"texts": [{"content": label}], "document": {"revision": self.sequence}},
            "preview_effects": [], "adjustments": {},
            "adjustment_base": str(path), "adjustment_base_sha256": digest,
            "adjustment_base_effects": [],
        }
        result.update(deepcopy(updates))
        return result

    def transform_preview(self, path, operation, value=1.0, *, metadata=None):
        self.transform_calls.append((path, operation, value, deepcopy(metadata)))
        effects = deepcopy((metadata or {}).get("preview_effects", []))
        effects.append({"operation": operation})
        return self.result("manual", preview_effects=effects, adjustment_base_effects=effects)

    def adjust_preview(self, base, adjustments, *, metadata=None):
        self.adjust_calls.append((base, deepcopy(adjustments), deepcopy(metadata)))
        if self.fail_adjustments:
            raise ValueError("fixture adjustment failure")
        current = metadata or {}
        effects = deepcopy(current.get("adjustment_base_effects", current.get("preview_effects", [])))
        return self.result(
            "adjustment", renderer="pillow-live-adjustment-v2", operation="combined_adjustment",
            adjustment_base=base, adjustment_base_effects=effects,
            adjustment_base_sha256=current.get("adjustment_base_sha256"),
            preview_effects=[*effects, {"operation": "adjust", "values": deepcopy(adjustments)}],
            adjustments=adjustments,
        )

    def edit_preview(self, metadata, instruction, **kwargs):
        self.edit_calls.append((deepcopy(metadata), instruction, deepcopy(kwargs)))
        if instruction in self.fail_edits:
            raise ValueError(f"fixture AI failure: {instruction}")
        effects = deepcopy(metadata.get("preview_effects", []))
        return self.result("ai", preview_effects=effects, adjustment_base_effects=effects)

    def render(self, profile_id, paths, **kwargs):
        self.render_calls.append((profile_id, list(paths), deepcopy(kwargs)))
        if kwargs.get("instruction") in self.fail_renders:
            raise ValueError("fixture render failure")
        return self.result(str(kwargs.get("instruction") or "render"))

    def save_preview(self, path, filename, metadata):
        self.save_calls.append((path, filename, deepcopy(metadata)))
        metadata["scene_plan"]["texts"][0]["content"] = "backend mutation"
        return {"output": filename}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def workspace(tmp_path, monkeypatch, app):
    jobs, warnings = [], []

    class DeferredThread:
        def __init__(self, *, target, args=(), daemon=False):
            self.target, self.args = target, args

        def start(self):
            jobs.append(self)

        def run(self):
            return self.target(*self.args)

    monkeypatch.setattr(workspaces.threading, "Thread", DeferredThread)
    monkeypatch.setattr(workspaces.QMessageBox, "warning", lambda *_args: warnings.append(_args[-1]))
    monkeypatch.setattr(workspaces.QMessageBox, "information", lambda *_args: None)
    runtime = RuntimeFixture(tmp_path)
    window = workspaces.MockupWorkspaceWindow(SimpleNamespace(title="QA mockup"), runtime=runtime)
    yield SimpleNamespace(window=window, runtime=runtime, jobs=jobs, warnings=warnings)
    window.adjustment_timer.stop()
    window.close()
    window.deleteLater()
    app.processEvents()


def _push(workspace, label="base", **metadata):
    result = workspace.runtime.result(label, **metadata)
    workspace.window._push_preview(result)
    return result


def _start_adjustment(workspace, name="brightness", value=70):
    window = workspace.window
    window.adjustment_sliders[name].setValue(value)
    window.adjustment_timer.stop()
    window._apply_live_adjustments()
    return workspace.jobs[-1]


def _start_ai(workspace, instruction="move text"):
    workspace.window.edit_instruction.setText(instruction)
    workspace.window._edit_with_ai()
    return workspace.jobs[-1]


def test_preview_history_and_public_metadata_are_detached_snapshots(workspace):
    original = _push(workspace)
    window = workspace.window
    original["scene_plan"]["texts"][0]["content"] = "mutated caller"
    assert window.preview_history[0]["scene_plan"]["texts"][0]["content"] == "base"
    window.preview_metadata["scene_plan"]["texts"][0]["content"] = "mutated public snapshot"
    assert window.preview_history[0]["scene_plan"]["texts"][0]["content"] == "base"
    window._show_history_preview()
    window.preview_metadata["preview_effects"].append({"operation": "mutated"})
    assert window.preview_history[0]["preview_effects"] == []


def test_manual_edit_passes_current_deep_metadata_and_keeps_prior_history(workspace):
    current = _push(workspace, preview_effects=[{"operation": "flip_horizontal"}])
    workspace.window._manual_edit("rotate_left")
    path, operation, value, metadata = workspace.runtime.transform_calls[-1]
    assert (path, operation, value) == (current["output"], "rotate_left", 1.0)
    assert metadata == current
    assert workspace.window.preview_history[0] == current
    assert workspace.window.preview_metadata["preview_effects"] == [
        {"operation": "flip_horizontal"}, {"operation": "rotate_left"},
    ]


def test_adjustment_sends_shown_metadata_and_distinct_stable_base(workspace):
    base = workspace.runtime.result("unadjusted")
    current = _push(
        workspace, "shown", preview_effects=[{"operation": "adjust", "values": {"contrast": 1.2}}],
        adjustment_base=base["output"], adjustment_base_effects=[],
        adjustment_base_sha256=base["output_sha256"], adjustments={"contrast": 1.2},
    )
    job = _start_adjustment(workspace)
    job.run()
    path, values, metadata = workspace.runtime.adjust_calls[-1]
    assert path == base["output"]
    assert values["brightness"] == 1.4
    assert metadata == current
    assert metadata["output"] != path
    assert metadata["adjustment_base_effects"] == []


def test_new_preview_uses_its_own_adjustments_without_reapplying_old_sliders(workspace):
    _push(workspace)
    workspace.window.adjustment_sliders["brightness"].setValue(73)
    _push(workspace, "new-ai", preview_effects=[{"operation": "adjust", "values": {"brightness": 1.46}}])
    assert workspace.window.adjustment_sliders["brightness"].value() == 50
    assert workspace.window.adjustment_labels["brightness"].text() == "50%"
    assert not workspace.window.adjustment_timer.isActive()
    assert workspace.jobs == []


def test_undo_redo_restore_slider_values_and_do_not_schedule_rendering(workspace):
    first = _push(workspace)
    _start_adjustment(workspace).run()
    adjusted = deepcopy(workspace.window.preview_metadata)
    workspace.window._undo_preview()
    assert workspace.window.preview_metadata == first
    assert workspace.window.adjustment_sliders["brightness"].value() == 50
    assert not workspace.window.adjustment_timer.isActive()
    workspace.window._redo_preview()
    assert workspace.window.preview_metadata == adjusted
    assert workspace.window.adjustment_sliders["brightness"].value() == 70
    assert not workspace.window.adjustment_timer.isActive()


@pytest.mark.parametrize("transition", ["undo", "undo_redo", "new_preview", "manual"])
def test_adjustment_started_from_old_screen_cannot_replace_new_selection(workspace, transition):
    _push(workspace, "first")
    _push(workspace, "second")
    job = _start_adjustment(workspace)
    window = workspace.window
    if transition in {"undo", "undo_redo"}:
        window._undo_preview()
        if transition == "undo_redo":
            window._redo_preview()
    elif transition == "manual":
        window._manual_edit("flip_horizontal")
    else:
        _push(workspace, "new-result")
    expected = deepcopy(window.preview_history), window.preview_index, deepcopy(window.preview_metadata)
    job.run()
    assert (window.preview_history, window.preview_index, window.preview_metadata) == expected


def test_adjustment_completion_without_base_binding_is_rejected(workspace):
    _push(workspace)
    before = deepcopy(workspace.window.preview_history)
    result = workspace.runtime.result("unbound", _serial=workspace.window._adjustment_serial)
    workspace.window._on_adjustment_done(result)
    assert workspace.window.preview_history == before


def test_old_ai_failure_does_not_clear_newer_request_busy_state(workspace):
    _push(workspace)
    workspace.runtime.fail_edits.add("old request")
    old = _start_ai(workspace, "old request")
    new = _start_ai(workspace, "new request")
    old.run()
    assert workspace.window._ai_edit_in_progress is True
    assert not workspace.window.ai_edit_button.isEnabled()
    assert not workspace.window.save_preview_button.isEnabled()
    assert workspace.warnings == []
    new.run()
    assert workspace.window._ai_edit_in_progress is False
    assert workspace.window.preview_metadata["scene_plan"]["texts"][0]["content"] == "ai"


def test_initial_render_completion_is_bound_to_latest_render_request(workspace):
    _push(workspace)
    window = workspace.window
    window.active_profile_id = "fixture"
    window.production_paths = [window.preview_metadata["output"]]
    window.instruction.setPlainText("old-render")
    window._render()
    old = workspace.jobs[-1]
    window.instruction.setPlainText("new-render")
    window._render()
    new = workspace.jobs[-1]
    new.run()
    expected = deepcopy(window.preview_metadata)
    old.run()
    assert window.preview_metadata == expected


def test_live_adjustment_results_are_deep_snapshots_not_backend_owned(workspace):
    _push(workspace)
    job = _start_adjustment(workspace)
    received = []
    workspace.window.adjustment_done.connect(received.append)
    job.run()
    result = received[-1]
    result["scene_plan"]["texts"][0]["content"] = "backend alias"
    assert workspace.window.preview_metadata["scene_plan"]["texts"][0]["content"] != "backend alias"
    workspace.window.preview_metadata["adjustments"]["brightness"] = 0
    assert workspace.window.preview_history[-1]["adjustments"]["brightness"] == 1.4


def test_save_uses_deep_current_history_snapshot(workspace, monkeypatch):
    current = _push(workspace)
    before = deepcopy(current)
    filename = str(workspace.runtime.root / "saved.png")
    workspace.window.output_dir.setText(str(workspace.runtime.root))
    monkeypatch.setattr(workspaces.QFileDialog, "getSaveFileName", lambda *_args: (filename, "PNG"))
    workspace.window._save_preview()
    assert workspace.runtime.save_calls[-1][0] == current["output"]
    assert workspace.window.preview_history[0] == before


def test_closed_preview_discards_late_background_completion(workspace):
    _push(workspace)
    job = _start_adjustment(workspace)
    before = deepcopy(workspace.window.preview_history)
    workspace.window.close()
    job.run()
    assert workspace.window.preview_history == before


@pytest.mark.parametrize("kind", ["ai_edit", "render", "adjustment"])
def test_failure_with_wrong_base_cannot_clear_current_busy_state(workspace, kind):
    _push(workspace)
    window = workspace.window
    setattr(window, f"_{kind}_in_progress", True)
    window._update_preview_busy_controls()
    serial = getattr(window, f"_{kind}_serial")
    window._on_preview_operation_failed({
        "kind": kind, "serial": serial, "base_output": "older-preview.png", "message": "old failure",
    })
    assert getattr(window, f"_{kind}_in_progress") is True
    assert not window.save_preview_button.isEnabled()
    assert workspace.warnings == []


@pytest.mark.parametrize("broken", ["missing", "corrupt", "mutated"])
def test_unloadable_or_changed_result_never_replaces_the_displayed_snapshot(workspace, broken):
    _push(workspace)
    window = workspace.window
    before = deepcopy(window.preview_history), window.preview_index, deepcopy(window.preview_metadata)
    picture = window.preview._source.cacheKey()
    result = workspace.runtime.result("broken")
    from pathlib import Path
    path = Path(result["output"])
    if broken == "missing":
        path.unlink()
    elif broken == "corrupt":
        path.write_bytes(b"not image data")
    else:
        Image.new("RGBA", (64, 48), "red").save(path)
    window._on_render_done(result)
    assert (window.preview_history, window.preview_index, window.preview_metadata) == before
    assert window.preview._source.cacheKey() == picture
    assert workspace.warnings


def test_unreadable_undo_target_keeps_the_current_visible_history(workspace):
    from pathlib import Path
    first = _push(workspace, "first")
    _push(workspace, "second")
    window = workspace.window
    expected = deepcopy(window.preview_metadata)
    Path(first["output"]).unlink()
    window._undo_preview()
    assert window.preview_index == 1
    assert window.preview_metadata == expected
    assert not window.preview._source.isNull()
    assert workspace.warnings


def test_idempotent_reply_cannot_rebind_history_to_an_unshown_image(workspace):
    _push(workspace)
    before = deepcopy(workspace.window.preview_history)
    result = workspace.runtime.result("other", already_satisfied=True)
    workspace.window._on_render_done(result)
    assert workspace.window.preview_history == before
    assert workspace.warnings


def test_idempotent_reply_cannot_rebind_changed_bytes_at_the_same_path(workspace):
    from pathlib import Path
    current = _push(workspace)
    before = deepcopy(workspace.window.preview_history)
    path = Path(current["output"])
    Image.new("RGBA", (64, 48), "red").save(path)
    reply = {**current, "already_satisfied": True,
             "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    workspace.window._on_render_done(reply)
    assert workspace.window.preview_history == before
    assert workspace.window.preview_metadata == current
    assert workspace.warnings


def test_idempotent_reply_keeps_the_visible_pixels_and_does_not_grow_history(workspace):
    current = _push(workspace)
    picture = workspace.window.preview._source.cacheKey()
    reply = {**current, "already_satisfied": True, "edit_instruction": "same request"}
    workspace.window._on_render_done(reply)
    assert len(workspace.window.preview_history) == 1
    assert workspace.window.preview_metadata == reply
    assert workspace.window.preview._source.cacheKey() == picture
    assert workspace.warnings == []


def test_loadable_result_without_display_labels_does_not_crash_ui_completion(workspace):
    metadata = workspace.runtime.result()
    for key in ("width", "height", "renderer"):
        metadata.pop(key)
    workspace.window._on_render_done(metadata)
    assert workspace.window.preview_metadata["output"] == metadata["output"]
    assert not workspace.window.preview._source.isNull()
    assert "64×48" in workspace.window.details.toPlainText()


def test_closing_then_reopening_a_cached_workspace_allows_new_results(workspace, app):
    _push(workspace)
    old = _start_ai(workspace, "before-close")
    workspace.window.close()
    workspace.window.show()
    app.processEvents()
    new = _start_ai(workspace, "after-reopen")
    new.run()
    expected = deepcopy(workspace.window.preview_metadata)
    assert expected["scene_plan"]["texts"][0]["content"] == "ai"
    old.run()
    assert workspace.window.preview_metadata == expected


def test_thread_start_failure_restores_current_adjustment_controls(workspace, monkeypatch):
    _push(workspace)

    class FailingThread:
        def __init__(self, **_kwargs):
            pass
        def start(self):
            raise RuntimeError("worker pool unavailable")

    monkeypatch.setattr(workspaces.threading, "Thread", FailingThread)
    workspace.window.adjustment_sliders["brightness"].setValue(70)
    workspace.window.adjustment_timer.stop()
    workspace.window._apply_live_adjustments()
    assert workspace.window._adjustment_in_progress is False
    assert workspace.window.save_preview_button.isEnabled()
    assert workspace.window.adjustment_sliders["brightness"].value() == 50
    assert workspace.warnings == ["worker pool unavailable"]


def test_consecutive_slider_updates_coalesce_but_navigation_starts_a_new_branch(workspace):
    first = _push(workspace)
    _start_adjustment(workspace, value=60).run()
    _start_adjustment(workspace, value=70).run()
    window = workspace.window
    assert len(window.preview_history) == 2
    assert window.preview_history[0] == first
    second = deepcopy(window.preview_metadata)
    _push(workspace, "future")
    window._undo_preview()
    assert window.preview_metadata == second
    _start_adjustment(workspace, value=80).run()
    assert len(window.preview_history) == 3
    assert not window.redo_button.isEnabled()
    assert window.preview_history[1] == second
    window._undo_preview()
    assert window.preview_metadata == second
    assert window.adjustment_sliders["brightness"].value() == 70


@pytest.mark.parametrize("reverse", [False, True])
def test_rapid_adjustments_only_publish_latest_requested_value(workspace, reverse):
    _push(workspace)
    old = _start_adjustment(workspace, value=60)
    new = _start_adjustment(workspace, value=80)
    for job in ((new, old) if reverse else (old, new)):
        job.run()
    assert len(workspace.window.preview_history) == 2
    assert workspace.window.preview_metadata["adjustments"]["brightness"] == 1.6
    assert workspace.window.adjustment_sliders["brightness"].value() == 80
    assert workspace.warnings == []


def test_current_ai_failure_preserves_history_and_reenables_editing(workspace):
    current = _push(workspace)
    workspace.runtime.fail_edits.add("failure")
    _start_ai(workspace, "failure").run()
    assert workspace.window.preview_metadata == current
    assert workspace.window.ai_edit_button.isEnabled()
    assert workspace.window.save_preview_button.isEnabled()
    assert "이전 미리보기를 그대로 유지" in workspace.window.details.toPlainText()
    assert workspace.warnings == ["fixture AI failure: failure"]


def test_current_adjustment_failure_reverts_unrendered_sliders(workspace):
    current = _push(workspace)
    workspace.runtime.fail_adjustments = True
    _start_adjustment(workspace).run()
    assert workspace.window.preview_metadata == current
    assert workspace.window.adjustment_sliders["brightness"].value() == 50
    assert workspace.window.save_preview_button.isEnabled()
    assert workspace.warnings == ["fixture adjustment failure"]


@pytest.mark.parametrize("kind", ["render", "ai_edit", "adjustment"])
def test_malformed_preview_callback_fails_closed_and_never_leaves_ui_busy(workspace, kind):
    _push(workspace)
    window = workspace.window
    if kind == "adjustment":
        window.adjustment_sliders["brightness"].setValue(70)
    setattr(window, f"_{kind}_in_progress", True)
    before_serial = getattr(window, f"_{kind}_serial")
    window._update_preview_busy_controls()
    assert not window.save_preview_button.isEnabled()

    if kind == "adjustment":
        window._on_adjustment_done(["not", "metadata"])
    else:
        window._on_render_done("not metadata")

    assert getattr(window, f"_{kind}_in_progress") is False
    assert getattr(window, f"_{kind}_serial") == before_serial + 1
    assert window.save_preview_button.isEnabled()
    assert workspace.warnings
    if kind == "adjustment":
        assert window.adjustment_sliders["brightness"].value() == 50


def test_malformed_callback_after_completion_does_not_create_a_new_failure(workspace):
    _push(workspace)
    workspace.window._on_render_done(None)
    workspace.window._on_adjustment_done(None)
    assert workspace.warnings == []


def test_current_render_failure_without_previous_preview_does_not_enable_save(workspace):
    window = workspace.window
    window.active_profile_id = "fixture"
    window.production_paths = [workspace.runtime.result()["output"]]
    window.instruction.setPlainText("failure")
    workspace.runtime.fail_renders.add("failure")
    window._render()
    workspace.jobs[-1].run()
    assert window.preview_history == []
    assert window._render_in_progress is False
    assert not window.save_preview_button.isEnabled()
    assert workspace.warnings == ["fixture render failure"]


def _select_backend(workspace, backend):
    selector = workspace.window.backend_selector
    selector.setCurrentIndex(selector.findData(backend))


@pytest.mark.parametrize("backend", ["auto", "local", "generative", "generative_sdxl"])
def test_model_preparation_uses_the_captured_selected_backend(workspace, backend):
    _select_backend(workspace, backend)
    workspace.window._prepare_models()
    job = workspace.jobs[-1]
    # The worker must not read GUI state after dispatch or download another model.
    _select_backend(workspace, "generative" if backend == "generative_sdxl" else "generative_sdxl")
    job.run()
    expected = "generative_sdxl" if backend == "generative_sdxl" else "generative"
    assert workspace.runtime.prepare_calls == [expected]


def test_switching_backend_displays_its_status_without_starting_downloads(workspace):
    _select_backend(workspace, "generative_sdxl")
    label = workspace.window.model_status.text()
    assert "SDXL" in label and "fixture/sdxl" in label
    assert "SD1.5" not in label and "IP-Adapter" not in label
    assert "CUDA" in label and "미검증" in label
    assert workspace.runtime.prepare_calls == []
    assert workspace.jobs == []


def test_sdxl_preparation_ready_does_not_claim_inference_was_verified(workspace):
    _select_backend(workspace, "generative_sdxl")
    workspace.window._prepare_models()
    workspace.jobs[-1].run()
    label = workspace.window.model_status.text()
    assert "SDXL" in label and "fixture/sdxl" in label
    assert "준비 완료" in label and "실제 생성 미검증" in label
    assert "IP-Adapter" not in label
    assert workspace.window.prepare_models_button.isEnabled()


def test_old_backend_completion_cannot_replace_current_backend_status(workspace):
    _select_backend(workspace, "generative")
    workspace.window._prepare_models()
    old = workspace.jobs[-1]
    _select_backend(workspace, "generative_sdxl")
    workspace.window._prepare_models()
    new = workspace.jobs[-1]
    new.run()
    expected = workspace.window.model_status.text()
    old.run()
    assert workspace.window.model_status.text() == expected
    assert workspace.window.prepare_models_button.isEnabled()
    _select_backend(workspace, "generative")
    assert "fixture/sd15" in workspace.window.model_status.text()


def test_duplicate_prepare_does_not_launch_two_downloads_of_same_model(workspace):
    _select_backend(workspace, "generative_sdxl")
    workspace.window._prepare_models()
    workspace.window._prepare_models()
    assert len(workspace.jobs) == 1
    assert not workspace.window.prepare_models_button.isEnabled()
    workspace.jobs[-1].run()
    assert workspace.runtime.prepare_calls == ["generative_sdxl"]
    assert workspace.window.prepare_models_button.isEnabled()


def test_preparation_failure_for_another_backend_does_not_interrupt_current_preview(workspace):
    current = _push(workspace)
    workspace.runtime.fail_preparations.add("generative_sdxl")
    _select_backend(workspace, "generative_sdxl")
    workspace.window._prepare_models()
    job = workspace.jobs[-1]
    _select_backend(workspace, "generative")
    label = workspace.window.model_status.text()
    job.run()
    assert workspace.window.model_status.text() == label
    assert workspace.window.preview_metadata == current
    assert workspace.warnings == []
    _select_backend(workspace, "generative_sdxl")
    assert "준비 실패" in workspace.window.model_status.text()
    assert workspace.window.prepare_models_button.isEnabled()


def test_sdxl_missing_status_never_reuses_sd15_ready_status(workspace):
    workspace.window._on_model_status({"ready": True, "cuda": True, "base_model": "ready/sd15"})
    workspace.window._on_model_status({"ready": True, "cuda": True, "sdxl": None})
    _select_backend(workspace, "generative_sdxl")
    label = workspace.window.model_status.text()
    assert "SDXL" in label and "정보 없음" in label
    assert "준비 완료" not in label


@pytest.mark.parametrize("status", [
    {"ready": "true", "cuda": True}, {"ready": [True], "cuda": True},
    {"ready": True, "cuda": False}, {"ready": True, "cuda": "true"},
])
def test_sdxl_readiness_is_not_based_on_truthy_or_inconsistent_flags(workspace, status):
    workspace.window._on_model_status({"ready": True, "cuda": True, "sdxl": status})
    _select_backend(workspace, "generative_sdxl")
    assert "준비 완료" not in workspace.window.model_status.text()


def test_actual_manual_backend_keeps_pixels_and_effect_history_in_sync(workspace, monkeypatch, tmp_path):
    import core.mockup_design as design
    from core.mockup_preview_effects import apply_preview_effects, assert_preview_state, with_preview_state

    monkeypatch.setattr(design.tempfile, "gettempdir", lambda: str(tmp_path))
    source = Image.new("RGBA", (36, 24), (30, 70, 90, 0))
    draw = ImageDraw.Draw(source)
    draw.rectangle((3, 2, 13, 17), fill=(110, 40, 160, 128))
    draw.rectangle((14, 7, 30, 21), fill=(80, 120, 35, 255))
    path = tmp_path / "asymmetric.png"
    source.save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    current = with_preview_state({
        "output": str(path), "output_sha256": digest, "width": 36, "height": 24,
        "renderer": "synthetic", "preview_effects": [], "adjustments": {},
        "adjustment_base": str(path), "adjustment_base_sha256": digest, "adjustment_base_effects": [],
    })
    window = workspace.window
    window.runtime = design.MockupDesignRuntime.__new__(design.MockupDesignRuntime)
    window._push_preview(current)

    def assert_pixels():
        metadata = window.preview_metadata
        assert_preview_state(metadata)
        expected = apply_preview_effects(source, metadata["preview_effects"])
        with Image.open(metadata["output"]) as result:
            assert result.size == expected.size
            assert all(channel.getbbox() is None for channel in
                       ImageChops.difference(result.convert("RGBA"), expected.convert("RGBA")).split())
        # The Qt image visible in the window uses those same dimensions and pixels.
        displayed = window.preview._source.toImage()
        assert (displayed.width(), displayed.height()) == expected.size
        for point in ((5, 7), (12, 15), (20, 10)):
            x, y = point
            rgba = expected.getpixel(point)
            actual = displayed.pixelColor(x, y).getRgb()
            assert actual[3] == rgba[3]
            if rgba[3]:
                # Qt's premultiplied-alpha round trip permits one 8-bit step.
                assert max(abs(a - b) for a, b in zip(actual[:3], rgba[:3])) <= 1

    _start_adjustment(workspace, value=30).run()
    assert_pixels()
    adjusted = deepcopy(window.preview_metadata)
    window._manual_edit("rotate_left")
    assert window.adjustment_sliders["brightness"].value() == 50
    assert not window.adjustment_timer.isActive()
    assert_pixels()
    rotated = deepcopy(window.preview_metadata)
    _start_adjustment(workspace, "saturation", 20).run()
    assert_pixels()
    assert [effect["operation"] for effect in window.preview_metadata["preview_effects"]] == [
        "adjust", "rotate_left", "adjust",
    ]
    final = deepcopy(window.preview_metadata)
    window._undo_preview()
    assert window.preview_metadata == rotated
    assert_pixels()
    window._undo_preview()
    assert window.preview_metadata == adjusted
    assert window.adjustment_sliders["brightness"].value() == 30
    assert_pixels()
    window._redo_preview()
    window._redo_preview()
    assert window.preview_metadata == final
    assert_pixels()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert workspace.warnings == []


def test_old_same_backend_prepare_completion_cannot_clear_new_prepare(workspace):
    _select_backend(workspace, "generative_sdxl")
    seen = []
    workspace.window.model_status_done.connect(seen.append)
    workspace.window._prepare_models()
    workspace.jobs[-1].run()
    old = deepcopy(seen[-1])
    workspace.window._prepare_models()
    label = workspace.window.model_status.text()
    workspace.window._on_model_status(old)
    assert workspace.window.model_status.text() == label
    assert not workspace.window.prepare_models_button.isEnabled()
    workspace.jobs[-1].run()
    assert workspace.window.prepare_models_button.isEnabled()


def test_model_status_is_a_detached_snapshot(workspace):
    status = workspace.runtime.generation_status()
    workspace.window._on_model_status(status)
    status["sdxl"]["model"] = "mutated by producer"
    status["sdxl"]["ready"] = True
    _select_backend(workspace, "generative_sdxl")
    label = workspace.window.model_status.text()
    assert "mutated" not in label
    assert "준비 완료" not in label


def test_prepare_worker_start_failure_reenables_only_its_model_control(workspace, monkeypatch):
    class FailingThread:
        def __init__(self, **_kwargs):
            pass
        def start(self):
            raise RuntimeError("worker unavailable")

    _select_backend(workspace, "generative_sdxl")
    monkeypatch.setattr(workspaces.threading, "Thread", FailingThread)
    workspace.window._prepare_models()
    assert workspace.window.prepare_models_button.isEnabled()
    assert "준비 실패" in workspace.window.model_status.text()
    assert "worker unavailable" in workspace.window.details.toPlainText()
    assert workspace.warnings == []


@pytest.mark.parametrize("warning_source", ["backend", "team", "both"])
def test_saved_file_is_not_reported_as_failed_when_post_save_memory_fails(workspace, monkeypatch, warning_source):
    current = _push(workspace)
    filename = str(workspace.runtime.root / "saved.png")
    workspace.window.output_dir.setText(str(workspace.runtime.root))
    monkeypatch.setattr(workspaces.QFileDialog, "getSaveFileName", lambda *_args: (filename, "PNG"))
    result = {"output": filename}
    if warning_source in {"backend", "both"}:
        result["post_save_warnings"] = ["이미지는 저장됐지만 품질 기록 갱신에 실패했습니다: OSError"]
    monkeypatch.setattr(workspace.runtime, "save_preview", lambda *_args: deepcopy(result))

    class FailingTeam:
        def remember_success(self, *_args, **_kwargs):
            raise RuntimeError("fixture memory outage")

    if warning_source in {"team", "both"}:
        workspace.window.team_runtime = FailingTeam()
    workspace.window._save_preview()
    text = workspace.window.details.toPlainText()
    assert f"최종 저장 완료: {filename}" in text
    assert "저장 후 작업 경고" in text
    assert "오류:" not in text
    if warning_source in {"backend", "both"}:
        assert "품질 기록" in text
    if warning_source in {"team", "both"}:
        assert "작업공간 기억" in text
    assert workspace.window.preview_metadata == current
    assert len(workspace.warnings) == 1
    assert "이미지는 정상 저장" in workspace.warnings[0]


def test_output_directory_error_does_not_escape_ui_save_handler(workspace, monkeypatch, tmp_path):
    _push(workspace)
    from pathlib import Path
    # A file at the chosen directory path is an ordinary user/configuration error.
    output_dir = Path(workspace.window.preview_metadata["output"])
    workspace.window.output_dir.setText(str(output_dir))
    workspace.window._save_preview()
    assert workspace.warnings
    assert "최종 저장 완료" not in workspace.window.details.toPlainText()
