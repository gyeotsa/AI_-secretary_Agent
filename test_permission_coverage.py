"""Exercise permission decisions with isolated storage and no external actions."""
from types import SimpleNamespace

import pytest

from core.permission import PermissionDecision, PermissionLevel, PermissionManager, TOOL_PERMISSION_MAP


PERMISSION_IDS = (
    "filesystem_read", "filesystem_write", "filesystem_delete", "shell_execute",
    "microphone", "clipboard", "git_commit", "git_push", "mail_send", "browser",
    "self_modify", "network_access", "windows_api", "data_delete", "automation",
    "cloud_account", "cloud_read", "external_send", "screen_capture", "screen_read",
    "coordinate_control", "computer_control", "proactive_read", "proactive_manage",
)


@pytest.fixture
def permissions(tmp_path, monkeypatch):
    import core.permission as module
    manager = PermissionManager(str(tmp_path / "permissions.json"))
    monkeypatch.setattr(module, "get_permission_manager", lambda: manager)
    return manager


@pytest.mark.parametrize("permission_id", PERMISSION_IDS)
def test_every_permission_blocks_before_tool_execution_and_allows_after_grant(permissions, permission_id, monkeypatch):
    from core import productization, quality_metrics
    from core.tool_result import Evidence, ToolRunResult
    from core.tools import ToolExecutor

    assert set(permissions.permissions) == set(PERMISSION_IDS)
    events = SimpleNamespace(emit=lambda *args, **kwargs: None, increment=lambda *args: None,
                             observe=lambda *args: None, record=lambda *args, **kwargs: None)
    monkeypatch.setattr(productization, "TRACE", events)
    monkeypatch.setattr(productization, "METRICS", events)
    monkeypatch.setattr(productization.SafeModeManager, "enabled", lambda self: False)
    monkeypatch.setattr(quality_metrics, "get_quality_metric_store", lambda: events)
    calls = []
    schema = SimpleNamespace(name="permission_probe", required_permissions=[permission_id], side_effect="read")

    def run(name, payload):
        calls.append((name, payload))
        return ToolRunResult.successful(tool_name=name, raw_output="검증용 결과",
                                        evidence=[Evidence("test", "외부 작업 없는 검증")])

    executor = ToolExecutor.__new__(ToolExecutor)
    executor.plugin_registry = SimpleNamespace(get_all_tools=lambda: [schema],
                                               get_capability=lambda _name: schema, execute_tool=run)
    executor._record_tool_run = lambda *_args: None
    executor._adapt_tool_output = lambda _name, _payload, result, _duration: result
    permissions.revoke_permission(permission_id)
    result = executor.execute_tool("permission_probe", {})
    assert not result.succeeded and permission_id in result.error
    assert calls == []
    reloaded = PermissionManager(str(permissions.storage_path))
    assert reloaded.check_permission(permission_id) is False
    assert reloaded.request_permission(permission_id) is False
    permissions.grant_permission(permission_id)
    assert executor.execute_tool("permission_probe", {}).succeeded
    assert len(calls) == 1
    assert PermissionManager(str(permissions.storage_path)).check_permission(permission_id) is True


def test_safe_permissions_stay_automatic_until_explicitly_blocked(permissions):
    permissions.set_request_callback(lambda _permission: pytest.fail("SAFE reads need no prompt"))
    for permission_id in ("filesystem_read", "proactive_read"):
        assert permissions.request_permission(permission_id)
        permissions.revoke_permission(permission_id)
        assert not permissions.request_permission(permission_id)
        assert not permissions.check_permission(permission_id)


def test_scoped_allow_cannot_override_global_block_or_unknown_permission(permissions):
    permissions.grant_scoped("filesystem_write", "folder", str(permissions.storage_path.parent))
    permissions.revoke_permission("filesystem_write")
    assert not permissions.request_scoped_permission("filesystem_write", "folder", str(permissions.storage_path))
    assert not permissions.request_scoped_permission("unknown_permission", "folder", str(permissions.storage_path))


def test_scope_can_still_restrict_global_allow(permissions):
    permissions.grant_permission("filesystem_read")
    permissions.grant_scoped("filesystem_read", "folder", str(permissions.storage_path.parent), decision="block")
    assert not permissions.request_scoped_permission("filesystem_read", "folder", str(permissions.storage_path))


