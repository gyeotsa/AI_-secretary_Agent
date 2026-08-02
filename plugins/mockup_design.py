"""Reference-driven mockup style learning and local rendering tools."""
from __future__ import annotations

import json

from core.mockup_design import MockupDesignRuntime
from core.plugin import BasePlugin, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult


class MockupDesignPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "mockup_design"
        self.version = "1.1.0"
        self.description = "참고 스타일 생성형 배경과 원본 사진 보존 합성을 지원하는 시안 제작 Runtime"
        self.runtime = None

    def _runtime(self):
        if self.runtime is None: self.runtime = MockupDesignRuntime()
        return self.runtime

    def get_tools(self):
        image_array = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12}
        return [
            ToolSchema("mockup_learn_style", "여러 학습용 시안에서 공통 디자인 규칙과 색상·비율을 학습합니다", {
                "type": "object", "properties": {
                    "reference_paths": image_array, "name": {"type": "string", "default": "새 시안 스타일"},
                }, "required": ["reference_paths"], "additionalProperties": False,
            }, ["filesystem_read", "screen_read"], side_effect="change", timeout_seconds=300),
            ToolSchema("mockup_render", "학습된 스타일과 제작용 사진으로 새 PNG 시안을 렌더링합니다", {
                "type": "object", "properties": {
                    "profile_id": {"type": "string"}, "production_paths": image_array,
                    "instruction": {"type": "string", "default": ""},
                    "output_dir": {"type": "string"}, "basename": {"type": "string", "default": "mockup"},
                    "backend": {"type": "string", "enum": ["auto", "generative", "local"], "default": "auto"},
                    "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647, "default": 42},
                }, "required": ["profile_id", "production_paths", "output_dir"], "additionalProperties": False,
            }, ["filesystem_read", "filesystem_write"], side_effect="change", timeout_seconds=300),
            ToolSchema("mockup_list_styles", "저장된 시안 스타일 프로필을 조회합니다", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, side_effect="read"),
            ToolSchema("mockup_generation_status", "생성형 시안 모델의 설치 및 GPU 준비 상태를 조회합니다", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, side_effect="read"),
            ToolSchema("mockup_prepare_generation", "생성형 시안 모델을 앱 전용 폴더에 내려받아 준비합니다", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, ["filesystem_write", "network_access"], side_effect="change", timeout_seconds=1800),
        ]

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
                result = self._runtime().render(
                    data["profile_id"], data["production_paths"], instruction=data.get("instruction", ""),
                    output_dir=data["output_dir"], basename=data.get("basename", "mockup"),
                    backend=data.get("backend", "auto"), seed=data.get("seed", 42),
                )
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
            if name == "mockup_generation_status":
                status = self._runtime().generation_status()
                return ToolRunResult.successful(
                    tool_name=name, raw_output=json.dumps(status, ensure_ascii=False),
                    evidence=[Evidence("mockup_generation_status", "생성형 모델 준비 상태를 확인했습니다.", status)],
                )
            if name == "mockup_prepare_generation":
                status = self._runtime().prepare_generation_models()
                if not status.get("ready"):
                    return ToolRunResult.failed(tool_name=name, error="모델 준비 후에도 생성형 백엔드가 준비 상태가 아닙니다.")
                return ToolRunResult.successful(
                    tool_name=name, raw_output="생성형 시안 모델 준비를 완료했습니다.",
                    evidence=[Evidence("mockup_generation_ready", "모델 파일과 CUDA 준비 상태를 확인했습니다.", status)],
                )
            return ToolRunResult.failed(tool_name=name, error="지원하지 않는 시안 도구입니다.")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=name, error=str(exc))
