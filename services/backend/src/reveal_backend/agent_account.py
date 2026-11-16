"""The account block of a scientific_account agent job.

A general agent job that returns a DAPPER ScientificAccount authors it with the tools a research run uses
(write_account_draft, lint_account) and is checked by the same linter. That linter works against a frozen evidence
package: the question the account answers, as a trusted KnowledgeGap, and the sources the account may cite. A
research run's package is built from curated data. An agent job has none of that: the requester supplies the
question, and every source is a paper the agent reads through the captured literature tools during the run.

`build` makes the smallest package the linter accepts for such a job: the requester's question as a KnowledgeGap,
the requester's claims as Propositions, and no source artifacts. Both are trusted nodes: the agent can build on
them and cannot change them.

Policy, in one place: REVEAL does not invent questions for curated (DisMech) research. Here the question is the
requester's own, and GAP_DESCRIPTION says so on the node, so nobody reading the account can take it for a curated
knowledge gap.

After the run, `accept` is the gate: the account the agent wrote is assembled and linted exactly as a research
account is (acceptance.assemble_account), then put in its deposit form (account_deposit). `store` keeps the result
with the job, and `public` is what the job result says about it.
"""
from pathlib import Path

from .box_agent import MAX_ACCOUNT_CLAIMS, TRIPLE_SLOTS, AgentInputError

PACKAGE_VERSION = 'reveal.evidence-package/0.2-draft'
# CURIE prefixes a triple in such an account may use beyond the pinned schema's own (MONDO, HGNC, KPN.TRAIT, obo, ...):
# the bindings a research draft gets (research_seed.CLAIM_PREFIXES). They travel in the package, so the draft writer
# declares the ones a draft uses and the linter resolves them.
PREFIXES = {'HGNC.SYMBOL': 'https://identifiers.org/hgnc.symbol:', 'biolink': 'https://w3id.org/biolink/vocab/'}
GAP_DESCRIPTION = ('Question supplied by the requester of a REVEAL literature account. It is the question the account '
                   'addresses, not a knowledge gap curated from a source catalogue.')


def build(question, claims, dapper=None):
    """{'question', 'claims': [{'statement', 'proposition_id', and the triple slots when given}], 'package'} for
    box_agent.freeze_input. `claims` are box_agent.claim_entry values.

    Ids are DAPPER-ID-1 addresses minted with the pinned snapshot, so the same question and claims give the same
    nodes in every job."""
    if dapper is None:
        from .acceptance import public_runtime
        dapper = public_runtime()
    assert len(claims) <= MAX_ACCOUNT_CLAIMS
    gap = {'text': question, 'gap_description': GAP_DESCRIPTION}
    gap = {'id': dapper.compute_id(gap, 'KnowledgeGap', dapper.schema), **gap}
    resolver = dapper.resolver_class(dapper.schema, {'prefixes': PREFIXES})
    propositions = []
    for claim in claims:
        # A claim is {'statement'} or that plus the three triple slots (box_agent.claim_entry). A term the pinned
        # release cannot resolve would fail the agent's lint on a node it cannot edit, so it is refused here.
        node = {'statement': claim['statement'], **{slot: claim[slot] for slot in TRIPLE_SLOTS if slot in claim}}
        for slot in TRIPLE_SLOTS:
            if slot in node:
                try: resolver.expand(node[slot])
                except ValueError:
                    raise AgentInputError(f'{slot} {node[slot]!r} is not an absolute URI or a CURIE with a prefix REVEAL knows. '
                                          'Known prefixes: ' + ', '.join(sorted(resolver.prefixes, key=str.lower))) from None
        propositions.append({'id': dapper.compute_id(node, 'Proposition', dapper.schema), **node})
    context = {'knowledge_gaps': [gap], 'prefixes': dict(PREFIXES), **({'propositions': propositions} if propositions else {})}
    package = {'package_version': PACKAGE_VERSION,
               'dapper_pin': {'name': 'configured-snapshot', 'snapshot_sha256': dapper.manifest['snapshot_sha256'],
                              'snapshot_manifest_sha256': dapper.manifest_sha256},
               'selection': {'knowledge_gap_id': gap['id']}, 'dapper_context': context, 'source_artifacts': {}}
    return {'question': question,
            'claims': [{'statement': node['statement'], 'proposition_id': node['id'], **{slot: node[slot] for slot in TRIPLE_SLOTS if slot in node}}
                       for node in propositions],
            'package': package}