def test_dismissed_request_does_not_become_saved_refusal(permissions):
    calls = []
    permissions.set_request_callback(lambda permission: calls.append(permission.id))
    assert not permissions.request_permission("filesystem_write")
    assert not permissions.request_permission("filesystem_write")
    assert calls == ["filesystem_write", "filesystem_write"]
    assert permissions.permissions["filesystem_write"].decision == PermissionDecision.UNDECIDED
    assert not permissions.storage_path.exists()


def test_once_always_prompts_and_never_persists_a_new_decision(permissions):
    permissions.grant_permission("computer_control")
    seen = []
    permissions.set_request_callback(lambda permission: seen.append(permission) or True)
    assert permissions.request_once("computer_control", "검증용 클릭 1회")
    assert permissions.request_once("computer_control", "검증용 클릭 2회")
    assert len(seen) == 2 and all(not permission.persist_decision for permission in seen)
    assert [permission.description for permission in seen] == ["검증용 클릭 1회", "검증용 클릭 2회"]
    permissions.revoke_permission("computer_control")
    assert not permissions.request_once("computer_control", "거절된 클릭")
    assert len(seen) == 2


def test_denied_level_cannot_be_reenabled_by_saved_allow(permissions):
    permissions.grant_permission("shell_execute")
    permissions.permissions["shell_execute"].level = PermissionLevel.DENIED
    assert not permissions.request_permission("shell_execute")
    assert not permissions.check_permission("shell_execute")


@pytest.mark.parametrize(("tool_name", "permission_id"), [
    ("local_calendar_delete_event", "data_delete"), ("rag_remove_document", "data_delete"),
    ("mockup_delete_style", "filesystem_delete"), ("learning_export_training_data", "filesystem_write"),
    ("web_search", "network_access"), ("get_weather", "network_access"),
    ("obsidian_status", "filesystem_read"), ("obsidian_explore", "filesystem_read"),
    ("obsidian_lint", "filesystem_read"),
])
def test_previously_unmapped_tools_respect_denial(permissions, tool_name, permission_id):
    from core.tools import ToolExecutor
    assert TOOL_PERMISSION_MAP[tool_name] == permission_id
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.plugin_registry = SimpleNamespace(get_all_tools=lambda: [])
    permissions.revoke_permission(permission_id)
    allowed, message = executor._request_tool_permissions(tool_name, {})
    assert not allowed and permission_id in message


def test_all_declared_plugin_permissions_are_known(permissions):
    import ast
    from pathlib import Path
    for path in Path(__file__).parent.joinpath("plugins").glob("*.py"):
        for call in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "ToolSchema":
                continue
            value = call.args[3] if len(call.args) > 3 else next(
                (kw.value for kw in call.keywords if kw.arg == "required_permissions"), None)
            if isinstance(value, (ast.List, ast.Tuple)):
                for permission_id in ast.literal_eval(value):
                    assert permission_id in permissions.permissions, (path.name, permission_id)
    assert set(TOOL_PERMISSION_MAP.values()) <= set(permissions.permissions)


@pytest.mark.parametrize("permission_id", ["browser", "cloud_read"])
def test_direct_browser_mail_refuses_before_any_browser_action(permissions, permission_id, monkeypatch):
    import core.browser_mail as module
    monkeypatch.setattr(module, "get_permission_manager", lambda: permissions)
    permissions.revoke_permission(permission_id)
    service = module.BrowserMailService.__new__(module.BrowserMailService)
    for operation, args in ((service.open, ("gmail",)), (service.list_inbox, ("gmail",)),
                            (service.list_current, ("gmail",)), (service.draft_count, ("gmail",)),
                            (service.read_message, ("gmail", "unused"))):
        with pytest.raises(module.BrowserMailError, match="권한"):
            operation(*args)


def test_direct_background_mail_refuses_before_loading_credentials(permissions, monkeypatch):
    import core.background_mail as module
    monkeypatch.setattr(module, "get_permission_manager", lambda: permissions)
    permissions.revoke_permission("cloud_read")
    service = module.BackgroundMailService()
    with pytest.raises(module.BackgroundMailError, match="권한"):
        service.list_inbox("gmail")
    assert service._gmail is None


