"""Conversation-first desktop shell; runtime actions stay owned by the window."""
from PyQt6.QtCore import Qt, pyqtSignal
from .theme import theme_manager, set_widget_style
from .window_resize import WindowResizeController
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
                            QListWidget, QListWidgetItem, QLineEdit, QTextEdit, QMenu,
                            QSizePolicy, QSizeGrip, QTabBar, QTabWidget)

STYLE = """
QWidget#jarvisShell { background: #212121; }
QWidget { color: #e5e5e5; font-family: 'Segoe UI'; font-size: 13px; }
QFrame#topBar { background: #191919; border-bottom: 1px solid #303030; border-top-left-radius: 12px; border-top-right-radius: 12px; }
QFrame#sidebar { background: #191919; border-right: 1px solid #303030; border-bottom-left-radius: 12px; }
QFrame#chatPanel { background: #212121; border: 0; border-bottom-right-radius: 12px; }
QTabWidget#workspaceTabs::pane { border: 0; background: #212121; border-bottom-right-radius: 12px; }
QTabWidget#workspaceTabs QTabBar { background: #191919; }
QTabWidget#workspaceTabs QTabBar::tab { background: #191919; color: #9b9b9b; border: 0; padding: 8px 14px; }
QTabWidget#workspaceTabs QTabBar::tab:selected { background: #2b2b2b; color: #e5e5e5; }
QTabWidget#workspaceTabs QTabBar::tab:hover { background: #353535; }
QFrame#composer { background: #2b2b2b; border: 1px solid #454545; border-radius: 18px; }
QPushButton { background: transparent; border: 0; border-radius: 7px; padding: 7px 10px; }
QPushButton:hover { background: #353535; }
QPushButton:pressed { background: #444444; }
QPushButton#nav { text-align: left; padding: 9px 12px; }
QPushButton#send { background: #eeeeee; color: #161616; border-radius: 16px; font-size: 19px; }
QPushButton#send:disabled { background: #444444; color: #888888; }
QPushButton#closeButton:hover { background: #b83240; }
QLabel#muted, QLabel#workspace, QLabel#status { color: #9b9b9b; font-size: 11px; }
QLabel#welcome { font-size: 29px; font-weight: 600; color: #ededed; }
QLabel#userMessage { background: #303030; border-radius: 14px; padding: 14px 18px; font-size: 14px; }
QLabel#assistantMessage { color: #e5e5e5; padding: 18px 8px; font-size: 14px; }
QScrollArea { background: transparent; border: 0; }
QTextEdit#commandInput { background: transparent; border: 0; padding: 7px; font-size: 14px; selection-background-color: #555555; }
QLineEdit { background: #242424; border: 1px solid #393939; border-radius: 7px; padding: 8px; selection-background-color: #555555; }
QListWidget { background: transparent; border: 0; outline: 0; }
QListWidget::item { padding: 10px 8px; border-radius: 7px; }
QListWidget::item:selected { background: #353535; }
QListWidget::item:hover { background: #292929; }
QMenu { background: #292929; border: 1px solid #454545; padding: 6px; }
QMenu::item { padding: 8px 20px; border-radius: 4px; }
QMenu::item:selected { background: #414141; }
QScrollBar:vertical { background: transparent; width: 7px; }
QScrollBar::handle:vertical { background: #505050; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QToolTip { background: #303030; color: #eeeeee; border: 1px solid #505050; padding: 6px; }
"""


def shell_style(c, mode):
    import re
    mapping = {'#e5e5e5': c['text'], '#191919': c['sidebar'], '#303030': c['bubble'],
               '#212121': c['window'], '#2b2b2b': c['composer'], '#454545': c['border'],
               '#353535': c['hover'], '#444444': c['disabled'], '#eeeeee': c['text'],
               '#161616': c['window'], '#888888': c['disabled_text'], '#9b9b9b': c['muted'],
               '#ededed': c['text'], '#555555': c['selected'], '#242424': c['input'],
               '#393939': c['border'], '#292929': c['surface'], '#414141': c['hover'],
               '#505050': c['border']}
    return re.sub(r'#[0-9a-fA-F]{6}', lambda m: mapping.get(m[0], m[0]), STYLE)


