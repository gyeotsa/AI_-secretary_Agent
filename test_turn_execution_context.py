import threading
import time
import os
from types import SimpleNamespace

import pytest

from core.executor import Executor, ExecutionOutcome
from core.turn_context import TurnExecutionContext, bind_turn_context, current_turn_context, check_turn_cancelled
from core.plugin import ToolCancelledError


def test_queued_old_turn_never_starts_after_new_input():
    executor = Executor.__new__(Executor)
    executor._turn_lock = threading.RLock()
    entered = threading.Event()
    release = threading.Event()
    executed = []
    outcomes = {}

    def execute(goal, *args, **kwargs):
        executed.append(goal)
        if goal == 'A':
            entered.set()
            assert release.wait(5)
        check_turn_cancelled()
        return ExecutionOutcome(goal, 'completed')

    executor._execute_turn_traced = execute
    contexts = {s: TurnExecutionContext(s, 'session') for s in 'ABC'}
    threads = []
    def run(key):
        outcomes[key] = executor.execute_turn(key, turn_context=contexts[key])
    first = threading.Thread(target=run, args=('A',))
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=run, args=('B',))
    second.start()
    contexts['A'].cancel()
    contexts['B'].cancel()
    third = threading.Thread(target=run, args=('C',))
    third.start()
    release.set()
    for thread in (first, second, third):
        thread.join(5)
        assert not thread.is_alive()
    assert executed == ['A', 'C']
    assert outcomes['A'].status == outcomes['B'].status == 'cancelled'
    assert outcomes['C'].status == 'completed'


def test_captured_session_and_workspace_override_mutable_runtime():
    executor = Executor.__new__(Executor)
    executor._turn_lock = threading.RLock()
    context = TurnExecutionContext('turn', 'original-session', 'C:/qa/original')
    seen = []
    def execute(goal, session, *args):
        seen.append((session, executor._workspace_scope()))
        return ExecutionOutcome('ok')
    executor._execute_turn_traced = execute
    executor.execute_turn('read', 'later-session', turn_context=context)
    assert seen == [('original-session', 'C:/qa/original')]
    assert current_turn_context() is None


def test_cancel_check_rejects_tool_boundary_and_resets_context():
    context = TurnExecutionContext('turn', 's')
    with bind_turn_context(context):
        context.cancel()
        try:
            check_turn_cancelled()
        except ToolCancelledError:
            pass
        else:
            raise AssertionError('cancelled turn could dispatch a tool')
    assert current_turn_context() is None


def test_tool_timeout_token_does_not_cancel_parent_or_sibling():
    parent = TurnExecutionContext('turn', 's')
    first, second = parent.tool_token(), parent.tool_token()
    first.cancel()
    assert first.cancelled and not parent.cancelled and not second.cancelled
    parent.cancel()
    assert first.cancelled and second.cancelled


def test_cancelled_turn_never_requests_tool_permission():
    from core.tools import ToolExecutor
    executor = ToolExecutor.__new__(ToolExecutor)
    called = []
    executor._request_tool_permissions = lambda *args: called.append(args)
    context = TurnExecutionContext('turn', 's')
    with bind_turn_context(context):
        context.cancel()
        with pytest.raises(ToolCancelledError):
            executor.execute_tool('anything', {})
    assert called == []


def _isolated_tool_executor(monkeypatch, registry):
    from core.tools import ToolExecutor
    import core.productization as product
    import core.quality_metrics as quality
    sink = SimpleNamespace(record=lambda *a, **k: None, emit=lambda *a, **k: None,
                           increment=lambda *a, **k: None, observe=lambda *a, **k: None)
    monkeypatch.setattr(product, 'SafeModeManager', lambda: SimpleNamespace(enabled=lambda: False))
    monkeypatch.setattr(product, 'TRACE', sink)
    monkeypatch.setattr(product, 'METRICS', sink)
    monkeypatch.setattr(quality, 'get_quality_metric_store', lambda: sink)
    executor = ToolExecutor.__new__(ToolExecutor)
    executor.plugin_registry = registry
    executor._request_tool_permissions = lambda *args: (True, '')
    executor._record_tool_run = lambda *args: None
    executor._adapt_tool_output = lambda _name, _input, output, _duration: output
    return executor