# Record kinds. The account is kept with the job that made it, readable by the job's owner only; it is not entered in
# REVEAL's catalogue of curated research accounts.
ACCOUNT_KIND = 'agent_account'
VALIDATED = 'validated'


def requested_claims(requested, document):
    """What became of each claim the requester asked about: the Claims the account makes on it, if any. A requested
    claim the agent did not assess is reported, never added on the agent's behalf."""
    claims, statements = {}, {}
    for claim in document.get('claims', []):
        claims.setdefault(claim.get('proposition'), []).append(claim['id'])
    for node in document.get('propositions', []):
        statements.setdefault(node.get('statement'), node['id'])
    result = []
    for entry in requested:
        proposition = entry['proposition_id'] if entry['proposition_id'] in claims else statements.get(entry['statement'])
        found = claims.get(proposition, [])
        result.append({'statement': entry['statement'], 'assessed': bool(found), 'claim_ids': found})
    return result


def bind(document, ledger_root, calls):
    """The agent's account with REVEAL's record of each quoted read attached (literature_locator.bind_sources).
    Raises AccountValidationError, with one literature-quote finding per quote that cannot be tied to a read."""
    from . import literature_locator as locator
    from .scientific_account_lint import AccountValidationError

    def refuse(problems):
        raise AccountValidationError({'report_version': 'reveal.account-lint/1', 'mode': 'final', 'valid': False, 'findings': [
            {'severity': 'error', 'check': 'literature-quote', 'where': where, 'message': message, 'why': ''} for where, message in problems]})
    try:
        papers = locator.paper_texts(ledger_root, calls)
    except locator.LocatorError as exc:
        refuse([('ledger', str(exc))])
    bound, problems = locator.bind_sources(document, papers, locator.captures(calls))
    if problems:
        refuse(problems)
    return bound


def accept(root, job, attempt, account, attribution, result, output_dir):
    """Check the one account an agent job wrote and put it in its deposit form. Returns the record to checkpoint.

    Runs once, in the workflow's validate step. Assembly stamps the time and the deposit form is minted from it, so a
    second run would give different ids: everything is written under `root` here, and the commit step only reads it
    back (`stored`).

    `account` is the frozen input's account block, `attribution` the requester as recorded at submission (it is never
    in the frozen input, which is copied into the sandbox). The gate attributes the account to the requester as
    it does a research account; the deposit form keeps that only when the request set `attribute_requester`. Raises AccountValidationError or EvidenceBuildError when
    the account is not acceptable; the workflow ends the job as VALIDATION_FAILED."""
    from . import account_deposit
    from .acceptance import assemble_account
    from .admin_jobs import redact
    from .evidence_package import EvidenceBuildError, canonical_json, decode, require, sha256
    from .literature_locator import LocatorError
    from .scientific_account_lint import AccountValidationError
    from .worker import assert_artifact

    # The account is output/account-1.json, the file the agent is told to save. A second draft the agent left beside
    # it is ignored, never a reason to lose the first.
    chosen = [path for path in result.account_paths if Path(path).name == 'account-1.json']
    require(len(chosen) == 1, 'An account job returns its account as output/account-1.json')
    require(result.ledger_manifest_path is not None, 'An account job needs its tool ledger')
    directory = root / VALIDATED; (directory / 'papers').mkdir(parents=True, exist_ok=True)
    diagnostics = root / f'attempt-{attempt}'; diagnostics.mkdir(exist_ok=True)
    package_path = directory / 'account-package.json'; package_path.write_bytes(canonical_json(account['package']))
    ledger_path = Path(result.ledger_manifest_path)
    calls = decode(ledger_path.read_bytes())['calls']
    try:
        # The agent names each paper and quotes it. REVEAL ties every quote to the read that served it, exactly as
        # the in-sandbox lint did, and checks that copy; a quote it cannot tie to a read fails the account.
        bound_path = diagnostics / 'account-bound.json'
        bound_path.write_bytes(canonical_json(bind(decode(assert_artifact(chosen[0], output_dir).read_bytes()), ledger_path.parent, calls)))
        document, report = assemble_account(bound_path, package_path,
                                            directory / 'account-assembled.json', attribution, job, attempt, 'box',
                                            result.ledger_manifest_path)
    except AccountValidationError as exc:
        (diagnostics / 'validation-1.json').write_bytes(canonical_json(redact(exc.report)))
        raise
    (diagnostics / 'validation-1.json').write_bytes(canonical_json(redact(report)))
    try:
        deposit, summary = account_deposit.deposit_document(document, ledger_path.parent, calls,
                                                            attribute_requester=account.get('attribute_requester') is True)
        texts = account_deposit.paper_text_files(ledger_path.parent, calls, [paper['text_sha256'] for paper in summary['papers']])
    except (account_deposit.DepositError, LocatorError) as exc:
        # The in-sandbox lint applies the same quote rule, so this is a defect or a tampered draft, not a repairable
        # authoring mistake. Either way no account is accepted.
        raise EvidenceBuildError('The account could not be put in its deposit form: ' + str(exc)) from exc
    data = canonical_json(deposit)
    target = directory / 'account.dapper.json'; target.write_bytes(data)
    papers = []
    for paper in summary['papers']:
        text = texts.get(paper['text_sha256'])
        require(text is not None and len(text) == paper['text_bytes'], 'A cited paper text is missing from the run capture')
        path = directory / 'papers' / paper['text_sha256']; path.write_bytes(text)
        papers.append({**paper, 'path': str(path.relative_to(root))})
    findings = report.get('findings', []) if isinstance(report, dict) else []
    return {'document': {'path': str(target.relative_to(root)), 'sha256': sha256(data), 'size_bytes': len(data)},
            'account_id': summary['account_id'], 'dapper_version': summary['dapper_version'],
            'objects': summary['objects'], 'edges': summary['edges'], 'claims': summary['claims'],
            'value_locators': summary['value_locators'], 'papers': papers,
            'requested_claims': requested_claims(account['claims'], deposit),
            'lint_warnings': sum(1 for finding in findings if isinstance(finding, dict) and finding.get('severity') == 'warning')}


