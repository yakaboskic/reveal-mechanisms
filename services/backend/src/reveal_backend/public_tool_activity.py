"""Bounded public projections of observable tool calls, never private traces."""
from __future__ import annotations

import json
import re

ARGUMENT_BYTES = 2048
PREVIEW_BYTES = 4096
REDACTED = '[REDACTED_CREDENTIAL]'
TRUNCATED = '\n[truncated]'
PRIVATE_KEYS = {'thinking', 'redactedthinking', 'reasoning', 'reasoningcontent', 'chainofthought', 'analysis', 'signature'}
PRIVATE_TYPES = {'thinking', 'redacted_thinking', 'reasoning', 'reasoning_content', 'signature'}
SECRET_KEYS = {'apikey', 'apitoken', 'authorization', 'authentication', 'password', 'passwd', 'secret', 'secretkey', 'privatekey',
               'token', 'accesstoken', 'refreshtoken', 'idtoken', 'authtoken', 'bearer', 'cookie',
               'setcookie', 'credentials', 'environment', 'env'}
ARGUMENTS = {
    'Read': ('file_path', 'path', 'offset', 'limit', 'pages'),
    'Grep': ('pattern', 'path', 'glob', 'type', 'output_mode', 'head_limit', 'offset', '-A', '-B', '-C', '-i', '-n', 'multiline'),
    'Glob': ('pattern', 'path'),
    'Write': ('file_path', 'path'),
    'Edit': ('file_path', 'path', 'replace_all'),
    'get_schema': ('graph',),
    'describe_kg': ('graph',),
    'query_graph': ('graph', 'subject', 'predicate', 'object', 'literal', 'contains', 'limit'),
    'lint_account': ('filename',),
    'write_account_draft': ('filename',),
    'search_papers': ('query', 'limit'),
    'read_paper': ('source', 'id', 'section', 'offset', 'limit'),
    'write_outcome': (),
    'get_local_work': (),
    'get_research_package': (),
    'get_operation': ('operation_id',),
    'list_data_operations': (),
    'describe_data_operation': ('operation_id',),
    'query_data': ('operation_id', 'arguments'),
    'find_propositions': ('query', 'filters', 'limit', 'offset'),
    'find_claims': ('query', 'filters', 'limit', 'offset'),
    'find_scientific_accounts': ('query', 'filters', 'limit', 'offset'),
    'get_scientific_object': ('selection',),
    'reuse_scientific_objects': ('selections',),
    'get_evidence_result': ('receipt_id',),
    'export_evidence_context': ('receipt_ids', 'import_ids', 'reuse_receipt_ids'),
    'read_evidence': ('artifact_id', 'sha256', 'pointer', 'mode', 'offset', 'limit', 'continuation'),
}
# Deliberate allowlist: new tools need an explicit public-display review. Work
# identities and proxy-supplied credentials/idempotency keys stay private.
for _name in (
        'search_factors', 'get_factor', 'get_factor_loadings', 'search_genes', 'resolve_gene',
        'get_gene_factors', 'search_gene_sets', 'get_gene_set', 'get_gene_set_members',
        'get_gene_set_factors', 'search_traits', 'get_trait', 'get_connections', 'get_imported_graph',
        'get_pigean_gene_phenotype', 'get_pigean_gene_set_phenotype'):
    ARGUMENTS[_name] = ('arguments',)
DURABLE_TOOLS = {name for name, fields in ARGUMENTS.items() if fields == ('arguments',)} | {
    'get_operation', 'query_data', 'export_evidence_context', 'reuse_scientific_objects'}


def bounded(text, limit):
    raw = text.encode('utf-8')
    if len(raw) <= limit:
        return text
    return raw[:max(0, limit - len(TRUNCATED.encode()))].decode('utf-8', 'ignore') + TRUNCATED


def normalized(key):
    return re.sub(r'[^a-z0-9]', '', str(key).lower())


