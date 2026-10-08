"""Advisory claim-structure suggestions for a ScientificAccount; never a validation gate.

claim_structure(document, package) classifies every authored Claim as one atomic family (endpoint kinds plus
predicate, else its ClaimScore metrics), a synthesis (its EvidenceItems carry source_claims) or other (KG,
literature, free interpretation). It checks atomic Claims and synthesis paths against the recommended
structure and summarizes conformance. Suggestions are value-free templates: they name checks, families and
recommended terms and never quote account or source text. Pure and standard-library only, so the isolated
linter subprocess can import it; it reads no file, database or network.
"""
import re
import string

FORMAT = 'reveal.claim-structure/1'
WHY = 'Claim-structure suggestions are optional guidance and never affect validation, including strict mode.'
OBO, PROV, BIOLINK = 'http://purl.obolibrary.org/obo/', 'http://www.w3.org/ns/prov#', 'https://w3id.org/biolink/vocab/'
LEGACY = 'urn:reveal:relation:'
GAP_RELATION = LEGACY + 'candidate-gap-connection'
DECLARED = ('obo', 'prov')  # DAPPER declares these; others must be server-declared package prefixes.
MAX_CLAIMS = 8
FOLD = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

# Predicate group: (CURIE, full IRI, accepted synonyms that are not recommended).
PREDICATES = {
    'member': ('prov:hadMember', PROV + 'hadMember', {LEGACY + 'member-of', LEGACY + 'has-member', 'RO:0002350', 'RO:0002351',
               'obo:RO_0002350', 'obo:RO_0002351', OBO + 'RO_0002350', OBO + 'RO_0002351'}),
    'correlated': ('obo:RO_0002610', OBO + 'RO_0002610', {'RO:0002610', *(LEGACY + name for name in (
        'has-loading-on', 'has-joint-loading-on', 'observed-loading', 'observed-projection', 'factor-gene-set-direct'))}),
    'genetic': ('biolink:genetically_associated_with', BIOLINK + 'genetically_associated_with', {LEGACY + 'genetically-associated-with'}),
    'involved': ('obo:RO_0002331', OBO + 'RO_0002331', {'RO:0002331', LEGACY + 'involved-in'}),
}
# family: subject kind, predicate group, object kind, accepted metrics, score kind, label, retrieval tools
FAMILIES = {
    'factor_gene': ('gene', 'correlated', 'factor', ('loading', 'factor_value'), 'LOADING', 'factor-gene',
                    'get_factor_loadings or get_gene_factors'),
    'phenotype_gene': ('gene', 'genetic', 'trait', ('combined', 'log_bf', 'prior'), 'SCORE', 'phenotype-gene',
                       'get_pigean_gene_phenotype'),
    'factor_gene_set': ('gene_set', 'correlated', 'factor', ('joint_loading', 'marginal_loading', 'factor_value'), 'LOADING',
                        'factor-gene set', 'get_factor_loadings with kind gene_set, or get_gene_set_factors'),
    'gene_gene_set': ('gene_set', 'member', 'gene', (), None, 'gene-gene set', 'get_gene_gene_sets, then get_gene_set and get_gene_set_members'),
    'gene_set_trait': ('gene_set', 'correlated', 'trait', ('beta_uncorrected', 'beta'), 'EFFECT_ESTIMATE', 'gene set-trait',
                       'get_pigean_gene_set_phenotype'),
}
TEMPLATES = {'factor_gene': 'Gene G has loading L on factor F.', 'phenotype_gene': 'Gene G is associated with P in PIGEAN (combined C).',
             'factor_gene_set': 'Gene set S has joint loading L on factor F.', 'gene_gene_set': 'Gene set S has member G.',
             'gene_set_trait': 'Gene set S is associated with P (beta_uncorrected B).'}
