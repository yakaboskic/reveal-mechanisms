"""Bounded Europe PMC reads; fixed official HTTPS origin, no credentials/redirects.

API contract: https://europepmc.org/RestfulWebService
Raw HTTP bytes are returned only to the trusted capture bridge. Search records
are discovery; only an inspected paper excerpt can become auxiliary evidence.
"""
import hashlib
import json
import re
import ssl
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from xml.etree import ElementTree

BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
MAX_RESPONSE_BYTES = 2_000_000


class LiteratureInputError(ValueError): pass


def request_spec(tool, arguments):
    def require(condition, message):
        if not condition: raise LiteratureInputError(message)
    require(isinstance(arguments, dict), 'Literature arguments must be an object')
    if tool == 'search_papers':
        require(not set(arguments) - {'query', 'limit'}, 'Unexpected paper search fields')
        query, limit = arguments.get('query'), arguments.get('limit', 5)
        require(isinstance(query, str) and 2 <= len(query.strip()) <= 300 and not any(ord(c) < 32 for c in query), 'Search query must contain 2–300 printable characters')
        require(type(limit) is int and 1 <= limit <= 5, 'Paper search limit must be 1–5')
        params = {'query': query, 'format': 'json', 'resultType': 'lite', 'pageSize': limit}
        return {'url': BASE + '/search?' + urlencode(params), 'scope': 'discovery', 'format': 'json'}
    require(tool == 'read_paper', 'Unsupported literature tool')
    require(not set(arguments) - {'source', 'id', 'section', 'offset', 'limit'}, 'Unexpected paper read fields')
    source, identity = arguments.get('source'), arguments.get('id')
    require(source in ('MED', 'PMC') and isinstance(identity, str), 'Choose source MED or PMC and its exact returned ID')
    require(bool(re.fullmatch(r'[1-9][0-9]{0,11}' if source == 'MED' else r'PMC[1-9][0-9]{0,11}', identity)), 'Invalid exact paper ID')
    section = arguments.get('section', 'abstract')
    require(section in ('abstract', 'full_text'), 'Choose abstract or full_text')
    offset, limit = arguments.get('offset', 0), arguments.get('limit', 8000)
    require(type(offset) is int and 0 <= offset <= 2_000_000 and type(limit) is int and 1 <= limit <= 12000, 'Invalid bounded paper text window')
    if section == 'full_text':
        require(source == 'PMC', 'Open-access full text requires the exact PMC identifier from a paper record')
        return {'url': BASE + '/' + identity + '/fullTextXML', 'scope': section, 'format': 'xml'}
    params = {'query': f'EXT_ID:{identity} AND SRC:{source}', 'format': 'json', 'resultType': 'core', 'pageSize': 1}
    return {'url': BASE + '/search?' + urlencode(params), 'scope': section, 'format': 'json'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise HTTPError(request.full_url, code, 'Literature redirects are unavailable', headers, fp)


class LiteratureClient:
    server_info = {'name': 'Europe PMC REST', 'contract': 'https://europepmc.org/RestfulWebService'}
    def __init__(self, timeout=20, opener=None):
        self.timeout = timeout
        self.opener = opener or build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def call(self, tool, arguments):
        spec = request_spec(tool, arguments)
        result = {'provider': 'Europe PMC', 'scope': spec['scope'], 'url': spec['url'],
                  'retrieved_at': datetime.now(timezone.utc).isoformat(), 'data': {}}
        raw = b''; is_error = False
        try:
            try:
                response = self.opener.open(Request(spec['url'], headers={'Accept': 'application/json' if spec['format'] == 'json' else 'application/xml',
                    'User-Agent': 'REVEAL-LiteratureReader/1'}), timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                result['http_status'] = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            result['upstream_sha256'] = hashlib.sha256(raw).hexdigest()
            result['upstream_capture_complete'] = len(raw) <= MAX_RESPONSE_BYTES
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError('Literature response exceeded byte limit')
            if result['http_status'] != 200:
                raise ValueError('Literature source unavailable (HTTP ' + str(result['http_status']) + ')')
            if spec['format'] == 'json':
                parsed = json.loads(raw)
                if not isinstance(parsed, dict) or 'error' in parsed: raise ValueError('Invalid literature response')
                result_list = parsed.get('resultList')
                records = result_list.get('result') if isinstance(result_list, dict) else None
                if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
                    raise ValueError('Literature response lacks valid result records')
                if tool == 'search_papers':
                    fields = ('id', 'source', 'pmid', 'pmcid', 'doi', 'title', 'authorString', 'pubYear', 'journalTitle', 'isOpenAccess')
                    result['data'] = {'query': arguments['query'], 'hit_count': parsed.get('hitCount'),
                        'records': [{key: record[key] for key in fields if key in record} for record in records[:arguments.get('limit', 5)]],
                        'usage': 'Discovery metadata only. Read the exact paper before using a scientific assertion.'}
                    result['row_count'] = len(result['data']['records'])
                else:
                    if not records:
                        result['row_count'] = 0; result['data'] = {'limitation': 'No exact paper record returned; this is not biological absence.'}
                    else:
                        record = records[0]
                        if record.get('id') != arguments['id'] or record.get('source') != arguments['source']:
                            raise ValueError('Literature response identity differs from exact requested paper')
                        text = record.get('abstractText') or ''
                        if not isinstance(text, str): raise ValueError('Paper abstract has invalid shape')
                        fields = ('id', 'source', 'pmid', 'pmcid', 'doi', 'title', 'authorString', 'pubYear', 'isOpenAccess', 'pubTypeList', 'commentCorrectionList')
                        self.excerpt(result, arguments, {key: record[key] for key in fields if key in record}, text, 'abstract_html')
            else:
                # NLM articles commonly declare an external DTD. ElementTree
                # does not fetch it; reject entity declarations/expansion.
                if b'<!ENTITY' in raw.upper():
                    raise ValueError('Unsupported XML declarations in paper response')
                article = ElementTree.fromstring(raw)
                ids = article.findall('.//article-meta/article-id')
                pmc = {''.join(node.itertext()).strip().removeprefix('PMC') for node in ids if node.get('pub-id-type') in ('pmc', 'pmcid')}
                if arguments['id'].removeprefix('PMC') not in pmc: raise ValueError('Full-text paper identity does not match requested PMC record')
                paper = {'id': arguments['id'], 'source': 'PMC'}
                title = article.find('.//article-meta/title-group/article-title')
                if title is not None: paper['title'] = ''.join(title.itertext())
                for node in ids:
                    if node.get('pub-id-type') in ('doi', 'pmid'): paper[node.get('pub-id-type')] = ''.join(node.itertext()).strip()
                text = '\n'.join(''.join(node.itertext()) for node in article.iter() if node.tag in ('title', 'p', 'article-title'))
                self.excerpt(result, arguments, paper, text, 'oa_xml_text')
            if len(json.dumps(result, ensure_ascii=False).encode()) > 100_000:
                raise ValueError('Literature metadata exceeded display byte limit')
        except (ValueError, ElementTree.ParseError) as error:
            is_error = True; result['error'] = str(error); result['data'] = {}
        # Network exceptions are handled by the bridge's strict wall deadline.
        # Do not serialize the private bytes into the model-facing envelope.
        return {'isError': is_error, 'structuredContent': result,
                'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                '_raw_response': raw, '_raw_format': spec['format']}

    @staticmethod
    def excerpt(result, arguments, paper, text, kind):
        offset, limit = arguments.get('offset', 0), arguments.get('limit', 8000)
        if offset > len(text): raise ValueError('Paper text offset exceeds available text')
        end = min(len(text), offset + limit)
        result.update(paper_id=arguments['id'], source=arguments['source'], row_count=1 if end > offset else 0)
        result['data'] = {'paper': paper, 'text': text[offset:end], 'text_kind': kind, 'offset': offset,
                          'next_offset': end if end < len(text) else None, 'total_characters': len(text),
                          'limitation': ('Only this title/paragraph text window from the open-access XML was inspected; tables, figures, supplementary material and markup are not a complete-paper review.'
                                         if kind == 'oa_xml_text' else
                                         'Only the displayed abstract excerpt was inspected; abstracts do not establish unreported methods or full-paper findings.')}
