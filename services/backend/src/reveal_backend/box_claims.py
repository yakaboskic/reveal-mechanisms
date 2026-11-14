"""Read access to the claims repository for an agent run.

The claims repository holds DAPPER objects that earlier runs and other groups deposited: Propositions, the Claims made
on them, their EvidenceItems, whole ScientificAccounts. The query_claims tool lets an agent see what is already there
before it writes its own: whether its proposition exists, what has been claimed about it, on what evidence.

Reads are public and need no key. The deposit key stays in the backend (claims_deposit) and is never in the sandbox.

What comes back is somebody's earlier claim, not a source. source_validation accepts only captured graph queries and
paper reads as evidence, so nothing returned here can be cited in an account, and the tool says so.

Standard library only: this module is bundled into the Box.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

TOOL = 'query_claims'
CLASSES = ('Claim', 'Proposition', 'EvidenceItem', 'ScientificAccount', 'KnowledgeGap', 'Publication', 'ValueLocator', 'File',
           'Activity', 'Person', 'Organization', 'GeneSet')
MAX_RESULTS = 25
MAX_RESPONSE_BYTES = 2_000_000
MAX_OBJECT_CHARACTERS = 40_000
DAPPER_ID = re.compile(r'dapper:[A-Za-z][A-Za-z0-9]*\.[A-Za-z0-9_-]{8,64}')

DESCRIPTION = ('Look up what the claims repository already holds: Propositions, Claims, EvidenceItems and ScientificAccounts deposited by '
               'earlier runs and other groups. Three ways to ask. (1) q: text to find in statements, names and descriptions, optionally '
               f'limited to one class_name; up to {MAX_RESULTS} results, continue with offset. (2) references: a dapper id, to list the '
               'objects that point at it (the Claims made on a Proposition, the EvidenceItems that target it). (3) id: fetch one object '
               'in full. Use it to see whether a proposition you are about to write already exists (copy its statement exactly and your '
               'claim is about the same Proposition) and what has been claimed on it. A repository record is an earlier claim, not '
               'evidence: nothing returned here may be cited as a source, and absence here says nothing about the science.')

SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'q': {'type': 'string', 'minLength': 2, 'maxLength': 200},
    'class_name': {'type': 'string', 'enum': list(CLASSES)},
    'references': {'type': 'string', 'description': 'A dapper id; returns the objects that refer to it.'},
    'id': {'type': 'string', 'description': 'A dapper id; returns that object.'},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': MAX_RESULTS},
    'offset': {'type': 'integer', 'minimum': 0, 'maximum': 10_000}}}


class ClaimsInputError(ValueError):
    """A query the repository cannot be asked as written. The message says what to change."""


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
        raise ClaimsInputError('query_claims takes q, class_name, references, id, limit and offset')
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


class ClaimsClient:
    server_info = {'name': 'claims-repository'}

    def __init__(self, url, timeout=20, opener=None):
        self.url = base_url(url)
        if self.url is None:
            raise ValueError('Invalid claims repository address')
        self.timeout, self.opener = timeout, opener or urllib.request.build_opener()

    def call(self, tool, arguments):
        if tool != TOOL:
            raise ClaimsInputError('Unknown claims repository tool')
        path, query = request_path(arguments)
        address = self.url + path + ('?' + urllib.parse.urlencode(query) if query else '')
        request = urllib.request.Request(address, headers={'Accept': 'application/json', 'User-Agent': 'reveal-mechanisms-agent'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code == 404 and query is None:
                data = {'found': False, 'id': arguments['id'], 'count': 0}
                return {'content': [{'type': 'text', 'text': json.dumps(data)}], 'structuredContent': {'source': 'claims-repository', 'data': data}}
            raise ClaimsInputError(f'The claims repository refused the query (HTTP {error.code}); nothing was returned') from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ClaimsInputError('The claims repository returned too much; narrow the query or lower limit')
        body = json.loads(raw)
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
