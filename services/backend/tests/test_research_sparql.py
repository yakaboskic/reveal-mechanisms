import unittest

from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery

from reveal_backend.auth import Problem
from reveal_backend.box_mcp import GRAPHS
from reveal_backend.research_sparql import query_arguments


class ResearchSparqlTests(unittest.TestCase):
    def check(self, query, **extra):
        return query_arguments({'graph': 'prokn', 'query': query, **extra}, ['prokn'])

    def test_prefix_comments_literals_joins_and_nested_queries_remain_bounded(self):
        queries = [
            'PREFIX : <https://purl.org/okn/frink/kg/> SELECT ?s WHERE {GRAPH :prokn {?s ?p ?o}}',
            '\tPREFIX\tkg: <https://purl.org/okn/frink/kg/>\n\tSELECT ?s WHERE {GRAPH kg:prokn {?s ?p ?o}}',
            'PREFIX kg: <https://purl.org/okn/frink/kg/>\nSELECT ?s WHERE { GRAPH kg:prokn { ?s ?p ?o . OPTIONAL {?s ?p2 ?o2} FILTER(CONTAINS(STR(?o), "SERVICE FROM GRAPH")) } }',
            '# discovery\nSELECT (COUNT(?s) AS ?count) WHERE { GRAPH <'+GRAPHS['prokn']+'> {?s ?p ?o} } # tail comment',
            'SELECT ?s WHERE { GRAPH <'+GRAPHS['prokn']+'> { {SELECT ?s WHERE {?s ?p ?o} LIMIT 10} FILTER EXISTS {?s ?p2 ?o2} } }',
            'SELECT ?s WHERE { GRAPH <'+GRAPHS['prokn']+'> {?s ?p ?o} } VALUES ?s {<urn:example>}',
        ]
        for query in queries:
            with self.subTest(query=query):
                result = self.check(query, limit=7)
                algebra = translateQuery(parseQuery(result['query'])).algebra
                self.assertEqual(algebra.p.name, 'Slice')
                self.assertEqual(algebra.p.length, 7)
                self.assertTrue(result['query'].endswith('LIMIT 7'))
                self.assertIn(query.split('SELECT', 1)[1], result['query'])

    def test_read_scope_cannot_escape_through_nested_syntax(self):
        selected = '<'+GRAPHS['prokn']+'>'
        bodies = [
            '?s ?p ?o',
            'GRAPH ?graph {?s ?p ?o}',
            'GRAPH <'+GRAPHS['biomarkerkg']+'> {?s ?p ?o}',
            'GRAPH '+selected+' {?s ?p ?o} ?s ?other ?outside',
            'GRAPH '+selected+' {?s ?p ?o} FILTER EXISTS {?s ?p2 ?outside}',
            'GRAPH '+selected+' {?s ?p ?o FILTER EXISTS {SERVICE <https://untrusted.example> {?x ?y ?z}}}',
            'GRAPH '+selected+' {?s ?p ?o {SELECT ?x WHERE {GRAPH ?g {?x ?y ?z}}}}',
            'GRAPH '+selected+' {?s ?p ?o BIND(<https://untrusted.example/function>(?o) AS ?x)}',
        ]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(Problem):
                self.check('SELECT * WHERE {'+body+'}')
        for query in ('SELECT * FROM <urn:elsewhere> WHERE {GRAPH '+selected+' {?s ?p ?o}}',
                      'ASK {GRAPH '+selected+' {?s ?p ?o}}',
                      'DELETE WHERE {GRAPH '+selected+' {?s ?p ?o}}',
                      'SELECT ?x WHERE {VALUES ?x {1}}',
                      'SELECT * WHERE {GRAPH '+selected+' {?s ?p ?o}}; DROP ALL'):
            with self.subTest(query=query), self.assertRaises(Problem): self.check(query)

    def test_selected_graph_and_bounds_checked_before_transport(self):
        query = 'SELECT * WHERE {GRAPH <'+GRAPHS['prokn']+'> {?s ?p ?o}}'
        with self.assertRaises(Problem): query_arguments({'graph': 'prokn', 'query': query}, [])
        for limit in (0, 101, True, '10'):
            with self.subTest(limit=limit), self.assertRaises(Problem): self.check(query, limit=limit)
        with self.assertRaises(Problem): self.check(query+'#'+'x'*16_000)
        with self.assertRaises(Problem): self.check(query, endpoint='https://untrusted.example')


if __name__ == '__main__': unittest.main()
