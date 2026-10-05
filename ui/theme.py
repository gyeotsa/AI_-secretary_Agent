"""Shared desktop theme, including live changes and persistent preference."""
import re
import weakref
from PyQt6.QtCore import QObject, QEvent, QSettings, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QDialog, QMainWindow

DARK = dict(window='#212121', surface='#292929', input='#242424', text='#eeeeee',
            muted='#b0b0b0', border='#555555', accent='#b9cdfb', selected='#3c485f',
            selected_text='#ffffff', button='#303030', hover='#414141', disabled='#292929',
            disabled_text='#a6a6a6', sidebar='#191919', composer='#2b2b2b', bubble='#303030',
            success='#8cd9ae', warning='#edc878', error='#ff9ca7')
LIGHT = dict(window='#ffffff', surface='#f7f7f8', input='#ffffff', text='#202124',
             muted='#60646c', border='#c8cbd0', accent='#315da8', selected='#dce7fa',
             selected_text='#172f55', button='#f1f2f4', hover='#e5e7eb', disabled='#f0f1f3',
             disabled_text='#62666e', sidebar='#f5f5f5', composer='#ffffff', bubble='#f0f1f3',
             success='#237345', warning='#855700', error='#b42338')

_COMMON = '''
QWidget { color: TEXT; font-family: "Segoe UI"; font-size: 13px; }
QDialog, QMainWindow, QMessageBox { background: WINDOW; }
QLabel { background: transparent; }
QLabel#title, QLabel#heading { font-size: 22px; font-weight: 600; color: TEXT; }
QLabel#subtitle, QLabel#description, QLabel#muted, QLabel#metricLabel { color: MUTED; }
QFrame#panel, QFrame#card, QFrame#hero { background: SURFACE; border: 1px solid BORDER; border-radius: 12px; }
QLineEdit, QTextEdit, QPlainTextEdit, QListWidget, QTreeWidget, QTableWidget, QComboBox, QSpinBox, QDoubleSpinBox, QDateTimeEdit {
 background: INPUT; color: TEXT; border: 1px solid BORDER; border-radius: 7px; padding: 7px;
 selection-background-color: SELECTED; selection-color: SELECTED_TEXT; gridline-color: BORDER;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: ACCENT; }
QListWidget::item, QTreeWidget::item { padding: 8px; border-radius: 5px; }
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected { background: SELECTED; color: SELECTED_TEXT; }
QListWidget::item:hover, QTreeWidget::item:hover { background: HOVER; }
QPushButton, QToolButton { background: BUTTON; color: TEXT; border: 1px solid BORDER; border-radius: 7px; padding: 7px 12px; }
QPushButton:hover, QToolButton:hover { background: HOVER; }
QPushButton:pressed, QPushButton:checked { background: SELECTED; color: SELECTED_TEXT; }
QPushButton:focus, QPushButton:default { border: 2px solid ACCENT; }
QPushButton:disabled, QLineEdit:disabled, QTextEdit:disabled, QComboBox:disabled { background: DISABLED; color: DISABLED_TEXT; }
QLabel:disabled { color: DISABLED_TEXT; }
QCheckBox, QRadioButton { color: TEXT; background: transparent; spacing: 8px; }
QTabWidget { background: WINDOW; }
QTabWidget::pane { background: WINDOW; border: 1px solid BORDER; border-radius: 8px; }
QTabBar { background: SURFACE; }
QTabBar::tab { background: SURFACE; color: MUTED; border: 0; padding: 10px 16px; }
QTabBar::tab:selected { background: SELECTED; color: SELECTED_TEXT; }
QHeaderView::section { background: SURFACE; color: TEXT; border: 0; border-bottom: 1px solid BORDER; padding: 8px; }
QTableCornerButton::section { background: SURFACE; border: 0; }
QMenu, QComboBox QAbstractItemView { background: WINDOW; color: TEXT; border: 1px solid BORDER; padding: 5px; selection-background-color: SELECTED; }
QMenu::item { padding: 8px 20px; }
QMenu::item:selected { background: SELECTED; color: SELECTED_TEXT; }
QProgressBar { background: SURFACE; color: TEXT; border: 1px solid BORDER; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: ACCENT; border-radius: 4px; }
QSlider::groove:horizontal { height: 5px; background: BORDER; border-radius: 2px; }
QSlider::handle:horizontal { background: ACCENT; width: 16px; margin: -6px 0; border-radius: 8px; }
QSplitter::handle { background: SURFACE; }
QScrollArea { border: 0; background: transparent; }
QScrollBar:vertical { background: SURFACE; width: 9px; margin: 0; }
QScrollBar:horizontal { background: SURFACE; height: 9px; margin: 0; }
QScrollBar::handle { background: BORDER; border-radius: 4px; min-width: 22px; min-height: 22px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QToolTip { background: SURFACE; color: TEXT; border: 1px solid BORDER; padding: 6px; }
'''

