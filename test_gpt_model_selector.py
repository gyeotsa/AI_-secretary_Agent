"""GPT picker flows use a fake runtime, no account secrets or external requests."""
import os
import threading
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel

from core import auxiliary_models
from core.assistant_settings import AssistantSettings
from core.codex_client import CodexRuntimeError
from ui.model_selector import ModelSelector, _ACTIVE_CODEX_CHECKS


_APP = None


def pump_until(predicate, timeout=3):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        QTest.qWait(5)
    return predicate()


class Runtime:
    def __init__(self):
        self.authenticated = False
        self.selected_model = ""
        self.started, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.calls = []
        self.login_error = False
        self.model_error = False
        self.discover_error = False
        self.discover_auth = None

    def status(self):
        return self.authenticated, "ChatGPT Plus 연결됨" if self.authenticated else "ChatGPT 로그인이 필요합니다."

    def discover(self):
        self.calls.append(("discover", threading.get_ident()))
        self.started.set()
        assert self.release.wait(3)
        if self.discover_error:
            raise CodexRuntimeError("auth", "Codex 인증 확인 실패")
        if self.discover_auth is not None:
            self.authenticated = self.discover_auth
        if self.authenticated and not self.selected_model:
            self.selected_model = "fixture-default"
        return {"authenticated": self.authenticated, "account_label": "fixture-account",
                "selected_model": self.selected_model,
                "models": [{"model": "fixture-default", "displayName": "Default GPT", "isDefault": True},
                           {"model": "fixture-second", "displayName": "Second GPT", "isDefault": False}]}

    def login(self):
        self.calls.append(("login", threading.get_ident()))
        if self.login_error:
            raise CodexRuntimeError("auth", "ChatGPT 인증 확인 실패")
        self.authenticated = True
        return self.discover()

    def set_model(self, model):
        self.calls.append(("model", threading.get_ident()))
        if self.model_error:
            raise CodexRuntimeError("model", "Codex 모델 설정 실패")
        self.selected_model = model
        return model


@pytest.fixture
def picker(monkeypatch):
    values = {}
    settings = AssistantSettings(SimpleNamespace(
        get_preference=lambda k, default: values.get(k, default),
        set_preference=lambda k, value: values.__setitem__(k, value)))
    monkeypatch.setattr(auxiliary_models, "get_assistant_settings", lambda: settings)
    monkeypatch.setattr(auxiliary_models, "_generation", 0)
    monkeypatch.setattr(auxiliary_models, "_shutdown", False)
    monkeypatch.setattr("ui.model_selector.jev_status", lambda: (False, "fixture · Jev 연결 필요"))
    assert pump_until(lambda: True)
    runtime = Runtime()
    widget = ModelSelector(codex_runtime=runtime)
    widget.show()
    yield widget, runtime
    runtime.release.set()
    assert pump_until(lambda: not _ACTIVE_CODEX_CHECKS)
    widget.popup.close()
    widget.close()
    widget.deleteLater()
    _APP.processEvents()


def select(widget, model):
    widget.click()
    _APP.processEvents()
    item = next(widget.models.item(i) for i in range(widget.models.count())
                if widget.models.item(i).data(Qt.ItemDataRole.UserRole) == model)
    widget._select(item)


def test_gpt_authentication_then_explicit_on_and_real_model_choice(picker):
    widget, runtime = picker
    select(widget, "gpt")
    assert pump_until(lambda: widget._codex_worker is None)
    assert not auxiliary_models.is_enabled("gpt")
    assert widget.connect_codex_button.isVisible() and widget.codex_model.isVisible()
    assert not widget.slider.isEnabled() and widget.codex_model.count() == 0
    assert "fixture-account" not in widget.detail.text()
    widget.connect_codex_button.click()
    assert pump_until(lambda: widget._codex_worker is None)
    assert not auxiliary_models.is_enabled("gpt")
    assert widget.slider.isEnabled() and widget.codex_model.currentData() == "fixture-default"
    assert "fixture-account" in widget.detail.text() and "fixture-default" in widget.detail.text()
    QTest.keyClick(widget.slider, Qt.Key.Key_Right)
    assert auxiliary_models.selection() == ("gpt", True)
    widget.codex_model.showPopup()
    _APP.processEvents()
    QTest.keyClick(widget.codex_model.view(), Qt.Key.Key_Down)
    QTest.keyClick(widget.codex_model.view(), Qt.Key.Key_Return)
    assert pump_until(lambda: widget._codex_worker is None)
    assert runtime.selected_model == "fixture-second" and "fixture-second" in widget.text()
    assert "fixture-second" in widget.detail.text()
    assert auxiliary_models.selection() == ("gpt", True)
    assert all(thread != threading.get_ident() for _, thread in runtime.calls)
    assert widget.popup.isVisible()
    widget.popup.hide()
    select(widget, "gpt")
    assert pump_until(lambda: widget._codex_worker is None)
    assert widget.codex_model.currentData() == "fixture-second"


