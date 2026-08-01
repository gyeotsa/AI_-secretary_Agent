from core.intent_router import IntentRouter
from core.plugin import BasePlugin, IntentSchema, PluginRegistry, SlotSchema, ToolSchema


class _CapabilityPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "documents"

    def get_tools(self):
        return [
            ToolSchema(
                "documents_create", "문서를 생성합니다",
                {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "style": {"type": "string"},
                    "source": {"type": "string"},
                }, "required": ["path"]},
                ["filesystem_write"],
                output_schema={"type": "object"},
                side_effect="change",
            ),
            ToolSchema(
                "documents_read", "문서를 읽습니다",
                {"type": "object", "properties": {"path": {"type": "string"}},
                 "required": ["path"]},
                ["filesystem_read"],
            ),
        ]

    def get_intents(self):
        return [
            IntentSchema(
                "documents.create", "보고서 문서 생성", "documents_create",
                ["보고서", "문서 생성"],
                [
                    SlotSchema("path", "대상 경로", "어디에 저장할까요?", role="target"),
                    SlotSchema("style", "작성 제약", "", required=False, role="constraint"),
                    SlotSchema("source", "참조 문서", "", required=False, role="reference"),
                ],
                execution_hints=["생성", "작성"],
                target_slot="path", constraint_slots=["style"], reference_slots=["source"],
                request_type="change",
            ),
            IntentSchema(
                "documents.read", "보고서 문서 조회", "documents_read",
                ["보고서", "문서 읽기"],
                [SlotSchema("path", "대상 경로", "어떤 문서인가요?", role="target")],
                execution_hints=["읽어", "조회"], request_type="query",
            ),
        ]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        if "sample.docx" in text:
            slots["path"] = "sample.docx"
        if "간결하게" in text:
            slots["style"] = "간결하게"
        if "기획서" in text:
            slots["source"] = "기획서"
        return slots

    def execute_tool(self, tool_name, tool_input):
        return "ok"


class _AmbiguousPlugin(_CapabilityPlugin):
    def __init__(self):
        super().__init__()
        self.name = "ambiguous"

    def get_intents(self):
        return [
            IntentSchema(
                "documents.summarize", "문서 요약", "documents_read",
                ["문서 처리"], [SlotSchema("path", "문서", "문서 경로를 알려주세요")],
                execution_hints=["처리"], request_type="query",
            ),
            IntentSchema(
                "documents.inspect", "문서 검사", "documents_read",
                ["문서 처리"], [SlotSchema("path", "문서", "문서 경로를 알려주세요")],
                execution_hints=["처리"], request_type="query",
            ),
        ]


def _registry():
    registry = PluginRegistry()
    registry.register_plugin(_CapabilityPlugin())
    return registry


def test_standard_intent_contains_domain_action_target_constraints_and_reference():
    resolution = IntentRouter(_registry()).resolve(
        "기획서를 참고해서 sample.docx 보고서를 간결하게 작성해줘"
    )

    assert resolution.matched
    assert resolution.domain == "documents"
    assert resolution.action == "create"
    assert resolution.target == "sample.docx"
    assert resolution.constraints == {"style": "간결하게"}
    assert resolution.reference == {"source": "기획서"}
    assert resolution.request_type == "change"
    assert resolution.routing_reason
    assert resolution.alternatives


def test_capability_contract_and_required_input_validation():
    registry = _registry()
    contract = registry.get_capability("documents_create")

    assert contract.side_effect == "change"
    assert contract.required_permissions == ["filesystem_write"]
    assert contract.verification_required is True
    assert registry.validate_tool_call("documents_create", {}) == ["필수 입력이 없습니다: path"]
    assert registry.validate_tool_call("documents_create", {"path": "a.docx"}) == []
    assert registry.validate_tool_call(
        "documents_create", {"path": "a.docx"}, "query"
    ) == ["요청 종류(query)와 도구 부작용(change)이 다릅니다."]
    assert registry.validate_tool_call("missing", {})


def test_generic_descriptor_word_does_not_create_false_domain_match():
    resolution = IntentRouter(_registry()).resolve("파일을 지워줘")
    assert not resolution.matched


def test_contract_defaults_normalize_request_and_side_effect_types():
    plugin = _CapabilityPlugin()
    tools = {tool.name: tool for tool in plugin.get_tools()}
    intents = {intent.name: intent for intent in plugin.get_intents()}

    assert tools["documents_read"].side_effect == "read"
    assert tools["documents_read"].output_schema["type"] == "object"
    assert intents["documents.read"].request_type == "query"
    assert intents["documents.create"].request_type == "change"
    assert _registry().validate_contracts() == {}


def test_all_installed_plugin_contracts_are_structurally_complete():
    registry = PluginRegistry()
    registry.load_plugins_from_directory()

    assert registry.get_all_tools()
    assert len(registry.get_capabilities()) == len(registry.get_all_tools())
    assert registry.validate_contracts() == {}
    assert all(contract.side_effect != "auto" for contract in registry.get_capabilities())
    assert all(contract.output_schema for contract in registry.get_capabilities())


def test_close_intent_collision_asks_one_explainable_question_without_execution():
    registry = PluginRegistry()
    registry.register_plugin(_AmbiguousPlugin())
    resolution = IntentRouter(registry).resolve("문서 처리해줘")

    assert resolution.ambiguous
    assert resolution.question.count("중") == 1
    assert "문서 요약" in resolution.question
    assert "문서 검사" in resolution.question
    assert resolution.alternatives