@pytest.mark.parametrize("operation", ["open", "inspect", "fill", "fill_saved"])
def test_direct_browser_login_respects_browser_block(permissions, operation, monkeypatch):
    import core.browser_login as module
    monkeypatch.setattr(module, "get_permission_manager", lambda: permissions)
    permissions.revoke_permission("browser")
    service = module.BrowserLoginService.__new__(module.BrowserLoginService)
    args = {"url": "https://accounts.google.com/"}
    with pytest.raises(ValueError, match="권한"):
        getattr(service, operation)(**args)


def test_saved_browser_credentials_respect_account_block(permissions, monkeypatch):
    import core.browser_login as module
    monkeypatch.setattr(module, "get_permission_manager", lambda: permissions)
    permissions.revoke_permission("cloud_account")
    service = module.BrowserLoginService.__new__(module.BrowserLoginService)
    with pytest.raises(ValueError, match="권한"):
        service.save_account("https://accounts.google.com/", "synthetic-user", "synthetic-secret")
    with pytest.raises(ValueError, match="권한"):
        service.fill_saved("https://accounts.google.com/")


def test_direct_oauth_and_provider_calls_do_not_bypass_block(permissions):
    from core.remote_runtime import OAuthCoordinator, ProviderApi, RemoteApplyRejected, RemoteRuntimeError
    # Missing attributes deliberately prove refusal happens before any transport or vault access.
    oauth = OAuthCoordinator.__new__(OAuthCoordinator)
    api = ProviderApi.__new__(ProviderApi)
    permissions.revoke_permission("cloud_account")
    with pytest.raises(RemoteRuntimeError, match="권한"):
        oauth.begin("google", "synthetic", "http://127.0.0.1/")
    with pytest.raises(RemoteRuntimeError, match="권한"):
        oauth.complete("synthetic-state", "synthetic-code")
    permissions.revoke_permission("external_send")
    with pytest.raises(RemoteApplyRejected, match="권한"):
        api.apply(None)
    permissions.revoke_permission("cloud_read")
    for operation, args in ((api.sync, ("google_drive", "synthetic", {})),
                            (api.read_messages, ("slack", "synthetic", {})),
                            (api.read_calendar, ("google", "synthetic", "", ""))):
        with pytest.raises(RemoteRuntimeError, match="권한"):
            operation(*args)


def test_direct_mail_account_save_and_authentication_respect_block(permissions, monkeypatch):
    import core.mail_accounts as module
    monkeypatch.setattr(module, "get_permission_manager", lambda: permissions)
    permissions.revoke_permission("cloud_account")
    service = module.MailAccountService.__new__(module.MailAccountService)
    with pytest.raises(module.MailReadError, match="권한"):
        service.save("synthetic-user", "synthetic-secret")
    with pytest.raises(module.MailReadError, match="권한"):
        service.verify()


@pytest.mark.parametrize("stage", ["before_capture", "during_capture", "during_transcription"])
def test_one_shot_listen_stops_and_discards_text_after_microphone_off(permissions, monkeypatch, stage):
    import numpy as np
    import core.tools as module
    hardware = SimpleNamespace(_listener_generation=0, _manual_stop=stage == "before_capture")
    captured, transcribed, closed = [], [], []
    hardware._select_microphone = lambda: ({"index": 0, "name": "검증용 입력"}, 16000)
    hardware._to_16khz = lambda audio, _rate: audio

    def transcribe(audio):
        transcribed.append(len(audio))
        if stage == "during_transcription":
            hardware._listener_generation += 1
            hardware._manual_stop = True
        return {"text": "폐기해야 하는 검증용 전사"}

    hardware._transcribe_audio = transcribe

    class InputStream:
        def __init__(self, **kwargs):
            assert kwargs["device"] == 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            closed.append(True)

        def read(self, count):
            captured.append(count)
            if stage == "during_capture":
                hardware._listener_generation += 1
                hardware._manual_stop = True
            return np.zeros((count, 1), dtype=np.float32), False

    monkeypatch.setattr(module, "WHISPER_AVAILABLE", True)
    monkeypatch.setattr(module, "sd", SimpleNamespace(InputStream=InputStream))
    executor = module.ToolExecutor.__new__(module.ToolExecutor)
    executor._hardware_manager = hardware
    result = executor.listen(1)
    assert result.status.value == "cancelled"
    assert "폐기해야" not in result.raw_output
    assert len(captured) == (0 if stage == "before_capture" else 1 if stage == "during_capture" else 10)
    assert len(transcribed) == (1 if stage == "during_transcription" else 0)
    assert bool(closed) == (stage != "before_capture")
