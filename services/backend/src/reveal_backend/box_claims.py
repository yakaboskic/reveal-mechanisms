"""Read access to the claims repository for an agent run, and the rule for citing what it holds.

The claims repository holds DAPPER objects: Propositions, the Claims made on them, their EvidenceItems, whole
ScientificAccounts that earlier runs and other groups deposited, and the claims its result collections state (PIGEAN
gene and gene-set associations, for one). Two tools read it:

  - query_claims looks up stored objects: by text, by what refers to an id, or one object in full;
  - run_collection_query runs one of a collection's query templates (the top genes for a trait, a gene across traits)
    and returns each row's numbers with the id of the Claim that states them. The repository mints a collection's
    claim the first time it serves it, so running a query is also how such a claim comes to exist.

Reads are public and need no key. The deposit key stays in the backend (claims_deposit) and is never in the sandbox.

A Claim the repository holds can be used as evidence. An account does not copy it: an EvidenceItem names it by id in
`source_claims`, and REVEAL checks that the repository resolves every such id to a Claim (`verify_references`) while
the agent drafts (box_remote.lint_tool) and again at the gate (agent_account.accept). That is the only place an
account may name an object it does not contain.

Standard library only: this module is bundled into the Box.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

TOOL = 'query_claims'
QUERY_TOOL = 'run_collection_query'
TOOLS = (TOOL, QUERY_TOOL)
CLASSES = ('Claim', 'Proposition', 'EvidenceItem', 'ScientificAccount', 'KnowledgeGap', 'Publication', 'ValueLocator', 'File',
           'Activity', 'Person', 'Organization', 'GeneSet')
MAX_RESULTS = 25
MAX_RESPONSE_BYTES = 2_000_000
MAX_OBJECT_CHARACTERS = 40_000
DAPPER_ID = re.compile(r'dapper:[A-Za-z][A-Za-z0-9]*\.[A-Za-z0-9_-]{8,64}')
CLAIM_ID = re.compile(r'dapper:Claim\.[A-Za-z0-9_-]{8,64}')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')   # a collection, query, dimension or claim key
# Serving a row asserts its claims in the repository, so one call is kept small: a few claim keys, bounded limits,
# and a compact answer.
MAX_CLAIM_KEYS = 3
MAX_INTEGER_VARIABLE = 50
MAX_VARIABLES = 20
MAX_ROWS = 40
MAX_JOINED = 5
# A run's raw answer carries whole DAPPER documents (about 10 to 15 kB a row against the repository's own fixture), and
# by the time it arrives the repository has recorded the claims: it is read whole and never refused for its size.
# What the agent sees is the compact form, cut to this many characters by showing fewer rows.
MAX_RUN_BYTES = 32_000_000
MAX_RUN_CHARACTERS = 30_000
MAX_BATCH = 500                 # the repository's objects:batchGet limit
MAX_REFERENCES = 200            # repository claims one account may cite
SOURCE_CLAIMS = 'source_claims'

DESCRIPTION = ('Look up objects the claims repository holds: Propositions, Claims, EvidenceItems, ScientificAccounts, GeneSets. Three '
               'ways to ask. (1) q: a word search, best with a class_name (search Propositions for what has been stated, Claims '
               'for what has been assessed). Every word of q must begin a word of the text, in any order: "NOD2 bowel" finds '
               'statements about NOD2 and inflammatory bowel disease. fields limits where to look (statement, name, description; '
               f'a Proposition is searched in its statement, a GeneSet in its name). Up to {MAX_RESULTS} results, continue with '
               'offset. (2) references: a dapper id, to list the objects that point at it (the Claims made on a Proposition, the '
               'EvidenceItems that target it); add class_name to keep one class. (3) id: fetch one object in full. For the '
               'numbers a result collection such as PIGEAN holds on a gene or trait, use run_collection_query instead: it '
               'returns the Claims with their scores. Use this tool to see whether a proposition you are about to write already '
               'exists (copy its statement exactly and your claim is about the same Proposition) and what has been claimed on '
               'it. A repository record is an earlier claim '
               'by somebody else, never a finding of yours: to use a Claim as evidence, name its id in an EvidenceItem\'s '
               'source_claims and say in explanation how it bears on your proposition. Absence here says nothing about the science.')

QUERY_DESCRIPTION = ('Run a query template of one of the claims repository\'s result collections (for example PIGEAN: the top genes '
                     'for a trait, the traits a gene is associated with) and get each row\'s numbers with the id of the Claim that '
                     'states them. Three ways to call it. (1) No arguments, or collection alone: the queries there are, each with '
                     'its params and the claim keys its rows carry. (2) collection and dimension, optionally search: the values a '
                     'dimension param takes (the id to give for a trait, say). (3) collection, query, variables and claims: run it. '
                     f'claims names 1 to {MAX_CLAIM_KEYS} of the query\'s claim keys; ask only for those you will use, and keep '
                     f'limits small (integer variables up to {MAX_INTEGER_VARIABLE}; the answer shows at most {MAX_ROWS} rows and '
                     f'{MAX_JOINED} joined rows each, fewer when they are long). '
                     'A Claim id returned here can be evidence: put it in an EvidenceItem\'s source_claims, with '
                     'target_proposition, direction, context (the collection, query and variables you ran) and explanation (what '
                     'the numbers say about your proposition). Do not copy the claim into your account, and never write a claim '
                     'id you did not get from this tool or query_claims.')

QUERY_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'collection': {'type': 'string', 'description': 'A collection key, for example pigean.'},
    'query': {'type': 'string', 'description': 'A query template of that collection.'},
    'variables': {'type': 'object', 'description': "The query's params, as its listing gives them."},
    'claims': {'type': 'array', 'minItems': 1, 'maxItems': MAX_CLAIM_KEYS, 'items': {'type': 'string'},
               'description': 'Claim keys to return with each row.'},
    'dimension': {'type': 'string', 'description': 'A dimension of the collection, to list its values.'},
    'search': {'type': 'string', 'minLength': 2, 'maxLength': 100},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': MAX_RESULTS}}}

SEARCH_FIELDS = ('statement', 'name', 'description')
SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'q': {'type': 'string', 'minLength': 2, 'maxLength': 200, 'description': 'Words to find; each must begin a word of the text.'},
    'class_name': {'type': 'string', 'enum': list(CLASSES), 'description': 'Search or list one class, for example Proposition or Claim.'},
    'fields': {'type': 'array', 'minItems': 1, 'maxItems': len(SEARCH_FIELDS), 'items': {'type': 'string', 'enum': list(SEARCH_FIELDS)},
               'description': 'With q: the text fields to search. Default all three.'},
    'references': {'type': 'string', 'description': 'A dapper id; returns the objects that refer to it.'},
    'id': {'type': 'string', 'description': 'A dapper id; returns that object.'},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': MAX_RESULTS},
    'offset': {'type': 'integer', 'minimum': 0, 'maximum': 10_000}}}


class ClaimsInputError(ValueError):
    """A query the repository cannot be asked as written. The message says what to change."""


class ClaimsUnavailable(ClaimsInputError):
    """The repository could not be reached, or failed, so nothing is known: the same call may succeed later."""


def base_url(value):
    """The repository address a sandbox may be given, normalised, or None: https, a host name, no credentials."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urllib.parse.urlsplit(value.strip().rstrip('/'))
        port = parsed.port
    except ValueError:   # unbalanced brackets, a port that is not a number
        return None
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if not re.fullmatch(r'[a-z0-9]([a-z0-9.-]*[a-z0-9])?', parsed.hostname) or '.' not in parsed.hostname \
            or re.fullmatch(r'[0-9.]+', parsed.hostname):   # a name, never an IP address
        return None
    port = f':{port}' if port and port != 443 else ''
    return f'https://{parsed.hostname}{port}{parsed.path.rstrip("/")}'