def test_failed_login_and_failed_model_change_keep_truthful_ui(picker):
    widget, runtime = picker
    runtime.login_error = True
    select(widget, "gpt")
    assert pump_until(lambda: widget._codex_worker is None)
    widget.connect_codex_button.click()
    assert pump_until(lambda: widget._codex_worker is None)
    assert not auxiliary_models.is_enabled("gpt")
    assert not widget.slider.isEnabled() and "인증 확인 실패" in widget.detail.text()
    assert "fixture-account" not in "\n".join(label.text() for label in widget.findChildren(QLabel))
    runtime.login_error = False
    widget.connect_codex_button.click()
    assert pump_until(lambda: widget._codex_worker is None)
    runtime.model_error = True
    widget.codex_model.setCurrentIndex(1)
    assert pump_until(lambda: widget._codex_worker is None)
    assert widget.codex_model.currentData() == runtime.selected_model == "fixture-default"
    assert "모델 설정 실패" in widget.detail.text()


def test_discovery_never_blocks_ui_or_enables_gpt_after_provider_switch(picker):
    widget, runtime = picker
    runtime.authenticated = True
    runtime.release.clear()
    select(widget, "gpt")
    assert runtime.started.wait(1)
    assert not widget.slider.isEnabled()
    widget.back_button.click()
    original = next(widget.models.item(i) for i in range(widget.models.count())
                    if widget.models.item(i).data(Qt.ItemDataRole.UserRole) == "default")
    widget._select(original)
    assert auxiliary_models.main_selection() == ("default", True)
    assert not widget.connect_codex_button.isVisible()
    runtime.release.set()
    assert pump_until(lambda: widget._codex_worker is None)
    assert auxiliary_models.main_selection() == ("default", True)
    assert not widget.connect_codex_button.isVisible()
    select(widget, "gpt")
    assert pump_until(lambda: widget._codex_worker is None)
    assert auxiliary_models.main_selection() == ("gpt", True)
    assert widget.codex_model.currentData() == "fixture-default"


def test_main_selection_is_exclusive_and_jev_stays_enabled(picker, monkeypatch):
    widget, runtime = picker
    runtime.authenticated = True
    monkeypatch.setattr("ui.model_selector.jev_status", lambda: (True, "verified fixture"))
    select(widget, "gpt")
    assert pump_until(lambda: widget._codex_worker is None)
    assert auxiliary_models.main_selection() == ("gpt", True)
    select(widget, "jev")
    assert auxiliary_models.is_enabled("gpt")
    QTest.keyClick(widget.slider, Qt.Key.Key_Right)
    assert auxiliary_models.is_enabled("jev") and auxiliary_models.is_enabled("gpt")
    rows = {widget.models.item(i).data(Qt.ItemDataRole.UserRole): widget.models.item(i).text()
            for i in range(widget.models.count())}
    assert "ON  ✓" in rows["gpt"] and "ON  ✓" in rows["jev"]
    assert "OFF" in rows["default"] and "OFF" in rows["kimi_k3"]
    select(widget, "default")
    assert auxiliary_models.main_selection() == ("default", True)
    assert not auxiliary_models.is_enabled("gpt") and auxiliary_models.is_enabled("jev")
    assert "대화:" in widget.detail.text() and not widget.slider.isEnabled()
    assert "기존 모델" in widget.text() and "Jev · ON" in widget.text()
    select(widget, "gpt")
    assert pump_until(lambda: widget._codex_worker is None)
    assert auxiliary_models.is_enabled("gpt") and auxiliary_models.is_enabled("jev")
    QTest.keyClick(widget.slider, Qt.Key.Key_Left)
    assert auxiliary_models.main_selection() == ("default", True)
    assert auxiliary_models.is_enabled("jev")


@pytest.mark.parametrize("outcome", ["authenticated", "unauthenticated", "failure"])
def test_restart_preserves_saved_gpt_on_until_authentication_is_checked(picker, outcome):
    _, runtime = picker
    runtime.discover_auth = outcome == "authenticated"
    runtime.discover_error = outcome == "failure"
    runtime.release.clear()
    auxiliary_models.configure("gpt", True)
    restored = ModelSelector(codex_runtime=runtime)
    try:
        assert runtime.started.wait(1)
        assert auxiliary_models.selection() == ("gpt", True)
        assert restored.state_label.text() == "확인 중" and not restored.slider.isEnabled()
        runtime.release.set()
        assert pump_until(lambda: restored._codex_worker is None)
        assert auxiliary_models.selection() == ("gpt", outcome == "authenticated")
        assert restored.state_label.text() == ("ON" if outcome == "authenticated" else "OFF")
    finally:
        runtime.release.set()
        assert pump_until(lambda: restored._codex_worker is None)
        restored.popup.close()
        restored.close()
        restored.deleteLater()
        _APP.processEvents()
