"""Source fidelity rules shared by agent lint and trusted worker acceptance."""
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from .evidence_package import decode, pointer, require, sha256

# The mathematical kind of each source metric a ClaimScore may restate; relabeling it is a source-fidelity error. PIGEAN
# gene and gene-set scores, EAGGL loadings, release gene-set betas (avg_postp a posterior probability) and factor-trait links.
METRIC_KINDS = {'factor_value': 'LOADING', 'loading': 'LOADING', 'joint_loading': 'LOADING', 'marginal_loading': 'LOADING',
                'beta': 'EFFECT_ESTIMATE', 'beta_uncorrected': 'EFFECT_ESTIMATE', 'avg_postp': 'POSTERIOR_PROBABILITY',
                'combined': 'SCORE', 'log_bf': 'SCORE', 'prior': 'SCORE', 'nnls_loading': 'LOADING', 'phewas_beta': 'EFFECT_ESTIMATE',
                'phewas_p_onesided': 'SCORE', 'phewas_q': 'SCORE'}


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


def finding(check, where, message, repair=None):
    # `repair` carries the cited source values to copy, so the author can fix the field in place.
    value = {'severity': 'error', 'check': check, 'where': where, 'message': message, 'why': ''}
    if repair: value['repair'] = repair
    return value


def ledger_sources(ledger_path):
    """Read completed response captures, including a live trusted lint snapshot.

    An account job's lint view of the ledger (written by REVEAL, never by the agent) also lists `source_files`: the
    files the agent saved under output/sources/ that source_evidence checked against its record and the Bash calls
    that wrote them. They count as captured sources by checksum and size. They carry no bytes here: they are papers,
    not tool responses, and nothing in this module reads them as JSON."""
    if ledger_path is None:
        return {}
    ledger_path = Path(ledger_path).resolve()
    ledger = decode(ledger_path.read_bytes())
    sources = {entry['sha256']: {'size_bytes': entry['size_bytes'], 'tool': 'source-file'} for entry in ledger.get('source_files', [])}
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
                cited = [_numeric(row[metric]) for row in rows if isinstance(metric, str) and metric in row]
                repair = {'metric': metric if isinstance(metric, str) else None, 'value': str(value) if value is not None else None}
                if cited: repair['source_values'] = [str(number) if number is not None else None for number in cited[:5]]
                elif rows: repair['source_metrics'] = sorted({key for row in rows for key, item in row.items() if _numeric(item) is not None})[:20]
                else: repair['note'] = 'No cited source row resolves for this Claim; fix its EvidenceItem locators first.'
                findings.append(finding('source-metric', score.get('id', claim.get('id', 'claims')),
                    'ClaimScore does not match the exact metric at its cited source row', repair))
            expected = METRIC_KINDS.get(metric) if isinstance(metric, str) else None
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
            excerpt = next((text for text in map(serialized, candidates) if text), '')
            findings.append(finding('evidence-snippet', item.get('id', 'evidence_items'),
                'Evidence snippet is not a verbatim excerpt of the cited source observation. Copy text from the exact captured row; put summaries and derived fields in explanation, not snippet.',
                {'source_excerpt': excerpt[:400], 'excerpt_truncated': len(excerpt) > 400} if excerpt else None))
    return findings


LOCATOR = re.compile(r'/(?:data|result|response|content|structuredContent|segments)(?:/[A-Za-z0-9_~.%-]+)*')
BINDING_CHECK = 'source-binding'


def _compact(text):
    return re.sub(r'\s+', '', text)


def _places(value, quote, path=''):
    """JSON Pointers into `value` for a quoted excerpt: (objects or arrays that are the quote, strings that hold it)."""
    from .evidence_reader import exact_json
    rows, texts = [], []
    pending = [(path, value)]
    while pending:
        where, node = pending.pop()
        if isinstance(node, str):
            if quote in _compact(node): texts.append(where)
            continue
        if isinstance(node, dict):
            if where and _compact(exact_json(node)) == quote: rows.append(where)
            pending.extend((where + '/' + str(key).replace('~', '~0').replace('/', '~1'), child) for key, child in node.items())
        elif isinstance(node, list):
            if where and _compact(exact_json(node)) == quote: rows.append(where)
            pending.extend((f'{where}/{index}', child) for index, child in enumerate(node))
    return sorted(rows, key=lambda item: (len(item), item)), sorted(texts, key=lambda item: (len(item), item))


