import time

import pytest

from core.dialogue_state import DialogueStateStore
from core.executor import Executor, ExecutionOutcome
from core.quality_metrics import QualityMetricStore


def runtime(tmp_path):
    executor = Executor.__new__(Executor)
    executor.dialogue_state = DialogueStateStore(str(tmp_path / 'dialogue.db'))
    executor.quality_metrics = QualityMetricStore(str(tmp_path / 'quality.db'))
    return executor


def completed(executor, status='completed'):
    store = executor.dialogue_state
    task = store.create_task('s', '회의 시간을 오후 3시로 기록', workspace_path='workspace')
    store.transition_task(task.task_id, 'running')
    store.transition_task(task.task_id, status, evidence=[{'kind': 'file_hash', 'hash': 'observed'}])
    return task


@pytest.mark.parametrize('passed', [True, False])
def test_final_review_updates_scoped_persistent_state_and_quality(tmp_path, monkeypatch, passed):
    monkeypatch.setattr('core.executor.record_runtime_event', lambda *a, **k: None)
    executor = runtime(tmp_path)
    task = completed(executor)
    outcome = ExecutionOutcome('재검수 결과', 'completed' if passed else 'partial', task_id=task.task_id)
    verdict = {'passed': passed, 'reason': '실제 문서 내용 검사', 'criteria_results': [{'passed': passed}]}
    assert executor.record_acceptance(outcome, verdict, session_id='s', workspace_path='workspace')
    assert not executor.record_acceptance(outcome, verdict, session_id='s', workspace_path='workspace')
    disk = DialogueStateStore(executor.dialogue_state.db_path).get_task('s', task.task_id, 'workspace')
    assert disk.status == ('completed' if passed else 'partial') == outcome.status
    assert disk.verification_status == ('passed' if passed else 'failed')
    assert disk.evidence[0]['kind'] == 'file_hash'
    assert disk.evidence[-1]['verdict'] == verdict
    metric = executor.quality_metrics.snapshot()['metrics']['task_success']
    assert metric['count'] == 1 and metric['average'] == float(passed)


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'awaiting_user', 'awaiting_approval'])
def test_late_review_cannot_reopen_or_complete_noncompleted_task(tmp_path, status):
    executor = runtime(tmp_path)
    task = completed(executor, status)
    assert not executor.dialogue_state.record_acceptance('s', task.task_id, 'workspace', {'passed': True})
    assert executor.dialogue_state.get_task('s', task.task_id).status == status


@pytest.mark.parametrize('session,workspace', [('other', 'workspace'), ('s', 'elsewhere')])
def test_review_cannot_cross_session_or_workspace(tmp_path, session, workspace):
    executor = runtime(tmp_path)
    task = completed(executor)
    assert not executor.dialogue_state.record_acceptance(session, task.task_id, workspace, {'passed': False})
    assert executor.dialogue_state.get_task('s', task.task_id).status == 'completed'


def test_runtime_completion_and_question_mark_are_not_goal_quality(tmp_path):
    executor = runtime(tmp_path)
    executor._record_turn_quality('안녕', ExecutionOutcome('안녕', 'completed'), time.perf_counter())
    executor._record_turn_quality('뭔가 해줘', ExecutionOutcome('어떤 작업을 원하시나요?', 'awaiting_user'), time.perf_counter())
    metrics = executor.quality_metrics.snapshot()['metrics']
    assert metrics['runtime_completion']['count'] == 2
    assert metrics['clarification_requested']['count'] == 1
    assert metrics['task_success']['count'] == metrics['clarification_quality']['count'] == 0


def test_acceptance_never_upgrades_partial_execution(tmp_path, monkeypatch):
    monkeypatch.setattr('core.executor.record_runtime_event', lambda *a, **k: None)
    executor = runtime(tmp_path)
    task = completed(executor, 'partial')
    outcome = ExecutionOutcome('일부만 완료', 'partial', task_id=task.task_id)
    assert executor.record_acceptance(outcome, {'passed': True}, session_id='s', workspace_path='workspace')
    assert outcome.status == 'partial'
    assert executor.quality_metrics.snapshot()['metrics']['task_success']['average'] == 0.0
