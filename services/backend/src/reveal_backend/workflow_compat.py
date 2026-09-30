"""At-least-once history handling missing from pinned workflow-py 2.0.0.

Call only inside a Serve route, after the SDK verifies the original signed
request. Upstream workflow-js deduplicates histories and acknowledges duplicate
tails without advancing execution. Python 2.0.0 instead replays by list position,
so an old result appended twice corrupts the next step name.

This shim supports our sequential workflows, rejects conflicting results, and
never changes the signed body or executes application effects. Keep the SDK
integration tests when changing the pinned version or these private attributes.

Upstream behavior: https://github.com/upstash/workflow-js/blob/main/src/workflow-parser.ts
"""
from upstash_workflow.error import WorkflowAbort, WorkflowError


def normalize_history(context):
    steps = context._steps
    if not steps: return  # SDK authorization pass, before its first disabled step.
    executor = context._executor
    if executor.step_count or executor.plan_step_count or executor._already_executed:
        raise WorkflowError('Normalize Workflow history before executing any step')
    unique, seen = [], {}
    duplicate_tail = False
    for position, step in enumerate(steps):
        if (type(step.step_id) is not int or step.concurrent != 1 or step.target_step is not None
                or (position == 0 and (step.step_id != 0 or step.step_type != 'Initial'))
                or (position > 0 and (step.step_id < 1 or step.step_type not in ('Run', 'SleepFor', 'SleepUntil', 'Call')))):
            raise WorkflowError('Unsupported nonsequential Workflow history')
        if step.step_id in seen:
            if seen[step.step_id] != step:
                raise WorkflowError('Conflicting duplicate Workflow step ' + str(step.step_id))
            duplicate_tail = position == len(steps) - 1
        else:
            seen[step.step_id] = step
            unique.append(step)
    if [step.step_id for step in unique] != list(range(len(unique))):
        raise WorkflowError('Workflow history is incomplete or out of order')
    if duplicate_tail:
        # The SDK treats this as an acknowledged step and skips workflow
        # deletion. Another delivery owns the already-recorded continuation.
        raise WorkflowAbort('duplicate-step')
    context._steps = unique
    executor.steps = unique
    executor.non_plan_step_count = len(unique)
