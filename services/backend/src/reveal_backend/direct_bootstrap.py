"""Prepare once, then let Box fetch its frozen input directly from S3."""
import hashlib
import json
import math
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit

from .box_adapter import BoxConfigurationError, make_bundle
from .box_mcp import GRAPHS
from .box_research import validate_context, network_policy
from .direct_capture import canonical, remote_session, require, storage_hosts

FORMAT = 'reveal.box-bootstrap/1'
MAX_BUNDLE = 25_000_000
POLICY = {'mode': 'custom', 'allowed_domains': ['api.anthropic.com', 'apps.okn.us', 'github.com']}
CONFIG_KEYS = {'job_id','attempt','kind','selected_graphs','timeout_seconds','max_budget_usd',
    'max_turns','model','claude_version','input_sha256','validation_feedback'}


def validate_config(config):
    require(isinstance(config, dict) and (set(config) == CONFIG_KEYS or set(config) == CONFIG_KEYS | {'research_context'}), 'Invalid frozen bootstrap configuration')
    require(isinstance(config['job_id'], str) and 0 < len(config['job_id']) <= 256
        and type(config['attempt']) is int and config['attempt'] > 0, 'Invalid bootstrap execution identity')
    if 'research_context' in config:
        require(config['kind'] == 'research', 'Only research may use a shared data context')
        validate_context(config['research_context'])
    graphs = config['selected_graphs']
    require(isinstance(graphs, list) and all(isinstance(graph, str) for graph in graphs)
        and len(graphs) == len(set(graphs)) and not set(graphs) - set(GRAPHS)
        and config['kind'] in ('research','paragraph') and (config['kind'] != 'paragraph' or not graphs),
        'Invalid bootstrap graph or execution kind')
    require(type(config['timeout_seconds']) is int and 10 <= config['timeout_seconds'] <= 3600
        and type(config['max_turns']) is int and 1 <= config['max_turns'] <= 100
        and type(config['max_budget_usd']) in (int,float) and math.isfinite(config['max_budget_usd'])
        and config['max_budget_usd'] > 0, 'Invalid bootstrap execution limits')
    require(isinstance(config['input_sha256'], str) and re.fullmatch('[a-f0-9]{64}', config['input_sha256'])
        and isinstance(config['model'], str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', config['model'])
        and isinstance(config['claude_version'], str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', config['claude_version']),
        'Invalid frozen bootstrap runtime')
    feedback = config['validation_feedback']
    require(isinstance(feedback, list) and len(feedback) <= 10
        and all(isinstance(value, str) and len(value) <= 4000 for value in feedback), 'Invalid bootstrap feedback')
    require(len(canonical(config)) <= 65536, 'Bootstrap configuration exceeds limit')


def freeze_bootstrap(adapter, request, storage):
    """Run scientific validation while newly prepared evidence is already local."""
    config = adapter.request_config(request)
    validate_config(config)
    bundle = make_bundle(adapter.project_root, request)
    reference = storage.put(bundle, 'application/gzip')
    return {'format': FORMAT, 'bundle': reference, 'config': config,
        'fingerprint': hashlib.sha256(bundle + json.dumps(config, sort_keys=True).encode()).hexdigest()}


def validate_descriptor(descriptor, handle, storage):
    require(isinstance(descriptor, dict) and set(descriptor) == {'format','bundle','config','fingerprint'}
        and descriptor['format'] == FORMAT, 'Invalid frozen bootstrap descriptor')
    validate_config(descriptor['config'])
    config = descriptor['config']
    require(config['job_id'] == handle.get('job_id') and config['attempt'] == handle.get('attempt')
        and handle.get('phase') in ('created','prepared'), 'Bootstrap belongs to another or launched Box execution')
    require(isinstance(descriptor['bundle'], dict), 'Invalid frozen bootstrap reference')
    storage.validate(descriptor['bundle'])
    require(0 < descriptor['bundle']['size_bytes'] <= MAX_BUNDLE
        and isinstance(descriptor['fingerprint'], str) and re.fullmatch('[a-f0-9]{64}', descriptor['fingerprint']),
        'Invalid frozen bootstrap size or fingerprint')


def download_ticket(storage, reference):
    parameters = storage.validate(reference)
    url = storage.signer.generate_presigned_url('get_object', Params=parameters, ExpiresIn=300, HttpMethod='GET')
    parsed = urlsplit(url)
    require(parsed.scheme == 'https' and parsed.netloc in storage_hosts(storage) and not parsed.username
        and not parsed.fragment and unquote(parsed.path) == '/' + reference['key']
        and parse_qs(parsed.query).get('versionId') == [reference['version_id']],
        'Bootstrap download escaped its exact immutable S3 object')
    return {'url':url, 'host':parsed.netloc}


async def prepare_from_store(adapter, descriptor, handle, storage, *, research_access=None):
    validate_descriptor(descriptor, handle, storage)
    box = await adapter.connect(handle)
    try:
        marker = await adapter.command(box, "sudo -n sh -c 'if [ -f /reveal/state/bootstrap-ready ]; then cat /reveal/state/bootstrap-ready; fi'")
        fingerprint = descriptor['fingerprint']
        if marker.strip():
            if marker.strip() != fingerprint:
                raise BoxConfigurationError('Existing Box bootstrap belongs to different frozen input or harness')
            context = descriptor['config'].get('research_context')
            if context:
                await adapter.finish_prepare(box, fingerprint, research_context=context, research_access=research_access)
            else:
                await box.update_network_policy(POLICY)
        else:
            from .workflow_execution import run_sync
            ticket = await run_sync(download_ticket, storage, descriptor['bundle'])
            verified = await remote_session(box, 'bootstrap', {'descriptor':descriptor, **ticket}, python='/usr/bin/python3')
            require(isinstance(verified, dict) and verified.get('fingerprint') == fingerprint,
                'Box did not verify its frozen bootstrap')
            # Only the small static installer travels over the control channel.
            # The input and harness bundle arrived directly from S3 above.
            script = adapter.bootstrap_script(descriptor['config']['claude_version'], unpack=False)
            await box.files.write(path='/tmp/reveal-bootstrap.sh', content=script)
            await adapter.command(box, 'sh /tmp/reveal-bootstrap.sh')
            context = descriptor['config'].get('research_context')
            if context:
                await adapter.finish_prepare(box, fingerprint, research_context=context, research_access=research_access)
            else:
                await adapter.finish_prepare(box, fingerprint)
        return dict(handle, phase='prepared', capture_protocol='s3-v1',
            timings={**handle.get('timings', {}), 'prepared_at':time.time()})
    finally:
        from .workflow_execution import drain_on_cancel
        await drain_on_cancel(box.aclose())