def host(url):
    return urllib.parse.urlsplit(url).hostname


def request_path(arguments):
    """The repository path and query for one call. Raises ClaimsInputError."""
    if not isinstance(arguments, dict) or set(arguments) - set(SCHEMA['properties']):
        raise ClaimsInputError('query_claims takes q, class_name, fields, references, id, limit and offset')
    for key in ('id', 'references'):
        if key in arguments and (not isinstance(arguments[key], str) or not DAPPER_ID.fullmatch(arguments[key])):
            raise ClaimsInputError(f'{key} must be a dapper id such as dapper:Proposition.<digest>')
    if 'id' in arguments:
        if set(arguments) != {'id'}:
            raise ClaimsInputError('Fetch one object with id alone')
        return '/v1/objects/' + urllib.parse.quote(arguments['id'], safe=':'), None
    query = {}
    if 'q' in arguments:
        if not isinstance(arguments['q'], str) or not 2 <= len(arguments['q'].strip()) <= 200:
            raise ClaimsInputError('q must be 2 to 200 characters of text')
        query['q'] = arguments['q'].strip()
    if 'fields' in arguments:
        fields = arguments['fields']
        if 'q' not in arguments or not isinstance(fields, list) or not fields or len(set(map(str, fields))) != len(fields) \
                or not all(field in SEARCH_FIELDS for field in fields):
            raise ClaimsInputError('fields goes with q and names one or more of ' + ', '.join(SEARCH_FIELDS))
        query['fields'] = ','.join(field for field in SEARCH_FIELDS if field in fields)
    if 'references' in arguments:
        query['references'] = arguments['references']
    if 'class_name' in arguments:
        if arguments['class_name'] not in CLASSES:
            raise ClaimsInputError('class_name must be one of ' + ', '.join(CLASSES))
        query['class_name'] = arguments['class_name']
    if not query:
        raise ClaimsInputError('Give q (text to search for), references (a dapper id) or id (a dapper id)')
    limit, offset = arguments.get('limit', 10), arguments.get('offset', 0)
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS or type(offset) is not int or not 0 <= offset <= 10_000:
        raise ClaimsInputError(f'limit must be 1 to {MAX_RESULTS} and offset 0 to 10000')
    query.update(limit=limit, offset=offset)
    return '/v1/objects/search', query