def test_permission_response_cannot_revive_cancelled_turn(monkeypatch):
    dispatched = []
    registry = SimpleNamespace(get_capability=lambda _: None,
                               execute_tool=lambda *a, **k: dispatched.append(a))
    executor = _isolated_tool_executor(monkeypatch, registry)
    context = TurnExecutionContext('turn', 's')
    def allow(*_):
        context.cancel()
        return True, ''
    executor._request_tool_permissions = allow
    with bind_turn_context(context), pytest.raises(ToolCancelledError):
        executor.execute_tool('test_send', {})
    assert dispatched == []


@pytest.mark.parametrize('uncertain', [False, True])
def test_late_cancellation_preserves_observed_tool_receipt_but_blocks_next_tool(monkeypatch, uncertain):
    from core.tool_result import Evidence, ToolRunResult, ToolRunStatus
    context = TurnExecutionContext('late-cancel', 's')
    receipt = ToolRunResult.successful(tool_name='test_send', raw_output='observed',
                                      evidence=[Evidence('send_receipt', 'synthetic observation')])
    if uncertain:
        receipt.status = ToolRunStatus.UNVERIFIED
    calls = []
    def send(*args, **kwargs):
        calls.append(args)
        context.cancel()
        return receipt
    registry = SimpleNamespace(get_capability=lambda _: None, execute_tool=send)
    executor = _isolated_tool_executor(monkeypatch, registry)
    with bind_turn_context(context):
        assert executor.execute_tool('test_send', {}) is receipt
        with pytest.raises(ToolCancelledError):
            executor.execute_tool('next', {})
    assert len(calls) == 1


def test_real_plugin_worker_observes_turn_cancel_without_followup_dispatch(monkeypatch):
    from core.plugin import BasePlugin, PluginRegistry, ToolSchema
    started = threading.Event()
    worker_stopped = threading.Event()
    effects = []
    class WaitingPlugin(BasePlugin):
        def __init__(self):
            super().__init__()
            self.name = 'turn_cancellation_test'
        def get_tools(self):
            return [ToolSchema(name='turn_test_read', description='isolated QA reader',
                               cancellable=True, timeout_seconds=5,
                               input_schema={'type': 'object'},
                               output_schema={'type': 'object'})]
        def execute_tool(self, name, values):
            context = self.get_execution_context()
            started.set()
            try:
                for _ in range(500):
                    context.raise_if_cancelled()
                    time.sleep(.002)
                effects.append('unexpected publication')
                return {}
            finally:
                worker_stopped.set()
    registry = PluginRegistry()
    registry.register_plugin(WaitingPlugin())
    tools = _isolated_tool_executor(monkeypatch, registry)
    executor = Executor.__new__(Executor)
    def execute(*_args):
        tools.execute_tool('turn_test_read', {})
        effects.append('unexpected followup')
        return ExecutionOutcome('done')
    executor._execute_turn_traced = execute
    context = TurnExecutionContext('turn', 's')
    results = []
    thread = threading.Thread(target=lambda: results.append(executor.execute_turn('read', turn_context=context)))
    thread.start()
    try:
        assert started.wait(3)
        context.cancel()
        thread.join(3)
        assert not thread.is_alive()
        assert worker_stopped.wait(1)
        assert results[0].status == 'cancelled'
        assert effects == []
    finally:
        context.cancel()
        thread.join(3)
        registry.shutdown()


@pytest.mark.parametrize('asynchronous', [False, True])
def test_reused_plugin_worker_inherits_only_its_callers_turn(asynchronous):
    from concurrent.futures import ThreadPoolExecutor
    from core.plugin import BasePlugin, PluginRegistry, ToolSchema

    observed = []
    class ProbePlugin(BasePlugin):
        def __init__(self):
            super().__init__()
            self.name = 'turn_context_probe'
        def get_tools(self):
            return [ToolSchema('turn_context_read', 'worker context QA',
                               output_schema={'type': 'object'})]
        def execute_tool(self, _name, _values):
            def observe():
                observed.append((threading.get_ident(), current_turn_context()))
                return {}
            async def async_observe():
                return observe()
            return async_observe() if asynchronous else observe()

    registry = PluginRegistry()
    registry._executor.shutdown()
    registry._executor = ThreadPoolExecutor(max_workers=1)
    registry.register_plugin(ProbePlugin())
    first = TurnExecutionContext('first', 'session-first', 'C:/qa/first')
    second = TurnExecutionContext('second', 'session-second', 'C:/qa/second')
    try:
        for context in (first, second, None):
            with bind_turn_context(context):
                assert registry.execute_tool('turn_context_read', {}) == {}
        assert [context for _, context in observed] == [first, second, None]
        assert len({thread_id for thread_id, _ in observed}) == 1
        assert observed[0][0] != threading.get_ident()
        assert current_turn_context() is None
    finally:
        assert registry.shutdown()['remaining_execution_ids'] == []


