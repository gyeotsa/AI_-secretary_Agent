"""Theme persistence, real rendered windows and edge drag regression coverage."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
import pytest
from PyQt6.QtCore import QSettings, Qt, QPoint, QPointF, QRect, QSize, QEvent
from PyQt6.QtGui import QPalette, QMouseEvent, QFontDatabase
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox, QTabWidget, QVBoxLayout, QWidget
from ui.theme import theme_manager, ThemeManager, LIGHT, DARK
from ui.main_window import (JarvisMainWindow, PermissionRequestDialog, PermissionSettingsDialog,
                            TTSVoiceDialog, SessionManagerDialog, TaskManagerDialog, PluginDiagnosticsDialog)
from ui.gesture_settings import GestureSettingsDialog
from ui.specialist_workspaces import SpecialistHubDialog, SpecialistWorkspaceWindow
from ui.command_center import FirstRunWizard
from ui.mail_account_dialog import MailAccountDialog
from ui.continuity_dialog import ContinuityDialog
from core.specialist_workspaces import get_specialist_workspace_registry
from test_continuity_ui import Service
from test_dialog_theme import Account, Registry, contrast

_APP = None

@pytest.fixture
def desktop(tmp_path):
    global _APP
    _APP = QApplication.instance() or QApplication([])
    manager = theme_manager()
    original_mode, original_settings = manager.mode, manager.settings
    manager.settings = QSettings(str(tmp_path / 'appearance.ini'), QSettings.Format.IniFormat)
    for font in ('malgun.ttf', 'segoeui.ttf'):
        QFontDatabase.addApplicationFont('C:/Windows/Fonts/' + font)
    manager.set_mode('dark', persist=False)
    yield _APP, manager
    manager.settings = original_settings
    manager.set_mode(original_mode, persist=False)


def make_window(kind, tmp_path):
    registry = get_specialist_workspace_registry()
    if kind == 'mockup':
        from ui.specialist_workspaces import MockupWorkspaceWindow
        runtime = SimpleNamespace(list_profiles=lambda: [], generation_status=lambda: {'ready':False, 'base_ready':False, 'adapter_ready':False, 'cuda':False})
        return MockupWorkspaceWindow(registry.get('mockup'), runtime=runtime)
    if kind == 'graph':
        from ui.knowledge_graph_workspace import KnowledgeGraphWindow
        from core.obsidian_vault import ObsidianVault
        return KnowledgeGraphWindow(vault=ObsidianVault(tmp_path / 'vault'))
    if kind == 'command':
        from ui.command_center import CommandCenterDialog
        data = dict(updated_at='검증 화면', workspace={}, contracts=[], dialogue_tasks=[], teams=[], plan_steps=[],
                    system={}, gpu={}, plugins=[], permissions=[], diagnostics={'summary':{},'probes':[]},
                    models=[], automation={'scheduler':{},'jobs':[]}, observer={}, workflow_runs=[], actions=[],
                    artifacts=[], acceptance={'scenarios':[]}, events=[])
        return CommandCenterDialog(SimpleNamespace(snapshot=lambda: data))
    if kind == 'main': return JarvisMainWindow()
    if kind == 'permission': return PermissionRequestDialog('파일 수정', '요청한 문서를 수정합니다.')
    if kind == 'permissions': return PermissionSettingsDialog(SimpleNamespace(get_all_permissions=lambda: []))
    if kind == 'voice': return TTSVoiceDialog(SimpleNamespace(list_voices=lambda **kw: [], selected_voice_name='한국어'))
    if kind == 'sessions': return SessionManagerDialog(SimpleNamespace(list_session_details=lambda: []), '')
    if kind == 'tasks': return TaskManagerDialog(SimpleNamespace(list_tasks=lambda *a, **kw: []), '', '')
    if kind == 'plugins': return PluginDiagnosticsDialog(Registry())
    if kind == 'mail': return MailAccountDialog(Account())
    if kind == 'gesture': return GestureSettingsDialog()
    if kind == 'hub': return SpecialistHubDialog(registry.all())
    if kind in ('document', 'photoshop'): return SpecialistWorkspaceWindow(registry.get(kind))
    if kind == 'setup': return FirstRunWizard(SimpleNamespace())
    if kind == 'reminder': return ContinuityDialog(Service())
    raise AssertionError(kind)


@pytest.mark.parametrize('kind', ['main','permission','permissions','voice','sessions','tasks',
                                  'plugins','mail','gesture','hub','document','photoshop','setup','reminder','mockup','graph','command'])
def test_all_windows_switch_live_and_render(desktop, tmp_path, kind):
    app, manager = desktop
    window = make_window(kind, tmp_path)
    try:
        window.show()
        for mode, colors in [('light', LIGHT), ('dark', DARK), ('light', LIGHT)]:
            manager.set_mode(mode, persist=False)
            for _ in range(3): app.processEvents()
            assert window.palette().color(QPalette.ColorRole.Window).name() == colors['window']
            assert window.grab().save(str(tmp_path / f'{kind}-{mode}.png'))
            if kind == 'main':
                assert window.theme_toggle_btn.text() == ('다크 모드' if mode == 'light' else '라이트 모드')
            else:
                assert window.grab().toImage().pixelColor(2,2).name() == colors['window']
        # A new dialog also inherits the selected theme without reopening the owner.
        box = QMessageBox(QMessageBox.Icon.Information, '알림', '테마 확인', parent=window)
        box.show(); app.processEvents()
        assert box.palette().color(QPalette.ColorRole.Window).name() == LIGHT['window']
        box.close(); box.deleteLater()
    finally:
        window.close(); window.deleteLater(); app.processEvents()


@pytest.mark.parametrize('colors', [LIGHT, DARK])
def test_both_theme_contrasts(colors):
    for foreground, background in [('text','window'),('muted','surface'),('disabled_text','disabled'),
                                   ('selected_text','selected'),('accent','window'),('error','window'),
                                   ('success','window'),('warning','window')]:
        assert contrast(colors[foreground], colors[background]) >= 4.5


@pytest.mark.parametrize('shell', [False, True])
def test_tab_bar_unused_space_and_pane_switch_theme(desktop, tmp_path, shell):
    app, manager = desktop
    window = JarvisMainWindow() if shell else QDialog()
    if shell:
        tabs = window.workspace_tabs
    else:
        tabs = QTabWidget(window)
        tabs.setDocumentMode(True)
        tabs.addTab(QWidget(), '대화')
        QVBoxLayout(window).addWidget(tabs)
        window.resize(640, 360)
    try:
        window.show()
        for mode, colors in [('dark', DARK), ('light', LIGHT), ('dark', DARK)]:
            manager.set_mode(mode, persist=False)
            for _ in range(3): app.processEvents()
            bar = tabs.tabBar()
            tab_end = bar.tabRect(bar.count() - 1).right()
            assert bar.width() - tab_end > 100  # Sample the empty strip, not a tab.
            unused = bar.grab().toImage().pixelColor((tab_end + bar.width()) // 2, bar.height() // 2)
            assert tabs.grab().save(str(tmp_path / f'tabs-{shell}-{mode}.png'))
            assert unused.name() == colors['sidebar' if shell else 'surface']
            pane = tabs.grab().toImage().pixelColor(tabs.width() - 20, tabs.height() // 2)
            assert pane.name() == colors['window']
    finally:
        window.close(); window.deleteLater(); app.processEvents()


def test_toggle_persists_and_reloads_without_changing_conversation(desktop):
    app, manager = desktop
    window = JarvisMainWindow()
    window.show_user_text('유지할 질문'); window.text_input.setText('작성 중')
    window.theme_toggle_btn.click()
    assert manager.mode == 'light'
    assert manager.settings.value('appearance/theme') == 'light'
    restored = ThemeManager(app, manager.settings)
    assert restored.mode == 'light'
    app.removeEventFilter(restored); restored.deleteLater()
    assert window.user_text_label.text() == '유지할 질문'
    assert window.text_input.text() == '작성 중'
    window.close(); window.deleteLater()


@pytest.mark.parametrize('index', range(8))
def test_drag_each_edge_or_corner_changes_geometry(desktop, index):
    from ui.window_resize import resized_geometry
    app, manager = desktop
    window = JarvisMainWindow()
    window.setGeometry(100, 100, 1180, 800)
    app.processEvents()
    handle = window.resize_controller.handles[index]
    old = window.geometry()
    # offscreen Qt has no system resize loop; send real press/move/release events.
    origin = handle.mapToGlobal(handle.rect().center())
    local = QPointF(handle.rect().center())
    for event_type, point, button, buttons in [
        (QEvent.Type.MouseButtonPress, origin, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseMove, origin+QPoint(40,30), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseButtonRelease, origin+QPoint(40,30), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)]:
        QApplication.sendEvent(handle, QMouseEvent(event_type, local, QPointF(point), button, buttons, Qt.KeyboardModifier.NoModifier))
    expected = resized_geometry(old, QPoint(40,30), handle.edges,
        window.minimumSize().expandedTo(window.minimumSizeHint()), window.maximumSize())
    assert window.geometry() == expected
    assert window.geometry() != old
    window.showMaximized(); app.processEvents()
    assert all(h.isHidden() for h in window.resize_controller.handles)
    window.showNormal(); app.processEvents()
    assert all(not h.isHidden() for h in window.resize_controller.handles)
    window.close(); window.deleteLater()


def test_resize_clamps_and_keeps_opposite_corner_fixed():
    from ui.window_resize import resized_geometry
    old = QRect(100,100,1000,700)
    result = resized_geometry(old, QPoint(2000,2000), Qt.Edge.LeftEdge|Qt.Edge.TopEdge, QSize(600,400), QSize(2000,2000))
    assert result.size() == QSize(600,400)
    assert result.bottomRight() == old.bottomRight()


def test_graph_labels_and_dynamic_status_follow_theme(desktop):
    from ui.knowledge_graph_workspace import NativeGraphView
    from PyQt6.QtWidgets import QGraphicsSimpleTextItem
    app, manager = desktop
    view = NativeGraphView(SimpleNamespace())
    view.set_graph({'nodes':[{'id':'one','label':'기억 노드','type':'fact','relative_path':'one.md'}], 'edges':[]})
    voice = make_window('voice', None)
    for mode, colors in [('light', LIGHT), ('dark', DARK)]:
        manager.set_mode(mode, persist=False)
        voice._refresh_backend_status()
        assert view.backgroundBrush().color().name() == colors['window']
        labels = [item for item in view.scene().items() if isinstance(item, QGraphicsSimpleTextItem)]
        assert labels and all(item.brush().color().name() == colors['text'] for item in labels)
        assert colors['success'] in voice.backend_status_label.styleSheet()
    voice.close(); voice.deleteLater(); view.close(); view.deleteLater()