def _name(arguments, key):
    value = arguments.get(key)
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ClaimsInputError(f'{key} must be a name as the listing gives it (letters, digits, dot, dash and underscore)')
    return value


def collection_request(arguments):
    """(mode, method, path, query, body) for one run_collection_query call. mode is 'queries', 'values' or 'run'.
    Raises ClaimsInputError."""
    if not isinstance(arguments, dict) or set(arguments) - set(QUERY_SCHEMA['properties']):
        raise ClaimsInputError('run_collection_query takes collection, query, variables, claims, dimension, search and limit')
    if 'query' in arguments:
        if set(arguments) - {'collection', 'query', 'variables', 'claims'}:
            raise ClaimsInputError('Run a query with collection, query, variables and claims only')
        collection, query = _name(arguments, 'collection'), _name(arguments, 'query')
        keys = arguments.get('claims')
        if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_CLAIM_KEYS or len(set(map(str, keys))) != len(keys) \
                or not all(isinstance(key, str) and NAME.fullmatch(key) for key in keys):
            raise ClaimsInputError(f"claims must name 1 to {MAX_CLAIM_KEYS} of the query's claim keys (its listing gives them)")
        variables = arguments.get('variables', {})
        if not isinstance(variables, dict) or len(variables) > MAX_VARIABLES:
            raise ClaimsInputError(f'variables must be an object of at most {MAX_VARIABLES} params')
        for name, value in variables.items():
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise ClaimsInputError('A variable name is a name as the listing gives it')
            if isinstance(value, bool) or value is None or isinstance(value, float) and value == value and abs(value) != float('inf'):
                continue
            if isinstance(value, int):
                if abs(value) > MAX_INTEGER_VARIABLE:
                    raise ClaimsInputError(f'{name} is {value}: ask for at most {MAX_INTEGER_VARIABLE} at a time and run the query again '
                                           'for more')
            elif not isinstance(value, str) or len(value) > 300:
                raise ClaimsInputError(f'{name} must be a number, true or false, or text of at most 300 characters')
        return 'run', 'POST', f'/v1/collections/{collection}/queries/{query}:run', None, {'variables': variables, 'claims': keys}
    if 'dimension' in arguments:
        if set(arguments) - {'collection', 'dimension', 'search', 'limit'}:
            raise ClaimsInputError('List a dimension with collection, dimension, search and limit only')
        collection, dimension = _name(arguments, 'collection'), _name(arguments, 'dimension')
        limit = arguments.get('limit', 10)
        if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
            raise ClaimsInputError(f'limit must be 1 to {MAX_RESULTS}')
        query = {'first': limit}
        if 'search' in arguments:
            if not isinstance(arguments['search'], str) or not 2 <= len(arguments['search'].strip()) <= 100:
                raise ClaimsInputError('search must be 2 to 100 characters of text')
            query['search'] = arguments['search'].strip()
        return 'values', 'GET', f'/v1/collections/{collection}/dimensions/{dimension}', query, None
    if set(arguments) - {'collection'}:
        raise ClaimsInputError('Give query (to run one), dimension (to list its values), or nothing but collection (to list the queries)')
    return 'queries', 'GET', '/v1/queries', ({'collection': _name(arguments, 'collection')} if 'collection' in arguments else None), None


