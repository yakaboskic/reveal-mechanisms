#!/usr/bin/env python3
"""Claim-structure baseline over accepted accounts and job outputs; read-only.

Recomputes the advisory claim_suggestions summary for account documents: files, or directories searched for
accepted-*.json and account-*.json/yaml (job outputs), and with --database the newest scientific_document
records, read in one read-only transaction. Prints per-family counts and conformance, synthesis coherence and
other Claims in total and per document; --json prints the rows. Nothing is written.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.claim_suggestions import FAMILIES, claim_structure
from reveal_backend.evidence_package import EvidenceBuildError, decode

PATTERNS = ('accepted-[0-9]*.json', 'account-*.json', 'account-*.yaml', 'account-*.yml')


def local_documents(paths, package=None):
    """(source, document, package) for each readable account document; other JSON is skipped."""
    for path in paths:
        files = sorted({file for pattern in PATTERNS for file in path.rglob(pattern)}) if path.is_dir() else [path]
        for file in files:
            try: document = decode(file.read_bytes(), 'yaml' if file.suffix in ('.yaml', '.yml') else 'json')
            except (OSError, ValueError, EvidenceBuildError): continue
            if isinstance(document, dict) and isinstance(document.get('scientific_accounts'), list): yield str(file), document, package


def database_documents(repository, limit):
    """The newest scientific_document records: ids first, then payloads by id (no sort over JSON payloads)."""
    with repository.read_transaction() as tx:
        ids = [row[0] for row in tx.execute("SELECT id FROM reveal_records WHERE kind='scientific_document' "
                                            'ORDER BY updated_at DESC, id LIMIT %s', (limit,)).fetchall()]
        rows = tx.get_many('scientific_document', ids)
    for identity in ids:
        if identity in rows and isinstance(rows[identity]['data'].get('document'), dict):
            yield 'scientific_document:' + identity, rows[identity]['data']['document'], None


def report(documents):
    """Per-document summaries and their totals, one row per distinct document."""
    rows, seen = [], set()
    for source, document, package in documents:
        key = json.dumps(document, sort_keys=True)
        if key in seen: continue
        seen.add(key)
        account = (document.get('scientific_accounts') or [{}])[0]
        rows.append({'source': source, 'account_id': account.get('id') if isinstance(account, dict) else None,
                     'summary': claim_structure(document, package)['summary']})
    total = {'documents': len(rows), 'families': {family: {'count': 0, 'conformant': 0} for family in FAMILIES},
             'synthesis': {'count': 0, 'coherent': 0, 'gap_relevance': 0}, 'other': 0, 'issues': {}}
    for row in rows:
        summary = row['summary']
        for family, counts in summary['families'].items():
            for key in ('count', 'conformant'): total['families'][family][key] += counts[key]
        for key in total['synthesis']: total['synthesis'][key] += summary['synthesis'][key]
        total['other'] += summary['other']
        for check, count in summary['issues'].items(): total['issues'][check] = total['issues'].get(check, 0) + count
    atomic = sum(value['count'] for value in total['families'].values())
    conformant = sum(value['conformant'] for value in total['families'].values())
    structured = atomic + total['synthesis']['count']
    total.update(atomic={'count': atomic, 'conformant': conformant},
                 conformance_rate=round((conformant + total['synthesis']['coherent']) / structured, 3) if structured else None)
    return {'total': total, 'documents': rows}


def render(result):
    total, lines = result['total'], []
    lines.append(f"{total['documents']} documents; conformance rate {total['conformance_rate']}")
    lines.append(f"{'family':<18}{'claims':>8}{'conformant':>12}")
    for family, counts in total['families'].items(): lines.append(f"{FAMILIES[family][5]:<18}{counts['count']:>8}{counts['conformant']:>12}")
    synthesis = total['synthesis']
    lines.append(f"synthesis {synthesis['count']} (coherent {synthesis['coherent']}, gap relevance {synthesis['gap_relevance']}); other {total['other']}")
    if total['issues']: lines.append('suggestions: ' + ', '.join(f'{check} {count}' for check, count in sorted(total['issues'].items())))
    for row in result['documents']:
        summary = row['summary']
        lines.append(f"- {row['source']}: atomic {summary['atomic']['conformant']}/{summary['atomic']['count']}, synthesis "
                     f"{summary['synthesis']['coherent']}/{summary['synthesis']['count']}, other {summary['other']}, rate {summary['conformance_rate']}")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='*', type=Path, help='Account documents or directories (job artifacts)')
    parser.add_argument('--package', type=Path, help='Evidence package or validation context applied to local documents')
    parser.add_argument('--database', action='store_true', help='Also read scientific_document records (read-only)')
    parser.add_argument('--limit', type=int, default=200, help='Newest database documents to read')
    parser.add_argument('--json', action='store_true', help='Print the per-document rows and totals as JSON')
    args = parser.parse_args(argv)
    if not args.paths and not args.database: parser.error('Give account paths, --database, or both')
    documents = list(local_documents(args.paths, decode(args.package.read_bytes()) if args.package else None))
    if args.database:
        from dotenv import load_dotenv
        from reveal_backend.repository import Repository
        load_dotenv(ROOT / '.env')
        documents += database_documents(Repository(), args.limit)
    result = report(documents)
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else render(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
