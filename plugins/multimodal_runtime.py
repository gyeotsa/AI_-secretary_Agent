"""Screen, image, video and GPU runtime tools."""
from __future__ import annotations

from typing import Any, Dict, List

from core.gpu_scheduler import get_gpu_resource_queue
from core.plugin import BasePlugin, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult
from core.vision_runtime import VisionRuntime


class MultimodalRuntimePlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "multimodal_runtime"
        self.description = "화면 영역, 다중 이미지, 영상 프레임과 GPU 자원 상태 분석"
        self.vision = None
        self.gpu = get_gpu_resource_queue()

    def _vision(self):
        if self.vision is None:
            self.vision = VisionRuntime()
        return self.vision

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("screen_capture_region", "전체 화면 또는 지정 영역을 캡처하고 해시를 검증합니다.", {
                "type": "object", "properties": {
                    "region": {"type": "object", "properties": {
                        "x": {"type": "integer"}, "y": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}},
                        "required": ["x", "y", "width", "height"], "additionalProperties": False},
                    "save_path": {"type": "string"}}, "additionalProperties": False,
            }, ["screen_capture"], side_effect="change"),
            ToolSchema("visual_analyze", "화면·이미지의 OCR, 표, 차트 또는 일반 내용을 Vision 모델로 분석합니다.", {
                "type": "object", "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12},
                    "prompt": {"type": "string"}, "mode": {"enum": ["ocr", "table", "chart", "general"]}},
                "required": ["paths", "prompt"], "additionalProperties": False,
            }, ["screen_read"], side_effect="read"),
            ToolSchema("video_sample_analyze", "영상을 시간축으로 샘플링해 다중 프레임 문맥을 분석합니다.", {
                "type": "object", "properties": {"path": {"type": "string"}, "prompt": {"type": "string"},
                    "max_frames": {"type": "integer", "minimum": 2, "maximum": 12}},
                "required": ["path", "prompt"], "additionalProperties": False,
            }, ["screen_read"], side_effect="read"),
            ToolSchema("gpu_runtime_status", "중앙 GPU 큐의 VRAM 예산과 대기 상태를 조회합니다.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, ["screen_read"], side_effect="read", verification_required=False),
        ]

    def execute_tool(self, name: str, data: Dict[str, Any]):
        try:
            if name == "screen_capture_region":
                frame = self._vision().capture_region(data.get("region"), data.get("save_path"))
                detail = frame.__dict__
                return ToolRunResult.successful(tool_name=name, raw_output="화면 캡처를 저장하고 해시를 확인했습니다.",
                    evidence=[Evidence("screen_capture", "화면 픽셀과 저장 파일 해시를 확인했습니다.", detail)],
                    artifacts=[Artifact("image", frame.path, {"sha256": frame.sha256})])
            if name == "visual_analyze":
                result = self._vision().analyze(data["paths"], data["prompt"], mode=data.get("mode", "general"))
                return ToolRunResult.successful(tool_name=name, raw_output=result["analysis"],
                    evidence=[Evidence("visual_analysis", "입력 이미지 해시와 Vision 응답을 연결했습니다.", result)],
                    artifacts=[Artifact("image", path) for path in data["paths"]])
            if name == "video_sample_analyze":
                frames = self._vision().sample_video(data["path"], int(data.get("max_frames", 8)))
                result = self._vision().analyze([frame.path for frame in frames], data["prompt"], mode="general")
                result["timeline"] = [frame.__dict__ for frame in frames]
                return ToolRunResult.successful(tool_name=name, raw_output=result["analysis"],
                    evidence=[Evidence("video_timeline", "샘플 프레임 시각·해시와 분석 결과를 연결했습니다.", result)],
                    artifacts=[Artifact("video", data["path"])])
            if name == "gpu_runtime_status":
                detail = self.gpu.snapshot()
                return ToolRunResult.successful(tool_name=name, raw_output=str(detail),
                    evidence=[Evidence("gpu_queue", "중앙 GPU 예약 상태를 조회했습니다.", detail)])
            raise ValueError(f"지원하지 않는 도구: {name}")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=name, error=str(exc))
