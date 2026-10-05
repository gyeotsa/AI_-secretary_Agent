"""Compatibility entry point for the shared, user-selected desktop theme."""
from .theme import DARK, common_style, set_widget_style

DIALOG_COLORS = DARK
DARK_DIALOG_STYLE = common_style(DARK)


def apply_dark_dialog_theme(dialog):
    """Apply the current theme (legacy name retained for existing callers)."""
    set_widget_style(dialog, "")
