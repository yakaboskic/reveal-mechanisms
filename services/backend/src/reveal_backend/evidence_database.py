"""Read existing versioned GeneSet objects without scanning/reimporting exports."""
import hashlib
import json
from .runtime_config import ROOT, mysql_connection
from .evidence_package import canonical_json, decode, require, sha256

def geneset_resolver(import_id):
    def resolve(wanted, model):
        connection = mysql_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT manifest FROM gene_set_imports WHERE import_id=%s AND status='complete' AND model=%s", (import_id,model))
                row = cursor.fetchone(); require(row is not None,'Pinned GeneSet import is unavailable')
                manifest = json.loads(row[0])
                selected=[]; aliases=[]
                keys={hashlib.sha256(identity.encode()).hexdigest():identity for identity in wanted}
                for start in range(0,len(keys),500):
                    batch=list(keys)[start:start+500]
                    cursor.execute('SELECT a.node_id,a.source_key,o.payload,o.payload_sha256 FROM cfde_gene_set_aliases a JOIN dapper_objects o ON o.id=a.dapper_id WHERE a.import_id=%s AND a.model=%s AND a.node_id_sha256 IN ('+','.join(['%s']*len(batch))+')',
                                   [import_id,model,*batch])
                    aliases.extend(cursor.fetchall())
                for alias in aliases:
                    identity=alias[0]; require(identity in wanted,'Alias query returned a different native identity')
                    payload=json.loads(alias[2]); require(sha256(canonical_json(payload))==alias[3] or hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()==alias[3],'Stored GeneSet checksum mismatch')
                    selected.append({'node_id':identity,'source_key':alias[1],'model':model,'gene_set':payload})
                # The import's own Activity bytes are pinned in its manifest and
                # resolved by exact identity, with the original exported observation.
                activity_path=ROOT/'data/cfde-genesets/2026-09-24/activity.json'
                activity_bytes=activity_path.read_bytes(); require(sha256(activity_bytes)==manifest['activity_sha256'],'GeneSet Activity differs from import')
                activity=decode(activity_bytes)
                cursor.execute('SELECT payload FROM dapper_objects WHERE id=%s',(activity['id'],))
                actual=cursor.fetchone(); require(actual and json.loads(actual[0])==activity,'Imported Activity observation changed')
                return canonical_json(manifest),activity,selected
        finally: connection.close()
    return resolve
