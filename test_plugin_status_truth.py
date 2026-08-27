import types

from core.plugin import BasePlugin, PluginRegistry, ToolSchema
from core.tool_result import Evidence, ToolRunResult


class _TruthPlugin(BasePlugin):
    def __init__(self, name="truth_plugin"):
        super().__init__()
        self.name = name

    def get_tools(self):
        return [ToolSchema(
            name=f"{self.name}_read",
            description="plugin status truth test",
            input_schema={"type": "object", "additionalProperties": False},
        )]

    def execute_tool(self, tool_name, tool_input):
        return ToolRunResult.successful(
            tool_name=tool_name,
            raw_output="실행됨",
            evidence=[Evidence("runtime_probe", "실제 핸들러가 실행되었습니다.")],
        )


class _ConfiguredButUnverifiedAuthPlugin(_TruthPlugin):
    def __init__(self):
        super().__init__("unverified_auth")
        self.auth_required = True
        self.auth_type = "OAuth"

    def is_connected(self):
        return None

    def is_authenticated(self):
        return None


def test_registered_plugin_is_not_reported_as_runtime_verified_before_execution():
    registry = PluginRegistry()
    plugin = _TruthPlugin()
    registry.register_plugin(plugin)

    status = registry.get_plugin_statuses()[0]
    assert status.registered is True
    assert status.installation_state == "confirmed"
    assert status.contract_state == "confirmed"
    assert status.connection_state == "not_applicable"
    assert status.authentication_state == "not_applicable"
    assert status.runtime_state == "unchecked"
    assert status.verification_state == "unchecked"
    assert status.verified is None


def test_evidence_backed_execution_promotes_only_runtime_verification():
    registry = PluginRegistry()
    plugin = _TruthPlugin()
    registry.register_plugin(plugin)

    result = registry.execute_tool("truth_plugin_read", {})
    assert result.succeeded

    status = registry.get_plugin_statuses()[0]
    assert status.runtime_state == "confirmed"
    assert status.verification_state == "confirmed"
    assert status.verified is True
    assert status.last_tool == "truth_plugin_read"
    assert status.runtime_evidence


def test_configured_auth_without_real_probe_stays_unchecked():
    registry = PluginRegistry()
    registry.register_plugin(_ConfiguredButUnverifiedAuthPlugin())

    status = registry.get_plugin_statuses()[0]
    assert status.connection_state == "unchecked"
    assert status.authentication_state == "unchecked"
    assert status.authenticated is None
    assert status.verification_state == "unchecked"


def test_one_broken_plugin_module_does_not_abort_other_plugin_loading(
    monkeypatch, tmp_path,
):
    (tmp_path / "broken.py").write_text("# broken plugin", encoding="utf-8")
    (tmp_path / "healthy.py").write_text("# healthy plugin", encoding="utf-8")

    module = types.ModuleType("plugins.healthy")

    class HealthyPlugin(_TruthPlugin):
        def __init__(self):
            super().__init__("healthy")

    HealthyPlugin.__module__ = module.__name__
    module.HealthyPlugin = HealthyPlugin

    def import_module(name):
        if name == "plugins.broken":
            raise OSError("native dependency failed")
        if name == "plugins.healthy":
            return module
        raise AssertionError(name)

    monkeypatch.setattr("core.plugin.importlib.import_module", import_module)
    registry = PluginRegistry()
    registry.load_plugins_from_directory(str(tmp_path))

    assert registry.get_plugin("healthy") is not None
    statuses = {status.name: status for status in registry.get_plugin_statuses()}
    assert statuses["healthy"].registered is True
    assert statuses["broken"].registered is False
    assert statuses["broken"].installation_state == "failed"
    assert statuses["broken"].verification_state == "failed"
