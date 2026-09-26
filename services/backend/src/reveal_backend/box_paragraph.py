"""Strict paragraph transport validation; scientific identity is backend-owned."""
from pathlib import Path
import json
import subprocess
import sys


def validate_paragraph_segments(output: dict, paragraph_input: dict) -> list[dict]:
    if paragraph_input.get('format') != 'reveal.paragraph-input/1':
        raise ValueError('Unsupported paragraph input format')
    if set(output) != {'format', 'segments'} or output['format'] != 'reveal.paragraph-output/1':
        raise ValueError('Paragraph output must contain only format and segments')
    allowed = {(c['target_id'], c['citation_metadata_revision']) for c in paragraph_input['allowed_citations']}
    segments = output['segments']
    if not isinstance(segments, list) or not 1 <= len(segments) <= 40:
        raise ValueError('Paragraph requires 1–40 segments')
    accounts = [x for x in paragraph_input['account_document'].get('scientific_accounts', []) if x.get('id') == paragraph_input.get('account_id')]
    if len(accounts) != 1:
        raise ValueError('Paragraph input must resolve one accepted ScientificAccount')
    claim_ids = set(accounts[0].get('component_claims', []))
    cited_claim = False
    size = 0
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) - {'text', 'citations'}:
            raise ValueError('Unexpected paragraph segment fields')
        text = segment.get('text')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Paragraph segment text must be nonblank')
        size += len(text)
        citations = segment.get('citations', [])
        if not isinstance(citations, list) or len(citations) > 25:
            raise ValueError('Invalid paragraph citations')
        seen = set()
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {'target_id', 'citation_metadata_revision'}:
                raise ValueError('Unexpected citation fields')
            if type(citation['citation_metadata_revision']) is not int or citation['citation_metadata_revision'] < 1:
                raise ValueError('Citation revision must be a positive integer')
            key = (citation['target_id'], citation['citation_metadata_revision'])
            if key not in allowed or key in seen:
                raise ValueError('Unavailable or duplicate citation target/revision')
            seen.add(key)
            cited_claim |= citation['target_id'] in claim_ids
    if size > 20000 or not cited_claim:
        raise ValueError('Paragraph is too long or does not cite an accepted account Claim')
    return segments


def assemble_paragraph(output: dict, paragraph_input: dict, *, dapper_root: Path, release_lock: Path) -> dict:
    """Validate exact allowed revisions then call the verified, pinned DAPPER function.

    The backend still supplies the Paragraph attribution/activity, runs DAPPER
    graph and registry validation, and mints the final immutable object.
    """
    from .dapper_release import verify_release
    verify_release(dapper_root, release_lock)
    segments = validate_paragraph_segments(output, paragraph_input)
    program = '''import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'schema'))
from scientific_claims import assemble_cited_text
print(json.dumps(assemble_cited_text(json.load(sys.stdin))))
'''
    result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(Path(dapper_root).resolve())],
                            input=json.dumps(segments), capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise ValueError('Pinned DAPPER paragraph assembly failed')
    return json.loads(result.stdout)
