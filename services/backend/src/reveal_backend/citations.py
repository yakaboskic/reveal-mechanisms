"""Immutable citation registry, pinned CSL rendering and complete Paragraph exports."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import lru_cache
import hashlib
import html
import json
import os
from pathlib import Path
import re
import threading
from types import ModuleType
from urllib.parse import quote, urlsplit

from .auth import Problem, owned
from .repository import canonical, digest, now, uid
from .runtime_config import ROOT, setting

ASSETS = ROOT / 'services/backend/csl'


@lru_cache(maxsize=1)
def validator():
    root = Path(setting('REVEAL_DAPPER_ROOT', str(ROOT / '.runtime/dapper')))
    path = root / 'schema/citation_metadata.py'
    module = ModuleType('reveal_citation_metadata')
    module.__file__ = str(path)
    # The trusted release is immutable; compile in memory without __pycache__.
    exec(compile(path.read_text(), str(path), 'exec'), module.__dict__)
    return module


def validate_record(record):
    errors = validator().check_citation_metadata(record)
    if errors:
        raise Problem(422, 'INVALID_CITATION_METADATA', '; '.join(errors))


def grant(tx, user, target):
    tx.put('grant', digest([user, target]), user, {'target_id': target})


def get(tx, user, target, revision=1):
    from .scientific_reuse import retained_citations
    retained = retained_citations(tx, user, target)
    if retained is not None:
        pinned_revision = revision if revision is not None else max((item['metadata_revision'] for item in retained), default=1)
        selected = [item for item in retained if item['metadata_revision'] == pinned_revision]
        if selected:
            if any(item != selected[0] for item in selected):
                raise Problem(409, 'REUSE_PAYLOAD_CONFLICT', 'Conflicting retained citation revision.')
            return deepcopy(selected[0])
        raise Problem(404, 'CITATION_NOT_FOUND', 'The exact authorized citation revision is unavailable.')
    if revision is None:
        revisions = [r['data']['metadata_revision'] for r in tx.list('citation') if r['data']['target_id'] == target]
        revision = max(revisions, default=1)
    row = tx.get('citation', f'{target}:{revision}')
    permission = tx.get('grant', digest([user, target]))
    if not row or (row['owner'] != user and not (permission and permission['owner'] == user)):
        raise Problem(404, 'CITATION_NOT_FOUND', 'The exact authorized citation revision is unavailable.')
    return row['data']


def representation(tx, user, target, format='native', revision=None, locale='en-US'):
    record = get(tx, user, target, revision)
    if format == 'native':
        return record, 'application/json'
    if format == 'csl-json':
        return csl(record), 'application/vnd.citationstyles.csl+json'
    if format in ('bibtex', 'biblatex'):
        return bibtex([record], biblatex=format == 'biblatex'), 'application/x-' + format
    if format in ('apa', 'mla'):
        result = render_records([record], [{'target_id': target, 'citation_metadata_revision': record['metadata_revision']}], format, locale)
        return result['bibliography'][0]['text'], 'text/plain'
    raise Problem(422, 'CITATION_FORMAT_UNAVAILABLE', 'Choose native, csl-json, bibtex, biblatex, apa or mla.')


def register(tx, user, document, attribution, generated_at, *, runtime=None, retained_citation_metadata=(), retained_object_ids=()):
    """Called inside trusted scientific acceptance, never from a browser body.

    Reusing a digest grants access to its unchanged first registry revision.
    Missing source authorship, DOI and software version remain unknown.
    """
    base = setting('REVEAL_CANONICAL_URL', setting('NEXTAUTH_URL', 'http://localhost:3000')).rstrip('/')
    if urlsplit(base).scheme not in ('http', 'https') or urlsplit(base).username:
        raise Problem(503, 'RESOLVER_NOT_CONFIGURED', 'Configure an HTTP(S) canonical resolver origin.')
    stamp = now()
    provenance = {'source_ref': 'urn:reveal:registration:' + uid(), 'recorded_at': stamp}
    lock = json.loads((ROOT / 'services/backend/agent-runtime/dapper-release.json').read_text())
    records = []
    retained = {}
    for record in retained_citation_metadata:
        validate_record(record)
        key = record['target_id']
        previous = retained.setdefault(key, {})
        revision = record['metadata_revision']
        if revision in previous and previous[revision] != record:
            raise Problem(409,'REUSE_PAYLOAD_CONFLICT','Conflicting payloads for a pinned citation revision.')
        previous[revision] = deepcopy(record)
    for group, cls in [('knowledge_gaps', 'KnowledgeGap'), ('questions', 'Question'), ('claims', 'Claim')]:
        for node in document.get(group, []):
            identity = node['id']
            if identity in retained:
                # These records arrived through a server-authorized reuse
                # receipt. Retain the exact original attribution/revision;
                # borrowing never issues the legacy permanent target grant.
                records.extend(deepcopy(retained[identity][revision]) for revision in sorted(retained[identity]))
                continue
            if identity in retained_object_ids:
                # Missing historical metadata is unknown, not current authorship.
                continue
            existing = tx.get('citation', f'{identity}:1')
            grant(tx, user, identity)
            if existing:
                records.append(existing['data'])
                continue
            imported = cls != 'Claim'
            title = node.get('text') if imported else node.get('statement')
            if not isinstance(title, str) or not title.strip():
                raise Problem(422, 'CITATION_TITLE_MISSING', 'A citation requires the exact inquiry or assessment text.')
            byline = []
            activity = node.get('was_generated_by')
            if not imported:
                person = attribution.get('person_id')
                if not person:
                    # The trusted assembler has already replaced authored-node
                    # attribution with the actual submitting actor and minted it.
                    people = {p['id'] for p in document.get('persons', [])}
                    actors = [identity for identity in node.get('was_attributed_to', []) if identity in people]
                    if len(actors) == 1:
                        person = actors[0]
                if not person:
                    actor = tx.get('citation_actor', user)
                    if not actor:
                        actor = {'data': {'id': 'urn:reveal:actor:' + uid()}}
                        tx.put('citation_actor', user, user, actor['data'])
                    person = actor['data']['id']
                author = {'agent_id': person, 'kind': 'person', 'roles': ['agent_operator'],
                          'source': {'source_ref': person, 'recorded_at': attribution.get('observed_at', stamp)},
                          'display_name': attribution.get('display_name') or ('Anonymous researcher' if attribution.get('principal_kind') == 'anonymous' else None)}
                if attribution.get('orcid'):
                    author['orcid'] = {'id': attribution['orcid'], 'status': 'verified' if attribution.get('orcid_authenticated') else 'supplied', 'source': author['source']}
                byline.append(author)
                if runtime and runtime.get('model_id'):
                    byline.append({'agent_id': 'urn:reveal:software:' + digest(runtime), 'kind': 'software',
                        'roles': ['ai_generator'], 'source': provenance, 'provider': runtime.get('provider'),
                        'display_name': f"Claude {runtime['model_id']} via Claude Code (AI generator)",
                        'model_id': runtime['model_id'], 'model_version': runtime.get('model_version'),
                        'harness_name': 'Claude Code', 'harness_version': runtime.get('harness_version'),
                        'generation_activity': activity})
            record = {'target_id': identity, 'target_class': cls, 'dapper_schema_version': lock['commit'],
                'dapper_identity_profile': 'DAPPER-ID-1', 'object_payload_ref': base + '/api/backend/v1/objects/' + quote(identity, safe=''),
                'metadata_revision': 1, 'citation_profile_version': 'reveal-citation-v1', 'title': title,
                'title_derivation': {'method': 'inquiry_text' if imported else 'assessment_statement', 'version': '1', 'source': provenance},
                'language': None, 'byline': byline, 'origin': 'imported' if imported else 'native',
                'generated_at': None if imported else generated_at, 'first_minted_at': stamp, 'published_at': None,
                'issued_date': stamp[:10], 'issued_basis': 'first_minted_at', 'date_provenance': {'first_minted_at': provenance},
                'publisher': None, 'repository': 'REVEAL Mechanisms', 'canonical_url': base + '/id/' + quote(identity, safe=''),
                'doi': None, 'generation_activity': activity, 'access_level': 'private', 'publication_state': 'unpublished'}
            if imported:
                record.update(original_issued_date=None, imported_at=stamp)
                record['date_provenance']['imported_at'] = provenance
            elif generated_at:
                record['date_provenance']['generated_at'] = {'source_ref': activity or provenance['source_ref'], 'recorded_at': generated_at}
            record['metadata_checksum'] = digest(record)
            validate_record(record)
            tx.put('citation', f'{identity}:1', user, record)
            records.append(record)
    return records


def revise(tx, user, target, previous_revision, changes):
    previous = get(tx, user, target, previous_revision)
    allowed = {'title', 'title_derivation', 'language', 'notices'}
    if not set(changes) <= allowed:
        raise Problem(422, 'IMMUTABLE_CITATION_FIELD', 'Only presentation metadata may be revised here.')
    record = deepcopy(previous)
    record.update(deepcopy(changes), metadata_revision=previous_revision + 1)
    key = f'{target}:{record["metadata_revision"]}'
    if tx.get('citation', key):
        raise Problem(409, 'CITATION_REVISION_CONFLICT', 'This citation revision already exists.')
    record.pop('metadata_checksum', None)
    record['metadata_checksum'] = digest(record)
    validate_record(record)
    tx.put('citation', key, user, record)
    return record


def csl(record):
    item = {'id': record['target_id'] + '@' + str(record['metadata_revision']), 'type': 'webpage',
            'title': record['title'], 'genre': 'DAPPER ' + record['target_class'],
            'container-title': record['repository'], 'URL': record['canonical_url'],
            'archive': 'DAPPER', 'archive_location': record['target_id'][7:]}
    names = []
    for author in record['byline']:
        if author.get('family_name'):
            names.append({k: v for k, v in [('family', author['family_name']), ('given', author.get('given_name'))] if v})
        elif author.get('display_name'):
            names.append({'literal': author['display_name']})
    if names:
        item['author'] = names
    if record.get('issued_date'):
        item['issued'] = {'date-parts': [[int(x) for x in record['issued_date'].split('-')]]}
    if record.get('publisher'):
        item['publisher'] = record['publisher']
    if record.get('doi'):
        item['DOI'] = record['doi']
    return item


@lru_cache(maxsize=1)
def assets():
    manifest = json.loads((ASSETS / 'manifest.json').read_text())
    result = {}
    for name, expected in manifest['sha256'].items():
        raw = (ASSETS / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise Problem(503, 'CITATION_RUNTIME_INVALID', 'Pinned citation renderer checksum mismatch.')
        result[name] = raw.decode()
    return manifest, result


# The pinned processor runs warm: one QuickJS runtime per process holds citeproc.js and one engine per style, so a
# render costs the citation clusters (~60 ms), not building the APA engine (~2.4 s). QuickJS runtimes must only
# ever be used from the thread that created them, so a single dedicated thread owns it; it is rebuilt after an
# error or ENGINE_RENDERS renders. restoreProcessorState([]) gives each render citeproc's fresh registry, so labels,
# disambiguation and bibliography match a fresh engine's.
ENGINE_RENDERS = 500
_citeproc, _citeproc_lock = {}, threading.Lock()
RENDER_JS = '''var ENGINES = {}, input = null;
function engine(style) {
  return ENGINES[style] || (ENGINES[style] = new module.exports.Engine({retrieveLocale: function() { return LOCALE; },
    retrieveItem: function(id) { return input.items[id]; }}, STYLES[style], 'en-US', true));
}
function render(style) {
  var processor = ENGINES[style]; processor.restoreProcessorState([]);
  processor.setOutputFormat('text'); processor.updateItems(Object.keys(input.items));
  var prior=[], labels=[];
  input.keys.forEach(function(key,i){
    var result=processor.processCitationCluster({citationID:'occ-'+i,citationItems:[{id:key}],properties:{noteIndex:0}},prior,[]);
    result[1].forEach(function(change){labels[change[0]]=change[1];}); prior.push(['occ-'+i,0]);
  });
  var bib=processor.makeBibliography();
  return JSON.stringify({labels:labels, ids:bib ? bib[0].entry_ids : [], entries:bib ? bib[1] : []});
}'''


def citeproc_thread():
    """This process's citeproc thread (after a fork, a new one with a new runtime)."""
    with _citeproc_lock:
        if _citeproc.get('pid') != os.getpid():
            _citeproc.clear()
            _citeproc.update(pid=os.getpid(), thread=ThreadPoolExecutor(1, thread_name_prefix='citeproc'), state={})
        return _citeproc['thread'], _citeproc['state']


