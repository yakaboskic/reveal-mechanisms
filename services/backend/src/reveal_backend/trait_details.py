"""Public reference traits, trait -> gene-set betas, factor -> phenotype links and minted Mechanism ids of the published release.

These reads use the release tables only (reveal_ref_*): no research jobs, account graphs, embeddings or remote calls.
Every request reads the environment's ref_release row first on its pooled reference lease (reference_pin), so all of
its reads are of that one release; release_id is in every response and every cache key. A trait's facts and a
listing's row count are facts of the immutable release, read once per process. Portal links are built from stored ids,
never fetched. Mechanism ids resolve through the catalog, which serves the same release.
"""
from collections import OrderedDict
import json
import re
import threading
from urllib.parse import quote

from .auth import Problem
from .catalog import MECHANISM_ID_RE
from .reference_generation import KPN_TRAIT_RE
from .reference_pin import current_release
from .repository import application_prefix, application_sql
from .runtime_config import reference_mysql_connection, setting

PORTAL_URL = 'https://translator.broadinstitute.org/artifact_provenance'  # REVEAL_PROVENANCE_PORTAL_URL overrides it
KPN_URL = 'https://broadinstitute.github.io/kpn-data-models/kpn.trait/{}/'
GENE_SET_ID = re.compile(r'dapper:GeneSet\.[A-Za-z0-9_-]{32}')
BETA_SORTS = ('beta_uncorrected', 'beta', 'avg_postp')  # all descending
LINK_SORTS = {'phewas_q': 'l.phewas_q', 'phewas_beta': 'l.phewas_beta DESC', 'nnls_loading': 'l.nnls_loading DESC'}
LINK_SUMMARY = ('n_phenotypes', 'n_linked', 'n_linked_kpn_traits', 'n_linked_atlas_traits', 'own_trait_q')
BETA_COVERAGE = ("Gene-set betas from PIGEAN betas mode (one fit per CFDE library on the trait's PIGEAN gene statistics, no outer Gibbs), "
                 'for the gene sets PIGEAN kept. A trait and gene set without a row were not fitted: an absent pair is not a zero beta. '
                 'Some kept rows are exactly 0.')
LINK_COVERAGE = ('Factor-phenotype links from the eaggl trait linkage and factor-PheWAS, kept at BH q <= 0.05 over all factor x phenotype '
                 'tests. A phenotype absent for a factor is unlinked at this threshold, not a negative result.')
# A release without an optional file (an older build) publishes its table empty: the routes say so instead of listing nothing.
ABSENT = {'trait_gene_sets.tsv.gz': 'gene-set betas', 'factor_trait_links.tsv.gz': 'factor-trait links'}
CACHE_ENTRIES = 8192  # small facts keyed by the immutable release id
_facts, _cache_lock = OrderedDict(), threading.Lock()

TRAIT_SQL = ('SELECT legacy_phenotype_id,phenotype_name,gwas_source_category,trait_group,trait_type,description,metadata'
             ' FROM reveal_ref_traits WHERE kpn_trait_id=%s')
PHENOTYPE_SQL = 'SELECT phenotype,display_name,legacy_phenotype_id,category,metadata FROM reveal_ref_phenotypes WHERE kpn_trait_id=%s'
# (table, page SELECT): every row of the table joins its gene set or factor and trait (the build writes them together),
# so a listing's total is a count of the table alone.
BETAS = ('reveal_ref_trait_gene_sets t', 'SELECT t.kpn_trait_id,r.phenotype_name,t.gene_set_id,g.gene_set_name,t.library,g.collection_id,'
         'g.n_genes,t.beta_uncorrected,t.beta,t.avg_postp,t.library_rank FROM reveal_ref_trait_gene_sets t'
         ' JOIN reveal_ref_gene_sets g ON g.gene_set_id=t.gene_set_id JOIN reveal_ref_traits r ON r.kpn_trait_id=t.kpn_trait_id WHERE ')
