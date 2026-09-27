"""Version-pinned Aurora source adapters shared by the API and collector."""
from collections import defaultdict
from difflib import SequenceMatcher
import json
from pathlib import Path
import threading
from .auth import Problem
from .repository import canonical, digest, now
from .runtime_config import ROOT, setting, mysql_connection
from .evidence_package import DapperRuntime, canonical_json, sha256
from .eaggl_embeddings import database_search_index
from .embedding_client import get_embeddings
from .dismech_embeddings import context_input, load_context_vectors
import numpy as np

class Catalog:
    def __init__(self):
        self.loaded = False
        self.lock = threading.Lock()
        self.dismech_catalog_lock=threading.Lock()
        self.complete_dismech_catalog=None
    def load(self):
        with self.lock:
            if self.loaded: return
            runtime = DapperRuntime(ROOT / 'data/dapper/2026-09-24-v8')
            self.runtime = runtime
            connection = mysql_connection()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT import_id,source_commit,source_files FROM dismech_imports WHERE status='complete'")
                    imports = cursor.fetchall()
                    requested = setting('REVEAL_DISMECH_IMPORT_ID')
                    imports = [r for r in imports if not requested or r[0] == requested]
                    if len(imports) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'Select one completed DisMech import.')
                    self.dismech_import, self.source_commit, files = imports[0]
                    files = json.loads(files)
                    # Import source_files stores both source and gap manifests.
                    if isinstance(files, dict): files = files.get('source', files.get('gaps', []))
                    self.file_hashes = files if isinstance(files, dict) else {r['path']: r['sha256'] for r in files}
                    cursor.execute('SELECT source_file,source_sha256 FROM dismech_documents WHERE import_id=%s', (self.dismech_import,))
                    self.file_hashes.update(dict(cursor.fetchall()))
                    cursor.execute('SELECT payload FROM dismech_discussions WHERE import_id=%s AND is_gap=1', (self.dismech_import,))
                    gap_rows = [json.loads(r[0]) for r in cursor.fetchall()]
                    cursor.execute('SELECT m.payload FROM dismech_mechanisms m WHERE m.import_id=%s AND EXISTS (SELECT 1 FROM dismech_gap_attachments a WHERE a.import_id=m.import_id AND a.target_mechanism_sha256=m.id_sha256)', (self.dismech_import,))
                    mechanism_rows = [json.loads(r[0]) for r in cursor.fetchall()]
                    cursor.execute('SELECT payload FROM dismech_gap_attachments WHERE import_id=%s ORDER BY attachment_index', (self.dismech_import,))
                    attachments = defaultdict(list)
                    for r in cursor.fetchall():
                        item = json.loads(r[0]); attachments[item['gap_id']].append(item)
                    cursor.execute("SELECT run_id,eaggl_import_id,gene_set_import_id FROM eaggl_cfde_link_runs WHERE status='complete'")
                    runs = [r for r in cursor.fetchall() if not setting('REVEAL_MAPPING_RUN_ID') or r[0] == setting('REVEAL_MAPPING_RUN_ID')]
                    if len(runs) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'Select one completed EAGGL mapping run.')
                    self.mapping_run, self.eaggl_import, self.geneset_import = runs[0]
                    cursor.execute("SELECT run_id FROM eaggl_embedding_runs WHERE import_id=%s AND status='complete'", (self.eaggl_import,))
                    runs = [r[0] for r in cursor.fetchall() if not setting('REVEAL_EMBEDDING_RUN_ID') or r[0] == setting('REVEAL_EMBEDDING_RUN_ID')]
                    if len(runs) != 1: raise Problem(503, 'SOURCE_NOT_READY', 'Select one completed embedding run.')
                    self.embedding_run = runs[0]
                    cursor.execute('SELECT f.factor_id,f.label,l.cfde_node_id,l.payload FROM eaggl_cfde_factor_links l JOIN eaggl_factors f ON f.import_id=l.eaggl_import_id AND f.factor_index=l.factor_index WHERE l.run_id=%s', (self.mapping_run,))
                    factor_rows = cursor.fetchall()
                self.index = database_search_index(connection, self.eaggl_import, self.embedding_run)
                # Import-time vectors cover all source mechanisms. Readiness
                # preloads only those currently used by gaps plus exact fallback
                # questions, so the first automatic suggestion does no I/O.
                context_ids = {row['id'] for row in mechanism_rows}
                inputs = [context_input(row['id'], 'mechanism', self.file_hashes[row['source_file']],
                    row.get('description') or row['name']) for row in mechanism_rows]
                inputs.extend(context_input(row['id'], 'knowledge_gap', self.file_hashes[row['source_file']], row['raw']['prompt'])
                    for row in gap_rows if not any(item.get('target_id') in context_ids for item in attachments[row['id']]))
                try:
                    self.dismech_embeddings = load_context_vectors(connection, self.dismech_import, self.index.run, inputs,
                        run_id=setting('REVEAL_DISMECH_EMBEDDING_RUN_ID'))
                    self.context_text_vectors = {row['input_sha256']: (row['input_text'], self.dismech_embeddings['vectors'][identity])
                        for identity, row in self.dismech_embeddings['bindings'].items()}
                except Exception as error:
                    raise Problem(503, 'DISMECH_EMBEDDINGS_NOT_READY',
                        'Import and verify compatible DisMech context embeddings before automatic retrieval.') from error
            finally: connection.close()
            self.mechanisms, self.gaps, self.by_source, self.bindings = {}, {}, {}, {}
            for row in mechanism_rows:
                node = {'name': row['name'], 'description': row.get('description') or row['name']}
                node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
                self.mechanisms[row['id']] = {'source': 'dismech', 'source_id': row['id'], 'source_revision': self.file_hashes[row['source_file']],
                    'object_class': 'Mechanism', 'object': node, 'disease_label': row.get('document_name', '')}
            for row in gap_rows:
                raw = row['raw']
                node = {'text': raw['prompt'], 'gap_description': raw.get('rationale') or raw['prompt'], 'gap_kind': row['kind'], 'scope': row['document_name']}
                disease = (row.get('disease_term') or {}).get('term', {}).get('id')
                if disease:
                    node['about_entities'] = [runtime.resolver({'MONDO': 'http://purl.obolibrary.org/obo/MONDO_'}).expand(disease)]
                node['id'] = runtime.compute_id(node, 'KnowledgeGap', runtime.schema)
                linked = []
                for item in attachments[row['id']]:
                    target = self.mechanisms.get(item.get('target_id'))
                    reference = {k: target[k] for k in ('source', 'source_id', 'source_revision')} | {'dapper_id': target['object']['id']} if target else None
                    linked.append({'source_reference': item['source_reference'], 'target_kind': item.get('target_kind') or 'unknown',
                        'resolution': item['resolution'], 'target': reference, 'label': target['object']['name'] if target else None})
                gap = {'object': node, 'source': {'source': 'dismech', 'source_id': row['id'], 'source_revision': self.file_hashes[row['source_file']],
                    'status': row.get('status'), 'disease_label': row['document_name'], 'description_derivation': 'source_rationale' if raw.get('rationale') else 'prompt_fallback'},
                    'attachments': linked, 'source_detail': {'source_file': row['source_file'], 'source_pointer': row['source_pointer'], 'payload_sha256': sha256(canonical_json(raw)), 'raw': raw},
                    'scientific_accounts': {'count': 0, 'scope': 'public_exact_gap', 'as_of': now(), 'ranking': 'curated', 'window_days': None}}
                self.gaps[node['id']] = gap; self.by_source[row['id']] = gap
            self.factors, self.factor_legacy = {}, {}
            for legacy, label, native, payload in factor_rows:
                raw = json.loads(payload)['raw']; trait, factor = native.split(':')[2], native.split(':')[4]
                node = {'name': f'{trait} mechanism {factor}', 'description': f'EAGGL mechanism {native}. Source label: {raw["label"]}.'}
                node['id'] = runtime.compute_id(node, 'Mechanism', runtime.schema)
                record = {'source': 'eaggl', 'source_id': native, 'source_revision': sha256(canonical_json(raw)), 'object_class': 'Mechanism', 'object': node,
                    'cfde_anchor': {'node_id': native, 'node_type': 'factor', 'label': label, 'subtitle': f'{trait} ({factor})'}, 'model': 'cfde-inc-v2',
                    'catalog_file': runtime.file('cfde-factor.json', canonical_json(raw), 'application/json')}
                self.factors[native] = record; self.factor_legacy[legacy] = record
                self.bindings[native] = {'eaggl_factor_id': legacy, 'eaggl_import_id': self.eaggl_import, 'embedding_run_id': self.embedding_run,
                    'mapping_run_id': self.mapping_run, 'gene_set_import_id': self.geneset_import, 'cfde_node_id': native, 'cfde_payload': raw}
            self.loaded = True
    def dismech_catalog(self):
        """Load the independent full corpus only when mechanism search needs it."""
        self.load()
        with self.dismech_catalog_lock:
            if self.complete_dismech_catalog is not None: return self.complete_dismech_catalog
            connection=mysql_connection()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT source_id,name,description,JSON_UNQUOTE(JSON_EXTRACT(payload,'$.source_file')),JSON_UNQUOTE(JSON_EXTRACT(payload,'$.document_name')) FROM dismech_mechanisms WHERE import_id=%s",(self.dismech_import,))
                    rows=cursor.fetchall()
            finally: connection.close()
            records={}
            for identity,name,description,source_file,disease in rows:
                node={'name':name,'description':description or name}
                node['id']=self.runtime.compute_id(node,'Mechanism',self.runtime.schema)
                records[identity]={'source':'dismech','source_id':identity,'source_revision':self.file_hashes[source_file],
                    'object_class':'Mechanism','object':node,'disease_label':disease or ''}
            self.complete_dismech_catalog=records
            return records
    def gap(self, identity):
        self.load()
        result = self.gaps.get(identity) or self.by_source.get(identity)
        if not result: raise Problem(404, 'GAP_NOT_FOUND', 'This source gap is unavailable.')
        return result
    def selected(self, selection):
        gap = self.gap(selection['id'])
        if any(selection[k] != gap['source'][k] for k in ('source_id', 'source_revision')):
            raise Problem(409, 'SOURCE_REVISION_CHANGED', 'Reload the source question before submitting.')
        return gap
    def validate_composer(self, composer, submit=False):
        self.load()
        gap = self.selected(composer['source_gap']) if composer['source_gap'] else None
        if submit and (not gap or not composer['eaggl_anchors']): raise Problem(422, 'ANCHOR_REQUIRED', 'Select a source question and at least one mechanism anchor.')
        seen = set()
        for selection in composer['eaggl_anchors']:
            ref = selection['reference']; record = self.factors.get(ref['source_id'])
            if not record or record['source_revision'] != ref['source_revision'] or record['object']['id'] != ref['dapper_id'] or ref['source'] != 'eaggl':
                raise Problem(409, 'SOURCE_REVISION_CHANGED', 'The selected mechanism binding is unavailable; select it again.')
            if ref['source_id'] in seen: raise Problem(422, 'DUPLICATE_ANCHOR', 'Select each native factor only once.')
            seen.add(ref['source_id'])
        return gap
    def provenance(self, query, mode, semantic=False):
        return {'query': query, 'mode': mode, 'corpus_snapshot': self.mapping_run if semantic else self.dismech_import,
            'embedding_model': self.index.run['config']['model'] if semantic else None, 'embedding_revision': self.embedding_run if semantic else None,
            'template_version': 'eaggl-label-v1' if semantic else 'dismech-question-v1', 'score_aggregation': 'maximum_per_context' if semantic else None}
    def search_gaps(self, query, limit=20,mode='fuzzy'):
        self.load(); words = query.casefold().split()
        scored = []
        for gap in self.gaps.values():
            text = (gap['object']['text']+' '+gap['source']['disease_label']).casefold()
            score = sum(w in text for w in words)/max(1,len(words)) if words else 1
            if words and not score and mode=='fuzzy':
                score = max((SequenceMatcher(None, query.casefold(), word).ratio() for word in text.split()), default=0)
                if score < 0.7: score = 0
            if score: scored.append((score, gap))
        scored.sort(key=lambda r: (-r[0], r[1]['source']['source_id']))
        return [{'gap': g, 'ranking': {'value': s, 'metric': 'fuzzy_similarity' if mode=='fuzzy' else 'lexical_rank', 'rank': i+1}} for i,(s,g) in enumerate(scored[:limit])]
    def search_factors(self, query, mode='semantic', limit=20, exclude=(), *, query_vector=None):
        self.load()
        if mode=='hybrid':
            combined={}
            for family in ('semantic','lexical'):
                for item in self.search_factors(query,family,len(self.factors),exclude,query_vector=query_vector):
                    identity=item['record']['source_id']
                    if identity not in combined: combined[identity]=[0,item['record']]
                    combined[identity][0]+=1/(60+item['ranking']['rank'])
            ranked=sorted(combined.values(),key=lambda r:(-r[0],r[1]['source_id']))[:limit]
            return [{'record':record,'ranking':{'value':score,'metric':'reciprocal_rank_fusion','rank':i+1}} for i,(score,record) in enumerate(ranked)]
        if mode == 'semantic' and query.strip():
            # Retrieve the full small index, filter mappings and deduplicate BEFORE cutoff.
            if query_vector is None:
                imported = getattr(self, 'context_text_vectors', {}).get(sha256(query.strip().encode('utf-8')))
                if imported and imported[0] == query.strip(): query_vector = imported[1]
            rows = self.index.search(query=query, top_k=len(self.index.factors), query_vector=query_vector)
            candidates = [(row['cosine_similarity'], self.factor_legacy[row['factor_id']]) for row in rows if row['factor_id'] in self.factor_legacy]
            metric = 'cosine_similarity'
        else:
            candidates = []
            for factor in self.factors.values():
                text = (factor['cfde_anchor']['label']+' '+factor['source_id']).casefold()
                score = sum(word in text for word in query.casefold().split())/max(1,len(query.split())) if query else 1
                if mode=='fuzzy' and query and not score:
                    score=max((SequenceMatcher(None,query.casefold(),word).ratio() for word in text.split()),default=0)
                    if score<0.7: score=0
                if score: candidates.append((score, factor))
            candidates.sort(key=lambda r: (-r[0], r[1]['source_id'])); metric = 'fuzzy_similarity' if mode=='fuzzy' else 'lexical_rank'
        seen = set(exclude); result = []
        for score, record in candidates:
            if record['source_id'] in seen: continue
            seen.add(record['source_id']); result.append({'record': record, 'ranking': {'value': score, 'metric': metric, 'rank': len(result)+1}})
            if len(result) == limit: break
        return result

    def stored_context_inputs(self, contexts):
        stored = getattr(self, 'dismech_embeddings', None)
        if stored is None:
            raise Problem(503, 'DISMECH_EMBEDDINGS_NOT_READY', 'Compatible imported DisMech context vectors are unavailable.')
        inputs = []
        for identity, text in contexts:
            if identity in self.mechanisms:
                source = self.mechanisms[identity]
                row = context_input(source['source_id'], 'mechanism', source['source_revision'], text)
            elif identity in self.gaps:
                source = self.gaps[identity]['source']
                row = context_input(source['source_id'], 'knowledge_gap', source['source_revision'], text)
            else:
                raise Problem(503, 'DISMECH_EMBEDDINGS_NOT_READY', 'The selected source context has no imported vector binding.')
            if stored['bindings'].get(row['source_id']) != row or row['source_id'] not in stored['vectors']:
                raise Problem(503, 'DISMECH_EMBEDDINGS_NOT_READY', 'The imported context vector does not match the exact source revision and text.')
            inputs.append(row)
        return inputs

    def context_embedding_provenance(self, contexts):
        inputs = self.stored_context_inputs(contexts)
        stored = self.dismech_embeddings
        return {'dismech_embedding_run_id': stored['run_id'], 'dismech_import_id': self.dismech_import,
                'context_embedding_templates': stored['config']['templates'],
                'context_embedding_inputs': [{key: value for key, value in row.items() if key != 'input_text'} for row in inputs]}

    def runtime_query_vectors(self, texts):
        imported = getattr(self, 'context_text_vectors', {})
        known = [imported.get(sha256(text.encode('utf-8'))) for text in texts]
        missing = [i for i, (text, row) in enumerate(zip(texts, known)) if row is None or row[0] != text]
        resolved = {i: row[1] for i, row in enumerate(known) if i not in missing}
        if missing:
            fresh = self.index.query_vectors([texts[i] for i in missing], embedder=get_embeddings)
            resolved.update(zip(missing, fresh))
        return np.stack([resolved[i] for i in range(len(texts))])

    def suggest_factors(self,contexts,mode,remaining,exclude,*,precomputed=False):
        self.load()
        if not remaining: return []
        stored_vectors = None
        if precomputed:
            inputs = self.stored_context_inputs(contexts)
            stored_vectors = np.stack([self.dismech_embeddings['vectors'][row['source_id']] for row in inputs])
        if mode=='semantic':
            vectors = stored_vectors
            if vectors is None:
                try:
                    vectors=self.runtime_query_vectors([text for _,text in contexts])
                except ValueError as error:
                    raise Problem(503,'EMBEDDING_UNAVAILABLE','The embedding service returned incompatible vectors.') from error
            scores=self.index.matrix @ vectors.T
            candidates={}
            for index,factor in enumerate(self.index.factors):
                record=self.factor_legacy.get(factor['factor_id'])
                if not record or record['source_id'] in exclude: continue
                score=float(np.clip(scores[index].max(),-1,1)); matched=[contexts[i][0] for i,value in enumerate(scores[index]) if np.isclose(value,score)]
                previous=candidates.get(record['source_id'])
                if previous is None or score>previous['ranking']['value']:
                    candidates[record['source_id']]={'record':record,'ranking':{'value':score,'metric':'cosine_similarity','rank':1},'contexts':matched}
        else:
            candidates={}
            for position,(context_id,text) in enumerate(contexts):
                for item in self.search_factors(text,mode,5,exclude,query_vector=None if stored_vectors is None else stored_vectors[position]):
                    identity=item['record']['source_id']; old=candidates.get(identity)
                    if old is None or item['ranking']['value']>old['ranking']['value']: candidates[identity]={**item,'contexts':[context_id]}
        items=sorted(candidates.values(),key=lambda x:(-x['ranking']['value'],x['record']['source_id']))[:remaining]
        for rank,item in enumerate(items,1): item['ranking']['rank']=rank
        return items