def _process(state, style, items, keys):
    """Runs only on the citeproc thread."""
    import quickjs
    try:
        if state.get('context') is None or state['renders'] >= ENGINE_RENDERS:
            state.clear(); _, files = assets()
            context = quickjs.Context()
            context.set_memory_limit(256 * 1024 * 1024)
            context.set_max_stack_size(8 * 1024 * 1024)
            context.set_time_limit(10)   # CPU seconds per eval
            context.eval('var module={exports:{}}; var console={log:function(){},warn:function(){}};')
            context.eval(files['citeproc.js'])
            context.eval('var LOCALE=' + json.dumps(files['en-US.xml']) + '; var STYLES=' +
                         json.dumps({name: files[name + '.csl'] for name in ('apa', 'mla')}) + ';' + RENDER_JS)
            state.update(context=context, renders=0)
        context = state['context']; state['renders'] += 1
        context.eval('engine(' + json.dumps(style) + '); true')
        return json.loads(context.eval('input=' + json.dumps({'items': items, 'keys': keys}) + '; render(' + json.dumps(style) + ')'))
    except BaseException:
        state.clear()   # never reuse an engine after a partial render
        raise


def render_records(records, occurrences, style, locale='en-US'):
    if style not in ('apa', 'mla') or locale != 'en-US':
        raise Problem(422, 'CITATION_STYLE_UNAVAILABLE', 'Available citation styles are APA/MLA with the pinned en-US locale.')
    manifest, files = assets()
    items = {item['id']: item for item in map(csl, records)}
    keys = [x['target_id'] + '@' + str(x['citation_metadata_revision']) for x in occurrences]
    thread, state = citeproc_thread()
    rendered = thread.submit(_process, state, style, items, keys).result()
    bibliography = []
    lookup = {r['target_id'] + '@' + str(r['metadata_revision']): r for r in records}
    for identities, value in zip(rendered['ids'], rendered['entries']):
        for identity in identities:
            record = lookup[identity]
            bibliography.append({'target_id': record['target_id'], 'citation_metadata_revision': record['metadata_revision'], 'text': value.strip()})
    return {'in_text': [{'occurrence_index': i, 'label': label} for i, label in enumerate(rendered['labels'])],
            'bibliography': bibliography, 'rendering_manifest': {'processor': 'citeproc-js', 'processor_version': manifest['processor_version'],
                'style_sha256': manifest['sha256'][style + '.csl'], 'locale_sha256': manifest['sha256']['en-US.xml'],
                'citation_profile': 'reveal-citation-v1', 'metadata_checksums': [digest(r) for r in records]}}


