"""Focused authoring feedback from exact captured entities, never gene-name guesses.

These are advisory scientific repairs, not a biological correctness gate. Only
trusted entity IDs observed at selected source locators drive the checks.
"""
from pathlib import Path
import re

from .evidence_package import decode, pointer, require, sha256


def reachable_entities(document, evidence_id):
    """Follow the profile's evidence and assessed-relationship paths, cycle safely."""
    nodes = {node['id']: node for rows in document.values() if isinstance(rows, list)
             for node in rows if isinstance(node, dict) and isinstance(node.get('id'), str)}
    pending, seen = [evidence_id], set()
    fields = ('was_derived_from', 'source_claims', 'has_evidence', 'target_proposition',
              'proposition', 'subject_entity', 'object_entity')
    while pending:
        identity = pending.pop()
        if identity in seen: continue
        seen.add(identity)
        node = nodes.get(identity, {})
        for field in fields:
            value = node.get(field, [])
            pending.extend(ref for ref in (value if isinstance(value, list) else [value]) if isinstance(ref, str))
    return seen


def relationship_advisories(document, package, package_path):
    trusted = {node['id']: (group, node) for group, rows in package.get('dapper_context', {}).items()
               if isinstance(rows, list) for node in rows if isinstance(node, dict) and isinstance(node.get('id'), str)}
    entities = {identity: value for identity, value in trusted.items() if value[0] in ('gene_sets', 'mechanisms')}
    if not entities: return []
    nodes = {node['id']: node for rows in document.values() if isinstance(rows, list)
             for node in rows if isinstance(node, dict) and isinstance(node.get('id'), str)}
    sources = {value['dapper_file_id']: value for value in package.get('source_artifacts', {}).values()}
    factor_ids = {source: record.get('dapper_id') for source, record in package.get('pigean', {}).get('mechanisms', {}).items()}
    for record in package.get('reference_objects', []):
        if record.get('status') == 'ready':
            for key in ('id', 'source_key'): factor_ids[record.get(key)] = record.get('dapper_id')
    root = Path(package_path).resolve().parent
    cache, findings = {}, []

    def advisory(check, where, message):
        value = {'severity': 'advisory', 'check': check, 'where': where, 'message': message,
                 'why': 'Scientific relationship guidance does not affect validation, including strict mode.'}
        if value not in findings: findings.append(value)

    def captured(file_id):
        if file_id not in cache:
            artifact = sources[file_id]
            if artifact.get('format') != 'json': return None
            path = (root / artifact['path']).resolve()
            require(path.is_relative_to(root), 'Source artifact path escape')
            raw = path.read_bytes()
            require(sha256(raw) == artifact['sha256'], 'Captured source checksum changed')
            cache[file_id] = decode(raw)
        return cache[file_id]

    def observed_ids(value):
        result = set()
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('gene_set_id', 'id', 'dapper_id', 'member') and isinstance(child, str) and child in entities:
                    result.add(child)
                elif isinstance(child, (dict, list)): result.update(observed_ids(child))
        elif isinstance(value, list):
            for child in value: result.update(observed_ids(child))
        return result

    for item in document.get('evidence_items', []):
        if not isinstance(item, dict): continue
        identity = item.get('id')
        proposition = nodes.get(item.get('target_proposition'), {})
        text = ' '.join(item.get(field, '') for field in ('context', 'source_locator') if isinstance(item.get(field, ''), str))
        locators = list(dict.fromkeys(re.findall(r'/(?:data|result|response|content|structuredContent|segments)(?:/[A-Za-z0-9_~.%-]+)*', text)))
        selected, operations, captures = set(), set(), []
        for file_id in item.get('was_derived_from', []):
            if file_id not in sources: continue
            source = captured(file_id)
            if not isinstance(source, dict): continue
            captures.append(source)
            operation = source.get('operation')
            if isinstance(operation, str): operations.add(operation)
            for locator in locators:
                for candidate in dict.fromkeys((locator, locator.rstrip('.'))):
                    try:
                        selected.update(observed_ids(pointer(source, candidate)))
                        break
                    except ValueError: pass
            # The selected reader operation itself binds the exact set/factor.
            if source.get('source_mode') == 'imported_reference':
                arguments = source.get('arguments', {})
                set_id = arguments.get('gene_set_id')
                if set_id in entities: selected.add(set_id)
                factor_id = factor_ids.get(arguments.get('factor_id'))
                if factor_id in entities: selected.add(factor_id)
        reachable = reachable_entities(document, identity)
        missing = selected - reachable
        sets = sorted(ref for ref in missing if entities[ref][0] == 'gene_sets')
        if sets:
            advisory('geneset-provenance-missing', identity,
                'Selected evidence uses trusted GeneSet(s) ' + ', '.join(sets) +
                '. Retain a path through target_proposition or source_claims, or include the exact GeneSet alongside the captured File in was_derived_from when its definition informed this evidence. An unrelated GeneSet or a name in prose does not supply this path.')
        if missing and not any(proposition.get(field) for field in ('subject_entity', 'relation', 'object_entity')):
            # Text-only synthesis is valid; only an explicit mention of a known
            # selected entity triggers the suggestion to assess it separately.
            prose = ' '.join(str(proposition.get(field, '')) for field in ('statement', 'scope'))
            explicit = [ref for ref in sorted(missing) if ref in prose or
                        (len(entities[ref][1].get('name', '')) >= 6 and entities[ref][1]['name'] in prose)]
            if explicit:
                advisory('entity-reference-missing', proposition.get('id', identity),
                    'This entity-centered assertion names ' + ', '.join(explicit) +
                    ' only in prose. Consider a supported structured Proposition with subject_entity, relation and object_entity together; preserve valid free-form synthesis separately.')
        relation = proposition.get('relation', '')
        membership = isinstance(relation, str) and bool(re.search(r'(?:member[-_]?of|has[-_]?member|membership)$', relation, re.I))
        if membership:
            subject, obj = proposition.get('subject_entity'), proposition.get('object_entity')
            gene, gene_set_id = (obj, subject) if subject in entities and entities[subject][0] == 'gene_sets' else (subject, obj)
            if gene_set_id in entities and entities[gene_set_id][0] == 'gene_sets':
                members = entities[gene_set_id][1].get('members')
                if isinstance(members, list) and gene not in members:
                    advisory('membership-identity-unverified', identity,
                        f'The asserted gene {gene} is not an exact member identifier in trusted GeneSet {gene_set_id}. Inspect exact membership and verified identifier mappings. A perturbation target or co-loading gene does not establish signature membership.')
        if membership and operations and operations <= {'get_factor_loadings', 'get_gene_factors', 'get_gene_set_factors', 'get_factor'}:
            advisory('relationship-support-missing', identity,
                'Loading or co-loading observations do not establish GeneSet membership, and a named perturbation target is not automatically a signature member. Retrieve get_gene_set and exact get_gene_set_members evidence, or reuse a supported source Claim; narrow the relationship if membership is unobserved.')
        for source in captures:
            result = source.get('result', {})
            statement = str(proposition.get('statement', ''))
            window_scoped = bool(re.search(r'\b(?:inspected|returned)\s+(?:window|page|top\b|first\b)|\btop[- ]\d+\b', statement, re.I))
            if result.get('truncated') and not window_scoped and re.search(r'\b(?:absent|absence|exclusive|exclusively|no stored|does not belong)\b', statement, re.I):
                advisory('query-scope-overclaim', proposition.get('id', identity),
                    'A cited result is partial/truncated. Absence or exclusivity must be narrowed to the inspected window or supported by a complete targeted query for each asserted factor; one factor query does not cover another fit.')
    # A narrowly targeted empty probe cannot be generalized to "both factors".
    # Count captured factor scopes across the whole assessment, not per row.
    for claim in document.get('claims', []):
        if not isinstance(claim, dict): continue
        proposition = nodes.get(claim.get('proposition'), {})
        statement = str(proposition.get('statement', ''))
        if not (re.search(r'\b(?:both|either|all)\b.*\bfactors?\b', statement, re.I) and
                re.search(r'\b(?:absent|absence|no stored|no nonzero)\b', statement, re.I)): continue
        scopes = set()
        for evidence_id in claim.get('has_evidence', []):
            for file_id in reachable_entities(document, evidence_id) & set(sources):
                source = captured(file_id)
                if not isinstance(source, dict) or source.get('operation') != 'get_factor_loadings': continue
                if source.get('result', {}).get('status') not in ('empty', 'complete') or source['result'].get('truncated'): continue
                args = source.get('arguments', {})
                if args.get('q') and args.get('factor_id'): scopes.add(args['factor_id'])
        if len(scopes) < 2:
            advisory('query-scope-overclaim', proposition.get('id', claim.get('id')),
                'The assertion spans multiple factors, but its evidence contains fewer than two complete targeted factor scopes. A Factor1 probe does not establish absence on Factor2; narrow the Proposition or capture each asserted scope.')
    return findings
