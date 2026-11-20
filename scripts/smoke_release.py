"""Read-only smoke test of a published reference release through a running API (local stack, QA or prod).

    python scripts/smoke_release.py [--base-url http://127.0.0.1:18001] [--trait KPN.TRAIT:0000398] [--account dapper:ScientificAccount.…]

Only GETs of public routes: the trait page and its gene-set betas, a factor's cross-trait links, the factor explorer's
library filter, a catalog gene set's lineage and portal links, a Mechanism id lookup and, with --account, a published
account's provenance trace. Every response is validated against api/openapi.json. Prints one line per check and exits 1
when any check fails.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / 'api/openapi.json').read_text())


class Smoke:
    def __init__(self, base_url):
        self.base, self.failures = base_url.rstrip('/'), 0

    def get(self, path, schema, status=200, **params):
        url = self.base + path + ('?' + urlencode(params) if params else '')
        started = time.monotonic()
        try:
            with urlopen(url, timeout=60) as response: code, body = response.status, response.read()
        except HTTPError as error: code, body = error.code, error.read()
        result = json.loads(body) if body else None
        if code != status: raise AssertionError(f'GET {path} -> {code}, expected {status}: {body[:300]!r}')
        Draft202012Validator({'$ref': '#/components/schemas/' + schema, **CONTRACT}).validate(result)
        return result, time.monotonic() - started

    def check(self, name, function):
        try:
            detail = function()
            print(f'ok    {name}: {detail}')
        except Exception as error:
            self.failures += 1
            print(f'FAIL  {name}: {type(error).__name__}: {error}')


def descending(values): return all(a >= b for a, b in zip(values, values[1:]))


def run(base_url, trait_id, account_id):
    smoke, state = Smoke(base_url), {}

    def ready():
        with urlopen(smoke.base + '/readyz', timeout=30) as response: return f'HTTP {response.status}'

    def trait():
        result, seconds = smoke.get('/v1/traits/' + quote(trait_id, safe=':'), 'TraitDetail')
        assert result['factors'], 'no fitted factors'
        state.update(release=result['release_id'], factor=result['factors'][0]['source_id'])
        libraries = {entry['library']: entry['n_gene_sets'] for entry in result['gene_set_libraries']}
        return (f"{result['name']}: {len(result['factors'])} factors, betas in {len(libraries)} libraries, GWAS {result['gwas']['status']}, "
                f"{result['linked_factors']['n_factors']} other traits' factors linked; release {result['release_id'][:12]} ({seconds:.2f}s)")

    def betas():
        result, seconds = smoke.get('/v1/trait-gene-sets', 'TraitGeneSets', trait_id=trait_id, limit=25)
        values = [item['beta_uncorrected'] for item in result['items']]
        assert result['total'] > 0 and descending(values), f'not sorted by beta_uncorrected: {values[:5]}'
        state['gene_set'] = result['items'][0]['gene_set_id']
        top = result['items'][0]
        return f"{result['total']} gene sets; top {top['gene_set_name']} ({top['library']}, beta_uncorrected {top['beta_uncorrected']}) ({seconds:.2f}s)"

    def reverse():
        result, seconds = smoke.get('/v1/trait-gene-sets', 'TraitGeneSets', gene_set_id=state['gene_set'], limit=10)
        assert any(item['kpn_trait_id'] == trait_id for item in result['items']) or result['total'] > 10, 'the trait is not among its gene set\'s traits'
        return f"{result['total']} traits for {state['gene_set']} ({seconds:.2f}s)"

    def links():
        result, seconds = smoke.get('/v1/factor-trait-links', 'FactorTraitLinks', source_id=state['factor'], limit=25)
        q = [item['phewas_q'] for item in result['items']]
        assert q == sorted(q), 'not sorted by phewas_q'
        return f"{state['factor']}: {result['total']} linked phenotypes, summary {result['summary']} ({seconds:.2f}s)"

    def linked_factors():
        result, seconds = smoke.get('/v1/factor-trait-links', 'FactorTraitLinks', trait_id=trait_id, scope='atlas', limit=10)
        return f"{result['total']} factors linked to {trait_id} ({seconds:.2f}s)"

    def factor():
        result, seconds = smoke.get('/v1/factors/' + quote(state['factor'], safe=':'), 'FactorDetail')
        assert result.get('trait_links'), 'FactorDetail.trait_links missing'
        return f"trait_links n_linked {result['trait_links']['n_linked']} ({seconds:.2f}s)"

    def library():
        everything, _ = smoke.get('/v1/factor-loadings', 'FactorLoadings', source_id=state['factor'], kind='gene_set', limit=200)
        library = everything['items'][0]['library']
        result, seconds = smoke.get('/v1/factor-loadings', 'FactorLoadings', source_id=state['factor'], kind='gene_set', library=library, limit=200)
        assert result['items'] and {item['library'] for item in result['items']} == {library}, 'the library filter is not applied'
        return f"{library}: {result['total']} of the factor's projected gene sets ({seconds:.2f}s)"

    def gene_set():
        result, seconds = smoke.get('/v1/catalog/gene-sets/' + quote(state['gene_set'], safe=':'), 'CatalogGeneSet')
        provenance = result.get('provenance') or {}
        assert provenance.get('datasets'), 'no Dataset in the gene set\'s lineage'
        return (f"{len(provenance['datasets'])} datasets, {len(provenance.get('organizations') or [])} organizations, "
                f"{len(result.get('external_links') or [])} portal links ({seconds:.2f}s)")

    def mechanism():
        found, _ = smoke.get('/v1/mechanisms/' + quote(state['factor'], safe=':'), 'MechanismRecord')
        identity = (found.get('object') or {}).get('id') or (found.get('mechanism') or {}).get('id')
        assert identity, 'the factor record names no minted Mechanism id'
        result, seconds = smoke.get('/v1/mechanism-objects/' + quote(identity, safe=':'), 'MechanismObject')
        assert result['source_id'] == state['factor'], f"{identity} resolves to {result['source_id']}"
        return f"{identity} -> {result['source_id']} ({seconds:.2f}s)"

    def trace():
        result, seconds = smoke.get('/v1/accounts/' + quote(account_id, safe=':') + '/provenance', 'AccountProvenance', limit=100)
        routes = sorted({route for trace in result['traces'] for route in trace.get('route_classifications') or []})
        return (f"{len(result['dataset_reuse'])} datasets, {len(result['organization_reuse'])} organizations, routes {routes}, "
                f"coverage {result['coverage'].get('lineage_resolution')} ({seconds:.2f}s)")

    for name, function in (('readyz', ready), ('trait page', trait), ('trait gene-set betas', betas), ('gene set traits', reverse),
                           ('factor-trait links', links), ('trait linked factors', linked_factors), ('factor detail', factor),
                           ('factor-loadings library filter', library), ('catalog gene set lineage', gene_set), ('Mechanism id lookup', mechanism)):
        smoke.check(name, function)
    if account_id: smoke.check('account provenance trace', trace)
    return smoke.failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--base-url', default='http://127.0.0.1:18001')
    parser.add_argument('--trait', default='KPN.TRAIT:0000398', help='A KPN trait of the release (default T2D)')
    parser.add_argument('--account', help='A published (or readable) dapper:ScientificAccount id to trace')
    args = parser.parse_args(argv)
    failures = run(args.base_url, args.trait, args.account)
    print(f'{failures} failed' if failures else 'all checks passed')
    return 1 if failures else 0


if __name__ == '__main__': sys.exit(main())
