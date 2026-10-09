"""Early locked-schema feedback and bounded, value-free hosted diagnostics.

This is representation assistance, not scientific validation or acceptance.
The complete account linter remains authoritative after trusted hydration.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reveal_backend.evidence_package import canonical_json

FORMAT = 'reveal.authoring-structure/1'
REPORT_FORMAT = 'reveal.hosted-diagnostics/1'
MAX_REPORT_BYTES = 1_000_000
MAX_REPORT_TOTAL = 4_000_000
MAX_PREVIEW_BYTES = 10_000
DEFERRED = frozenset(('was_generated_by', 'was_attributed_to'))


def _finding(check, where, rule, message, **details):
    return {'severity': 'error', 'check': check, 'where': where,
            'rule': rule, 'message': message, **details}


def preflight_document(document, *, dapper_root, release_lock, timeout=12):
    """Validate exact authored shapes in a fresh, hash-verified interpreter.

    Omitted runtime attribution and absent referenced bodies are deferred.
    Supplied malformed attribution, fields, identifiers and enums are not.
    """
    raw = canonical_json(document)
    with tempfile.TemporaryDirectory(prefix='reveal-authoring-structure-') as directory:
        path = Path(directory) / 'draft.json'; path.write_bytes(raw)
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        try:
            process = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                str(path), str(Path(dapper_root).resolve()), str(Path(release_lock).resolve())],
                capture_output=True, timeout=timeout, env=environment)
            if process.returncode or len(process.stdout) > 8_000_000:
                raise ValueError('Structure checker unavailable')
            report = json.loads(process.stdout)
            if report.get('format') != FORMAT or report.get('document_sha256') != hashlib.sha256(raw).hexdigest():
                raise ValueError('Structure checker response differs')
            return report
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {'format': FORMAT, 'valid': False, 'operational_error': True,
                'document_sha256': hashlib.sha256(raw).hexdigest(),
                'counts': {'errors': 1, 'warnings': 0}, 'findings': [
                    _finding('structure-runtime', 'runtime', 'unavailable',
                        'The pinned structure checker did not complete; retry without changing scientific claims.')]}


def _required_field(error):
    if error.validator == 'required':
        return next((field for field in error.validator_value
                     if error.message == f'{field!r} is a required property'), None)
    return None


def _schema_finding(error, where):
    from jsonschema.exceptions import best_match
    error = best_match([error])
    parts = list(error.absolute_path)
    field = _required_field(error)
    if field is not None: parts.append(field)
    path = where + ''.join(f'[{part}]' if isinstance(part, int) else '.' + str(part) for part in parts)
    messages = {'required': 'Required field is missing.', 'additionalProperties': 'Remove fields absent from the pinned class.',
        'type': 'Use the type required by the pinned field.', 'enum': 'Use an exact value from the pinned enum.',
        'pattern': 'Value does not satisfy the pinned field pattern.', 'minItems': 'Include the required number of references.',
        'anyOf': 'Satisfy one of the pinned alternatives.', 'allOf': 'Satisfy every pinned field condition.',
        'oneOf': 'Satisfy exactly one pinned alternative.'}
    details = {}
    if error.validator in ('type', 'enum', 'minItems', 'maxItems', 'minimum', 'maximum'):
        details['expected'] = error.validator_value
    if error.validator == 'additionalProperties' and isinstance(error.instance, dict):
        properties = error.schema.get('properties', {})
        patterns = error.schema.get('patternProperties', {})
        details['fields'] = sorted(name for name in error.instance if name not in properties
                                  and not any(re.search(pattern, name) for pattern in patterns))
    return _finding('nodes', path, str(error.validator),
                    messages.get(error.validator, 'Follow the pinned field constraint.'), **details)


def _preflight(document, dapper_root, release_lock):
    from reveal_backend.dapper_release import verify_release
    release = verify_release(dapper_root, release_lock)
    schema_root = Path(dapper_root).resolve() / 'schema'
    sys.path[:0] = [str(schema_root / 'lint'), str(schema_root / 'identity'), str(schema_root)]
    import yaml
    from lint_provenance import Vocabulary, build_validator, META_KEYS
    from dapper_identity import load_schema
    from document_prefixes import transform_identifiers
    sv = load_schema(schema_root / 'dapper.yaml')
    vocabulary = Vocabulary.build(sv, yaml.safe_load((schema_root / 'lint/profiles.yaml').read_text()))
    validator = build_validator(schema_root / 'dapper.yaml')
    findings, nodes = [], 0
    groups = {**vocabulary.node_groups, **vocabulary.edge_groups}
    if not isinstance(document, dict):
        findings.append(_finding('shape', 'document', 'type', 'Use a document object containing plural group arrays.'))
        resolved = {}
    else:
        resolved, prefix_errors = transform_identifiers(document, sv, groups, compact_dapper=True)
        findings.extend(_finding('prefixes', where, 'identifier',
            'Use an absolute URI or a declared, non-conflicting CURIE; inspect the pinned prefixes.') for where, _ in prefix_errors)
    accounts = resolved.get('scientific_accounts')
    if not isinstance(accounts, list) or len(accounts) != 1:
        findings.append(_finding('shape', 'scientific_accounts', 'cardinality', 'Supply exactly one ScientificAccount in its array.'))
    seen, supplied, indexed, locations = set(), {}, {}, {}
    for group, values in resolved.items():
        if group in META_KEYS: continue
        if group not in groups:
            findings.append(_finding('shape', str(group), 'unknown_group', 'Use a plural group from the pinned document vocabulary.'))
            continue
        if not isinstance(values, list):
            findings.append(_finding('shape', group, 'type', 'Use an array for this document group.')); continue
        cls = groups[group]
        # This is the same closed JSON Schema used by the authoritative linter.
        # Inspect errors directly: LinkML's rendered anyOf message can repeat
        # an entire account, obscuring useful paths and exhausting capture space.
        schema_validator = validator._context(cls).json_schema_validator(closed=True, include_range_class_descendants=True)
        for index, node in enumerate(values):
            where = f'{group}[{index}]'; nodes += 1
            if not isinstance(node, dict):
                findings.append(_finding('shape', where, 'type', 'Use an object for each node.')); continue
            identity = node.get('id')
            if group in vocabulary.node_groups and isinstance(identity, str):
                if identity in seen:
                    findings.append(_finding('shape', where + '.id', 'duplicate_id', 'Each supplied node must have a distinct identifier.'))
                seen.add(identity)
                supplied.setdefault(identity, cls)
                indexed.setdefault(identity, (cls, node))
                locations.setdefault(identity, where)
            for error in schema_validator.iter_errors(node):
                field = _required_field(error)
                if cls in ('Claim', 'ScientificAccount') and not error.absolute_path and field in DEFERRED and field not in node:
                    continue
                findings.append(_schema_finding(error, where))
    # Reference existence belongs to hydration. A supplied target of the wrong
    # class is already knowable and must not be excused as an omitted body.
    classes = sv.all_classes()
    for group, values in resolved.items():
        if group not in vocabulary.node_groups or not isinstance(values, list): continue
        cls = groups[group]
        slots = [slot for slot in sv.class_induced_slots(cls)
                 if slot.is_a == 'relationship' and slot.range in classes and not slot.inlined]
        for index, node in enumerate(values):
            if not isinstance(node, dict): continue
            for slot in slots:
                value = node.get(slot.name)
                for offset, reference in enumerate(value if isinstance(value, list) else [value]):
                    actual = supplied.get(reference) if isinstance(reference, str) else None
                    if actual is not None and slot.range not in sv.class_ancestors(actual):
                        where = f'{group}[{index}].{slot.name}' + (f'[{offset}]' if isinstance(value, list) else '')
                        findings.append(_finding('endpoints', where, 'reference_class',
                            'Use a reference to the required class or one of its subclasses.', expected=slot.range))
    from scientific_claims import check_scientific_content
    # Cross-record routines assume shaped scalar/reference fields. The exact
    # schema errors already identify malformed types; never turn them into a
    # runtime failure while trying to inspect their graph.
    shaped = not any(f['check'] == 'nodes' and f['rule'] == 'type' for f in findings)
    for where, message in check_scientific_content(indexed) if shaped else []:
        # The upstream checker also requires dependency bodies. Missing bodies
        # are precisely what hydration supplies; defer only that case.
        if message.endswith('found no local record'): continue
        path = next((location + where[len(identity):] for identity, location in locations.items()
                     if where == identity or where.startswith(identity + '.')), 'document')
        if 'must resolve to' in message: continue  # Exact field/range already checked above.
        if 'different proposition' in message: continue  # More precise EvidenceItem path below.
        rules = {'references must be distinct': ('distinct_references', 'References in this field must be distinct.'),
            'conclusion_claims must be a subset of component_claims': ('conclusion_subset', 'conclusion_claims must be a subset of component_claims.'),
            'an account needs at least one claim outside its closing conclusions': ('supporting_claim', 'Retain at least one component Claim outside conclusion_claims.'),
            'framing needs a Question (including KnowledgeGap) or hypothesis': ('framing', 'Supply question or hypothesis framing.'),
            'circular evidential support between claims': ('evidence_cycle', 'Remove circular evidential support between Claims.')}
        rule, text = rules.get(message, ('scientific_structure', 'Follow the pinned scientific content relationships.'))
        findings.append(_finding('scientific-content', path, rule, text))
    # REVEAL's structural authoring policy is stricter than optional upstream
    # slots. Mirror only these local checks from scientific_account_lint;
    # selected-gap identity, ancestry and source fidelity still run after export.
    for account in resolved.get('scientific_accounts', []) if isinstance(accounts, list) else []:
        if not isinstance(account, dict): continue
        account_id = account.get('id')
        account_path = locations.get(account_id, 'scientific_accounts') if isinstance(account_id, str) else 'scientific_accounts'
        if not isinstance(account.get('closing_remarks'), str) or not account['closing_remarks'].strip():
            findings.append(_finding('account-synthesis', account_path + '.closing_remarks', 'required', 'Supply a nonempty closing synthesis.'))
        members = account.get('component_claims', [])
        for reference in members if isinstance(members, list) else []:
            entry = indexed.get(reference) if isinstance(reference, str) else None
            if entry is None or entry[0] != 'Claim': continue
            claim = entry[1]; evidence = claim.get('has_evidence')
            if not isinstance(evidence, list) or not evidence:
                findings.append(_finding('claim-evidence', locations[reference] + '.has_evidence', 'required',
                    'Account Claims require explicit EvidenceItem references.'))
                continue
            for evidence_id in evidence:
                item_entry = indexed.get(evidence_id) if isinstance(evidence_id, str) else None
                if item_entry is None or item_entry[0] != 'EvidenceItem': continue
                item = item_entry[1]; item_path = locations[evidence_id]
                if item.get('target_proposition') != claim.get('proposition'):
                    findings.append(_finding('evidence-target', item_path + '.target_proposition', 'target_match',
                        'Use the same Proposition as the owning Claim.'))
                for field in ('direction', 'context', 'explanation'):
                    if not isinstance(item.get(field), str) or not item[field].strip():
                        findings.append(_finding('evidence-interpretation', item_path + '.' + field, 'required',
                            'Supply this nonempty EvidenceItem interpretation field.'))
    return {'format': FORMAT, 'valid': not findings,
        'document_sha256': hashlib.sha256(canonical_json(document)).hexdigest(), 'dapper_release': release,
        'deferred': ['omitted runtime attribution', 'referenced dependency bodies', 'source fidelity and scientific validation'],
        'counts': {'nodes': nodes, 'errors': len(findings), 'warnings': 0}, 'findings': findings}


# A dangling DAPPER reference names only an identifier, never prose.
REFERENCE = re.compile(r'reference to ((?:dapper:[A-Za-z]+\.[A-Za-z0-9_-]{32})|(?:urn:[A-Za-z0-9:._~-]{1,280})|(?:https?://[^\s<>"]{1,280})) does not resolve')


def _bounded(value, depth=0):
    """Repair details are short data: strings at most 400 characters, at most 20 items, shallow."""
    if isinstance(value, str): return value[:400]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)): return value
    if isinstance(value, list) and depth < 2: return [_bounded(item, depth + 1) for item in value[:20]]
    if isinstance(value, dict) and depth < 2: return {str(key)[:80]: _bounded(item, depth + 1) for key, item in list(value.items())[:20]}
    return None


def _normalized_finding(finding):
    """Keep all locations/rules; never repeat rendered lint messages in feedback.

    Source-fidelity findings carry a bounded `repair` with the cited source values or excerpt to copy, and a
    dangling reference names the unresolved identifier, so the author can fix the field in place.
    """
    check = finding.get('check', 'validation')
    where = finding.get('where', '')
    rule = finding.get('rule')
    details = {}
    if finding.get('severity') == 'suggestion':  # claim_suggestions emits value-free templates and claim ids only
        return {'severity': 'suggestion', 'check': check, 'where': where, 'rule': check, 'message': finding.get('message', ''),
                **{key: finding[key] for key in ('repair', 'family', 'claims', 'count') if key in finding}}
    if rule:  # The structure checker already emits value-free diagnostics.
        message = finding['message']
        details = {key: finding[key] for key in ('expected', 'fields') if key in finding}
    else:
        # Ordinary lint messages may quote private account or source text.
        # Preserve their exact original check/location, not the quoted values.
        original = finding.get('message', '')
        if check == 'nodes':
            suffix = re.search(r' in (/(?:[A-Za-z0-9_~.-]+/)*[A-Za-z0-9_~.-]*)\Z', original)
            if suffix and suffix.group(1) != '/': where += suffix.group(1)
            required = re.match(r"^[A-Za-z][A-Za-z0-9_]*: '([A-Za-z_][A-Za-z0-9_]*)' is a required property(?: in /[^\n]*)?\Z", original)
            if required:
                where += '.' + required.group(1); rule = 'required'
                message = 'Required field is missing.'
            elif 'Additional properties are not allowed' in original:
                rule = 'additionalProperties'; message = 'Remove fields absent from the pinned class.'
            else:
                rule = 'schema'; message = 'Use the pinned type, enum and conditional requirements for this field.'
        else:
            rule = check
            message = {
                'prefixes': 'Use an absolute URI or a declared, non-conflicting CURIE.',
                'claim-evidence': 'Link the Claim to target-matched EvidenceItems with eligible source observations.',
                'source-ancestry': 'Preserve the exact eligible source and upstream provenance path.',
                'source-locator': 'Use an exact locator in the captured source artifact.',
                'source-snippet': 'Copy the exact observed source text at the stated locator.',
                'evidence-snippet': 'Replace snippet with a verbatim excerpt of repair.source_excerpt, the cited source text at this EvidenceItem locator; put summaries in explanation.',
                'source-metric': 'Set this ClaimScore value to its cited row value in repair.source_values (repair.source_metrics lists the numeric fields when the metric is absent); keep metric and score_kind, and fix the EvidenceItem locator if it cites the wrong row.',
                'refs': 'repair.unresolved names no node in this draft: reference an object present in the draft or an exact trusted id, or add it with write_account_draft so its dependencies are copied.',
                'source-metric-kind': 'Keep the source metric mathematical meaning: loading is LOADING, beta is EFFECT_ESTIMATE, combined is SCORE.',
                'source-file': 'Use the unchanged trusted capture File with its exact checksum and size; do not author replacement source bytes.',
                'evidence-target': 'Set EvidenceItem.target_proposition to the owning Claim.proposition.',
                'evidence-interpretation': 'Supply nonempty EvidenceItem.direction, context and explanation.',
                'selected-gap': 'Reference the exact selected KnowledgeGap in ScientificAccount.question and preserve its trusted body.',
                'draft-id-collision': 'Renumber this kind of draft id at one width (claim-01 to claim-30) so no node id contains another; trusted minting reads an embedded id as a reference and fails on the cycle.',
                'account-synthesis': 'Supply a nonempty closing_remarks synthesis within the account limit.',
                'shape': 'Use the pinned plural group arrays and object records.',
                'scientific-content': 'Check target classes, distinct references, conclusion subsets and acyclic evidence links.',
                'endpoints': 'Use a reference to the class required by the pinned relationship.',
                'references': 'Supply or reference the exact trusted dependency.',
                'linter-runtime': 'The linter did not complete; retry without changing scientific claims.',
                'cfde-grounding-missing': 'Seek relevant CFDE grounding when defensible and explain its absence; supported independent evidence remains eligible.',
                'geneset-provenance-missing': 'Preserve a structural path to the exact trusted GeneSet alongside the captured source File.',
                'entity-reference-missing': 'Use supported structured Proposition entity references; names in prose do not establish identity.',
                'membership-identity-unverified': 'Verify exact gene identity and GeneSet membership; co-loading and perturbation names do not establish membership.',
                'relationship-support-missing': 'Retain an exact source observation supporting this relationship and its biological scope.',
                'query-scope-overclaim': 'Restrict each assertion to its exact queried factors, filters, page and coverage; partial or empty results do not establish broader absence.',
            }.get(check, 'Inspect this location against the pinned account profile and captured source; preserve trusted objects.')
            if check in ('source-metric', 'evidence-snippet') and isinstance(finding.get('repair'), dict):
                details['repair'] = _bounded(finding['repair'])
            elif check == 'refs' and (match := REFERENCE.match(original)):
                details['repair'] = {'unresolved': match.group(1)}
    return {'severity': finding.get('severity', 'error'), 'check': check, 'where': where,
            'rule': rule, 'message': message, **details}


def diagnostic_response(report, *, output, filename, capture_roots=()):
    """Return bounded feedback, retaining the complete normalized finding list.

    Reports are feedback only and never replace the canonical account or
    authoritative linter report. Storage respects report and remaining capture
    budgets; failure to retain diagnostics never changes the account result.
    """
    findings = [_normalized_finding(f) for f in report.get('findings', [])]
    advisories = [_normalized_finding(f) for f in report.get('advisories', [])]
    normalized = {key: report[key] for key in ('valid', 'operational_error', 'mode', 'profile',
        'document_sha256', 'evidence_package_sha256', 'dapper_release', 'counts', 'deferred', 'claim_structure') if key in report}
    normalized.update(format=REPORT_FORMAT, source_report_format=report.get('report_version', report.get('format')),
        message_format='Normalized field/rule diagnostics; original rendered values are omitted. Source-metric, evidence-snippet and refs findings add a bounded repair with the cited source values, a source excerpt or the unresolved id.',
        findings=findings, finding_count=len(findings), findings_complete=True,
        advisories=advisories, advisory_count=len(advisories))
    raw = canonical_json(normalized)
    descriptor = {'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw), 'path': None, 'retained': False}
    # Fixed report budget, hash-addressed reuse, and no symlink writes. The
    # agent can read these ordinary JSON files without a shell/decompressor.
    root_fd = report_fd = None
    try:
        if len(raw) > MAX_REPORT_BYTES: raise ValueError('report_byte_budget')
        root_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.mkdir('reports', mode=0o755, dir_fd=root_fd)
        except FileExistsError: pass
        report_fd = os.open('reports', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        info = os.fstat(report_fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022: raise ValueError('unsafe_report_directory')
        os.fchmod(report_fd, 0o755)  # The unprivileged agent must be able to Read, even under umask 0077.
        name = Path(filename).stem + '.' + descriptor['sha256'] + '.json'
        existing = os.listdir(report_fd)
        used = sum(os.stat(item, dir_fd=report_fd, follow_symlinks=False).st_size for item in existing)
        if name not in existing and used + len(raw) > MAX_REPORT_TOTAL: raise ValueError('report_total_budget')
        if name not in existing and capture_roots:
            from reveal_backend.box_upload import MAX_TOTAL
            captured = 0
            for root in map(Path, capture_roots):
                if not root.exists(): continue
                paths = [root] if root.is_file() else root.rglob('*')
                for path in paths:
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode): raise ValueError('unsafe_capture_path')
                    if stat.S_ISREG(info.st_mode): captured += info.st_size
            # Leave room for the bounded tool response, its duplicate ledger
            # projection and the final runtime timing/usage update.
            if captured + len(raw) + 100_000 > MAX_TOTAL: raise ValueError('capture_total_budget')
        try: fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=report_fd)
        except FileExistsError:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=report_fd)
            with os.fdopen(fd, 'rb') as saved:
                metadata = os.fstat(saved.fileno())
                if metadata.st_uid != os.geteuid() or not stat.S_ISREG(metadata.st_mode) or saved.read(MAX_REPORT_BYTES + 1) != raw:
                    raise ValueError('report_checksum_mismatch')
                os.fchmod(saved.fileno(), 0o644)
        else:
            with os.fdopen(fd, 'wb') as saved:
                os.fchmod(saved.fileno(), 0o644)
                saved.write(raw)
        descriptor.update(path=str(Path(output) / 'reports' / name), retained=True)
    except (OSError, ValueError):
        descriptor['limitation'] = 'Normalized report could not be retained within the safe diagnostic storage budget.'
    finally:
        if report_fd is not None: os.close(report_fd)
        if root_fd is not None: os.close(root_fd)
    counts = Counter((f['severity'], f['check'], f['rule']) for f in findings)
    summary = {'format': REPORT_FORMAT, 'valid': bool(report.get('valid')),
        'counts': report.get('counts', {'errors': sum(f['severity'] == 'error' for f in findings),
                                      'warnings': sum(f['severity'] == 'warning' for f in findings)}),
        'finding_count': len(findings), 'report': descriptor, 'findings': [],
        'advisory_count': len(advisories), 'advisories': [],
        'groups': [{'severity': severity, 'check': check, 'rule': rule, 'count': count}
                   for (severity, check, rule), count in counts.items()]}
    if report.get('operational_error'): summary['operational_error'] = True
    if 'claim_structure' in report: summary['claim_structure'] = report['claim_structure']
    for finding in findings:
        candidate = {**summary, 'findings': [*summary['findings'], finding]}
        if len(canonical_json(candidate)) > MAX_PREVIEW_BYTES: break
        summary['findings'].append(finding)
    summary['findings_included'] = len(summary['findings'])
    summary['findings_omitted'] = len(findings) - len(summary['findings'])
    for advisory in advisories:
        candidate = {**summary, 'advisories': [*summary['advisories'], advisory]}
        if len(canonical_json(candidate)) > MAX_PREVIEW_BYTES: break
        summary['advisories'].append(advisory)
    summary['advisories_omitted'] = len(advisories) - len(summary['advisories'])
    if report.get('operational_error'):
        summary['guidance'] = 'The checker did not complete. Retry the same check without changing scientific claims; this is an operational failure, not a finding about the evidence.'
    elif report.get('valid'):
        summary['guidance'] = 'Draft lint passed. Stop schema repairs and return the completed output with this result. The backend still performs independent validation and acceptance; this report accepts no science. Advisories are optional guidance, not errors.'
    else:
        summary['guidance'] = 'Repair the listed fields using the pinned skeleton/schema, then write and lint again. Read report.path for every normalized finding. Advisories are optional guidance, not errors.'
    return {'isError': not report.get('valid'), 'structuredContent': summary,
            'content': [{'type': 'text', 'text': json.dumps(summary, ensure_ascii=False)}]}


if __name__ == '__main__':
    result = _preflight(json.loads(Path(sys.argv[1]).read_bytes()), sys.argv[2], sys.argv[3])
    sys.stdout.buffer.write(canonical_json(result))
