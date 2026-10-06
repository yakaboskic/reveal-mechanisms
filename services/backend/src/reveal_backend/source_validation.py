"""Source fidelity rules shared by agent lint and trusted worker acceptance."""
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from .evidence_package import decode, pointer, require, sha256


def exact_document(raw, format='json'):
    """Preserve numeric spelling for fidelity checks without changing schema types."""
    from .evidence_reader import Number, parse
    if format == 'json': return parse(raw)
    import yaml
    class ExactLoader(yaml.SafeLoader): pass
    def number(loader, node):
        # YAML numeric underscores are separators; Decimal retains all digits.
        return Number(loader.construct_scalar(node).replace('_', ''))
    ExactLoader.add_constructor('tag:yaml.org,2002:float', number)
    return yaml.load(raw, Loader=ExactLoader)


def _numeric(value):
    from .evidence_reader import Number
    if isinstance(value, bool) or not isinstance(value, (Number, int, float)): return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def finding(check, where, message):
    return {'severity': 'error', 'check': check, 'where': where, 'message': message, 'why': ''}


def ledger_sources(ledger_path):
    """Read completed response captures, including a live trusted lint snapshot."""
    if ledger_path is None:
        return {}
    ledger_path = Path(ledger_path).resolve()
    ledger = decode(ledger_path.read_bytes())
    sources = {}
    for call in ledger['calls']:
        if call.get('tool') not in ('query_graph', 'read_paper') or call.get('status') not in ('completed', 'empty') or not call.get('response'):
            continue
        if call['tool'] == 'read_paper' and call['status'] != 'completed':
            continue
        source = call['response']
        path = (ledger_path.parent / source['path']).resolve()
        require(path.is_relative_to(ledger_path.parent), 'Tool source artifact path escape')
        data = path.read_bytes()
        require(sha256(data) == source['sha256'] and len(data) == source['size_bytes'], 'Tool source checksum or size changed')
        sources[source['sha256']] = {'path': path, 'bytes': data, 'size_bytes': len(data),
                                     'tool': call['tool'], 'selected_graph': call.get('selected_graph')}
    return sources


