"""Reviewed learning, evaluation and post-training preparation tools."""
from __future__ import annotations

from core.evaluation_runtime import seed_core_evaluation_cases
from core.learning_runtime import get_learning_runtime
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Evidence, ToolRunResult


class LearningPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "learning"
        self.version = "1.0.0"
        self.description = "사용자 피드백과 검토 가능한 학습 데이터를 관리합니다."

    def get_tools(self):
        return [
            ToolSchema(
                "learning_record_feedback", "직전 실행에 대한 평가와 올바른 답변을 기록합니다.",
                {"type": "object", "properties": {
                    "trajectory_id": {"type": "string"}, "session_id": {"type": "string"},
                    "rating": {"type": "integer", "enum": [-1, 0, 1]},
                    "correction": {"type": "string"}, "reason": {"type": "string"},
                }, "additionalProperties": False}, side_effect="change", verification_required=False,
            ),
            ToolSchema(
                "learning_export_training_data", "검토용 SFT/DPO JSONL 데이터셋을 내보냅니다.",
                {"type": "object", "properties": {"output_dir": {"type": "string", "default": "data/training_exports/latest"}},
                 "additionalProperties": False}, side_effect="change", verification_required=True,
            ),
            ToolSchema(
                "learning_seed_evaluations", "핵심 회귀 평가 사례를 등록합니다.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                side_effect="change", verification_required=False,
            ),
        ]

    def get_intents(self):
        return [
            IntentSchema(
                "learning.feedback", "아니스의 직전 답변을 평가하거나 정정", "learning_record_feedback",
                ["피드백", "답변이 틀", "정정할게", "이 답변"],
                [SlotSchema("correction", "올바른 답변", "어떻게 고치면 되는지 알려주세요.", required=False),
                 SlotSchema("reason", "평가 이유", "어떤 점이 잘못됐는지 알려주세요.", required=False)],
                execution_hints=["기록", "정정", "틀", "좋았"], request_type="change",
            )
        ]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        slots.setdefault("correction", text)
        slots.setdefault("reason", "사용자 자연어 피드백")
        slots.setdefault("rating", -1 if any(token in text for token in ("틀", "잘못", "이상")) else 1)
        return slots

    def execute_tool(self, tool_name, tool_input):
        runtime = get_learning_runtime()
        if tool_name == "learning_record_feedback":
            feedback_id = runtime.add_feedback(**tool_input)
            return ToolRunResult.successful(
                tool_name=tool_name, raw_output="피드백을 학습 후보로 기록했습니다.",
                evidence=[Evidence("feedback", feedback_id, {"review_required": True})],
            )
        if tool_name == "learning_export_training_data":
            output_dir = tool_input.get("output_dir", "data/training_exports/latest")
            counts = runtime.export_training_data(output_dir)
            return ToolRunResult.successful(
                tool_name=tool_name, raw_output=f"검토용 데이터셋을 내보냈습니다: {counts}",
                evidence=[Evidence("training_export", output_dir, counts)],
            )
        if tool_name == "learning_seed_evaluations":
            count = seed_core_evaluation_cases(runtime)
            return ToolRunResult.successful(
                tool_name=tool_name, raw_output=f"핵심 평가 사례 {count}개를 등록했습니다.",
                evidence=[Evidence("evaluation_seed", str(count), {})],
            )
        return ToolRunResult.failed(tool_name=tool_name, error="지원하지 않는 학습 도구입니다.")
