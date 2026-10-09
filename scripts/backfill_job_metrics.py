#!/usr/bin/env python3
"""Backfill `job_metrics` for workflow jobs that finished before cost telemetry was recorded.

Reads each job's durable S3 workspace checkpoint (the same `attempt-N/output/runtime.json`
and `attempt-N/failure.json` the runner and workflow already capture) and records the
compact metrics the admin console shows. Dry run by default; `--apply` writes. Jobs that
already have metrics are skipped unless `--force`. Never reads scientific content.

    .venv/bin/python scripts/backfill_job_metrics.py --prefix reveal_workflow_qa
    .venv/bin/python scripts/backfill_job_metrics.py --prefix reveal_workflow_qa --apply
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--prefix', help='application table prefix (default REVEAL_APPLICATION_TABLE_PREFIX)')
    parser.add_argument('--aws-profile', help='AWS profile for reading the artifact bucket')
    parser.add_argument('--apply', action='store_true', help='write metrics (default: report only)')
    parser.add_argument('--force', action='store_true', help='rewrite jobs that already have metrics')
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    import boto3
    from reveal_backend.job_metrics import KIND, agent_metrics, record
    from reveal_backend.admin_jobs import redact
    from reveal_backend.repository import Repository
    s3 = boto3.Session(profile_name=args.aws_profile).client('s3')
    def read(reference):
        kwargs = {'Bucket': reference['bucket'], 'Key': reference['key']}
        if reference.get('version_id'): kwargs['VersionId'] = reference['version_id']
        return json.loads(s3.get_object(**kwargs)['Body'].read())
    from reveal_backend.telemetry import records
    repository = Repository(table_prefix=args.prefix)
    with repository.read_transaction() as tx:
        identities = [identity for identity, in tx.execute('SELECT id FROM reveal_records WHERE kind=%s', ('job',)).fetchall()]
        jobs = {i: row['data'] for i, row in records(tx, 'job', ('kind', 'status', 'created_at', 'completed_at'), identities).items()}
        executions = {i: row['data'] for i, row in records(tx, 'execution', ('workspace',), identities).items()}
        done = {identity for identity, in tx.execute('SELECT id FROM reveal_records WHERE kind=%s', (KIND,)).fetchall()}
    written = skipped = missing = 0
    for identity, job in sorted(jobs.items(), key=lambda item: item[1].get('created_at') or ''):
        execution = executions.get(identity, {}); workspace = execution.get('workspace')
        if identity in done and not args.force: skipped += 1; continue
        if not workspace or workspace.get('store') != 's3': missing += 1; continue
        files = {item['path']: item['storage'] for item in read(workspace)['files']}
        attempts = sorted({int(m[1]) for path in files if (m := re.fullmatch(r'attempt-([1-9][0-9]*)/.*', path))})
        attempt = f'attempt-{attempts[-1]}' if attempts else None
        runtime = read(files[f'{attempt}/output/runtime.json']) if attempt and f'{attempt}/output/runtime.json' in files else None
        failure = read(files[f'{attempt}/failure.json']) if attempt and f'{attempt}/failure.json' in files else None
        agent = agent_metrics(runtime) if runtime else None
        error = ({'phase': failure.get('phase'), 'error_type': failure.get('error_type') or 'Error',
                  'message': (redact(failure.get('message') or '')[:500] or None), 'recorded_at': job.get('completed_at')}
                 if isinstance(failure, dict) else None)
        cost = agent['cost_usd'] if agent else None
        print(f"{identity} {job.get('kind', '?'):10} {job.get('status', '?'):22} "
              f"{'$%.4f' % cost if cost is not None else ('unreported' if agent else 'no agent run'):>12}"
              f"{'  ' + error['error_type'] + ' · ' + str(error['phase']) if error else ''}")
        if (agent or error) and args.apply:
            written += record(repository, {'id': identity}, agent=agent, error=error)
    print(f"\n{'Recorded' if args.apply else 'Would record'} metrics for jobs above; {written} written, "
          f"{skipped} already had metrics, {missing} have no S3 workspace.")


if __name__ == '__main__':
    main()
