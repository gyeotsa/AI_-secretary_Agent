"""Render toolbar dialogs against a light desktop palette, without real accounts."""
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette, QRawFont
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox, QStyleFactory, QWidget

from ui.dialog_theme import DIALOG_COLORS
from ui.mail_account_dialog import MailAccountDialog
from ui.main_window import MAIN_STYLE, PluginDiagnosticsDialog


_APP = None


class Account:
    def status(self):
        return {"configured": False, "username": "", "verified_at": None}


class Registry:
    def get_plugin_statuses(self):
        return [SimpleNamespace(
            name=name, version="1.0", registered=True, installed=True,
            installation_state="confirmed", connected=None, connection_state="unchecked",
            authenticated=None, authentication_state="unchecked", runtime_state="unchecked",
            verified=None, verification_state="unchecked", contract_state="confirmed",
            enabled=True, last_tool="", runtime_summary="아직 실제 실행하지 않았습니다.",
            runtime_evidence=[], diagnostics=["계정을 연결한 뒤 별도로 인증을 확인하세요."],
        ) for name in ("네이버 메일", "브라우저", "파일 작업")]


@pytest.fixture(params=[key for key in QStyleFactory.keys() if key.lower() in {"fusion", "windows"}])
def desktop(request):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    app = _APP
    original_palette, original_style, original_qss = QPalette(app.palette()), app.style().objectName(), app.styleSheet()
    original_font = QFont(app.font())
    loaded_fonts = []
    # Windows offscreen Qt does not enumerate desktop fonts. Load real fonts
    # into this test process only, so screenshots verify text rather than tofu.
    if not QFontDatabase.families():
        for name in ("malgun.ttf", "segoeui.ttf"):
            path = Path("C:/Windows/Fonts") / name
            if path.is_file():
                font_id = QFontDatabase.addApplicationFont(str(path))
                if font_id >= 0:
                    loaded_fonts.append(font_id)
        if "Malgun Gothic" in QFontDatabase.families():
            app.setFont(QFont("Malgun Gothic", 9))
    if os.name == "nt":
        assert QRawFont.fromFont(QFont("Malgun Gothic", 9)).supportsCharacter("가"), "Korean render QA needs real glyphs"
    app.setStyleSheet("")
    app.setStyle(QStyleFactory.create(request.param))
    light = QPalette()
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
            light.setColor(group, role, QColor("#ffffff"))
        for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
            light.setColor(group, role, QColor("#111111"))
    app.setPalette(light)
    parent = QWidget()
    parent.setStyleSheet(MAIN_STYLE)
    dialogs = []

    def create(kind="plugin", *, parented=True):
        owner = parent if parented else None
        dialog = PluginDiagnosticsDialog(Registry(), owner) if kind == "plugin" else MailAccountDialog(Account(), owner)
        dialogs.append(dialog)
        dialog.show()
        app.processEvents()
        return dialog

    yield app, create, parent, request.param
    for dialog in reversed(dialogs):
        dialog.close()
        dialog.deleteLater()
    parent.close()
    parent.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.setStyle(QStyleFactory.create(original_style))
    app.setPalette(original_palette)
    app.setStyleSheet(original_qss)
    app.setFont(original_font)
    for font_id in loaded_fonts:
        QFontDatabase.removeApplicationFont(font_id)


def contrast(a, b):
    def luminance(value):
        c = QColor(value)
        channels = (c.redF(), c.greenF(), c.blueF())
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("foreground,background", [
    ("text", "window"), ("text", "input"), ("text", "button"), ("text", "hover"),
    ("selected_text", "selected"), ("accent", "window"), ("muted", "input"),
    ("disabled_text", "disabled"), ("text", "surface"),
])
def test_theme_text_contrast(foreground, background):
    assert contrast(DIALOG_COLORS[foreground], DIALOG_COLORS[background]) >= 4.5


@pytest.mark.parametrize("kind", ["plugin", "mail"])
@pytest.mark.parametrize("parented", [True, False])
def test_light_desktop_and_inherited_main_style_have_opaque_dark_dialogs(desktop, kind, parented):
    app, create, parent, _style = desktop
    app_palette, parent_style = QPalette(app.palette()), parent.styleSheet()
    dialog = create(kind, parented=parented)
    image = dialog.grab().toImage()
    assert image.pixelColor(2, 2).name() == DIALOG_COLORS["window"]
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        palette = dialog.palette()
        assert palette.color(group, QPalette.ColorRole.Window).name() == DIALOG_COLORS["window"]
        assert contrast(palette.color(group, QPalette.ColorRole.WindowText), palette.color(group, QPalette.ColorRole.Window)) >= 4.5
    assert app.palette() == app_palette
    assert parent.styleSheet() == parent_style


