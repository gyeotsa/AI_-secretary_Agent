import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from core.gesture_runtime import (
    DEFAULT_GESTURE_MAPPING,
    GestureRuntime,
    GestureTuning,
    normalize_gesture_mapping,
    normalize_gesture_sensitivity,
)
from core.interface_control import get_interface_control_bridge
from plugins.interface_control import InterfaceControlPlugin
from ui.brain_orbit import BrainOrbitWidget
from ui.gesture_settings import GestureSettingsDialog


_APP = QApplication.instance() or QApplication([])


class _Point:
    def __init__(self, x=0.5, y=0.5):
        self.x = x
        self.y = y
        self.z = 0.0


def _thumbs_up():
    points = [_Point() for _ in range(21)]
    for tip, pip in zip((8, 12, 16, 20), (6, 10, 14, 18)):
        points[pip].y = 0.45
        points[tip].y = 0.72
    points[2].y = 0.52
    points[3].y = 0.38
    points[4].y = 0.22
    return points


def _neutral_pose():
    points = _thumbs_up()
    points[8].y = 0.20
    points[12].y = 0.20
    return points


def test_sensitivity_normalization_and_thresholds_are_monotonic():
    assert normalize_gesture_sensitivity(-10) == 0
    assert normalize_gesture_sensitivity("87") == 87
    assert normalize_gesture_sensitivity(140) == 100
    assert normalize_gesture_sensitivity("invalid") == 60

    low = GestureTuning().with_sensitivity(10)
    high = GestureTuning().with_sensitivity(90)
    assert high.swipe_enter_velocity < low.swipe_enter_velocity
    assert high.swipe_min_travel < low.swipe_min_travel
    assert high.swipe_cooldown < low.swipe_cooldown


def test_mapping_normalization_rejects_unknown_actions_without_losing_defaults():
    mapping = normalize_gesture_mapping({
        "open_palm": "toggle_chat",
        "thumbs_up": "unsafe_shell_command",
        "closed_fist": "",
    })
    assert mapping["open_palm"] == "toggle_chat"
    assert mapping["thumbs_up"] == DEFAULT_GESTURE_MAPPING["thumbs_up"]
    assert mapping["closed_fist"] == ""
    assert mapping["point_up"] == DEFAULT_GESTURE_MAPPING["point_up"]


def test_runtime_applies_mapping_live_and_requires_release_before_repeat():
    calls = []
    runtime = GestureRuntime(
        enable_command_gestures=True,
        gesture_mapping={"thumbs_up": "toggle_chat"},
        actions={"toggle_chat": lambda: calls.append("toggle_chat")},
    )
    runtime._recognize_discrete(_thumbs_up(), timestamp=1.0)
    runtime._recognize_discrete(_thumbs_up(), timestamp=1.81)
    runtime._recognize_discrete(_thumbs_up(), timestamp=3.0)
    assert calls == ["toggle_chat"]

    runtime._recognize_discrete(_neutral_pose(), timestamp=3.1)
    runtime._recognize_discrete(_thumbs_up(), timestamp=4.0)
    runtime._recognize_discrete(_thumbs_up(), timestamp=4.81)
    assert calls == ["toggle_chat", "toggle_chat"]

    updated = runtime.configure(sensitivity=91, command_gestures_enabled=False)
    assert updated["sensitivity"] == 91
    assert updated["command_gestures_enabled"] is False
    assert runtime._recognizers == []


def test_brain_response_gain_tracks_the_user_sensitivity():
    widget = BrainOrbitWidget()
    widget.set_gesture_sensitivity(0)
    low = widget._gesture_response_gain
    widget.set_gesture_sensitivity(100)
    high = widget._gesture_response_gain
    assert low < high
    assert widget._gesture_sensitivity == 100
    widget.deleteLater()


def test_gesture_dialog_previews_and_saves_complete_configuration():
    dialog = GestureSettingsDialog({
        "sensitivity": 45,
        "command_gestures_enabled": False,
        "gesture_mapping": DEFAULT_GESTURE_MAPPING,
    }, {"running": True, "backend": "MediaPipe Tasks"})
    previews = []
    saves = []
    dialog.preview_changed.connect(previews.append)
    dialog.settings_saved.connect(saves.append)
    dialog.sensitivity_slider.setValue(82)
    dialog.command_enabled.setChecked(True)
    assert previews[-1]["sensitivity"] == 82
    assert previews[-1]["command_gestures_enabled"] is True
    dialog._save()
    assert saves[-1]["sensitivity"] == 82
    assert saves[-1]["gesture_mapping"] == DEFAULT_GESTURE_MAPPING
    dialog.deleteLater()


def test_interface_plugin_changes_live_sensitivity_and_mapping_with_evidence():
    bridge = get_interface_control_bridge()
    state = {
        "sensitivity": 60,
        "command_gestures_enabled": False,
        "gesture_mapping": dict(DEFAULT_GESTURE_MAPPING),
    }

    def apply_configuration(payload):
        state.update({key: value for key, value in payload.items() if key != "persist"})
        return dict(state)

    bridge.register("set_gesture_configuration", apply_configuration)
    bridge.register("get_gesture_configuration", lambda: dict(state))
    plugin = InterfaceControlPlugin()
    try:
        slots = plugin.extract_slots(
            "interface.gesture_sensitivity", "스와이프 민감도 88퍼센트로 설정해줘", {}
        )
        assert slots == {"sensitivity": 88}
        result = plugin.execute_tool("set_gesture_sensitivity", slots)
        assert result.succeeded and state["sensitivity"] == 88

        slots = plugin.extract_slots(
            "interface.gesture_mapping", "손바닥을 펴면 채팅을 접거나 펼치도록 연결해줘", {}
        )
        assert slots == {"pose": "open_palm", "action": "toggle_chat", "enabled": True}
        result = plugin.execute_tool("set_gesture_command_mapping", slots)
        assert result.succeeded
        assert state["command_gestures_enabled"] is True
        assert state["gesture_mapping"]["open_palm"] == "toggle_chat"
        assert result.evidence
    finally:
        bridge.unregister("set_gesture_configuration")
        bridge.unregister("get_gesture_configuration")