def stored(root, record):
    """(the deposit-form document's bytes, {text sha256: bytes}) read back from the checkpointed workspace, each
    checked against the checkpoint. The commit step stores and deposits exactly these bytes."""
    from .evidence_package import require, sha256
    from .worker import assert_artifact
    data = assert_artifact(root / record['document']['path'], root / VALIDATED).read_bytes()
    require(sha256(data) == record['document']['sha256'], 'The checked account changed after validation')
    texts = {}
    for paper in record['papers']:
        text = assert_artifact(root / paper['path'], root / VALIDATED).read_bytes()
        require(sha256(text) == paper['text_sha256'], 'A cited paper text changed after validation')
        texts[paper['text_sha256']] = text
    return data, texts


def retain(job_id, sha256, data, content_type):
    """Keep bytes beyond the workflow's scratch directory: object storage where configured, else the artifacts
    directory. Returns the location record `served` reads."""
    from .artifact_store import s3_enabled, store
    if s3_enabled():
        return {'storage': store().put(data, content_type)}
    from .runtime_config import artifacts_root
    path = artifacts_root() / 'agent-accounts' / job_id / sha256
    path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    return {'path': str(path)}


def public(record, deposit):
    """The `account` block of the job result."""
    return {'account_id': record['account_id'], 'dapper_version': record['dapper_version'],
            'document_sha256': record['document']['sha256'], 'document_bytes': record['document']['size_bytes'],
            'objects': record['objects'], 'edges': record['edges'], 'claims': record['claims'],
            'value_locators': record['value_locators'], 'lint_warnings': record['lint_warnings'],
            'requested_claims': record['requested_claims'],
            'papers': [{key: paper[key] for key in ('source', 'id', 'section', 'reference', 'text_sha256', 'text_bytes',
                                                    'media_type', 'filename', 'evidence_items', 'file_id')}
                       for paper in record['papers']],
            'deposit': deposit}


def save(tx, owner, job_id, record, document, retained, deposit, observed_at):
    """Write the account's record inside the caller's fenced transaction. Retrying the commit writes it again."""
    tx.put(ACCOUNT_KIND, job_id, owner, {
        'job_id': job_id, 'account': public(record, deposit), 'document': document, 'observed_at': observed_at,
        'texts': {paper['text_sha256']: {**retained[paper['text_sha256']], 'filename': paper['filename'],
                                         'media_type': paper['media_type'], 'size_bytes': paper['text_bytes']}
                  for paper in record['papers']}})
