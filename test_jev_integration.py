"""Offline Jev transport, secure account lifecycle, real routing boundary and Qt UI."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import threading

import httpx
import pytest

from core import auxiliary_models as selection, jev_client as jev
from core.jev_client import JevAccounts, JevError
from core.plugin import ToolCancelledError
from core.remote_runtime import SecureTokenVault
from core.semantic_request import SemanticRequestInterpreter
from test_kimi_integration import isolated
from test_semantic_request import _data, registry
from test_semantic_response_mode import _ModeModel, _conversation, _mode


def choice(value, options, confidence=.98):
    return {"type": "choice", "choice": value, "confidence": confidence,
            "probabilities": {k: confidence if k == value else (1 - confidence) / (len(options) - 1)
                              for k in options}}


def answer(mode="answer", kind="code", confidence=.98):
    return {"model": jev.JEV_MODEL, "answers": {
        "mode": choice(mode, ("answer", "action", "uncertain"), confidence),
        "answer_kind": choice(kind, ("conversation", "code", "reasoning")),
    }}


class MemoryVault:
    data = None

    def load(self, *_):
        return deepcopy(self.data)

    def save(self, _provider, _account, data):
        self.data = deepcopy(data)


@pytest.fixture
def service(monkeypatch, isolated):
    accounts = JevAccounts(MemoryVault())
    monkeypatch.setattr(jev, "JevAccounts", lambda: accounts)
    return accounts


def register(service, monkeypatch):
    monkeypatch.setattr(jev, "_request", lambda *a, **kw: {"models": [{"name": "jev-latest"}]})
    account_id = service.save("개인", "owner@example.test", "jev-test-secret-1234")
    service.verify(account_id)
    return account_id


def test_account_lifecycle_masks_persists_rotates_and_disables(service, monkeypatch):
    account_id = register(service, monkeypatch)
    records = service.list()
    assert records[0]["active"] and records[0]["verified_at"]
    assert "api_key" not in records[0] and "secret" not in json.dumps(records)
    assert JevAccounts(service.vault).credentials() == (account_id, "jev-test-secret-1234")
    service.save("업무", "", "", account_id)
    assert service.list()[0]["name"] == "업무" and service.list()[0]["verified_at"]
    other = service.save("두 번째", "", "jev-another-5678")
    with pytest.raises(JevError):
        service.activate(other)
    service.verify(other)
    service.activate(other)
    selection.configure("jev", True)
    generation = selection.ticket()
    service.save("두 번째", "", "jev-replacement-9100", other)
    assert not selection.is_current(generation)
    assert selection.selection() == ("jev", False)
    assert service.list()[1]["verified_at"] == ""
    with pytest.raises(JevError):
        service.credentials()
    service.verify(other)
    selection.configure("jev", True)
    service.delete(other)
    assert selection.selection() == ("jev", False)
    assert len(service.list()) == 1 and not service.list()[0]["active"]
    with pytest.raises(JevError):
        service.save("bad", "", "bad\nsecret")
    with pytest.raises(JevError):
        service.save("", "", "jev-secret-1234")


def test_dpapi_roundtrip_has_no_plaintext_and_storage_failure_is_safe(tmp_path, isolated):
    service = JevAccounts(SecureTokenVault(str(tmp_path)))
    key = "synthetic-test-key-not-a-real-secret"
    account_id = service.save("test", "", key)
    assert JevAccounts(SecureTokenVault(str(tmp_path))).credentials(account_id)[1] == key
    assert all(key.encode() not in p.read_bytes() for p in tmp_path.iterdir())
    class BrokenVault:
        def load(self, *_):
            raise RuntimeError(key)
    with pytest.raises(JevError) as error:
        JevAccounts(BrokenVault()).list()
    assert key not in str(error.value)


def test_verification_failure_never_keeps_active_key_enabled(service, monkeypatch):
    account_id = register(service, monkeypatch)
    selection.configure("jev", True)
    def rejected(*a, **kw):
        raise JevError("인증 실패")
    monkeypatch.setattr(jev, "_request", rejected)
    with pytest.raises(JevError):
        service.verify(account_id)
    assert not service.list()[0]["verified_at"]
    assert selection.selection() == ("jev", False)
    service.vault.data = {}  # Corruption must not be silently overwritten as a new store.
    with pytest.raises(JevError):
        service.save("test", "", "test-secret-key")
    assert service.vault.data == {}


def test_real_interpreter_routes_answer_and_keeps_action_validation(service, monkeypatch, registry):
    register(service, monkeypatch)
    selection.configure("jev", True)
    calls = []
    result = answer()
    def request(method, path, key, **kw):
        calls.append((method, path, deepcopy(kw["body"])))
        return result
    monkeypatch.setattr(jev, "_request", request)
    local = _ModeModel(_conversation())
    interpreter = SemanticRequestInterpreter(local, registry, classify_response_mode=True)
    decision = interpreter.interpret("간단한 반복문 예시를 보여줄래?")
    assert decision.is_grounded_conversation and decision.answer_kind == "code"
    assert len(local.calls) == 1 and len(calls) == 1
    assert "available_tools" in json.loads(local.calls[0][0][1]["content"])
    method, path, body = calls[0]
    assert (method, path, body["model"]) == ("POST", "/systemone", jev.JEV_MODEL)
    assert set(body) == {"model", "state", "questions"}
    assert set(body["questions"]) == {"mode", "answer_kind"}
    assert "secret" not in json.dumps(body)
    # Even a confidently false answer/code result cannot hide an actual action:
    # Jev is consulted only after a validated, tool-free conversation.
    result = answer("answer", "code")
    local.outputs.append(json.dumps(_data()))
    decision = interpreter.interpret("Agent 인수인계.txt 파일을 읽어줘")
    assert decision.grounded and decision.to_resolution(registry).tool_name == "read_note"
    assert len(local.calls) == 2 and len(calls) == 1
    local.outputs.append(json.dumps(_data(slots={"filename": "invented.txt"})))
    local.outputs.append(json.dumps(_data(slots={"filename": "invented.txt"})))
    decision = interpreter.interpret("Agent 인수인계.txt 파일을 읽어줘")
    assert not decision.grounded and decision.reason == "ungrounded_literal:filename"
    assert len(calls) == 1


@pytest.mark.parametrize("case", ["off", "unverified", "low", "invalid", "timeout", "changed", "large", "cancel"])
def test_no_unauthorized_or_stale_routing_and_local_fallback(service, monkeypatch, registry, case):
    account_id = register(service, monkeypatch)
    selection.configure("jev", case != "off")
    if case == "unverified":
        service.save("test", "", "replacement-key-2345", account_id)
        selection.configure("jev", True)  # Even bypassing UI cannot use an unverified key.
    calls = []
    def request(*a, **kw):
        calls.append(kw)
        if case == "timeout":
            raise JevError("연결 실패")
        if case == "changed":
            selection.configure("jev", False)
            selection.configure("jev", True)
        if case == "cancel":
            raise ToolCancelledError("취소")
        return {} if case == "invalid" else answer(confidence=.6 if case == "low" else .98)
    monkeypatch.setattr(jev, "_request", request)
    local = _ModeModel(_mode(answer_kind="reasoning"))
    interpreter = SemanticRequestInterpreter(local, registry, classify_response_mode=True)
    text = "제공된 표를 분석해줘." + ("X" * 17000 if case == "large" else "")
    if case == "cancel":
        with pytest.raises(ToolCancelledError):
            interpreter._classify_response_mode(text, [], {})
        assert not local.calls
    elif case == "large":
        from core.llm import ModelCallError
        with pytest.raises(ModelCallError) as error:
            interpreter._classify_response_mode(text, [], {})
        assert error.value.code == "context_saturated" and not local.calls
    else:
        assert interpreter._classify_response_mode(text, [], {}) == ("answer", .98, "reasoning")
        assert len(local.calls) == 1
    assert len(calls) == (0 if case in {"off", "unverified", "large"} else 1)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -.1, 1.1, ".9"])
def test_probability_validation_rejects_untrusted_numbers(value):
    payload = choice("answer", ("answer", "action", "uncertain"))
    payload["confidence"] = value
    with pytest.raises(JevError):
        jev._choice(payload, ("answer", "action", "uncertain"))


def test_http_contract_errors_redirects_size_and_cancellation(monkeypatch):
    real_client = httpx.AsyncClient
    calls = []
    status = 200
    oversized = False
    waiting, released = threading.Event(), threading.Event()
    async def handler(request):
        calls.append(request)
        if request.url.path.endswith("/wait"):
            waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                released.set()
        return httpx.Response(status, headers={"Location": "https://example.test/secret"},
                              content=b"x" * (1024 * 1024 + 1) if oversized else b'{"models": []}')
    monkeypatch.setattr(jev.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), **kw))
    assert jev._request("GET", "/models", "fake-test-key") == {"models": []}
    jev._request("POST", "/systemone", "fake-test-key", body={"model": jev.JEV_MODEL})
    assert calls[0].url == "https://api.typesafe.ai/v1/models"
    assert calls[0].headers["Authorization"] == "Bearer fake-test-key"
    assert json.loads(calls[1].content) == {"model": jev.JEV_MODEL}
    for status in (301, 401, 403, 422, 429, 529):
        count = len(calls)
        with pytest.raises(JevError) as error:
            jev._request("GET", "/models", "fake-test-key")
        assert len(calls) == count + 1 and "fake-test-key" not in str(error.value)
    status, oversized = 200, True
    with pytest.raises(JevError, match="크기"):
        jev._request("GET", "/models", "fake-test-key")
    def checkpoint():
        if waiting.is_set():
            raise ToolCancelledError("cancel")
    with pytest.raises(ToolCancelledError):
        jev._request("GET", "/wait", "fake-test-key", checkpoint=checkpoint)
    assert released.is_set()


def test_two_step_picker_and_account_ui(service, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from ui.model_selector import ModelSelector
    from ui.jev_account_dialog import JevAccountDialog
    app = QApplication.instance() or QApplication([])
    selection.configure("kimi_k3", True)
    widget = ModelSelector()
    dialog = JevAccountDialog(service)
    widget.show()
    try:
        widget.click(); app.processEvents()
        assert widget.models.isVisible() and not widget.slider.isVisible()
        assert selection.selection() == ("kimi_k3", False)
        widget.models.setCurrentRow(0)
        QTest.keyClick(widget.models, Qt.Key.Key_Return)
        assert not widget.models.isVisible() and widget.slider.isVisible()
        assert not widget.slider.isEnabled() and "보류" in widget.detail.text()
        widget.back_button.click()
        widget.models.setCurrentRow(1)
        QTest.keyClick(widget.models, Qt.Key.Key_Return)
        assert widget.jev_link.isVisible() and "console.typesafe.ai/login" in widget.jev_link.text()
        assert not widget.models.isVisible() and widget.slider.isEnabled()
        QTest.keyClick(widget.slider, Qt.Key.Key_Right)
        assert selection.selection() == ("jev", False) and "ON 전환 불가" in widget.detail.text()
        widget.popup.hide()
        # Saving authenticates in a worker without placing keys in item data.
        monkeypatch.setattr(jev, "_request", lambda *a, **kw: {"models": [{"name": "jev-latest"}]})
        dialog.show()
        dialog.account_name.setText("개인")
        dialog.api_key.setText("ui-test-secret-1234")
        dialog.save_button.click()
        assert not dialog.api_key.text()
        worker = dialog._worker
        assert worker is not None and worker.wait(2000)
        app.processEvents()
        assert dialog._worker is None and service.list()[0]["verified_at"]
        item = dialog.account_list.item(0)
        assert item.data(Qt.ItemDataRole.UserRole) == service.list()[0]["id"]
        assert "secret" not in item.text() and "1234" in item.text()
        dialog._load_selected()
        assert not dialog.api_key.text()
        dialog.account_name.setText("업무")
        dialog.save_button.click()
        assert dialog._worker.wait(2000)
        app.processEvents()
        assert service.list()[0]["name"] == "업무"
        dialog.hide()
        widget.click(); app.processEvents()
        assert widget.models.isVisible() and not widget.slider.isVisible()
        widget._select(widget.models.item(1))
        QTest.keyClick(widget.slider, Qt.Key.Key_Right)
        assert selection.selection() == ("jev", True)
        QTest.keyClick(widget.slider, Qt.Key.Key_Left)
        assert selection.selection() == ("jev", False)
        selection.configure("jev", True)
        monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
        dialog._remove_selected()
        assert service.list() == [] and selection.selection() == ("jev", False)
        assert dialog.account_list.count() == 0
    finally:
        dialog.close(); widget.popup.close(); widget.close()
        dialog.deleteLater(); widget.deleteLater(); app.processEvents()


def test_closing_account_check_cancels_worker_and_clears_key(service, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    from ui.jev_account_dialog import JevAccountDialog, _ACTIVE_CHECKS
    app = QApplication.instance() or QApplication([])
    register(service, monkeypatch)
    started = threading.Event()
    async def slow_check():
        await asyncio.sleep(.01)
    def verify(_account, checkpoint):
        started.set()
        while True:
            checkpoint()
            asyncio.run(slow_check())
    monkeypatch.setattr(service, "verify", verify)
    dialog = JevAccountDialog(service)
    try:
        dialog.show()
        dialog.account_list.setCurrentRow(0)
        dialog.api_key.setText("unsaved-test-secret")
        dialog._verify()
        worker = dialog._worker
        assert started.wait(1) and worker in _ACTIVE_CHECKS
        dialog.close()
        assert not dialog.api_key.text() and not dialog.isVisible()
        assert worker.wait(2000)
        QTest.qWait(10)
        assert dialog._worker is None and worker not in _ACTIVE_CHECKS
    finally:
        dialog.close(); dialog.deleteLater(); app.processEvents()


def test_full_composer_and_account_layout_in_both_themes(service, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QFontDatabase
    from PyQt6.QtWidgets import QApplication
    from ui.main_window import JarvisMainWindow
    from ui.jev_account_dialog import JevAccountDialog
    from ui.theme import theme_manager
    app = QApplication.instance() or QApplication([])
    for font in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/seguisym.ttf"):
        if Path(font).is_file():
            QFontDatabase.addApplicationFont(font)
    register(service, monkeypatch)
    selection.configure("jev", True)
    main, dialog = JarvisMainWindow(), JevAccountDialog(service)
    theme, previous = theme_manager(), theme_manager().mode
    def capture(widget, name):
        app.processEvents()
        if os.getenv("JEV_UI_CAPTURE_DIR"):
            output = Path(os.environ["JEV_UI_CAPTURE_DIR"])
            output.mkdir(parents=True, exist_ok=True)
            assert widget.grab().save(str(output / name))
    try:
        main.resize(1100, 760)
        main.show()
        for mode in ("dark", "light"):
            theme.set_mode(mode, persist=False)
            main.model_selector.click(); app.processEvents()
            selector = main.model_selector
            assert selector.models.isVisible() and not selector.slider.isVisible()
            for i in range(selector.models.count()):
                assert selector.models.viewport().rect().contains(selector.models.visualItemRect(selector.models.item(i)))
            capture(selector.popup, f"model-list-{mode}.png")
            selector._select(selector.models.item(1)); app.processEvents()
            assert selector.detail_page.rect().contains(selector.slider.geometry())
            assert main.composer.rect().contains(selector.geometry())
            capture(selector.popup, f"jev-controls-{mode}.png")
            capture(main, f"composer-{mode}.png")
            selector.popup.hide()
            dialog.show()
            capture(dialog, f"jev-accounts-{mode}.png")
            dialog.hide()
    finally:
        theme.set_mode(previous, persist=False)
        dialog.close(); main.model_selector.popup.close(); main.close()
        dialog.deleteLater(); main.deleteLater(); app.processEvents()
