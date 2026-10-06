"""Shared forecasts for public-only inputs, with immutable leader references.

Only the scientific result/status projection is shared. Every caller retains
its own authorized assessment/draft wrapper. A cache index may move to a new
leader without changing the version an older caller originally followed.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import re
from uuid import UUID

from .repository import digest, now
from .user_inputs import INPUT_FIELDS

OWNER = 'service:cfde-assessment'
KIND = 'cfde_assessment_shared'
INDEX_KIND = 'cfde_assessment_shared_cache'
PUBLIC_FIELDS = ('status', 'updated_at', 'expires_at', 'reference_generation_id', 'result', 'coverage', 'error')
PENDING = ('preparing', 'assessing')
TTL = timedelta(days=7)
SHA256 = re.compile(r'[a-f0-9]{64}')
RELATIONSHIPS = ('gene_gene_set', 'gene_mechanism', 'gene_set_mechanism')
RESULT_FIELDS = ('verdict', 'probability_yes', 'probability_no', 'confidence', 'main_blocker', 'relationship_support', 'calibration')
COUNTS = ('factor_count', 'gene_loading_count', 'gene_set_loading_count', 'unique_gene_set_count')
BLOCKERS = {'none', 'weak_relevance', 'missing_cfde_evidence', 'wrong_data_type',
            'species_or_context_mismatch', 'incomplete_inputs', 'unsupported_inference'}


def _hash(value): return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _uuid(value):
    try: return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError): return False


def shared_key(composer, generation, *, model, rubric):
    """Canonical public state identity; client selection metadata has no effect.

    Eligibility is deliberately conservative. Missing generation pins, malformed
    references, any private text, or an upload make a request private-only.
    ``model`` and ``rubric`` are explicit to avoid a runtime-module import cycle.
    """
    if not _hash(generation) or not isinstance(composer, dict): return None
    if not all(isinstance(composer.get(field, ''), str) and not composer.get(field, '').strip() for field in INPUT_FIELDS):
        return None
    if composer.get('upload_ids', []) != []: return None
    if not all(isinstance(value, str) and value.strip() for value in (model, rubric)): return None
    gap = composer.get('source_gap')
    if not isinstance(gap, dict) or set(gap) != {'id', 'source_id', 'source_revision'}: return None
    if not (isinstance(gap['id'], str) and re.fullmatch(r'dapper:KnowledgeGap\.[A-Za-z0-9_-]{32}', gap['id']) and
            isinstance(gap['source_id'], str) and gap['source_id'].startswith('dismech:') and _hash(gap['source_revision'])):
        return None
    anchors = composer.get('eaggl_anchors')
    if not isinstance(anchors, list) or not 1 <= len(anchors) <= 10: return None
    references = []
    for selection in anchors:
        reference = selection.get('reference') if isinstance(selection, dict) else None
        if not isinstance(reference, dict) or set(reference) != {'source', 'source_id', 'source_revision', 'dapper_id'}: return None
        if not (reference['source'] == 'eaggl' and isinstance(reference['source_id'], str) and
                reference['source_id'].startswith('factor:') and _hash(reference['source_revision']) and
                isinstance(reference['dapper_id'], str) and re.fullmatch(r'dapper:Mechanism\.[A-Za-z0-9_-]{32}', reference['dapper_id'])):
            return None
        references.append(reference)
    if len({item['source_id'] for item in references}) != len(references): return None
    if composer.get('model') not in ('cfde-inc-v2', 'eaggl-capped-v1'): return None
    kgs = composer.get('selected_kgs', [])
    if not isinstance(kgs, list) or any(item not in ('biomarkerkg', 'prokn') for item in kgs) or len(set(kgs)) != len(kgs): return None
    return digest(['reveal.cfde-shared-assessment/1', generation, gap, references,
                   composer['model'], kgs, model, rubric])


def _stamp(value):
    if not isinstance(value, str): raise ValueError('Missing cache timestamp')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None: raise ValueError('Cache timestamp must include a timezone')
    return stamp.astimezone(timezone.utc)


def _probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _public(value):
    """Copy only the modeled projection, including nested allowlists.

    Ignore unrelated wrapper metadata; reject malformed modeled fields rather
    than propagating untyped payloads to another user's assessment.
    """
    if not isinstance(value, dict) or any(field not in value for field in PUBLIC_FIELDS): raise ValueError('Incomplete shared projection')
    result = {field: deepcopy(value[field]) for field in PUBLIC_FIELDS}
    if result['status'] not in (*PENDING, 'succeeded', 'failed', 'interrupted'): raise ValueError('Invalid shared status')
    _stamp(result['updated_at']); _stamp(result['expires_at'])
    if not _hash(result['reference_generation_id']): raise ValueError('Missing generation')
    outcome = result['result']
    if outcome is not None:
        if not isinstance(outcome, dict) or any(field not in outcome for field in RESULT_FIELDS): raise ValueError('Invalid shared result')
        outcome = {field: outcome[field] for field in RESULT_FIELDS}
        if (outcome['verdict'] not in ('yes', 'no') or outcome['main_blocker'] not in BLOCKERS or
                outcome['calibration'] != 'not_calibrated' or
                not all(_probability(outcome[key]) for key in ('probability_yes', 'probability_no', 'confidence'))):
            raise ValueError('Invalid shared result values')
        relationships = outcome['relationship_support']
        if not isinstance(relationships, dict) or not all(_probability(relationships.get(key)) for key in RELATIONSHIPS):
            raise ValueError('Invalid shared relationship scores')
        outcome['relationship_support'] = {key: relationships[key] for key in RELATIONSHIPS}
        result['result'] = outcome
    coverage = result['coverage']
    if coverage is not None:
        if not isinstance(coverage, dict) or not all(type(coverage.get(key)) is int and coverage[key] >= 0 for key in COUNTS):
            raise ValueError('Invalid shared coverage counts')
        if (type(coverage.get('complete')) is not bool or not isinstance(coverage.get('missing'), list) or
                not all(isinstance(item, str) for item in coverage['missing']) or not isinstance(coverage.get('truncations'), list)):
            raise ValueError('Invalid shared coverage')
        truncations = []
        for item in coverage['truncations']:
            if (not isinstance(item, dict) or not isinstance(item.get('source'), str) or
                    not all(type(item.get(key)) is int and item[key] >= 0 for key in ('included_chars', 'total_chars'))):
                raise ValueError('Invalid shared truncation')
            truncations.append({key: item[key] for key in ('source', 'included_chars', 'total_chars')})
        result['coverage'] = {**{key: coverage[key] for key in COUNTS}, 'complete': coverage['complete'],
                              'missing': coverage['missing'], 'truncations': truncations}
    error = result['error']
    if error is not None:
        if not (isinstance(error, dict) and isinstance(error.get('code'), str) and isinstance(error.get('detail'), str) and
                type(error.get('retryable')) is bool): raise ValueError('Invalid shared error')
        result['error'] = {key: error[key] for key in ('code', 'detail', 'retryable')}
    if result['status'] == 'succeeded' and (outcome is None or coverage is None or error is not None): raise ValueError('Incomplete successful result')
    return result


def _version(tx, key, leader):
    if not _hash(key) or not _uuid(leader): return None
    row = tx.get(KIND, digest([key, leader]))
    if not row or row['owner'] != OWNER: return None
    value = row['data']
    if not isinstance(value, dict) or value.get('key') != key or value.get('leader') != leader: return None
    try: return {'leader': leader, 'public': _public(value.get('public'))}
    except (ValueError, TypeError, OverflowError): return None


def read_shared(tx, key):
    """Read the current reusable version; failures and expired work are misses."""
    if not _hash(key): return None
    index = tx.get(INDEX_KIND, key)
    if not index or index['owner'] != OWNER or not isinstance(index['data'], dict): return None
    value = _version(tx, key, index['data'].get('leader'))
    if not value: return None
    public = value['public']; stamp = _stamp(now())
    if public['status'] in PENDING:
        return value if _stamp(public['expires_at']) > stamp else None
    if public['status'] == 'succeeded' and stamp - TTL < _stamp(public['updated_at']) <= stamp: return value
    return None


def projection_fields(tx, private_data):
    """Follow the exact historical leader, independently of current cache TTL."""
    reference = private_data.get('shared_ref') if isinstance(private_data, dict) else None
    if not isinstance(reference, dict): return {}
    value = _version(tx, reference.get('key'), reference.get('leader'))
    return value['public'] if value else {}


def create_shared(tx, key, public):
    """Create a new leader version and move only its current-cache index."""
    if not _hash(key) or not isinstance(public, dict) or not _uuid(public.get('id')): raise ValueError('Invalid shared leader')
    leader = public['id']; identity = digest([key, leader]); reference = {'key': key, 'leader': leader}
    if tx.get(KIND, identity): return reference  # Idempotence never moves a newer index backwards.
    tx.put(KIND, identity, OWNER, {'key': key, 'leader': leader, 'public': _public(public)})
    tx.put(INDEX_KIND, key, OWNER, {'leader': leader})
    return reference


def publish_shared(tx, private_data, identity):
    """Only the leader updates its own version; never modify the cache index."""
    reference = private_data.get('shared_ref') if isinstance(private_data, dict) else None
    if not isinstance(reference, dict) or reference.get('leader') != identity: return False
    key = reference.get('key'); current = _version(tx, key, identity)
    if not current: return False
    public = private_data.get('public')
    if not isinstance(public, dict) or public.get('id') != identity: return False
    try: projection = _public(public)
    except (ValueError, TypeError, OverflowError): return False
    if projection['reference_generation_id'] != current['public']['reference_generation_id']: return False
    tx.put(KIND, digest([key, identity]), OWNER, {'key': key, 'leader': identity, 'public': projection})
    return True
