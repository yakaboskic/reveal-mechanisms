"""Local-only canonical fixture persistence, without jobs, reviews, or publication.

Uses the normal scientific read model; never invokes worker acceptance. Uploads
finish before the short database transaction. A receipt inventories only newly
inserted records so shared scientific objects are never treated as fixture-owned.
"""
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

from .acceptance import LOCK, object_envelope, release_root
from .citations import validate_record
from .evidence_package import canonical_json, sha256
from .repository import digest, now
from .scientific_account_lint import lint_scientific_account

LOCAL_PREFIX = 'reveal_workflow_local'
FIXTURE_VERSION = 'bubble-account-v1'


def check(condition, message):
    if not condition:
        raise ValueError(message)


def load_fixture(directory):
    root = Path(directory).resolve()
    manifest = json.loads((root / 'manifest.json').read_text())
    check(manifest.get('format') == 'reveal.canonical-fixture/1' and manifest.get('fixture_version') == FIXTURE_VERSION,
          'Unsupported canonical fixture version')
    check(manifest['dapper_release_lock_sha256'] == sha256(LOCK.read_bytes()), 'Fixture DAPPER release pin changed; rebuild explicitly')
    check(digest(manifest['contents']) == manifest['content_sha256'], 'Fixture content manifest checksum differs')
    for name, checksum in manifest['contents'].items():
        path = (root / name).resolve()
        check(path.is_relative_to(root), 'Fixture path escapes its directory')
        check(path.is_file() and sha256(path.read_bytes()) == checksum, 'Fixture bytes changed: ' + name)
    document = json.loads((root / 'scientific-account.json').read_text())
    check(len(document['scientific_accounts']) == 1 and document['scientific_accounts'][0]['id'] == manifest['account_id'], 'Fixture account identity differs')
    check(document['scientific_accounts'][0]['question'] == manifest['selected_gap']['id'], 'Fixture gap binding differs')
    source = manifest['source_binding']
    source_path = 'sources/' + Path(source['source_file']).name
    check(manifest['selected_gap']['source_revision'] == source['source_file_sha256'] == manifest['contents'].get(source_path),
          'Imported source binding is not backed by retained source bytes')
    files = {file['id']: file for file in document['files']}
    check(set(files) == {item['id'] for item in manifest['files']}, 'Fixture artifact inventory differs')
    for item in manifest['files']:
        path = (root / item['path']).resolve()
        check(path.is_relative_to(root) and item['path'] in manifest['contents'], 'Untracked fixture source path')
        raw = path.read_bytes(); file = files[item['id']]
        check(sha256(raw) == item['sha256'] == file['sha256'] and len(raw) == item['size_bytes'] == file['size_in_bytes'], 'Fixture file checksum/size differs')
    report = lint_scientific_account(root / 'scientific-account.json', dapper_root=release_root(), release_lock=LOCK, mode='profile-only')
    check(report['valid'], 'Fixture fails pinned DAPPER validation: ' + json.dumps(report['findings']))
    metadata = json.loads((root / 'citation-registry.json').read_text())
    for record in metadata:
        validate_record(record)
    return {'root': root, 'manifest': manifest, 'document': document, 'metadata': metadata, 'validation': report}


def target(repository, owner, fixture, catalog, base_url):
    check(repository.table_prefix == LOCAL_PREFIX, 'Fixture seeding requires isolated reveal_workflow_local tables')
    check(str(UUID(owner)) == owner, 'Pass an exact workspace owner UUID')
    parsed = urlsplit(base_url)
    check(parsed.scheme in ('http', 'https') and parsed.hostname in ('localhost', '127.0.0.1', '::1')
          and not parsed.username and not parsed.query and not parsed.fragment and parsed.path in ('', '/'),
          'Fixture URLs must use a local application origin')
    gap = catalog.selected(fixture['manifest']['selected_gap'])
    check(gap['object'] == fixture['document']['knowledge_gaps'][0], 'Imported gap payload differs from the fixture; do not rebind by text')
    with repository.read_transaction() as tx:
        principal = tx.get('principal', owner)
        check(principal is not None and principal['owner'] == owner and not principal['data'].get('retired'), 'Target workspace owner is unavailable')
        check(principal['data'].get('me', {}).get('user_id') == owner, 'Target principal identity differs')
        expires = principal['data'].get('me', {}).get('workspace_expires_at')
        check(not expires or expires > now(), 'Target anonymous workspace has expired')
    return gap


def receipt_id(owner, manifest):
    return digest([owner, 'canonical-fixture', manifest['fixture_version'], manifest['content_sha256']])


