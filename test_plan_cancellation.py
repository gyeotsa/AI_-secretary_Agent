import pytest

from core.plan_runtime import PlanCoordinator, PlanDAG, PlanStep, PlanExecutionStore, StepStatus
from core.plugin import ToolCancelledError
from core.tool_result import Artifact, Evidence, ToolRunResult, ToolRunStatus
from core.turn_context import TurnExecutionContext, bind_turn_context, current_turn_context


def coordinator(tmp_path):
    return PlanCoordinator(PlanExecutionStore(str(tmp_path / 'plan.db')), max_parallel=1)


@pytest.mark.parametrize('typed', [True, False])
def test_cancelled_step_never_retries_or_dispatches_successor(tmp_path, typed):
    runtime = coordinator(tmp_path)
    plan = PlanDAG('synthetic', [PlanStep('first', tool_name='a'), PlanStep('later', tool_name='b', dependencies=['first'])])
    called = []
    def execute(step, _strategy):
        called.append(step.id)
        if typed: return ToolRunResult.cancelled(tool_name='a', message='cancelled')
        raise ToolCancelledError('cancelled')
    result = runtime.run(plan, execute, lambda *a: pytest.fail('cancelled result must not start verifier'),
                         replan=lambda *a: pytest.fail('cancelled result must not replan'))
    assert result.status == 'cancelled' and called == ['first']
    assert all(s.status == StepStatus.CANCELLED for s in result.plan.steps)
    assert result.plan.steps[0].attempts == 1


def test_dag_worker_retains_turn_context_and_prior_completed_receipt(tmp_path):
    runtime = coordinator(tmp_path)
    plan = PlanDAG('synthetic', [PlanStep('first', tool_name='a'), PlanStep('later', tool_name='b', dependencies=['first'])])
    context = TurnExecutionContext('t', 'session', 'workspace')
    receipt = ToolRunResult.successful(tool_name='a', raw_output='observed', evidence=[Evidence('receipt', 'observed')])
    called = []
    def execute(step, _strategy):
        assert current_turn_context() is context
        called.append(step.id)
        context.cancel()
        return receipt
    with bind_turn_context(context):
        result = runtime.run(plan, execute, lambda _step, candidate: candidate)
    assert result.status == 'cancelled'
    assert called == ['first'] and result.results['first'] is receipt
    assert plan.steps[0].status == StepStatus.COMPLETED
    assert plan.steps[1].status == StepStatus.CANCELLED


def test_already_cancelled_turn_cannot_start_approval_or_tool(tmp_path):
    runtime = coordinator(tmp_path)
    context = TurnExecutionContext('t', 's')
    plan = PlanDAG('synthetic', [PlanStep('first', tool_name='a', requires_approval=True, approval_reason='synthetic')])
    with bind_turn_context(context):
        context.cancel()
        result = runtime.run(plan, lambda *a: pytest.fail('must not execute'),
                             lambda *a: pytest.fail('must not verify'),
                             approve=lambda *a: pytest.fail('must not request approval'))
    assert result.status == 'cancelled'


@pytest.mark.parametrize('interrupted', [True, False])
def test_verifier_exception_preserves_receipt_without_retry_or_replan(tmp_path, interrupted):
    runtime = coordinator(tmp_path)
    context = TurnExecutionContext('t', 's')
    plan = PlanDAG('synthetic', [PlanStep('first', tool_name='a'),
                               PlanStep('later', tool_name='b', dependencies=['first'])])
    receipt = ToolRunResult.successful(tool_name='a', raw_output='observed-result',
        evidence=[Evidence('receipt', 'observed')], artifacts=[Artifact('file', 'synthetic.txt')])
    called = []
    def execute(step, _strategy):
        called.append(step.id)
        return receipt
    def verify(_step, _candidate):
        if interrupted:
            context.cancel()
            raise ToolCancelledError('cancelled while verifying')
        raise ValueError('invalid verifier response')
    with bind_turn_context(context):
        result = runtime.run(plan, execute, verify,
            replan=lambda *a: pytest.fail('uncertain execution must not replan'))
    observed = result.results['first']
    assert called == ['first'] and plan.steps[0].attempts == 1
    assert observed.status == ToolRunStatus.UNVERIFIED
    assert observed.raw_output == receipt.raw_output
    assert observed.evidence[0] == receipt.evidence[0]
    assert observed.artifacts == receipt.artifacts
    assert result.status == ('cancelled' if interrupted else 'failed')