def test_plugin_list_details_and_inactive_selection_are_readable(desktop, tmp_path):
    app, create, _parent, style = desktop
    dialog = create()
    assert dialog.plugin_list.count() == 3
    dialog.plugin_list.setCurrentRow(1)
    assert "브라우저" in dialog.details.toPlainText()
    dialog.details.setFocus()  # List selection must stay legible when it loses focus.
    app.processEvents()
    path = tmp_path / f"plugin-diagnostics-{style}.png"
    assert dialog.grab().save(str(path))
    print(f"UI_RENDER={path}")
    image = dialog.plugin_list.viewport().grab().toImage()
    selected_rect = dialog.plugin_list.visualItemRect(dialog.plugin_list.item(1)).intersected(dialog.plugin_list.viewport().rect())
    # Rows may extend past the viewport horizontally; sample visible padding,
    # not an out-of-image coordinate (which Qt reports as black).
    assert image.pixelColor(selected_rect.center().x(), selected_rect.top() + 3).name() == DIALOG_COLORS["selected"]
    detail_image = dialog.details.viewport().grab().toImage()
    assert detail_image.pixelColor(detail_image.width() - 10, 10).name() == DIALOG_COLORS["input"]
    for widget in (dialog.plugin_list, dialog.details):
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            palette = widget.palette()
            assert contrast(palette.color(group, QPalette.ColorRole.Text), palette.color(group, QPalette.ColorRole.Base)) >= 4.5
            assert contrast(palette.color(group, QPalette.ColorRole.HighlightedText), palette.color(group, QPalette.ColorRole.Highlight)) >= 4.5


def test_mail_placeholder_password_and_disabled_actions_remain_readable(desktop, tmp_path):
    app, create, _parent, style = desktop
    dialog = create("mail")
    assert not dialog.save_button.isEnabled() and not dialog.verify_button.isEnabled()
    for field in (dialog.username_input, dialog.password_input):
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
            palette = field.palette()
            assert contrast(palette.color(group, QPalette.ColorRole.PlaceholderText), palette.color(group, QPalette.ColorRole.Base)) >= 4.5
    for button in (dialog.save_button, dialog.verify_button, dialog.cancel_button):
        palette = button.palette()
        group = QPalette.ColorGroup.Disabled
        assert contrast(palette.color(group, QPalette.ColorRole.ButtonText), palette.color(group, QPalette.ColorRole.Button)) >= 4.5
    path = tmp_path / f"mail-account-{style}.png"
    assert dialog.grab().save(str(path))
    print(f"UI_RENDER={path}")
    dialog.username_input.setText("fixture@naver.com")
    dialog.password_input.setText("synthetic-not-a-real-password")
    app.processEvents()
    assert dialog.save_button.isEnabled()
    assert "synthetic" not in dialog.password_input.displayText()
    dialog.password_input.setEnabled(False)
    palette = dialog.password_input.palette()
    assert contrast(palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text), palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base)) >= 4.5


@pytest.mark.parametrize("kind", ["plugin", "mail"])
def test_real_static_confirmation_dialog_inherits_dark_theme(desktop, kind, tmp_path):
    app, create, _parent, style = desktop
    dialog = create(kind)
    observed = []
    timer = QTimer(dialog)
    timer.setInterval(10)
    watchdog = QTimer(dialog)
    watchdog.setSingleShot(True)

    def inspect_and_close():
        box = app.activeModalWidget()
        if not isinstance(box, QMessageBox) or box.parentWidget() is not dialog:
            return
        timer.stop()
        image = box.grab().toImage()
        labels = [label for label in box.findChildren(QLabel) if label.text()]
        observed.append((image.pixelColor(2, 2).name(), [
            contrast(label.palette().color(QPalette.ColorRole.WindowText), box.palette().color(QPalette.ColorRole.Window))
            for label in labels
        ]))
        path = tmp_path / f"confirmation-{kind}-{style}.png"
        box.grab().save(str(path))
        print(f"UI_RENDER={path}")
        box.button(QMessageBox.StandardButton.No).click()

    def close_owned_boxes():
        timer.stop()
        # Fail with missing observations rather than hanging on a timing issue.
        # Never close unrelated dialogs belonging to other tests/apps.
        for box in dialog.findChildren(QMessageBox):
            box.reject()

    timer.timeout.connect(inspect_and_close)
    watchdog.timeout.connect(close_owned_boxes)
    timer.start()
    watchdog.start(1500)
    # Real Qt static helper, as used by local disconnect; no service action.
    result = QMessageBox.question(dialog, "로컬 연결 해제", "이 앱에서 연결을 해제할까요?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    timer.stop()
    watchdog.stop()
    assert result == QMessageBox.StandardButton.No
    assert observed and observed[0] is not None
    background, ratios = observed[0]
    assert background == DIALOG_COLORS["window"]
    assert ratios and min(ratios) >= 4.5