def dry_run(repository, owner, fixture, catalog, base_url='http://localhost:3000'):
    target(repository, owner, fixture, catalog, base_url)
    manifest = fixture['manifest']; identity = receipt_id(owner, manifest)
    with repository.read_transaction() as tx:
        previous = tx.get('fixture_seed', identity)
        account = tx.get('account', digest([owner, manifest['account_id']]))
        if account and not previous:
            check(account['data'].get('fixture_origin', {}).get('content_sha256') == manifest['content_sha256'], 'Account exists outside this fixture seed; refusing overwrite')
        for item in manifest['files']:
            artifact = tx.get('artifact', digest([owner, item['sha256']]))
            if artifact:
                check(artifact['owner'] == owner and artifact['data'].get('sha256') == item['sha256'], 'Existing artifact binding differs')
                check(artifact['data'].get('storage', {}).get('store') == 's3', 'Shared artifact needs its own S3 migration before fixture seeding; refusing overwrite')
    return {'mode': 'dry-run', 'table_prefix': repository.table_prefix, 'owner_user_id': owner,
        'fixture_version': manifest['fixture_version'], 'content_sha256': manifest['content_sha256'],
        'account_id': manifest['account_id'], 'selected_gap': manifest['selected_gap'],
        'already_seeded': bool(previous), 'receipt_id': identity, 'counts': manifest['counts'],
        'artifact_bytes': sum(item['size_bytes'] for item in manifest['files']), 'writes': 0,
        'jobs_to_dispatch': 0, 'scientific_acceptance': 'not_reviewed'}


def _prepare(fixture, owner, storage, base_url):
    manifest = fixture['manifest']; document = deepcopy(fixture['document'])
    origin = {'kind': 'canonical_fixture', 'fixture_version': manifest['fixture_version'],
              'content_sha256': manifest['content_sha256'], 'scientific_acceptance': 'not_reviewed'}
    file_map = {file['id']: file for file in document['files']}
    retained = {}; access = {}
    for item in manifest['files']:
        file = file_map[item['id']]; data = (fixture['root'] / item['path']).read_bytes()
        # Validate again at use time so a changed file cannot be uploaded after planning.
        check(sha256(data) == item['sha256'], 'Source changed before retention')
        reference = storage.put(data, file['mime_type'])
        check(reference.get('store') == 's3' and reference.get('version_id') not in (None, '', 'null')
              and reference.get('sha256') == item['sha256'] and reference.get('size_bytes') == len(data), 'Storage returned an invalid immutable reference')
        check(storage.get(reference) == data, 'Retained fixture bytes failed readback verification')
        file['location'] = f's3://{reference["bucket"]}/{reference["key"]}'
        retained[file['sha256']] = {'sha256': file['sha256'], 'file': file, 'storage': reference,
                                     'job_id': None, 'fixture_origin': origin}
        access[file['id']] = {'file': file, 'download_url': base_url.rstrip('/') + '/api/backend/v1/artifacts/' + file['sha256'],
                             'expires_at': None, 'availability': 'available', 'verification': 'checksum_verified'}
    for dataset in document.get('datasets', []):
        # Dataset is a logical distribution; has_file preserves exact membership.
        first = retained[file_map[dataset['has_file'][0]]['sha256']]['storage']
        dataset['location'] = f's3://{first["bucket"]}/' + first['key'].split('artifacts/sha256/')[0] + 'artifacts/sha256/'
    metadata = deepcopy(fixture['metadata'])
    for record in metadata:
        record['object_payload_ref'] = base_url.rstrip('/') + '/api/backend/v1/objects/' + quote(record['target_id'], safe='')
        record['canonical_url'] = base_url.rstrip('/') + '/id/' + quote(record['target_id'], safe='')
        record.pop('metadata_checksum', None); record['metadata_checksum'] = digest(record); validate_record(record)
    return document, metadata, retained, access, origin


