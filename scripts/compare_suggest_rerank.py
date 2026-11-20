#!/usr/bin/env python3
"""Compare today's cosine suggestions with the Jev rerank for selected knowledge gaps.

Each gap runs the exact /v1/mechanisms/suggest path (build_suggestions) with the rerank
enabled and its audit row captured in memory; nothing is written to the database.
Without --send, Jev is never called: requests are captured to report their sizes and
the rerank falls back to the cosine policy. Requires the backend runtime environment
(Aurora reference catalog and the active vector snapshot).
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import sys
from unittest.mock import patch

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
TOP = 5


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + '\n')
    temporary.replace(path)


def cosine_policy(rows):
    """Today's order over the audited pool: disease identities in native id order, then cosine."""
    pinned = sorted(row[0] for row in rows if row[2])[:TOP]
    return pinned + [row[0] for row in rows if row[1] is not None and row[0] not in pinned][:TOP - len(pinned)]


def jev_order(rows):
    """Every scored pool row in gap_rerank.rank order."""
    scored = [row for row in rows if row[3] is not None]
    return [row[0] for row in sorted(scored, key=lambda row: (-(row[3] + row[4]) / 8,
            (0, -row[1]) if row[1] is not None else (1, 0), row[0]))]


def compare(api, gap_rerank, canonical, gap, send):
    body = {'manual_eaggl_anchors': [], 'dismissed_source_ids': [], 'subquery': '', 'mode': 'semantic', 'model': api.catalog.model,
            'source_gap': {'id': gap['object']['id'], **{key: gap['source'][key] for key in ('source_id', 'source_revision')}}}
    rows, sent = {}, []
    class Capture:
        def append(self, kind, identity, owner, data): rows[identity] = data
    prepare = gap_rerank.request_bodies
    def measured(*args, **kwargs):
        bodies = prepare(*args, **kwargs)
        sent.extend(len(canonical(body).encode()) for body, _ in bodies)
        return bodies
    def never_sent(content, **kwargs): raise gap_rerank.JevCallError('unavailable')
    with ExitStack() as stack:
        stack.enter_context(patch.object(api, 'repo', Capture()))
        if not send:
            stack.enter_context(patch.object(gap_rerank, 'request_bodies', measured))
            stack.enter_context(patch.object(gap_rerank, 'post_systemone', never_sent))
        result = api.build_suggestions(body)
    audit = rows[result['suggestion_id']]
    pool = audit.get('rerank_pool', [])
    by_id = {row[0]: row for row in pool}
    def describe(native):
        row = by_id[native]
        return {'source_id': native, **gap_rerank.factor_view(api.catalog.factors[native]), 'cosine': row[1],
                'disease_identity': row[2], 'relevance': row[3], 'addresses': row[4]}
    cosine = cosine_policy(pool); jev = jev_order(pool)
    position = {native: rank for rank, native in enumerate(jev, 1)}
    return {'gap': {'source_id': gap['source']['source_id'], 'question': gap['object']['text'],
                    'disease': gap['source'].get('disease_label'), 'linked_mechanisms': [item['label'] for item in gap['attachments'] if item['target']]},
            'rerank': audit.get('rerank'), 'dry_run_request_bytes': sent,
            'returned': [item['factor']['source_id'] for item in result['automatic_anchors']],
            'cosine_top': [describe(native) for native in cosine], 'jev_top': [describe(native) for native in jev[:TOP]],
            'overlap_at_5': len(set(cosine) & set(jev[:TOP])) if jev else None,
            'jev_rank_of_cosine_top': {native: position.get(native) for native in cosine},
            'pool': [describe(row[0]) for row in pool]}


def table(rows):
    lines = ['| # | Label | Trait | Cosine | Relevance | Addresses |', '|---|---|---|---|---|---|']
    for rank, row in enumerate(rows, 1):
        number = lambda value, digits: '—' if value is None else f'{value:.{digits}f}'
        label = row['label'] + (' (disease)' if row['disease_identity'] else '')
        lines.append(f"| {rank} | {label} | {row['trait']} | {number(row['cosine'], 3)} | {number(row['relevance'], 1)} | {number(row['addresses'], 1)} |")
    return '\n'.join(lines)