def _text(value, limit=240):
    return value if not isinstance(value, str) or len(value) <= limit else value[:limit - 1] + '…'


def _listing(body):
    """The queries listing, kept to what an agent needs to call one."""
    entries = []
    for entry in body.get('queries', []) if isinstance(body.get('queries'), list) else []:
        if not isinstance(entry, dict):
            continue
        params = {}
        for name, param in (entry.get('params') or {}).items():
            if isinstance(param, dict):
                shown = {key: _text(param[key]) for key in ('type', 'required', 'default', 'min', 'max', 'description', 'dimension') if key in param}
                sample = (param.get('values') or {}).get('sample') if isinstance(param.get('values'), dict) else None
                if isinstance(sample, list):
                    shown['examples'] = [item.get('value') for item in sample[:5] if isinstance(item, dict)]
                params[name] = shown
        returns = entry.get('returns') if isinstance(entry.get('returns'), dict) else {}
        rows = returns.get('rows') if isinstance(returns.get('rows'), dict) else {}
        described = rows.get('claims') if isinstance(rows.get('claims'), dict) else {}
        keys = entry.get('claims') if isinstance(entry.get('claims'), list) else list(described)
        entries.append({'collection': entry.get('collection'), 'query': entry.get('name'), 'description': _text(entry.get('description'), 400),
                        'params': params,
                        'claims': {key: _text((described.get(key) or {}).get('description'), 200) if isinstance(described.get(key), dict) else None
                                   for key in keys if isinstance(key, str)},
                        'metrics': {name: _text(metric.get('description'), 200) for name, metric in (rows.get('metrics') or {}).items()
                                    if isinstance(metric, dict)}})
    return {'count': len(entries), 'queries': entries}


def _claims(envelopes):
    """{claim key: {id, statement, triple, scores}} of a row's claim envelopes: enough to cite one and say what it states."""
    shown = {}
    for key, envelope in (envelopes.items() if isinstance(envelopes, dict) else ()):
        if not isinstance(envelope, dict) or not isinstance(envelope.get('id'), str):
            continue
        proposition = envelope.get('proposition') if isinstance(envelope.get('proposition'), dict) else {}
        claim = envelope.get('claim') if isinstance(envelope.get('claim'), dict) else {}
        scores = [{name: score.get(name) for name in ('metric', 'value', 'scoreKind') if name in score}
                  for score in envelope.get('scores', []) if isinstance(score, dict)] if isinstance(envelope.get('scores'), list) else []
        shown[key] = {'id': envelope['id'], 'statement': _text(proposition.get('statement') or claim.get('statement'), 300),
                      'triple': envelope.get('triple') if isinstance(envelope.get('triple'), dict) else None,
                      **({'direction': claim['direction']} if isinstance(claim.get('direction'), str) else {}),
                      **({'scores': scores[:8]} if scores else {})}
    return shown


