#!/usr/bin/env python3
"""Review/apply local browser upload CORS and staging expiry, preserving other rules."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'services/backend/src'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',type=Path,default=ROOT/'.runtime/workflow/backend.env')
    parser.add_argument('--origin',default='http://localhost:3000')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if args.origin not in ('http://localhost:3000','http://localhost:3100'):
        raise ValueError('Only the local application origins are supported')
    from dotenv import dotenv_values
    config={k:v for k,v in dotenv_values(args.env,interpolate=False).items() if v is not None}
    if config.get('REVEAL_S3_PREFIX')!='local/' or config.get('REVEAL_APPLICATION_TABLE_PREFIX')!='reveal_workflow_local':
        raise ValueError('Use the isolated workflow-local configuration')
    os.environ.update(config)
    from reveal_backend.artifact_store import store
    storage=store(); client=storage.client
    def existing(method,field,missing):
        try: return getattr(client,method)(Bucket=storage.bucket).get(field,[])
        except client.exceptions.ClientError as error:
            if error.response['Error']['Code']==missing: return []
            raise
    cors=existing('get_bucket_cors','CORSRules','NoSuchCORSConfiguration')
    lifecycle=existing('get_bucket_lifecycle_configuration','Rules','NoSuchLifecycleConfiguration')
    cors=[r for r in cors if r.get('ID')!='reveal-local-uploads']+[{'ID':'reveal-local-uploads',
        'AllowedOrigins':[args.origin],'AllowedMethods':['POST'],'AllowedHeaders':['*'],
        'ExposeHeaders':['ETag','x-amz-version-id'],'MaxAgeSeconds':300}]
    lifecycle=[r for r in lifecycle if r.get('ID')!='reveal-local-upload-staging']+[{
        'ID':'reveal-local-upload-staging','Status':'Enabled','Filter':{'Prefix':'local/uploads/staging/'},
        'Expiration':{'Days':1},'NoncurrentVersionExpiration':{'NoncurrentDays':1},
        'AbortIncompleteMultipartUpload':{'DaysAfterInitiation':1}}]
    if args.apply:
        receipt=ROOT/'.runtime/workflow/upload-storage-before.json'
        if receipt.exists(): raise RuntimeError('Prior configuration receipt exists; review it before applying again')
        receipt.parent.mkdir(parents=True,exist_ok=True)
        with os.fdopen(os.open(receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as output:
            json.dump({'bucket':storage.bucket,'cors':existing('get_bucket_cors','CORSRules','NoSuchCORSConfiguration'),
                'lifecycle':existing('get_bucket_lifecycle_configuration','Rules','NoSuchLifecycleConfiguration')},output,indent=2)
        client.put_bucket_cors(Bucket=storage.bucket,CORSConfiguration={'CORSRules':cors})
        client.put_bucket_lifecycle_configuration(Bucket=storage.bucket,LifecycleConfiguration={'Rules':lifecycle})
    print(json.dumps({'applied':args.apply,'bucket':storage.bucket,'cors':cors,'lifecycle':lifecycle},indent=2))


if __name__=='__main__': main()
