"""Trusted root process inside Box. Not an agent-authored executable.

The transport uploads this module and a minimal project bundle. Every fresh
attempt clones and verifies DAPPER before a separate unprivileged Claude uid runs.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time

from .box_agent import (INPUT_FILENAME as AGENT_INPUT_FILENAME, RESPONSE_FILENAME as AGENT_RESPONSE_FILENAME,
                        KeyProxy, agent_environment, deliverable_of as agent_deliverable, response_record, run_limits as agent_run_limits,
                        validate_input as validate_agent_input)
from .box_mcp import DraftValidationError, Ledger, PolicyError, ScopedTools, canonical, serve, stamp
from .box_stream import ClaudeStream, SecretFilter, StreamProtocolError
from .box_timing import RuntimeTiming, provider_metrics
from .public_tool_activity import DURABLE_TOOLS, durable_operation, tool_failed, tool_kind
from .box_research import HostedResearchClient, ResearchAccessError, validate_context
from .dispatch_view import (FILE_INPUT_FILENAME, FILE_INPUT_FORMAT, research_authoring_requirements,
                            research_prompt, validate_file_input)
from .evidence_files import INDEX_PATH, build_evidence_index
from .evidence_package import declare_trusted_prefixes, trusted_prefixes
from .evidence_reader import WorkspaceReader, read_artifact, READER_VERSION
from .box_literature import LiteratureClient
from .research_outcome import validate_insufficient_outcome
from .box_upload import MAX_TOTAL, capture_file_limit, source_file

BASE = Path('/reveal')
STATE = BASE / 'state'
OUTPUT = BASE / 'output'
SECRETS = ()
RESEARCH = None


def write_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(canonical(data)); temporary.replace(path)


def emit(kind, payload):
    raw = canonical({'type': kind, 'payload': payload})
    for secret in SECRETS:
        raw = raw.replace(secret.encode(), b'[REDACTED_CREDENTIAL]')
    with (STATE / 'events.jsonl').open('ab') as handle:
        handle.write(raw + b'\n')
        handle.flush(); os.fsync(handle.fileno())


def protect(root):
    for path in [root, *root.rglob('*')]:
        if path.is_symlink():
            continue
        os.chown(path, 0, 0)
        os.chmod(path, 0o555 if path.is_dir() or os.access(path, os.X_OK) else 0o444)



def prepare_writable_output(work):
    """One writable destination; relative output/ resolves to that same directory."""
    if OUTPUT.is_symlink(): raise ValueError('Canonical output directory cannot be a symlink')
    OUTPUT.mkdir(mode=0o700, exist_ok=True)
    user = pwd.getpwnam('reveal-agent')
    os.chown(OUTPUT, user.pw_uid, user.pw_gid); os.chmod(OUTPUT, 0o700)
    alias = work / 'output'
    if alias.exists() or alias.is_symlink():
        if not alias.is_symlink() or alias.resolve() != OUTPUT.resolve():
            raise ValueError('Workspace output path has an unexpected existing target')
    else: alias.symlink_to(OUTPUT, target_is_directory=True)


def verify_writable_output(work):
    user = pwd.getpwnam('reveal-agent')
    # Test as the actual authoring UID, after protecting the source workspace.
    script = '''import os, pathlib, sys
work, output = map(pathlib.Path, sys.argv[1:])
assert (work/'output').resolve() == output.resolve()
assert not os.access(work, os.W_OK)
path = work/'output'/'.trusted-write-probe'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try: os.write(fd, b'REVEAL output probe')
finally: os.close(fd); path.unlink()
'''
    result = subprocess.run(['setpriv', '--reuid', str(user.pw_uid), '--regid', str(user.pw_gid),
        '--clear-groups', '--no-new-privs', sys.executable, '-I', '-c', script, str(work), str(OUTPUT)],
        capture_output=True, timeout=10)
    if result.returncode: raise RuntimeError('Trusted output permission preflight failed before agent start')


def write_outcome_tool(value):
    if isinstance(value, dict) and value.get('format') == 'reveal.research-outcome/1' and value.get('status') == 'succeeded':
        if RESEARCH is None: raise DraftValidationError('Reuse outcomes require a progressive research context')
        allowed = {'format', 'status', 'existing_account_ids', 'receipt_ids', 'reuse_receipt_ids'}
        accounts = value.get('existing_account_ids', [])
        if set(value) - allowed or not isinstance(accounts, list) or not 1 <= len(accounts) <= 3 or not all(isinstance(item, str) for item in accounts) or len(set(accounts)) != len(accounts) or not set(accounts) <= RESEARCH.existing_account_ids:
            raise DraftValidationError('Select one to three exact-question accounts using authorized reuse receipts first')
        for key, known in (('receipt_ids', RESEARCH.receipt_ids), ('reuse_receipt_ids', RESEARCH.reuse_receipt_ids)):
            choices = value.get(key, [])
            if not isinstance(choices, list) or not all(isinstance(item, str) for item in choices) or not set(choices) <= known:
                raise DraftValidationError('Outcome receipts must belong to this captured research')
        outcome = {**value, 'receipt_ids': sorted(RESEARCH.receipt_ids), 'reuse_receipt_ids': sorted(RESEARCH.reuse_receipt_ids)}
        RESEARCH.materialize()
    else:
        try: outcome = validate_insufficient_outcome(value)
        except ValueError as error: raise DraftValidationError(str(error)) from None
    # Keep the legacy format distinguishable for trusted acceptance, including
    # its optional selected-gap binding. Never relabel old output as new schema.
    if 'format' not in value: outcome.pop('format')
    path = OUTPUT / 'outcome.json'
    if path.is_symlink(): raise PolicyError('Outcome output symlink forbidden')
    user = pwd.getpwnam('reveal-agent')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(canonical(outcome))
        os.fchown(handle.fileno(), user.pw_uid, user.pw_gid)
    return {'content': [{'type': 'text', 'text': 'Research outcome saved to ' + str(path) + '; no scientific account has been accepted.'}]}


def setup(request):
    from .dapper_release import clone_release, prepare_agent_workspace
    project = BASE / 'bundle'
    lock = project / 'services/backend/agent-runtime/dapper-release.json'
    if request['kind'] == 'research':
        runtime = prepare_agent_workspace(BASE / 'workspace', project, BASE / 'input/evidence-package.json', lock)
        work = Path(runtime['working_directory'])
        package = Path(runtime['evidence_package'])
        # The bootstrap already verifies all original artifact bytes.
        frozen = json.loads(package.read_text())
        if set(request['selected_graphs']) != set(frozen['external_evidence']['selected_graphs']):
            raise ValueError('Selected graph bindings differ from the immutable package')
        progressive = frozen.get('retrieval_mode') == 'progressive'
        if progressive:
            context = validate_context(frozen.get('research_context'))
            if request.get('research_context') != context:
                raise ValueError('Research context differs from the frozen seed')
            runtime['research_context'] = context
        from .dispatch_view import pinned_claim_structure_sha256, pinned_contract_sha256, pinned_kit_version, pinned_skeleton_sha256
        contract_sha256 = pinned_contract_sha256(frozen)
        prompt = research_prompt(request['selected_graphs'], request.get('validation_feedback', ()), progressive=progressive,
                                 contract_sha256=contract_sha256, skeleton_sha256=pinned_skeleton_sha256(frozen),
                                 claim_structure_sha256=pinned_claim_structure_sha256(frozen), lightning_audit=bool(frozen.get('lightning_audit')),
                                 kit_version=pinned_kit_version(frozen))
        frozen_input = BASE / 'input' / FILE_INPUT_FILENAME
        if frozen_input.exists():
            manifest = json.loads(frozen_input.read_bytes())
            validate_file_input(package.read_bytes(), manifest, prompt)
            runtime['file_input'] = manifest
        # A small index binds the shared bounded reader to original source bytes.
        source_bytes = {}
        for identity, item in frozen['source_artifacts'].items():
            source = (package.parent / item['path']).resolve()
            if not source.is_relative_to(package.parent.resolve()):
                raise ValueError('Evidence reader source path escapes input directory')
            source_bytes[identity] = source.read_bytes()
        reading_files = {INDEX_PATH: build_evidence_index(package.read_bytes(), source_bytes=source_bytes)}
        views = {}
        for relative, data in reading_files.items():
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            views[relative] = hashlib.sha256(data).hexdigest()
        runtime['derived_input_views'] = views
        runtime['evidence_reader'] = {'format': READER_VERSION, 'index_path': INDEX_PATH,
                                     'index_sha256': views[INDEX_PATH],
                                     'package_sha256': hashlib.sha256(package.read_bytes()).hexdigest(),
                                     'source_artifact_count': len(source_bytes),
                                     'file_count': len(reading_files),
                                     'total_bytes': sum(map(len, reading_files.values()))}
        runtime['research_prompt_sha256'] = hashlib.sha256(prompt.encode()).hexdigest()
        # This small authoring aid is separate from evidence navigation.
        sections = work / 'input/package-sections'
        sections.mkdir(exist_ok=True)
        from .authoring_contract import pinned_schema
        excerpt_path = sections / 'authoring-schema-excerpt.yaml'
        example_path = sections / 'authoring-examples.json'
        if contract_sha256 is None:
            excerpt_path.write_bytes(pinned_schema(project))
            shutil.copyfile(project / 'services/backend/agent-runtime/authoring-examples.json', example_path)
        for target in (excerpt_path, example_path):
            views[str(target.relative_to(work))] = hashlib.sha256(target.read_bytes()).hexdigest()
        runtime['authoring_contract'] = {'format': 'reveal.authoring-contract/2',
            'sha256': contract_sha256 or hashlib.sha256((work/'docs/authoring-contract.md').read_bytes()).hexdigest(),
            'reader_version': READER_VERSION,
            'schema_sha256': views[str(excerpt_path.relative_to(work))],
            'examples_sha256': views[str(example_path.relative_to(work))]}
    elif request['kind'] == 'agent':
        # A general agent job: no DAPPER release, evidence package or skill. The caller's prompt is the task.
        work = BASE / 'trusted'
        work.mkdir()
        shutil.copyfile(BASE / 'input' / AGENT_INPUT_FILENAME, work / AGENT_INPUT_FILENAME)
        agent_input = validate_agent_input(json.loads((work / AGENT_INPUT_FILENAME).read_bytes()))
        if agent_input['graphs'] != request['selected_graphs'] or agent_input['model'] != request['model']:
            raise ValueError('Agent graphs or model differ from the frozen input')
        if {k: request[k] for k in ('timeout_seconds', 'max_turns')} != {k: agent_input['limits'][k] for k in ('timeout_seconds', 'max_turns')} \
                or request['max_budget_usd'] > agent_input['limits']['max_budget_usd']:
            raise ValueError('Agent limits differ from the frozen input')
        runtime = {'runtime_version': 'reveal.agent-runtime/1', 'working_directory': str(work),
                   'evidence_package_sha256': hashlib.sha256((work / AGENT_INPUT_FILENAME).read_bytes()).hexdigest(),
                   'derived_input_views': {},
                   'agent': {**{key: agent_input[key] for key in ('tools', 'graphs', 'literature', 'network', 'allowed_domains', 'effort')},
                             'limits': agent_run_limits(agent_input),
                             # Frozen with the bootstrap: the one repository address this sandbox may read, or None.
                             'claims_url': (request.get('agent') or {}).get('claims_url')}}
        if agent_deliverable(agent_input) == 'scientific_account':
            account_workspace(work, project, lock, agent_input, runtime)
        prompt = agent_input['prompt']
    else:
        root = BASE / 'workspace'
        root.mkdir()
        release = clone_release(root / 'dapper', lock)
        work = BASE / 'trusted'
        work.mkdir()
        shutil.copyfile(BASE / 'input/paragraph-input.json', work / 'paragraph-input.json')
        paragraph_input = json.loads((work / 'paragraph-input.json').read_text())
        if paragraph_input.get('format') != 'reveal.paragraph-input/1' or request['selected_graphs']:
            raise ValueError('Invalid paragraph inputs or forbidden external tools')
        readable = work / 'paragraph-input-readable.json'
        readable.write_text(json.dumps(paragraph_input, ensure_ascii=False, indent=2) + '\n')
        skill = project / 'services/backend/agent-skills/write-cited-paragraph/SKILL.md'
        runtime = {'runtime_version': 'reveal.agent-runtime/1', 'dapper': release,
                   'dapper_root': str(root / 'dapper'), 'working_directory': str(work),
                   'evidence_package_sha256': hashlib.sha256((work / 'paragraph-input.json').read_bytes()).hexdigest(),
                   'derived_input_views': {readable.name: hashlib.sha256(readable.read_bytes()).hexdigest()}}
        prompt = f'Read and follow {skill}. Use only the frozen /reveal/trusted/paragraph-input.json; /reveal/trusted/paragraph-input-readable.json contains the identical object formatted across lines for economical reads. Write /reveal/output/paragraph.json. No fresh evidence or new propositions.'
    runtime.update({'provider': 'anthropic', 'model': request['model'], 'harness': 'Claude Code',
                    'harness_version': request['claude_version'], 'started_at': stamp(),
                    'job_id': request['job_id'], 'attempt': request['attempt'],
                    'execution_limits': {k: request[k] for k in ['timeout_seconds', 'max_budget_usd', 'max_turns']},
                    'input_sha256': request['input_sha256']})
    runtime['validation_feedback_sha256'] = hashlib.sha256(canonical(request.get('validation_feedback', []))).hexdigest()
    if request['kind'] == 'research' or runtime.get('deliverable') == 'scientific_account':
        executor_id = 'urn:reveal:runtime:executor'
        activity_id = 'urn:reveal:runtime:execution:' + request['job_id'] + ':' + str(request['attempt'])
        runtime['draft_attribution'] = {'organizations': [{'id': executor_id, 'name': 'REVEAL Mechanisms runtime executor'}],
                                      'activities': [{'id': activity_id, 'name': 'Claude Code research execution',
                                                      'command': 'reveal Box scientific-account authoring',
                                                      'software_name': 'Claude Code', 'software_version': request['claude_version'],
                                                      'generated_at_time': runtime['started_at']}]}
        (work / 'runtime-context.json').write_bytes(canonical(runtime['draft_attribution']))
    if request['kind'] == 'paragraph' and request.get('validation_feedback'):
        prompt += '\nTrusted independent review feedback from a rejected earlier draft. Address these constraints afresh; they are not new evidence:\n' + '\n'.join(request['validation_feedback'])
    prepare_writable_output(work)
    runtime['output_directory'] = str(OUTPUT)
    runtime['output_alias'] = str(work / 'output')
    write_json(STATE / 'runtime.json', runtime)
    # A general agent run has no DAPPER workspace; an account job and every other kind create one above.
    for target in [project, BASE / 'input', *([BASE / 'workspace'] if (BASE / 'workspace').exists() else [])]:
        protect(target)
    if (BASE / 'trusted').exists():
        protect(BASE / 'trusted')
    verify_writable_output(work)
    return work, runtime, prompt


def read_frozen_evidence(**arguments):
    """Read existing seed bytes; later retained captures use the authorized proxy."""
    runtime = json.loads((STATE / 'runtime.json').read_bytes())
    path = Path(runtime['evidence_package'])
    raw = path.read_bytes(); checksum = hashlib.sha256(raw).hexdigest()
    frozen = json.loads(raw)
    identity = arguments.get('artifact_id')
    if identity == 'package:' + checksum:
        descriptor = {'artifact_id': identity, 'sha256': checksum, 'size_bytes': len(raw)}
    elif identity in frozen['source_artifacts']:
        descriptor = {**frozen['source_artifacts'][identity], 'artifact_id': identity}
        from .evidence_reader import safe_read
        raw = safe_read(path.parent, descriptor['path'])
    elif RESEARCH:
        result = RESEARCH.call('read_evidence', arguments)
        if result.get('isError'):
            raise ValueError('Retained artifact is unavailable for this research scope')
        return result.get('structuredContent', result)
    else:
        raise ValueError('Artifact absent from the frozen source inventory')
    return read_artifact(raw, descriptor, **arguments)


def terminate(process):
    if process.poll() is None:
        def send_signal(name):
            # Box root has SETUID but may lack CAP_KILL for a different uid.
            user = pwd.getpwnam('reveal-agent')
            command = ['setpriv', '--reuid', str(user.pw_uid), '--regid', str(user.pw_gid),
                       '--clear-groups', '--no-new-privs', '/bin/kill', '-' + name, '--', '-' + str(process.pid)]
            subprocess.run(command, capture_output=True, check=False, timeout=5)
        send_signal('TERM')
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            send_signal('KILL')
            process.wait(timeout=5)


def lint_tool(filename, ledger):
    deadline = time.monotonic() + 55
    from .box_research import AuthoringTiming
    with AuthoringTiming(STATE / 'ledger/authoring-timing.json', 'lint_account', deadline, clock=time.monotonic) as timing:
        from .scientific_account_lint import lint_scientific_account
        from .authoring_structure import preflight_document, diagnostic_response
        from .evidence_package import decode
        path = OUTPUT / filename
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
            return {'isError': True, 'content': [{'type': 'text', 'text': 'Account output missing, symlinked or oversized'}]}
        frozen = STATE / ('lint-' + filename)
        frozen.write_bytes(path.read_bytes())
        runtime = json.loads((STATE / 'runtime.json').read_text())
        deadline = min(deadline, runtime.get('execution_deadline_monotonic', deadline))
        timing.deadline = deadline
        timing.require_remaining(1)
        timing.phase('structure_preflight')
        try:
            document = decode(frozen.read_bytes(), 'yaml' if path.suffix in ('.yaml', '.yml') else 'json')
        except ValueError:
            raise DraftValidationError('Account is not valid JSON/YAML; repair its document syntax') from None
        timing.require_remaining(0.1)
        structure = preflight_document(document, dapper_root=runtime['dapper_root'],
            release_lock=BASE / 'bundle/services/backend/agent-runtime/dapper-release.json',
            timeout=min(12, deadline - time.monotonic()))
        if not structure['valid']:
            timing.phase('diagnostic_report')
            return timing.feedback(diagnostic_response(structure, output=OUTPUT, filename=filename,
                capture_roots=(OUTPUT, STATE / 'ledger', STATE / 'runtime.json')), runtime.get('execution_deadline_monotonic'))
        # The final manifest is written only when execution ends. Snapshot completed
        # captures under the ledger lock so draft lint sees the same trusted bytes
        # without freezing or interrupting the agent's remaining tool calls.
        ledger_path = ledger.root / 'lint-sources.json'
        with ledger.lock:
            write_json(ledger_path, json.loads(ledger.sanitized_bytes({'calls': ledger.entries})[0]))
        evidence_path = RESEARCH.materialize(deadline=deadline, progress=timing.phase) if RESEARCH else runtime['evidence_package']
        timing.phase('full_lint')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DraftValidationError('Draft lint deadline reached during evidence materialization; retry this call to resume verified progress')
        report = lint_scientific_account(frozen, dapper_root=runtime['dapper_root'],
                                         release_lock=BASE / 'bundle/services/backend/agent-runtime/dapper-release.json',
                                         evidence_package=evidence_path, ledger_path=ledger_path, mode='draft', timeout=remaining)
        checks = {finding.get('check') for finding in report.get('findings', []) if finding.get('severity') == 'error'}
        if checks & {'source-ancestry', 'claim-evidence'}:
            report['repair_guidance'] = 'Each component Claim needs explicit, target-matched EvidenceItems and unchanged eligible scientific source Files with exact locators. Use captured reference data, authorized prior science or eligible independent evidence. Seek a relevant CFDE connection when supported and explain its absence when not; never attach an unrelated row to satisfy guidance. If no useful supported interpretation exists, save an insufficient-evidence outcome identifying the missing observation.'
        if runtime.get('deliverable') == 'scientific_account':
            # The pinned linter checked each snippet against the read it cites. The deposit also needs the quote's
            # exact place in the paper, so that is checked here too, while the agent can still fix it.
            with ledger.lock:
                calls = json.loads(ledger.sanitized_bytes({'calls': ledger.entries})[0])['calls']
            located = quote_findings(document, ledger.root, calls)
            if located:
                report['findings'] = [*report.get('findings', []), *located]
                report['valid'] = False
                if isinstance(report.get('counts'), dict):
                    report['counts'] = {**report['counts'], 'errors': report['counts'].get('errors', 0) + len(located)}
        timing.phase('diagnostic_report')
        return timing.feedback(diagnostic_response(report, output=OUTPUT, filename=filename,
            capture_roots=(OUTPUT, STATE / 'ledger', STATE / 'runtime.json')), runtime.get('execution_deadline_monotonic'))


ACCOUNT_SKILL = 'services/backend/agent-skills/construct-literature-account/SKILL.md'
ACCOUNT_SKILL_PATH = 'account/SKILL.md'
ACCOUNT_REQUEST_FILENAME = 'account-request.json'


def account_workspace(work, project, lock, agent_input, runtime):
    """What a scientific_account job needs beyond a general agent job, the way a research run has it: the pinned
    DAPPER release the draft and lint tools check against, the account's evidence package (the requester's question
    and claims as trusted nodes, no source artifacts), and the authoring skill and schema for the agent to read.
    The package comes from the frozen input, so the run lints against exactly what was frozen when the job was queued."""
    from .authoring_contract import pinned_schema
    from .dapper_release import clone_release
    root = BASE / 'workspace'
    root.mkdir()
    release = clone_release(root / 'dapper', lock)
    account = agent_input['account']
    package = root / 'evidence-package.json'
    package.write_bytes(canonical(account['package']))
    folder = work / 'account'
    folder.mkdir()
    shutil.copyfile(project / ACCOUNT_SKILL, work / ACCOUNT_SKILL_PATH)
    (folder / 'authoring-schema-excerpt.yaml').write_bytes(pinned_schema(project))
    request_view = {'question': account['question'], 'question_id': account['package']['selection']['knowledge_gap_id'],
                    'claims': account['claims'], 'limits': runtime['agent']['limits'],
                    'deliverable': 'One DAPPER ScientificAccount saved as output/account-1.json. Follow ' + ACCOUNT_SKILL_PATH + '.'}
    (work / ACCOUNT_REQUEST_FILENAME).write_text(json.dumps(request_view, ensure_ascii=False, indent=2) + '\n')
    runtime.update({'dapper': release, 'dapper_root': str(root / 'dapper'), 'evidence_package': str(package),
                    'deliverable': 'scientific_account'})
    runtime['derived_input_views'] = {str(path.relative_to(work)): hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in (work / ACCOUNT_SKILL_PATH, folder / 'authoring-schema-excerpt.yaml', work / ACCOUNT_REQUEST_FILENAME)}


# Lint feedback drops a finding's message unless the finding names a `rule` (authoring_structure._normalized_finding):
# ordinary lint messages may quote private text. A quote finding's message is the repair instruction and never repeats
# the snippet or the paper (literature_locator.LocatorError), so it carries a rule and reaches the agent as written.
QUOTE_CHECK = 'literature-quote'


def quote_findings(document, ledger_root, calls):
    """Lint findings for snippets that cannot be located in the paper they quote. The backend puts every literature
    EvidenceItem into its deposit form after the run (account_deposit), which needs the quote's exact place in the
    paper; the same check here lets the agent repair a quote while it is still drafting."""
    from . import literature_locator as locator
    try:
        papers = locator.paper_texts(ledger_root, calls)
    except locator.LocatorError as exc:
        return [{'severity': 'error', 'check': QUOTE_CHECK, 'rule': QUOTE_CHECK, 'where': 'ledger', 'message': str(exc), 'why': ''}]
    _, problems = locator.document_quotes(document, papers, locator.captures(calls))
    return [{'severity': 'error', 'check': QUOTE_CHECK, 'rule': QUOTE_CHECK, 'where': where, 'message': message, 'why': ''}
            for where, message in problems]


def agent_tools(selected_graphs, ledger, settings, literature_client=LiteratureClient, lint=None, write_draft=None, claims_client=None):
    """The scoped REVEAL tools of a general agent job, bounded by the job's own frozen limits. A spent allowance
    is a tool error the agent can finish after, never a failed job. A scientific_account job also gets the draft and
    lint tools a research run authors with."""
    limits = settings['limits']
    literature = literature_client(max_results=limits['literature_search_results'], guidance=True) if settings['literature'] else None
    claims = None
    if settings.get('claims_url'):
        from .box_claims import ClaimsClient
        claims = (claims_client or ClaimsClient)(settings['claims_url'])
    return ScopedTools(selected_graphs, ledger, max_calls=limits['tool_calls'], soft_budget=True, literature=literature, claims=claims,
                       draft_files=('account-1.json',),
                       lint=lint, write_draft=write_draft,
                       max_searches=limits['literature_searches'], max_papers=limits['literature_papers'],
                       max_characters=limits['literature_characters'])


def agent_system_prompt(agent_input):
    """Harness facts the agent needs, then the caller's system prompt; appended to Claude Code's own."""
    note = ('You are running unattended in an isolated, disposable sandbox. Nobody can answer questions mid-run, so '
            'make reasonable assumptions and finish the task. Your working directory is read-only. Write any files '
            'you want returned to the output/ directory (/reveal/output): regular files only, no symlinks, each under '
            '8 MB and 40 MB in all. Use /tmp or your home directory for scratch work. Everything else is discarded '
            'when the run ends. Your final message is returned to the requester as the result, so make it complete '
            'on its own.')
    if agent_input['network'] == 'restricted':
        reachable = (['the selected knowledge-graph and literature services'] if agent_input['graphs'] or agent_input['literature'] else []) \
            + (['these hosts over HTTPS: ' + ', '.join(agent_input['allowed_domains'])] if agent_input['allowed_domains'] else [])
        note += (' The network is restricted to the Anthropic API' + ''.join(' and ' + item for item in reachable) +
                 '; other hosts, package installs and arbitrary downloads will fail.' +
                 (' WebSearch still works.' if 'WebSearch' in agent_input['tools'] else ''))
    if agent_input.get('deliverable') == 'scientific_account':
        note += (' This run\'s deliverable is one DAPPER ScientificAccount, saved as output/account-1.json with '
                 'mcp__reveal__write_account_draft and checked with mcp__reveal__lint_account. Read and follow '
                 f'{ACCOUNT_SKILL_PATH} before anything else; the question and the requester\'s claims are in '
                 f'{ACCOUNT_REQUEST_FILENAME}. A run that ends without a saved account returns nothing. Your final message is a '
                 'short report to the requester: what the account concludes, which of their claims you assessed, and what you did not read.')
    return note + ('\n\n' + agent_input['system_prompt'] if agent_input['system_prompt'] else '')


def deadline_reason(request, last_activity=None):
    last_activity = last_activity or {}
    stage = {'research': 'during research', 'agent': 'while working on the prompt'}.get(request['kind'], 'while writing the research statement')
    last_tool = last_activity.get('tool_name')
    operation = last_activity.get('operation') or {}
    if (operation.get('state') in ('received', 'running') or
            last_tool and tool_kind(last_tool) == 'get_operation' and last_activity.get('kind') == 'tool_call'):
        stage = 'while waiting for a durable evidence operation'
    elif last_activity.get('kind') == 'tool_call' and last_tool and tool_kind(last_tool) == 'lint_account':
        stage = 'while checking the draft account and sources'
    elif last_activity.get('authoring_phase') == 'draft_repair':
        stage = 'while repairing the draft after an unsuccessful authoring check'
    suffix = '; last observed tool: ' + last_tool if last_tool else ''
    return f"Agent reached its {request['timeout_seconds']}-second execution limit {stage}{suffix}. No output was accepted."


def observable_activity(kind, payload, call, previous=None):
    """Record actual public tool progress for timeout diagnostics, never reasoning."""
    value = {'kind': kind, 'tool_name': payload.get('tool_name')}
    phase = (previous or {}).get('authoring_phase')
    if phase in ('draft_repair', 'draft_checked'):
        value['authoring_phase'] = phase
    name = tool_kind(value['tool_name'] or '')
    result = call.get('result')
    if kind == 'tool_result' and name in ('lint_account', 'write_account_draft'):
        failed = payload.get('status') == 'error' or tool_failed(name, result)
        if failed:
            value['authoring_phase'] = 'draft_repair'
        elif name == 'lint_account':
            value['authoring_phase'] = 'draft_checked'
    if kind == 'tool_result' and tool_kind(value['tool_name'] or '') in DURABLE_TOOLS:
        operation = durable_operation(call.get('result'), SECRETS)
        if operation:
            value['operation'] = operation
    return value


def provider_failure_reason(request, result):
    if request['kind'] == 'agent':
        if result.get('subtype') == 'error_max_turns':
            return f"The agent reached its {request['max_turns']}-turn limit before finishing."
        if result.get('subtype') == 'error_max_budget_usd':
            return f"The agent reached its ${request['max_budget_usd']:g} budget before finishing."
        return 'Claude execution failed: ' + str(result.get('subtype', 'nonzero exit'))
    if result.get('subtype') == 'error_max_turns':
        return (f"The agent reached its {request['max_turns']}-turn execution limit before completing the result. "
                'No scientific result was accepted. Retry the analysis to start a new attempt with the current execution limits.')
    if result.get('subtype') == 'error_max_budget_usd':
        return f"The agent reached its ${request['max_budget_usd']:g} execution budget. No scientific result was accepted."
    return 'Claude execution failed: ' + str(result.get('subtype', 'nonzero exit'))


def runtime_completion(request, started, status, reason, process=None, parser=None, last_activity=None, timing=None):
    """Safe terminal metrics, including deadline exits without provider usage."""
    result = parser.result if parser else None
    reported = provider_metrics(result)
    summary = {'completed_at': stamp(), 'elapsed_seconds': round(time.monotonic() - started, 3),
               'time_limit_seconds': request['timeout_seconds'],
               'max_budget_usd': request.get('max_budget_usd'),
               'turn_limit': request['max_turns'], 'turns_used': reported['num_turns'],
               'provider_result_subtype': reported['subtype'],
               'status': status, 'reason': reason, 'process_returncode': process.returncode if process else None,
               'provider_terminal_received': result is not None,
               'usage_status': 'reported' if reported['cost_usd'] is not None else 'unavailable',
               'cost_usd': reported['cost_usd'],
               'provider_reported': reported,
               'last_observable_activity': last_activity}
    timing = timing if timing is not None else getattr(parser, 'timing', None)
    if timing is not None:
        summary['timing'] = timing.snapshot()
    from .box_research import authoring_timing_snapshot
    authoring = authoring_timing_snapshot(STATE / 'ledger/authoring-timing.json', terminal=True, clock=time.monotonic)
    if authoring is not None: summary['authoring_timing'] = authoring
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        summary['child_cpu_seconds'] = round(usage.ru_utime + usage.ru_stime, 3)
        summary['child_peak_rss_bytes'] = int(usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024))
    except (ImportError, OSError):
        pass
    return summary


def write_draft_tool(filename, document):
    """Representation assistance only: source hydration never implies acceptance."""
    deadline = time.monotonic() + 55
    from .box_research import AuthoringTiming
    with AuthoringTiming(STATE / 'ledger/authoring-timing.json', 'write_account_draft', deadline, clock=time.monotonic) as timing:
        if not isinstance(document, dict) or len(canonical(document)) > 4_000_000:
            raise DraftValidationError('Invalid or oversized draft document')
        if not isinstance(document.get('scientific_accounts'), list) or len(document['scientific_accounts']) != 1:
            raise DraftValidationError('Expected document={"scientific_accounts":[one account],"claims":[...],"propositions":[...],"evidence_items":[...]}; group values must be arrays, not a class instance or graph/nodes envelope')
        runtime = json.loads((STATE / 'runtime.json').read_text())
        deadline = min(deadline, runtime.get('execution_deadline_monotonic', deadline))
        timing.deadline = deadline
        timing.require_remaining(1)
        # Server-declared seed prefixes (HGNC.SYMBOL, biolink) resolve authored triples without an authored prefix map.
        seed = Path(runtime.get('evidence_package') or '')
        if seed.is_file(): declare_trusted_prefixes(document, trusted_prefixes(json.loads(seed.read_bytes())))
        timing.phase('structure_preflight')
        from .authoring_structure import preflight_document, diagnostic_response
        timing.require_remaining(0.1)
        structure = preflight_document(document, dapper_root=runtime['dapper_root'],
            release_lock=BASE / 'bundle/services/backend/agent-runtime/dapper-release.json',
            timeout=min(12, deadline - time.monotonic()))
        if not structure['valid']:
            timing.phase('diagnostic_report')
            return timing.feedback(diagnostic_response(structure, output=OUTPUT, filename=filename,
                capture_roots=(OUTPUT, STATE / 'ledger', STATE / 'runtime.json')), runtime.get('execution_deadline_monotonic'))
        evidence_path = RESEARCH.materialize(deadline=deadline, progress=timing.phase) if RESEARCH else Path(runtime['evidence_package'])
        timing.phase('draft_hydration')
        package = json.loads(Path(evidence_path).read_text())
        trusted = {n['id']: (group, n) for group, rows in package['dapper_context'].items() if isinstance(rows, list) for n in rows if isinstance(n, dict) and 'id' in n}
        for rows in document.values():
            if isinstance(rows, list):
                for node in rows:
                    if isinstance(node, dict) and node.get('id') in trusted and node != trusted[node['id']][1]:
                        raise PolicyError('A trusted source object was changed')
        def exact_references(value):
            if isinstance(value, dict):
                for child in value.values():
                    yield from exact_references(child)
            elif isinstance(value, list):
                for child in value:
                    yield from exact_references(child)
            elif isinstance(value, str):
                yield value
        for _ in range(len(trusted) + 1):
            if time.monotonic() >= deadline:
                raise DraftValidationError('Draft preparation deadline reached; retry this call to resume verified evidence materialization')
            present = {n['id'] for rows in document.values() if isinstance(rows, list) for n in rows if isinstance(n, dict) and 'id' in n}
            references = set(exact_references(document))
            missing = [identity for identity in trusted if identity not in present and identity in references]
            if not missing:
                break
            for identity in missing:
                group, node = trusted[identity]
                document.setdefault(group, []).append(node)
        declare_trusted_prefixes(document, trusted_prefixes(package))
        context = runtime.get('draft_attribution')
        if context:
            for group in ('persons', 'organizations', 'activities'):
                document[group] = [node for node in document.get(group, []) if node.get('id') in trusted]
                document[group].extend(context.get(group, []))
            for group in ('claims', 'scientific_accounts'):
                for node in document.get(group, []):
                    if node.get('id') not in trusted:
                        node['was_generated_by'] = context['activities'][0]['id']
                        node['was_attributed_to'] = [context['organizations'][0]['id']]
        path = OUTPUT / filename
        if time.monotonic() >= deadline:
            raise DraftValidationError('Draft preparation deadline reached; retry this call to resume verified evidence materialization')
        if path.is_symlink():
            raise PolicyError('Draft output symlink forbidden')
        timing.phase('draft_write')
        # One field per line, so the agent can Read the saved draft and Edit reported fields in place
        # instead of regenerating the whole document for every repair.
        raw = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=1) + '\n').encode()
        timing.check()
        path.write_bytes(raw)
        user = pwd.getpwnam('reveal-agent')
        os.chown(path, user.pw_uid, user.pw_gid)
        return timing.feedback({'content': [{'type': 'text', 'text': 'Draft saved to ' + str(path) + '. Run lint_account; this file has not been accepted or minted. To repair findings, Edit only the reported fields in this file and run lint_account again.'}]}, runtime.get('execution_deadline_monotonic'))


def main():
    global SECRETS, RESEARCH
    if os.getuid() != 0:
        raise RuntimeError('The trusted Box runner must be root')
    STATE.mkdir(exist_ok=True)
    # flock fences duplicated launch requests after an uncertain network response.
    import fcntl
    lockfile = (STATE / 'runner.lock').open('w')
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lockfile.close()
        return 0
    if (STATE / 'status.json').exists():
        lockfile.close()
        return 0
    request = json.loads((BASE / 'request.json').read_text())
    ledger = Ledger(STATE / 'ledger', request['job_id'], request['attempt'])
    process = server = parser = runtime = proxy = None
    agent = request['kind'] == 'agent'
    last_activity = None
    builtin_calls = {}
    status, reason = 'failed', None
    started = time.monotonic()
    timing = RuntimeTiming(started=started, clock=time.monotonic)
    last_checkpoint = started
    def checkpoint(force=False):
        nonlocal last_checkpoint
        observed = time.monotonic()
        if runtime is not None and (force or observed - last_checkpoint >= 5):
            runtime['timing_checkpoint'] = {'recorded_at': stamp(), 'timing': timing.snapshot(),
                                          'last_observable_activity': last_activity}
            from .box_research import authoring_timing_snapshot
            authoring = authoring_timing_snapshot(STATE / 'ledger/authoring-timing.json', clock=time.monotonic)
            if authoring is not None: runtime['timing_checkpoint']['authoring_timing'] = authoring
            write_json(STATE / 'runtime.json', runtime)
            os.chmod(STATE / 'runtime.json', 0o600)
            last_checkpoint = observed
    write_json(STATE / 'status.json', {'status': 'preparing', 'started_at': stamp(), 'pid': os.getpid()})
    try:
        emit('stage', {'stage': 'starting_agent', 'state': 'started', 'source': 'harness',
                       'message': 'Preparing the agent sandbox.' if agent else 'Verifying the research runtime and preparing evidence files.'})
        credentials = json.loads((BASE / 'credentials.json').read_text())
        (BASE / 'credentials.json').unlink()
        SECRETS = tuple(value for value in credentials.values() if isinstance(value, str) and value)
        ledger.secrets = SECRETS
        work, runtime, prompt = setup(request)
        runtime['execution_deadline_monotonic'] = started + request['timeout_seconds']
        if runtime.get('research_context'):
            RESEARCH = HostedResearchClient(runtime['research_context'], credentials.get('REVEAL_RESEARCH_TOKEN'),
                STATE / 'ledger/research-context', seed_path=runtime['evidence_package'],
                execution_id=request['job_id'] + ':' + str(request['attempt']))
            runtime['research_receipts_path'] = 'ledger/research-receipts.json'
        actual_version = subprocess.run(['/reveal/claude/node_modules/.bin/claude', '--version'], capture_output=True, text=True, check=True, timeout=15).stdout.strip()
        if actual_version.split()[0] != request['claude_version']:
            raise ValueError('Installed Claude Code version differs from the pinned harness')
        runtime['observed_harness_version'] = actual_version
        user = pwd.getpwnam('reveal-agent')
        OUTPUT.mkdir(exist_ok=True)
        os.chown(OUTPUT, user.pw_uid, user.pw_gid)
        os.chmod(OUTPUT, 0o700)
        literature = request['kind'] == 'research' or agent and runtime['agent']['literature']
        if agent:
            accounting = runtime.get('deliverable') == 'scientific_account'
            tools = agent_tools(request['selected_graphs'], ledger, runtime['agent'],
                                lint=(lambda filename: lint_tool(filename, ledger)) if accounting else None,
                                write_draft=write_draft_tool if accounting else None)
        else:
            tools = ScopedTools(request['selected_graphs'], ledger, lint=(lambda filename: lint_tool(filename, ledger)) if request['kind'] == 'research' else None,
                                write_draft=write_draft_tool if request['kind'] == 'research' else None,
                                literature=LiteratureClient() if literature else None,
                                write_outcome=write_outcome_tool if request['kind'] == 'research' else None, research=RESEARCH,
                                read_evidence=read_frozen_evidence if request['kind'] == 'research' else None)
        server = serve(tools)
        config = {'mcpServers': {'reveal': {'type': 'http', 'url': 'http://127.0.0.1:8765/mcp'}}}
        config_path = BASE / 'mcp.json'
        config_path.write_bytes(canonical(config)); os.chmod(config_path, 0o444)
        if agent:
            # The agent may run shell commands and reach the open network, so it never holds the API key.
            proxy = KeyProxy(credentials['ANTHROPIC_API_KEY']).start()
            env = agent_environment(user.pw_dir, '/reveal/venv/bin:/usr/local/bin:/usr/bin:/bin', proxy)
        else:
            env = {'PATH': '/reveal/venv/bin:/usr/local/bin:/usr/bin:/bin', 'HOME': user.pw_dir,
                   'ANTHROPIC_API_KEY': credentials['ANTHROPIC_API_KEY'],
                   'PYTHONDONTWRITEBYTECODE': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
                   'DISABLE_AUTOUPDATER': '1', 'REVEAL_DAPPER_ROOT': runtime['dapper_root']}
        command = ['setpriv', '--reuid', str(user.pw_uid), '--regid', str(user.pw_gid), '--clear-groups', '--no-new-privs',
                   '/usr/bin/timeout', '--signal=TERM', '--kill-after=10s', str(max(1, int(request['timeout_seconds'] - (time.monotonic() - started)))),
                   '/reveal/claude/node_modules/.bin/claude', '-p', '--verbose', '--output-format', 'stream-json',
                   '--include-partial-messages', '--model', request['model'], '--max-turns', str(request['max_turns']),
                   # Low effort: about 60% of a run's output was reasoning, and slow runs hit the time limit.
                   '--effort', runtime['agent']['effort'] if agent else 'low',
                   '--max-budget-usd', str(request['max_budget_usd']), '--permission-mode', 'bypassPermissions',
                   '--tools', ','.join(runtime['agent']['tools']) if agent else 'Read,Glob,Grep,Write,Edit',
                   '--strict-mcp-config', '--mcp-config', str(config_path),
                   '--setting-sources', '', '--disable-slash-commands']
        if agent:
            command += ['--append-system-prompt', agent_system_prompt(json.loads((work / AGENT_INPUT_FILENAME).read_bytes()))]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=work, env=env, start_new_session=True)
        process.stdin.write(prompt.encode()); process.stdin.close()
        write_json(STATE / 'status.json', {'status': 'running', 'started_at': stamp(), 'pid': os.getpid(), 'agent_pid': process.pid})
        emit('agent_started', {'message': 'Claude is working on the prompt.' if agent else 'Claude is reading the frozen evidence and authoring a result.',
                               'model': request['model'],
                               'stage': {'research': 'authoring_account', 'agent': 'running_agent'}.get(request['kind'], 'authoring_paragraph')})
        parser = ClaudeStream(secrets=SECRETS, timing=timing)
        checkpoint(force=True)
        trace_filter, error_filter = SecretFilter(SECRETS), SecretFilter(SECRETS)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        # Private raw trace is never an SSE payload. Restrict access to the trusted runner.
        with (STATE / 'claude-stream.jsonl').open('wb') as trace, (STATE / 'claude-stderr.txt').open('wb') as errors:
            os.chmod(trace.name, 0o600); os.chmod(errors.name, 0o600)
            total = 0
            while selector.get_map():
                checkpoint()
                if (STATE / 'cancel').exists():
                    status, reason = 'cancelled', 'Cancelled by the owning job'
                    terminate(process); break
                if time.monotonic() - started > request['timeout_seconds']:
                    reason = deadline_reason(request, last_activity); terminate(process); break
                for key, _ in selector.select(timeout=0.25):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    total += len(chunk)
                    if total > 40_000_000:
                        raise StreamProtocolError('Agent trace exceeded capture budget')
                    if key.data == 'stdout':
                        trace.write(trace_filter.feed(chunk)); trace.flush()
                        for kind, payload in parser.feed(chunk):
                            if kind in ('tool_call', 'tool_result'):
                                last_activity = observable_activity(kind, payload, parser.tools[payload['call_id']], last_activity)
                            if kind == 'tool_call':
                                call = parser.tools[payload['call_id']]
                                entry = ledger.start(call['name'], call['input'], None)
                                entry['claude_call_id'] = payload['call_id']
                                builtin_calls[payload['call_id']] = entry
                            elif kind == 'tool_result':
                                call_id = payload['call_id']
                                result = parser.tools[call_id]['result']
                                ledger.finish(builtin_calls[call_id], result, 'failed' if tool_failed(parser.tools[call_id]['name'], result) else 'completed')
                                payload['artifact_sha256'] = builtin_calls[call_id]['response']['sha256']
                            emit(kind, payload)
                    else:
                        timing.bytes_received('stderr', len(chunk))
                        errors.write(error_filter.feed(chunk)); errors.flush()
            trace.write(trace_filter.feed(b'', final=True)); trace.flush()
            errors.write(error_filter.feed(b'', final=True)); errors.flush()
            if process.poll() is None:
                process.wait(timeout=10)
        if not reason:
            if process.returncode == 124 or (process.returncode == 137 and time.monotonic() - started >= request['timeout_seconds'] - 1):
                reason = deadline_reason(request, last_activity)
            elif process.returncode == 137:
                reason = 'Agent process was killed before its execution deadline (exit 137). No output was accepted.'
        if not reason:
            for kind, payload in parser.finish():
                emit(kind, payload)
            if process.returncode != 0 or parser.result.get('is_error'):
                reason = provider_failure_reason(request, parser.result)
            else:
                status = 'succeeded'
                # A general agent's output/ is its own; only research writes a structured outcome there.
                outcome_path = None if agent else OUTPUT / 'outcome.json'
                if outcome_path and outcome_path.exists():
                    outcome = json.loads(outcome_path.read_text())
                    if outcome.get('status') in ('insufficient_evidence', 'failed'):
                        status, reason = outcome['status'], str(outcome.get('reason', ''))[:2000]
                # An account job's deliverable is its saved account; a general agent job's is its answer.
                # An account job returns one file, the one its gate reads (agent_account.accept).
                expected = ([OUTPUT / 'account-1.json'] if (OUTPUT / 'account-1.json').is_file() else []) if runtime.get('deliverable') == 'scientific_account' else (
                            list(OUTPUT.glob('account-*.*')) if request['kind'] == 'research' else
                            [True] if agent else list(OUTPUT.glob('paragraph.json')))
                reused = outcome.get('existing_account_ids', []) if outcome_path and outcome_path.exists() else []
                reuse_output = RESEARCH is not None and bool(reused) and isinstance(reused, list) and all(isinstance(item, str) for item in reused) and set(reused) <= RESEARCH.existing_account_ids
                if status == 'succeeded' and not expected and not reuse_output:
                    status, reason = 'failed', 'Claude completed without the required output documents'
    except Exception as exc:
        # Values from provider exceptions can contain credentials. Keep diagnostics typed.
        status = 'failed'
        reason = type(exc).__name__ + ': trusted agent execution failed'
        emit('warning', {'code': 'agent_runtime_error', 'message': reason})
        (STATE / 'failure-type.txt').write_text(type(exc).__name__)
    finally:
        if process:
            terminate(process)
        if server:
            server.shutdown()
        if proxy:
            proxy.shutdown()
        if agent and parser is not None and parser.result is not None:
            # Root-owned, beside the tool ledger: the agent cannot write or replace its own final answer.
            write_json(STATE / 'ledger' / AGENT_RESPONSE_FILENAME,
                       response_record(request['job_id'], request['attempt'], status, parser.result, SECRETS))
        if RESEARCH is not None:
            RESEARCH.freeze()
        ledger.freeze()
        if runtime is not None:
            completion = runtime_completion(request, started, status, reason, process, parser, last_activity, timing)
            runtime['completed_at'], runtime['cost_usd'] = completion['completed_at'], completion['cost_usd']
            runtime['completion'] = completion
            runtime['timing_checkpoint'] = {'recorded_at': completion['completed_at'], 'timing': completion['timing'],
                                          'last_observable_activity': last_activity}
            runtime['observed_claude_runtime'] = parser.runtime if parser else {}
            write_json(STATE / 'runtime.json', runtime)
            os.chmod(STATE / 'runtime.json', 0o600)
        write_json(STATE / 'status.json', {'status': status, 'reason': reason, 'completed_at': stamp()})
        lockfile.close()
    return 0


def poll(cursor=0):
    status_path = STATE / 'status.json'
    status = json.loads(status_path.read_text()) if status_path.exists() else {'status': 'not_started'}
    if status.get('status') in ('preparing', 'running') and status.get('pid'):
        try:
            os.kill(status['pid'], 0)
            alive = True
        except ProcessLookupError:
            alive = False
        if not alive:
            # Recover durable tool starts/results conservatively; never invent a missing result.
            request = json.loads((BASE / 'request.json').read_text())
            ledger = Ledger(STATE / 'ledger', request['job_id'], request['attempt'])
            events = STATE / 'ledger/events.jsonl'
            latest = {}
            for line in events.read_text().splitlines() if events.exists() else []:
                event = json.loads(line)
                latest[event['sequence']] = {key: value for key, value in event.items() if key != 'phase'}
            ledger.entries = [latest[key] for key in sorted(latest)]
            ledger.freeze()
            status = {'status': 'cancelled' if (STATE / 'cancel').exists() else 'failed',
                      'reason': 'Trusted runner stopped before terminal state; unfinished calls marked interrupted', 'completed_at': stamp()}
            write_json(status_path, status)
    path = STATE / 'events.jsonl'
    lines = path.read_text().splitlines() if path.exists() else []
    # Caller checkpoints cursor only after durable event writes. Replay is at least once.
    items = [json.loads(line) for line in lines[cursor:cursor + 100]]
    return {'state': status, 'events': items, 'cursor': cursor + len(items), 'has_more': cursor + len(items) < len(lines)}


def collect():
    files, size = {}, 0
    for root, prefix in [(OUTPUT, 'output'), (STATE / 'ledger', 'ledger')]:
        for path in root.rglob('*'):
            if path.is_symlink():
                raise ValueError('Output symlinks are forbidden')
            if not path.is_file():
                continue
            data = path.read_bytes(); size += len(data)
            if len(data) > capture_file_limit(prefix + '/' + str(path.relative_to(root))) or size > MAX_TOTAL:
                raise ValueError('Output artifact budget exceeded')
            files[prefix + '/' + str(path.relative_to(root))] = base64.b64encode(data).decode()
    if (STATE / 'runtime.json').exists() or (STATE / 'runtime.json').is_symlink():
        source, _ = source_file(STATE, 'runtime.json', runtime_metadata=True)
        with source: data = source.read(capture_file_limit('runtime.json') + 1)
        if len(data) > capture_file_limit('runtime.json') or size + len(data) > MAX_TOTAL:
            raise ValueError('Output artifact budget exceeded')
        files['runtime.json'] = base64.b64encode(data).decode()
    return {'files': files}


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else 'run'
    if command == 'run':
        raise SystemExit(main())
    elif command == 'poll':
        print(json.dumps(poll(int(sys.argv[2]))))
    elif command == 'collect':
        print(json.dumps(collect()))
    elif command == 'cancel':
        STATE.mkdir(exist_ok=True); (STATE / 'cancel').touch()
        path = STATE / 'status.json'
        status = json.loads(path.read_text()) if path.exists() else {}
        if status.get('status') == 'running' and type(status.get('agent_pid')) is int:
            user = pwd.getpwnam('reveal-agent')
            subprocess.run(['setpriv', '--reuid', str(user.pw_uid), '--regid', str(user.pw_gid), '--clear-groups',
                            '--no-new-privs', '/bin/kill', '-TERM', '--', '-' + str(status['agent_pid'])], capture_output=True, timeout=5)
