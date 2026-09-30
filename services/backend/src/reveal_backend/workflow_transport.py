"""Pinned Workflow SDK transport safeguards for replayed HTTP deliveries."""
import json
import os

from .repository import digest


class WorkflowHttp:
    """Keep bounded deadlines and stable identities on every continuation.

    The Python SDK sends continuations directly through its QStash HTTP client.
    Its initial trigger options are not propagated to these later batches.
    Provider deduplication is an optimization; RDS fences and SDK history
    normalization still protect replays beyond the provider's dedup window.
    """
    def __init__(self, delegate): self.delegate = delegate

    def __getattr__(self, name): return getattr(self.delegate, name)

    async def request(self, *args, **kwargs):
        if kwargs.get('path') == '/v2/batch' and kwargs.get('method') == 'POST':
            batch = json.loads(kwargs['body'])
            for item in batch:
                headers = item.get('headers', {})
                run_id = headers.get('Upstash-Workflow-RunId')
                if not run_id: continue
                headers.setdefault('Upstash-Timeout', os.getenv('REVEAL_WORKFLOW_DELIVERY_TIMEOUT', '420s'))
                if headers.get('Upstash-Workflow-Init') == 'false':
                    step = json.loads(item['body'])
                    if 'stepId' in step and step.get('stepType') in ('Run', 'SleepFor', 'SleepUntil'):
                        headers.setdefault('Upstash-Deduplication-Id', digest([
                            'workflow-continuation', run_id, step['stepId'], step['stepType']]))
            kwargs['body'] = json.dumps(batch)
        return await self.delegate.request(*args, **kwargs)