def apply_seed(repository, owner, fixture, catalog, storage, base_url='http://localhost:3000'):
    plan = dry_run(repository, owner, fixture, catalog, base_url)
    if plan['already_seeded']:
        return verify_seed(repository, owner, fixture, catalog, storage, base_url)
    document, metadata, retained, access, origin = _prepare(fixture, owner, storage, base_url)
    # Citation revisions are global and immutable. Reuse the exact registered
    # revision for shared nodes (especially the imported gap).
    with repository.read_transaction() as tx:
        metadata = [deepcopy(existing['data']) if (existing := tx.get('citation', f'{record["target_id"]}:1')) else record for record in metadata]
    manifest = fixture['manifest']; identity = receipt_id(owner, manifest); account = document['scientific_accounts'][0]
    document_sha = sha256(canonical_json(document)); account_key = digest([owner, account['id']])
    nodes = [node for rows in document.values() if isinstance(rows, list) for node in rows
             if isinstance(node, dict) and str(node.get('id', '')).startswith('dapper:')]
    # Projection is CPU work; calculate outside the application write fence.
    projections = {node['id']: object_envelope(document, node['id'], metadata, access) for node in nodes}
    envelope = deepcopy(projections[account['id']])
    state = {'status': 'not_requested', 'job_id': None, 'paragraph_id': None}
    envelope.update(research_statement=state, fixture_origin=origin)
    summary = {'account': account, 'knowledge_gap': document['knowledge_gaps'][0], 'claim_count': len(account['component_claims']),
        'created_at': manifest['authored_at'], 'job_id': None, 'research_statement': state, 'attribution': None, 'fixture_origin': origin}
    records = [('account', account_key, {'result': envelope, 'summary': summary, 'fixture_origin': origin}),
        ('account_membership', account_key, {'account_id': account['id'], 'summary': summary, 'fixture_origin': origin}),
        ('scientific_document', digest([owner, document_sha]), {'sha256': document_sha, 'document': document, 'job_id': None,
            'observed_at': manifest['authored_at'], 'citation_metadata': metadata, 'artifact_access': access, 'fixture_origin': origin})]
    records += [('artifact', digest([owner, checksum]), record) for checksum, record in retained.items()]
    records += [('citation', f'{record["target_id"]}:1', record) for record in metadata]
    for node in nodes:
        node_id = node['id']; key = digest([owner, node_id])
        records.extend([('grant', key, {'target_id': node_id}), ('object', key, projections[node_id]),
            ('object_document', key, {'object_id': node_id, 'sha256': document_sha}),
            ('object_observation', digest([owner, node_id, sha256(canonical_json(node))]),
             {'object_id': node_id, 'payload': node, 'document_sha256': document_sha})])
    with repository.transaction() as tx:
        prior = tx.get('fixture_seed', identity)
        if prior:
            return prior['data']
        principal = tx.get('principal', owner)
        check(principal is not None and principal['owner'] == owner and not principal['data'].get('retired'), 'Workspace owner changed before seed commit')
        inserted, reused = [], []
        for kind, key, data in records:
            previous = tx.get(kind, key)
            if previous:
                if kind != 'citation':
                    check(previous['owner'] == owner, 'Existing fixture record belongs to another owner')
                if kind in ('account', 'account_membership'):
                    check(previous['data'].get('fixture_origin') == origin, 'Refusing to overwrite unrelated account work')
                if kind == 'artifact':
                    check(previous['data']['sha256'] == data['sha256'], 'Existing artifact binding differs')
                if kind == 'citation':
                    check(previous['data'] == data, 'Citation registration changed during seed; retry')
                reused.append({'kind': kind, 'id': key})
            else:
                tx.put(kind, key, owner, data); inserted.append({'kind': kind, 'id': key})
        receipt = {'format': 'reveal.fixture-seed-receipt/1', 'fixture_version': manifest['fixture_version'],
            'content_sha256': manifest['content_sha256'], 'owner_user_id': owner, 'table_prefix': repository.table_prefix,
            'account_id': account['id'], 'selected_gap': manifest['selected_gap'], 'scientific_document_sha256': document_sha,
            'inserted_records': inserted, 'reused_records': reused, 'artifacts': list(retained.values()),
            'account_url': base_url.rstrip('/') + '/accounts/' + quote(account['id'], safe=''),
            'gap_url': base_url.rstrip('/') + '/knowledge-gaps/' + quote(manifest['selected_gap']['id'], safe=''),
            'seeded_at': now(), 'receipt_id': identity, 'scientific_acceptance': 'not_reviewed', 'jobs_dispatched': 0}
        tx.put('fixture_seed', identity, owner, receipt)
    return receipt


def verify_seed(repository, owner, fixture, catalog, storage, base_url='http://localhost:3000'):
    target(repository, owner, fixture, catalog, base_url)
    manifest = fixture['manifest']; identity = receipt_id(owner, manifest)
    with repository.read_transaction() as tx:
        row = tx.get('fixture_seed', identity)
        check(row is not None and row['owner'] == owner, 'Fixture is not seeded for this owner')
        receipt = deepcopy(row['data'])
        for key in receipt['inserted_records'] + receipt['reused_records']:
            record = tx.get(key['kind'], key['id'])
            check(record is not None and (key['kind'] == 'citation' or record['owner'] == owner), 'Seeded record is missing or inaccessible: ' + key['kind'])
        account = tx.get('account', digest([owner, manifest['account_id']]))['data']
        check(account['summary']['job_id'] is None and account['result']['research_statement']['status'] == 'not_requested', 'Fixture unexpectedly has a research job or paragraph')
        check(account.get('fixture_origin', {}).get('content_sha256') == manifest['content_sha256'], 'Fixture origin differs')
        stored = tx.get('scientific_document', digest([owner, receipt['scientific_document_sha256']]))['data']
        check(sha256(canonical_json(stored['document'])) == receipt['scientific_document_sha256'], 'Stored scientific document checksum differs')
        check(stored['document']['scientific_accounts'][0]['id'] == manifest['account_id'], 'Stored account identity differs')
        for node in [node for rows in stored['document'].values() if isinstance(rows, list) for node in rows if 'id' in node]:
            observation = tx.get('object_observation', digest([owner, node['id'], sha256(canonical_json(node))]))
            check(observation is not None and observation['data']['payload'] == node, 'Exact scientific observation is missing')
        artifact_records = [tx.get('artifact', digest([owner, item['sha256']]))['data'] for item in manifest['files']]
    # Actual stored API download references are checked, including reused artifacts.
    for artifact in artifact_records:
        reference = artifact.get('storage')
        check(reference is not None, 'Artifact is not retained in S3')
        data = storage.get(reference)
        check(sha256(data) == artifact['sha256'], 'Seeded artifact checksum differs')
    return {**receipt, 'verified': True}