class ThemeToggleButton(QPushButton):
    def __init__(self):
        super().__init__()
        self.setAccessibleName('다크 모드 / 라이트 모드 전환')
        self.setToolTip('클릭하여 전체 창의 테마를 전환합니다')
        self.clicked.connect(theme_manager().toggle)
        theme_manager().changed.connect(self.update_theme)
        self.update_theme(theme_manager().mode)

    def update_theme(self, mode):
        self.setText('라이트 모드' if mode == 'dark' else '다크 모드')


class ComposerInput(QTextEdit):
    returnPressed = pyqtSignal()

    def text(self):
        return self.toPlainText()

    def setText(self, text):
        self.setPlainText(text)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            # Let the input method finish Korean composition before sending.
            if not getattr(self, '_composing', False):
                self.returnPressed.emit()
                return
        super().keyPressEvent(event)

    def inputMethodEvent(self, event):
        self._composing = bool(event.preeditString())
        super().inputMethodEvent(event)


def button(text, callback, parent=None):
    result = QPushButton(text, parent)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    result.clicked.connect(callback)
    return result


def install_shell(w):
    w.setObjectName('jarvisShell')
    w.setProperty('themeShell', True)
    set_widget_style(w, '')
    w.resize(1180, 800)
    w.normal_geometry = w.geometry()
    w.drag_tab.setFixedHeight(48)
    toolbar = w.drag_tab.layout()
    for i in reversed(range(toolbar.count())):
        item = toolbar.takeAt(i)
        if item.widget():
            item.widget().hide()
    w.sidebar_toggle = button('☰', lambda: w.sidebar.setVisible(w.sidebar.isHidden()))
    w.sidebar_toggle.setToolTip('사이드바 표시 / 숨기기')
    toolbar.addWidget(w.sidebar_toggle)
    w.conversation_title = QLabel('새 대화')
    toolbar.addWidget(w.conversation_title)
    toolbar.addStretch()
    w.theme_toggle_btn = ThemeToggleButton()
    toolbar.addWidget(w.theme_toggle_btn)
    toolbar.addWidget(w.status_label)
    w.status_label.setText('준비됨')
    for control in (w.minimize_btn, w.size_btn, w.close_btn):
        toolbar.addWidget(control)
        control.show()
    w.size_btn.clicked.disconnect()
    w.size_btn.clicked.connect(lambda: w.showNormal() if w.isMaximized() else w.showMaximized())
    w.size_btn.setToolTip('최대화 / 복원')

    # Reuse the existing chat scroll area, audio state and runtime signal wiring.
    for i in reversed(range(w.center_layout.count())):
        w.center_layout.takeAt(i)
    w.center_layout.setContentsMargins(0, 0, 0, 0)
    w.brain_orbit.hide()
    w.chat_toggle_btn.hide()
    body = QHBoxLayout()
    w.shell_body = body
    body.setSpacing(0)
    w.center_layout.addLayout(body)
    w.sidebar = QFrame()
    w.sidebar.setObjectName('sidebar')
    w.sidebar.setFixedWidth(252)
    side = QVBoxLayout(w.sidebar)
    side.setContentsMargins(12, 18, 12, 12)
    side.setSpacing(5)
    brand = QLabel('  JARVIS')
    brand.setStyleSheet('font-size: 16px; font-weight: 600; padding: 0 0 16px 0;')
    side.addWidget(brand)
    w.new_chat_btn = button('＋   새 대화', lambda: w.session_created.emit('새 대화'))
    for control, text in ((w.new_chat_btn, '＋   새 대화'), (w.task_btn, '☷   작업 및 리마인더'),
                          (w.specialist_btn, '◇   전문가 작업공간'), (w.mail_btn, '✉   메일 조회'),
                          (w.plugin_btn, '⊞   플러그인')):
        control.setText(text)
        control.setObjectName('nav')
        control.setMinimumSize(0, 0)
        control.setMaximumSize(16777215, 16777215)
        side.addWidget(control)
        control.show()
    side.addSpacing(22)
    label = QLabel('  프로젝트')
    label.setObjectName('muted')
    side.addWidget(label)
    w.workspace_btn.setText('＋   작업 폴더 선택')
    w.workspace_btn.setObjectName('nav')
    w.workspace_btn.setMinimumSize(0, 0)
    w.workspace_btn.setMaximumSize(16777215, 16777215)
    side.addWidget(w.workspace_btn)
    w.workspace_btn.show()
    side.addSpacing(20)
    label = QLabel('  대화')
    label.setObjectName('muted')
    side.addWidget(label)
    w.session_search = QLineEdit()
    w.session_search.setPlaceholderText('대화 검색')
    w.session_search.textChanged.connect(lambda text: filter_sessions(w, text))
    side.addWidget(w.session_search)
    w.session_list = QListWidget()
    w.session_list.itemClicked.connect(lambda item: w.session_selected.emit(item.data(Qt.ItemDataRole.UserRole)))
    side.addWidget(w.session_list, 1)
    w.session_btn.setText('대화 관리')
    w.session_btn.setMinimumSize(0, 0)
    w.session_btn.setMaximumSize(16777215, 16777215)
    side.addWidget(w.session_btn)
    w.session_btn.show()
    settings = button('⚙   설정 및 도구', lambda: None)
    settings.setObjectName('nav')
    menu = QMenu(settings)
    for name, action in [('권한 관리', w.show_permission_settings), ('목소리 설정', w.show_tts_voice_settings),
                         ('제스처 카메라 켜기 / 끄기', lambda: w.gesture_camera_requested.emit(not w.gesture_camera_running)), ('제스처 설정', w.show_gesture_settings), ('Command Center', w.show_command_center),
                         ('지식 그래프', lambda: w.open_specialist_workspace('knowledge_graph')),
                         ('미니 모드', w.toggle_window_mode)]:
        menu.addAction(name, action)
    menu.addAction('Jev 계정/API 키 관리', lambda: w.model_selector.open_jev_accounts())
    settings.setMenu(menu)
    side.addWidget(settings)
    body.addWidget(w.sidebar)

    chat = w.chat_panel.layout()
    for i in reversed(range(chat.count())):
        item = chat.takeAt(i)
        if item.widget():
            item.widget().hide()
    chat.setContentsMargins(40, 24, 40, 12)
    chat.setSpacing(14)
    w.message_scroll.setMaximumHeight(16777215)
    w.message_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    messages = w.message_scroll.widget().layout()
    messages.setContentsMargins(18, 18, 18, 18)
    messages.setSpacing(18)
    w.welcome = QLabel('무엇을 도와드릴까요?')
    w.welcome.setObjectName('welcome')
    w.welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
    w.welcome.setMinimumHeight(260)
    messages.insertWidget(0, w.welcome)
    for label in (w.user_text_label, w.assistant_text_label):
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.hide()
    chat.addWidget(w.message_scroll, 1)
    w.message_scroll.show()
    w.composer = QFrame()
    w.composer.setObjectName('composer')
    compose = QVBoxLayout(w.composer)
    compose.setContentsMargins(12, 9, 12, 9)
    old_input = w.text_input
    w.text_input = ComposerInput()
    w.text_input.setObjectName('commandInput')
    w.text_input.setAcceptRichText(False)
    w.text_input.setPlaceholderText('메시지를 입력하세요. 작업을 맡겨도 좋아요.')
    w.text_input.setFixedHeight(82)
    w.text_input.returnPressed.connect(w._on_text_submitted)
    old_input.deleteLater()
    compose.addWidget(w.text_input)
    controls = QHBoxLayout()
    folder = button('＋', w._select_workspace)
    folder.setToolTip('작업 폴더 선택')
    controls.addWidget(folder)
    controls.addWidget(w.workspace_label, 1)
    w.workspace_label.show()
    from .model_selector import ModelSelector
    w.model_selector = ModelSelector(w.composer)
    controls.addWidget(w.model_selector)
    controls.addWidget(w.voice_output_btn)
    w.voice_output_btn.show()
    w.send_btn = button('↑', w._on_text_submitted)
    w.send_btn.setObjectName('send')
    w.send_btn.setFixedSize(34, 34)
    w.send_btn.setToolTip('보내기 (Enter)')
    w.send_btn.setEnabled(False)
    w.text_input.textChanged.connect(lambda: w.send_btn.setEnabled(bool(w.text_input.text().strip())))
    controls.addWidget(w.send_btn)
    compose.addLayout(controls)
    chat.addWidget(w.composer)
    footer = QHBoxLayout()
    hint = QLabel('Enter로 보내기 · Shift+Enter로 줄바꿈')
    hint.setObjectName('muted')
    footer.addWidget(hint)
    footer.addStretch()
    footer.addWidget(QSizeGrip(w))
    chat.addLayout(footer)
    w.workspace_tabs = QTabWidget()
    w.workspace_tabs.setObjectName('workspaceTabs')
    w.workspace_tabs.setDocumentMode(True)
    w.workspace_tabs.setMovable(True)
    w.workspace_tabs.setTabsClosable(True)
    w.workspace_tabs.tabCloseRequested.connect(lambda index: close_tab(w, index))
    w.workspace_tabs.addTab(w.chat_panel, '대화')
    w.workspace_tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
    w._workspace_tab_keys = {}
    body.addWidget(w.workspace_tabs, 1)
    w.chat_panel.show()
    w._archived_messages = []
    w.text_input.setFocus()
    w.resize_controller = WindowResizeController(w)