def _row(row):
    """One result row without the documents it carries: who it is about, its numbers, and its claims by id."""
    if not isinstance(row, dict):
        return None
    def end(value):
        return {key: _text(value[key], 120) for key in ('id', 'value', 'label') if key in value} if isinstance(value, dict) else None
    shown = {'kind': row.get('kind'), 'subject': end(row.get('subject')), 'object': end(row.get('object')),
             'metrics': row.get('metrics') if isinstance(row.get('metrics'), dict) else {}}
    dataset = row.get('dataset') if isinstance(row.get('dataset'), dict) else {}
    if isinstance(dataset.get('dimensions'), dict):
        shown['dimensions'] = dataset['dimensions']
    claims = _claims(row.get('claims'))
    if claims:
        shown['claims'] = claims
    for name, value in row.items():   # joined rows: a list of {edge, row} under the join's name
        if isinstance(value, list) and value and all(isinstance(item, dict) and 'row' in item for item in value):
            joined = []
            for item in value[:MAX_JOINED]:
                inner = _row(item.get('row'))
                edge = item.get('edge') if isinstance(item.get('edge'), dict) else {}
                linked = _claims(edge.get('claims'))
                if inner is not None:
                    joined.append({**inner, **({'edge_claims': linked} if linked else {})})
            shown[name] = joined
            if len(value) > MAX_JOINED:
                shown[name + '_more'] = len(value) - MAX_JOINED
    return shown


