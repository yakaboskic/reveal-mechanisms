#!/usr/bin/env python3
"""Preserve legacy drafts as saved inputs. Dry-run by default; no scientific rewrites.

Use the isolated workflow-local backend environment. Applying writes a private
backup before one optimistic, atomic transaction. Restore from that backup only
after reviewing subsequent edits; never blindly overwrite newer revisions.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/backend/src'))
from reveal_backend.repository import Repository,now


def migration_rows(tx):
    result=[]
    for row in tx.list('draft'):
        value=deepcopy(row['data']); changed=False
        if 'lifecycle' not in value:
            value.update(lifecycle='saved',expires_at=None); changed=True
        for key,default in [('research_direction',''),('context',''),('hypotheses',''),('upload_ids',[])]:
            if key not in value['composer']: value['composer'][key]=default; changed=True
        if changed:
            value.update(version=value['version']+1,updated_at=now())
            result.append((row,value))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',type=Path,default=ROOT/'.runtime/workflow/backend.env')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--backup',type=Path,default=ROOT/'.runtime/workflow/research-inputs-backup.json')
    args=parser.parse_args()
    from dotenv import dotenv_values
    config={k:v for k,v in dotenv_values(args.env,interpolate=False).items() if v is not None}
    if config.get('REVEAL_APPLICATION_TABLE_PREFIX')!='reveal_workflow_local':
        raise RuntimeError('Migration requires isolated reveal_workflow_local tables')
    if config.get('REVEAL_ENVIRONMENT') not in ('development','test'):
        raise RuntimeError('Migration requires a development environment')
    if config.get('REVEAL_MYSQL_CA_FILE','').startswith('/app/'):
        config['REVEAL_MYSQL_CA_FILE']=str(ROOT/'.deployment-assets/rds-ca.pem')
    os.environ.update(config); repository=Repository()
    with repository.read_transaction() as tx: rows=migration_rows(tx)
    if args.apply and rows:
        args.backup.parent.mkdir(parents=True,exist_ok=True)
        descriptor=os.open(args.backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w') as file:
            json.dump({'format':'reveal.research-inputs-migration/1','created_at':now(),'table_prefix':repository.table_prefix,
                'rows':[row for row,_ in rows]},file,indent=2)
        with repository.transaction() as tx:
            for row,value in rows: tx.put('draft',row['id'],row['owner'],value,expected=row['version'])
    print(json.dumps({'applied':args.apply,'table_prefix':repository.table_prefix,'draft_ids':[row['id'] for row,_ in rows],
        'frozen_records_changed':0,'backup':str(args.backup) if args.apply and rows else None}))


if __name__=='__main__': main()
