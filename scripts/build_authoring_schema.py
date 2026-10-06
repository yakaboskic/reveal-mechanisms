#!/usr/bin/env python3
"""Regenerate the authoring excerpt from the hash-locked DAPPER checkout."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/backend/src'))
from reveal_backend.authoring_contract import schema_excerpt

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dapper-root',required=True,type=Path)
    args=parser.parse_args()
    raw=schema_excerpt(args.dapper_root,ROOT/'services/backend/agent-runtime/dapper-release.json')
    target=ROOT/'services/backend/agent-runtime/authoring-schema-excerpt.yaml'
    target.write_bytes(raw)
    print(str(target)+' ('+str(len(raw))+' bytes)')