@pytest.mark.parametrize('workspace_captured', [True, False])
def test_real_filesystem_worker_uses_captured_workspace_not_later_selection(
        monkeypatch, tmp_path, workspace_captured):
    from core.plugin import PluginRegistry
    from core.tool_result import ToolRunStatus
    import plugins.filesystem as filesystem

    original, later = tmp_path / 'original', tmp_path / 'later'
    original.mkdir()
    later.mkdir()
    # No persisted user workspace is read or modified by this fixture.
    workspace = SimpleNamespace(get_workspace_path=lambda: str(later))
    monkeypatch.setattr(filesystem, 'get_workspace_manager', lambda: workspace)
    registry = PluginRegistry()
    registry.register_plugin(filesystem.FilesystemPlugin())
    context = TurnExecutionContext('frozen', 'session', str(original) if workspace_captured else '')
    try:
        with bind_turn_context(context):
            result = registry.execute_tool('filesystem_create_file', {
                'filename': 'captured.txt', 'content': 'original turn',
            }, cancellation_token=context.tool_token())
        assert not (later / 'captured.txt').exists()
        if workspace_captured:
            assert result.status == ToolRunStatus.SUCCEEDED
            assert (original / 'captured.txt').read_text(encoding='utf-8') == 'original turn'
        else:
            assert result.status == ToolRunStatus.FAILED
            assert not (original / 'captured.txt').exists()
    finally:
        assert registry.shutdown()['remaining_execution_ids'] == []


def test_real_filesystem_worker_checks_turn_cancel_after_generation(monkeypatch, tmp_path):
    from core.plugin import PluginRegistry
    from core.tool_result import ToolRunStatus
    import plugins.filesystem as filesystem

    target = tmp_path / 'existing.txt'
    target.write_text('original bytes', encoding='utf-8')
    workspace = SimpleNamespace(get_workspace_path=lambda: str(tmp_path))
    monkeypatch.setattr(filesystem, 'get_workspace_manager', lambda: workspace)
    plugin = filesystem.FilesystemPlugin()
    started, release, worker_stopped = threading.Event(), threading.Event(), threading.Event()
    observed = []
    def generate(_target, _instruction):
        observed.append(current_turn_context())
        started.set()
        assert release.wait(5)
        return 'cancelled replacement'
    monkeypatch.setattr(plugin, '_generate_file_content', generate)
    original_execute = plugin.execute_tool
    def execute(*args):
        try:
            return original_execute(*args)
        finally:
            worker_stopped.set()
    monkeypatch.setattr(plugin, 'execute_tool', execute)
    registry = PluginRegistry()
    registry.register_plugin(plugin)
    context = TurnExecutionContext('cancel-during-generation', 'session', str(tmp_path))
    results = []
    def invoke():
        with bind_turn_context(context):
            results.append(registry.execute_tool('filesystem_write_file', {
                'filename': target.name, 'instruction': 'replace the contents',
            }, cancellation_token=context.tool_token()))
    caller = threading.Thread(target=invoke)
    caller.start()
    try:
        assert started.wait(3)
        context.cancel()
        release.set()
        caller.join(3)
        assert not caller.is_alive()
        assert worker_stopped.wait(3)
        assert observed == [context]
        assert results[0].status == ToolRunStatus.CANCELLED
        assert target.read_text(encoding='utf-8') == 'original bytes'
    finally:
        context.cancel()
        release.set()
        caller.join(3)
        assert registry.shutdown()['remaining_execution_ids'] == []