def prefetch(tx, user, keys):
    """Read every cited target's dependency row, exact registry revision and grant in one statement. get() then
    serves them from the transaction's identity map, so its authorization and error order are unchanged."""
    if not keys or not hasattr(tx, 'get_records'): return
    targets = list(dict.fromkeys(target for target, _ in keys))
    tx.get_records([('scientific_dependencies', digest([user, target])) for target in targets] +
                   [('citation', f'{target}:{revision}') for target, revision in keys] +
                   [('grant', digest([user, target])) for target in targets])


def load_paragraph(tx, user, identity):
    row = owned(tx, 'paragraph', identity, user)['data']
    result = row.get('result', row)
    matches = [p for p in result['document']['paragraphs'] if p['id'] == identity]
    if len(matches) != 1:
        raise Problem(422, 'INVALID_PARAGRAPH', 'The saved paragraph is ambiguous.')
    paragraph = matches[0]
    prefetch(tx, user, list(dict.fromkeys((occurrence['target_id'], occurrence['citation_metadata_revision'])
        for occurrence in paragraph.get('citations', []) if isinstance(occurrence, dict) and isinstance(occurrence.get('target_id'), str)
        and type(occurrence.get('citation_metadata_revision')) is int)))
    records, seen = [], set()
    for occurrence in paragraph.get('citations', []):
        start, end = occurrence['start'], occurrence['end']
        if not 0 <= start < end <= len(paragraph['text']) or ('exact_text' in occurrence and occurrence['exact_text'] != paragraph['text'][start:end]):
            raise Problem(422, 'INVALID_CITATION_SPAN', 'A saved citation does not match its Unicode text span.')
        key = (occurrence['target_id'], occurrence['citation_metadata_revision'])
        if key not in seen:
            records.append(get(tx, user, *key)); seen.add(key)
    errors = validator().check_citation_registry_links(paragraph, records)
    if errors:
        raise Problem(422, 'MISSING_CITATION_REVISION', '; '.join(errors))
    return paragraph, records