def redact_text(text, secrets=()):
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    # Redact before truncating so a boundary cannot publish a credential prefix.
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer ' + REDACTED, text)
    text = re.sub(r'\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}', REDACTED, text)
    text = re.sub(r'''(?i)(["']?(?:api[_-]?(?:key|token)|authorization|authentication|password|access[_-]?token|refresh[_-]?token|secret|credentials)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}\]]+)''',
                  lambda match: match.group(1) + '"' + REDACTED + '"', text)
    # Such blocks are never useful tool previews, including an unterminated block.
    return re.sub(r'(?is)<(thinking|analysis|reasoning|redacted_thinking)\b[^>]*>.*?(?:</\1\s*>|$)', '[PRIVATE_CONTENT_OMITTED]', text)


def public_value(value, secrets=(), depth=0):
    if depth > 8:
        return '[nested content omitted]'
    if isinstance(value, dict):
        if isinstance(value.get('type'), str) and value['type'] in PRIVATE_TYPES:
            return '[PRIVATE_CONTENT_OMITTED]'
        result = {}
        for key, child in list(value.items())[:40]:
            name = normalized(key)
            if name in PRIVATE_KEYS or name.startswith(('thinking', 'reasoning', 'chainofthought')):
                continue
            public_key = redact_text(str(key), secrets)
            if name in SECRET_KEYS or name.endswith(('apikey', 'apitoken', 'password', 'accesstoken', 'refreshtoken', 'secretkey', 'secretaccesskey')):
                result[public_key] = REDACTED
            else:
                result[public_key] = public_value(child, secrets, depth + 1)
        if len(value) > 40:
            result['additional_fields'] = '[omitted from preview]'
        return result
    if isinstance(value, list):
        result = [public_value(child, secrets, depth + 1) for child in value[:20]]
        if len(value) > 20:
            result.append('[additional items omitted from preview]')
        return result
    if isinstance(value, str):
        return redact_text(value, secrets)
    if value is None or type(value) in (bool, int, float):
        return value
    return '[unsupported value]'


def tool_kind(name):
    return name[len('mcp__reveal__'):] if name.startswith('mcp__reveal__') else name


def display_arguments(name, arguments, secrets=()):
    if not isinstance(arguments, dict):
        return None
    kind = tool_kind(name)
    if kind not in ARGUMENTS:
        return 'Arguments unavailable for this tool.'
    values = {key: public_value(arguments[key], secrets) for key in ARGUMENTS[kind] if key in arguments}
    if kind == 'Write' and isinstance(arguments.get('content'), str):
        values['content_bytes'] = len(arguments['content'].encode())
    if kind == 'Edit':
        for key in ('old_string', 'new_string'):
            if isinstance(arguments.get(key), str):
                values[key + '_bytes'] = len(arguments[key].encode())
    if kind == 'write_account_draft' and isinstance(arguments.get('document'), dict):
        document = arguments['document']
        values['node_counts'] = {key: len(document[key]) for key in
            ('scientific_accounts', 'claims', 'propositions', 'evidence_items', 'files', 'mechanisms', 'knowledge_gaps')
            if isinstance(document.get(key), list)}
    return bounded(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(', ', ': ')), ARGUMENT_BYTES)


def sensitive_path(arguments):
    if not isinstance(arguments, dict):
        return False
    path = arguments.get('file_path') or arguments.get('path')
    if not isinstance(path, str):
        return False
    return bool(re.search(r'(^|/)(?:\.env(?:\.[^/]*)?|credentials[^/]*|\.ssh|\.aws|\.config)(?:/|$)|^/(?:proc|dev/fd)(?:/|$)|/reveal/state(?:/|$)', path, re.I))


def text_preview(text, secrets):
    # Read prefixes complete text files with line numbers. Parse a complete JSON
    # response when possible so private/credential fields are filtered by key.
    candidate = re.sub(r'(?m)^\s*\d+\t', '', text)
    try:
        value = json.loads(candidate)
    except (ValueError, TypeError):
        return redact_text(text, secrets)
    return json.dumps(public_value(value, secrets), ensure_ascii=False, separators=(', ', ': '))


