from core.capability_audit import audit_capabilities
from core.permission import PermissionManager
from core.plugin import PluginRegistry
from core.tools import AUTO_LOOP_EXCLUDED_TOOLS


def test_complete_registry_capability_inventory_has_no_contract_or_permission_errors(tmp_path):
    registry = PluginRegistry()
    registry.load_plugins_from_directory()
    permissions = PermissionManager(str(tmp_path / "permissions.json"))

    report = audit_capabilities(
        registry,
        known_permission_ids=permissions.permissions,
        runtime_only_tools=AUTO_LOOP_EXCLUDED_TOOLS,
    )

    assert report.passed, report.to_dict()
    assert report.plugin_count == len(registry.plugins)
    assert report.tool_count == len(registry.get_all_tools())
    assert report.intent_count == len(registry.get_all_intents())
    assert set(report.intent_bound_tools) <= set(report.planner_reachable_tools)
    assert set(report.runtime_only_tools) == (
        set(AUTO_LOOP_EXCLUDED_TOOLS) & {tool.name for tool in registry.get_all_tools()}
    )


def test_compound_alternatives_are_pinned_even_without_descriptor_overlap():
    registry = PluginRegistry()
    registry.load_plugins_from_directory()
    from core.intent_router import IntentRouter
    from core.tool_loadout import ToolLoadoutSelector

    request = "메모장을 실행해줘. 그리고 유튜브에서 재즈 음악을 틀어줘"
    resolution = IntentRouter(registry).resolve(request)
    alternative_tools = {
        item["tool_name"] for item in resolution.alternatives if item.get("tool_name")
    }
    loadout = ToolLoadoutSelector(registry).select(request, resolution)

    assert resolution.tool_name in loadout.tool_names
    assert alternative_tools <= set(loadout.tool_names)
    assert not (set(AUTO_LOOP_EXCLUDED_TOOLS) & set(loadout.tool_names))


def test_every_declared_intent_has_a_deterministic_representative_route():
    registry = PluginRegistry()
    registry.load_plugins_from_directory()
    from core.intent_router import IntentRouter

    router = IntentRouter(registry)
    explicit_probes = {
        "filesystem.write_file": "sample.txt 파일 내용을 수정해줘",
        "speech.repeat_text": '"hello"를 읽어줘',
    }
    failures = []
    for _plugin, intent in registry.get_all_intents():
        probe = explicit_probes.get(intent.name)
        if not probe:
            probe = " ".join(filter(None, (
                intent.utterance_hints[0] if intent.utterance_hints else "",
                intent.execution_hints[0] if intent.execution_hints else "",
            )))
        if not probe:
            failures.append((intent.name, "대표 발화 없음", ""))
            continue
        resolved = router.resolve(probe)
        if resolved.intent_name != intent.name:
            failures.append((intent.name, probe, resolved.intent_name))

    assert not failures, failures