def _result(body):
    rows = [shown for shown in map(_row, body.get('rows', []) if isinstance(body.get('rows'), list) else []) if shown is not None]
    data = {'collection': body.get('collection'), 'query': body.get('query'), 'release': body.get('release'),
            'variables': body.get('variables'), 'count': len(rows), 'rows': rows[:MAX_ROWS]}
    # The run has happened: show as many rows, in order, as fit, and say how many are left out.
    while len(data['rows']) > 1 and len(json.dumps(data, ensure_ascii=False)) > MAX_RUN_CHARACTERS:
        data['rows'] = data['rows'][:len(data['rows']) * 3 // 4]
    if len(rows) > len(data['rows']):
        data['more_rows'] = len(rows) - len(data['rows'])
        data['note'] = (f"Only the first {len(data['rows'])} rows are shown. Run the query again with lower limits, or with a filter, "
                        'to see the rest in order.')
    if isinstance(body.get('warnings'), list) and body['warnings']:
        data['warnings'] = [_text(item, 300) for item in body['warnings'][:10] if isinstance(item, str)]
    return data


def repository_references(document):
    """({claim id: [where]}, {claim id: [where]}) for an account draft: the Claims it names in an EvidenceItem's
    source_claims without containing them, and where it names any of those ids anywhere else (which it may not). A
    Claim of the draft's own, named in source_claims, is an ordinary reference and is left to the linter."""
    groups = {key: rows for key, rows in document.items() if isinstance(rows, list)} if isinstance(document, dict) else {}
    present = {node['id'] for rows in groups.values() for node in rows if isinstance(node, dict) and isinstance(node.get('id'), str)}
    cited, misplaced = {}, {}
    for index, item in enumerate(groups.get('evidence_items', [])):
        if not isinstance(item, dict) or not isinstance(item.get(SOURCE_CLAIMS), list):
            continue
        where = item['id'] if isinstance(item.get('id'), str) else f'evidence_items[{index}]'
        for reference in item[SOURCE_CLAIMS]:
            if isinstance(reference, str) and reference not in present and CLAIM_ID.fullmatch(reference):
                cited.setdefault(reference, []).append(where)

    def elsewhere(value, where, allowed):
        if isinstance(value, dict):
            for key, child in value.items():
                elsewhere(child, f'{where}.{key}', allowed and key == SOURCE_CLAIMS)
        elif isinstance(value, list):
            for child in value:
                elsewhere(child, where, allowed)
        elif isinstance(value, str) and value in cited and not allowed:
            misplaced.setdefault(value, []).append(where)
    for group, rows in groups.items():
        for index, node in enumerate(rows):
            if isinstance(node, dict):
                name = node['id'] if isinstance(node.get('id'), str) else f'{group}[{index}]'
                for key, child in node.items():
                    if key != 'id':
                        elsewhere(child, f'{name}.{key}', group == 'evidence_items' and key == SOURCE_CLAIMS)
    return cited, misplaced


def verify_references(document, resolve, unavailable=None):
    """([(where, what to change)], sorted ids REVEAL may treat as resolved) for an account draft.

    `resolve(ids)` returns {id: class name} for the ids the repository holds (ClaimsClient.resolve), or raises
    ClaimsInputError when it cannot be asked; with resolve None there is no repository to ask. An id counts only when
    the repository holds it as a Claim. This is the one rule for an object an account names without containing, used
    while the agent drafts and at the gate, so they cannot disagree.

    A repository that cannot be reached (ClaimsUnavailable) verifies nothing. While the agent drafts that is a finding
    it can retry. The gate passes `unavailable`, an exception class raised instead, so the workflow retries the step:
    a finished run is not refused because the repository was down for a moment."""
    cited, misplaced = repository_references(document)
    if not cited:
        return [], []
    problems = [(wheres[0], f'{reference} is a claim in the claims repository, not an object of this account. Name it only in an '
                 "EvidenceItem's source_claims") for reference, wheres in sorted(misplaced.items())]
    if len(cited) > MAX_REFERENCES:
        problems.append(('evidence_items', f'This account cites {len(cited)} repository claims; the most is {MAX_REFERENCES}. Keep those '
                         'that bear on its claims'))
    cited = {reference: wheres for reference, wheres in cited.items() if reference not in misplaced}
    if resolve is None:
        return problems + [(wheres[0], f'{reference} is not an object of this account, and this run has no claims repository to '
                            'check it against. Remove it from source_claims') for reference, wheres in sorted(cited.items())], []
    try:
        held = resolve(sorted(cited))
    except ClaimsUnavailable as error:
        if unavailable is not None:
            raise unavailable(str(error)) from None
        return problems + [(wheres[0], f'{reference} could not be checked: {error}. Run lint_account again') for reference, wheres in sorted(cited.items())], []
    except ClaimsInputError as error:
        return problems + [(wheres[0], f'{reference} could not be checked: {error}. Run lint_account again') for reference, wheres in sorted(cited.items())], []
    verified = []
    for reference, wheres in sorted(cited.items()):
        if held.get(reference) == 'Claim':
            verified.append(reference)
        elif reference in held:
            problems.append((wheres[0], f'{reference} is a {held[reference]} in the claims repository, not a Claim. source_claims names Claims'))
        else:
            problems.append((wheres[0], f'The claims repository holds no claim {reference}. Use an id exactly as run_collection_query or '
                             'query_claims returned it, or remove it'))
    return problems, verified


class ClaimsClient:
    server_info = {'name': 'claims-repository'}

    def __init__(self, url, timeout=20, opener=None, checked=False):
        # checked: the caller is the backend, whose own rule already accepted this address (claims_deposit.address,
        # which also allows a repository on this machine). A sandbox's address always passes base_url.
        self.url = url.rstrip('/') if checked and isinstance(url, str) and url else base_url(url)
        if self.url is None:
            raise ValueError('Invalid claims repository address')
        self.timeout, self.opener = timeout, opener or urllib.request.build_opener()

    def _open(self, method, path, query=None, body=None, limit=MAX_RESPONSE_BYTES):
        """The decoded JSON answer of one request. Raises ClaimsInputError for a refusal, an oversized or malformed answer
        and a repository that cannot be reached; urllib.error.HTTPError 404 is left to the caller."""
        address = self.url + urllib.parse.quote(path, safe='/:') + ('?' + urllib.parse.urlencode(query) if query else '')
        headers = {'Accept': 'application/json', 'User-Agent': 'reveal-mechanisms-agent'}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode(); headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(address, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise
            detail = None
            try:
                detail = json.loads(error.read(65536)).get('detail')
            except (ValueError, AttributeError, OSError):
                pass
            if error.code >= 500 or error.code == 429:
                raise ClaimsUnavailable(f'The claims repository is unavailable (HTTP {error.code})') from None
            reason = f': {detail[:600]}' if isinstance(detail, str) and detail else ''
            raise ClaimsInputError(f'The claims repository refused the query (HTTP {error.code}){reason}') from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            if isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError):
                raise ClaimsUnavailable(f'The claims repository did not answer within {self.timeout:g} seconds; nothing is known, '
                                        'which is not the same as nothing found') from None
            raise ClaimsUnavailable('The claims repository could not be reached') from None
        if len(raw) > limit:
            raise ClaimsInputError('The claims repository returned too much; narrow the query or lower its limits')
        try:
            answer = json.loads(raw)
        except ValueError:
            raise ClaimsInputError('The claims repository did not answer with JSON') from None
        if not isinstance(answer, dict):
            raise ClaimsInputError('The claims repository did not answer with an object')
        return answer

    def resolve(self, ids):
        """{id: class name} of the ids the repository holds, from POST /v1/objects:batchGet: a plain read that mints
        nothing. Raises ClaimsInputError when the repository cannot be asked."""
        ids = [identity for identity in dict.fromkeys(ids) if isinstance(identity, str) and DAPPER_ID.fullmatch(identity)]
        held = {}
        for start in range(0, len(ids), MAX_BATCH):
            try:
                answer = self._open('POST', '/v1/objects:batchGet', body={'ids': ids[start:start + MAX_BATCH]})
            except urllib.error.HTTPError:
                raise ClaimsInputError('The claims repository has no batch lookup') from None
            objects = answer.get('objects')
            if not isinstance(objects, dict):
                raise ClaimsInputError('The claims repository did not answer the lookup')
            for identity, document in objects.items():
                if identity in ids and isinstance(document, dict):
                    # The class the repository presents the object with; its id names the same class.
                    held[identity] = document.get('class_name') if isinstance(document.get('class_name'), str) else identity[7:].split('.', 1)[0]
        return held

    def _collection(self, arguments):
        mode, method, path, query, body = collection_request(arguments)
        try:
            answer = self._open(method, path, query, body, MAX_RUN_BYTES if mode == 'run' else MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError:
            raise ClaimsInputError('The claims repository has no such collection, query or dimension; list the queries first') from None
        if mode == 'queries':
            data = _listing(answer)
        elif mode == 'values':
            values = [{key: _text(item[key], 160) for key in ('value', 'label', 'datasets', 'concept_id', 'concept_label', 'category') if key in item}
                      for item in answer.get('values', []) if isinstance(item, dict)] if isinstance(answer.get('values'), list) else []
            data = {'collection': answer.get('collection'), 'dimension': answer.get('dimension'), 'count': answer.get('count'), 'values': values}
        else:
            data = _result(answer)
        return {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}],
                'structuredContent': {'source': 'claims-repository', 'data': data}}

    def call(self, tool, arguments):
        if tool == QUERY_TOOL:
            return self._collection(arguments)
        if tool != TOOL:
            raise ClaimsInputError('Unknown claims repository tool')
        path, query = request_path(arguments)
        try:
            body = self._open('GET', path, query)
        except urllib.error.HTTPError:
            if query is None:
                data = {'found': False, 'id': arguments['id'], 'count': 0}
                return {'content': [{'type': 'text', 'text': json.dumps(data)}], 'structuredContent': {'source': 'claims-repository', 'data': data}}
            raise ClaimsInputError('The claims repository refused the query (HTTP 404); nothing was returned') from None
        except ClaimsUnavailable as error:
            if query is not None and 'q' in query:
                raise ClaimsUnavailable(f'{error}. Narrow the search: name a class_name, add a more specific word, or limit fields. For '
                                        'what a result collection such as PIGEAN holds on a gene or trait, use run_collection_query') from None
            raise
        if query is None:
            text = json.dumps(body, ensure_ascii=False)
            data = {'found': True, 'count': 1, 'object': body} if len(text) <= MAX_OBJECT_CHARACTERS else {
                'found': True, 'count': 1, 'truncated': True, 'object': {key: body[key] for key in ('id', 'class_name') if key in body},
                'note': f'This object is larger than {MAX_OBJECT_CHARACTERS:,} characters; fetch the objects it refers to one at a time.'}
        else:
            objects = [{key: item.get(key) for key in ('id', 'class_name', 'label')} for item in body.get('objects', []) if isinstance(item, dict)]
            data = {'count': len(objects), 'total': body.get('total'), 'offset': query['offset'], 'objects': objects}
            if isinstance(data['total'], int) and query['offset'] + len(objects) < data['total']:
                data['next_offset'] = query['offset'] + len(objects)
        return {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}],
                'structuredContent': {'source': 'claims-repository', 'data': data}}