def result_preview(result, arguments, secrets=()):
    if sensitive_path(arguments):
        return 'Output from a sensitive file path is not displayed.'
    if not isinstance(result, dict):
        return 'Tool result details unavailable.'
    content = result.get('content')
    parts = []
    if isinstance(content, str):
        parts.append(text_preview(content, secrets))
    elif isinstance(content, list):
        for block in content[:20]:
            if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
                parts.append(text_preview(block['text'], secrets))
        if len(content) > 20:
            parts.append('[additional result blocks omitted]')
    if not parts and 'structuredContent' in result:
        parts.append(json.dumps(public_value(result['structuredContent'], secrets), ensure_ascii=False, separators=(', ', ': ')))
    if not parts:
        return 'Returned an empty result.' if content in ('', []) else 'No public text preview available.'
    return bounded('\n\n'.join(parts), PREVIEW_BYTES)


def tool_call_payload(call_id, name, arguments, secrets=()):
    name = name if isinstance(name, str) else 'Unknown tool'
    safe_name = bounded(redact_text(name, secrets), 200)
    graph = arguments.get('graph') if isinstance(arguments, dict) else None
    return {'call_id': call_id, 'tool': safe_name, 'tool_name': safe_name,
            'selected_graph': graph if graph in ('biomarkerkg', 'prokn') else None,
            'display_arguments': display_arguments(name, arguments, secrets),
            'message': 'Calling ' + safe_name}


def tool_failed(name, result):
    """Inspect bounded MCP envelopes, never arbitrary scientific result fields.

Claude may mark the outer tool_result successful while wrapping an MCP isError
response as JSON text. Only lint/write tools interpret a report's valid=false.
No parsed content is returned or retained by this status projection.
"""
    authoring = isinstance(name, str) and tool_kind(name) in ('lint_account', 'write_account_draft')
    budget = 100_000
    def failed(value, depth=0):
        nonlocal budget
        if not isinstance(value, dict) or depth > 3:
            return False
        if value.get('is_error') is True or value.get('isError') is True:
            return True
        if authoring and value.get('valid') is False:
            return True
        structured = value.get('structuredContent')
        if authoring and isinstance(structured, dict) and structured.get('valid') is False:
            return True
        content = value.get('content')
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        if not isinstance(content, list):
            return False
        for block in content[:8]:
            if not isinstance(block, dict) or block.get('type') != 'text':
                continue
            text = block.get('text')
            if not isinstance(text, str) or len(text) > budget:
                continue
            size = len(text.encode())
            if size > budget:
                continue
            budget -= size
            try:
                parsed = json.loads(text)
            except (ValueError, RecursionError):
                continue
            if not isinstance(parsed, dict):
                continue
            envelope = (set(parsed) <= {'content', 'structuredContent', 'isError', '_meta'}
                        and isinstance(parsed.get('isError'), bool)
                        and (isinstance(parsed.get('content'), list) or isinstance(parsed.get('structuredContent'), dict)))
            report = authoring and isinstance(parsed.get('valid'), bool)
            if (envelope or report) and failed(parsed, depth + 1):
                return True
        return False
    return failed(result)


def tool_result_payload(call_id, name, arguments, result, secrets=()):
    payload = tool_call_payload(call_id, name, arguments, secrets)
    failed = tool_failed(name, result)
    payload.update(status='error' if failed else 'completed',
                   message=payload['tool_name'] + (' failed' if failed else ' completed'),
                   output_excerpt=result_preview(result, arguments, secrets))
    operation = durable_operation(result, secrets) if tool_kind(payload['tool_name']) in DURABLE_TOOLS else None
    if not failed and operation and operation['state'] in ('received', 'running'):
        payload['message'] = (payload['tool_name'] + ': evidence operation ' + operation['operation_id']
                              + ' is ' + operation['state'] + '; scientific result is not ready')
    return payload


def durable_operation(result, secrets=()):
    """Only bounded operation identity/state, never scientific or private text."""
    if not isinstance(result, dict):
        return None
    value = result.get('structuredContent')
    if not isinstance(value, dict):
        content = result.get('content')
        text = content if isinstance(content, str) else None
        if isinstance(content, list):
            text = next((block.get('text') for block in content
                if isinstance(block, dict) and block.get('type') == 'text'), None)
        try:
            value = json.loads(text) if isinstance(text, str) and len(text) <= 100_000 else None
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    identity, state = value.get('operation_id'), value.get('state')
    if (isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity)
            and redact_text(identity, secrets) == identity
            and state in ('received', 'running', 'succeeded', 'failed', 'cancelled')):
        return {'operation_id': identity, 'state': state}
    return None