def common_style(c):
    return _fill(_COMMON, c)

def _fill(source, colors):
    return re.sub(r'\b[A-Z_]+\b', lambda m: colors.get(m[0].lower(), m[0]), source)

def palette(colors):
    result = QPalette()
    roles = {'Window':'window', 'WindowText':'text', 'Base':'input', 'AlternateBase':'surface',
             'Text':'text', 'Button':'button', 'ButtonText':'text', 'Highlight':'selected',
             'HighlightedText':'selected_text', 'PlaceholderText':'muted', 'ToolTipBase':'surface',
             'ToolTipText':'text', 'Link':'accent', 'Light':'surface', 'Mid':'border', 'Dark':'border'}
    for group in QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled:
        for role, token in roles.items():
            if group == QPalette.ColorGroup.Disabled and token in ('text', 'muted'):
                token = 'disabled_text'
            result.setColor(group, getattr(QPalette.ColorRole, role), QColor(colors[token]))
    return result


def adapt_style(source, colors):
    """Map legacy decorative styles to semantic colors; retain spacing and typography."""
    def declaration(match):
        prop, value = match.group(1), match.group(2)
        def replace_color(m):
            color = QColor(m[0])
            if prop.startswith('selection-background'):
                token = 'selected'
            elif prop.startswith('selection-color'):
                token = 'selected_text'
            elif 'border' in prop or prop == 'gridline-color':
                token = 'border'
            elif 'background' in prop:
                token = 'surface'
            elif 'color' in prop:
                h, sat, _, _alpha = color.getHsvF()
                if sat > .4 and (h < .06 or h > .93): token = 'error'
                elif sat > .4 and .06 <= h < .18: token = 'warning'
                elif sat > .4 and .18 <= h < .46: token = 'success'
                else: token = 'text' if color.lightnessF() > .78 else 'muted'
            else:
                return m[0]
            return colors[token]
        value = re.sub(r'#[0-9a-fA-F]{6}\b', replace_color, value)
        return prop + ':' + value
    return re.sub(r'([\w-]+)\s*:([^;{}]+)', declaration, source)


class ThemeManager(QObject):
    changed = pyqtSignal(str)
    def __init__(self, app, settings=None):
        super().__init__(app)
        self.settings = settings if settings is not None else QSettings('JARVIS', 'ANIS')
        saved = self.settings.value('appearance/theme', 'dark')
        self.mode = saved if saved in ('dark', 'light') else 'dark'
        self.sources = weakref.WeakKeyDictionary()
        self.app = app
        app.installEventFilter(self)

    @property
    def colors(self):
        return LIGHT if self.mode == 'light' else DARK

    def register(self, widget, source=''):
        self.sources[widget] = source
        self.apply(widget, source)

    def apply(self, widget, source):
        from PyQt6 import sip
        if sip.isdeleted(widget): return
        c = self.colors
        root = isinstance(widget, (QDialog, QMainWindow)) or widget.property('themeShell')
        style = adapt_style(source, c)
        if root:
            style += common_style(c)
            widget.setPalette(palette(c))
            widget.setAutoFillBackground(True)
        if widget.property('themeShell'):
            from .codex_shell import shell_style
            style += shell_style(c, self.mode)
        widget.setStyleSheet(style)
        widget.update()

    def set_mode(self, mode, persist=True):
        if mode not in ('dark', 'light'): raise ValueError(mode)
        self.mode = mode
        if persist:
            self.settings.setValue('appearance/theme', mode)
            self.settings.sync()
        for widget, source in list(self.sources.items()):
            self.apply(widget, source)
        self.changed.emit(mode)
        for widget in self.app.topLevelWidgets(): widget.update()

    def toggle(self):
        self.set_mode('light' if self.mode == 'dark' else 'dark')

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and isinstance(watched, QDialog) and watched not in self.sources:
            self.register(watched)
        return False


def theme_manager():
    app = QApplication.instance()
    if app is None: raise RuntimeError('A QApplication is required')
    if not hasattr(app, '_jarvis_theme_manager'):
        app._jarvis_theme_manager = ThemeManager(app)
    return app._jarvis_theme_manager


def set_widget_style(widget, source):
    theme_manager().register(widget, source)


def theme_color(token, alpha=None):
    color = QColor(theme_manager().colors[token])
    if alpha is not None: color.setAlpha(alpha)
    return color
