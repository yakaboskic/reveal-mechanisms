"""Read-only, single-named-graph SPARQL for the connected graph registry.

Validate the parsed syntax before RDFLib rewrites it. In particular, EXISTS
patterns and nested SELECTs must obey the same graph restriction. The original
query is preserved as a bounded subselect rather than rewritten with regexes.
"""
from collections.abc import Mapping

from pyparsing import Empty, ParseResults, Regex
from rdflib import URIRef
from rdflib.plugins.sparql.algebra import translatePrologue
from rdflib.plugins.sparql.parser import Prologue, parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

from .auth import Problem
from .box_mcp import GRAPHS


def invalid(detail):
    return Problem(422, 'INVALID_SPARQL', detail)


def query_arguments(arguments, selected_graphs=None):
    if not isinstance(arguments, dict) or set(arguments) - {'graph', 'query', 'limit'}:
        raise invalid('Supply graph, a read-only SELECT query, and an optional result limit.')
    graph = arguments.get('graph')
    if graph not in GRAPHS or selected_graphs is not None and graph not in selected_graphs:
        raise Problem(403, 'GRAPH_NOT_SELECTED', 'Choose a connected graph selected for this research.')
    query = arguments.get('query'); limit = arguments.get('limit', 25)
    if (not isinstance(query, str) or not 1 <= len(query.encode('utf-8')) <= 16_000
            or any(ord(char) < 32 and char not in '\n\r\t' for char in query)):
        raise invalid('Use a SELECT query of at most 16,000 UTF-8 bytes.')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise invalid('The result limit must be an integer from 1 to 100.')
    try:
        parsed = parseQuery(query)
        if parsed[1].name != 'SelectQuery':
            raise invalid('Only read-only SELECT queries are available.')
        prologue = translatePrologue(parsed[0], None)
        prefixes = {dict.get(item, 'prefix', '') for item in parsed[0] if item.name == 'PrefixDecl'}
        nodes = 0; patterns = 0

        def inspect(node, scoped=False, depth=0):
            nonlocal nodes, patterns
            nodes += 1
            if nodes > 4096 or depth > 64:
                raise invalid('The query is too complex; split it into smaller reads.')
            if isinstance(node, CompValue):
                if node.name in ('ServiceGraphPattern', 'DatasetClause', 'Function'):
                    raise invalid('SERVICE, FROM/FROM NAMED and extension functions are unavailable.')
                if node.name == 'GraphGraphPattern':
                    term = node['term']
                    if isinstance(term, CompValue) and term.name == 'pname' and dict.get(term, 'prefix', '') not in prefixes:
                        raise invalid('Declare graph prefixes explicitly.')
                    term = prologue.absolutize(term)
                    if not isinstance(term, URIRef) or str(term) != GRAPHS[graph]:
                        raise invalid('Every GRAPH must name the exact connected graph IRI; graph variables and other graphs are unavailable.')
                    scoped = True
                if node.name == 'TriplesBlock' and node.get('triples'):
                    if not scoped:
                        raise invalid('Put all triple patterns inside GRAPH <'+GRAPHS[graph]+'> { ... }.')
                    patterns += 1
            if isinstance(node, Mapping):
                for value in node.values(): inspect(value, scoped, depth+1)
            elif isinstance(node, (list, tuple, ParseResults)):
                for value in node: inspect(value, scoped, depth+1)

        inspect(parsed[1])
        if not patterns:
            raise invalid('Query at least one triple pattern in the selected named graph.')
        # Prologue is the parser's own PREFIX/BASE grammar. The offset keeps
        # comments, strings and source spelling intact without keyword matching.
        boundary = (Prologue + Empty().set_parse_action(lambda source, location, tokens: location)).ignore(Regex(r'#[^\r\n]*')).parse_with_tabs()
        offset = boundary.parse_string(query)[-1]
        bounded = query[:offset] + 'SELECT * WHERE { {\n' + query[offset:] + '\n} } LIMIT ' + str(limit)
        parseQuery(bounded)
    except Problem:
        raise
    except Exception:
        raise invalid('The SELECT query could not be parsed. Use explicit prefixes and the connected named graph.') from None
    return {'query': bounded, 'format': 'json', 'exploratory': True, 'compact': False}
