"""Where in a paper a quoted passage sits.

An agent reads papers through the captured read_paper tool and quotes them as evidence. This module turns a quote
into something a third party can check without REVEAL publishing the paper's text:

  - the paper's text, re-derived from the upstream bytes the tool ledger captured, with the same code that served
    the read (box_literature.article_text), so it is byte-for-byte the text the agent saw;
  - the quote's exact byte span in that text, which is what a DAPPER ValueLocator records;
  - the paragraph and section it falls in, for people.

The text itself stays with REVEAL. Only its SHA-256 and size are recorded, so anyone holding the same Europe PMC
document and this extraction can rebuild the file and check the span.

A quote is located only inside text the agent was actually served, in one paragraph, and only when it occurs once in
the whole paper. Every refusal says what to change, because the same check runs while the agent is still drafting.

The verdict on a quote never turns from accepted to refused as the run reads more: reading more only widens what was
served. So a draft that linted clean is still clean when the backend checks it against the finished ledger.

The agent never names a capture. It writes the paper (`reference`) and the quote (`snippet`); `bind_sources` finds the
read that served the quote and attaches REVEAL's own record of it, in a copy the linter and the gate check. The
agent's saved draft is not changed.

Standard library only: this module is bundled into the Box.
"""
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

from .box_literature import abstract_record, article_blocks, parse_article

# Names the derivation of a text file from upstream bytes. Change it whenever article_blocks or the joining rule
# changes: the same upstream document would then give different bytes, and so a different file.
EXTRACTION = 'reveal.literature-text/1'
MIN_QUOTE_CHARACTERS = 20
# A locator carries the text it points at. The caps keep a deposited account from carrying the paper: one quote is a
# sentence or two, and all the quotes of one paper text together stay a small part of it.
MAX_QUOTE_CHARACTERS = 400
MAX_PAPER_QUOTE_CHARACTERS = 4000
# ...and of a short text such as an abstract, at most this share (one full-length quote is always allowed).
MAX_PAPER_QUOTE_SHARE = 0.4


class LocatorError(ValueError):
    """A quote that cannot be located as written. The message says what to change."""


class QuoteAbsent(LocatorError):
    """The quote is not text of the paper it was looked for in (it may be text of another paper the run read)."""


def _read(root, descriptor):
    data = (Path(root) / descriptor['path']).read_bytes()
    if hashlib.sha256(data).hexdigest() != descriptor['sha256'] or len(data) != descriptor['size_bytes']:
        raise LocatorError('A captured paper read does not match its recorded checksum')
    return data


