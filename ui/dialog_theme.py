"""Scoped, opaque dark surfaces for dialogs opened from the ANIS toolbar.

The main window paints its own translucent background. Its text-only QWidget
rule is not a dialog theme: inheriting that rule over a native light palette
makes lists, inputs and dialog surfaces unreadable. Pair every foreground with
its background here, without changing the application's or Windows' palette.
"""
from PyQt6.QtGui import QColor, QPalette


DIALOG_COLORS = {
    "window": "#0b1320", "surface": "#111f30", "input": "#0e1927",
    "text": "#e6eef8", "muted": "#a4b6cb", "border": "#49627c",
    "accent": "#67d8ef", "selected": "#214f72", "selected_text": "#ffffff",
    "button": "#172c42", "hover": "#244967", "disabled": "#142131",
    "disabled_text": "#a4b6cb",
}

DARK_DIALOG_STYLE = """
QWidget { background-color: #0b1320; color: #e6eef8; font-size: 12px; }
QDialog, QMessageBox { background-color: #0b1320; color: #e6eef8; }
QLabel { background: transparent; color: #e6eef8; }
QLabel#dialogGuide { color: #67d8ef; padding: 6px; }
QLineEdit, QTextEdit, QPlainTextEdit, QListWidget {
    background-color: #0e1927; color: #e6eef8;
    border: 1px solid #49627c; border-radius: 6px; padding: 7px;
    selection-background-color: #214f72; selection-color: #ffffff;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QListWidget:focus {
    border-color: #67d8ef;
}
QListWidget::item { padding: 7px 5px; border-radius: 3px; }
QListWidget::item:hover { background-color: #172c42; }
QListWidget::item:selected, QListWidget::item:selected:!active {
    background-color: #214f72; color: #ffffff;
}
QPushButton {
    color: #e6eef8; background-color: #172c42; border: 1px solid #49627c;
    border-radius: 6px; padding: 7px 12px; min-height: 20px;
}
QPushButton:hover { background-color: #244967; border-color: #67d8ef; }
QPushButton:pressed { background-color: #214f72; }
QPushButton:focus, QPushButton:default { border: 2px solid #67d8ef; }
QPushButton:disabled, QLineEdit:disabled, QTextEdit:disabled, QListWidget:disabled {
    background-color: #142131; color: #a4b6cb; border-color: #49627c;
}
QLabel:disabled { color: #a4b6cb; }
QScrollBar:vertical {
    background: #0b1320; width: 12px; margin: 0;
}
QScrollBar:horizontal {
    background: #0b1320; height: 12px; margin: 0;
}
QScrollBar::handle { background: #49627c; border-radius: 5px; min-width: 22px; min-height: 22px; }
QScrollBar::handle:hover { background: #67d8ef; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: #0b1320; }
QToolTip { color: #e6eef8; background: #111f30; border: 1px solid #49627c; padding: 5px; }
"""


def apply_dark_dialog_theme(dialog):
    """Set all palette groups as well as QSS (including child message boxes)."""
    palette = QPalette(dialog.palette())
    roles = {
        QPalette.ColorRole.Window: "window", QPalette.ColorRole.WindowText: "text",
        QPalette.ColorRole.Base: "input", QPalette.ColorRole.AlternateBase: "surface",
        QPalette.ColorRole.Text: "text", QPalette.ColorRole.Button: "button",
        QPalette.ColorRole.ButtonText: "text", QPalette.ColorRole.Highlight: "selected",
        QPalette.ColorRole.HighlightedText: "selected_text",
        QPalette.ColorRole.PlaceholderText: "muted", QPalette.ColorRole.ToolTipBase: "surface",
        QPalette.ColorRole.ToolTipText: "text", QPalette.ColorRole.Link: "accent",
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for role, color in roles.items():
            palette.setColor(group, role, QColor(DIALOG_COLORS[color]))
        if group == QPalette.ColorGroup.Disabled:
            for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
                palette.setColor(group, role, QColor(DIALOG_COLORS["disabled_text"]))
            palette.setColor(group, QPalette.ColorRole.Button, QColor(DIALOG_COLORS["disabled"]))
            palette.setColor(group, QPalette.ColorRole.Base, QColor(DIALOG_COLORS["disabled"]))
    dialog.setPalette(palette)
    dialog.setAutoFillBackground(True)
    dialog.setStyleSheet(DARK_DIALOG_STYLE)
