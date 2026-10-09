"""Real SSE framing and partial prose extraction without a provider or database."""
import json

import pytest

from reveal_backend import lightning_stream as stream
from reveal_backend.auth import Problem


def event(kind, **fields):
    return ('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **fields}, ensure_ascii=False) + '\n\n').encode()


def provider_events(response, *, chunk_size=37):
    yield event('message_start', message={'content': [], 'usage': {**response['usage'], 'output_tokens': 0}})
    for index, block in enumerate(response['content']):
        yield event('content_block_start', index=index, content_block={'type': block['type'], 'text': ''})
        for offset in range(0, len(block['text']), chunk_size):
            yield event('content_block_delta', index=index, delta={'type': 'text_delta', 'text': block['text'][offset:offset + chunk_size]})
        yield event('content_block_stop', index=index)
    yield event('message_delta', delta={'stop_reason': response['stop_reason']}, usage={'output_tokens': response['usage']['output_tokens']})
    yield event('message_stop')


def provider_bytes(response): return b''.join(provider_events(response))


def fixture_response():
    return {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"summary":"Résumé α and 🧬"}'}],
            'usage': {'input_tokens': 84, 'output_tokens': 19}}


@pytest.mark.parametrize('size', [1, 2, 7, 41, 16384])
def test_sse_chunk_boundaries_utf8_and_cumulative_usage(size):
    raw = provider_bytes(fixture_response()); parser = stream.MessageStream()
    for offset in range(0, len(raw), size): parser.feed(raw[offset:offset + size])
    assert parser.finish() == fixture_response()
    assert parser.text == fixture_response()['content'][0]['text']


def test_sse_crlf_comments_multiline_data_ping_and_future_events():
    response = fixture_response(); parser = stream.MessageStream()
    parser.feed(b': keepalive\r\n\r\n' + event('ping').replace(b'\n', b'\r\n'))
    parser.feed(event('future_safe_event', text='private reasoning must never appear'))
    for item in provider_events(response):
        # SSE permits multiple data lines; split only on whitespace in JSON.
        item = item.replace(b', "', b',\ndata: "', 1)
        parser.feed(item.replace(b'\n', b'\r\n'))
    assert parser.finish() == response
    assert 'private' not in parser.text


@pytest.mark.parametrize('variant', ['no_stop', 'open_block', 'no_delta', 'invalid_utf8', 'malformed_json', 'wrong_event', 'hidden_thinking', 'tool', 'wrong_index', 'after_stop'])
def test_stream_protocol_failures_never_become_complete(variant):
    events = list(provider_events(fixture_response())); parser = stream.MessageStream()
    if variant == 'no_stop': events.pop()
    elif variant == 'open_block': events = events[:3] + [event('message_stop')]
    elif variant == 'no_delta': events = events[:-2] + [event('message_stop')]
    elif variant == 'invalid_utf8': events[2] = b'\xff\n\n'
    elif variant == 'malformed_json': events[2] = b'event: content_block_delta\ndata: not json\n\n'
    elif variant == 'wrong_event': events[2] = event('ping').replace(b'event: ping', b'event: message_stop')
    elif variant in ('hidden_thinking', 'tool'):
        events[1] = event('content_block_start', index=0, content_block={'type': 'thinking' if variant == 'hidden_thinking' else 'tool_use', 'text': 'private'})
    elif variant == 'wrong_index': events[2] = event('content_block_delta', index=8, delta={'type': 'text_delta', 'text': 'private'})
    elif variant == 'after_stop': events.append(event('message_stop'))
    with pytest.raises(Problem) as error:
        for item in events: parser.feed(item)
        parser.finish()
    assert error.value.code in ('LIGHTNING_RESPONSE_INVALID', 'LIGHTNING_INCOMPLETE')
    assert 'private' not in parser.text


def test_http_200_stream_error_is_sanitized():
    parser = stream.MessageStream()
    with pytest.raises(Problem) as error:
        parser.feed(event('error', error={'type': 'overloaded_error', 'message': 'secret credentials and source text'}))
    assert error.value.code == 'LIGHTNING_PROVIDER_UNAVAILABLE'
    assert 'secret' not in error.value.detail


@pytest.mark.parametrize('bound', ['stream', 'event', 'events', 'content'])
def test_independent_stream_bounds(monkeypatch, bound):
    monkeypatch.setattr(stream, {'stream': 'MAX_STREAM_BYTES', 'event': 'MAX_EVENT_BYTES',
        'events': 'MAX_EVENTS', 'content': 'MAX_CONTENT_BYTES'}[bound], 2)
    parser = stream.MessageStream()
    with pytest.raises(Problem) as error:
        for item in provider_events(fixture_response()): parser.feed(item)
        parser.finish()
    assert error.value.code == 'LIGHTNING_RESPONSE_INVALID'


def test_partial_json_decodes_escaped_unicode_without_exposing_keys_or_references():
    summary = 'CFDE résumé α includes a "named" signal.\nSpecies 🧬 scope remains uncertain.'
    text = json.dumps({'assessment': 'partial', 'summary': summary, 'observations': [
        {'text': 'One grounded observation.', 'evidence_refs': ['PRIVATE_REFERENCE']}],
        'recommended_direction': 'Inspect the source.', 'thinking': 'PRIVATE_REASONING',
        'missing_evidence': ['Tissue context.'], 'next_steps': ['Check coverage.'], 'limitations': ['A bounded sample.']})
    for offset in range(1, len(text) + 1):
        preview = stream.preview(text[:offset])
        if preview:
            assert summary.startswith(preview['summary'])
            assert 'PRIVATE' not in json.dumps(preview)
            assert '\\u' not in preview['summary']
    assert stream.preview(text) == {'summary': summary, 'recommended_direction': 'Inspect the source.',
        'observations': ['One grounded observation.'], 'missing_evidence': ['Tissue context.'],
        'next_steps': ['Check coverage.'], 'limitations': ['A bounded sample.']}


@pytest.mark.parametrize('tail', ['\\', '\\u', '\\u03', '\\ud83e', '\\ud83e\\', '\\ud83e\\udd'])
def test_incomplete_escape_or_surrogate_is_never_shown(tail):
    assert stream.preview('{"summary":"Grounded. ' + tail)['summary'] == 'Grounded. '


def test_partial_preview_bounds_and_unknown_values():
    assert stream.preview('{"thinking":"private", "summary":') is None
    assert stream.preview('{"summary":{"nested":"private"}}') is None
    assert stream.preview('{"summary":"safe\\xinvalid"}') is None
    value = stream.preview(json.dumps({'summary': 's' * 30000, 'recommended_direction': 'hidden after cap',
        'observations': [{'text': 'o'}] * 1000}))
    assert value['summary'] == 's' * stream.MAX_PREVIEW_CHARS
    assert value['recommended_direction'] == '' and len(value['observations']) == 12
