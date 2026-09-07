"""Mockup capability/dispatch contracts; all generation backends are fakes.

The real PluginRegistry and MockupDesignRuntime preparation/status delegation
are exercised. No model is downloaded, loaded, warmed up or remotely called.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from core.mockup_design import MockupDesignRuntime
from core.plugin import PluginRegistry
from core.tool_result import ToolRunStatus
from plugins.mockup_design import MockupDesignPlugin


class BackendProbe:
    def __init__(self, name, ready):
        self.name, self.ready = name, ready
        self.calls = []

    def status(self):
        self.calls.append("status")
        return {"ready": self.ready, "model": self.name, "cuda": True, "vram_mb": 8192,
                "inference_verified": True}  # A prepare response must not claim inference.

    def prepare(self, progress=None):
        self.calls.append("prepare")
        if progress:
            progress(f"fake preparing {self.name}")
        return self.status()


class RuntimeProbe:
    generation_status = MockupDesignRuntime.generation_status
    prepare_generation_models = MockupDesignRuntime.prepare_generation_models
    prepare_sdxl_model = MockupDesignRuntime.prepare_sdxl_model

    def __init__(self, tmp_path):
        self.generation_backend = BackendProbe("sd15-probe", True)
        self.sdxl_backend = BackendProbe("sdxl-probe", False)
        self.render_calls = []
        self.output = tmp_path / "mockup.png"

    def render(self, profile_id, production_paths, **options):
        self.render_calls.append((profile_id, deepcopy(production_paths), deepcopy(options)))
        # A file artifact fixture only; this test makes no layout-quality claim.
        self.output.write_bytes(b"\x89PNG\r\n\x1a\nfake-render-result")
        backend = options["backend"]
        return {"output": str(self.output), "generation_backend": backend if backend.startswith("generative")
                else "model_planned_local", "reference_pixels_sent_to_generator": backend == "generative"}


@pytest.fixture
def mockup_registry(tmp_path):
    plugin = MockupDesignPlugin()
    plugin.runtime = RuntimeProbe(tmp_path)
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    yield registry, plugin, plugin.runtime
    registry.shutdown(timeout_seconds=1)


def _evidence(result):
    assert result.evidence
    return result.evidence[0].data


def _render_input(**options):
    return {"profile_id": "profile", "production_paths": ["source.png"],
            "output_dir": "output", **options}


def test_existing_tools_expose_canonical_sdxl_backend_without_new_stub_tools(mockup_registry):
    registry, _plugin, _runtime = mockup_registry
    assert {tool.name for tool in registry.get_all_tools()} == {
        "mockup_learn_style", "mockup_render", "mockup_list_styles", "mockup_delete_style",
        "mockup_generation_status", "mockup_prepare_generation",
    }
    render = registry.get_capability("mockup_render")
    prepare = registry.get_capability("mockup_prepare_generation")
    status = registry.get_capability("mockup_generation_status")
    assert set(render.input_schema["properties"]["backend"]["enum"]) == {"auto", "local", "generative", "generative_sdxl"}
    assert set(prepare.input_schema["properties"]["backend"]["enum"]) == {"generative", "generative_sdxl"}
    assert set(status.input_schema["properties"]["backend"]["enum"]) == {"all", "generative", "generative_sdxl"}
    assert "network_access" in prepare.required_permissions and prepare.side_effect == "change"
    assert status.side_effect == "read" and "network_access" not in status.required_permissions
    assert "SDXL" in render.description and "미지원" in render.description
    assert "인페인팅" in render.description and "FLUX" in render.description
    assert "가중치 학습 없이" in registry.get_capability("mockup_learn_style").description


@pytest.mark.parametrize("backend", ["auto", "local", "generative", "generative_sdxl"])
def test_registry_render_dispatch_preserves_requested_backend_and_other_arguments(mockup_registry, backend):
    registry, _plugin, runtime = mockup_registry
    payload = _render_input(backend=backend, seed=314, instruction="원형 스티커 배경", visible_copy="테스트", basename="sticker")
    result = registry.execute_tool("mockup_render", payload)
    assert result.succeeded, result.error
    assert len(runtime.render_calls) == 1
    profile, paths, options = runtime.render_calls[0]
    assert profile == "profile" and paths == ["source.png"]
    assert options == {"backend": backend, "seed": 314, "instruction": "원형 스티커 배경", "visible_copy": "테스트",
                       "output_dir": "output", "basename": "sticker"}
    evidence = _evidence(result)
    assert evidence["requested_backend"] == backend
    assert evidence["generation_backend"] == (backend if backend.startswith("generative") else "model_planned_local")
    assert evidence["backend_capabilities"]["reference_image_conditioning"] is (backend == "generative")
    assert result.artifacts[0].uri == str(runtime.output)
    assert Path(result.artifacts[0].uri).is_file()
    assert "prepare" not in runtime.generation_backend.calls + runtime.sdxl_backend.calls


def test_omitted_render_backend_preserves_auto_behavior(mockup_registry):
    registry, _plugin, runtime = mockup_registry
    result = registry.execute_tool("mockup_render", _render_input())
    assert result.succeeded
    assert runtime.render_calls[0][2]["backend"] == "auto"
    assert _evidence(result)["backend_capabilities"]["generated_background"] is False


def test_status_all_keeps_sdxl_readiness_separate_from_sd15(mockup_registry):
    registry, _plugin, runtime = mockup_registry
    result = registry.execute_tool("mockup_generation_status", {})
    assert result.succeeded
    status = json.loads(result.raw_output)
    assert status["backend"] == "all" and "ready" not in status
    assert status["backends"]["generative"]["ready"] is True
    assert status["backends"]["generative_sdxl"]["ready"] is False
    assert status["backends"]["generative_sdxl"]["model"] == "sdxl-probe"
    assert all(item["inference_verified"] is False for item in status["backends"].values())
    assert status["unsupported_backends"] == ["flux"]
    assert "prepare" not in runtime.generation_backend.calls + runtime.sdxl_backend.calls


@pytest.mark.parametrize("backend,ready,model", [("generative", True, "sd15-probe"), ("generative_sdxl", False, "sdxl-probe")])
def test_selected_status_reports_the_selected_model_only(mockup_registry, backend, ready, model):
    registry, _plugin, runtime = mockup_registry
    result = registry.execute_tool("mockup_generation_status", {"backend": backend})
    assert result.succeeded
    status = _evidence(result)
    assert status["backend"] == backend and status["model"] == model and status["ready"] is ready
    assert "sdxl" not in status and "backends" not in status
    assert status["inference_verified"] is False
    assert "prepare" not in runtime.generation_backend.calls + runtime.sdxl_backend.calls


def test_sdxl_status_preserves_detailed_readiness_scope(mockup_registry):
    registry, _plugin, runtime = mockup_registry
    original = runtime.generation_status

    def detailed_status():
        status = original()
        status["sdxl"].update({
            "readiness_scope": "managed_local_fp16_snapshot_dependencies_and_cuda",
            "weight_values_verified": False,
            "checksums_verified": False,
            "source_revision_verified": False,
            "package_imports_verified": False,
        })
        return status

    runtime.generation_status = detailed_status
    result = registry.execute_tool("mockup_generation_status", {"backend": "generative_sdxl"})
    assert result.succeeded
    evidence = _evidence(result)
    assert evidence["readiness_scope"] == "managed_local_fp16_snapshot_dependencies_and_cuda"
    assert evidence["weight_values_verified"] is False
    assert evidence["checksums_verified"] is False
    assert evidence["source_revision_verified"] is False
    assert evidence["package_imports_verified"] is False
    assert evidence["inference_verified"] is False


def test_sdxl_capability_does_not_claim_reference_inpainting_flux_or_training(mockup_registry):
    registry, _plugin, _runtime = mockup_registry
    result = registry.execute_tool("mockup_generation_status", {"backend": "generative_sdxl"})
    capabilities = _evidence(result)["capabilities"]
    assert capabilities["generation_mode"] == "text_to_image"
    assert capabilities["generated_background"] is True
    assert capabilities["reference_style_profile"] is True
    assert capabilities["exact_text"] == "separate_scene_layers"
    assert capabilities["reference_image_conditioning"] is False
    assert capabilities["generative_inpainting"] is False
    assert capabilities["controlnet"] is False and capabilities["flux"] is False
    assert capabilities["model_weight_training"] is False
    assert "픽셀" in " ".join(capabilities["limitations"])


@pytest.mark.parametrize("backend", ["generative", "generative_sdxl"])
def test_prepare_dispatches_only_to_selected_real_runtime_delegate(mockup_registry, backend):
    registry, _plugin, runtime = mockup_registry
    runtime.sdxl_backend.ready = True
    result = registry.execute_tool("mockup_prepare_generation", {"backend": backend})
    assert result.succeeded, result.error
    target = runtime.sdxl_backend if backend == "generative_sdxl" else runtime.generation_backend
    other = runtime.generation_backend if backend == "generative_sdxl" else runtime.sdxl_backend
    assert target.calls == ["prepare", "status"]
    assert other.calls == []
    assert _evidence(result)["backend"] == backend
    assert _evidence(result)["inference_verified"] is False
    assert "실제 이미지 추론 성공은 아직 검증하지 않았습니다" in result.raw_output


def test_legacy_prepare_without_backend_still_prepares_sd15_only(mockup_registry):
    registry, _plugin, runtime = mockup_registry
    result = registry.execute_tool("mockup_prepare_generation", {})
    assert result.succeeded
    assert runtime.generation_backend.calls == ["prepare", "status"]
    assert runtime.sdxl_backend.calls == []
    assert _evidence(result)["backend"] == "generative"


def test_sdxl_prepare_failure_cannot_borrow_ready_sd15_status(mockup_registry):
    registry, _plugin, runtime = mockup_registry
    result = registry.execute_tool("mockup_prepare_generation", {"backend": "generative_sdxl"})
    assert result.status == ToolRunStatus.FAILED
    assert "generative_sdxl" in result.error
    assert _evidence(result)["ready"] is False
    assert runtime.sdxl_backend.calls == ["prepare", "status"]
    assert runtime.generation_backend.calls == []


@pytest.mark.parametrize("invalid_ready", ["true", "false", 1, None, [], {}])
@pytest.mark.parametrize("tool", ["mockup_prepare_generation", "mockup_generation_status"])
def test_invalid_ready_truth_is_never_coerced_to_success(mockup_registry, invalid_ready, tool):
    registry, _plugin, runtime = mockup_registry
    runtime.sdxl_backend.ready = invalid_ready
    result = registry.execute_tool(tool, {"backend": "generative_sdxl"})
    assert result.status == ToolRunStatus.FAILED
    assert "유효한 준비 상태" in result.error


@pytest.mark.parametrize("tool,backend", [
    ("mockup_render", "flux"), ("mockup_render", "sdxl"), ("mockup_render", "inpainting"),
    ("mockup_prepare_generation", "flux"), ("mockup_prepare_generation", "auto"),
    ("mockup_prepare_generation", "local"), ("mockup_prepare_generation", "all"),
    ("mockup_generation_status", "flux"),
])
def test_unsupported_backend_rejected_before_runtime_dispatch(mockup_registry, tool, backend):
    registry, plugin, runtime = mockup_registry
    data = _render_input(backend=backend) if tool == "mockup_render" else {"backend": backend}
    result = registry.execute_tool(tool, data)
    assert result.status == ToolRunStatus.FAILED
    assert runtime.render_calls == []
    assert runtime.generation_backend.calls == [] and runtime.sdxl_backend.calls == []
    # Also preserve the same safety boundary for direct legacy plugin calls.
    direct = plugin.execute_tool(tool, data)
    assert direct.status == ToolRunStatus.FAILED
    assert "지원하지 않는" in direct.error
    assert runtime.render_calls == []
    assert runtime.generation_backend.calls == [] and runtime.sdxl_backend.calls == []


def test_unimplemented_inpainting_parameter_does_not_become_a_silent_background_render(mockup_registry):
    registry, _plugin, runtime = mockup_registry
    result = registry.execute_tool("mockup_render", _render_input(backend="generative_sdxl", mask_path="mask.png"))
    assert result.status == ToolRunStatus.FAILED
    assert runtime.render_calls == []


def test_missing_sdxl_status_fails_explicitly_without_substituting_sd15(mockup_registry, monkeypatch):
    registry, _plugin, runtime = mockup_registry
    monkeypatch.setattr(runtime, "generation_status", lambda: {"ready": True, "model": "sd15-only"})
    result = registry.execute_tool("mockup_generation_status", {"backend": "generative_sdxl"})
    assert result.status == ToolRunStatus.FAILED
    assert "generative_sdxl" in result.error
