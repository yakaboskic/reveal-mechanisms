"""Shape validation only; trusted acceptance separately verifies source references."""
import json
import re

FORMAT = 'reveal.insufficient-evidence/1'


def validate_insufficient_outcome(value):
    def require(condition, message):
        if not condition: raise ValueError(message)
    require(isinstance(value, dict), 'Outcome must be an object')
    require(len(json.dumps(value, ensure_ascii=False).encode()) <= 100_000, 'Outcome exceeds byte limit')
    legacy = 'format' not in value
    allowed = {'format', 'status', 'reason', 'summary', 'explored_topics', 'limitations', 'missing_evidence', 'next_steps', 'evidence_refs'}
    if legacy: allowed.add('knowledge_gap_id')
    require(not set(value) - allowed, 'Unexpected outcome fields')
    require(value.get('format', FORMAT) == FORMAT and value.get('status') == 'insufficient_evidence', 'Invalid insufficient-evidence format/status')
    result = dict(value, format=FORMAT)
    for key, maximum in [('reason', 8000), ('summary', 1200), ('knowledge_gap_id', 300)]:
        if key != 'reason' and key not in result: continue
        item = result.get(key)
        require(isinstance(item, str) and bool(item.strip()) and len(item) <= maximum, f'Invalid outcome {key}')
    for key, maximum in [('explored_topics', 1000), ('limitations', 2000), ('missing_evidence', 2000), ('next_steps', 2000)]:
        items = result.setdefault(key, [])
        require(isinstance(items, list) and len(items) <= 20 and all(isinstance(x, str) and x.strip() and len(x) <= maximum for x in items), f'Invalid outcome {key}')
    refs = result.setdefault('evidence_refs', [])
    require(isinstance(refs, list) and len(refs) <= 40, 'Invalid outcome evidence_refs')
    for ref in refs:
        require(isinstance(ref, dict) and set(ref) == {'source', 'pointer', 'ledger_sequence'}, 'Invalid outcome evidence reference')
        location = ref['pointer']
        require(isinstance(location, str) and len(location) <= 4000 and (location == '' or location.startswith('/'))
                and re.search(r'~(?![01])', location) is None, 'Invalid evidence JSON pointer')
        require(ref['source'] in ('package', 'tool_response'), 'Invalid evidence source')
        require(ref['ledger_sequence'] is None if ref['source'] == 'package' else type(ref['ledger_sequence']) is int and ref['ledger_sequence'] > 0,
                'Invalid evidence ledger sequence')
    return result
