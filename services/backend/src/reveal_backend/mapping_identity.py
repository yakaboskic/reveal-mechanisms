"""Conservative, versioned retrieval eligibility; never rewrite upstream mappings."""
from decimal import Decimal, InvalidOperation
import re

POLICY_VERSION = 'reveal.trait-identity-eligibility/2'
PREFIXES = {'MONDO', 'DOID', 'EFO', 'HP', 'MESH', 'ORPHANET', 'OMIM'}
EXACT = {'skos:exactMatch', 'http://www.w3.org/2004/02/skos/core#exactMatch',
         'owl:equivalentClass', 'http://www.w3.org/2002/07/owl#equivalentClass'}
JUSTIFICATIONS = {'curated', 'manual', 'expert_curated', 'xref', 'semapv:ManualMappingCuration'}
OFFICIAL_URI_PATTERNS = (
    ('EFO', r'https?://www\.ebi\.ac\.uk/efo/EFO_([0-9]+)'),
    ('MESH', r'https?://id\.nlm\.nih\.gov/mesh/([DCQ][0-9]+)'),
    ('OMIM', r'https?://omim\.org/entry/([0-9]{6})'),
)


def normalize_disease_id(value):
    """Normalize only supported registries and their authoritative URI forms."""
    if not isinstance(value, str): return None
    value = value.strip()
    # Exact host/path patterns reject credentials, ports, query/fragment suffixes,
    # encoded separators and lookalike hosts. Eligibility is checked separately.
    for prefix, pattern in OFFICIAL_URI_PATTERNS:
        match = re.fullmatch(pattern, value)
        if match: return prefix + ':' + match.group(1)
    match = re.fullmatch(r'https?://purl\.obolibrary\.org/obo/([A-Za-z]+)_([0-9]+)', value)
    if not match:
        match = re.fullmatch(r'https?://identifiers\.org/(mondo|doid|efo|hp|mesh|orphanet|omim)[:/]([A-Za-z0-9]+)', value, re.I)
    if not match:
        match = re.fullmatch(r'(MONDO|DOID|EFO|HP|MESH|ORPHANET|Orphanet|OMIM):([A-Za-z0-9]+)', value, re.I)
    if not match: return None
    prefix, accession = match.groups(); prefix = prefix.upper()
    if prefix not in PREFIXES: return None
    if prefix != 'MESH' and not accession.isdigit(): return None
    return prefix + ':' + accession


def interpret_mapping(mapping):
    """Derived eligibility is separate from the untouched assertion and confidence."""
    normalized = normalize_disease_id(mapping.get('target_id'))
    justification = str(mapping.get('mapping_justification') or '').strip()
    reason = ('unsupported_identifier' if not normalized else
              'inherited_mapping' if 'inherited' in justification.casefold() else
              'non_identity_predicate' if mapping.get('mapping_predicate') not in EXACT else
              'unsupported_or_lexical_justification' if justification not in JUSTIFICATIONS else
              'missing_source' if not mapping.get('source') else 'eligible_curated_identity')
    result = {'policy_version': POLICY_VERSION, 'normalized_target_id': normalized,
              'identity_eligible': reason == 'eligible_curated_identity', 'reason': reason}
    try:
        confidence = Decimal(str(mapping.get('confidence', '')))
        if confidence.is_finite():
            result['parsed_confidence'] = str(confidence)
            result['confidence_semantics'] = 'Uncalibrated source value; not a probability.'
    except InvalidOperation: pass
    return result


def interpreted_mappings(mappings):
    results = [{'mapping_index': index, **interpret_mapping(row)} for index, row in enumerate(mappings)]
    targets = {}
    for item in results:
        if item['identity_eligible']:
            identity = item['normalized_target_id']
            targets.setdefault(identity.split(':', 1)[0], set()).add(identity)
    for item in results:
        if item['identity_eligible'] and len(targets[item['normalized_target_id'].split(':', 1)[0]]) > 1:
            item.update(identity_eligible=False, reason='ambiguous_identity_targets')
    return results
