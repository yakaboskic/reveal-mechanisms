#!/usr/bin/env python3
"""Build a labeled, offline ScientificAccount design fixture with DAPPER IDs."""
from copy import deepcopy
import gzip
import hashlib
from html import escape as html_escape
import json
from pathlib import Path
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'design/data/cad-account'
PIN = ROOT / 'data/dapper/2026-09-24-v8/snapshot/schema'
sys.path[:0] = [str(PIN / 'identity'), str(PIN / 'lint'), str(PIN), str(ROOT / 'scripts')]
from dapper_identity import assign_ids, compute_id, load_schema, verify
from scientific_claims import assemble_cited_text, check_scientific_content, index_document
from lint_provenance import Vocabulary, lint, build_validator
from import_cfde_genesets import metadata_record
from citation_metadata import check_citation_metadata, check_citation_registry_links

GAP_SOURCE = 'dismech:disorders/Coronary_Artery_Disease#discussion:cad_pgsxc_reverse_causation'
FACTOR = 'factor:portal:CADinT2D:cfde-inc-v2:Factor1'
MODEL = 'cfde-inc-v2'

def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')

def write_paragraph_exports(paragraph, registry, title):
    """Number exact target/revision pairs once, including repeated citation spans."""
    records = {(r['target_id'], r['metadata_revision']): r for r in registry}
    references, numbers, endings = [], {}, {}
    for citation in sorted(paragraph['citations'], key=lambda c: (c['start'], c['end'])):
        key = (citation['target_id'], citation['citation_metadata_revision'])
        if key not in numbers:
            numbers[key] = len(references) + 1
            references.append(records[key])
        endings.setdefault(citation['end'], []).append(numbers[key])
    last, chunks = 0, []
    for end, labels in sorted(endings.items()):
        chunks.append(paragraph['text'][last:end])
        chunks.append(' ' + ''.join(f'[{n}](#reference-{n})' for n in dict.fromkeys(labels)))
        last = end
    chunks.append(paragraph['text'][last:])
    markdown = [f'# {title}',
                '> Authored design example; unpublished. CFDE observations are captured; KG membership assertions are illustrative and unverified.',
                ''.join(chunks), '## References']
    # A simple numbered DAPPER export, not a claimed APA/MLA rendering.
    tex_special = {'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}',
                   '&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#',
                   '_': r'\_', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}',
                   '×': r'\ensuremath{\times}', '–': '--', '—': '---',
                   '’': "'", '‘': '`', '“': '``', '”': "''"}
    def tex(text):
        return ''.join(tex_special.get(char, char) for char in str(text))
    entries = []
    for n, r in enumerate(references, 1):
        names = [a.get('literal_name') or ', '.join(filter(None, [a.get('family_name'), a.get('given_name')])) for a in r['byline']]
        credit = '; '.join(names) or 'No author recorded'
        year = r['issued_date'][:4] if r.get('issued_date') else 'n.d.'
        markdown.append(f'<a id="reference-{n}"></a>\n{n}. {credit} ({year}). {r["title"]} '
                        f'DAPPER {r["target_class"]}. {r["repository"]}. '
                        f'[{r["target_id"]}]({r["canonical_url"]}). Metadata revision {r["metadata_revision"]}.')
        fields = {'title': '{' + tex(r['title']) + '}', 'year': year,
                  'archivePrefix': 'DAPPER', 'eprint': r['target_id'].removeprefix('dapper:'),
                  'howpublished': tex(r['repository']), 'url': r['canonical_url'],
                  'note': tex(f'Unpublished design fixture. DAPPER ID: {r["target_id"]}. Citation metadata revision {r["metadata_revision"]}. Issued date basis: {r["issued_basis"]}.')}
        if names:
            fields['author'] = ' and '.join(tex(name) for name in names)
        entries.append('@misc{' + r['target_id'] + ',\n' + ',\n'.join(
            f'  {key} = {{{value}}}' for key, value in fields.items()) + '\n}')
    markdown.extend(['## Object identity', f'Paragraph: `{paragraph["id"]}`  \nScientific account: `{paragraph["scientific_account"]}`',
                     'The companion Paragraph JSON preserves exact Unicode citation spans and pinned metadata revisions. References resolve to local design fixtures, not published records.'])
    (OUT / 'research-statement.md').write_text('\n\n'.join(markdown) + '\n')
    (OUT / 'references.bib').write_text('% Unpublished REVEAL design example. No DOI has been issued.\n\n' + '\n\n'.join(entries) + '\n')
    # Word receives HTML and plain text together, with absolute links that survive
    # pasting outside the browser. The same citation ordering drives LaTeX.
    rich_chunks, plain_chunks, latex_chunks, last = [], [], [], 0
    for end, labels in sorted(endings.items()):
        text = paragraph['text'][last:end]
        labels = list(dict.fromkeys(labels))
        rich_chunks.append(html_escape(text) + '<sup>' + ', '.join(
            f'<a href="{html_escape(references[n-1]["canonical_url"], quote=True)}" style="color:#265ca2;text-decoration:underline">{n}</a>'
            for n in labels) + '</sup>')
        plain_chunks.append(text + ' [' + ', '.join(map(str, labels)) + ']')
        latex_chunks.append(tex(text) + r'~\cite{' + ','.join(references[n-1]['target_id'] for n in labels) + '}')
        last = end
    rich_chunks.append(html_escape(paragraph['text'][last:]))
    plain_chunks.append(paragraph['text'][last:])
    latex_chunks.append(tex(paragraph['text'][last:]))
    rich_refs, plain_refs = [], []
    for n, r in enumerate(references, 1):
        names = [a.get('literal_name') or ', '.join(filter(None, [a.get('family_name'), a.get('given_name')])) for a in r['byline']]
        credit = '; '.join(names) or 'No author recorded'
        year = r['issued_date'][:4] if r.get('issued_date') else 'n.d.'
        meta = f'DAPPER {r["target_class"]}. {r["repository"]}. Metadata revision {r["metadata_revision"]}.'
        url = html_escape(r['canonical_url'], quote=True)
        rich_refs.append(f'<li style="margin-bottom:8pt">{html_escape(credit)} ({year}). '
                         f'<a href="{url}" style="color:#265ca2;text-decoration:underline">{html_escape(r["title"])}</a> '
                         f'{html_escape(meta)} <a href="{url}">{html_escape(r["target_id"])}</a>.</li>')
        plain_refs.append(f'{n}. {credit} ({year}). {r["title"]} {meta} {r["target_id"]}. {r["canonical_url"]}')
    note = 'Authored design example; unpublished. CFDE observations are captured; KG membership assertions are illustrative and unverified.'
    rich_html = (f'<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt;line-height:1.5;color:#222222">'
                 f'<h2 style="font-size:14pt">{html_escape(title)}</h2><p>{"".join(rich_chunks)}</p>'
                 f'<h3 style="font-size:12pt">References</h3><ol>{"".join(rich_refs)}</ol>'
                 f'<p style="font-size:9pt;color:#666666">{note}</p></div>')
    plain_text = title + '\n\n' + ''.join(plain_chunks) + '\n\nReferences\n\n' + '\n\n'.join(plain_refs) + '\n\n' + note
    write_json(OUT / 'rich-text.json', {'html':rich_html, 'text':plain_text})
    latex = '\n'.join([
        '% Download references.bib and save it beside this file.',
        '% Compile: pdflatex research-statement; bibtex research-statement; pdflatex research-statement; pdflatex research-statement',
        r'\documentclass[11pt]{article}', r'\usepackage[utf8]{inputenc}', r'\usepackage[T1]{fontenc}',
        r'\usepackage[hidelinks]{hyperref}', r'\usepackage[margin=1in]{geometry}',
        r'\begin{document}', r'\section*{' + tex(title) + '}', ''.join(latex_chunks), '',
        r'\par\medskip{\small\itshape ' + tex(note) + '}',
        r'\bibliographystyle{unsrt}', r'\bibliography{references}', r'\end{document}', ''])
    (OUT / 'research-statement.tex').write_text(latex)
    write_json(OUT / 'paragraph-object.json', paragraph)
    write_json(OUT / 'paragraph-packet.json', {'dapper_schema_pin':'2026-09-24-v8',
               'paragraph':paragraph, 'citation_records':references})

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'sources').mkdir(exist_ok=True)
    sv = load_schema(PIN / 'dapper.yaml')
    with gzip.open(ROOT / 'data/dismech-gaps/2026-09-24/knowledge-gaps.jsonl.gz', 'rt') as stream:
        gap_source = next(json.loads(line) for line in stream if json.loads(line)['id'] == GAP_SOURCE)
    gap = {'text': gap_source['raw']['prompt'], 'gap_description': gap_source['raw']['rationale'],
           'gap_kind': 'KNOWLEDGE_GAP', 'scope': gap_source['document_name']}
    term = ((gap_source.get('disease_term') or {}).get('term') or {}).get('id')
    if term and term.startswith('MONDO:'):
        gap['about_entities'] = ['http://purl.obolibrary.org/obo/' + term.replace(':', '_')]
    gap['id'] = compute_id(gap, 'KnowledgeGap', sv)
    person = {'id': 'urn:example:designer', 'given_name': 'Design', 'family_name': 'Example'}
    activity = {'id': 'urn:example:assembly', 'name': 'ScientificAccount design example assembly',
                'description': 'Offline authored UI example. No research agent or external KG query was executed.',
                'software_name': 'REVEAL HTML fixture builder', 'software_version': '0.2.0',
                'command': 'python3 design/build_account_example.py'}
    encoding = json.loads((ROOT / 'data/cfde-genesets/2026-09-24/activity.json').read_text())
    mechanism = {'id': 'urn:example:mechanism', 'name': 'CAD-in-T2D mechanism Factor1',
                 'description': f'EAGGL mechanism {FACTOR}. Factor1 is its interim name. Scope: coronary artery disease in people with type 2 diabetes.'}
    doc = {'knowledge_gaps': [gap], 'mechanisms': [mechanism], 'persons': [person],
           'activities': [encoding, activity], 'files': [], 'gene_sets': [],
           'propositions': [], 'claims': [], 'evidence_items': [], 'scientific_accounts': []}
    view = {'mode': 'Authored design example; no live agent or KG retrieval', 'gap_source_id': GAP_SOURCE,
            'gap_source': gap_source, 'factor_source_id': FACTOR, 'claim_views': {}, 'evidence_views': {},
            'source_files': {}, 'gene_set_labels': {},
            'coverage': 'Captured CFDE neighborhood: 8 genes, 8 gene sets, 1 mechanism; 16 direct edges. This account selects 3 genes and 2 sets. Contextual retrieval added no distinct edges.'}

    def source_file(key, filename, raw, origin, description):
        (OUT / 'sources' / filename).write_bytes(raw)
        node = {'id': 'urn:example:file-' + key, 'filename': filename, 'mime_type': 'application/json',
                'sha256': hashlib.sha256(raw).hexdigest(), 'size_in_bytes': len(raw), 'description': description}
        doc['files'].append(node)
        view['source_files'][node['id']] = {'path': 'data/cad-account/sources/' + filename,
                                          'origin': origin, 'payload': json.loads(raw)}
        return node

    source_files = {}
    for key, filename in [('genes', 'pigean-gene-factor.response.json'), ('sets', 'pigean-gene-set-factor.response.json')]:
        raw = (ROOT / 'data/cfde/cad-in-t2d' / filename).read_bytes()
        source_files[key] = source_file(key, filename, raw, 'captured', 'Original CFDE response bytes captured September 25, 2026; not a new retrieval.')
    source_file('gap', 'dismech-gap.source.json', (json.dumps(gap_source, indent=2, ensure_ascii=False)+'\n').encode(),
                'captured', 'Frozen DisMech source discussion; its exact question frames this example.')
    genes = json.loads((OUT / 'sources' / source_files['genes']['filename']).read_text())['data']
    sets = json.loads((OUT / 'sources' / source_files['sets']['filename']).read_text())['data']
    view['graph_counts']={'nodes':1+len({r['gene'] for r in genes})+len({r['gene_set'] for r in sets}),
                          'edges':len(genes)+len(sets)}
    selected_genes = ['SHH', 'GLI3', 'TCF7L2']
    selected_sets = [sets[0]['gene_set'], sets[2]['gene_set']]
    set_labels = ['AMP AD / GTEx brain-aging set', 'PsychENCODE geneM16 set']
    for key, label in zip(selected_sets, set_labels):
        node = metadata_record(key, MODEL, encoding['id'])
        node['id'] = compute_id(node, 'GeneSet', sv)
        doc['gene_sets'].append(node)
        view['gene_set_labels'][node['id']] = label
    # These are deliberate mock statements, never represented as returned KG data.
    mocks = {'illustrative': True, 'retrieved': False, 'purpose': 'Design-only membership and KG evidence interactions',
             'assertions': [
                 {'id': 'mock-prokn-1', 'graph': 'ProKN', 'statement': 'SHH is a member of the retained AMP AD / GTEx gene set.',
                  'subject': 'SHH', 'gene_set': selected_sets[0], 'status': 'invented for UI design; membership unverified'},
                 {'id': 'mock-biomarkerkg-1', 'graph': 'BiomarkerKG', 'statement': 'TCF7L2 is a member of the PsychENCODE geneM16 gene set.',
                  'subject': 'TCF7L2', 'gene_set': selected_sets[1], 'status': 'invented for UI design; membership unverified'}]}
    mock_file = source_file('mock-kg', 'illustrative-kg-assertions.json', (json.dumps(mocks, indent=2)+'\n').encode(),
                            'illustrative', 'Invented assertions for UI design. Not retrieved from ProKN or BiomarkerKG; not verified GeneSet membership.')

    def claim(key, proposition, statement, group, label, subject, object_id=None, illustrative=False):
        p = {'id': 'urn:example:p-' + key, 'statement': proposition, 'proposition_kind': 'BIOLOGICAL_INTERPRETATION',
             'scope': 'Coronary artery disease in people with type 2 diabetes (portal CADinT2D). ' +
                      ('Illustrative membership proposition; not established by captured CFDE data.' if illustrative else
                       'The mechanism is ' + FACTOR + '.' if group.endswith('mechanism') else 'Trait-level biological involvement.'),
             'subject_entity': subject,
             'relation': 'urn:reveal:relation:member-of' if illustrative else 'urn:reveal:relation:involved-in',
             'object_entity': object_id or 'urn:cfde:trait:portal:CADinT2D'}
        c = {'id': 'urn:example:c-' + key, 'proposition': p['id'], 'statement': statement,
             'direction': 'SUPPORTS', 'status': 'proposed', 'has_evidence': [],
             'was_generated_by': activity['id'], 'was_attributed_to': [person['id']]}
        doc['propositions'].append(p); doc['claims'].append(c)
        view['claim_views'][c['id']] = {'group': group, 'label': label, 'illustrative': illustrative}
        return c

    def evidence(c, key, files, locator, snippet, explanation, metrics=None, origin='captured', graph='CFDE', direction='SUPPORTS'):
        e = {'id': 'urn:example:e-' + key, 'target_proposition': c['proposition'], 'direction': direction,
             'evidence_source': graph + (' — illustrative assertion, not retrieved' if origin == 'illustrative' else ' — captured PIGEAN/EAGGL result'),
             'snippet': snippet, 'explanation': explanation,
             'context': f'Source locator: {locator}. Trait: portal CADinT2D; model: {MODEL}; mechanism: {FACTOR}. ' +
                        ('This is synthetic design evidence and has not been verified.' if origin == 'illustrative' else 'Source metrics retain their original meanings; no probability calibration is assumed.'),
             'was_derived_from': [f['id'] for f in files], 'was_generated_by': activity['id'], 'was_attributed_to': [person['id']]}
        doc['evidence_items'].append(e); c['has_evidence'].append(e['id'])
        view['evidence_views'][e['id']] = {'origin': origin, 'graph': graph, 'locator': locator, 'metrics': metrics or []}
        return e

    gene_claims = {}
    for gene in selected_genes:
        index, row = next((i, r) for i, r in enumerate(genes) if r['gene'] == gene)
        c = claim('gene-mechanism-'+gene, f'{gene} is involved in mechanism Factor1 relevant to coronary artery disease in people with type 2 diabetes.',
                  f'The EAGGL result supports {gene} involvement in this mechanism through its reported loading of {row["factor_value"]} under cfde-inc-v2.',
                  'Genes → mechanism', gene, 'urn:cfde:gene:'+gene, mechanism['id'])
        gene_claims[gene] = c
        evidence(c, 'gene-mechanism-'+gene, [source_files['genes']], f'pigean-gene-factor.response.json#/data/{index}',
                 '"factor_value":'+str(row['factor_value']), 'The loading is used as evidence of involvement in the mechanism. It does not distinguish amplification from inhibition.',
                 [{'metric': 'factor_value', 'value': row['factor_value'], 'meaning': 'Mechanism loading'}])
    for i, node in enumerate(doc['gene_sets']):
        index, row = next((j, r) for j, r in enumerate(sets) if r['gene_set'] == node['term'])
        label = set_labels[i]
        c = claim('set-mechanism-'+str(i), f'The gene program represented by the {label} is involved in mechanism Factor1 relevant to CAD in people with type 2 diabetes.',
                  f'The EAGGL result supports involvement of the {label}, with a loading of {row["factor_value"]} on mechanism Factor1.',
                  'Gene sets → mechanism', label, node['id'], mechanism['id'])
        evidence(c, 'set-mechanism-'+str(i), [source_files['sets']], f'pigean-gene-set-factor.response.json#/data/{index}',
                 '"factor_value":'+str(row['factor_value']), 'This is program-level involvement. The source label alone does not establish a particular causal process, cell type or activity readout.',
                 [{'metric':'factor_value','value':row['factor_value'],'meaning':'Mechanism loading'}])
    for gene in selected_genes:
        index, row = next((i, r) for i, r in enumerate(genes) if r['gene'] == gene)
        c = claim('gene-trait-'+gene, f'{gene} is involved in the biology of coronary artery disease in people with type 2 diabetes.',
                  f'PIGEAN supports {gene} trait-level involvement with a combined relevance score of {row["combined"]}; this is distinct from its mechanism loading.',
                  'Genes → trait', gene, 'urn:cfde:gene:'+gene)
        evidence(c, 'gene-trait-'+gene, [source_files['genes']], f'pigean-gene-factor.response.json#/data/{index}',
                 '"combined":'+str(row['combined']), 'The reported gene–trait relevance supports this scoped involvement assessment. It does not answer whether genetic risk is amplified by an exposure or whether the exposure is a downstream readout.',
                 [{'metric':'combined','value':row['combined'],'meaning':'Gene–trait relevance score'},
                  {'metric':'log_bf','value':row['log_bf'],'meaning':'Source-reported log_bf'}, {'metric':'prior','value':row['prior'],'meaning':'Source-reported prior'}])
    for i, node in enumerate(doc['gene_sets']):
        index, row = next((j, r) for j, r in enumerate(sets) if r['gene_set'] == node['term'])
        label = set_labels[i]
        c = claim('set-trait-'+str(i), f'The gene program represented by the {label} is involved in the biology of CAD in people with type 2 diabetes.',
                  f'PIGEAN supports trait-level involvement of the {label}, with beta={row["beta"]} and beta_uncorrected={row["beta_uncorrected"]}.',
                  'Gene sets → trait', label, node['id'])
        evidence(c, 'set-trait-'+str(i), [source_files['sets']], f'pigean-gene-set-factor.response.json#/data/{index}',
                 f'"beta":{row["beta"]},"beta_uncorrected":{row["beta_uncorrected"]}',
                 'The two effect estimates describe the annotation–trait relationship on their source scales. They come from the same analysis and are not independent replications.',
                 [{'metric':'beta','value':row['beta'],'meaning':'Gene-set effect estimate'}, {'metric':'beta_uncorrected','value':row['beta_uncorrected'],'meaning':'Uncorrected effect estimate'}])
    for i, assertion in enumerate(mocks['assertions']):
        gene, node = assertion['subject'], doc['gene_sets'][i]
        label = set_labels[i]
        c = claim('membership-'+gene, f'{gene} belongs to the gene set representing the {label}.',
                  f'For this illustrative scenario, a mock {assertion["graph"]} assertion supports {gene} membership in the {label}. The membership is unverified.',
                  'Genes → gene sets', gene+' → '+label, 'urn:cfde:gene:'+gene, node['id'], True)
        evidence(c, 'membership-'+gene, [mock_file], f'illustrative-kg-assertions.json#/assertions/{i}', assertion['statement'],
                 'This invented assertion directly supplies membership for the UI example. It is not a returned KG result and must not update the imported GeneSet membership.', origin='illustrative', graph=assertion['graph'])
        evidence(c, 'membership-context-'+gene, [source_files['genes'], source_files['sets']],
                 'pigean-gene-factor.response.json and pigean-gene-set-factor.response.json; shared Factor1 neighborhood',
                 '"factor":"Factor1"', 'Both entities load on the same mechanism. That is useful investigation context but does not establish that the gene belongs to the set.', direction='NEUTRAL')
        evidence(gene_claims[gene], 'kg-context-'+gene, [mock_file], f'illustrative-kg-assertions.json#/assertions/{i}', assertion['statement'],
                 'The mock membership suggests a gene-to-program connection to inspect alongside the program’s captured mechanism loading. It supplies context only; it is not independent support for mechanism involvement.',
                 origin='illustrative', graph=assertion['graph'], direction='NEUTRAL')
    synthesis = ('SHH, GLI3 and TCF7L2, together with the AMP AD / GTEx and PsychENCODE geneM16 programs, are implicated in the same CAD-in-T2D mechanism by their captured EAGGL loadings. '
                 'Their PIGEAN trait-level results connect these assessments to CAD in people with type 2 diabetes. '
                 'The illustrative membership assertions show how genes could be connected to the programs, but those links remain unverified. '
                 'Together, the assessed relationships prioritize a mechanism for investigating the selected gap. They do not distinguish exposure-driven amplification of genetic risk from downstream readouts or reverse causation; that requires temporal or intervention evidence not present here.')
    account = {'id':'urn:example:account', 'name':'Genes and gene programs in a shared CAD-in-T2D mechanism',
               'question':gap['id'], 'context':'A CAD-in-T2D evidence slice used to investigate the selected broader CAD gap. This authored design packet combines captured CFDE observations with explicitly illustrative KG membership assertions. No live agent ran.',
               'component_claims':[c['id'] for c in doc['claims']], 'conclusion_claims':[c['id'] for c in doc['claims'][:5]],
               'closing_remarks':synthesis, 'was_generated_by':activity['id'], 'was_attributed_to':[person['id']]}
    doc['scientific_accounts'].append(account)
    doc['used_edges'] = [{'subject':activity['id'],'predicate':'prov:used','object':f['id']} for f in doc['files']] + [
        {'subject':activity['id'],'predicate':'prov:used','object':gap['id']}]
    mapping = assign_ids(doc, sv)
    assert not verify(doc, sv)
    assert not check_scientific_content(index_document(doc))
    for key in ['claim_views','evidence_views','source_files','gene_set_labels']:
        view[key] = {mapping.get(k,k):v for k,v in view[key].items()}
    view['account_id'] = account['id']; view['gap_id'] = gap['id']; view['schema_pin'] = '2026-09-24-v8'
    # Save the authentic catalog identities, never synthetic members, on the source GeneSets.
    assert doc['gene_sets'][0]['id'] == 'dapper:GeneSet.I1UZhVhKU-YDerPLIxtnZOGlTXNAlrgD'
    assert all('members' not in node for node in doc['gene_sets'])
    write_json(OUT / 'scientific-account.json', doc)
    (OUT / 'scientific-account.yaml').write_text('# Authored UI example. Captured CFDE + explicitly illustrative KG evidence.\n'+yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))
    write_json(OUT / 'presentation.json', view)
    # Keep the paragraph coherent with this account; it is a separate authored preview.
    para_doc = deepcopy(doc)
    pa = {'id':'urn:example:paragraph-activity','name':'Design paragraph expression','description':'Offline authored paragraph preview, not an executed agent.'}
    para_doc['activities'].append(pa)
    para_doc['paragraphs'] = [{'id':'urn:example:paragraph','scientific_account':account['id'],'language':'en',
         'was_generated_by':pa['id'],'was_attributed_to':[person['id']], **assemble_cited_text([
             {'text':'The selected CAD knowledge gap asks whether context amplifies genetic risk or instead reflects incipient disease.',
              'citations':[{'target_id':gap['id'],'citation_metadata_revision':1}]},
             {'text':'In the captured CAD-in-T2D analysis, SHH, GLI3 and TCF7L2 are implicated in mechanism Factor1 by EAGGL loadings of 0.5042, 0.3245 and 0.3813, respectively.',
              'citations':[{'target_id':c['id'],'citation_metadata_revision':1} for c in doc['claims'][:3]]},
             {'text':'The AMP AD / GTEx brain-aging and PsychENCODE geneM16 programs are also implicated in this mechanism by their gene-set loadings.',
              'citations':[{'target_id':c['id'],'citation_metadata_revision':1} for c in doc['claims'][3:5]]},
             {'text':'PIGEAN gene–trait and gene-set–trait results further connect these genes and programs to CAD in people with type 2 diabetes.',
              'citations':[{'target_id':c['id'],'citation_metadata_revision':1} for c in doc['claims'][5:10]]},
             {'text':'Illustrative, unverified KG membership assertions connect SHH to the AMP AD / GTEx set and TCF7L2 to the PsychENCODE set; these links demonstrate a possible evidence connection and are not retrieved biological findings.',
              'citations':[{'target_id':c['id'],'citation_metadata_revision':1} for c in doc['claims'][10:]]},
             {'text':'Taken together, these assessments prioritize a shared mechanism for investigating the gap, but do not resolve whether exposures amplify genetic risk or are downstream readouts. Temporal or intervention evidence is still needed.',
              'citations':[{'target_id':gap['id'],'citation_metadata_revision':1},
                           *[{'target_id':c['id'],'citation_metadata_revision':1} for c in doc['claims'][:5]]]}])}]
    para_doc['used_edges'].append({'subject':pa['id'],'predicate':'prov:used','object':account['id']})
    assign_ids(para_doc, sv)
    assert not verify(para_doc, sv)
    assert not check_scientific_content(index_document(para_doc))
    write_json(OUT / 'paragraph.json', para_doc)
    # Companion citation records make the preview's pinned metadata revisions resolvable.
    from build_openapi import citation
    byline=[{'agent_id':person['id'],'kind':'person','given_name':'Design','family_name':'Example',
             'roles':['agent_operator'],'source':{'source_ref':'urn:example:cad-account-design'}}]
    registry=[]
    for node,cls in [(gap,'KnowledgeGap')]+[(c,'Claim') for c in doc['claims']]:
        record=citation(node,cls,origin='imported' if cls=='KnowledgeGap' else 'native',byline=[] if cls=='KnowledgeGap' else byline)
        record['title_derivation']['source']={'source_ref':'urn:example:cad-account-design'}
        record['repository']='REVEAL design examples — unpublished'
        record['object_payload_ref']='http://127.0.0.1:8767/data/cad-account/scientific-account.json'
        pointer='knowledge_gaps/0' if cls=='KnowledgeGap' else 'claims/'+str(doc['claims'].index(node))
        record['canonical_url']=record['object_payload_ref']+'#/'+pointer
        assert not check_citation_metadata(record),check_citation_metadata(record)
        registry.append(record)
    assert not check_citation_registry_links(para_doc['paragraphs'][0],registry)
    write_json(OUT/'citation-registry.json',registry)
    write_paragraph_exports(para_doc['paragraphs'][0], registry, account['name'])
    vocab = Vocabulary.build(sv, yaml.safe_load((PIN/'lint/profiles.yaml').read_text()))
    validator = build_validator(PIN/'dapper.yaml')
    report = lint(OUT/'scientific-account.json', vocab, sv, validator, profile_name='scientific-account')
    assert not report.errors, [(e.check,e.message) for e in report.errors]
    vocab.profiles.update(yaml.safe_load((ROOT/'api/paragraph-profile.yaml').read_text()))
    paragraph_report = lint(OUT/'paragraph.json', vocab, sv, validator, profile_name='reveal-paragraph')
    assert not paragraph_report.errors, [(e.check,e.message) for e in paragraph_report.errors]
    # Direct evidence uses are checked against retained artifacts as well as schema shape.
    for evidence_node in doc['evidence_items']:
        assert all(ref in view['source_files'] for ref in evidence_node['was_derived_from'])
        for fid in evidence_node['was_derived_from']:
            source = view['source_files'][fid]
            raw = (ROOT/'design'/source['path']).read_text()
            assert evidence_node['snippet'] in raw, evidence_node['id']
        presentation=view['evidence_views'][evidence_node['id']]
        if presentation['metrics']:
            filename, index=presentation['locator'].split('#/data/')
            row=json.loads((OUT/'sources'/filename).read_text())['data'][int(index)]
            assert all(row[m['metric']]==m['value'] for m in presentation['metrics'])
    for group in ['Genes → mechanism','Gene sets → mechanism','Genes → trait','Gene sets → trait','Genes → gene sets']:
        assert sum(v['group']==group for v in view['claim_views'].values())>=2
    assert len(doc['claims'])==len(doc['propositions'])==12
    assert all(not e.get('source_claims') for e in doc['evidence_items'])
    validation={'identity':'passed','scientific_references':'passed','dapper_profile':'scientific-account',
                'schema_errors':0,'claims':12,'propositions':12,'evidence_items':len(doc['evidence_items']),
                'paragraph_profile':'passed','citation_registry_links':'passed','displayed_source_metrics':'passed',
                'captured_gene_sets_preserved':True,'mock_membership_not_added_to_genesets':True,
                'warnings':[{'check':w.check,'message':w.message} for w in report.warnings],
                'scope':'Design fixture validation; not scientific validation of authored assessments.'}
    write_json(OUT/'validation.json',validation)
    print(json.dumps(validation,indent=2))

if __name__=='__main__': main()