ENDPOINTS = {'gene': 'HGNC.SYMBOL:<symbol>', 'gene_set': '<trusted dapper:GeneSet>', 'factor': '<trusted dapper:Mechanism>',
             'trait': 'KPN.TRAIT:<NNNNNNN>'}
PAIRS = {frozenset((spec[0], spec[2])): family for family, spec in FAMILIES.items()}
METRIC_FAMILY = {'loading': 'factor_gene', 'joint_loading': 'factor_gene_set', 'marginal_loading': 'factor_gene_set',
                 'combined': 'phenotype_gene', 'log_bf': 'phenotype_gene', 'prior': 'phenotype_gene',
                 'beta': 'gene_set_trait', 'beta_uncorrected': 'gene_set_trait'}
# Legacy relations name their family even when an endpoint is not a recognized identifier kind.
RELATION_FAMILY = {LEGACY + 'member-of': 'gene_gene_set', LEGACY + 'has-member': 'gene_gene_set',
                   LEGACY + 'genetically-associated-with': 'phenotype_gene',
                   LEGACY + 'has-loading-on': 'factor_gene', LEGACY + 'observed-loading': 'factor_gene',
                   LEGACY + 'has-joint-loading-on': 'factor_gene_set', LEGACY + 'observed-projection': 'factor_gene_set',
                   LEGACY + 'factor-gene-set-direct': 'factor_gene_set'}
KINDS = (('gene', ('HGNC.SYMBOL:', 'HGNC:', 'https://identifiers.org/hgnc.symbol:', 'http://identifiers.org/hgnc/')),
         ('gene_set', ('dapper:GeneSet.',)), ('factor', ('dapper:Mechanism.',)),
         ('trait', ('KPN.TRAIT:', 'https://broadinstitute.github.io/kpn-data-models/kpn.trait/')), ('gap', ('dapper:KnowledgeGap.',)))
SYMBOLS = ('HGNC.SYMBOL:', 'https://identifiers.org/hgnc.symbol:')
TRAIT_TEXT = re.compile(r'\bKPN trait KPN\.TRAIT:(\d{7})\b')
WITHIN_FIT = re.compile(r"\bwithin[- ](?:the |its |this )?(?:factor(?:'s)? )?fit|\bown (?:fitted )?trait|\bin-fit\b", re.I)
NUMBER = re.compile(r'(?<![\w.])[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')

