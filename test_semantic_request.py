import json
from types import SimpleNamespace

import pytest

from core.plugin import BasePlugin, IntentSchema, PluginRegistry, SlotSchema, ToolSchema
from core.semantic_request import SemanticRequestInterpreter, explicit_control, literal_reply


class _Surface(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "semantic_test_surface"

    def get_tools(self):
        return [
            ToolSchema("read_note", "파일 읽기", {"type": "object", "properties": {
                "filename": {"type": "string"}}, "required": ["filename"]}, side_effect="read"),
            ToolSchema("write_note", "파일 작성", {"type": "object", "properties": {
                "filename": {"type": "string"}, "instruction": {"type": "string"}},
                "required": ["filename", "instruction"]}, side_effect="change"),
            ToolSchema("desktop_send_message", "카카오톡 전송", {"type": "object", "properties": {
                "recipient": {"type": "string"}, "message": {"type": "string"},
                "provider": {"type": "string", "enum": ["kakaotalk"]}},
                "required": ["recipient", "message", "provider"]}, side_effect="external_send"),
            # No declared intent: discovery must still expose this capability.
            ToolSchema("weather_lookup", "지정 장소의 현재 기상 자료", {"type": "object", "properties": {
                "location": {"type": "string"}}, "required": ["location"]}, side_effect="read"),
            ToolSchema("listen", "마이크에서 다음 입력 대기", {}, side_effect="execute"),
        ]

    def get_intents(self):
        return [
            IntentSchema("notes.read", "파일 읽기", "read_note", ["읽기"],
                         [SlotSchema("filename", "파일명", "어떤 파일인가요?")], request_type="query"),
            IntentSchema("notes.write", "파일 작성", "write_note", ["작성"],
                         [SlotSchema("filename", "파일명", "어떤 파일인가요?"),
                          SlotSchema("instruction", "내용", "어떤 내용인가요?")]),
            IntentSchema("messaging.send", "메시지 전송", "desktop_send_message", ["카톡"],
                         [SlotSchema("provider", "메신저", "어떤 메신저인가요?"),
                          SlotSchema("recipient", "받는 사람", "누구에게 보낼까요?"),
                          SlotSchema("message", "본문", "무슨 내용인가요?")], request_type="external_send"),
        ]

    def execute_tool(self, tool_name, tool_input):
        raise AssertionError("Semantic interpretation must not execute tools")


@pytest.fixture
def registry():
    value = PluginRegistry()
    value.register_plugin(_Surface())
    return value


def _data(**changes):
    return {"relation": "new", "operation": "read", "intent_name": "notes.read",
            "tool_names": ["read_note"], "slots": {"filename": "Agent 인수인계.txt"},
            "confidence": .98, "needs_clarification": False, "clarification_question": "",
            "control_scope": "current", **changes}


class _Model:
    def __init__(self, data):
        self.data = data
        self.messages = []
        self.calls = []

    def chat(self, messages):
        self.messages = messages
        self.calls.append(messages)
        return json.dumps(self.data, ensure_ascii=False)


def _interpret(registry, raw, data, **kwargs):
    model = _Model(data)
    decision = SemanticRequestInterpreter(model, registry).interpret(raw, **kwargs)
    return decision, model


@pytest.mark.parametrize("text, expected", [
    ("취소", ("cancel", "current")),
    ("이전 승인 대기중 작업을 모두 취소해줘", ("cancel", "all")),
    ("승인 대기 중인 작업을 취소해 주세요", ("cancel", "pending")),
    ("전체 작업들을 전부 중단해줘", ("cancel", "all")),
    ("그만해", ("cancel", "current")),
    ("승인", ("approve", "current")),
    ("'취소'라고 보내줘", None),
    ("'승인'이라는 단어를 설명해줘", None),
    ("취소하면 어떻게 돼?", None),
    ("이 작업은 취소하지 마", None),
    ("이전 작업 취소", None),
])
def test_controls_require_complete_nonquoted_instruction(text, expected):
    assert explicit_control(text) == expected


@pytest.mark.parametrize("raw, expected", [
    ('"내일  학교에서 만나\nx = [1, 2]"라고 보내줘', "내일  학교에서 만나\nx = [1, 2]"),
    ("‘취소’라고 보내줘", "취소"),
    ("'테스트 메시지'", "테스트 메시지"),
    ("메시지 내용은 뭘까?", None),
    ("'안녕'이라는 말을 쓰지 마", None),
])
def test_literal_body_preserves_exact_user_data(raw, expected):
    assert literal_reply(raw) == expected


def test_unfamiliar_file_query_calls_semantic_model_before_keyword_routing(registry):
    raw = "Agent 인수인계.txt 파일에 작성되어 있는 첫 번째 줄 내용을 알려줘"
    decision, model = _interpret(registry, raw, _data())
    assert decision.raw_text == raw
    assert decision.grounded and decision.operation == "read"
    assert decision.to_resolution(registry).tool_name == "read_note"
    assert model.messages


def test_model_cannot_label_read_request_as_write(registry):
    raw = "Agent 인수인계.txt 파일에 작성되어 있는 첫 줄을 보여줘"
    decision, _ = _interpret(registry, raw, _data(operation="change", intent_name="notes.write",
                            tool_names=["write_note"], slots={"filename": "Agent 인수인계.txt"}))
    assert not decision.grounded
    assert decision.reason == "read_request_cannot_mutate"


@pytest.mark.parametrize("operation, names", [("read", ["write_note"]), ("change", ["read_note"])])
def test_operation_must_match_actual_tool_contract(registry, operation, names):
    decision, _ = _interpret(registry, "Agent 인수인계.txt 수정해줘", _data(
        operation=operation, intent_name="", tool_names=names))
    assert not decision.grounded
    assert decision.reason == "operation_tool_mismatch"


def test_tools_without_intent_are_discoverable_and_validated(registry):
    decision, model = _interpret(registry, "서울 비 올까?", _data(
        intent_name="", tool_names=["weather_lookup"], slots={"location": "서울"}))
    tools = json.loads(model.messages[1]["content"])["available_tools"]
    assert "weather_lookup" in {t["tool"] for t in tools}
    assert "listen" not in {t["tool"] for t in tools}
    assert decision.grounded
    assert decision.to_resolution(registry).ready


def test_allowed_scope_is_hard_boundary(registry):
    decision, model = _interpret(registry, "서울 날씨 알려줘", _data(
        intent_name="", tool_names=["weather_lookup"], slots={"location": "서울"}), allowed_tools=["read_note"])
    assert not decision.grounded
    assert [t["tool"] for t in json.loads(model.messages[1]["content"])["available_tools"]] == ["read_note"]


@pytest.mark.parametrize("changes", [
    {"intent_name": "invented.intent"}, {"tool_names": ["invented_tool"]},
    {"slots": {"filename": ["Agent 인수인계.txt"]}},
    {"slots": {"unknown": "x"}}, {"confidence": True}, {"confidence": "0.99"},
    {"confidence": .2}, {"confidence": float("nan")}, {"relation": "maybe"},
    {"needs_clarification": "false"}, {"tool_names": "read_note"},
])
def test_bad_model_output_never_becomes_executable(registry, changes):
    decision, _ = _interpret(registry, "Agent 인수인계.txt 읽어줘", _data(**changes))
    assert not decision.grounded
    assert not decision.to_resolution(registry).ready


def test_assistant_history_cannot_supply_recipient_or_body(registry):
    data = _data(operation="external_send", intent_name="messaging.send", tool_names=["desktop_send_message"],
                 slots={"provider": "kakaotalk", "recipient": "홍길동", "message": "돈 보내줘"})
    decision, _ = _interpret(registry, "카톡 보내줘", data,
                             history=[{"role": "assistant", "content": "홍길동에게 돈 보내줘라고 보낼까요?"}])
    assert not decision.grounded
    assert decision.reason == "ungrounded_literal:recipient"


def test_pending_quotative_reply_inherits_recipient_and_preserves_payload(registry):
    pending = {"intent_name": "messaging.send", "slots": {"provider": "kakaotalk", "recipient": "형택"}}
    decision, _ = _interpret(registry, "테스트 메시지 라고 보내줘", _data(
        relation="continue", operation="external_send", intent_name="messaging.send",
        tool_names=["desktop_send_message"], slots={"message": "테스트 메시지"}), pending=pending)
    assert decision.grounded
    assert decision.slots == {**pending["slots"], "message": "테스트 메시지"}
    assert decision.to_resolution(registry).ready


def test_quoted_pending_body_requires_no_model_and_never_executes(registry):
    pending = {"intent_name": "messaging.send", "slots": {"provider": "kakaotalk", "recipient": "형택"}}
    decision = SemanticRequestInterpreter(None, registry).interpret('"내일  학교에서 만나"라고 보내줘', pending=pending)
    assert decision.grounded and decision.source == "literal_reply"
    assert decision.slots["message"] == "내일  학교에서 만나"


def test_new_topic_never_inherits_old_pending_message_fields(registry):
    pending = {"intent_name": "messaging.send", "slots": {"provider": "kakaotalk", "recipient": "형택"}}
    decision, _ = _interpret(registry, "서울 날씨 알려줘", _data(intent_name="", tool_names=["weather_lookup"],
                                                               slots={"location": "서울"}), pending=pending)
    assert decision.grounded and "recipient" not in decision.slots


def test_mismatched_intent_cannot_insert_an_unselected_external_tool(registry):
    decision, _ = _interpret(registry, "서울 날씨 알려줘", _data(
        intent_name="messaging.send", tool_names=["weather_lookup"], slots={"location": "서울"}))
    assert not decision.grounded and decision.reason == "intent_tool_mismatch"


def test_registered_intent_is_derived_from_selected_tool(registry):
    data = _data()
    data.pop("intent_name")
    decision, _ = _interpret(registry, "Agent 인수인계.txt 읽어줘", data)
    assert decision.grounded and decision.intent_name == "notes.read"


class _DiscoveryModel:
    def __init__(self, discovery, final):
        self.outputs = [discovery, final]
        self.calls = []

    def chat_structured(self, messages, schema, *, context_window=None):
        self.calls.append((messages, schema, context_window))
        return json.dumps(self.outputs[len(self.calls) - 1], ensure_ascii=False)


def test_two_stage_discovery_exposes_every_tool_then_only_selected_contract(registry):
    model = _DiscoveryModel({"tool_names": ["weather_lookup"], "confidence": .9}, _data(
        intent_name="", tool_names=["weather_lookup"], slots={"location": "서울"}))
    interpreter = SemanticRequestInterpreter(model, registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    decision = interpreter.interpret("서울 비 올까?")
    assert decision.grounded and decision.tool_names == ("weather_lookup",)
    first, second = [json.loads(call[0][1]["content"]) for call in model.calls]
    assert {c["tool"] for c in first["available_tools"]} == {
        "read_note", "write_note", "desktop_send_message", "weather_lookup"}
    assert all("parameters" not in c for c in first["available_tools"])
    assert [c["tool"] for c in second["available_tools"]] == ["weather_lookup"]
    assert "location" in second["available_tools"][0]["parameters"]
    assert [call[0][-1] for call in model.calls] == [{"role": "user", "content": "서울 비 올까?"}] * 2
    assert [call[2] for call in model.calls] == [8192, 8192]


@pytest.mark.parametrize("discovery", [
    {"tool_names": ["invented_tool"], "confidence": .9},
    {"tool_names": "weather_lookup", "confidence": .9},
    {"tool_names": ["listen"], "confidence": .9},
    {"tool_names": ["weather_lookup"], "confidence": .2},
    {"tool_names": ["weather_lookup"], "confidence": True},
    {"tool_names": ["weather_lookup"], "confidence": float("nan")},
    {"tool_names": ["weather_lookup"] * 17, "confidence": .9},
])
def test_invalid_discovery_never_reaches_semantic_resolution(registry, discovery):
    model = _DiscoveryModel(discovery, _data())
    interpreter = SemanticRequestInterpreter(model, registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    decision = interpreter.interpret("서울 날씨 알려줘")
    assert not decision.grounded and decision.reason == "semantic_discovery_invalid"
    assert len(model.calls) == 1


def test_final_pass_cannot_select_tool_outside_discovered_scope(registry):
    model = _DiscoveryModel({"tool_names": ["weather_lookup"], "confidence": .9}, _data())
    model.outputs.append(_data())  # Reconsideration must retain discovery's tool scope.
    interpreter = SemanticRequestInterpreter(model, registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    decision = interpreter.interpret("Agent 인수인계.txt 읽어줘")
    assert not decision.grounded and decision.reason == "unknown_or_out_of_scope_tool"


def test_explicit_conversation_discovery_skips_redundant_model_call(registry):
    model = _DiscoveryModel({"request_kind": "conversation", "tool_names": [],
                             "confidence": .99}, None)
    interpreter = SemanticRequestInterpreter(model, registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    result = interpreter.interpret("안녕?", pending={"task_id": "pending-send"})
    assert result.grounded and result.relation == "conversation"
    assert result.tool_names == () and result.slots == {}
    assert len(model.calls) == 1


def test_cancelled_discovery_cannot_dispatch_followup_model_call(registry):
    from core.plugin import ToolCancelledError
    from core.turn_context import TurnExecutionContext, bind_turn_context
    context = TurnExecutionContext("old", "session")
    calls = []
    class CancellingModel:
        def chat_structured(self, messages, schema, **kwargs):
            calls.append(messages)
            context.cancel()
            return json.dumps({"request_kind": "action", "tool_names": ["read_note"], "confidence": .99})
    interpreter = SemanticRequestInterpreter(CancellingModel(), registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        interpreter.interpret("Agent 인수인계.txt 읽어줘")
    assert len(calls) == 1


def test_unsupported_action_is_not_conversation(registry):
    model = _DiscoveryModel({"request_kind": "unsupported", "tool_names": [],
                             "confidence": .95}, None)
    interpreter = SemanticRequestInterpreter(model, registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    result = interpreter.interpret("실제 우주선을 조종해줘")
    assert result.relation == "new" and not result.needs_clarification
    assert result.reason == "no_supported_tool" and not result.tool_names
    assert len(model.calls) == 1


@pytest.mark.parametrize("kind,tools,confidence", [
    ("conversation", ["write_note"], .99), ("unsupported", ["read_note"], .99),
    ("action", [], .99), ("invented", [], .99), ("conversation", [], True),
])
def test_discovery_disposition_cannot_hide_actions(registry, kind, tools, confidence):
    model = _DiscoveryModel({"request_kind": kind, "tool_names": tools,
                             "confidence": confidence}, None)
    interpreter = SemanticRequestInterpreter(model, registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    result = interpreter.interpret("파일을 작성해줘")
    assert not result.grounded and result.reason == "semantic_discovery_invalid"
    assert len(model.calls) == 1


@pytest.mark.parametrize("selection_key", ["group_names", "tool_names"])
def test_discovery_generation_schema_enforces_kind_and_selection_consistency(registry, selection_key):
    from jsonschema import Draft202012Validator
    model = _RoutingModel(lambda *_: {"request_kind": "conversation", selection_key: [], "confidence": .99})
    interpreter = SemanticRequestInterpreter(model, registry)
    messages = [{"role": "system", "content": "discovery"}, {"role": "user", "content": json.dumps({
        "current_user_input": "이야기하자", "recent_dialogue": []})}]
    assert interpreter._discovery_selection(messages, {"candidate"}, selection_key) == ((), "conversation", .99)
    schema = model.calls[0][1]
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for kind, names, expected in [
        ("conversation", [], True), ("conversation", ["candidate"], False),
        ("unsupported", [], True), ("unsupported", ["candidate"], False),
        ("action", ["candidate"], True), ("action", [], False),
        ("unknown", [], True), ("unknown", ["candidate"], True),
        ("action", ["invented"], False),
    ]:
        assert validator.is_valid({"request_kind": kind, selection_key: names, "confidence": .85}) is expected
    # Each grammar branch is independently a complete object contract, not a
    # partial condition that a provider can ignore while generating output.
    assert all(branch["required"] == schema["required"] and not branch["additionalProperties"]
               for branch in schema["oneOf"])


@pytest.mark.parametrize("kind,confidence", [("unknown", .95), ("conversation", .75)])
def test_ambiguous_empty_tools_still_require_interpretation(registry, kind, confidence):
    model = _DiscoveryModel({"request_kind": kind, "tool_names": [], "confidence": confidence},
                            _data(relation="conversation", operation="conversation",
                                  tool_names=[], intent_name="", slots={}))
    interpreter = SemanticRequestInterpreter(model, registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    result = interpreter.interpret("생각을 정리하고 싶어")
    assert result.grounded and result.relation == "conversation"
    assert len(model.calls) == 2


def test_production_catalog_is_complete_without_tool_execution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    value = PluginRegistry()
    value.load_plugins_from_directory()
    from plugins.legacy_runtime import LegacyRuntimePlugin
    value.register_plugin(LegacyRuntimePlugin())
    assert not value._load_failures

    def forbidden(*args, **kwargs):
        raise AssertionError("Catalog inspection must not execute tools")

    monkeypatch.setattr(value, "execute_tool", forbidden)
    class GroupConversation:
        def __init__(self):
            self.calls = []

        def chat_structured(self, messages, schema, **kwargs):
            self.calls.append(messages)
            key = "group_names" if "group_names" in schema["properties"] else "tool_names"
            return json.dumps({"request_kind": "conversation", key: [], "confidence": .99})

    model = GroupConversation()
    # A greeting takes the deterministic conversation path; use an unresolved
    # conversational request to exercise the production catalogue itself.
    decision = SemanticRequestInterpreter(model, value).interpret("생각을 정리하고 싶어")
    assert decision.grounded
    prompts = [json.loads(call[1]["content"]) for call in model.calls]
    indexes = [prompt for prompt in prompts if "available_tool_groups" in prompt]
    details = [prompt for prompt in prompts if "available_tool_groups" not in prompt]
    assert len(indexes) == 1  # Entire index fits one call; it does not grant a no-tool disposition.
    expected = {c.name for c in value.get_capabilities()} - SemanticRequestInterpreter.RUNTIME_ONLY_TOOLS
    assert {name for prompt in indexes for names in prompt["available_tool_groups"].values()
            for name in names} == expected
    assert all(prompt["available_tools"] == [] for prompt in indexes)
    assert {entry["tool"] for prompt in details for entry in prompt["available_tools"]} == expected
    assert 1 < len(model.calls) <= SemanticRequestInterpreter.DISCOVERY_MAX_CALLS
    assert len(expected) >= 202  # Full production inventory, including legacy compatibility tools.
    encoded = json.dumps(model.calls[0], ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"production_catalog_tools": len(expected), "registered_plugins": len(value.plugins),
                      "registered_intents": len(value.get_all_intents()), "prompt_chars": len(encoded),
                      "prompt_utf8_bytes": len(encoded.encode("utf-8")), "index_calls": len(indexes),
                      "detail_calls": len(details), "discovery_calls": len(model.calls)}))


def test_literal_substring_is_not_enough_when_full_body_boundary_is_known(registry):
    pending = {"intent_name": "messaging.send", "slots": {"provider": "kakaotalk", "recipient": "형택"}}
    decision, _ = _interpret(registry, "테스트 메시지 라고 보내줘", _data(
        relation="continue", operation="external_send", intent_name="messaging.send",
        tool_names=["desktop_send_message"], slots={"message": "메시지"}), pending=pending)
    assert not decision.grounded and decision.reason == "message_literal_changed"


@pytest.mark.parametrize("relation, scope", [("approve", "all"), ("cancel", "all"), ("cancel", "pending")])
def test_model_control_never_silently_expands_authority(registry, relation, scope):
    decision, _ = _interpret(registry, "앞서 이야기한 건 없던 걸로", _data(
        relation=relation, operation="control", intent_name="", tool_names=[], slots={}, control_scope=scope))
    if relation == "approve":
        assert not decision.grounded
    else:
        assert decision.needs_clarification


def test_actual_workspace_filename_with_spaces_is_grounded_without_invention(registry, tmp_path):
    from plugins.filesystem import FilesystemPlugin
    target = tmp_path / "Agent 인수인계.txt"
    target.write_bytes("실제 첫 줄\n두 번째 줄\n".encode("utf-8"))
    plugin = FilesystemPlugin()
    plugin.workspace = SimpleNamespace(get_workspace_path=lambda: str(tmp_path))
    registry.register_plugin(plugin)
    decision, model = _interpret(registry, "Agent 인수인계 파일 첫 번째 줄 읽어줘", _data(
        intent_name="filesystem.read_file", tool_names=["filesystem_read_file"],
        slots={"filename": "Agent 인수인계.txt", "start_line": 1, "end_line": 1}))
    assert decision.grounded
    assert json.loads(model.messages[1]["content"])["verified_workspace_file_candidates"] == ["Agent 인수인계.txt"]
    result = plugin.execute_tool(decision.tool_names[0], decision.slots)
    assert json.loads(result.raw_output)["content"] == "실제 첫 줄\n"
    assert target.read_text(encoding="utf-8") == "실제 첫 줄\n두 번째 줄\n"


def test_ambiguous_real_file_names_require_confirmation(registry, tmp_path):
    from plugins.filesystem import FilesystemPlugin
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "노트.txt").write_text(name, encoding="utf-8")
    plugin = FilesystemPlugin()
    plugin.workspace = SimpleNamespace(get_workspace_path=lambda: str(tmp_path))
    registry.register_plugin(plugin)
    decision, _ = _interpret(registry, "노트.txt 읽어줘", _data(
        intent_name="filesystem.read_file", tool_names=["filesystem_read_file"], slots={"filename": "a/노트.txt"}))
    assert decision.needs_clarification
    assert not decision.to_resolution(registry).ready


def test_unavailable_model_stays_unresolved_not_fake_conversation(registry):
    decision = SemanticRequestInterpreter(None, registry).interpret("안 써본 방식으로 도와줘")
    assert not decision.grounded and decision.relation == "unknown"


def test_structured_capable_models_receive_json_contract(registry):
    class Structured:
        def chat_structured(self, messages, schema):
            assert schema["properties"]["relation"]["enum"]
            assert "intent_name" not in schema["properties"]
            return json.dumps(_data(), ensure_ascii=False)

    decision = SemanticRequestInterpreter(Structured(), registry).interpret("Agent 인수인계.txt 읽어줘")
    assert decision.grounded


class _ManyToolSurface(BasePlugin):
    def __init__(self, number):
        super().__init__()
        self.name = f"group_{number}"

    def get_tools(self):
        return [ToolSchema(f"{self.name}_inspect_{'x' * 90}_{i}", "Inspection contract " * 12,
                           {"type": "object", "properties": {"limit": {
                               "type": "integer", "description": "Exact requested quantity " * 6}}},
                           side_effect="read") for i in range(10)]

    def execute_tool(self, *args):
        raise AssertionError("Discovery must not execute a tool")


def _many_tools():
    value = PluginRegistry()
    for number in range(8):
        value.register_plugin(_ManyToolSurface(number))
    return value


class _RoutingModel:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def chat_structured(self, messages, schema, **kwargs):
        payload = json.loads(messages[1]["content"])
        self.calls.append((payload, schema, kwargs, messages))
        return json.dumps(self.respond(payload, schema, len(self.calls)), ensure_ascii=False)


def test_global_refinement_preserves_complete_overflow_candidate_pool_before_final_validation():
    class CandidatePool(_Surface):
        def get_tools(self):
            tools = []
            for number in range(9):
                tools.extend(ToolSchema(f"choice_{2 * number + offset}", "candidate", {}, side_effect="read")
                             for offset in range(2))
                tools.append(ToolSchema(f"ballast_{number}", "contract", {"type": "object", "properties": {
                    "mode": {"type": "string", "enum": ["a" * 700, "b" * 700]}}}, side_effect="read"))
            return tools

        def get_intents(self):
            return []

    value = PluginRegistry()
    value.register_plugin(CandidatePool())
    refinements = []
    def respond(payload, schema, call):
        if "available_tool_groups" in payload:
            return {"request_kind": "action", "group_names": list(payload["available_tool_groups"]), "confidence": .99}
        if "request_kind" not in schema["properties"]:
            assert [entry["tool"] for entry in payload["available_tools"]] == ["choice_17"]
            return _data(intent_name="", tool_names=["choice_17"], slots={})
        names = [entry["tool"] for entry in payload["available_tools"]]
        selected = [name for name in names if name.startswith("choice_")]
        if len(selected) == 18:
            assert names == selected
            assert schema["properties"]["tool_names"]["maxItems"] == 16
            refinements.append(selected)
            selected = ["choice_17"]
        return {"request_kind": "action" if selected else "unsupported", "tool_names": selected, "confidence": .99}
    model = _RoutingModel(respond)
    result = SemanticRequestInterpreter(model, value).interpret("현재 자료를 조회해줘")
    assert result.grounded and result.tool_names == ("choice_17",)
    assert len(refinements) == 1 and set(refinements[0]) == {f"choice_{number}" for number in range(18)}
    assert len(model.calls) <= SemanticRequestInterpreter.DISCOVERY_MAX_CALLS + 1


@pytest.mark.parametrize("kind", ["conversation", "unsupported", "unknown"])
def test_negative_candidate_refinement_cannot_promote_prior_action_to_conversation(registry, kind):
    model = _DiscoveryModel({"request_kind": "action", "tool_names": ["read_note", "weather_lookup"], "confidence": .99},
                            {"request_kind": kind, "tool_names": [], "confidence": .99})
    interpreter = SemanticRequestInterpreter(model, registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    result = interpreter.interpret("자료를 조회해줘")
    assert not result.grounded and not result.is_grounded_conversation
    assert result.reason == "semantic_discovery_invalid" and len(model.calls) == 2


def test_global_refinement_keeps_all_mock_mail_read_and_send_stages():
    class MailPair(_Surface):
        def get_tools(self):
            return [ToolSchema("mail_read", "메일 조회", {"type": "object", "properties": {
                "message_id": {"type": "string"}}, "required": ["message_id"]}, side_effect="read"),
                ToolSchema("mail_send", "메일 전송", {"type": "object", "properties": {
                    "recipient": {"type": "string"}, "body": {"type": "string"}},
                    "required": ["recipient", "body"]}, side_effect="external_send")]

        def get_intents(self):
            return []

    value = PluginRegistry()
    value.register_plugin(MailPair())
    selected = {"request_kind": "action", "tool_names": ["mail_read", "mail_send"], "confidence": .99}
    model = _DiscoveryModel(selected, selected)
    model.outputs.append(_data(intent_name="", operation="external_send", tool_names=["mail_read", "mail_send"],
                               slots={"message_id": "msg_42"}))
    interpreter = SemanticRequestInterpreter(model, value)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    result = interpreter.interpret("msg_42 메일을 조회한 뒤 수신자에게 전달해줘")
    assert result.grounded and result.tool_names == ("mail_read", "mail_send")
    assert result.operation == "external_send" and len(model.calls) == 3
    assert all({entry["tool"] for entry in json.loads(call[0][1]["content"])["available_tools"]}
               == {"mail_read", "mail_send"} for call in model.calls)
    assert not result.to_resolution(value).ready  # Still requires compound planning and approval, never direct execution.


def test_oversized_refinement_pool_fails_without_truncating_candidates():
    value = _many_tools()
    detailed = set()
    def respond(payload, schema, call):
        if "available_tool_groups" in payload:
            groups = [group for group in payload["available_tool_groups"] if group in {"group_0", "group_1"}]
            return {"request_kind": "action" if groups else "unsupported", "group_names": groups, "confidence": .99}
        names = [entry["tool"] for entry in payload["available_tools"]]
        detailed.update(names)
        return {"request_kind": "action", "tool_names": names, "confidence": .99}
    model = _RoutingModel(respond)
    result = SemanticRequestInterpreter(model, value).interpret("현재 자료를 조회해줘")
    assert result.reason.endswith(":context_saturated") and not result.grounded
    assert detailed == {c.name for c in value.get_capabilities() if c.name.startswith(("group_0_", "group_1_"))}
    assert all("request_kind" in schema["properties"] for _, schema, *_ in model.calls)


def test_candidate_refinement_shares_discovery_call_cap_and_cancellation(registry):
    from core.plugin import ToolCancelledError
    from core.turn_context import TurnExecutionContext, bind_turn_context
    selected = {"request_kind": "action", "tool_names": ["read_note", "weather_lookup"], "confidence": .99}
    limited = _DiscoveryModel(selected, selected)
    interpreter = SemanticRequestInterpreter(limited, registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    interpreter.DISCOVERY_MAX_CALLS = 1
    result = interpreter.interpret("자료를 조회해줘")
    assert result.reason.endswith(":discovery_budget_exhausted") and len(limited.calls) == 1
    context = TurnExecutionContext("refinement", "session")
    def respond(payload, schema, call):
        if call == 2:
            context.cancel()
        return selected
    cancelling = _RoutingModel(respond)
    interpreter = SemanticRequestInterpreter(cancelling, registry)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        interpreter.interpret("자료를 조회해줘")
    assert len(cancelling.calls) == 2


@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("index_kind", ["action", "conversation", "unsupported", "wrong_group", "unknown"])
def test_grouped_discovery_covers_last_group_and_pluginless_capabilities(orphan, index_kind):
    value = _many_tools()
    target = value.get_capabilities()[-1].name
    if orphan:
        value.plugins.pop("group_7")  # Capability remains registered without group ownership metadata.
    seen, detailed = set(), set()

    def respond(payload, schema, call):
        if "available_tool_groups" in payload:
            groups = payload["available_tool_groups"]
            seen.update(name for names in groups.values() for name in names)
            chosen = ([group for group in groups if group == "group_0"] if index_kind == "wrong_group"
                      else [group for group, names in groups.items() if target in names] if index_kind == "action" else [])
            kind = "action" if chosen else "unsupported" if index_kind in {"action", "wrong_group"} else index_kind
            return {"request_kind": kind, "group_names": chosen, "confidence": 0.0 if kind == "unknown" else .95}
        if "request_kind" in schema["properties"]:
            detailed.update(entry["tool"] for entry in payload["available_tools"])
            chosen = [target] if any(entry["tool"] == target for entry in payload["available_tools"]) else []
            return {"request_kind": "action" if chosen else "unsupported", "tool_names": chosen, "confidence": .99}
        return _data(intent_name="", tool_names=[target], slots={})

    model = _RoutingModel(respond)
    decision = SemanticRequestInterpreter(model, value).interpret("현재 측정값 알려줘")
    assert decision.grounded and decision.tool_names == (target,)
    assert seen == {c.name for c in value.get_capabilities()}
    assert detailed == ({name for name in seen if name.startswith("group_7_")} if index_kind == "action" else seen)
    assert sum("available_tool_groups" in p for p, *_ in model.calls) >= 2
    assert [entry["tool"] for entry in model.calls[-1][0]["available_tools"]] == [target]
    for _, schema, options, messages in model.calls:
        reserve = 512 if "request_kind" in schema["properties"] else 1024
        assert options == {"context_window": 8192, "max_output_tokens": reserve}
        assert sum(len(m["content"].encode("utf-8")) for m in messages) + len(messages) * 64 + 256 + reserve <= 8192


@pytest.mark.parametrize("later_kind", ["conversation", "unknown"])
def test_mixed_index_batches_refine_all_groups_before_global_unsupported(later_kind):
    value = _many_tools()
    detailed = set()
    def respond(payload, schema, call):
        if "available_tool_groups" in payload:
            return {"request_kind": "unsupported" if call == 1 else later_kind,
                    "group_names": [], "confidence": .99}
        detailed.update(entry["tool"] for entry in payload["available_tools"])
        return {"request_kind": "unsupported", "tool_names": [], "confidence": .99}
    model = _RoutingModel(respond)
    result = SemanticRequestInterpreter(model, value).interpret("실제 위성의 궤도를 바꿔줘")
    assert result.reason == "no_supported_tool" and not result.is_grounded_conversation
    assert detailed == {c.name for c in value.get_capabilities()}


def test_selected_group_no_hit_checks_remainder_and_mixed_negative_is_not_unsupported():
    value = _many_tools()
    detailed = set()
    def respond(payload, schema, call):
        if "available_tool_groups" in payload:
            chosen = [group for group in payload["available_tool_groups"] if group == "group_0"]
            return {"request_kind": "action" if chosen else "unsupported", "group_names": chosen, "confidence": .99}
        if "request_kind" in schema["properties"]:
            names = {entry["tool"] for entry in payload["available_tools"]}
            detailed.update(names)
            kind = "conversation" if all(name.startswith("group_0_") for name in names) else "unsupported"
            return {"request_kind": kind, "tool_names": [], "confidence": .99}
        assert payload["available_tools"] == []
        return _data(relation="conversation", operation="conversation", intent_name="", tool_names=[], slots={})
    model = _RoutingModel(respond)
    result = SemanticRequestInterpreter(model, value).interpret("새로운 작업을 해줘")
    assert detailed == {c.name for c in value.get_capabilities()}
    assert result.is_grounded_conversation and result.source == "model"
    assert "request_kind" not in model.calls[-1][1]["properties"]


def test_invalid_later_index_batch_never_reaches_contract_or_execution():
    value = _many_tools()
    def respond(payload, schema, call):
        assert "available_tool_groups" in payload
        return {"request_kind": "unsupported" if call == 1 else "action",
                "group_names": [] if call == 1 else ["invented_group"], "confidence": .99}
    model = _RoutingModel(respond)
    result = SemanticRequestInterpreter(model, value).interpret("새로운 검사 작업을 해줘")
    assert result.reason == "semantic_discovery_invalid" and not result.grounded
    assert len(model.calls) == 2


@pytest.mark.parametrize("key,kind,names,confidence,valid", [
    ("group_names", "unknown", [], 0, True),
    ("group_names", "unknown", [], .64, True),
    ("group_names", "unknown", ["candidate"], .64, False),
    ("group_names", "action", ["candidate"], .64, False),
    ("group_names", "unsupported", [], .64, False),
    ("tool_names", "unknown", [], .64, False),
    ("group_names", "unknown", [], True, False),
    ("group_names", "unknown", [], float("nan"), False),
    ("group_names", "unknown", [], -1, False),
])
def test_low_confidence_abstention_only_opens_detail_review(registry, key, kind, names, confidence, valid):
    model = _RoutingModel(lambda *_: {"request_kind": kind, key: names, "confidence": confidence})
    result = SemanticRequestInterpreter(model, registry)._discovery_selection(
        [{"role": "system", "content": "discovery"}, {"role": "user", "content": json.dumps({
            "current_user_input": "요청을 검토해줘", "recent_dialogue": []})}], {"candidate"}, key)
    assert (result is not None) is valid


def test_discovery_call_budget_stops_before_another_send():
    value = _many_tools()
    model = _RoutingModel(lambda *_: {"request_kind": "unsupported", "group_names": [], "confidence": .99})
    interpreter = SemanticRequestInterpreter(model, value)
    interpreter.DISCOVERY_MAX_CALLS = 1
    result = interpreter.interpret("새로운 검사 작업을 해줘")
    assert result.reason.endswith(":discovery_budget_exhausted") and not result.grounded
    assert len(model.calls) == 1


def test_cancellation_after_group_index_propagates_without_detail_send():
    from core.plugin import ToolCancelledError
    from core.turn_context import TurnExecutionContext, bind_turn_context
    value = _many_tools()
    context = TurnExecutionContext("grouped", "session")
    def respond(payload, schema, call):
        context.cancel()
        return {"request_kind": "action", "group_names": [next(iter(payload["available_tool_groups"]))],
                "confidence": .99}
    model = _RoutingModel(respond)
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        SemanticRequestInterpreter(model, value).interpret("검사 작업을 해줘")
    assert len(model.calls) == 1


@pytest.mark.parametrize("style", ["strict", "context", "capped", "variadic", "uninspectable", "chat"])
def test_model_call_negotiates_only_supported_keywords_and_preserves_dialogue(registry, monkeypatch, style):
    calls = []
    model = SimpleNamespace()
    def invoke(messages, schema=None, **options):
        calls.append((messages, options))
        return "{}"
    if style == "strict":
        model.chat_structured = lambda messages, schema: invoke(messages, schema)
    elif style == "context":
        model.chat_structured = lambda messages, schema, *, context_window=None: invoke(messages, schema, context_window=context_window)
    elif style == "capped":
        model.chat_structured = lambda messages, schema, *, max_output_tokens=None: invoke(messages, schema, max_output_tokens=max_output_tokens)
    elif style == "variadic":
        model.chat_structured = invoke
    elif style == "uninspectable":
        model.chat_structured = lambda messages, schema: invoke(messages, schema)
        monkeypatch.setattr("core.semantic_request.inspect.signature", lambda _: (_ for _ in ()).throw(ValueError()))
    else:
        model.chat = invoke
    history = [{"role": "user", "content": "old exact 🙂"}, {"role": "assistant", "content": "old reply"}]
    envelope = [{"role": "system", "content": "system"}, {"role": "user", "content": json.dumps({
        "current_user_input": "current exact 🙂", "recent_dialogue": history, "data": "untrusted metadata"})}]
    interpreter = SemanticRequestInterpreter(model, registry)
    assert interpreter._model_call(envelope, {}) == "{}"
    assert calls[0][0][2:] == [*history, {"role": "user", "content": "current exact 🙂"}]
    options = calls[0][1]
    assert ("context_window" in options) == (style in {"context", "variadic"})
    assert ("max_output_tokens" in options) == (style in {"capped", "variadic"})


@pytest.mark.parametrize("profile_limit", [1024, 8192, 0])
def test_whole_prompt_and_uncapped_provider_output_are_budgeted_before_send(registry, profile_limit):
    model = _Model(_data())
    model.profile = SimpleNamespace(max_tokens=profile_limit)
    history = [{"role": "user", "content": "preserved history " * 500}]
    result = SemanticRequestInterpreter(model, registry).interpret("Agent 인수인계.txt 읽어줘", history=history)
    assert result.reason.endswith(":context_saturated") and not result.grounded
    assert model.calls == []
    assert history[0]["content"] == "preserved history " * 500


def test_pending_tool_group_is_refined_even_after_index_conversation(registry):
    for number in range(8):
        registry.register_plugin(_ManyToolSurface(number))
    pending = {"intent_name": "messaging.send", "slots": {"recipient": "형택", "provider": "kakaotalk"},
               "question": "무슨 내용을 보낼까요?", "original_request": "형택에게 카톡 보내줘"}
    def respond(payload, schema, call):
        if "request_kind" in schema["properties"]:
            key = "group_names" if "available_tool_groups" in payload else "tool_names"
            return {"request_kind": "conversation", key: [], "confidence": .99}
        assert [entry["tool"] for entry in payload["available_tools"]] == ["desktop_send_message"]
        return _data(relation="conversation", operation="conversation", intent_name="", tool_names=[], slots={})
    model = _RoutingModel(respond)
    result = SemanticRequestInterpreter(model, registry).interpret("잠깐 이야기하고 싶어", pending=pending)
    assert result.is_grounded_conversation
    details = [p for p, schema, *_ in model.calls if "request_kind" in schema["properties"]
               and "available_tool_groups" not in p]
    assert {entry["tool"] for p in details for entry in p["available_tools"]} == {
        c.name for c in registry.get_capabilities()} - SemanticRequestInterpreter.RUNTIME_ONLY_TOOLS
    assert pending["slots"] == {"recipient": "형택", "provider": "kakaotalk"}


def test_range_parameter_descriptions_and_defaults_reach_discovery_and_final(tmp_path):
    from plugins.filesystem import FilesystemPlugin
    from plugins.legacy_runtime import LegacyRuntimePlugin
    value = PluginRegistry()
    plugin = FilesystemPlugin()
    plugin.workspace = SimpleNamespace(get_workspace_path=lambda: str(tmp_path))
    value.register_plugin(plugin)
    value.register_plugin(LegacyRuntimePlugin())
    model = _DiscoveryModel({"request_kind": "action", "tool_names": ["filesystem_read_file"], "confidence": .99},
                            _data(intent_name="filesystem.read_file", tool_names=["filesystem_read_file"],
                                  slots={"filename": "QA 기록.txt", "start_line": 1, "end_line": 1}))
    interpreter = SemanticRequestInterpreter(model, value)
    interpreter.SINGLE_PASS_CATALOG_CHARS = 0
    result = interpreter.interpret("QA 기록.txt 파일의 첫 번째 줄만 그대로 알려줘",
                                   allowed_tools=["read_file", "filesystem_read_file"])
    assert result.grounded and result.slots["start_line"] == result.slots["end_line"] == 1
    first, final = [json.loads(call[0][1]["content"]) for call in model.calls]
    assert {entry["tool"] for entry in first["available_tools"]} == {"read_file", "filesystem_read_file"}
    fields = next(entry["fields"] for entry in first["available_tools"] if entry["tool"] == "filesystem_read_file")
    parameters = final["available_tools"][0]["parameters"]
    original = value.get_capability("filesystem_read_file").input_schema["properties"]
    for name in ("filename", "start_line", "end_line", "max_chars"):
        assert fields[name]["description"] == parameters[name]["description"] == original[name]["description"][:160]
    assert fields["start_line"]["default"] == 1 and fields["max_chars"]["default"] == 16000
    assert "default" not in fields["end_line"]


def test_complete_slots_with_clarification_flag_never_become_ready(registry):
    decision, _ = _interpret(registry, "Agent 인수인계.txt 읽어줘", _data(
        needs_clarification=True, clarification_question="파일 전체를 읽을까요, 특정 부분을 읽을까요?"))
    assert decision.grounded and not decision.to_resolution(registry).ready


@pytest.mark.parametrize("relation,operation", [("unknown", "read"), ("conversation", "conversation")])
def test_nonaction_relation_cannot_hide_executable_tools(registry, relation, operation):
    decision, _ = _interpret(registry, "Agent 인수인계.txt 읽어줘", _data(relation=relation, operation=operation))
    assert not decision.grounded and not decision.to_resolution(registry).ready


def test_new_request_cannot_reuse_old_user_payload(registry):
    decision, _ = _interpret(registry, "카톡 하나 보내줘", _data(
        operation="external_send", intent_name="messaging.send", tool_names=["desktop_send_message"],
        slots={"provider": "kakaotalk", "recipient": "형택", "message": "옛날 본문"}),
        history=[{"role": "user", "content": "형택에게 옛날 본문이라고 보내줘"}])
    assert not decision.grounded and decision.reason == "ungrounded_literal:recipient"


def test_confirmed_recent_referent_can_supply_exact_filename(registry):
    decision, _ = _interpret(registry, "그 파일 다시 읽어줘", _data(relation="continue", slots={}),
        pending={"recent_completed": {"intent_name": "notes.read", "slots": {
            "filename": "Agent 인수인계.txt"}, "original_request": "Agent 인수인계.txt 읽어줘"}})
    assert decision.grounded and decision.slots["filename"] == "Agent 인수인계.txt"


def test_correction_inherits_only_unchanged_pending_slots(registry):
    decision, _ = _interpret(registry, "아니 형택 말고 민수에게 보내줘", _data(
        relation="correct", operation="external_send", intent_name="messaging.send",
        tool_names=["desktop_send_message"], slots={"recipient": "민수"}),
        pending={"intent_name": "messaging.send", "slots": {
            "provider": "kakaotalk", "recipient": "형택", "message": "안녕  🙂"}})
    assert decision.grounded
    assert decision.slots == {"provider": "kakaotalk", "recipient": "민수", "message": "안녕  🙂"}


@pytest.mark.parametrize("body", ["내일  학교에서 만나 🙂\nx = [1, 2]", "학교에서 만나"])
def test_full_send_request_preserves_entire_quoted_body(registry, body):
    expected = "내일  학교에서 만나 🙂\nx = [1, 2]"
    decision, _ = _interpret(registry, f'형택에게 "{expected}"라고 보내줘', _data(
        operation="external_send", intent_name="messaging.send", tool_names=["desktop_send_message"],
        slots={"provider": "kakaotalk", "recipient": "형택", "message": body}))
    assert decision.grounded == (body == expected)
    if body != expected:
        assert decision.reason == "message_literal_changed"


def test_semantic_cancel_is_not_execution_authority(registry):
    decision, _ = _interpret(registry, "앞에서 말한 건 없던 걸로", _data(
        relation="cancel", operation="control", intent_name="", tool_names=[], slots={}))
    assert decision.needs_clarification and not decision.grounded


@pytest.mark.parametrize("operation,grounded", [("read", False), ("change", True)])
def test_compound_effect_cannot_hide_mutation_behind_read_tool(registry, operation, grounded):
    decision, _ = _interpret(registry, "Agent 인수인계.txt 읽고 결과도 작성해줘", _data(
        operation=operation, tool_names=["read_note", "write_note"]))
    assert decision.grounded is grounded
    assert not decision.to_resolution(registry).ready


@pytest.mark.integration
@pytest.mark.parametrize("case", ["spaced_filename", "no_intent_tool", "pending_reply", "new_topic", "correction"])
def test_live_local_semantic_interpretation(case, registry, tmp_path, monkeypatch):
    """Explicitly opt-in local inference; no external sends or real user files."""
    import os
    import time
    if os.getenv("JARVIS_RUN_LIVE_SEMANTIC") != "1":
        pytest.skip("Set JARVIS_RUN_LIVE_SEMANTIC=1 for actual local Ollama inference")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    from core.llm import OllamaClient
    from plugins.filesystem import FilesystemPlugin
    # Invoke the production transport without BaseLLMClient's unrelated global
    # plugin/bootstrap initialization. All supplied data is synthetic.
    client = OllamaClient.__new__(OllamaClient)
    client.base_url = "http://localhost:11434"
    client.model = "qwen2.5:7b-instruct"
    client.profile = SimpleNamespace(keep_alive="5m", temperature=0, max_tokens=1024)
    client.system_prompt = ""
    client.role = "tool_selection"
    target = tmp_path / "Agent 인수인계.txt"
    target.write_bytes("실제 첫 줄\n두 번째 줄\n".encode("utf-8"))
    plugin = FilesystemPlugin()
    plugin.workspace = SimpleNamespace(get_workspace_path=lambda: str(tmp_path))
    registry.register_plugin(plugin)
    pending = {"intent_name": "messaging.send", "slots": {"provider": "kakaotalk", "recipient": "형택"},
               "original_request": "형택에게 카톡 보내줘", "question": "어떤 내용을 보낼까요?", "task_id": "synthetic"}
    raw, state = {
        "spaced_filename": ("Agent 인수인계 파일에 작성되어 있는 첫 번째 줄 내용을 알려줘", {}),
        "no_intent_tool": ("서울에 지금 비 올까?", {}),
        "pending_reply": ("테스트 메시지 라고 보내줘", pending),
        "new_topic": ("서울 날씨 알려줘", pending),
        "correction": ("아니 형택 말고 민수에게 보내줘", {**pending, "slots": {**pending["slots"], "message": "안녕  🙂"}}),
    }[case]
    responses = []
    original_chat = client.chat_structured

    def capture_chat(messages, schema, **kwargs):
        response = original_chat(messages, schema, **kwargs)
        responses.append(response)
        return response

    client.chat_structured = capture_chat
    started = time.perf_counter()
    decision = SemanticRequestInterpreter(client, registry).interpret(raw, pending=state)
    print(json.dumps({"case": case, "model": client.model, "latency_seconds": round(time.perf_counter() - started, 3),
                      "decision": decision.__dict__, "model_responses": responses}, ensure_ascii=True))
    assert decision.source == "model", decision.reason
    assert decision.grounded, decision.reason
    if case == "spaced_filename":
        assert decision.operation == "read" and decision.tool_names == ("filesystem_read_file",)
        result = plugin.execute_tool(decision.tool_names[0], decision.slots)
        assert json.loads(result.raw_output)["content"] == "실제 첫 줄\n"
    elif case in {"no_intent_tool", "new_topic"}:
        assert decision.operation == "read" and decision.tool_names == ("weather_lookup",)
        assert "recipient" not in decision.slots
    elif case == "pending_reply":
        assert decision.relation == "continue" and decision.slots["message"] == "테스트 메시지"
        assert decision.slots["recipient"] == "형택"
    elif case == "correction":
        assert decision.relation == "correct" and decision.slots["recipient"] == "민수"
        assert decision.slots["message"] == "안녕  🙂"
    assert target.read_bytes() == "실제 첫 줄\n두 번째 줄\n".encode("utf-8")


@pytest.mark.integration
@pytest.mark.parametrize("catalog_scope", ["two_contracts", "production"])
def test_live_production_catalog_semantics(tmp_path, monkeypatch, catalog_scope):
    """Full production discovery, synthetic file, local inference only; no tool runs."""
    import os
    import time
    import requests
    if os.getenv("JARVIS_RUN_LIVE_SEMANTIC_PRODUCTION") != "1":
        pytest.skip("Set JARVIS_RUN_LIVE_SEMANTIC_PRODUCTION=1 for the full catalog probe")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    from core.llm import OllamaClient
    value = PluginRegistry()
    value.load_plugins_from_directory()
    from plugins.legacy_runtime import LegacyRuntimePlugin
    value.register_plugin(LegacyRuntimePlugin())
    assert not value._load_failures
    value.get_plugin("filesystem").workspace = SimpleNamespace(get_workspace_path=lambda: str(tmp_path))

    def forbidden(*args, **kwargs):
        raise AssertionError("Live production interpretation must not execute any tool")

    monkeypatch.setattr(value, "execute_tool", forbidden)
    for plugin in value.plugins.values():
        monkeypatch.setattr(plugin, "execute_tool", forbidden)
    target = tmp_path / "QA 기록.txt"
    target.write_bytes("실제 첫 줄\n두 번째 줄\n".encode("utf-8"))
    client = OllamaClient.__new__(OllamaClient)
    client.base_url = "http://localhost:11434"
    client.model = "qwen2.5:7b-instruct"
    client.profile = SimpleNamespace(keep_alive="5m", temperature=0, max_tokens=1024)
    client.system_prompt = ""
    client.role = "tool_selection"
    original_post = requests.post
    metrics = []

    def measured_post(url, **kwargs):
        assert url == "http://localhost:11434/api/chat"
        if os.getenv("JARVIS_SEMANTIC_DIAGNOSTIC_NUM_CTX"):
            # Explicit diagnostic override; printed in evidence and never a
            # substitute for verifying the unmodified production transport.
            kwargs["json"]["options"]["num_ctx"] = int(os.environ["JARVIS_SEMANTIC_DIAGNOSTIC_NUM_CTX"])
        response = original_post(url, **kwargs)
        result = response.json()
        payload = kwargs["json"]
        metrics.append({
            "prompt_chars": sum(len(m["content"]) for m in payload["messages"]),
            "requested_options": payload["options"],
            **{k: result.get(k) for k in ("prompt_eval_count", "prompt_eval_duration", "eval_count",
                                        "eval_duration", "total_duration", "load_duration", "done_reason")},
            "model_response": result.get("message", {}).get("content", ""),
        })
        return response

    monkeypatch.setattr(requests, "post", measured_post)
    started = time.perf_counter()
    decision = SemanticRequestInterpreter(client, value).interpret(
        "QA 기록.txt 파일의 첫 번째 줄만 그대로 알려줘",
        allowed_tools=["read_file", "filesystem_read_file"] if catalog_scope == "two_contracts" else None)
    print(json.dumps({"case": f"{catalog_scope}_file_read", "model": client.model,
                      "latency_seconds": round(time.perf_counter() - started, 3),
                      "decision": decision.__dict__, "metrics": metrics}, ensure_ascii=True))
    assert decision.grounded, decision.reason
    assert decision.operation == "read" and decision.tool_names == ("filesystem_read_file",)
    assert decision.slots["filename"] == target.name
    assert decision.slots.get("start_line", 1) == 1 and decision.slots["end_line"] == 1
    assert target.read_bytes() == "실제 첫 줄\n두 번째 줄\n".encode("utf-8")
