"""Progressive evidence shared by the existing hosted worker and local MCP."""
from copy import deepcopy
from pathlib import Path

from .auth import owned
from .repository import digest, now, uid
from .research_work import ResearchWorkService, deadline, issue_grant
from .evidence_package import canonical_json, decode, require, sha256
from . import user_inputs


def create(tx, job, frozen, binding):
    """Hosted progressive state for a request already written; a request not yet frozen as progressive is
    written again with that mode."""
    if frozen.get('retrieval_mode') != 'progressive':
        frozen['retrieval_mode'] = 'progressive'; tx.put('request', frozen['id'], job['owner_user_id'], frozen)
    for row in rows(job, frozen, binding): tx.put(*row)


def rows(job, frozen, binding):
    """The local_work and research_pin rows of a new hosted analysis; no I/O."""
    from .reference_generation import generation_of_anchors
    generation = generation_of_anchors(binding['anchors']); owner = job['owner_user_id']
    work = {'id': job['id'], 'job_id': job['id'], 'owner_user_id': owner, 'research_request_id': frozen['id'],
        'state': 'preparing', 'created_at': now(), 'last_activity': now(), 'last_action': 'created',
        'expires_at': deadline(30*86400), 'reference_generation_id': generation,
        'package_id': None, 'package_sha256': None, 'last_error': None}
    return [('local_work', work['id'], owner, work),
            ('research_pin', frozen['id'], owner, {'id': frozen['id'], 'research_request_id': frozen['id'],
                'generation_id': generation, 'state': 'active', 'expires_at': work['expires_at']})]


def collect(repository, job, directory):
    service = ResearchWorkService(repository); owner = job['owner_user_id']
    with repository.read_transaction() as tx:
        work = owned(tx, 'local_work', job['id'], owner)['data']
        saved = tx.get('research_package', work['package_id']) if work['package_id'] else None
    if saved:
        result = saved['data']
    else:
        result = service.prepare({'owner_user_id': owner, 'local_work_id': job['id'], 'research_request_id': job['research_request_id']})
        with repository.transaction() as tx:
            work = owned(tx, 'local_work', job['id'], owner)['data']
            # Concurrent recovery can only choose one immutable seed.
            if work['package_id']: result = owned(tx, 'research_package', work['package_id'], owner)['data']
            else:
                tx.put('research_package', result['id'], owner, result)
                work.update(state='ready', package_id=result['id'], package_sha256=result['sha256'])
                tx.put('local_work', work['id'], owner, work)
    files = {}
    with repository.read_transaction() as tx:
        references = [owned(tx, 'research_artifact', a['id'], owner)['data'] for a in result['artifacts']]
    for artifact in references: files[artifact['filename']] = user_inputs.read(artifact['storage'])
    from .research_execution import write_context
    path = write_context(Path(directory)/'package', result['package'], files)
    require(sha256(path.read_bytes()) == result['sha256'], 'Hosted seed changed')
    return path, result['package']


def access(repository, job, attempt):
    with repository.transaction() as tx:
        work = tx.get('local_work', job['id'])
        if not work: return None
        value = issue_grant(tx, job['owner_user_id'], job['id'], uid(), kind='hosted', execution_id=str(attempt))
        return {key: value[key] for key in ('mcp_url', 'token', 'local_work_id')} | {'research_request_id': job['research_request_id']}


def captured_context(repository, job, result, seed_path, output):
    """Resolve root-captured receipt IDs. A model-authored context is never trusted."""
    seed = decode(Path(seed_path).read_bytes())
    if seed.get('retrieval_mode') != 'progressive': return Path(seed_path), {}, []
    receipt_path = result.research_receipts_path
    require(receipt_path is not None, 'Progressive execution is missing its trusted research receipt capture')
    path = Path(receipt_path).resolve(); root = Path(result.output_dir).resolve()
    require(path.is_relative_to(root) and not Path(receipt_path).is_symlink(), 'Invalid research receipt capture path')
    require(path.stat().st_size <= 200_000, 'Research receipt capture exceeds its bound')
    captured = decode(path.read_bytes())
    require(captured.get('research_request_id') == job['research_request_id'] and captured.get('local_work_id') == job['id'], 'Research receipt capture scope differs')
    args = {field: captured.get(field, []) for field in ('receipt_ids', 'reuse_receipt_ids')}
    require(all(isinstance(v, list) and len(v) <= 300 and all(isinstance(i, str) for i in v) for v in args.values()), 'Invalid research receipt list')
    existing = []
    if result.outcome_path:
        outcome = decode(Path(result.outcome_path).read_bytes())
        if outcome.get('status') == 'succeeded':
            existing = outcome.get('existing_account_ids', [])
            require(isinstance(existing, list) and len(existing) <= 3 and len(set(existing)) == len(existing), 'Invalid existing account selection')
    from .research_execution import load_context, write_context
    service = ResearchWorkService(repository)
    package, files, reused = load_context(service, job['owner_user_id'], job['id'], args)
    require(set(existing) <= {r['account_id'] for r in reused['existing_accounts']}, 'Existing account selection requires an exact-question reuse receipt')
    require(package['validation_context']['seed_sha256'] == sha256(Path(seed_path).read_bytes()), 'Validation context belongs to another seed')
    package['validation_context']['existing_account_ids'] = existing
    target = write_context(output, package, files)
    # Existing accounts are validated under the same current policy as new ones.
    from .acceptance import release_root, LOCK
    from .scientific_account_lint import validate_scientific_account
    for identity in existing:
        document = next(c['dapper_context'] for c in reused['contexts'] if c['selection']['object_id'] == identity)
        account = next(a for a in document['scientific_accounts'] if a['id'] == identity)
        require(account['question'] == seed['selection']['knowledge_gap_id'], 'Reused account answers another question')
        path = Path(output)/('reused-'+digest(identity)+'.json'); path.write_bytes(canonical_json(document))
        validate_scientific_account(path, dapper_root=release_root(), release_lock=LOCK, evidence_package=target)
    return target, reused, existing


def release(tx, job):
    if job.get('kind') != 'analysis': return
    row = tx.get('local_work', job['id'])
    if not row: return
    row['data'].update(state='closed', closed_at=now())
    tx.put('local_work', job['id'], row['owner'], row['data'])
    ResearchWorkService.release_pin_if_idle(tx, row['data'])