def open_tab(w, widget, title, key):
    existing = w._workspace_tab_keys.get(key)
    index = w.workspace_tabs.indexOf(existing) if existing is not None else -1
    if index >= 0:
        w.workspace_tabs.setCurrentIndex(index)
        return existing
    index = w.workspace_tabs.addTab(widget, title)
    w._workspace_tab_keys[key] = widget
    if hasattr(widget, 'finished') and not widget.property('_workspaceTabHook'):
        widget.setProperty('_workspaceTabHook', True)
        widget.finished.connect(
            lambda _result, page=widget: close_tab(w, w.workspace_tabs.indexOf(page))
        )
    w.workspace_tabs.setCurrentIndex(index)
    widget.show()
    return widget


def close_tab(w, index):
    if not 0 <= index < w.workspace_tabs.count():
        return
    widget = w.workspace_tabs.widget(index)
    if widget is w.chat_panel:
        return
    can_close = getattr(widget, "can_close_workspace_tab", None)
    if callable(can_close) and not can_close():
        return
    key = next((name for name, value in w._workspace_tab_keys.items() if value is widget), None)
    if key:
        del w._workspace_tab_keys[key]
    w.workspace_tabs.removeTab(index)
    widget.hide()


def refresh_sessions(w):
    if not hasattr(w, 'session_list') or w.memory_manager is None:
        return
    w.session_list.clear()
    for session in w.memory_manager.list_session_details():
        item = QListWidgetItem(session['title'] or '새 대화')
        item.setData(Qt.ItemDataRole.UserRole, session['session_id'])
        item.setToolTip(session['title'])
        w.session_list.addItem(item)
        if session['session_id'] == w.current_session_id:
            w.session_list.setCurrentItem(item)
            w.conversation_title.setText(session['title'] or '새 대화')
    filter_sessions(w, w.session_search.text())


def filter_sessions(w, text):
    for index in range(w.session_list.count()):
        item = w.session_list.item(index)
        item.setHidden(text.casefold() not in item.text().casefold())


def archive_turn(w):
    layout = w.message_scroll.widget().layout()
    for source in (w.user_text_label, w.assistant_text_label):
        if source.text():
            label = QLabel(source.text())
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setObjectName(source.objectName())
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.insertWidget(layout.indexOf(w.user_text_label), label)
            w._archived_messages.append(label)
        source.clear()
        source.hide()