# check: (message, repair). Placeholders are filled only from the constants above, never from document text.
MESSAGES = {
    'gap-relevance-missing': ('No gap-relevance synthesis Claim connects the atomic Claims to the selected KnowledgeGap.',
        'Add a BIOLOGICAL_INTERPRETATION Claim whose EvidenceItem.source_claims cite factor-gene, gene-gene set and phenotype-gene '
        '(or gene set-trait) Claims on one path: a text-only Proposition, or gene/gene set ' + GAP_RELATION + ' the selected KnowledgeGap.'),
    'gap-target': ('A gap-relevance Claim does not target the selected KnowledgeGap.',
        'Use the account question as object_entity with relation ' + GAP_RELATION + ', or a text-only Proposition.'),
    'synthesis-families': ('A synthesis Claim cites atomic Claims from fewer than two families.',
        'Cite atomic Claims from at least two families (for example factor-gene plus gene-gene set) through EvidenceItem.source_claims.'),
    'synthesis-path': ('A synthesis Claim cites atomic Claims that do not share one gene, gene set, factor or phenotype path.',
        'Cite atomic Claims with complete triples that connect (factor-gene G, gene set S containing G, phenotype-gene G); '
        'the synthesis subject must be on that path. Keep unrelated observations in separate syntheses.'),
    'synthesis-predicate': ('An involvement synthesis uses the legacy involved-in relation.', 'Use relation obo:RO_0002331 (involved in).'),
    'synthesis-within-fit': ("A synthesis path links a factor to the phenotype its own fit was estimated from.",
        "Say the phenotype link is within the factor's fit, not independent support; a different phenotype is an independent bridge."),
    'atomic-result-kind': ('{label} observation is not a RESULT Proposition.',
        'Set proposition_kind RESULT; put interpretation in a separate synthesis Claim citing this one through EvidenceItem.source_claims.'),
    'atomic-triple': ('{label} Claim lacks a complete subject_entity, relation and object_entity triple.', 'Write {triple}.'),
    'atomic-orientation': ('{label} triple has subject and object reversed.', 'Write {triple}.'),
    'atomic-endpoints': ('{label} triple endpoints are not the expected identifier kinds.',
        'Write {triple}: genes as captured HGNC.SYMBOL:<symbol>, the trusted dapper:GeneSet and dapper:Mechanism ids from '
        'get_gene_set and get_factor, traits as KPN.TRAIT:<NNNNNNN>.'),
    'atomic-predicate': ('{label} relation is an accepted synonym, not the recommended predicate.', 'Use relation {predicate}.'),
    'atomic-score': ('{label} Claim lacks a ClaimScore with the expected metric and kind.',
        'Add has_score: ClaimScore metric {metrics} (the exact captured column), score_kind {kind}, the exact captured value and an interpretation.'),
    'atomic-capture': ('{label} evidence does not derive directly from a captured source File{sets}.',
        'List the capture File{sets} in EvidenceItem.was_derived_from, with the exact row locator in context and a verbatim snippet.'),
    'atomic-single-fact': ('{label} Claim bundles more than one observation.',
        'Write one atomic Claim per observed pair and family; cite them together from a synthesis Claim.'),
    'atomic-value-in-statement': ('{label} statement does not state its ClaimScore value.',
        'State the exact value in the Proposition statement, for example: {template}'),
    'family-missing': ('No atomic Claims for: {families}.', 'When relevant to the gap, consider {tools}.'),
}
ORDER = list(MESSAGES)