def report(results):
    applied = [value for value in results if (value['rerank'] or {}).get('status') == 'applied']
    usage = {field: sum(value['rerank'].get('usage', {}).get(field, 0) for value in applied) for field in ('input_tokens', 'output_tokens')}
    lines = ['# Cosine vs Jev suggestions', '',
             f'{len(results)} gaps; rerank applied to {len(applied)}. Jev usage: {usage["input_tokens"]:,} input and '
             f'{usage["output_tokens"]:,} output tokens.', '']
    if applied:
        lines.append(f'Mean overlap@5: {sum(value["overlap_at_5"] for value in applied) / len(applied):.2f}. '
                     f'Mean rerank time: {sum(value["rerank"]["elapsed_ms"] for value in applied) / len(applied):.0f} ms.')
        lines.append('')
    for value in results:
        gap, rerank = value['gap'], value['rerank'] or {}
        lines += [f"## {gap['disease']}: {gap['question']}", '', f"`{gap['source_id']}`", '',
                  f"Linked mechanisms: {', '.join(gap['linked_mechanisms']) or 'none (gap text used)'}.", '',
                  f"Rerank: {rerank.get('status')}" + (f" ({rerank['reason']})" if rerank.get('reason') else '') +
                  f"; pool {rerank.get('pool_size')}; overlap@5 {value['overlap_at_5']}.", '']
        if value['dry_run_request_bytes']:
            lines += [f"Dry run, nothing sent: {len(value['dry_run_request_bytes'])} request(s) of {', '.join(f'{size:,}' for size in value['dry_run_request_bytes'])} bytes.", '']
        lines += ['**Today (disease identity, then cosine)**', '', table(value['cosine_top']), '']
        if value['jev_top']:
            ranks = ', '.join(f'{rank or "—"}' for rank in value['jev_rank_of_cosine_top'].values())
            lines += ['**Jev**', '', table(value['jev_top']), '', f'Jev ranks of today\'s five: {ranks}.', '']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--env-file', type=Path, help='Backend runtime environment file (default: the root .env)')
    parser.add_argument('--disease', help='Case-insensitive substring of the source disease name')
    parser.add_argument('--gap-id', action='append', default=[], help='Exact DisMech gap source id; repeat to select several')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--include-resolved', action='store_true')
    parser.add_argument('--send', action='store_true', help='Call Jev (needs TYPESAFE_API_KEY); otherwise only capture request sizes')
    parser.add_argument('--force', action='store_true', help='Recompute gaps that already have a result in --output')
    parser.add_argument('--output', type=Path, default=ROOT / '.runtime/suggest-rerank/compare')
    args = parser.parse_args(argv)
    env_file = args.env_file or ROOT / '.env'
    if not env_file.is_file(): parser.error(f'Environment file does not exist: {env_file}')
    load_dotenv(env_file, override=False, interpolate=False)
    os.environ['REVEAL_SUGGEST_RERANK'] = 'jev'
    if args.send and not os.getenv('TYPESAFE_API_KEY', '').strip(): parser.error('--send requires TYPESAFE_API_KEY')
    # A dry run needs the rerank path, but its provider call is replaced before anything is sent.
    if not args.send: os.environ['TYPESAFE_API_KEY'] = os.getenv('TYPESAFE_API_KEY', '').strip() or 'dry-run-never-sent'
    from reveal_backend import app as api, gap_rerank
    from reveal_backend.dismech_import import canonical
    api.catalog.load()
    gaps = sorted(api.catalog.gaps.values(), key=lambda gap: gap['source']['source_id'])
    if not args.include_resolved: gaps = [gap for gap in gaps if gap['source'].get('status') != 'RESOLVED']
    if args.disease: gaps = [gap for gap in gaps if args.disease.casefold() in (gap['source'].get('disease_label') or '').casefold()]
    if args.gap_id:
        wanted = set(args.gap_id); gaps = [gap for gap in gaps if gap['source']['source_id'] in wanted]
        missing = wanted - {gap['source']['source_id'] for gap in gaps}
        if missing: parser.error(f'Unknown or excluded gap ids: {", ".join(sorted(missing))}')
    gaps = gaps[:args.limit]
    if not gaps: parser.error('No gaps matched')
    results = []
    for number, gap in enumerate(gaps, 1):
        path = args.output / 'gaps' / (re.sub(r'[^A-Za-z0-9_.-]+', '_', gap['source']['source_id']) + '.json')
        if path.exists() and not args.force:
            results.append(json.loads(path.read_text())); continue
        value = compare(api, gap_rerank, canonical, gap, args.send)
        write_json(path, value); results.append(value)
        rerank = value['rerank'] or {}
        print(f"[{number}/{len(gaps)}] {gap['source']['source_id']}: {rerank.get('status')}"
              + (f" ({rerank['reason']})" if rerank.get('reason') else '') + f", overlap@5 {value['overlap_at_5']}", flush=True)
    (args.output / 'report.md').write_text(report(results))
    print(f'Wrote {args.output / "report.md"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
