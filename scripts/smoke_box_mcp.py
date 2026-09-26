#!/usr/bin/env python3
"""Live selected-graph MCP probe, capturing exact empty/error responses too."""
import argparse
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'services/backend/src'))
from reveal_backend.box_mcp import Ledger, MCPClient, ScopedTools

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    ledger = Ledger(args.output, 'standalone-selected-graph-probe', 1)
    tools = ScopedTools(('prokn', 'biomarkerkg'), ledger)
    for name, arguments in [('get_schema', {'graph':'prokn'}),
                            ('query_graph', {'graph':'prokn','subject':'http://identifiers.org/ncbigene/6934','limit':3}),
                            ('query_graph', {'graph':'prokn','subject':'urn:reveal:deliberate-no-match:2026-09-25','limit':1}),
                            ('query_graph', {'graph':'unselected','contains':'TCF7L2'})]:
        result = tools.call(name, arguments)
        print(json.dumps({'tool':name,'graph':arguments['graph'],'is_error':bool(result.get('isError')),'status':ledger.entries[-1]['status']}),flush=True)
    ledger.freeze()
if __name__ == '__main__': main()