def render_paragraph(paragraph, records, paragraph_id, style='apa', locale='en-US'):
    """Pure: needs no transaction, so routes run it after their read lease is returned. Nothing is persisted."""
    return {'paragraph_id': paragraph_id, 'style': style, 'locale': locale,
            **render_records(records, paragraph.get('citations', []), style, locale)}


def render(tx, user, paragraph_id, style='apa', locale='en-US'):
    paragraph, records = load_paragraph(tx, user, paragraph_id)
    return render_paragraph(paragraph, records, paragraph_id, style, locale)


def tex(value):
    mapping = {'\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#', '_': r'\_', '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}
    return ''.join(mapping.get(c, c) for c in value)


def markdown_text(value):
    return re.sub(r'([\\`*_{}\[\]()#+.!<>|\-])', r'\\\1', value)


def bibkey(record):
    return record['target_id'] + '-r' + str(record['metadata_revision'])


def bibtex(records, biblatex=False):
    entries = []
    for record in records:
        names = []
        for author in record['byline']:
            if author.get('family_name'):
                names.append(tex(author['family_name']) + (', ' + tex(author['given_name']) if author.get('given_name') else ''))
            elif author.get('display_name'):
                names.append('{' + tex(author['display_name']) + '}')
        fields = {'title': '{' + tex(record['title']) + '}', 'url': tex(record['canonical_url']),
                  'howpublished': tex(record['repository'] + ': DAPPER ' + record['target_class']),
                  'note': tex('DAPPER ID: ' + record['target_id'] + '. Metadata revision ' + str(record['metadata_revision']) + '.'),
                  'archivePrefix': 'DAPPER', 'eprint': tex(record['target_id'][7:])}
        if names:
            fields['author'] = ' and '.join(names)
        if record.get('issued_date'):
            fields['date' if biblatex else 'year'] = record['issued_date'] if biblatex else record['issued_date'][:4]
        if record.get('doi'):
            fields['doi'] = tex(record['doi'])
        entries.append('@misc{' + bibkey(record) + ',\n' + ',\n'.join('  ' + k + ' = {' + v + '}' for k, v in fields.items()) + '\n}')
    return '\n\n'.join(entries) + ('\n' if entries else '')


def export(tx, user, paragraph_id, format='markdown'):
    if format not in ('markdown', 'latex', 'bibtex', 'rich-text'):
        raise Problem(422, 'EXPORT_FORMAT_UNAVAILABLE', 'Choose markdown, latex, bibtex or rich-text.')
    paragraph, records = load_paragraph(tx, user, paragraph_id)
    numbers = {(r['target_id'], r['metadata_revision']): i + 1 for i, r in enumerate(records)}
    ends = defaultdict(list)
    for occurrence in paragraph.get('citations', []):
        key = (occurrence['target_id'], occurrence['citation_metadata_revision'])
        if key not in ends[occurrence['end']]:
            ends[occurrence['end']].append(key)
    plain, rich, latex, markdown = [], [], [], []
    lookup = {(r['target_id'], r['metadata_revision']): r for r in records}
    for i, character in enumerate(paragraph['text'], 1):
        plain.append(character); rich.append(html.escape(character)); latex.append(tex(character)); markdown.append(markdown_text(character))
        if i in ends:
            keys = ends[i]
            plain.append(' [' + ', '.join(str(numbers[k]) for k in keys) + ']')
            markdown.append(' [' + ', '.join(str(numbers[k]) for k in keys) + ']')
            rich.append('<sup>' + ', '.join('<a href="' + html.escape(lookup[k]['canonical_url'], quote=True) + '">' + str(numbers[k]) + '</a>' for k in keys) + '</sup>')
            latex.append(r'\cite{' + ','.join(bibkey(lookup[k]) for k in keys) + '}')
    references = [f"[{i+1}] {r['title']} ({r.get('issued_date') or 'date unknown'}). {r['canonical_url']}" for i, r in enumerate(records)]
    plain_text = ''.join(plain) + ('\n\nReferences\n' + '\n'.join(references) if records else '')
    warnings = ['References retain exact citation metadata revisions. Access to private resolver links requires authorization.']
    if format == 'latex':
        content = '\\documentclass{article}\n\\usepackage[utf8]{inputenc}\n\\usepackage{hyperref}\n\\begin{document}\n' + ''.join(latex) + '\n\\bibliographystyle{plain}\n\\bibliography{references}\n\\end{document}\n'
        filename, media = 'research-statement.tex', 'application/x-tex'
        warnings.append('Download references.bib beside the .tex file and compile with BibTeX.')
    elif format == 'bibtex':
        content, filename, media = bibtex(records), 'references.bib', 'application/x-bibtex'
    elif format == 'rich-text':
        content = '<div><p>' + ''.join(rich).replace('\n', '<br>') + '</p>'
        if records:
            content += '<h2>References</h2><ol>' + ''.join('<li>' + html.escape(r['title']) + '. <a href="' + html.escape(r['canonical_url'], quote=True) + '">' + html.escape(r['target_id']) + '</a></li>' for r in records) + '</ol>'
        content += '</div>'
        filename, media = 'research-statement.html', 'text/html'
    else:
        # Text and titles are data, never executable HTML or authored Markdown links.
        content = ''.join(markdown)
        if records:
            content += '\n\n## References\n\n' + '\n\n'.join(
                f"[{i+1}] {markdown_text(r['title'])} ({r.get('issued_date') or 'date unknown'}). <{r['canonical_url']}>"
                for i, r in enumerate(records))
        filename, media = 'research-statement.md', 'text/markdown'
    return {'paragraph_id': paragraph_id, 'format': format, 'filename': filename, 'media_type': media,
            'content': content, 'plain_text': plain_text if format == 'rich-text' else None,
            'required_companions': ['references.bib'] if format == 'latex' else [],
            'citation_targets': [{'target_id': r['target_id'], 'citation_metadata_revision': r['metadata_revision']} for r in records], 'warnings': warnings}
