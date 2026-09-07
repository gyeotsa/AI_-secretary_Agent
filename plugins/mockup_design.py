"""Reference-driven mockup style learning and local rendering tools."""
from __future__ import annotations

import json

from core.mockup_design import MockupDesignRuntime
from core.plugin import BasePlugin, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult


class MockupDesignPlugin(BasePlugin):
    GENERATIVE_BACKENDS = ("generative", "generative_sdxl")
    RENDER_BACKENDS = ("auto", "local", *GENERATIVE_BACKENDS)
    BACKEND_DESCRIPTION = (
        "auto/local은 AI 설계와 원본 보존 합성, generative는 SD1.5 + IP-Adapter 참고 이미지 조건 배경, "
        "generative_sdxl은 SDXL Turbo 텍스트→배경 생성입니다. SDXL에는 참고 이미지 픽셀이 전달되지 않습니다. "
        "생성형 인페인팅·ControlNet·FLUX는 미지원이며 정확한 문구는 생성 모델과 분리된 레이어로 합성합니다."
    )

    def __init__(self):
        super().__init__()
        self.name = "mockup_design"
        self.version = "1.3.0"
        self.description = "구조 학습·원본 보존 재렌더링·비파괴 보정을 지원하는 시안 제작 Runtime"
        self.runtime = None

    def _runtime(self):
        if self.runtime is None: self.runtime = MockupDesignRuntime()
        return self.runtime

    def get_tools(self):
        image_array = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12}
        return [
            ToolSchema("mockup_learn_style", "모델 가중치 학습 없이 여러 참고 시안의 디자인 규칙·색상·비율을 분석해 프로필로 저장합니다", {
                "type": "object", "properties": {
                    "reference_paths": image_array, "name": {"type": "string", "default": "새 시안 스타일"},
                }, "required": ["reference_paths"], "additionalProperties": False,
            }, ["filesystem_read", "screen_read"], side_effect="change", timeout_seconds=300),
            ToolSchema("mockup_render", "저장된 스타일과 제작용 사진으로 새 PNG 시안을 렌더링합니다. " + self.BACKEND_DESCRIPTION, {
                "type": "object", "properties": {
                    "profile_id": {"type": "string"}, "production_paths": image_array,
                    "instruction": {"type": "string", "default": ""},
                    "visible_copy": {"type": "string", "default": ""},
                    "output_dir": {"type": "string"}, "basename": {"type": "string", "default": "mockup"},
                    "backend": {"type": "string", "enum": list(self.RENDER_BACKENDS), "default": "auto",
                                "description": self.BACKEND_DESCRIPTION},
                    "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647, "default": 42},
                }, "required": ["profile_id", "production_paths", "output_dir"], "additionalProperties": False,
            }, ["filesystem_read", "filesystem_write"], side_effect="change", timeout_seconds=300),
            ToolSchema("mockup_list_styles", "저장된 시안 스타일 프로필을 조회합니다", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, side_effect="read"),
            ToolSchema("mockup_delete_style", "저장된 시안 스타일 프로필 하나를 삭제합니다", {
                "type": "object", "properties": {"profile_id": {"type": "string"}},
                "required": ["profile_id"], "additionalProperties": False,
            }, ["filesystem_write"], side_effect="change"),
            ToolSchema("mockup_generation_status", "백엔드별 설치·장치 준비 상태와 실제 지원 기능을 조회합니다. 준비 상태는 추론 성공 검증이 아닙니다. " + self.BACKEND_DESCRIPTION, {
                "type": "object", "properties": {
                    "backend": {"type": "string", "enum": ["all", *self.GENERATIVE_BACKENDS], "default": "all",
                                "description": "all은 SD1.5/IP-Adapter와 SDXL을 구분해 조회합니다. 모델 다운로드나 추론은 실행하지 않습니다."},
                }, "additionalProperties": False,
            }, side_effect="read"),
            ToolSchema("mockup_prepare_generation", "선택한 생성형 백엔드만 앱 전용 폴더에 내려받습니다. generative=SD1.5/IP-Adapter, generative_sdxl=SDXL Turbo. 추론 성공 검증·가중치 학습은 수행하지 않습니다.", {
                "type": "object", "properties": {
                    "backend": {"type": "string", "enum": list(self.GENERATIVE_BACKENDS), "default": "generative",
                                "description": "기존 기본값은 generative입니다. SDXL을 준비하려면 generative_sdxl을 명시하세요."},
                }, "additionalProperties": False,
            }, ["filesystem_write", "network_access"], side_effect="change", timeout_seconds=1800),
        ]

    @staticmethod
    def _selected_backend(data, allowed, default):
        backend = data.get("backend", default)
        if not isinstance(backend, str) or backend not in allowed:
            raise ValueError(f"지원하지 않는 시안 백엔드입니다: {backend!r}. 사용 가능: {', '.join(allowed)}")
        return backend

    @classmethod
    def _backend_capabilities(cls, backend):
        if backend not in cls.RENDER_BACKENDS:
            raise ValueError("지원하지 않는 시안 백엔드입니다.")
        generated = backend in cls.GENERATIVE_BACKENDS
        return {
            "backend": backend,
            "generation_mode": ("reference_conditioned_text_to_image" if backend == "generative" else
                                "text_to_image" if backend == "generative_sdxl" else "original_preserving_scene"),
            "generated_background": generated,
            "reference_image_conditioning": backend == "generative",
            "reference_style_profile": True,
            "exact_text": "separate_scene_layers",
            "generative_inpainting": False,
            "controlnet": False,
            "flux": False,
            "model_weight_training": False,
            "limitations": (
                ["SDXL은 텍스트 기반 배경 생성만 지원합니다. 참고 이미지는 설계 분석에만 쓰이며 SDXL 입력 픽셀 조건이 아닙니다.",
                 "SDXL 참조 이미지 편집·인페인팅·ControlNet·FLUX는 연결되지 않았습니다."]
                if backend == "generative_sdxl" else
                ["IP-Adapter 참고 이미지에서 인물·문구가 재생성될 수 있어 원본 동일성은 보장하지 않습니다.",
                 "생성형 인페인팅·ControlNet·FLUX는 연결되지 않았습니다."]
                if backend == "generative" else
                ["로컬 장면 레이어 합성이며 임의의 생성형 픽셀 편집은 지원하지 않습니다."]
            ),
        }

    @classmethod
    def _backend_status(cls, backend, status):
        if not isinstance(status, dict) or type(status.get("ready")) is not bool:
            raise ValueError(f"{backend} 백엔드가 유효한 준비 상태를 반환하지 않았습니다.")
        return {
            **status, "backend": backend, "capabilities": cls._backend_capabilities(backend),
            # Preserve the backend's narrower, auditable scope.  In
            # particular SDXL distinguishes structural FP16 snapshot checks
            # from package imports, checksums, tensor values and inference.
            "readiness_scope": str(status.get("readiness_scope") or "installed_files_and_device"),
            "inference_verified": False,
        }

    def _generation_status(self, backend):
        status = self._runtime().generation_status()
        if not isinstance(status, dict):
            raise ValueError("생성형 백엔드의 상태 응답이 올바르지 않습니다.")
        # Never let SD1.5's top-level ready flag stand in for SDXL readiness.
        raw = {"generative": {key: value for key, value in status.items() if key != "sdxl"},
               "generative_sdxl": status.get("sdxl")}
        if backend != "all":
            return self._backend_status(backend, raw[backend])
        return {
            "backend": "all",
            "backends": {key: self._backend_status(key, value) for key, value in raw.items()},
            "unsupported_backends": ["flux"], "inference_verified": False,
        }

    def execute_tool(self, name, data):
        try:
            if name == "mockup_learn_style":
                profile = self._runtime().learn_style(data["reference_paths"], name=data.get("name", "새 시안 스타일"))
                return ToolRunResult.successful(
                    tool_name=name, raw_output=f"시안 스타일 '{profile.name}' 분석을 완료했습니다.",
                    evidence=[Evidence("mockup_style_profile", "참고 이미지 해시·색상·비율과 Vision 분석을 저장했습니다.", profile.__dict__)],
                    artifacts=[Artifact("style_profile", str(self._runtime().profile_dir / f"{profile.profile_id}.json"))],
                )
            if name == "mockup_render":
                backend = self._selected_backend(data, self.RENDER_BACKENDS, "auto")
                result = self._runtime().render(
                    data["profile_id"], data["production_paths"], instruction=data.get("instruction", ""),
                    visible_copy=data.get("visible_copy", ""),
                    output_dir=data["output_dir"], basename=data.get("basename", "mockup"),
                    backend=backend, seed=data.get("seed", 42),
                )
                result = {**result, "requested_backend": backend,
                          "backend_capabilities": self._backend_capabilities(backend)}
                return ToolRunResult.successful(
                    tool_name=name, raw_output=f"시안 이미지 생성 완료: {result['output']}",
                    evidence=[Evidence("mockup_render", "출력 이미지와 입력·스타일 해시를 기록했습니다.", result)],
                    artifacts=[Artifact("image", result["output"], result)],
                )
            if name == "mockup_list_styles":
                profiles = [profile.__dict__ for profile in self._runtime().list_profiles()]
                return ToolRunResult.successful(
                    tool_name=name, raw_output=json.dumps(profiles, ensure_ascii=False),
                    evidence=[Evidence("mockup_style_catalog", f"스타일 프로필 {len(profiles)}개를 조회했습니다.", {"profiles": profiles})],
                )
            if name == "mockup_delete_style":
                deleted = self._runtime().delete_profile(data["profile_id"])
                if not deleted:
                    return ToolRunResult.failed(tool_name=name, error="해당 스타일 프로필이 없습니다.")
                return ToolRunResult.successful(
                    tool_name=name, raw_output="선택한 시안 스타일 프로필을 삭제했습니다.",
                    evidence=[Evidence("mockup_style_deleted", "원본 참고 이미지는 유지했습니다.",
                                       {"profile_id": data["profile_id"]})],
                )
            if name == "mockup_generation_status":
                backend = self._selected_backend(data, ("all", *self.GENERATIVE_BACKENDS), "all")
                status = self._generation_status(backend)
                return ToolRunResult.successful(
                    tool_name=name, raw_output=json.dumps(status, ensure_ascii=False),
                    evidence=[Evidence("mockup_generation_status", "설치·장치 상태와 지원 범위를 조회했습니다. 실제 이미지 추론은 검증하지 않았습니다.", status)],
                )
            if name == "mockup_prepare_generation":
                backend = self._selected_backend(data, self.GENERATIVE_BACKENDS, "generative")
                runtime = self._runtime()
                prepare = runtime.prepare_sdxl_model if backend == "generative_sdxl" else runtime.prepare_generation_models
                status = self._backend_status(backend, prepare())
                if status["ready"] is not True:
                    return ToolRunResult.failed(tool_name=name,
                        error=f"{backend} 모델 준비 후에도 해당 백엔드의 파일·장치 준비 조건을 충족하지 못했습니다.",
                        evidence=[Evidence("mockup_generation_status", "선택한 백엔드의 미준비 상태를 확인했습니다.", status)])
                return ToolRunResult.successful(
                    tool_name=name, raw_output=f"{backend} 모델 파일·장치 준비를 완료했습니다. 실제 이미지 추론 성공은 아직 검증하지 않았습니다.",
                    evidence=[Evidence("mockup_generation_ready", "선택한 모델의 파일·장치 준비 상태만 확인했습니다.", status)],
                )
            return ToolRunResult.failed(tool_name=name, error="지원하지 않는 시안 도구입니다.")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=name, error=str(exc))