def records(document, group):
    rows = document.get(group, [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def references(node, field):
    values = node.get(field, [])
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def new_file_findings(document, trusted, sources):
    findings = []
    for file in records(document, 'files'):
        if file.get('id') in trusted:
            continue
        captured = sources.get(file.get('sha256'))
        if captured is None or file.get('size_in_bytes') != captured['size_bytes']:
            findings.append(finding('source-file', file.get('id', 'files'),
                'New evidence File is not bound to an exact trusted tool response checksum and size'))
    return findings


def validate_new_files(document, trusted, sources):
    findings = new_file_findings(document, trusted, sources)
    require(not findings, findings[0]['message'] if findings else '')


def observation_findings(document, observed, *, exact_observed=None, authored_exact=None):
    """Check exact cited rows; never use the entire response after a bad locator."""
    nodes = {node['id']: node for group in document for node in records(document, group)
             if isinstance(node.get('id'), str)}
    exact_nodes = {node['id']: node for group in (authored_exact or {}) for node in records(authored_exact, group)
                   if isinstance(node.get('id'), str)}
    findings, located, exact_located = [], {}, {}
    for node in nodes.values():
        direct = [observed[source] for source in references(node, 'was_derived_from') if source in observed]
        exact_direct = [(exact_observed or {}).get(source, observed[source])
                        for source in references(node, 'was_derived_from') if source in observed]
        text = ' '.join(node.get(field, '') for field in ('context', 'source_locator') if isinstance(node.get(field, ''), str))
        locators = list(dict.fromkeys(re.findall(r'/(?:data|result|response|content|structuredContent|segments)(?:/[A-Za-z0-9_~.%-]+)*', text)))
        if any(isinstance(source,dict) and source.get('format')=='reveal.upload-text/1' for source in direct):
            structured=any(isinstance(source,dict) and source.get('structured_extractor')=='reveal.structured-import/1' for source in direct)
            if not any(re.fullmatch(r'/segments/(?:0|[1-9][0-9]*)(?:/text)?\.?', path) or
                       (structured and re.match(r'/data(?:/|$)',path)) for path in locators):
                findings.append(finding('source-locator',node['id'],
                    'Uploaded evidence requires an exact extraction JSON pointer /segments/N or a structured /data locator.'))
        values, exact_values = [], []
        for locator in locators:
            matches = []
            for source, exact_source in zip(direct, exact_direct):
                # A dot may be a literal JSON key or sentence punctuation. Try
                # the exact key first, then remove punctuation only on failure.
                for candidate in dict.fromkeys((locator, locator.rstrip('.'))):
                    try:
                        matches.append(pointer(source, candidate))
                        exact_values.append(pointer(exact_source, candidate))
                        break
                    except ValueError:
                        pass
            if direct and not matches:
                findings.append(finding('source-locator', node['id'],
                    f'JSON pointer {locator!r} does not resolve in the cited source. Copy the exact source row pointer.'))
            values.extend(matches)
        located[node['id']] = (values, direct, bool(locators))
        exact_located[node['id']] = (exact_values, exact_direct, bool(locators))

    def rows_for(node):
        # A source Claim can be shared by many evidence branches. Visit each
        # node once per assessment, so a diamond-shaped provenance DAG does not
        # duplicate rows exponentially. Iteration also handles deep histories.
        pending, seen, rows = [node.get('id')], set(), []
        observations = exact_located if exact_observed is not None else located
        while pending:
            identity = pending.pop()
            if identity in seen or identity not in nodes:
                continue
            seen.add(identity)
            rows.extend(value for value in observations.get(identity, ([], [], False))[0] if isinstance(value, dict))
            current = nodes[identity]
            pending.extend(references(current, 'source_claims') + references(current, 'has_evidence'))
        return rows

    for claim in records(document, 'claims'):
        scores = [nodes[identity] for identity in references(claim, 'has_score') if identity in nodes]
        scores.extend(records(claim, 'scores'))
        if not scores:
            continue
        rows = rows_for(claim)
        for score in scores:
            metric = score.get('metric')
            value = _numeric(exact_nodes.get(score.get('id'), score).get('value'))
            if value is None or not isinstance(metric, str) or not any(metric in row and _numeric(row[metric]) == value for row in rows):
                findings.append(finding('source-metric', score.get('id', claim.get('id', 'claims')),
                    'ClaimScore does not match the exact metric at its cited source row'))
            expected = {'factor_value': 'LOADING', 'loading': 'LOADING', 'joint_loading': 'LOADING',
                        'marginal_loading': 'LOADING', 'beta': 'EFFECT_ESTIMATE',
                        'beta_uncorrected': 'EFFECT_ESTIMATE', 'combined': 'SCORE'}.get(metric) if isinstance(metric, str) else None
            if expected and score.get('score_kind') != expected:
                findings.append(finding('source-metric-kind', score.get('id', claim.get('id', 'claims')),
                    'Source metric was relabeled as a different mathematical quantity'))

    for item in records(document, 'evidence_items'):
        snippet = item.get('snippet')
        if not isinstance(snippet, str) or not snippet:
            continue
        values, direct, has_locator = (exact_located if exact_observed is not None else located).get(item.get('id'), ([], [], False))
        if not direct:
            continue
        candidates = values if has_locator else direct
        def norm(text):
            return re.sub(r'\s+', '', text)
        from .evidence_reader import exact_json
        serialized = lambda value: value if type(value) is str else exact_json(value) if exact_observed is not None else json.dumps(value, ensure_ascii=False)
        if not any(norm(snippet) in norm(serialized(value)) for value in candidates):
            findings.append(finding('evidence-snippet', item.get('id', 'evidence_items'),
                'Evidence snippet is not a verbatim excerpt of the cited source observation. Copy text from the exact captured row; put summaries and derived fields in explanation, not snippet.'))
    return findings


def validate_observations(document, observed):
    findings = observation_findings(document, observed)
    require(not findings, findings[0]['message'] if findings else '')


def source_findings(document, package, package_path, ledger_path=None, *, authored_exact=None):
    from .evidence_reader import parse
    observed, exact_observed = {}, {}
    root = Path(package_path).resolve().parent
    for artifact in package['source_artifacts'].values():
        path = (root / artifact['path']).resolve()
        require(path.is_relative_to(root), 'Source artifact path escape')
        data = path.read_bytes()
        require(sha256(data) == artifact['sha256'], 'Captured source checksum changed')
        if artifact.get('format') == 'json':
            observed[artifact['dapper_file_id']] = decode(data)
            exact_observed[artifact['dapper_file_id']] = parse(data)
    # The verified extraction represents the original document too; citing the
    # PDF/DOCX File directly must not bypass the exact segment/snippet checks.
    for upload in package.get('user_inputs',{}).get('uploads',[]):
        derived=observed[upload['extraction_file_id']]
        require(derived.get('original_sha256')==upload['sha256'],'Upload extraction lost its original checksum')
        observed[upload['original_file_id']]=derived
        exact_observed[upload['original_file_id']]=exact_observed[upload['extraction_file_id']]
    captured = ledger_sources(ledger_path)
    trusted = {node['id'] for group in package['dapper_context'] for node in records(package['dapper_context'], group) if 'id' in node}
    findings = new_file_findings(document, trusted, captured)
    for file in records(document, 'files'):
        if file.get('id') not in trusted and file.get('sha256') in captured:
            observed[file['id']] = decode(captured[file['sha256']]['bytes'])
            exact_observed[file['id']] = parse(captured[file['sha256']]['bytes'])
    return findings + observation_findings(document, observed, exact_observed=exact_observed, authored_exact=authored_exact)