def _qt_runtime(monkeypatch):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    import main_qt
    from PyQt6.QtWidgets import QApplication
    from ui.main_window import JarvisMainWindow
    qt = QApplication.instance() or QApplication([])
    settings = SimpleNamespace(assistant_name='Anis QA', wake_word='', tts_enabled=False,
                               get=lambda key: '자연스러운 존댓말로 대답' if key == 'response_style' else '')
    monkeypatch.setattr(main_qt, 'get_assistant_settings', lambda: settings)
    app = main_qt.JarvisApp.__new__(main_qt.JarvisApp)
    app.window = JarvisMainWindow()
    app.window.show()
    app.state_machine = SimpleNamespace(start_listening=lambda: None, start_processing=lambda: None,
                                        start_responding=lambda: None, go_idle=lambda: None)
    app.assistant_settings = settings
    app.session_id = 'qa-session'
    app.workspace_manager = None
    app.memory = SimpleNamespace(workspace_namespace='qa-memory', save_message=lambda *args: None)
    app.messages = []
    app.last_response = ''
    app._is_processing_ai = False
    app._archive_obsidian_exchange_async = lambda *a, **kw: None
    app._consolidate_memory_async = lambda *a, **kw: None
    app._reset_all = lambda: None
    app.signals = main_qt.AppSignals()
    app.signals.ai_response_ready.connect(app._on_ai_response)
    app.signals.progress_update.connect(app._on_progress_update)
    return qt, app


def _pump(qt, predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


def test_qt_worker_roundtrip_only_latest_input_updates_actual_window(monkeypatch):
    import main_qt
    qt, app = _qt_runtime(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    executed = []
    executor = Executor.__new__(Executor)
    executor._turn_lock = threading.RLock()
    executor.current_agent_task_id = ''
    executor.is_control_command = lambda _: False
    def execute(goal, _session, _history, progress, *_):
        executed.append(goal)
        if goal == 'A':
            entered.set()
            assert release.wait(4)
        check_turn_cancelled()
        progress('진행 ' + goal)
        return ExecutionOutcome('최종 ' + goal)
    executor._execute_turn_traced = execute
    app.executor = executor
    try:
        app._on_user_input('A')
        assert entered.wait(3)
        app._on_user_input('B')
        cancelled_b = app._active_turn_context
        app._on_user_input('C')
        release.set()
        assert _pump(qt, lambda: not app._is_processing_ai)
        assert cancelled_b.cancelled
        assert executed == ['A', 'C']
        assert app.window.assistant_text_label.text() == '최종 C'
        assert [item['content'] for item in app.messages if item['role'] == 'assistant'] == ['최종 C']
    finally:
        release.set()
        app.window._allow_close = True
        app.window.close()
        qt.processEvents()


def test_qt_stale_progress_and_control_do_not_change_current_window(monkeypatch):
    import main_qt
    qt, app = _qt_runtime(monkeypatch)
    app._active_turn_id = 'current'
    app.window.show_assistant_text('현재 화면')
    try:
        old = main_qt.TurnEnvelope('old', 'qa-session', 'old', ())
        app.signals.progress_update.emit(main_qt.TurnProgress(old, '오래된 진행'))
        app._on_control_response(main_qt.ControlResult('other-session', 'current', ExecutionOutcome('오래된 제어')))
        qt.processEvents()
        assert app.window.assistant_text_label.text() == '현재 화면'
        current = main_qt.TurnEnvelope('current', 'qa-session', 'current', ())
        app.signals.progress_update.emit(main_qt.TurnProgress(current, '정상 진행'))
        qt.processEvents()
        assert app.window.assistant_text_label.text() == '정상 진행'
    finally:
        app.window._allow_close = True
        app.window.close()


def test_qt_final_code_and_quoted_address_remain_exact(monkeypatch):
    import main_qt
    qt, app = _qt_runtime(monkeypatch)
    app.tool_executor = SimpleNamespace(tts_settings=SimpleNamespace(
        personalize_address=lambda value: value.replace('보스', '지휘관님')))
    app._active_turn_id = 'source'
    source = '보스, 아래가 원문입니다.\n```python\nx = [1, 2]\nmessage = "보스, 보내주세요"\n```\n본문: 보스, 보내주세요'
    expected = source.replace('보스, 아래가', '지휘관님, 아래가')
    try:
        turn = main_qt.TurnEnvelope('source', app.session_id, '원문과 코드를 보여줘', ())
        app.signals.ai_response_ready.emit(main_qt.TurnResult(turn, source))
        qt.processEvents()
        assert app.window.assistant_text_label.text() == expected
        assert app.last_response == expected
    finally:
        app.window._allow_close = True
        app.window.close()