def _named(value):
    """What a captured query was asked about: its string arguments (a factor, trait, gene or gene-set identifier)."""
    if isinstance(value, dict):
        for child in value.values(): yield from _named(child)
    elif isinstance(value, list):
        for child in value: yield from _named(child)
    elif isinstance(value, str) and len(value) >= 4:
        yield value


def bind_sources(document, package, package_path, ledger_path=None):
    """(the document with each quoted observation tied to the source that holds it, findings for those that cannot be).

    An author cites evidence the way a scientist would: the verbatim row or passage in `snippet`, and in `context`
    what was asked (the tool and the factor, trait, gene or gene set). REVEAL holds every source the run could have
    read, so it finds the one that contains the quote and attaches its own record: the source File in
    `was_derived_from` (and, for a tool response, the File node itself), and the exact place at the end of `context`.
    The linter and the gate then check that copy exactly as they check a document that names its Files.

    Nothing is guessed. A quote found in no eligible source, or in several that the words of the evidence do not
    tell apart, is reported with what to change. An EvidenceItem that already names a source File is left as written,
    and so are one that cites other Claims (`source_claims`) and one the package itself carries as trusted. The same function runs in draft lint and in
    trusted assembly, so they cannot disagree; binding an already bound document changes nothing."""
    from copy import deepcopy
    from .evidence_reader import parse
    from .scientific_account_lint import eligible_source_files
    items = document.get('evidence_items') if isinstance(document.get('evidence_items'), list) else []
    rows = records(document, 'files')
    present = {node.get('id'): node for node in rows if isinstance(node.get('id'), str)}
    trusted_files = {node['id']: node for node in records(package.get('dapper_context', {}), 'files') if isinstance(node.get('id'), str)}
    artifacts = {value['dapper_file_id']: value for value in package.get('source_artifacts', {}).values()
                 if isinstance(value, dict) and isinstance(value.get('dapper_file_id'), str)}

    def cites_source(item):
        for ref in references(item, 'was_derived_from'):
            if ref in artifacts or ref in trusted_files or ref in present:
                return True
        return False
    # Evidence that rests on other Claims quotes nothing of its own, and a trusted EvidenceItem (prior science the
    # package carries) is never rewritten: both are left exactly as they are, as they always were.
    trusted = {node['id'] for group in package.get('dapper_context', {}) for node in records(package['dapper_context'], group)
               if isinstance(node.get('id'), str)}
    waiting = [item for item in items if isinstance(item, dict) and item.get('id') not in trusted and not cites_source(item)
               and not references(item, 'source_claims')]
    if not waiting:
        return document, []
    eligible, captured = eligible_source_files(package, package_path, ledger_path)
    root = Path(package_path).resolve().parent
    sources = []   # (File id, parsed JSON, compact text, the File node to add when the draft must carry it, locator prefix)
    for identity in sorted(eligible):
        artifact = artifacts.get(identity)
        if not artifact or artifact.get('format') != 'json':
            continue
        path = (root / artifact['path']).resolve()
        require(path.is_relative_to(root), 'Source artifact path escape')
        data = path.read_bytes()
        require(sha256(data) == artifact['sha256'], 'Captured source checksum changed')
        sources.append((identity, parse(data), _compact(data.decode('utf-8', 'replace')), trusted_files.get(identity), ''))
    sequences = {}
    if ledger_path:
        for call in decode(Path(ledger_path).read_bytes())['calls']:
            if isinstance(call.get('response'), dict): sequences.setdefault(call['response'].get('sha256'), call.get('sequence'))
    for checksum, source in sorted(captured.items()):
        if 'bytes' not in source:
            continue   # an account job's saved paper: cited by its own File, never bound by quotation
        node = {'id': 'urn:reveal:tool-response:sha256:' + checksum, 'filename': checksum + '.json', 'mime_type': 'application/json',
                'sha256': checksum, 'size_in_bytes': source['size_bytes']}
        sources.append((node['id'], parse(source['bytes']), _compact(source['bytes'].decode('utf-8', 'replace')), node,
                        f'ledger_sequence={sequences.get(checksum)};pointer='))
    bound = deepcopy(document)
    bound_rows = bound.get('files') if isinstance(bound.get('files'), list) else []
    propositions = {node.get('id'): node for node in records(bound, 'propositions')}
    findings = []
    waiting_ids = {id(item) for item in waiting}
    for original, item in zip(items, bound.get('evidence_items', [])):
        if id(original) not in waiting_ids:
            continue
        where = item.get('id') if isinstance(item.get('id'), str) else 'evidence_items'
        snippet = item.get('snippet')
        if not isinstance(snippet, str) or len(_compact(snippet)) < 8:
            findings.append(finding(BINDING_CHECK, where, 'This EvidenceItem names no source REVEAL can find. Put the exact source row or '
                                    'passage in snippet, copied verbatim, and say in context what you asked (the tool and the factor, '
                                    'trait, gene or gene set)'))
            continue
        quote = _compact(snippet)
        # A quote copied out of a JSON string keeps its escapes in the stored bytes.
        escaped = _compact(json.dumps(snippet, ensure_ascii=False)[1:-1])
        found = [source for source in sources if quote in source[2] or escaped in source[2]]
        if not found:
            findings.append(finding(BINDING_CHECK, where, 'The snippet is not verbatim text of any source this run retrieved. Copy the '
                                    'row or passage exactly as the tool returned it; put summaries and derived values in explanation'))
            continue
        if len(found) > 1:
            proposition = propositions.get(item.get('target_proposition'), {})
            said = ' '.join(text for text in (item.get('context'), item.get('explanation'), item.get('evidence_source'),
                                              proposition.get('statement')) if isinstance(text, str))
            # An identifier is named only as a whole: Factor1 is not named by text about Factor10.
            named = [sum(1 for value in set(_named(source[1].get('arguments') if isinstance(source[1], dict) else None))
                         if re.search(r'(?<![A-Za-z0-9_])' + re.escape(value) + r'(?![A-Za-z0-9_])', said)) for source in found]
            best = max(named)
            found = [source for source, count in zip(found, named) if count == best and best > 0]
            if len(found) != 1:
                findings.append(finding(BINDING_CHECK, where, 'This snippet occurs in more than one result this run retrieved. Say in '
                                        'context exactly what you asked for the result you are quoting: the tool and the identifier of '
                                        'the factor, trait, gene or gene set, as you passed it'))
                continue
        identity, parsed, _, node, prefix = found[0]
        if node is not None and identity not in present and not any(isinstance(row, dict) and row.get('id') == identity for row in bound_rows):
            bound_rows.append(deepcopy(node))
        item['was_derived_from'] = [identity, *references(item, 'was_derived_from')]
        context = item.get('context')
        if isinstance(context, str) and context.strip() and not LOCATOR.search(context):
            whole, inside = _places(parsed, quote)
            place = (whole or inside or [None])[0]
            if place:
                context = context.rstrip()
                item['context'] = context + ('' if context.endswith(('.', '!', '?', ';', ':')) else '.') + f' Source row: {prefix}{place}'
    if bound_rows:
        bound['files'] = bound_rows
    return bound, findings


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
        if file.get('id') not in trusted and 'bytes' in captured.get(file.get('sha256'), {}):
            observed[file['id']] = decode(captured[file['sha256']]['bytes'])
            exact_observed[file['id']] = parse(captured[file['sha256']]['bytes'])
    return findings + observation_findings(document, observed, exact_observed=exact_observed, authored_exact=authored_exact)
