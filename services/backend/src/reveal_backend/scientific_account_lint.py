"""One scientific-account linter shared by agent feedback and backend validation.

The upstream DAPPER modules run in a fresh interpreter so another imported
schema snapshot cannot leak into validation of this release.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reveal_backend.dapper_release import verify_release
from reveal_backend.evidence_package import EvidenceBuildError, canonical_json, decode, require, sha256
from reveal_backend.source_validation import source_findings


class AccountValidationError(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__('Scientific account failed validation; inspect report.findings')


def cfde_source_files(package, package_path):
    """Recognize frozen reference evidence across the HTTP and SQL collectors.

    A SQL-looking origin alone is not evidence. Bind its verified capture to the
    package's reference generation and source envelope, and follow derived
    captures only when all their inputs are captured reference observations.
    """
    sources = package['source_artifacts']
    accepted = {key for key, source in sources.items() if isinstance(source.get('origin'), str)
                and source['origin'].startswith(('https://dev.cfdeknowledge.org/api/',
                                                  'https://cfde-dev.hugeampkpnbi.org/api/'))}
    pigean = package.get('pigean', {})
    generations = {node.get('fit', {}).get('upstream_build') for node in pigean.get('mechanisms', {}).values()}
    if pigean.get('model') == 'eaggl-capped-v1' and len(generations) == 1:
        generation = next(iter(generations))
        if isinstance(generation, str) and re.fullmatch(r'[a-f0-9]{64}', generation):
            tables = {'reference_factors', 'kpn_traits', 'eaggl_factors', 'eaggl_genes',
                      'eaggl_gene_loadings', 'factor_gene_set_projections', 'cfde_gene_sets',
                      'cfde_gene_set_collections'}
            reference, derived = set(), {}
            root = Path(package_path).resolve().parent
            for key, source in sources.items():
                origin = source.get('origin')
                if source.get('format') != 'json' or not isinstance(origin, str) or not origin.startswith(('mysql:', 'mysql-derived:')):
                    continue
                path = (root / source['path']).resolve()
                require(path.is_relative_to(root), 'Source artifact path escape')
                raw = path.read_bytes()
                require(sha256(raw) == source['sha256'], 'Captured source checksum changed')
                capture = decode(raw)
                provenance = capture.get('source', {})
                kind = provenance.get('kind')
                names = provenance.get('tables' if kind == 'mysql' else 'derived_from')
                if (capture.get('generation_id') != generation or capture.get('model') != 'eaggl-capped-v1'
                        or not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names)
                        or origin != f"{kind}:{'+'.join(names)}?generation_id={generation}"):
                    continue
                if kind == 'mysql' and capture.get('format') == 'reveal.reference-evidence.mysql-capture/1' and set(names) <= tables:
                    reference.add(key)
                elif kind == 'mysql-derived' and capture.get('format') == 'reveal.reference-evidence.derived-capture/1':
                    derived[key] = set(names)
            while added := {key for key, dependencies in derived.items() if key not in reference and dependencies <= reference}:
                reference.update(added)
            accepted.update(reference)
    return {sources[key]['dapper_file_id'] for key in accepted}


def lint_scientific_account(document, *, dapper_root, release_lock, evidence_package=None, ledger_path=None, mode='draft', strict=False):
    """Return a machine-readable report. Files are read, never minted or edited."""
    command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--internal',
               str(Path(document).resolve()), str(Path(dapper_root).resolve()), str(Path(release_lock).resolve()),
               str(Path(evidence_package).resolve()) if evidence_package else '', mode, 'strict' if strict else 'normal',
               str(Path(ledger_path).resolve()) if ledger_path else '']
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None); environment['PYTHONDONTWRITEBYTECODE'] = '1'
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=120, env=environment)
        require(process.returncode == 0, f'Account linter process failed: {process.stderr.strip()}')
        report = decode(process.stdout.encode())
        require(report.get('report_version') == 'reveal.account-lint/1', 'Unexpected account linter report')
        return report
    except (OSError, subprocess.TimeoutExpired, EvidenceBuildError) as exc:
        return {'report_version': 'reveal.account-lint/1', 'mode': mode, 'valid': False, 'operational_error': True,
                'findings': [{'severity': 'error', 'check': 'linter-runtime', 'where': 'runtime', 'message': str(exc), 'why': ''}]}


def validate_scientific_account(document, *, dapper_root, release_lock, evidence_package, ledger_path=None, strict=False):
    """Backend gate: rerun the same linter on actual output, in final mode.

    Passing this gate verifies structure and source fidelity, not scientific acceptance.
    Never accept an agent-written report as a replacement for this invocation.
    """
    report = lint_scientific_account(document, dapper_root=dapper_root, release_lock=release_lock,
                                     evidence_package=evidence_package, ledger_path=ledger_path, mode='final', strict=strict)
    if not report['valid']:
        raise AccountValidationError(report)
    return report


def _lint(document_path, dapper_root, lock_path, package_path, mode, strict, ledger_path=None):
    require(mode in ('draft', 'final', 'profile-only'), 'Unknown account lint mode')
    require(mode == 'profile-only' or package_path, 'REVEAL linting requires the frozen evidence package')
    release = verify_release(dapper_root, lock_path)
    raw = Path(document_path).read_bytes()
    document = decode(raw, 'yaml' if Path(document_path).suffix in ('.yaml', '.yml') else 'json')
    require(isinstance(document, dict), 'Account document must be a mapping')
    schema_root = Path(dapper_root).resolve() / 'schema'
    sys.path[:0] = [str(schema_root / 'lint'), str(schema_root / 'identity'), str(schema_root)]
    import yaml
    from lint_provenance import Vocabulary, build_validator, lint
    from dapper_identity import DOC_GROUPS, load_schema
    sv = load_schema(schema_root / 'dapper.yaml')
    vocabulary = Vocabulary.build(sv, yaml.safe_load((schema_root / 'lint/profiles.yaml').read_text()))
    validator = build_validator(schema_root / 'dapper.yaml')
    # Validate the exact parsed bytes; do not reread a file the agent may edit mid-check.
    with tempfile.TemporaryDirectory(prefix='reveal-account-lint-') as directory:
        frozen = Path(directory) / 'account.json'; frozen.write_bytes(canonical_json(document))
        upstream = lint(frozen, vocabulary, sv, validator, profile_name='scientific-account')
    findings = [asdict(f) for f in upstream.findings]
    def error(check, where, message):
        findings.append({'severity': 'error', 'check': check, 'where': where, 'message': message, 'why': ''})
    package_hash = None
    if mode != 'profile-only':
        package_raw = Path(package_path).read_bytes()
        package = decode(package_raw, 'yaml' if Path(package_path).suffix in ('.yaml', '.yml') else 'json')
        package_hash = sha256(package_raw)
        lock = decode(Path(lock_path).read_bytes())
        require(package['package_version'] == 'reveal.evidence-package/0.2-draft', 'Unsupported evidence package')
        require(package['dapper_pin']['snapshot_sha256'] in lock['compatible_input_snapshots'], 'Evidence-package DAPPER pin is not approved for the release')
        expected_gap = package['selection']['knowledge_gap_id']
        nodes = {n['id']: (DOC_GROUPS[group], n) for group, rows in document.items() if group in DOC_GROUPS and isinstance(rows, list)
                 for n in rows if isinstance(n, dict) and isinstance(n.get('id'), str)}
        trusted = {n['id']: n for group, rows in package['dapper_context'].items() if group in DOC_GROUPS
                   for n in rows}
        require(expected_gap in trusted, 'Frozen gap is missing from trusted inputs')
        for identity, (_, node) in nodes.items():
            if identity in trusted and node != trusted[identity]:
                error('trusted-input', identity, 'Saved input payload differs from the frozen evidence package')
        for account in document.get('scientific_accounts', []) if isinstance(document.get('scientific_accounts'), list) else []:
            if not isinstance(account, dict):
                continue
            identity = account.get('id', 'scientific_accounts')
            if account.get('question') != expected_gap:
                error('selected-gap', identity, 'Account.question must reference the exact selected DisMech KnowledgeGap')
            if nodes.get(expected_gap, (None,))[0] != 'KnowledgeGap':
                error('selected-gap', identity, 'Hydrate the selected KnowledgeGap in this account document')
            if not isinstance(account.get('closing_remarks'), str) or not account['closing_remarks'].strip():
                error('account-synthesis', identity, 'A nonempty closing_remarks synthesis is required')
        # Structural CFDE ancestry is explicit evidence lineage, not shared Activity inputs.
        cfde_files = cfde_source_files(package, package_path)
        def evidence_lineage(identity, seen):
            if identity in cfde_files:
                return identity in nodes and identity in trusted and nodes[identity][1] == trusted[identity]
            if identity in seen or identity not in nodes:
                return False
            seen = seen | {identity}; _, node = nodes[identity]
            refs = []
            for field in ('has_evidence', 'source_claims', 'was_derived_from'):
                value = node.get(field, [])
                if isinstance(value, list): refs.extend(r for r in value if isinstance(r, str))
            return any(evidence_lineage(r, seen) for r in refs)
        for account in document.get('scientific_accounts', []) if isinstance(document.get('scientific_accounts'), list) else []:
            if not isinstance(account, dict): continue
            for claim_id in account.get('component_claims', []) if isinstance(account.get('component_claims'), list) else []:
                if not isinstance(claim_id, str) or claim_id not in nodes: continue
                claim = nodes[claim_id][1]
                if not isinstance(claim.get('has_evidence'), list) or not claim['has_evidence']:
                    error('claim-evidence', claim_id, 'Account Claims require explicit EvidenceItems assessing their proposition')
                if not evidence_lineage(claim_id, set()):
                    error('cfde-ancestry', claim_id, 'Account Claim lacks explicit evidence lineage to an unchanged captured CFDE File')
                for evidence_id in claim.get('has_evidence', []) if isinstance(claim.get('has_evidence'), list) else []:
                    if not isinstance(evidence_id, str) or evidence_id not in nodes: continue
                    item = nodes[evidence_id][1]
                    if item.get('target_proposition') != claim.get('proposition'):
                        error('evidence-target', evidence_id, 'Evidence target must match its owning Claim proposition')
                    for field in ('direction', 'context', 'explanation'):
                        if not isinstance(item.get(field), str) or not item[field].strip():
                            error('evidence-interpretation', evidence_id, f'EvidenceItem requires nonempty {field}')
        findings.extend(source_findings(document, package, package_path, ledger_path or None))
        if mode == 'final':
            for identity, (cls, _) in nodes.items():
                if cls in ('ScientificAccount', 'Claim', 'Proposition', 'EvidenceItem', 'KnowledgeGap') and not identity.startswith('dapper:' + cls + '.'):
                    error('final-identity', identity, 'Final scientific nodes must have verified DAPPER digest IDs; mint authored nodes in trusted assembly')
    errors = sum(f['severity'] == 'error' for f in findings)
    warnings = sum(f['severity'] == 'warning' for f in findings)
    return {'report_version': 'reveal.account-lint/1', 'mode': mode, 'profile': 'scientific-account',
            'valid': errors == 0 and (not strict or warnings == 0), 'strict': strict,
            'document_sha256': sha256(raw), 'evidence_package_sha256': package_hash,
            'dapper_release': release, 'counts': {**upstream.counts, 'errors': errors, 'warnings': warnings},
            'findings': findings, 'scientific_grounding_evaluated': False,
            'remaining_acceptance_checks': ['trusted attribution and job ownership',
                                           'external-evidence ledger and tool policy']}


if __name__ == '__main__':
    try:
        require(len(sys.argv) == 9 and sys.argv[1] == '--internal', 'Use scripts/lint_scientific_account.py')
        result = _lint(*sys.argv[2:7], strict=sys.argv[7] == 'strict', ledger_path=sys.argv[8])
    except Exception as exc:
        result = {'report_version': 'reveal.account-lint/1', 'valid': False, 'operational_error': True,
                  'findings': [{'severity': 'error', 'check': 'linter-runtime', 'where': 'runtime', 'message': str(exc), 'why': ''}]}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
