"""Credential-free local workspaces; legacy ticket redemption for old downloads."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
import re
import secrets
import threading
from urllib.parse import quote, urlsplit
import zipfile

from .auth import Problem, owned, principal
from .evidence_package import canonical_json
from .repository import digest, now, uid
from .research_work import deadline, prompt, public_base, valid_principal
from .runtime_config import ROOT, setting
from . import user_inputs

SETUP_VERSION = 'reveal.local-setup/2'
TICKET_TTL = 30 * 60
MAX_FILES = 256
MAX_FILE_BYTES = 32_000_000
MAX_WORKSPACE_BYTES = 64_000_000
READERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix='reveal-setup')
_cache = OrderedDict()
_cache_lock = threading.Lock()
_sha = re.compile(r'[0-9a-f]{64}\Z')
_ticket = re.compile(r'rvls_[A-Za-z0-9_-]{43}\Z')


def invalid(detail='The retained research workspace is inconsistent.'):
    return Problem(409, 'SETUP_PACKAGE_INVALID', detail)


def safe_path(value):
    if (not isinstance(value, str) or len(value) > 240 or not re.fullmatch(r'[A-Za-z0-9_./-]+', value)
            or value.startswith('/') or any(part in ('', '.', '..') for part in value.split('/'))):
        raise invalid('A workspace artifact has an unsafe relative path.')
    for part in value.split('/'):
        if (part.endswith('.') or part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                *('COM'+str(n) for n in range(1, 10)), *('LPT'+str(n) for n in range(1, 10))}):
            raise invalid('A workspace artifact has an unsupported relative path.')
    return value


def urls(work_id):
    api = public_base()
    # Deployment-owned configuration only: a tunneled API can legitimately use
    # a loopback browser frontend. Never infer reconnect URLs from request headers.
    web = next((setting(name) for name in ('REVEAL_PUBLIC_WEB_URL', 'REVEAL_CANONICAL_URL',
        'NEXTAUTH_URL') if setting(name)), '').rstrip('/')
    if not web and urlsplit(api).hostname in ('localhost', '127.0.0.1', '::1'):
        web = 'http://localhost:3000'
    try:
        parsed = urlsplit(web)
        invalid_web = (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or '\\' in web
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in web)
            or '%' in (parsed.hostname or '') or parsed.netloc.endswith(':')
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or (parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1')))
    except ValueError:
        invalid_web = True
    if invalid_web:
        raise Problem(503, 'SETUP_NOT_CONFIGURED',
            'Configure a valid canonical browser URL with REVEAL_PUBLIC_WEB_URL, REVEAL_CANONICAL_URL or NEXTAUTH_URL.')
    return {'mcp_url': api+'/mcp', 'device_authorization_url': api+'/oauth/device_authorization',
        'token_url': api+'/oauth/token', 'revocation_url': api+'/oauth/revoke',
        'return_url': web+'/local-runs/'+quote(work_id, safe='')}


def active_work(tx, owner, work_id, package_sha256=None):
    valid_principal(tx, owner)
    work = owned(tx, 'local_work', work_id, owner)['data']
    if work.get('job_id'): raise Problem(404, 'NOT_FOUND', 'Local research unavailable.')
    if work['state'] == 'closed' or work['expires_at'] <= now():
        raise Problem(409, 'WORK_CLOSED', 'This research work is closed or expired.')
    if work['state'] != 'ready' or not work.get('package_id'):
        raise Problem(409, 'PACKAGE_NOT_READY', 'The research seed is not ready. Retry when preparation completes.')
    if package_sha256 is not None and work.get('package_sha256') != package_sha256:
        raise Problem(409, 'SETUP_PACKAGE_CHANGED', 'Download a setup workspace for the current research package.')
    return work


def _snapshot(tx, owner, work_id):
    work = active_work(tx, owner, work_id)
    package = owned(tx, 'research_package', work['package_id'], owner)['data']
    descriptors = package.get('artifacts')
    if not isinstance(descriptors, list) or not 1 <= len(descriptors) <= MAX_FILES:
        raise invalid('The seed artifact count is outside its allowed bounds.')
    rows = tx.get_many('research_artifact', [item.get('id') for item in descriptors])
    artifacts = []
    for descriptor in descriptors:
        row = rows.get(descriptor.get('id'))
        if not row or row['owner'] != owner: raise invalid('A seed artifact is unavailable.')
        value = row['data']
        if value.get('local_work_id') != work_id or value.get('purpose') != 'seed':
            raise invalid('A seed artifact belongs to a different research context.')
        if any(value.get(key) != descriptor.get(key) for key in ('id', 'filename', 'sha256', 'size_bytes', 'purpose')):
            raise invalid('A seed artifact differs from its package descriptor.')
        safe_path(value.get('filename'))
        if (not isinstance(value.get('sha256'), str) or not _sha.fullmatch(value['sha256'])
                or type(value.get('size_bytes')) is not int or not 0 <= value['size_bytes'] <= MAX_FILE_BYTES):
            raise invalid('A seed artifact has invalid checksum or size metadata.')
        artifacts.append(deepcopy(value))
    if sum(item['size_bytes'] for item in artifacts) > MAX_WORKSPACE_BYTES:
        raise Problem(413, 'SETUP_TOO_LARGE', 'The research workspace exceeds the download size limit.')
    return deepcopy(work), deepcopy(package), artifacts


def _read(artifact):
    try: raw = user_inputs.read(artifact['storage'])
    except Exception:
        raise invalid('A retained seed artifact cannot be read or verified.') from None
    if len(raw) != artifact['size_bytes'] or hashlib.sha256(raw).hexdigest() != artifact['sha256']:
        raise invalid('A retained seed artifact failed checksum verification.')
    return artifact['filename'], raw


def _archive(work, package, artifacts, client, connection, launcher):
    """Return credential-free ZIP bytes; exact source bytes are never regenerated."""
    key = digest([SETUP_VERSION, work['id'], work['package_sha256'], package, artifacts,
        client, connection, hashlib.sha256(launcher).hexdigest()])
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    raw_files = {}
    pending = [READERS.submit(_read, artifact) for artifact in artifacts]
    try:
        for future in pending:
            path, raw = future.result()
            if path in raw_files: raise invalid('The seed contains duplicate artifact paths.')
            raw_files[path] = raw
    finally:
        for future in pending: future.cancel()
    try:
        raw_package = raw_files['evidence-package.json']
        raw_manifest = raw_files['manifest.json']
        seed = json.loads(raw_package); seed_manifest = json.loads(raw_manifest)
    except (KeyError, ValueError, TypeError): raise invalid('The retained seed or manifest is missing or malformed.') from None
    if (hashlib.sha256(raw_package).hexdigest() != work['package_sha256']
            or package.get('sha256') != work['package_sha256'] or seed != package.get('package')
            or seed_manifest != package.get('manifest') or seed_manifest.get('package_sha256') != work['package_sha256']):
        raise invalid('The retained seed differs from its frozen package binding.')
    hashes = {path: hashlib.sha256(raw).hexdigest() for path, raw in raw_files.items() if path != 'manifest.json'}
    if seed_manifest.get('files') != hashes:
        raise invalid('The retained seed manifest does not match its complete artifact inventory.')
    context = seed.get('research_context', {})
    if context.get('local_work_id') != work['id'] or context.get('research_request_id') != work['research_request_id']:
        raise invalid('The seed names a different research context.')
    files = {}; normalized = set()
    def add(path, raw):
        safe_path(path)
        folded = path.casefold()
        if (folded in normalized or any(folded.startswith(old+'/') or old.startswith(folded+'/') for old in normalized)):
            raise invalid('The workspace contains conflicting artifact paths.')
        files[path] = raw; normalized.add(folded)
        if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_WORKSPACE_BYTES:
            raise Problem(413, 'SETUP_TOO_LARGE', 'The research workspace exceeds the download size limit.')
    for path, raw in raw_files.items():
        if path not in ('evidence-package.json', 'manifest.json') and not path.startswith(('sources/', 'package-sections/')) and path != 'evidence-index.json':
            raise invalid('A seed artifact is outside the declared input layout.')
        add('input/'+path, raw)
    sources = seed.get('source_artifacts', {})
    for source in sources.values():
        raw = raw_files.get(source.get('path'))
        if raw is None or len(raw) != source.get('size_bytes') or hashlib.sha256(raw).hexdigest() != source.get('sha256'):
            raise invalid('A declared source artifact is missing or changed.')
    kit = seed.get('authoring_kit', {})
    entries = kit.get('files')
    if not isinstance(entries, list) or not entries or kit.get('kit_sha256') != hashlib.sha256(canonical_json(entries)).hexdigest():
        raise invalid('The authoring-kit manifest is invalid.')
    for entry in entries:
        source = sources.get(entry.get('artifact_id'), {})
        raw = raw_files.get(source.get('path'))
        if raw is None or hashlib.sha256(raw).hexdigest() != entry.get('sha256'):
            raise invalid('An authoring-kit source is missing or changed.')
        target = safe_path(entry.get('path'))
        if not target.startswith(('docs/', 'scripts/', 'services/backend/', 'input/package-sections/')):
            raise invalid('An authoring-kit file is outside its allowed workspace directories.')
        if target not in files:
            add(target, raw)
        elif files[target] != raw:
            raise invalid('Conflicting authoring artifact.')
    server = 'reveal_'+work['id'].replace('-', '_')
    if not re.fullmatch(r'[A-Za-z0-9_]+', server): raise invalid('The local research identifier is invalid.')
    add('.codex/config.toml', ('[mcp_servers.'+server+']\ncommand = "python3"\n'
        'args = ["start.py", "--mcp"]\n').encode())
    add('.mcp.json', canonical_json({'mcpServers': {server: {'type': 'stdio', 'command': 'python3',
        'args': ['start.py', '--mcp']}}}))
    guidance = ('Read RESEARCH.md, docs/authoring-contract.md and input/evidence-index.json before investigating. '
        'Use the pinned authoring instructions included in this workspace. Keep outputs in output/. '
        'Public Reveal tools work without sign-in. Read the connection instructions in RESEARCH.md before contributing. '
        'Do not read local credential state. Never include credentials in prompts or artifacts. '
        'The bundled lint source is not an installed offline validator.\n')
    add('AGENTS.md', guidance.encode()); add('CLAUDE.md', guidance.encode())
    add('RESEARCH.md', ('# Reveal local research\n\n'
        'The frozen research seed and source files are in input/. Read input/evidence-index.json and use read_evidence for exact selections; '
        'verify input/manifest.json; read authoring_kit.files for the included instructions. '
        'Save authored documents in output/ and materialized evidence in evidence/closures/. This workspace contains no Reveal credentials.\n\n'
        '## Explore without signing in\n\n'
        'Search existing public scientific accounts, Propositions and Claims before authoring new science. '
        'Use public loaded-reference tools with reference_generation_id='+work['reference_generation_id']+'. '
        'The connection helper fills this version automatically. Only the two explicitly offered small-model '
        'phenotype tools may query BioIndex. For connected knowledge graphs, call list_knowledge_graphs, then '
        'get_schema or describe_kg with graph, and query_graph with graph plus bounded triple arguments. These '
        'tools also include sparql_query with graph, a read-only SELECT query over that literal named GRAPH, '
        'and an optional limit of 1–100. SELECT bindings retain their query semantics; they are not necessarily triples. '
        'Live graph reads do not use reference_generation_id. Only evidence from graphs selected in '
        'input/evidence-package.json external_evidence.selected_graphs can be attached to this research. '
        'Preserve public capture IDs, exact payloads, coverage and source identities. '
        'Use get_public_capture to download retained evidence before its stated expiry. Seek relevant CFDE grounding, '
        'but supported accounts without it are allowed. Preserve authorship of reused science.\n\n'
        '## Contribute when ready\n\n'
        'Server validation, private research, evidence uploads and submission require a registered Reveal sign-in. '
        'Call connect_reveal to get a browser link and code; show them to the user. Call get_reveal_connection '
        'after approval. Never ask the user to paste tokens. The helper stores and refreshes credentials securely. '
        'Then get_local_work with local_work_id='+work['id']+' and verify package_sha256='+work['package_sha256']+'. '
        'Attach selected public evidence with attach_public_captures; reuse public science with reuse_scientific_objects '
        'so Reveal rechecks its exact identity and permissions. Use upload_research_files for local account/evidence '
        'files; it handles authenticated transfers without exposing credentials. Import independent evidence, export '
        'the selected evidence context and poll get_operation, materialize_evidence_context, then validate_submission and repair errors. Validation retains private results and normalized artifacts; it neither accepts nor publishes. Submit_accounts separately accepts accounts only when authorized. '
        'Return submission IDs/account links. Do not publish automatically or start hosted agents.\n\n'
        '## Launcher\n\n'
        '`python3 start.py codex` or `python3 start.py claude` starts your agent without requiring Reveal sign-in. '
        'Add `--offline` to disable remote Reveal calls while retaining the local read_evidence tool. `--check-only` tests anonymous MCP without '
        'starting a model; `--login` opens device sign-in; `--logout` disconnects while preserving local files. '
        'You can instead call disconnect_reveal from your agent. A complete offline DAPPER validator is not installed.\n').encode())
    add('.gitignore', b'setup.json\n.reveal-setup/\n__pycache__/\n*.pyc\n')
    add('output/.gitkeep', b''); add('start.py', launcher)
    manifest = {'schema_version': 1, 'setup_version': SETUP_VERSION, 'client': client,
        'local_work_id': work['id'], 'research_request_id': work['research_request_id'],
        'reference_generation_id': work['reference_generation_id'], 'package_sha256': work['package_sha256'], **connection,
        'files': [{'path': path, 'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}
            for path, raw in sorted(files.items())]}
    add('setup-manifest.json', canonical_json(manifest))
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, raw in sorted(files.items()):
            info = zipfile.ZipInfo('reveal-'+work['id']+'/'+path)
            info.external_attr = (0o100755 if path == 'start.py' else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
    result = output.getvalue()
    with _cache_lock:
        _cache[key] = result; _cache.move_to_end(key)
        while len(_cache) > 8 or sum(map(len, _cache.values())) > MAX_WORKSPACE_BYTES:
            _cache.popitem(last=False)
    return result


def build_setup(repository, authorization, work_id, client):
    if client not in ('codex', 'claude_code'):
        raise Problem(422, 'INVALID_REQUEST', 'Choose codex or claude_code.')
    with repository.read_transaction() as tx:
        owner = principal(tx, authorization)['user_id']
        work, package, artifacts = _snapshot(tx, owner, work_id)
    connection = urls(work_id)
    try: launcher = (ROOT/'services/backend/agent-runtime/reveal_local_launcher.py').read_bytes()
    except OSError: raise Problem(503, 'SETUP_NOT_CONFIGURED', 'The local workspace launcher is unavailable.') from None
    archive = _archive(work, package, artifacts, client, connection, launcher)
    # A download grants access only to these bytes. Recheck ownership after the
    # potentially slow artifact reads, without minting connection authority.
    with repository.read_transaction() as tx:
        current_owner = principal(tx, authorization)['user_id']
        if current_owner != owner: raise Problem(401, 'SESSION_EXPIRED', 'The workspace identity changed.')
        active_work(tx, owner, work_id, work['package_sha256'])
    return archive


def exchange(tx, ticket, token_sha256, authorization=None):
    if (not isinstance(ticket, str) or not _ticket.fullmatch(ticket)
            or not isinstance(token_sha256, str) or not _sha.fullmatch(token_sha256)):
        raise Problem(422, 'INVALID_REQUEST', 'Supply a setup ticket and a lowercase SHA-256 bearer-token digest.')
    identity = hashlib.sha256(ticket.encode()).hexdigest()
    row = tx.get('research_setup_ticket', identity)
    if not row: raise Problem(401, 'SETUP_TICKET_INVALID', 'Download a new setup workspace in Reveal.')
    value = row['data']; owner = row['owner']
    if value.get('issued_principal_kind') != 'registered':
        raise Problem(403, 'SIGN_IN_REQUIRED', 'This old setup ticket requires a fresh browser sign-in. Download the current workspace launcher.')
    if owner != value['issued_owner_user_id'] or value.get('revoked_at'):
        raise Problem(401, 'SETUP_TICKET_REVOKED', 'This setup ticket is no longer authorized.')
    # A consumed ticket never mints authority again. Its exact bound hash can
    # recover a lost acknowledgement while that grant remains valid.
    if value['expires_at'] <= now() and not value.get('token_sha256'):
        raise Problem(410, 'SETUP_TICKET_EXPIRED', 'Download a fresh setup ticket in Reveal.')
    work = active_work(tx, owner, value['local_work_id'], value['package_sha256'])
    if value['research_request_id'] != work['research_request_id']:
        raise Problem(409, 'SETUP_PACKAGE_CHANGED', 'This setup ticket belongs to a different frozen request.')
    existing = value.get('token_sha256')
    if existing and existing != token_sha256:
        raise Problem(409, 'SETUP_TICKET_USED', 'This ticket was already redeemed by a different installation.')
    if existing:
        grant = tx.get('research_access', existing)
        if (not grant or grant['owner'] != owner or grant['data'].get('revoked_at')
                or grant['data']['expires_at'] <= now() or grant['data']['grant_id'] != value['grant_id']
                or grant['data']['local_work_id'] != work['id'] or grant['data']['kind'] != 'local'
                or grant['data']['research_request_id'] != work['research_request_id']):
            raise Problem(401, 'MCP_GRANT_EXPIRED', 'The connection expired or was revoked; reconnect in Reveal.')
        grant = grant['data']
    else:
        previous = None
        if authorization is not None:
            # The fresh ticket authorizes this reconnect. An expired/revoked
            # previous credential may identify the installation being replaced.
            if not isinstance(authorization, str) or not authorization.startswith('Bearer rvlm_'):
                raise Problem(401, 'MCP_AUTH_REQUIRED', 'Supply the prior local research credential.')
            previous_row = tx.get('research_access', hashlib.sha256(authorization[7:].encode()).hexdigest())
            if (not previous_row or previous_row['owner'] != owner
                    or previous_row['data']['local_work_id'] != work['id']
                    or previous_row['data']['research_request_id'] != work['research_request_id']
                    or previous_row['data']['kind'] != 'local'):
                raise Problem(403, 'SETUP_SCOPE_MISMATCH', 'The connection belongs to different local research.')
            previous = previous_row['data']
        if tx.get('research_access', token_sha256):
            raise Problem(409, 'SETUP_TOKEN_CONFLICT', 'Generate a new local bearer secret before redeeming this ticket.')
        active = {row['data']['grant_id'] for row in tx.list('research_access', owner) if row['data']['local_work_id'] == work['id']
            and not row['data'].get('revoked_at') and row['data']['expires_at'] > now()}
        active.update(row['id'] for row in tx.list('research_oauth_family', owner)
            if row['data']['local_work_id'] == work['id'] and not row['data'].get('revoked_at') and row['data']['expires_at'] > now())
        previous_active = bool(previous and not previous.get('revoked_at') and previous['expires_at'] > now())
        if len(active) - previous_active >= 5:
            raise Problem(429, 'GRANT_LIMIT', 'Revoke an old connection before adding another.')
        me = valid_principal(tx, owner)
        ttl = max(1, min(7 * 86400, int(setting('REVEAL_LOCAL_GRANT_TTL_SECONDS', str(7 * 86400)))))
        grant = {'grant_id': uid(), 'local_work_id': work['id'], 'research_request_id': work['research_request_id'],
            'issued_principal_kind': 'registered',
            'expires_at': min(deadline(ttl), me.get('workspace_expires_at') or '9999', work['expires_at']),
            'created_at': now(), 'revoked_at': None, 'kind': 'local', 'execution_id': None}
        tx.put('research_access', token_sha256, owner, grant)
        if previous:
            if previous.get('oauth_family_id'):
                from .research_oauth import revoke_oauth_grant
                revoke_oauth_grant(tx, owner, previous['grant_id'])
            previous.update(revoked_at=previous.get('revoked_at') or now(), revocation_reason='setup_reconnected')
            tx.put('research_access', hashlib.sha256(authorization[7:].encode()).hexdigest(), owner, previous)
        value.update(token_sha256=token_sha256, grant_id=grant['grant_id'], consumed_at=now())
        tx.put('research_setup_ticket', identity, owner, value)
    return {key: grant[key] for key in ('grant_id', 'expires_at', 'local_work_id')} | {
        'package_sha256': value['package_sha256'], 'mcp_url': value['connection']['mcp_url']}


def revoke_tickets(tx, owner, work_id):
    work = owned(tx, 'local_work', work_id, owner)['data']
    if work.get('job_id'): raise Problem(404, 'NOT_FOUND', 'Local research unavailable.')
    for row in tx.list('research_setup_ticket', owner):
        value = row['data']
        if value['local_work_id'] == work_id and not value.get('token_sha256') and not value.get('revoked_at'):
            value['revoked_at'] = now(); tx.put('research_setup_ticket', row['id'], owner, value)