def _merge(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [tuple(pair) for pair in merged]


def _full_text(raw, form, source, identity):
    """(kind, text, blocks, paper metadata) of one upstream document. blocks: (start, end, tag, section)."""
    if form == 'xml':
        article = parse_article(raw)
        blocks, position, section, texts = [], 0, None, []
        for tag, text in article_blocks(article):
            if tag in ('title', 'article-title'):
                section = ' '.join(text.split())[:80] or section
            blocks.append((position, position + len(text), tag, section))
            texts.append(text)
            position += len(text) + 1  # the newline that joins blocks
        paper = {'id': identity, 'source': source}
        title = article.find('.//article-meta/title-group/article-title')
        if title is not None:
            paper['title'] = ''.join(title.itertext())
        for node in article.findall('.//article-meta/article-id'):
            if node.get('pub-id-type') in ('doi', 'pmid'):
                paper[node.get('pub-id-type')] = ''.join(node.itertext()).strip()
        return 'oa_xml_text', '\n'.join(texts), blocks, paper
    record = abstract_record(raw, source, identity)
    if record is None:
        raise LocatorError('The captured read returned no paper record')
    text = record.get('abstractText') or ''
    if not isinstance(text, str):
        raise LocatorError('The captured abstract has an invalid shape')
    paper = {key: record[key] for key in ('id', 'source', 'pmid', 'pmcid', 'doi', 'title') if isinstance(record.get(key), str)}
    return 'abstract_html', text, [(0, len(text), 'abstract', None)], paper


def paper_texts(ledger_root, calls):
    """Every paper text the run was served, from the ledger's completed read_paper calls.

    Returns {(source, id, scope): paper}. A paper holds its full text, the character ranges actually served, its
    blocks, bibliographic metadata, the upstream capture it was derived from, and the response checksums of the
    reads that served it (a draft cites a read by its response File).

    `calls` is the ledger's entries: the frozen manifest's `calls`, or the live list while the run is going."""
    papers = {}
    for call in calls:
        if call.get('tool') != 'read_paper' or call.get('status') != 'completed' or not call.get('upstream_response'):
            continue
        response = json.loads(_read(ledger_root, call['response']))
        view = response.get('structuredContent') or {}
        data = view.get('data') or {}
        served = data.get('text')
        if not isinstance(served, str) or not served or type(data.get('offset')) is not int:
            continue
        key = (view.get('source'), view.get('paper_id'), view.get('scope'))
        paper = papers.get(key)
        if paper is None:
            if view.get('upstream_capture_complete') is not True:
                # The capture was cut at the byte limit: the text cannot be rebuilt, so nothing in it can be located.
                continue
            raw = _read(ledger_root, call['upstream_response'])
            form = 'xml' if call['upstream_response']['path'].endswith('.xml') else 'json'
            kind, text, blocks, metadata = _full_text(raw, form, key[0], key[1])
            encoded = text.encode('utf-8')
            paper = papers[key] = {
                'key': key, 'kind': kind, 'text': text, 'blocks': blocks, 'paper': {**metadata, **(data.get('paper') or {})},
                'served': [], 'sequences': [], 'captures': {}, 'reads': [],
                'text_sha256': hashlib.sha256(encoded).hexdigest(), 'text_bytes': len(encoded),
                'upstream': {'sha256': call['upstream_response']['sha256'], 'size_bytes': call['upstream_response']['size_bytes'],
                             'format': form, 'url': view.get('url'), 'retrieved_at': view.get('retrieved_at')}}
        start = data['offset']
        if paper['text'][start:start + len(served)] != served:
            # The source changed between two reads of one paper. This read is left out: evidence that cites it is
            # refused by evidence_quote, here and at the gate alike, and the rest of the run is unaffected.
            continue
        paper['served'] = _merge([*paper['served'], (start, start + len(served))])
        paper['sequences'].append(call['sequence'])
        paper['captures'][call['response']['sha256']] = call['sequence']
        paper['reads'].append({'sha256': call['response']['sha256'], 'size_bytes': call['response']['size_bytes'],
                               'sequence': call['sequence'], 'start': start, 'end': start + len(served)})
    return papers


def paper_for_capture(papers, sha256):
    """The paper a read_paper response File (cited by its sha256) served, or None."""
    return next((paper for paper in papers.values() if sha256 in paper['captures']), None)


def _where(paper, start):
    """Human-readable position of a character in a paper: its paragraph and section."""
    if paper['kind'] == 'abstract_html':
        return 'abstract'
    paragraph = 0
    for block_start, block_end, tag, section in paper['blocks']:
        if tag == 'p':
            paragraph += 1
        if block_start <= start <= block_end:
            label = f'paragraph={paragraph}' if tag == 'p' else 'heading'
            return label + (f'; section={section}' if section else '')
    return 'text'


def locate(paper, quote):
    """The exact span of `quote` in the paper's text.

    Whitespace in the quote may differ from the source (a line break for a space); everything else must match
    exactly. Returns byte_start, byte_length and value_text for a ValueLocator over the paper's text file, the
    character range, and a selector naming the paragraph. Raises LocatorError with what to change otherwise."""
    if not isinstance(quote, str) or not quote.strip():
        raise LocatorError('Evidence from a paper needs a snippet: a verbatim quote from the text you read')
    tokens = quote.split()
    compact = sum(len(token) for token in tokens)
    if compact < MIN_QUOTE_CHARACTERS:
        raise LocatorError(f'The snippet is too short to locate: quote at least {MIN_QUOTE_CHARACTERS} characters of the passage, verbatim')
    if len(' '.join(tokens)) > MAX_QUOTE_CHARACTERS:
        raise LocatorError(f'The snippet is too long: quote at most {MAX_QUOTE_CHARACTERS} characters, the sentence that carries the '
                           'finding. Put the rest in explanation, in your own words')
    text = paper['text']
    pattern = re.compile(r'\s+'.join(re.escape(token) for token in tokens))
    found = [match.span() for match in pattern.finditer(text)]
    if not found:
        raise QuoteAbsent('The snippet is not verbatim text of this paper. Copy the passage exactly as read_paper returned it '
                          '(same words, punctuation and symbols); only whitespace may differ')
    served = [span for span in found if any(start <= span[0] and span[1] <= end for start, end in paper['served'])]
    if not served:
        raise LocatorError('The snippet is in a part of this paper you were not served. Read that part with read_paper '
                           '(offset/next_offset) before quoting it')
    # Counted over the whole paper, not over what was served so far: a count over the served text would rise as the
    # agent reads on, and a quote accepted by an early lint would be refused after the run.
    if len(found) > 1:
        raise LocatorError(f'The snippet occurs {len(found)} times in this paper. Quote a longer passage that occurs once')
    start, end = served[0]
    block = next((item for item in paper['blocks'] if item[0] <= start and end <= item[1]), None)
    if block is None:
        raise LocatorError('The snippet runs across two paragraphs. Quote from one paragraph; use a second EvidenceItem for the other')
    value = text[start:end]
    if len(value) > MAX_QUOTE_CHARACTERS:   # runs of whitespace in the source that the quote wrote as single spaces
        raise LocatorError(f'The quoted passage is {len(value)} characters in the paper, over the {MAX_QUOTE_CHARACTERS} allowed. Quote a shorter passage')
    return {'byte_start': len(text[:start].encode('utf-8')), 'byte_length': len(value.encode('utf-8')), 'value_text': value,
            'character_start': start, 'character_end': end,
            'selector': f'{_where(paper, start)}; chars={start}-{end}',
            'media_type': 'text/html' if paper['kind'] == 'abstract_html' else 'text/plain'}


def captures(calls):
    """{response checksum: tool} for every tool response the run captured."""
    return {call['response']['sha256']: call.get('tool') for call in calls
            if isinstance(call.get('response'), dict) and call['response'].get('sha256')}


def evidence_quote(item, files, papers, captured):
    """(paper, span) for an EvidenceItem that quotes a paper, or None when it cites no captured tool response.

    `files` maps the document's File ids to their nodes, `papers` is paper_texts(), `captured` is captures().
    Raises LocatorError with what to change."""
    sources = item.get('was_derived_from') if isinstance(item.get('was_derived_from'), list) else []
    cited = [files[source].get('sha256') for source in sources if isinstance(source, str) and isinstance(files.get(source), dict)]
    cited = [sha256 for sha256 in cited if sha256 in captured]
    if not cited:
        return None
    quoted = {}
    for sha256 in cited:
        if captured[sha256] != 'read_paper':
            raise LocatorError(f'This EvidenceItem cites the result of {captured[sha256]}. A literature account cites paper reads '
                               'only: quote a passage that read_paper returned')
        paper = paper_for_capture(papers, sha256)
        if paper is None:
            raise LocatorError('This EvidenceItem cites a read_paper call that returned no text REVEAL can rebuild (an empty read, a '
                               'source document too large to keep whole, or one that changed between reads). Cite a read that '
                               "returned the passage: read the paper's abstract (scope=abstract), or another paper")
        quoted[paper['key']] = paper
    if len(quoted) > 1:
        raise LocatorError(f'This EvidenceItem cites reads of {len(quoted)} papers. Quote one paper per EvidenceItem')
    paper = next(iter(quoted.values()))
    span = locate(paper, item.get('snippet'))
    for slot in ('target_proposition', 'explanation'):
        if not isinstance(item.get(slot), str) or not item[slot].strip():
            raise LocatorError(f'Evidence quoted from a paper needs {slot}: say what the passage bears on and how')
    return paper, span


def document_quotes(document, papers, captured):
    """([(index into evidence_items, paper, span)], [(where, what to change)]) for a whole account document.

    The one rule for literature evidence. It runs while the agent drafts (box_remote.quote_findings) and again when
    the accepted account takes its deposit form (account_deposit); both read the result of this function, so they
    cannot disagree about a document."""
    files = {node.get('id'): node for node in document.get('files', []) if isinstance(node, dict) and isinstance(node.get('id'), str)}
    items = document.get('evidence_items') if isinstance(document.get('evidence_items'), list) else []
    located, problems, quoted = [], [], {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        where = item.get('id') if isinstance(item.get('id'), str) else 'evidence_items'
        try:
            quote = evidence_quote(item, files, papers, captured)
        except LocatorError as exc:
            problems.append((where, str(exc)))
            continue
        if quote is None:
            continue
        paper, span = quote
        located.append((index, paper, span))
        entry = quoted.setdefault(paper['key'], {'spans': [], 'where': where, 'length': len(paper['text'])})
        entry['spans'].append((span['character_start'], span['character_end'])); entry['where'] = where
    for entry in quoted.values():
        total = sum(end - start for start, end in _merge(entry['spans']))
        allowed = min(MAX_PAPER_QUOTE_CHARACTERS, max(MAX_QUOTE_CHARACTERS, int(entry['length'] * MAX_PAPER_QUOTE_SHARE)))
        if total > allowed:
            problems.append((entry['where'], f'This account quotes {total:,} characters of one {entry["length"]:,}-character text; '
                             f'the most is {allowed:,}. Keep the sentences that carry each finding and put the rest in '
                             'explanation, in your own words'))
    return located, problems


SERVED_TEXT = '/structuredContent/data/text'
BOUND_LOCATOR = re.compile(r'ledger_sequence=\d+;pointer=')


def paper_ids(paper):
    """The identifiers one read paper answers to: {('pmid', '123'), ('pmcid', 'PMC1'), ('doi', '10.1/x')}."""
    metadata, (source, identity, scope) = paper['paper'], paper['key']
    found = set()
    pmid = metadata.get('pmid') or (identity if source == 'MED' else None)
    pmcid = metadata.get('pmcid') or (identity if source == 'PMC' else None)
    if isinstance(pmid, str) and pmid.strip().isdigit():
        found.add(('pmid', pmid.strip()))
    if isinstance(pmcid, str) and re.fullmatch(r'(?i)PMC\d+', pmcid.strip()):
        found.add(('pmcid', pmcid.strip().upper()))
    if isinstance(metadata.get('doi'), str) and metadata['doi'].strip():
        found.add(('doi', metadata['doi'].strip().lower()))
    return found


def named_papers(reference):
    """The paper identifiers an EvidenceItem's `reference` names, in the forms paper_ids() gives. Empty when the
    reference is missing or names a paper some other way (a title, an author and year)."""
    if not isinstance(reference, str):
        return set()
    text = reference.strip()
    found = {('pmcid', 'PMC' + digits) for digits in re.findall(r'(?i)PMC\s*:?\s*(\d+)', text)}
    found |= {('pmid', digits) for digits in re.findall(r'(?i)\bPMID\s*:?\s*(\d+)', text)}
    found |= {('doi', doi.rstrip('.,;)').lower()) for doi in re.findall(r'\b(10\.\d+(?:\.\d+)*/[^\s"<>]+)', text)}
    if re.fullmatch(r'\d{1,9}', text):
        found.add(('pmid', text))
    return found


def capture_file(read):
    """REVEAL's File record of one read_paper response: the form the linter and the gate check a quote against.
    It never leaves REVEAL (account_deposit replaces it with the paper's text file)."""
    return {'id': 'urn:reveal:tool-response:sha256:' + read['sha256'], 'filename': read['sha256'] + '.json',
            'mime_type': 'application/json', 'sha256': read['sha256'], 'size_in_bytes': read['size_bytes']}


def bind_sources(document, papers, captured):
    """(a copy of `document` whose paper evidence cites the reads that served it, [(where, what to change)]).

    The agent writes a paper's evidence as a `reference` (PMID, PMC id or DOI) and a verbatim `snippet`. For each
    such EvidenceItem this finds the one read of this run that served the quote and attaches REVEAL's record of it:
    the capture File, the `was_derived_from` link, and the read's locator at the end of `context`. Nothing is
    guessed: a quote found in no paper read, or in two different papers with no reference to tell them apart, is
    reported with what to change and left unbound.

    An EvidenceItem that already cites a captured response is left as written (document_quotes judges it), and so
    is one that rests only on other Claims (`source_claims`, no snippet). `papers` is paper_texts(), `captured` is
    captures(). The same function runs in the draft lint and at the gate, so they cannot disagree."""
    bound = deepcopy(document)
    rows = bound.get('files') if isinstance(bound.get('files'), list) else []
    files = {node.get('id'): node for node in rows if isinstance(node, dict) and isinstance(node.get('id'), str)}
    items = bound.get('evidence_items') if isinstance(bound.get('evidence_items'), list) else []
    problems = []
    for item in items:
        if not isinstance(item, dict):
            continue
        where = item.get('id') if isinstance(item.get('id'), str) else 'evidence_items'
        sources = [ref for ref in item.get('was_derived_from', []) if isinstance(ref, str)] if isinstance(item.get('was_derived_from'), list) else []
        if any(isinstance(files.get(ref), dict) and files[ref].get('sha256') in captured for ref in sources):
            continue
        snippet = item.get('snippet')
        if not (isinstance(snippet, str) and snippet.strip()) and isinstance(item.get('source_claims'), list) and item['source_claims']:
            continue
        named = named_papers(item.get('reference'))
        pool = [paper for paper in papers.values() if not named or named & paper_ids(paper)]
        if not pool:
            problems.append((where, 'reference names a paper that read_paper did not return in this run. Evidence can quote only a '
                             'paper you read: set reference to the PMID, PMC id or DOI of the paper this snippet is from'
                             if named else 'No paper text was read in this run, so there is nothing to quote. Read a paper with '
                             'read_paper, then quote a passage it returned'))
            continue
        found, refusals = [], []
        for paper in pool:
            try:
                found.append((paper, locate(paper, snippet)))
            except LocatorError as exc:
                refusals.append(exc)
        if not found:
            # A refusal that is about the quote itself (too short, twice in the paper, not yet served) says more than
            # "not in this paper", which every other paper read also answers.
            specific = next((exc for exc in refusals if not isinstance(exc, QuoteAbsent)), None)
            if specific is None and not named and len(pool) > 1:
                problems.append((where, 'The snippet is not verbatim text of any paper read in this run. Copy the passage exactly '
                                 'as read_paper returned it (same words, punctuation and symbols); only whitespace may differ'))
            else:
                problems.append((where, str(specific or refusals[0])))
            continue
        # The abstract and the full text of one paper share an identifier and are one paper; anything else is two.
        works = []
        for paper, _ in found:
            ids = paper_ids(paper)
            known = next((work for work in works if work & ids), None)
            if known is None:
                works.append(set(ids))
            else:
                known |= ids
        if len(works) > 1:
            problems.append((where, 'This snippet occurs in more than one paper read in this run. Set reference to the PMID, PMC '
                             'id or DOI of the paper you are quoting'))
            continue
        # One paper read both as an abstract and as full text: the full text, then the earliest read.
        paper, span = min(found, key=lambda hit: (hit[0]['kind'] != 'oa_xml_text', min(hit[0]['sequences'])))
        read = min((entry for entry in paper['reads'] if entry['start'] <= span['character_start'] and span['character_end'] <= entry['end']),
                   key=lambda entry: entry['sequence'], default=None)
        if read is None:
            problems.append((where, 'The snippet runs across two separate reads of this paper. Quote a passage that lies inside '
                             'one read'))
            continue
        node = capture_file(read)
        if node['id'] not in files:
            files[node['id']] = node
            rows.append(node)
        item['was_derived_from'] = [*sources, node['id']]
        context = item.get('context')
        if isinstance(context, str) and context.strip() and not BOUND_LOCATOR.search(context):
            context = context.rstrip()
            item['context'] = (context + ('' if context.endswith(('.', '!', '?', ';', ':')) else '.')
                               + f" Quote location: ledger_sequence={read['sequence']};pointer={SERVED_TEXT}.")
    if rows:
        bound['files'] = rows
    return bound, problems


def text_bytes(paper):
    """The paper text file's bytes: what a locator's byte span addresses."""
    return paper['text'].encode('utf-8')


def _label(paper):
    source, identity, scope = paper['key']
    return f"{'PMID ' if source == 'MED' else ''}{identity} {'abstract' if scope == 'abstract' else 'full text'}"


def text_file(paper):
    """The DAPPER File for a paper's text: named by checksum, with no location. REVEAL holds the bytes and does not
    redistribute them; the description says how to rebuild them."""
    source, identity, scope = paper['key']
    html = paper['kind'] == 'abstract_html'
    origin = ('the abstractText of its Europe PMC record' if html else
              'the title and paragraph text of its Europe PMC open-access full-text XML, in document order, joined by newlines')
    return {'filename': f"{identity}.{'abstract.html' if html else 'fulltext.txt'}",
            'mime_type': 'text/html' if html else 'text/plain', 'sha256': paper['text_sha256'], 'size_in_bytes': paper['text_bytes'],
            'description': (f'Text of {_label(paper)} as read by a REVEAL agent: {origin} (UTF-8, extraction {EXTRACTION}). '
                            f"Derived from the upstream document with SHA-256 {paper['upstream']['sha256']}. The text is not "
                            'redistributed; this record identifies it by checksum so a quoted span can be re-checked against a '
                            'copy rebuilt from the same document.')}


def upstream_file(paper):
    """The DAPPER File for the upstream document a paper's text was derived from. Its location is public."""
    source, identity, scope = paper['key']
    upstream = paper['upstream']
    node = {'filename': f"{identity}.europepmc.{upstream['format']}",
            'mime_type': 'application/xml' if upstream['format'] == 'xml' else 'application/json',
            'sha256': upstream['sha256'], 'size_in_bytes': upstream['size_bytes'],
            'description': f'Europe PMC response for {_label(paper)}, exactly as fetched'
                           + (f" at {upstream['retrieved_at']}" if upstream.get('retrieved_at') else '')
                           + '. A later fetch of the same address may differ in bytes.'}
    if isinstance(upstream.get('url'), str) and upstream['url'].startswith('https://'):
        node['location'] = upstream['url']
    return node


def publication(paper):
    """The DAPPER Publication a paper's reads describe, from the identifiers Europe PMC returned."""
    metadata, (source, identity, scope) = paper['paper'], paper['key']
    pmid = metadata.get('pmid') or (identity if source == 'MED' else None)
    pmcid = metadata.get('pmcid') or (identity if source == 'PMC' else None)
    node = {}
    if isinstance(metadata.get('title'), str) and metadata['title'].strip():
        node['title'] = ' '.join(metadata['title'].split())
    if isinstance(pmid, str) and pmid.isdigit():
        node['pmid'] = pmid
    if isinstance(metadata.get('doi'), str) and metadata['doi'].strip():
        node['doi'] = 'doi:' + metadata['doi'].strip()
    if isinstance(pmcid, str) and pmcid.startswith('PMC'):
        node['citation'] = 'PMCID:' + pmcid  # DAPPER's Publication has no PMC id slot
    return node


def reference(paper):
    """A CURIE for the paper, for places that cannot hold a Publication node."""
    node = publication(paper)
    return 'PMID:' + node['pmid'] if 'pmid' in node else node.get('citation') or node.get('doi')
