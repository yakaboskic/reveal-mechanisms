"""Transport-neutral worker boundary. Results are untrusted until backend acceptance."""
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Awaitable, Callable, Literal, Protocol, Sequence

ExecutionStatus = Literal['succeeded', 'insufficient_evidence', 'failed', 'cancelled']
Emit = Callable[[str, dict], Awaitable[None]]
MAX_EMIT_BATCH_EVENTS = 20
MAX_EMIT_BATCH_BYTES = 256 * 1024


def emit_batch_size(events: Sequence[tuple[str, dict]]) -> int:
    """Wire size includes every event and its remote cursor/deduplication fields."""
    return len(json.dumps(events, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


class BatchedEmit(Protocol):
    """Optional durable batch capability; individual events remain unchanged.

    Returning from either method acknowledges persistence. Exceptions leave the
    remote cursor unacknowledged and must permit replay with the same event IDs.
    """
    async def __call__(self, kind: str, payload: dict) -> None: ...
    async def emit_batch(self, events: Sequence[tuple[str, dict]]) -> None: ...


Cancelled = Callable[[], Awaitable[bool]]
Checkpoint = Callable[[dict], Awaitable[None]]


def agent_budget_usd(kind: str) -> float:
    """Dollar cap for one agent run. Writing a statement costs cents, so it does not share research's cap."""
    from .runtime_config import setting
    if kind == 'paragraph':
        return float(setting('REVEAL_PARAGRAPH_MAX_BUDGET_USD', '1'))
    return float(setting('REVEAL_AGENT_MAX_BUDGET_USD', '5'))


@dataclass(frozen=True)
class ExecutionRequest:
    job_id: str
    attempt: int
    kind: Literal['research', 'paragraph']
    input_path: Path
    output_dir: Path
    selected_graphs: tuple[str, ...] = ()
    timeout_seconds: int = 1800
    max_budget_usd: float = 5.0
    max_turns: int = 100
    remote_handle: dict | None = None
    validation_feedback: tuple[str, ...] = ()
    # Trusted transport only. Never serialize this credential into frozen inputs or public events.
    research_access: dict | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    output_dir: Path
    account_paths: tuple[Path, ...] = ()
    paragraph_path: Path | None = None
    runtime_manifest_path: Path | None = None
    ledger_manifest_path: Path | None = None
    reason: str | None = None
    remote_handle: dict | None = None
    outcome_path: Path | None = None
    research_receipts_path: Path | None = None


class ExecutionAdapter(Protocol):
    async def execute(self, request: ExecutionRequest, emit: Emit,
                      cancelled: Cancelled, checkpoint: Checkpoint) -> ExecutionResult: ...
