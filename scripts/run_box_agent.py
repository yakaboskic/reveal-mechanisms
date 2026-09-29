#!/usr/bin/env python3
"""Standalone real Box consumer; raw outputs require trusted backend acceptance."""
import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import uuid
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.agent_execution import ExecutionRequest
from reveal_backend.box_adapter import BoxExecutionAdapter, required_environment

async def run(args):
    if args.env_file:
        from dotenv import dotenv_values
        values = dotenv_values(args.env_file)
        for name in ('UPSTASH_BOX_API_KEY', 'ANTHROPIC_API_KEY', 'REVEAL_CLAUDE_MODEL'):
            if values.get(name): os.environ.setdefault(name, values[name])
    if required_environment():
        raise ValueError('Missing configuration: ' + ', '.join(required_environment()))
    args.output.mkdir(parents=True, exist_ok=True)
    checkpoint_path = args.output / 'remote-handle.json'
    previous = json.loads(checkpoint_path.read_text()) if args.resume else None
    request = ExecutionRequest(job_id=previous['job_id'] if previous else str(uuid.uuid4()), attempt=previous['attempt'] if previous else 1,
                               kind=args.kind, input_path=args.input.resolve(), output_dir=args.output.resolve(),
                               selected_graphs=tuple(args.graph), timeout_seconds=args.timeout,
                               max_budget_usd=args.budget, max_turns=args.turns, remote_handle=previous,
                               validation_feedback=tuple(json.loads(args.feedback_file.read_text())) if args.feedback_file else ())
    async def checkpoint(handle): checkpoint_path.write_text(json.dumps(handle, indent=2))
    async def emit(kind, payload):
        with (args.output / 'public-events.jsonl').open('a') as events:
            events.write(json.dumps({'type': kind, 'payload': payload}) + '\n')
        print(json.dumps({'type': kind, 'payload': {k: v for k, v in payload.items() if k != 'text'}}), flush=True)
    async def cancelled(): return (args.output / 'cancel').exists()
    result = await BoxExecutionAdapter(ROOT).execute(request, emit, cancelled, checkpoint)
    summary = asdict(result)
    for key, value in summary.items():
        if isinstance(value, Path): summary[key] = str(value)
        if isinstance(value, tuple): summary[key] = [str(p) for p in value]
    (args.output / 'execution-result.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0 if result.status in ('succeeded', 'insufficient_evidence') else 1

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--kind', choices=['research', 'paragraph'], default='research')
    parser.add_argument('--graph', action='append', default=[])
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--feedback-file', type=Path, help='Trusted reviewer feedback as a JSON list; fresh bounded repair attempt only')
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--budget', type=float, default=3)
    parser.add_argument('--turns', type=int, default=100)
    args = parser.parse_args()
    try: return asyncio.run(run(args))
    except KeyboardInterrupt:
        print('Consumer disconnected; remote execution continues. Use --resume with the same output directory.',file=sys.stderr)
        return 130
    except Exception as exc:
        print('Execution interrupted: ' + type(exc).__name__ + '. Retain remote-handle.json and resume the same attempt.', file=sys.stderr)
        return 2
if __name__ == '__main__': raise SystemExit(main())
