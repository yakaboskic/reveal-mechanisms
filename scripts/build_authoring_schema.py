#!/usr/bin/env python3
"""Regenerate the authoring excerpt from the hash-locked DAPPER checkout."""
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/backend/src'))
from reveal_backend.authoring_contract import ACCOUNT_EXCERPT, ACCOUNT_WANTED, EXCERPT, WANTED, schema_excerpt

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dapper-root',required=True,type=Path)
    args=parser.parse_args()
    # The research excerpt, and the account-job excerpt that adds Activity and the provenance edges.
    for name,wanted in ((EXCERPT,WANTED),(ACCOUNT_EXCERPT,ACCOUNT_WANTED)):
        raw=schema_excerpt(args.dapper_root,ROOT/'services/backend/agent-runtime/dapper-release.json',wanted)
        target=ROOT/name
        target.write_bytes(raw)
        print(str(target)+' ('+str(len(raw))+' bytes)')