def records(value, group):
    rows = value.get(group) if isinstance(value, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def refs(node, field):
    values = node.get(field) if isinstance(node, dict) else None
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def text(value):
    return value if isinstance(value, str) else ''


def kind_of(value):
    return next((kind for kind, prefixes in KINDS if isinstance(value, str) and value.startswith(prefixes)), None)


def entity(value):
    """Comparable identity: gene symbols ASCII-case-folded, traits by KPN number, DAPPER ids exact."""
    kind = kind_of(value)
    if kind == 'gene':
        return ('gene', value.rsplit(':', 1)[-1].translate(FOLD) if value.startswith(SYMBOLS) else 'HGNC:' + re.split('[:/]', value)[-1])
    if kind == 'trait': return ('trait', re.split('[:/]', value)[-1])
    return (kind, value) if kind else None


def predicate_group(relation):
    """(group, recommended form used). Unlisted urn:reveal:relation:* terms are accepted legacy synonyms."""
    for group, (curie, iri, synonyms) in PREDICATES.items():
        if relation in (curie, iri): return group, True
        if relation in synonyms: return group, False
    return ('legacy', False) if relation.startswith(LEGACY) and relation != GAP_RELATION else (None, False)


def significant(token):
    return re.sub(r'[eE].*', '', token).lstrip('+-').replace('.', '').lstrip('0')


def mentions(statement, value):
    """The statement states value (sign aside), exactly or rounded to at least three significant digits (within 1%)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)): return False
    try: value = abs(float(value))
    except OverflowError: return False
    wanted = min(3, len(significant(repr(value)).rstrip('0')) or 1)
    for token in NUMBER.findall(statement):
        mantissa, _, exponent = token.lower().partition('e')
        scale = min(300, int(exponent or 0)) - len(mantissa.partition('.')[2])
        tolerance = 0.5 * 10.0 ** scale + 1e-12 * max(1, value)
        if len(significant(token)) >= wanted and abs(abs(float(token)) - value) <= min(tolerance, 0.01 * value + 1e-12): return True
    return False


def factor_traits(document, context, package):
    """Each trusted Mechanism's own fitted KPN trait number, when its description or fit records it."""
    traits = {}
    for node in records(context, 'mechanisms') + records(document, 'mechanisms'):
        match = TRAIT_TEXT.search(text(node.get('description')))
        if match and isinstance(node.get('id'), str): traits.setdefault(node['id'], match.group(1))
    pigean = package.get('pigean')
    bindings = pigean.get('mechanisms') if isinstance(pigean, dict) else None
    for binding in bindings.values() if isinstance(bindings, dict) else ():
        fit = binding.get('fit') if isinstance(binding, dict) else None
        if isinstance(fit, dict) and fit.get('trait_group') == 'kpn' and re.fullmatch(r'\d{7}', text(fit.get('phenotype'))) \
                and isinstance(binding.get('dapper_id'), str):
            traits.setdefault(binding.get('dapper_id'), fit['phenotype'])
    return traits


def claim_structure(document, package=None):
    """{'summary', 'suggestions'} for one account document; package is its trusted evidence context when known."""
    document = document if isinstance(document, dict) else {}
    package = package if isinstance(package, dict) else {}
    context = package.get('dapper_context') if isinstance(package.get('dapper_context'), dict) else {}
    nodes = {node['id']: node for group in document for node in records(document, group) if isinstance(node.get('id'), str)}
    trusted = {node['id'] for group in context for node in records(context, group) if isinstance(node.get('id'), str)}
    artifacts = package.get('source_artifacts') if isinstance(package.get('source_artifacts'), dict) else {}
    files = {artifact.get('dapper_file_id') for artifact in artifacts.values() if isinstance(artifact, dict)}
    files |= {node['id'] for node in records(document, 'files') + records(context, 'files') if isinstance(node.get('id'), str)}
    prefixes = {key for value in (package.get('prefixes'), context.get('prefixes')) if isinstance(value, dict) for key in value}
    account = (records(document, 'scientific_accounts') or [{}])[0]
    selection = package.get('selection') if isinstance(package.get('selection'), dict) else {}
    gap = text(selection.get('knowledge_gap_id')) or text(account.get('question'))
    where = text(account.get('id')) or 'scientific_accounts'
    traits = factor_traits(document, context, package)
    claims = {claim['id']: claim for claim in records(document, 'claims') if isinstance(claim.get('id'), str)}

    def proposition(claim):
        value = nodes.get(claim.get('proposition')) if isinstance(claim.get('proposition'), str) else None
        return value if isinstance(value, dict) else {}

    def triple(claim):
        value = proposition(claim)
        return tuple(value.get(key) for key in ('subject_entity', 'relation', 'object_entity'))

    def evidence(claim): return [nodes[ref] for ref in refs(claim, 'has_evidence') if isinstance(nodes.get(ref), dict)]

    def scores(claim):
        return [score for score in [nodes.get(ref) for ref in refs(claim, 'has_score')] + records(claim, 'scores')
                if isinstance(score, dict) and isinstance(score.get('metric'), str)]

    def classify(claim):
        if any(refs(item, 'source_claims') for item in evidence(claim)): return 'synthesis'
        subject, relation, obj = triple(claim)
        group = predicate_group(relation)[0] if isinstance(relation, str) else None
        pair = frozenset((kind_of(subject), kind_of(obj)))
        if group not in (None, 'involved') and pair in PAIRS: return PAIRS[pair]
        found = [METRIC_FAMILY[score['metric']] for score in scores(claim) if score['metric'] in METRIC_FAMILY]
        if found: return min(found, key=list(FAMILIES).index)
        return RELATION_FAMILY.get(text(relation), 'other')

    def predicate(group):
        curie, iri, _ = PREDICATES[group]
        return curie if curie.split(':')[0] in DECLARED or curie.split(':')[0] in prefixes else iri

    def triple_text(family):
        subject, group, obj = FAMILIES[family][:3]
        return f'subject_entity {ENDPOINTS[subject]}, relation {predicate(group)}, object_entity {ENDPOINTS[obj]}'

    def atomic(claim, family):
        subject_kind, group, object_kind, metrics, score_kind = FAMILIES[family][:5]
        subject, relation, obj = triple(claim)
        issues = [] if proposition(claim).get('proposition_kind') == 'RESULT' else ['atomic-result-kind']
        kinds = (kind_of(subject), kind_of(obj))
        if not all(isinstance(value, str) and value for value in (subject, relation, obj)): issues.append('atomic-triple')
        else:
            if kinds == (object_kind, subject_kind): issues.append('atomic-orientation')
            elif kinds != (subject_kind, object_kind) or trusted and any(
                    kind_of(value) in ('gene_set', 'factor') and value not in trusted for value in (subject, obj)):
                issues.append('atomic-endpoints')
            if relation not in PREDICATES[group][:2]: issues.append('atomic-predicate')
        expected = [score for score in scores(claim) if score['metric'] in metrics and score.get('score_kind') == score_kind]
        if metrics and not expected: issues.append('atomic-score')
        derived = {ref for item in evidence(claim) for ref in refs(item, 'was_derived_from')}
        sets = {value for value in (subject, obj) if kind_of(value) == 'gene_set'}
        captured = bool(derived & files) or any(ref.startswith('dapper:File.') for ref in derived)
        if not captured or subject_kind == 'gene_set' and not (derived & sets if sets else any(ref.startswith('dapper:GeneSet.') for ref in derived)):
            issues.append('atomic-capture')
        values = {}
        for score in scores(claim): values.setdefault(score['metric'], set()).add(repr(score.get('value')))
        if len({METRIC_FAMILY[metric] for metric in values if metric in METRIC_FAMILY}) > 1 or any(len(seen) > 1 for seen in values.values()):
            issues.append('atomic-single-fact')
        statement = text(proposition(claim).get('statement')) + ' ' + text(claim.get('statement'))
        if expected and not any(mentions(statement, score.get('value')) for score in expected): issues.append('atomic-value-in-statement')
        return issues

    families = {identity: classify(claim) for identity, claim in claims.items()}

    def atoms_of(claim, seen):
        found = []
        for item in evidence(claim):
            for ref in refs(item, 'source_claims'):
                if ref in seen or ref not in families: continue
                seen.add(ref)
                if families[ref] in FAMILIES: found.append(ref)
                elif families[ref] == 'synthesis': found.extend(atoms_of(claims[ref], seen))
        return found

    def path(atoms):
        parent, complete = {}, True
        def root(value):
            parent.setdefault(value, value)
            while parent[value] != value: value = parent[value]
            return value
        for atom in atoms:
            subject, _, obj = (entity(value) for value in triple(claims[atom]))
            if subject is None or obj is None: complete = False; continue
            parent[root(subject)] = root(obj)
        return set(parent), complete and len({root(value) for value in list(parent)}) == 1

    authored = [claim for identity, claim in claims.items() if identity not in trusted]
    grouped, counts = {}, {family: {'count': 0, 'conformant': 0} for family in FAMILIES}
    synthesis = {'count': 0, 'coherent': 0, 'gap_relevance': 0}
    for claim in authored:
        family = families[claim['id']]
        if family in FAMILIES:
            issues = atomic(claim, family)
            counts[family]['count'] += 1; counts[family]['conformant'] += not issues
        elif family == 'synthesis':
            atoms = atoms_of(claim, {claim['id']})
            entities, connected = path(atoms)
            subject, relation, obj = triple(claim)
            issues = [] if len({families[atom] for atom in atoms}) >= 2 else ['synthesis-families']
            own = {entity(value) for value in (subject, obj) if kind_of(value) not in (None, 'gap')}
            if atoms and not (connected and own <= entities): issues.append('synthesis-path')
            synthesis['count'] += 1; synthesis['coherent'] += not issues
            if isinstance(relation, str) and predicate_group(relation) == ('involved', False): issues.append('synthesis-predicate')
            if relation == GAP_RELATION or atoms and not any((subject, relation, obj)) and proposition(claim).get('proposition_kind') == 'BIOLOGICAL_INTERPRETATION':
                synthesis['gap_relevance'] += 1
                if relation == GAP_RELATION and gap and obj != gap: issues.append('gap-target')
            fitted = {traits.get(value) for kind, value in entities if kind == 'factor'}
            words = ' '.join(text(value) for value in (proposition(claim).get('statement'), proposition(claim).get('scope'), claim.get('statement')))
            if fitted & {value for kind, value in entities if kind == 'trait'} and not WITHIN_FIT.search(words):
                issues.append('synthesis-within-fit')
            family = None
        else: continue
        for check in issues: grouped.setdefault((check, family), []).append(claim['id'])
    atomic_count = sum(value['count'] for value in counts.values()); conformant = sum(value['conformant'] for value in counts.values())
    if atomic_count and not synthesis['gap_relevance']: grouped[('gap-relevance-missing', None)] = []
    missing = [family for family in FAMILIES if not counts[family]['count']]
    if authored and missing: grouped[('family-missing', None)] = []
    structured = atomic_count + synthesis['count']
    summary = {'format': FORMAT, 'claims': len(authored), 'families': counts, 'atomic': {'count': atomic_count, 'conformant': conformant},
               'synthesis': synthesis, 'other': len(authored) - structured,
               'conformance_rate': round((conformant + synthesis['coherent']) / structured, 3) if structured else None, 'issues': {}}
    for (check, _), ids in grouped.items(): summary['issues'][check] = summary['issues'].get(check, 0) + (len(ids) or 1)
    present = sum(bool(value['count']) for value in counts.values())
    suggestions = [{'severity': 'suggestion', 'check': 'claim-structure-summary', 'where': where,
        'message': f"{len(authored)} Claims: {atomic_count} atomic ({conformant} follow the recommended structure) in {present} of "
                   f"{len(FAMILIES)} families; {synthesis['count']} synthesis ({synthesis['coherent']} coherent, "
                   f"{synthesis['gap_relevance']} gap relevance); {summary['other']} other.",
        'repair': 'Optional: one atomic RESULT Claim per observation (subject-relation-object triple plus exact ClaimScore), then synthesis '
                  'Claims citing them through EvidenceItem.source_claims. KG and literature Claims remain welcome.', 'why': WHY}] if authored else []
    for check, family in sorted(grouped, key=lambda key: (ORDER.index(key[0]), list(FAMILIES).index(key[1]) if key[1] else -1)):
        ids = grouped[(check, family)]
        spec = FAMILIES.get(family)
        values = {'triple': triple_text(family), 'predicate': predicate(spec[1]), 'metrics': ' or '.join(spec[3]), 'kind': spec[4],
                  'label': spec[5][0].upper() + spec[5][1:], 'template': TEMPLATES[family],
                  'sets': ' and the trusted GeneSet' if spec[0] == 'gene_set' else ''} if spec else {
                  'families': ', '.join(FAMILIES[name][5] for name in missing),
                  'tools': '; '.join(f'{FAMILIES[name][5]} via {FAMILIES[name][6]}' for name in missing)}
        message, repair = (part.format(**values) for part in MESSAGES[check])
        item = {'severity': 'suggestion', 'check': check, 'where': ids[0] if ids else where, 'message': message, 'repair': repair, 'why': WHY}
        if family: item['family'] = family
        if ids: item.update(claims=ids[:MAX_CLAIMS], count=len(ids))
        suggestions.append(item)
    return {'summary': summary, 'suggestions': suggestions}
