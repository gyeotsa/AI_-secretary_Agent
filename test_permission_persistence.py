import json

from core.permission import PermissionDecision, PermissionManager


def test_allow_decision_is_reused_without_callback(tmp_path):
    path = tmp_path / "permissions.json"
    manager = PermissionManager(str(path))
    calls = []
    manager.set_request_callback(lambda permission: calls.append(permission.id) or True)

    assert manager.request_permission("filesystem_write") is True
    assert manager.request_permission("filesystem_write") is True
    assert calls == ["filesystem_write"]
    assert PermissionManager(str(path)).check_permission("filesystem_write") is True


def test_block_decision_is_reused_without_callback(tmp_path):
    path = tmp_path / "permissions.json"
    manager = PermissionManager(str(path))
    calls = []
    manager.set_request_callback(lambda permission: calls.append(permission.id) or False)

    assert manager.request_permission("shell_execute") is False
    assert manager.request_permission("shell_execute") is False
    assert calls == ["shell_execute"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["shell_execute"]["decision"] == PermissionDecision.BLOCK.value


def test_toggle_can_replace_saved_block(tmp_path):
    manager = PermissionManager(str(tmp_path / "permissions.json"))
    manager.revoke_permission("mail_send")
    assert manager.request_permission("mail_send") is False
    manager.grant_permission("mail_send")
    assert manager.request_permission("mail_send") is True