LINKS = ('reveal_ref_factor_trait_links l', 'SELECT f.public_id,f.label,f.kpn_trait_id,r.phenotype_name,l.phenotype,l.phenotype_kpn_trait_id,'
         'COALESCE(p.display_name,l.phenotype),l.is_own_trait,l.is_atlas_trait,l.nnls_loading,l.phewas_beta,l.phewas_p_onesided,l.phewas_q'
         ' FROM reveal_ref_factor_trait_links l JOIN reveal_ref_factors f ON f.factor_key=l.factor_key'
         ' JOIN reveal_ref_traits r ON r.kpn_trait_id=f.kpn_trait_id LEFT JOIN reveal_ref_phenotypes p ON p.phenotype=l.phenotype WHERE ')


def _immutable(key, compute):
    """A fact of the published release (its id leads the key); failures, a 404 included, are never cached."""
    with _cache_lock:
        if key in _facts:
            _facts.move_to_end(key); return _facts[key]
    value = compute()
    with _cache_lock:
        _facts[key] = value; _facts.move_to_end(key)
        while len(_facts) > CACHE_ENTRIES: _facts.popitem(last=False)
    return value


def clear_caches():
    with _cache_lock: _facts.clear()


def _json(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else (value or {})


def _read(handler):
    """handler(rows, release) on one pooled reference lease whose first read, the ref_release row, pins its release."""
    connection = reference_mysql_connection()
    try:
        try: release = current_release(connection)
        except ValueError: raise Problem(503, 'SOURCE_NOT_READY', 'The published reference release is malformed.') from None
        if not release: raise Problem(503, 'SOURCE_NOT_READY', 'No reference release is published for this environment.')
        def rows(sql, args=()):
            with connection.cursor() as cursor:
                cursor.execute(application_sql(sql, application_prefix()), args)
                return cursor.fetchall()
        return handler(rows, release)
    finally:
        connection.close()


def _included(release, name):
    if name in (release['manifest'].get('absent_files') or ()):
        raise Problem(404, 'NOT_FOUND', f'The published reference release does not include {ABSENT[name]}.')


def _page(rows, release, listing, where, args, ordering, limit, offset):
    """(rows of one page, total): the total is a fact of the release; a page past it reads nothing more."""
    table, select = listing
    total = _immutable((release['release_id'], table, where, args),
                       lambda: int(rows('SELECT COUNT(*) FROM ' + table + ' WHERE ' + where, args)[0][0]))
    return (rows(select + where + ' ORDER BY ' + ordering + ' LIMIT %s OFFSET %s', args + (limit, offset)) if offset < total else []), total


def _portal():
    return (setting('REVEAL_PROVENANCE_PORTAL_URL') or PORTAL_URL).rstrip('/')


def _gwas(line):
    """The trait's portal GWAS (metadata.bottom_line): its status, the artifact the GWAS route uses and every artifact, as links."""
    line, portal = line or {}, _portal()
    trait, artifact = line.get('portal_trait_id'), line.get('gwas_artifact')
    return {'status': 'available' if artifact else line.get('gwas_reason') or 'not_on_portal', 'portal_trait_id': trait,
            'gwas_artifact_id': artifact['artifact_id'] if artifact else None,
            'portal_href': f"{portal}/bottom_line/{quote(trait, safe='')}" if trait else None,
            'artifacts': [{'ancestry_code': item['ancestry_code'], 'ancestry_name': item['ancestry_name'], 'artifact_id': item['artifact_id'],
                           'href': f"{portal}/artifact/{quote(item['artifact_id'], safe='')}"} for item in line.get('artifacts') or ()]}


def _trait_facts(rows, kpn):
    trait, phenotype = rows(TRAIT_SQL, (kpn,)), rows(PHENOTYPE_SQL, (kpn,))
    if not trait and not phenotype: raise Problem(404, 'NOT_FOUND', 'The trait is not in the published reference release.')
    facts = {'factors': [{'source_id': source, 'label': label, 'factor_number': int(number)} for source, label, number in
                         rows('SELECT public_id,label,factor_number FROM reveal_ref_factors WHERE kpn_trait_id=%s ORDER BY factor_number', (kpn,))],
             'gene_set_libraries': [{'library': library, 'n_gene_sets': int(count), 'n_nonzero': int(nonzero or 0)} for library, count, nonzero in
                                    rows('SELECT library,COUNT(*),SUM(beta_uncorrected<>0) FROM reveal_ref_trait_gene_sets WHERE kpn_trait_id=%s'
                                         ' GROUP BY library ORDER BY library', (kpn,))],
             'linked_factors': {'n_factors': int(rows('SELECT COUNT(*) FROM reveal_ref_factor_trait_links'
                                                      ' WHERE phenotype_kpn_trait_id=%s AND is_own_trait=0', (kpn,))[0][0])}}
    if trait:  # an atlas trait: its own row wins; without a phenotype row its phenotype is its EAGGL phenotype code
        legacy, name, category, group, kind, description, metadata = trait[0]
        return {**facts, 'phenotype': phenotype[0][0] if phenotype else legacy, 'name': name, 'category': category, 'is_atlas': True,
                'legacy_phenotype_id': legacy, 'trait_group': group, 'trait_type': kind, 'description': description,
                'bottom_line': _json(metadata).get('bottom_line')}
    identity, name, legacy, category, metadata = phenotype[0]
    return {**facts, 'phenotype': identity, 'name': name, 'category': category, 'is_atlas': False, 'legacy_phenotype_id': legacy,
            'trait_group': None, 'trait_type': None, 'description': None, 'bottom_line': _json(metadata).get('bottom_line')}


def trait_detail(kpn_trait_id):
    """GET /v1/traits/{kpn_trait_id}: an atlas trait, or a phenotype the linkage linked factors to (is_atlas false)."""
    match = KPN_TRAIT_RE.fullmatch(kpn_trait_id or '')
    if not match: raise Problem(404, 'NOT_FOUND', 'The trait is not in the published reference release.')
    def read(rows, release):
        facts = _immutable((release['release_id'], 'trait', kpn_trait_id), lambda: _trait_facts(rows, kpn_trait_id))
        return {'kpn_trait_id': kpn_trait_id, **{key: facts[key] for key in ('phenotype', 'name', 'category', 'is_atlas', 'legacy_phenotype_id',
                'trait_group', 'trait_type', 'description')}, 'kpn_url': KPN_URL.format(match.group(1)), 'gwas': _gwas(facts['bottom_line']),
                'factors': [dict(item) for item in facts['factors']], 'gene_set_libraries': [dict(item) for item in facts['gene_set_libraries']],
                'linked_factors': dict(facts['linked_factors']), 'release_id': release['release_id']}
    return _read(read)


def trait_gene_sets(*, trait_id=None, gene_set_id=None, library=None, sort='beta_uncorrected', limit=50, offset=0):
    """GET /v1/trait-gene-sets: a trait's gene-set betas, or a gene set's betas across traits, best first."""
    if (trait_id is None) == (gene_set_id is None) or sort not in BETA_SORTS or not 1 <= limit <= 500 or offset < 0 \
            or (library is not None and not 1 <= len(library) <= 64) \
            or not (KPN_TRAIT_RE.fullmatch(trait_id) if trait_id is not None else GENE_SET_ID.fullmatch(gene_set_id)):
        raise Problem(422, 'INVALID_QUERY', 'Give exactly one of trait_id or gene_set_id, with a valid library, sort and page.')
    where, args = ('t.kpn_trait_id=%s', (trait_id,)) if trait_id is not None else ('t.gene_set_id=%s', (gene_set_id,))
    if library is not None: where, args = where + ' AND t.library=%s', args + (library,)
    def read(rows, release):
        _included(release, 'trait_gene_sets.tsv.gz')
        # Descending on the metric; ties keep the library's own rank (by beta_uncorrected), then the ids.
        page, total = _page(rows, release, BETAS, where, args, f't.{sort} DESC,t.library_rank,t.gene_set_id,t.kpn_trait_id', limit, offset)
        items = [{'kpn_trait_id': trait, 'trait_name': name, 'gene_set_id': identity, 'gene_set_name': label, 'library': set_library,
                  'collection_id': collection, 'n_genes': int(size), 'beta_uncorrected': float(uncorrected), 'beta': float(beta),
                  'avg_postp': float(postp), 'library_rank': int(rank)}
                 for trait, name, identity, label, set_library, collection, size, uncorrected, beta, postp, rank in page]
        next_offset = offset + len(items)
        return {'trait_id': trait_id, 'gene_set_id': gene_set_id, 'library': library, 'sort': sort, 'items': items, 'total': total,
                'next_offset': next_offset if items and next_offset < total else None, 'coverage': BETA_COVERAGE, 'release_id': release['release_id']}
    return _read(read)


def _factor(rows, release, source_id):
    """(factor_key, metadata.trait_links summary or None) of a factor the release serves; 404 otherwise."""
    def read():
        found = rows('SELECT factor_key,metadata FROM reveal_ref_factors WHERE public_id=%s', (source_id,))
        if not found: raise Problem(404, 'NOT_FOUND', 'The factor is not in the published reference release.')
        links = _json(found[0][1]).get('trait_links')
        return found[0][0], {key: links.get(key) for key in LINK_SUMMARY} if links else None
    return _immutable((release['release_id'], 'factor', source_id), read)


def factor_trait_links(*, source_id=None, trait_id=None, scope='all', sort='phewas_q', limit=50, offset=0):
    """GET /v1/factor-trait-links: a factor's linked phenotypes, or the factors linked to a trait (its own factors flagged)."""
    if (source_id is None) == (trait_id is None) or scope not in ('atlas', 'all') or sort not in LINK_SORTS or not 1 <= limit <= 500 \
            or offset < 0 or (source_id is not None and not 1 <= len(source_id) <= 255) or (trait_id is not None and not KPN_TRAIT_RE.fullmatch(trait_id)):
        raise Problem(422, 'INVALID_QUERY', 'Give exactly one of source_id or trait_id, with a valid scope, sort and page.')
    def read(rows, release):
        _included(release, 'factor_trait_links.tsv.gz')
        summary = None
        if source_id is not None:
            key, summary = _factor(rows, release, source_id)
            where, args = 'l.factor_key=%s', (key,)
        else: where, args = 'l.phenotype_kpn_trait_id=%s', (trait_id,)
        if scope == 'atlas': where += ' AND l.is_atlas_trait=1'
        # q ascending, effect and loading descending; ties by phenotype, then factor (the trait form's rows share one phenotype).
        page, total = _page(rows, release, LINKS, where, args, LINK_SORTS[sort] + ',l.phenotype,l.factor_key', limit, offset)
        items = [{'source_id': source, 'factor_label': label, 'fitted_kpn_trait_id': fitted, 'fitted_trait_name': fitted_name,
                  'phenotype': phenotype, 'phenotype_kpn_trait_id': kpn or None, 'phenotype_name': name, 'is_own_trait': bool(own),
                  'is_atlas_trait': bool(atlas), 'nnls_loading': float(nnls), 'phewas_beta': float(beta), 'phewas_p_onesided': float(p),
                  'phewas_q': float(q)} for source, label, fitted, fitted_name, phenotype, kpn, name, own, atlas, nnls, beta, p, q in page]
        next_offset = offset + len(items)
        return {'source_id': source_id, 'trait_id': trait_id, 'scope': scope, 'sort': sort, 'summary': dict(summary) if summary else None,
                'items': items, 'total': total, 'next_offset': next_offset if items and next_offset < total else None,
                'coverage': LINK_COVERAGE, 'release_id': release['release_id']}
    return _read(read)


def mechanism_object(catalog, dapper_id):
    """GET /v1/mechanism-objects/{dapper_id}: the served factor a minted Mechanism id names, 410 with the frozen factor when a
    superseded release's factor minted it (the environment's archive index), 404 otherwise."""
    catalog.load()
    with catalog.lock:  # the factors and their release swap together on a reload
        release = catalog.release_id
        index = _immutable((release, 'mechanisms'), lambda: {record['object']['id']: source for source, record in catalog.factors.items()})
    valid = isinstance(dapper_id, str) and MECHANISM_ID_RE.fullmatch(dapper_id)
    if valid and dapper_id in index: return {'dapper_id': dapper_id, 'status': 'current', 'source_id': index[dapper_id], 'release_id': release}
    lookup = getattr(catalog, 'archived_for_mechanism', None)
    archived = lookup(dapper_id) if valid and lookup else None
    if archived:
        raise Problem(410, 'REFERENCE_GENERATION_SUPERSEDED', 'This Mechanism names a factor the published reference release no longer serves.',
                      archived_reference_factor=archived)
    raise Problem(404, 'NOT_FOUND', 'The Mechanism is not a factor of the published reference release.')
