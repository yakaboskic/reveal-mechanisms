"""Archive-on-reload: user work built on a superseded reference generation (docs/reference-reload.md §3-§4).

A reload never deletes scientific work. This module
- classifies every <prefix>_records kind (KIND_ACTIONS; `plan_prefix` fails closed on unknown kinds);
- freezes the factors user work references into `archived_reference_factors` (never purged);
- backfills `request_binding.anchor_display` from the saved draft selection;
- stamps archived rows with `reference_generation.build_stamp`, placed per §4.2;
- drops drafts anchored to a superseded generation (gap-only drafts stay) and cancels
  uncollected analysis jobs.

Stamping bumps only the row version: logical payload versions (draft/publication/outcome
publication `version`), `provenance` and account `result` are never touched. Every pass is
idempotent and runs one repository transaction per owner. SQL helpers take an explicit
DB-API connection; retained source capture uses an explicit, read-only artifact reader.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import re

from .reference_generation import (ARCHIVE_RUN_KIND, CATALOG_OWNER, GENERATION_RE, KPN_KIND, KPN_MODEL, LEGACY_KIND, LEGACY_MODEL, MODELS,
    PROJECTION_SCOPE, ReferenceError, archive_id, build_stamp, generation_of_anchors, generation_of_binding, mechanism_node,
    model_of_source_id, parse_factor_key, parse_public_id, public_stamp, read_active)
from .repository import canonical, digest, now

ARCHIVE, KEEP, DROP_ANCHORED_DRAFT, CANCEL_NONTERMINAL, PURGE_AT_RETIRE = (
    'archive', 'keep', 'drop_anchored_draft', 'cancel_nonterminal', 'purge_at_retire')

# Where each archived kind carries its stamp (§4.2): the dict path that receives `archive`.
PLACEMENT = {'account': ('summary',), 'account_membership': ('summary',), 'publication': ('summary',),
             'publication_snapshot': ('summary',), 'analysis_outcome': ('record',), 'outcome_snapshot': ('record',),
             'outcome_summary': (), 'outcome_publication': ('summary',), 'request': ()}
ARCHIVE_KINDS = tuple(PLACEMENT)
# Public copies never carry private job/request ids (reference_generation.public_stamp).
PUBLIC_KINDS = frozenset({'publication', 'publication_snapshot', 'outcome_publication', 'outcome_snapshot'})

# Every kind the backend writes (repository put/insert_many/update_existing across reveal_backend).
KIND_ACTIONS: dict[str, str] = {
    **dict.fromkeys(ARCHIVE_KINDS, ARCHIVE),
    # Dropped only when anchored to a superseded generation, after the anchor_display backfill.
    'draft': DROP_ANCHORED_DRAFT, 'draft_binding': DROP_ANCHORED_DRAFT,
    # Terminal jobs are kept; uncollected non-terminal analysis jobs are cancelled.
    'job': CANCEL_NONTERMINAL,
    # Retrieval caches and superseded vector snapshots, kept for rollback until purge-retired.
    **dict.fromkeys(('suggestion', 'vector_snapshot', 'vector_batch'), PURGE_AT_RETIRE),
    **dict.fromkeys((
        # The frozen analysis and its provenance; content-addressed rows are never stamped.
        'request_binding', 'evidence', 'artifact', 'queue', 'attempt', 'execution', 'dispatch', 'event', 'remote_event',
        'analysis_outcome_by_job', 'scientific_document', 'object', 'object_document', 'object_observation', 'paragraph',
        'citation', 'citation_rendering', 'grant', 'exploration', 'outbox', 'notification_outbox', 'idempotency',
        'workspace_event', 'workspace_cursor',
        # Private upload metadata and immutable fixture receipts are independent
        # of the reference generation; frozen requests retain their input refs.
        'upload', 'fixture_seed',
        # Identities.
        'principal', 'identity', 'transfer', 'citation_actor',
        # Community ballots and their public tallies (votes.py), keyed by gap or published account id and never
        # by a factor: they stay valid across generations and are never stamped.
        'vote', 'vote_total',
        # Infrastructure: workers, durable workflow, vector bookkeeping and the reference pointers.
        'worker_control', 'runtime', 'probe_result', 'workflow_activity', 'workflow_cleanup', 'workflow_cleanup_completed',
        'workflow_control', 'workflow_delivery', 'workflow_dispatch', 'workflow_recovery_audit', 'workflow_step',
        'vector_active', 'vector_archive', 'vector_dispatch', 'vector_failure', 'vector_import_request', 'vector_inventory',
        'vector_quality', 'reference_active', 'reference_control', ARCHIVE_RUN_KIND, 'reference_reload'), KEEP)}


def classify(kind: str) -> str | None:
    """The cutover action of a record kind, or None for a kind no one has classified (fail closed)."""
    return KIND_ACTIONS.get(kind)


def source_id_sha256(source_id: str) -> str:
    """archived_reference_factors.source_id_sha256: sha256 of the UTF-8 source id."""
    return hashlib.sha256(source_id.encode('utf-8')).hexdigest()


# --------------------------------------------------------------------------------------
# Stamp placement


def _target(kind: str, payload):
    if kind not in PLACEMENT: raise ReferenceError(f'Record kind {kind!r} is never archived')
    target = payload
    for key in PLACEMENT[kind]: target = target.get(key) if isinstance(target, dict) else None
    return target if isinstance(target, dict) else None


def stamp_of(kind: str, payload) -> dict | None:
    """The archive stamp a row of `kind` currently carries, from its §4.2 place."""
    target = _target(kind, payload)
    stamp = target.get('archive') if target is not None else None
    return stamp if isinstance(stamp, dict) else None


def stamp_payload(kind: str, payload: dict, stamp: dict | None) -> dict:
    """A copy of `payload` carrying `stamp` in its §4.2 place; public kinds get public_stamp.

    Rows without that place (an unpublished publication's null summary) are returned unchanged.
    Nothing else is touched: not `result`, `provenance` or any logical payload version.
    """
    result = deepcopy(payload)
    target = _target(kind, result)
    if target is not None and stamp: target['archive'] = public_stamp(stamp) if kind in PUBLIC_KINDS else deepcopy(stamp)
    return result


# --------------------------------------------------------------------------------------
# Generations and frozen anchors of stored records

ANCHOR_FIELDS = ('source_id', 'mechanism_id', 'factor_id', 'trait', 'kpn_trait_id', 'label', 'name', 'origin',
                 'archived_reference_factor_id')
MECHANISM_RE = re.compile(r'EAGGL mechanism (\S+?)\.(?:\s|$)')
SOURCE_LABEL_RE = re.compile(r'Source label: (.*)\.\s*$', re.S)
KPN_TRAIT_TEXT_RE = re.compile(r'KPN trait (KPN\.TRAIT:\d{7}) \((.*?)\)\.')
FACTOR_SUFFIX_RE = re.compile(r'\s*\(Factor\d+\)\s*$', re.I)


def _source_binding_generation(item: dict) -> str:
    """Generation of an outcome provenance.source_bindings entry, which copies only run ids.

    KPN bindings set mapping_run_id to the generation id itself (docs §4)."""
    if item.get('reference_generation_id'): return item['reference_generation_id']
    if model_of_source_id(item.get('source_id')) == KPN_MODEL:
        if GENERATION_RE.fullmatch(item.get('mapping_run_id') or ''): return item['mapping_run_id']
        raise ReferenceError('KPN source binding has no generation')
    return generation_of_binding(item)


def _single(generations) -> str | None:
    values = set(generations)
    if len(values) > 1: raise ReferenceError('Anchors span several reference generations')
    return next(iter(values), None)


def _model(bindings=(), source_ids=()) -> str:
    for binding in bindings or ():
        if binding.get('model') in MODELS: return binding['model']
        if binding.get('reference_generation_id'): return KPN_MODEL
        if binding.get('mapping_run_id'): return LEGACY_MODEL
    return next((model for model in map(model_of_source_id, source_ids) if model), LEGACY_MODEL)


def _parse_mechanism(node) -> dict | None:
    """Factor source id and label named by a catalog Mechanism node's description."""
    description = (node or {}).get('description') or ''
    match = MECHANISM_RE.match(description)
    if not match or not model_of_source_id(match.group(1)): return None
    label, trait = SOURCE_LABEL_RE.search(description), KPN_TRAIT_TEXT_RE.search(description)
    return {'source_id': match.group(1), 'label': label.group(1) if label else None,
            'kpn_trait_id': trait.group(1) if trait else None, 'phenotype': trait.group(2) if trait else None}


def _implied(source_id: str) -> dict:
    """factor_id / trait / kpn_trait_id implied by a factor source id alone."""
    parts, model = source_id.split(':'), model_of_source_id(source_id)
    if model == LEGACY_MODEL: return {'factor_id': f'{parts[2]}::{parts[4]}', 'trait': parts[2], 'kpn_trait_id': None}
    if model == KPN_MODEL:
        try: return {'factor_id': None, 'trait': None, 'kpn_trait_id': parse_public_id(source_id)['kpn_trait_id']}
        except ReferenceError: pass
    return {'factor_id': None, 'trait': None, 'kpn_trait_id': None}


def _anchor(source_id, generation_id, *, binding=None, mechanism_id=None, node=None, label=None, subtitle=None,
            trait=None, origin=None) -> dict:
    binding, node = binding or {}, node or {}
    implied, parsed = _implied(source_id), _parse_mechanism(node) or {}
    factor_id = binding.get('eaggl_factor_id') or implied['factor_id']
    from_subtitle = FACTOR_SUFFIX_RE.sub('', subtitle).strip() if isinstance(subtitle, str) else ''
    return {'source_id': source_id, 'mechanism_id': mechanism_id or node.get('id'), 'factor_id': factor_id,
            'trait': trait or from_subtitle or (factor_id.split('::')[0] if factor_id and '::' in factor_id else None)
                     or implied['trait'] or parsed.get('phenotype'),
            'kpn_trait_id': binding.get('kpn_trait_id') or implied['kpn_trait_id'] or parsed.get('kpn_trait_id'),
            'label': label or parsed.get('label'), 'name': node.get('name'), 'origin': origin,
            'archived_reference_factor_id': archive_id(generation_id, source_id)}


def anchors_for_stamp(request_binding, *, draft_binding=None, scientific_document=None, composer=None, generation_id) -> list[dict]:
    """The frozen anchors of an archive stamp (`archive.reference.anchors`, docs §4.1).

    Built from request_binding.anchors + anchor_display. Fallbacks, in order: the saved
    draft_binding selection holding the same frozen binding, the frozen Mechanism nodes of
    `scientific_document` (a DAPPER document, a scientific_document row or request.document)
    and `composer` (origins and dapper ids). archived_reference_factor_id is
    archive_id(generation_id, source_id).
    """
    document = scientific_document or {}
    if isinstance(document.get('document'), dict): document = document['document']
    mechanisms = [node for node in document.get('mechanisms') or [] if isinstance(node, dict)]
    nodes = {node['id']: node for node in mechanisms if node.get('id')}
    named = {parsed['source_id']: node for node in mechanisms if (parsed := _parse_mechanism(node))}
    selections = (draft_binding or {}).get('selections') or {}
    chosen = [item for item in (composer or {}).get('eaggl_anchors') or [] if isinstance(item, dict)]
    references = {(item.get('reference') or {}).get('source_id'): item.get('reference') or {} for item in chosen}
    origins = {(item.get('reference') or {}).get('source_id'): item.get('origin') for item in chosen}
    bindings = [item for item in (request_binding or {}).get('anchors') or [] if item.get('cfde_node_id')]
    if bindings:
        displays, anchors = request_binding.get('anchor_display') or {}, []
        for binding in bindings:
            native = binding['cfde_node_id']; display = displays.get(native) or {}
            selection = selections.get(native) or {}
            # A draft selection describes this anchor only while it holds the same frozen binding.
            record = (selection.get('record') or {}) if selection.get('binding') == binding else {}
            shown = record.get('cfde_anchor') or {}
            mechanism_id = ((display.get('reference') or {}).get('dapper_id') or (record.get('object') or {}).get('id')
                            or references.get(native, {}).get('dapper_id'))
            node = nodes.get(mechanism_id) or record.get('object') or named.get(native)
            anchors.append(_anchor(native, generation_id, binding=binding, mechanism_id=mechanism_id, node=node,
                label=display.get('label') or shown.get('label'), subtitle=display.get('subtitle') or shown.get('subtitle'),
                origin=origins.get(native)))
        return anchors
    if selections:
        order = [native for native in references if native in selections] or sorted(selections)
        anchors = []
        for native in order:
            selection = selections[native]; record = selection.get('record') or {}; shown = record.get('cfde_anchor') or {}
            anchors.append(_anchor(native, generation_id, binding=selection.get('binding'),
                mechanism_id=(record.get('object') or {}).get('id') or (selection.get('reference') or {}).get('dapper_id'),
                node=record.get('object'), label=shown.get('label'), subtitle=shown.get('subtitle'), origin=origins.get(native)))
        return anchors
    # Last resort: frozen catalog Mechanism nodes, whose descriptions name the factor and its label.
    return [_anchor(native, generation_id, node=node, origin=origins.get(native)) for native, node in named.items()]


def _outcome_anchors(record: dict, generation_id: str) -> list[dict]:
    """Stamp anchors from an outcome's own frozen anchors (analysis_outcomes.prepare)."""
    return [_anchor(item['source_id'], generation_id, mechanism_id=item.get('mechanism_id'), node={'name': item.get('name')},
                    label=item.get('name'), trait=item.get('trait'), origin=item.get('origin'))
            for item in record.get('anchors') or [] if isinstance(item, dict) and item.get('source_id')]


def _account_id(kind: str, data: dict):
    summary = data.get('summary') if isinstance(data.get('summary'), dict) else {}
    identity = (summary.get('account') or {}).get('id')
    if kind == 'account': return identity or (data.get('result') or {}).get('root_id')
    return data.get('account_id') or identity


def _gap(selected=None, bound=None, knowledge_gap=None) -> dict | None:
    """The stamp's gap {id, source_id, source_revision}: composer selection, bound catalog gap, or gap object."""
    if isinstance(selected, dict) and selected.get('id'):
        return {key: selected.get(key) for key in ('id', 'source_id', 'source_revision')}
    bound = bound if isinstance(bound, dict) else {}
    if (bound.get('object') or {}).get('id'):
        source = bound.get('source') or {}
        return {'id': bound['object']['id'], 'source_id': source.get('source_id'), 'source_revision': source.get('source_revision')}
    if isinstance(knowledge_gap, dict) and knowledge_gap.get('id'):
        return {'id': knowledge_gap['id'], 'source_id': None, 'source_revision': None}
    return None


# --------------------------------------------------------------------------------------
# One owner's rows, read once inside that owner's transaction


class _Owner:
    """Survey (and, when applying, rewrite) one owner's rows for a cutover to `to_generation`.

    `fallback` is the generation assumed for rows whose own generation cannot be derived
    (only the legacy generation: before any reload it is the only one ever used), or None.
    """
    STAMP_ORDER = ('account', 'account_membership', 'publication_snapshot', 'publication', 'analysis_outcome',
                   'outcome_summary', 'outcome_snapshot', 'outcome_publication', 'request')

    def __init__(self, tx, owner, *, to_generation=None, fallback=None, at=None):
        self.tx, self.owner, self.to, self.fallback, self.at = tx, owner, to_generation, fallback, at
        self.loaded, self.contexts = {}, {}
        self.unresolved, self.stamps, self.drops, self.backfilled = [], [], [], {}
        self.unrecoverable, self.missing = [], 0
        self.counts, self.restamped, self.already, self.current, self.skipped = Counter(), Counter(), Counter(), Counter(), Counter()

    def rows(self, kind):
        # Oldest update first, so rewriting keeps the relative updated_at order of listings.
        if kind not in self.loaded: self.loaded[kind] = list(reversed(self.tx.list(kind, self.owner)))
        return self.loaded[kind]

    def index(self, kind):
        key = ('index', kind)
        if key not in self.contexts: self.contexts[key] = {row['id']: row for row in self.rows(kind)}
        return self.contexts[key]

    @property
    def bindings(self):
        if 'bindings' not in self.contexts: self.contexts['bindings'] = {row['id']: row['data'] for row in self.rows('request_binding')}
        return self.contexts['bindings']

    def data(self, kind, identity):
        row = self.index(kind).get(identity) if identity else None
        return row['data'] if row else {}

    def job_of_request(self, request_id):
        if 'jobs_by_request' not in self.contexts:
            found = {}
            for row in self.rows('job'):
                job = row['data']
                if job.get('kind') == 'analysis' and job.get('research_request_id'): found.setdefault(job['research_request_id'], row)
            self.contexts['jobs_by_request'] = found
        return self.contexts['jobs_by_request'].get(request_id)

    def accounts(self):
        if 'accounts' not in self.contexts:
            found = defaultdict(dict)
            for kind in ('account', 'account_membership'):
                for row in self.rows(kind):
                    identity = _account_id(kind, row['data'])
                    if identity: found[identity].setdefault(kind, row)
            self.contexts['accounts'] = found
        return self.contexts['accounts']

    # -- anchor_display backfill (scripts/recover_analysis_outcome.py) -------------------
    def backfill(self):
        from .analysis_outcomes import anchor_display
        missing = 0
        for row in self.rows('request_binding'):
            binding = self.bindings[row['id']]
            displays = binding.get('anchor_display') if isinstance(binding.get('anchor_display'), dict) else None
            wanted = [item['cfde_node_id'] for item in binding.get('anchors') or [] if item.get('cfde_node_id')]
            absent = [native for native in wanted if native not in (displays or {})]
            if not absent: continue
            missing += 1
            request = self.data('request', row['id']); saved = self.index('draft_binding').get(request.get('source_draft_id'))
            recovered = {}
            if saved and request.get('composer'):
                recovered = {native: value for native, value in anchor_display(request['composer'], binding, saved['data']).items()
                             if native in absent}
            if recovered:
                self.bindings[row['id']] = dict(binding, anchor_display={**recovered, **(displays or {})})
                self.backfilled[row['id']] = row
            left = [native for native in absent if native not in recovered]
            if left: self.unrecoverable.append({'id': row['id'], 'owner': self.owner, 'missing': left})
        self.missing = missing
        return missing

    # -- generation, reference, gap and analysis of each archived row --------------------
    def request_reference(self, request_id):
        key = ('request_reference', request_id)
        if key in self.contexts: return self.contexts[key]
        binding = self.bindings.get(request_id) if request_id else None
        result = None, 'request_binding_missing'
        if binding is not None:
            try: generation = generation_of_anchors(binding.get('anchors'))
            except ReferenceError: generation = None; result = None, 'anchor_generation_unresolved'
            else:
                if generation is None: result = None, 'no_anchors'
            if generation:
                request = self.data('request', request_id); saved = self.index('draft_binding').get(request.get('source_draft_id'))
                anchors = anchors_for_stamp(binding, draft_binding=saved['data'] if saved else None,
                    scientific_document=request.get('document'), composer=request.get('composer'), generation_id=generation)
                result = {'generation': generation, 'reference': {'model': _model(binding.get('anchors')), 'anchors': anchors}}, None
        self.contexts[key] = result
        return result

    def request_gap(self, request_id, *, selected=None, knowledge_gap=None):
        request = self.data('request', request_id)
        return _gap((request.get('composer') or {}).get('source_gap') or selected,
                    (self.bindings.get(request_id) or {}).get('source_gap') if request_id else None, knowledge_gap)

    def finish(self, found, reason, gap, analysis, anchors, generation=None):
        """Complete a context; `anchors(generation)` builds frozen anchors when the binding is unusable."""
        context = {'gap': gap, 'analysis': analysis, 'reason': reason, 'fallback': False}
        if found: return {**context, **found, 'reason': None}
        if generation: context['reason'] = None
        elif self.fallback: generation = self.fallback; context['fallback'] = True
        else: return {**context, 'generation': None, 'reference': None}
        items = anchors(generation)
        return {**context, 'generation': generation,
                'reference': {'model': _model((), [item['source_id'] for item in items]), 'anchors': items}}

    def account_context(self, account_id, *, summary=None, document=None):
        key = ('account', account_id)
        if key in self.contexts: return self.contexts[key]
        rows = self.accounts().get(account_id) or {}
        stored = rows.get('account') or rows.get('account_membership')
        summary = (stored['data'].get('summary') if stored else None) or summary or {}
        if rows.get('account'): document = (rows['account']['data'].get('result') or {}).get('document') or document
        job = self.data('job', summary.get('job_id')); request_id = job.get('research_request_id')
        found, reason = self.request_reference(request_id) if request_id else (None, 'job_missing' if summary.get('job_id') else 'job_unknown')
        analysis = {'job_id': summary.get('job_id'), 'request_id': request_id,
                    'evidence_package_sha256': (job.get('result') or {}).get('evidence_package_sha256'), 'account_id': account_id, 'outcome_id': None}
        context = self.finish(found, reason, self.request_gap(request_id, knowledge_gap=summary.get('knowledge_gap')), analysis,
                              lambda generation: anchors_for_stamp(None, scientific_document=document, generation_id=generation))
        self.contexts[key] = context
        return context

    def outcome_context(self, outcome_id, *, record=None):
        key = ('outcome', outcome_id)
        if key in self.contexts: return self.contexts[key]
        record = self.data('analysis_outcome', outcome_id).get('record') or record or {}
        job_id = record.get('job_id'); job = self.data('job', job_id); request_id = job.get('research_request_id')
        found, reason = self.request_reference(request_id) if request_id else (None, 'job_missing' if job_id else 'job_unknown')
        generation = None
        if not found:
            try: generation = _single(_source_binding_generation(item) for item in (record.get('provenance') or {}).get('source_bindings') or [])
            except ReferenceError: reason = 'source_bindings_unresolved'
        provenance = record.get('provenance') or {}
        analysis = {'job_id': job_id, 'request_id': request_id, 'evidence_package_sha256': provenance.get('evidence_package_sha256'),
                    'account_id': None, 'outcome_id': outcome_id}
        context = self.finish(found, reason, self.request_gap(request_id, selected=record.get('source_gap'), knowledge_gap=record.get('knowledge_gap')),
                              analysis, lambda value: _outcome_anchors(record, value), generation)
        self.contexts[key] = context
        return context

    def request_context(self, request_id):
        key = ('request', request_id)
        if key in self.contexts: return self.contexts[key]
        request = self.data('request', request_id); job_row = self.job_of_request(request_id)
        job = job_row['data'] if job_row else {}; result = job.get('result') if isinstance(job.get('result'), dict) else {}
        outcome = self.data('analysis_outcome_by_job', job_row['id']).get('id') if job_row else None
        analysis = {'job_id': job_row['id'] if job_row else None, 'request_id': request_id,
                    'evidence_package_sha256': result.get('evidence_package_sha256'),
                    'account_id': next(iter(result.get('account_ids') or []), None), 'outcome_id': outcome or result.get('outcome_id')}
        found, reason = self.request_reference(request_id)
        context = self.finish(found, reason, self.request_gap(request_id, knowledge_gap={'id': request.get('question_id')}), analysis,
            lambda generation: anchors_for_stamp(None, scientific_document=request.get('document'), composer=request.get('composer'), generation_id=generation))
        self.contexts[key] = context
        return context

    def context(self, kind, row):
        data = row['data']
        if kind in ('account', 'account_membership'): return self.account_context(_account_id(kind, data))
        if kind in ('publication', 'publication_snapshot'):
            return self.account_context(_account_id(kind, data), summary=data.get('summary'), document=data.get('document'))
        if kind == 'analysis_outcome': return self.outcome_context(row['id'])
        if kind == 'outcome_summary': return self.outcome_context(row['id'], record=data)
        if kind == 'outcome_publication': return self.outcome_context(row['id'], record=data.get('summary'))
        if kind == 'outcome_snapshot':
            record = data.get('record') or {}
            return self.outcome_context(record.get('id') or row['id'], record=record)
        return self.request_context(row['id'])

    def unresolve(self, kind, row, reason, archived):
        self.unresolved.append({'kind': kind, 'id': row['id'], 'owner': self.owner, 'reason': reason, 'archived': archived})

    # -- the archive pass ---------------------------------------------------------------
    def survey_stamps(self):
        for kind in self.STAMP_ORDER:
            for row in self.rows(kind):
                data = row['data']
                if (kind == 'request' and data.get('kind') not in (None, 'analysis')) or _target(kind, data) is None:
                    self.skipped[kind] += 1; continue
                previous = stamp_of(kind, data)
                if previous and previous.get('to_reference_generation') == self.to: self.already[kind] += 1; continue
                if previous:
                    # Re-archive: keep the first from-generation and frozen reference, append history.
                    stamp = build_stamp(previous.get('from_reference_generation'), self.to, reference=previous.get('reference'),
                                        gap=previous.get('gap'), analysis=previous.get('analysis') or {}, previous=previous, at=self.at)
                    generation = previous.get('from_reference_generation')
                else:
                    context = self.context(kind, row); generation = context['generation']
                    if generation is None: self.unresolve(kind, row, context['reason'], False); continue
                    if generation == self.to: self.current[kind] += 1; continue
                    if context['fallback']: self.unresolve(kind, row, context['reason'], True)
                    stamp = build_stamp(generation, self.to, reference=context['reference'], gap=context['gap'],
                                        analysis=context['analysis'], at=self.at)
                payload = stamp_payload(kind, data, stamp)
                if payload == data: self.already[kind] += 1; continue
                self.stamps.append((kind, row, payload, {'id': row['id'], 'owner': self.owner, 'generation': generation, 'restamp': bool(previous)}))
                self.counts[kind] += 1
                if previous: self.restamped[kind] += 1

    def survey_drafts(self):
        # Explorations are kept untouched (docs §4.2): a dropped draft_id is harmless, since the
        # workspace falls back to the newest remaining draft on the gap, and rewriting a row would
        # move it to the top of the owner's updated_at-ordered exploration list.
        for row in self.rows('draft'):
            composer = row['data'].get('composer') or {}
            anchors = [item for item in composer.get('eaggl_anchors') or [] if isinstance(item, dict)]
            if not anchors: continue  # A gap-only draft has no generation and stays current.
            saved = self.data('draft_binding', row['id']); generations, invalid = set(), False
            for selection in (saved.get('selections') or {}).values():
                try: generations.add(generation_of_binding((selection or {}).get('binding') or {}))
                except ReferenceError: invalid = True
            sources = [(item.get('reference') or {}).get('source_id') for item in anchors]
            if generations == {self.to} and not invalid: continue
            if not generations or generations == {self.to}:
                # No usable frozen binding: only a legacy-mode cutover can assume the legacy generation.
                if self.fallback and all(model_of_source_id(source) == LEGACY_MODEL for source in sources):
                    generations = {self.fallback}; self.unresolve('draft', row, 'draft_binding_unresolved', True)
                else:
                    self.unresolve('draft', row, 'draft_binding_unresolved', False); continue
            self.drops.append({'id': row['id'], 'owner': self.owner, 'generations': sorted(generations - {self.to}),
                               'source_ids': sources, 'gap_id': (composer.get('source_gap') or {}).get('id')})

    def write(self):
        for identity, row in self.backfilled.items():
            self.tx.put('request_binding', identity, row['owner'], self.bindings[identity], expected=row['version'])
        for kind, row, payload, _ in self.stamps: self.tx.put(kind, row['id'], row['owner'], payload, expected=row['version'])
        for item in self.drops: self.tx.remove('draft', item['id']); self.tx.remove('draft_binding', item['id'])

    # -- jobs -----------------------------------------------------------------------------
    def job_generation(self, job):
        found, _ = self.request_reference(job.get('research_request_id')) if job.get('research_request_id') else (None, None)
        return found['generation'] if found else None

    def nonterminal_jobs(self):
        from .jobs import TERMINAL
        for row in self.rows('job'):
            job = row['data']
            if job.get('status') in TERMINAL: continue
            queue = self.tx.get('queue', row['id']); collected = bool(queue and queue['data'].get('dispatch_input'))
            analysis = job.get('kind') == 'analysis'
            action = ('continue' if not analysis else 'wait' if collected or job.get('status') == 'cancel_requested' else 'cancel')
            yield {'id': row['id'], 'owner': self.owner, 'kind': job.get('kind'), 'status': job.get('status'),
                   'generation': self.job_generation(job) if analysis else None, 'collected': collected, 'action': action}


OWNER_KINDS = ARCHIVE_KINDS + ('draft', 'draft_binding', 'request_binding', 'job')


def _owners(tx, kinds=OWNER_KINDS) -> list[str]:
    rows = tx.execute('SELECT DISTINCT owner_id FROM reveal_records WHERE kind IN (' + ','.join(['%s'] * len(kinds)) +
                      ') ORDER BY owner_id', tuple(kinds)).fetchall()
    return [row[0] for row in rows]


def _require_generation(*generations):
    for generation in generations:
        if not GENERATION_RE.fullmatch(generation or ''): raise ReferenceError(f'Invalid generation id: {generation!r}')


def _legacy_mode(active, to_generation) -> bool:
    """True when the prefix served only the legacy generation before cutting over to `to_generation`."""
    if active is None: return True
    return active.get('generation_id') == to_generation and active.get('previous_generation_id') is None


# --------------------------------------------------------------------------------------
# Public passes (repository)


def referenced_sources(repo) -> dict[str, set[str]]:
    """{generation_id: {factor source_id}} referenced by a prefix's drafts, requests, outcomes and stamps.

    Accounts reference factors through job -> request_binding, which is scanned directly.
    """
    found = defaultdict(set)

    def add(generation_of, item, source):
        try: generation = generation_of(item)
        except ReferenceError: return
        if source: found[generation].add(source)

    with repo.read_transaction() as tx:
        for row in tx.list('draft_binding'):
            for native, selection in (row['data'].get('selections') or {}).items():
                binding = (selection or {}).get('binding') or {}
                add(generation_of_binding, binding, binding.get('cfde_node_id') or native)
        for row in tx.list('request_binding'):
            for binding in row['data'].get('anchors') or []: add(generation_of_binding, binding, binding.get('cfde_node_id'))
        for row in tx.list('analysis_outcome'):
            for item in ((row['data'].get('record') or {}).get('provenance') or {}).get('source_bindings') or []:
                add(_source_binding_generation, item, item.get('source_id'))
        # Stamps frozen from Mechanism nodes (rows whose binding was unusable) name their own factors.
        for kind in ('account', 'analysis_outcome', 'request'):
            for row in tx.list(kind):
                stamp = stamp_of(kind, row['data'])
                if stamp and stamp.get('from_reference_generation'):
                    for anchor in (stamp.get('reference') or {}).get('anchors') or []:
                        if anchor.get('source_id'): found[stamp['from_reference_generation']].add(anchor['source_id'])
    return dict(found)


def backfill_anchor_display(repo, *, apply: bool) -> dict:
    """Fill request_binding.anchor_display from the exact saved draft selection (idempotent).

    Must run before anchored drafts are dropped; archive_prefix repeats it per owner first.
    """
    report = {'apply': apply, 'missing': 0, 'backfilled': 0, 'unrecoverable': []}
    with repo.read_transaction() as tx: owners = _owners(tx, ('request_binding',))
    for owner in owners:
        with (repo.transaction() if apply else repo.read_transaction()) as tx:
            survey = _Owner(tx, owner); report['missing'] += survey.backfill()
            report['backfilled'] += len(survey.backfilled); report['unrecoverable'] += survey.unrecoverable
            if apply:
                for identity, row in survey.backfilled.items():
                    tx.put('request_binding', identity, row['owner'], survey.bindings[identity], expected=row['version'])
    return report


def _survey(tx, owner, to_generation, fallback, at):
    survey = _Owner(tx, owner, to_generation=to_generation, fallback=fallback, at=at)
    survey.backfill(); survey.survey_stamps(); survey.survey_drafts()
    return survey


def plan_prefix(repo, active_generation_id: str, *, from_generation: str | None = None) -> dict:
    """Read-only cutover plan of one prefix toward `active_generation_id` (the generation to activate).

    Fails closed by reporting `unknown_kinds` (and ok=False) for any kind absent from KIND_ACTIONS.
    Deterministic for identical records, so its canonical sha pins an approval. Pass
    `from_generation` (the legacy generation in legacy mode) to list rows that can only be
    archived by assuming it.
    """
    _require_generation(active_generation_id)
    candidates, already, unresolved, drafts, jobs, unrecoverable = defaultdict(list), Counter(), [], [], [], []
    missing = recoverable = 0
    with repo.read_transaction() as tx:
        counts = {kind: int(total) for kind, total in tx.execute('SELECT kind,COUNT(*) FROM reveal_records GROUP BY kind').fetchall()}
        active = read_active(tx); legacy = _legacy_mode(active, active_generation_id)
        fallback = from_generation if legacy else None
        for owner in _owners(tx):
            survey = _survey(tx, owner, active_generation_id, fallback, 'plan')
            missing += survey.missing; recoverable += len(survey.backfilled)
            unrecoverable += survey.unrecoverable; already.update(survey.already); unresolved += survey.unresolved
            drafts += survey.drops; jobs += list(survey.nonterminal_jobs())
            for kind, _, _, item in survey.stamps: candidates[kind].append(item)
    unknown = sorted(kind for kind in counts if kind not in KIND_ACTIONS)
    order = lambda item: (item['owner'], item['id'])
    return {'active_generation_id': active_generation_id, 'current_generation_id': active['generation_id'] if active else None,
            'legacy_mode': legacy, 'from_generation': from_generation,
            'counts_by_kind': dict(sorted(counts.items())), 'actions': {kind: KIND_ACTIONS.get(kind) for kind in sorted(counts)},
            'unknown_kinds': unknown, 'ok': not unknown,
            'archive_candidates': {kind: sorted(candidates[kind], key=order) for kind in ARCHIVE_KINDS if candidates[kind]},
            'already_archived': dict(sorted(already.items())),
            'unresolved': sorted(unresolved, key=lambda item: (item['kind'], item['owner'], item['id'])),
            'anchored_drafts': sorted(drafts, key=order),
            'anchor_display_backfill': {'missing': missing, 'recoverable': recoverable, 'unrecoverable': sorted(unrecoverable, key=order)},
            'nonterminal_jobs': sorted(jobs, key=order)}


def archive_prefix(repo, from_generation: str, to_generation: str, *, apply: bool, at: str | None = None,
                   from_is_legacy: bool | None = None) -> dict:
    """Archive one prefix's work built on generations other than `to_generation` (docs §4.2, §6 P4).

    Per owner, in one repository transaction: backfill anchor_display, stamp archived kinds
    (build_stamp; history is appended when a row already carries an older stamp), drop drafts
    anchored to a superseded generation (explorations stay untouched). Rows already stamped
    to `to_generation` are left untouched, so re-runs are no-ops. Rows whose generation cannot
    be derived are reported in `unresolved`, and still archived when `from_generation` is the
    legacy generation (auto-detected from the prefix's reference_active record unless
    `from_is_legacy` is given). Applying requires `to_generation` to be the active generation.
    Writes the reference_archive_run ledger (id digest([prefix, from, to])).
    """
    _require_generation(from_generation, to_generation)
    if from_generation == to_generation: raise ReferenceError('Archive toward a different generation')
    at = at or now(); started = now(); prefix = repo.table_prefix; run_id = digest([prefix, from_generation, to_generation])
    with repo.read_transaction() as tx:
        active = read_active(tx); owners = _owners(tx)
    if apply and (not active or active.get('generation_id') != to_generation):
        raise ReferenceError('Activate the new reference generation before archiving toward it')
    legacy = _legacy_mode(active, to_generation) if from_is_legacy is None else from_is_legacy
    fallback = from_generation if legacy else None
    report = {'run_id': run_id, 'prefix': prefix, 'from_generation': from_generation, 'to_generation': to_generation,
              'apply': apply, 'archived_at': at, 'legacy_fallback': legacy, 'owners': len(owners), 'counts': Counter(),
              'restamped': Counter(), 'already_archived': Counter(), 'current': Counter(), 'skipped': Counter(),
              'dropped_drafts': [], 'anchor_display_backfilled': 0,
              'anchor_display_unrecoverable': [], 'unresolved': [], 'started_at': started, 'completed_at': None}
    if apply:
        with repo.transaction() as tx:
            if not tx.get(ARCHIVE_RUN_KIND, run_id):
                tx.put(ARCHIVE_RUN_KIND, run_id, CATALOG_OWNER, {'prefix': prefix, 'from_generation': from_generation,
                    'to_generation': to_generation, 'started_at': started, 'completed_at': None, 'counts': {},
                    'dropped_drafts': [], 'unresolved': [], 'runs': 0})
    for owner in owners:
        with (repo.transaction() if apply else repo.read_transaction()) as tx:
            survey = _survey(tx, owner, to_generation, fallback, at)
            if apply: survey.write()
        for key, counter in (('counts', survey.counts), ('restamped', survey.restamped), ('already_archived', survey.already),
                             ('current', survey.current), ('skipped', survey.skipped)): report[key].update(counter)
        report['dropped_drafts'] += survey.drops
        report['anchor_display_backfilled'] += len(survey.backfilled)
        report['anchor_display_unrecoverable'] += survey.unrecoverable; report['unresolved'] += survey.unresolved
    report['completed_at'] = now()
    for key in ('counts', 'restamped', 'already_archived', 'current', 'skipped'): report[key] = dict(sorted(report[key].items()))
    if apply:
        with repo.transaction() as tx:
            row = tx.get(ARCHIVE_RUN_KIND, run_id); ledger = row['data']
            counts = Counter(ledger.get('counts') or {}); counts.update(report['counts'])
            drops = {item['id']: item for item in ledger.get('dropped_drafts') or []}
            drops.update({item['id']: item for item in report['dropped_drafts']})
            pending = {(item['kind'], item['id']): item for item in ledger.get('unresolved') or []}
            pending.update({(item['kind'], item['id']): item for item in report['unresolved']})
            ledger.update(completed_at=report['completed_at'], counts=dict(sorted(counts.items())), runs=ledger.get('runs', 0) + 1,
                          dropped_drafts=sorted(drops.values(), key=lambda item: (item['owner'], item['id'])),
                          unresolved=sorted(pending.values(), key=lambda item: (item['kind'], item['owner'], item['id'])))
            tx.put(ARCHIVE_RUN_KIND, run_id, CATALOG_OWNER, ledger, expected=row['version'])
    return report


def cancel_nonterminal_jobs(repo, generation_id: str, *, apply: bool) -> list[str]:
    """Cancel (jobs.cancel) non-terminal analysis jobs of `generation_id` that were never collected.

    `generation_id` is the superseded generation. A job without a readable request binding
    cannot be collected either and is cancelled too. Collected jobs (queue.dispatch_input) may
    finish and are stamped at creation; paragraph jobs need no reference data and continue.
    """
    from . import jobs
    _require_generation(generation_id)
    cancelled = []
    with repo.read_transaction() as tx: owners = _owners(tx, ('job',))
    for owner in owners:
        with (repo.transaction() if apply else repo.read_transaction()) as tx:
            survey = _Owner(tx, owner)
            for item in list(survey.nonterminal_jobs()):
                if item['action'] != 'cancel' or item['generation'] not in (generation_id, None): continue
                if apply:
                    row = survey.index('job')[item['id']]
                    jobs.cancel(tx, dict(row['data'], owner_user_id=row['owner']))
                cancelled.append(item['id'])
    return cancelled


# --------------------------------------------------------------------------------------
# Frozen factor snapshots (shared scientific tables, explicit DB-API connection)

SNAPSHOT_FORMAT = 'reveal.archived-reference-factor/1'
TOP_GENES = TOP_GENE_SETS = 50
ARCHIVED_COLUMNS = ('archive_id', 'generation_id', 'source_id', 'source_id_sha256', 'model', 'factor_id', 'trait',
                    'kpn_trait_id', 'label', 'snapshot', 'snapshot_sha256')
TOP_GENES_SQL = ('SELECT g.symbol,l.loading FROM eaggl_factors e JOIN eaggl_gene_loadings l ON l.import_id=e.import_id AND '
                 'l.factor_index=e.factor_index JOIN eaggl_genes g ON g.import_id=l.import_id AND g.gene_index=l.gene_index '
                 'WHERE e.import_id=%s AND {} ORDER BY l.loading DESC,l.gene_index LIMIT ' + str(TOP_GENES))


def _json(value):
    if isinstance(value, (bytes, bytearray)): value = value.decode('utf-8')
    return json.loads(value) if isinstance(value, str) else value


def _batches(values, size=500):
    values = list(values)
    for start in range(0, len(values), size): yield values[start:start + size]


def _in(values): return '(' + ','.join(['%s'] * len(values)) + ')'


def _legacy_gene_set(rank, key, dapper_id) -> dict:
    """A legacy CFDE gene-set link; name/library are import_cfde_genesets.metadata_record's display forms."""
    segments = key.split('___')
    name = ' | '.join(' / '.join(part.replace('_', ' ') for part in segment.split('__')) for segment in segments)
    prefixes = {segment.split('__', 1)[0] for segment in segments if '__' in segment}
    library = next(iter(prefixes)) if len(prefixes) == 1 and all('__' in segment for segment in segments) else None
    return {'rank': rank, 'gene_set_id': dapper_id, 'name': name, 'library': library, 'collection_id': None, 'source_key': key,
            'joint_loading': None, 'marginal_loading': None, 'score': None}


def _top_genes(cursor, import_id, where, params) -> list[dict]:
    cursor.execute(TOP_GENES_SQL.format(where), (import_id, *params))
    return [{'symbol': symbol, 'loading': float(loading)} for symbol, loading in cursor.fetchall()]


def _legacy_factors(connection, generation, wanted) -> dict:
    run = generation.get('legacy_mapping_run_id')
    if not GENERATION_RE.fullmatch(run or ''): raise ReferenceError('Legacy generation has no mapping run')
    # eaggl_cfde_factor_links.cfde_node_sha256 is digest(node id); also accept the plain UTF-8 hash.
    hashes = sorted({value for source in wanted for value in (digest(source), source_id_sha256(source))})
    found, wanted = {}, set(wanted)
    with connection.cursor() as cursor:
        for batch in _batches(hashes):
            cursor.execute('SELECT l.cfde_node_id,l.factor_index,l.eaggl_import_id,l.payload,f.factor_id,f.trait,f.label,f.metadata '
                           'FROM eaggl_cfde_factor_links l JOIN eaggl_factors f ON f.import_id=l.eaggl_import_id AND '
                           'f.factor_index=l.factor_index WHERE l.run_id=%s AND l.cfde_node_sha256 IN ' + _in(batch), (run, *batch))
            for native, index, import_id, payload, factor_id, trait, label, metadata in cursor.fetchall():
                if native in wanted: found[native] = (index, import_id, _json(payload), factor_id, trait, label, _json(metadata))
        factors = {}
        for native, (index, import_id, payload, factor_id, trait, label, metadata) in sorted(found.items()):
            parts = native.split(':')
            raw = (payload or {}).get('raw') or {}
            cursor.execute('SELECT l.gene_set_rank,l.source_key,l.node_id,a.dapper_id FROM eaggl_cfde_gene_set_links l '
                           'LEFT JOIN cfde_gene_set_aliases a ON a.import_id=l.gene_set_import_id AND a.node_id_sha256=l.resolved_alias_sha256 '
                           'WHERE l.run_id=%s AND l.factor_index=%s ORDER BY l.gene_set_rank', (run, index))
            gene_sets = [_legacy_gene_set(rank, key, dapper) for rank, key, _, dapper in cursor.fetchall()]
            factors[native] = {'model': LEGACY_MODEL, 'factor_id': factor_id, 'trait': trait, 'kpn_trait_id': None, 'label': label,
                'node': {'name': f'{parts[2]} mechanism {parts[4]}', 'description': f'EAGGL mechanism {native}. Source label: {raw.get("label", label)}.'},
                'metadata': metadata, 'top_genes': _top_genes(cursor, import_id, 'e.factor_index=%s', (index,)), 'top_gene_sets': gene_sets}
    return factors


def _kpn_factors(connection, generation, wanted) -> dict:
    identity, factors = generation['generation_id'], {}
    with connection.cursor() as cursor:
        rows = []
        for batch in _batches(sorted(wanted)):
            cursor.execute('SELECT f.public_id,f.factor_key,f.eaggl_factor_id,f.kpn_trait_id,f.label,f.eaggl_import_id,f.metadata,'
                           't.phenotype_name,t.legacy_phenotype_id FROM reference_factors f JOIN kpn_traits t ON '
                           't.generation_id=f.generation_id AND t.kpn_trait_id=f.kpn_trait_id WHERE f.generation_id=%s AND f.public_id IN ' +
                           _in(batch), (identity, *batch))
            rows += cursor.fetchall()
        for public, key, eaggl_factor_id, kpn_trait_id, label, import_id, metadata, phenotype, legacy_phenotype in sorted(rows):
            if public not in wanted: continue
            import_id = import_id or generation.get('eaggl_import_id')
            genes = (_top_genes(cursor, import_id, 'e.factor_id_sha256 IN (%s,%s) AND e.factor_id=%s',
                                (source_id_sha256(eaggl_factor_id), digest(eaggl_factor_id), eaggl_factor_id)) if import_id else [])
            cursor.execute('SELECT p.joint_rank,p.gene_set_id,s.gene_set_name,s.library,s.collection_id,s.legacy_source_key,'
                           'p.joint_loading,p.marginal_loading FROM factor_gene_set_projections p JOIN cfde_gene_sets s ON '
                           's.generation_id=p.generation_id AND s.gene_set_id=p.gene_set_id WHERE p.generation_id=%s AND p.scope=%s '
                           'AND p.factor_key=%s AND p.joint_rank<=%s ORDER BY p.joint_rank,p.gene_set_id',
                           (identity, PROJECTION_SCOPE, key, TOP_GENE_SETS))
            gene_sets = [{'rank': rank, 'gene_set_id': gene_set, 'name': name, 'library': library, 'collection_id': collection,
                          'source_key': source_key, 'joint_loading': float(joint), 'marginal_loading': float(marginal), 'score': None}
                         for rank, gene_set, name, library, collection, source_key, joint, marginal in cursor.fetchall()]
            factor = parse_factor_key(key)['factor']
            factors[public] = {'model': generation.get('model') or KPN_MODEL, 'factor_id': eaggl_factor_id,
                'trait': legacy_phenotype or eaggl_factor_id.split('::')[0], 'kpn_trait_id': kpn_trait_id, 'label': label,
                'node': mechanism_node(public, phenotype, kpn_trait_id, factor, label), 'metadata': _json(metadata),
                'top_genes': genes, 'top_gene_sets': gene_sets}
    return factors


def _default_runtime():
    """The pinned DAPPER runtime the catalog mints Mechanism ids with, or None when it is not installed."""
    try:
        from .acceptance import public_runtime
        return public_runtime()
    except (OSError, ValueError, ImportError):
        return None


def _archived_row(snapshot: dict) -> dict:
    return {'archive_id': archive_id(snapshot['generation_id'], snapshot['source_id']), 'generation_id': snapshot['generation_id'],
            'source_id': snapshot['source_id'], 'source_id_sha256': source_id_sha256(snapshot['source_id']), 'model': snapshot['model'],
            'factor_id': snapshot['factor_id'], 'trait': snapshot['trait'], 'kpn_trait_id': snapshot['kpn_trait_id'],
            'label': snapshot['label'], 'snapshot': snapshot, 'snapshot_sha256': digest(snapshot)}


def capture_factors(connection, generation: dict, source_ids, *, runtime=None, strict: bool = False) -> list[dict]:
    """archived_reference_factors rows (snapshot format docs §3) for `source_ids` of `generation`.

    `generation` is a reference_generations row (reference_generation.get_generation). Legacy
    generations read eaggl_cfde_factor_links + eaggl_factors, top-50 eaggl_gene_loadings x
    eaggl_genes and the ranked eaggl_cfde_gene_set_links with their cfde_gene_set_aliases DAPPER
    ids; KPN generations read reference_factors + kpn_traits, the top-50 joint
    factor_gene_set_projections x cfde_gene_sets and the EAGGL loadings of eaggl_factor_id.
    The Mechanism id is minted as the catalog does (default: the pinned DAPPER runtime; null
    only when no runtime is installed). Factors absent from the generation are skipped (the
    caller reports them; `verify` fails closed on stamped anchors without a snapshot), or
    raise ReferenceError with strict=True.
    """
    wanted = sorted({source for source in source_ids if source})
    if not wanted: return []
    identity, kind = generation.get('generation_id'), generation.get('kind')
    _require_generation(identity)
    if kind == LEGACY_KIND: factors = _legacy_factors(connection, generation, wanted)
    elif kind == KPN_KIND: factors = _kpn_factors(connection, generation, wanted)
    else: raise ReferenceError(f'Unknown reference generation kind: {kind!r}')
    missing = [source for source in wanted if source not in factors]
    if missing and strict:
        raise ReferenceError(f'{len(missing)} referenced factor(s) absent from generation {identity}: ' + ', '.join(missing[:10]))
    if not factors: return []
    runtime = runtime or _default_runtime(); manifest_sha256 = digest(generation.get('manifest') or {})
    mint = (lambda node: runtime.compute_id(dict(node), 'Mechanism', runtime.schema)) if runtime else (lambda node: None)
    rows = []
    for source, item in sorted(factors.items()):
        node = item['node']
        snapshot = {'format': SNAPSHOT_FORMAT, 'generation_id': identity, 'model': item['model'], 'source_id': source,
                    'factor_id': item['factor_id'], 'trait': item['trait'], 'kpn_trait_id': item['kpn_trait_id'], 'label': item['label'],
                    'mechanism': {'id': mint(node), **node},
                    'metadata': item['metadata'], 'top_genes': item['top_genes'], 'top_gene_sets': item['top_gene_sets'],
                    'generation_manifest_sha256': manifest_sha256}
        rows.append(_archived_row(snapshot))
    return rows


# This authored local fixture predates reference-generation membership. Its original
# CFDE response captures are real, but CADinT2D::Factor1 was not in the imported legacy
# factors. Pin the reviewed fixture and source bytes: a Mechanism description, arbitrary
# fixture_origin, or edited receipt must never manufacture an external factor archive.
EXTERNAL_FIXTURE_CONTENT = '83e9b71b365954a3d212c40896232958342693e775e9f1cb6349f990d14043b3'
EXTERNAL_FIXTURE_DOCUMENT = 'ede867fbd62d442986c26ceea7bf157ef56a864397ed0aec23072fab3f0a98ab'
EXTERNAL_FIXTURE_ACCOUNT = 'dapper:ScientificAccount.05Vs-l6pVZHt9ttebmJqK2VNodZUouTb'
EXTERNAL_FIXTURE_SOURCE = 'factor:portal:CADinT2D:cfde-inc-v2:Factor1'
EXTERNAL_FIXTURE_MECHANISM = 'dapper:Mechanism.kEJMDzCkDYE94DpWaXrQg-eC4gFGQyuX'
EXTERNAL_FIXTURE_FILES = (
    ('pigean-gene-factor', 'dapper:File.PvSoiIqLFFGMD3LdzGXvwugdzrtD3KxZ',
     '3fdf0f2ae3f782824ec7f96687c4a988a88a6ffb0a106b52f4e20464cdf51e30', 2945),
    ('pigean-gene-set-factor', 'dapper:File.Dt0S38v_sLn5jiOAVXMrMWTrXBLnsn83',
     'b4ba6a21f6791c8b80ce8ef471361634ebb8b9f1f4e91e886fe1f05d02e7c1fa', 3785),
)


def capture_fixture_factors(repository, generation: dict, source_ids, *, read_artifact) -> list[dict]:
    """Supplement *missing* imported factors from the pinned local fixture's retained bytes.

    This is deliberately not a general description-based factor fallback. The receipt,
    owner, archived account, complete scientific document (apart from storage locations),
    original Mechanism, and two versioned artifact references must all agree. The reader
    receives only those S3 references, outside the read transaction; returned bytes are
    independently size/checksum checked. Nothing is written here.

    The resulting generation is the retiring *account context*, not a claim that the
    external source belonged to the imported generation. Metadata preserves the original
    observations and their limitations. Empty top_* arrays avoid presenting the partial
    eight-row responses as a complete top-50 or manufacturing ranked gene-set mappings.
    """
    if (repository.table_prefix != 'reveal_workflow_local' or generation.get('kind') != LEGACY_KIND
            or generation.get('model') != LEGACY_MODEL or EXTERNAL_FIXTURE_SOURCE not in source_ids):
        return []
    identity = generation.get('generation_id'); _require_generation(identity)
    from .evidence_package import canonical_json, sha256

    def require(condition, message):
        if not condition: raise ReferenceError('Retained canonical factor source: ' + message)

    origin = {'kind': 'canonical_fixture', 'fixture_version': 'bubble-account-v1',
              'content_sha256': EXTERNAL_FIXTURE_CONTENT, 'scientific_acceptance': 'not_reviewed'}
    retained = []
    with repository.read_transaction() as tx:
        for row in tx.list('fixture_seed'):
            receipt, owner = row['data'], row['owner']
            if (receipt.get('content_sha256') != EXTERNAL_FIXTURE_CONTENT
                    or receipt.get('account_id') != EXTERNAL_FIXTURE_ACCOUNT):
                continue
            expected_receipt = digest([owner, 'canonical-fixture', 'bubble-account-v1', EXTERNAL_FIXTURE_CONTENT])
            require(row['id'] == receipt.get('receipt_id') == expected_receipt
                    and receipt.get('format') == 'reveal.fixture-seed-receipt/1'
                    and receipt.get('fixture_version') == 'bubble-account-v1'
                    and receipt.get('owner_user_id') == owner
                    and receipt.get('table_prefix') == repository.table_prefix
                    and receipt.get('scientific_acceptance') == 'not_reviewed'
                    and receipt.get('jobs_dispatched') == 0, 'fixture receipt identity differs')
            account = tx.get('account', digest([owner, EXTERNAL_FIXTURE_ACCOUNT]))
            require(account is not None and account['owner'] == owner
                    and account['data'].get('fixture_origin') == origin, 'owning fixture account is missing or changed')
            stamp = stamp_of('account', account['data'])
            if not stamp or stamp.get('from_reference_generation') != identity: continue
            require(any(a.get('source_id') == EXTERNAL_FIXTURE_SOURCE
                        and a.get('mechanism_id') == EXTERNAL_FIXTURE_MECHANISM
                        and a.get('archived_reference_factor_id') == archive_id(identity, EXTERNAL_FIXTURE_SOURCE)
                        for a in (stamp.get('reference') or {}).get('anchors') or []), 'archived account anchor differs')
            document_sha = receipt.get('scientific_document_sha256')
            stored = tx.get('scientific_document', digest([owner, document_sha]))
            require(stored is not None and stored['owner'] == owner and stored['data'].get('fixture_origin') == origin,
                    'retained scientific document is missing or belongs to another owner')
            document = stored['data'].get('document') or {}
            require(stored['data'].get('sha256') == document_sha == sha256(canonical_json(document)),
                    'retained scientific document checksum differs')
            scientific = deepcopy(document)
            for group in ('files', 'datasets'):
                for item in scientific.get(group) or []: item.pop('location', None)
            require(sha256(canonical_json(scientific)) == EXTERNAL_FIXTURE_DOCUMENT,
                    'scientific content differs from the pinned original fixture')
            mechanism, = document['mechanisms']
            require(mechanism['id'] == EXTERNAL_FIXTURE_MECHANISM
                    and (_parse_mechanism(mechanism) or {}).get('source_id') == EXTERNAL_FIXTURE_SOURCE,
                    'original Mechanism identity differs')
            files = {file['id']: file for file in document['files']}
            artifacts = []
            for index, file_id, checksum, size in EXTERNAL_FIXTURE_FILES:
                artifact = tx.get('artifact', digest([owner, checksum]))
                require(artifact is not None and artifact['owner'] == owner, 'retained source artifact is missing or belongs to another owner')
                data = artifact['data']; storage = data.get('storage') or {}; file = files[file_id]
                require(data.get('sha256') == file.get('sha256') == storage.get('sha256') == checksum
                        and file.get('size_in_bytes') == storage.get('size_bytes') == size
                        and data.get('file') == file and storage.get('store') == 's3'
                        and bool(storage.get('bucket')) and bool(storage.get('key'))
                        and isinstance(storage.get('version_id'), str) and storage['version_id'] not in ('', 'null')
                        and file.get('location') == f's3://{storage["bucket"]}/{storage["key"]}',
                        'retained source file or immutable version binding differs')
                require(any(item.get('file') == file and item.get('storage') == storage
                            for item in receipt.get('artifacts') or []), 'source artifact is absent from the original seed receipt')
                artifacts.append((index, file_id, checksum, size, deepcopy(storage)))
            retained.append((deepcopy(mechanism), artifacts))
    if not retained: return []

    snapshots = []
    for mechanism, artifacts in retained:
        captures, labels, other_label_rows = [], set(), 0
        for index, file_id, checksum, size, storage in artifacts:
            raw = read_artifact(storage)
            require(isinstance(raw, bytes) and len(raw) == size and sha256(raw) == checksum, 'source bytes fail checksum verification')
            response = json.loads(raw)
            require(response.get('index') == index and response.get('q') == ['CADinT2D', LEGACY_MODEL, 'Factor1']
                    and response.get('count') == len(response.get('data') or []) == 8, 'source response query or row count differs')
            for item in response['data']:
                require(item.get('phenotype') == 'CADinT2D' and item.get('trait_group') == 'portal'
                        and item.get('gene_set_size') == LEGACY_MODEL and item.get('factor') == 'Factor1',
                        'source observation factor identity differs')
                labels.add(item['label'])
                if item.get('label_factor') != 'Factor1': other_label_rows += 1
            captures.append({'file_id': file_id, 'sha256': checksum, 'size_bytes': size,
                             'source_path': f'data/fixtures/bubble-account-v1/sources/{index}.response.json',
                             'index': index, 'query': response['q'], 'page': response['page'], 'limit': response['limit'],
                             'progress': response['progress'], 'partial': True, 'observations': response['data']})
        snapshots.append({'format': SNAPSHOT_FORMAT, 'generation_id': identity, 'model': LEGACY_MODEL,
            'source_id': EXTERNAL_FIXTURE_SOURCE, 'factor_id': 'CADinT2D::Factor1', 'trait': 'CADinT2D', 'kpn_trait_id': None,
            'label': 'Factor1', 'mechanism': mechanism,
            'metadata': {'source_provenance': {'kind': 'retained_external_cfde_capture', 'fixture_origin': origin,
                'generation_membership': 'not_asserted',
                'generation_scope': 'The generation identifies the archived account context, not membership of this external factor in that import.',
                'completeness': 'partial',
                'label_basis': 'Original fixture interim name; source label disagreements are not resolved.',
                'source_labels': sorted(labels), 'rows_with_other_label_factor': other_label_rows,
                'limitations': 'Two retained eight-row CFDE responses only. Full factor loadings, top-50 rankings and imported gene-set mappings are unavailable. Source labels differ, including a Factor2 label_factor on a Factor1 row; the original observations are preserved.',
                'captures': captures}},
            'top_genes': [], 'top_gene_sets': [], 'generation_manifest_sha256': digest(generation.get('manifest') or {})})
    require(len({digest(snapshot) for snapshot in snapshots}) == 1, 'fixture owners have conflicting source captures')
    return [_archived_row(snapshots[0])]


def write_archived_factors(connection, rows) -> int:
    """Insert frozen factor snapshots if absent; returns the number inserted.

    Idempotent for identical snapshots. A stored snapshot with a different snapshot_sha256
    is a conflict (ReferenceError) and nothing is written: archives are never overwritten.
    """
    unique = {}
    for row in rows:
        snapshot = row['snapshot']
        if (row['archive_id'] != archive_id(row['generation_id'], row['source_id']) or row['snapshot_sha256'] != digest(snapshot)
                or row['source_id_sha256'] != source_id_sha256(row['source_id'])):
            raise ReferenceError(f'Archived factor row {row["archive_id"]} does not match its snapshot')
        previous = unique.setdefault(row['archive_id'], row)
        if previous['snapshot_sha256'] != row['snapshot_sha256']: raise ReferenceError(f'Conflicting snapshots for {row["archive_id"]}')
    if not unique: return 0
    try:
        with connection.cursor() as cursor:
            stored = {}
            for batch in _batches(sorted(unique)):
                cursor.execute('SELECT archive_id,snapshot_sha256 FROM archived_reference_factors WHERE archive_id IN ' + _in(batch), tuple(batch))
                stored.update({identity: checksum for identity, checksum in cursor.fetchall()})
            conflicts = sorted(identity for identity, checksum in stored.items() if unique[identity]['snapshot_sha256'] != checksum)
            if conflicts: raise ReferenceError('Archived factor snapshot differs from the stored one: ' + ', '.join(conflicts[:10]))
            new = [unique[identity] for identity in sorted(unique) if identity not in stored]
            if new:
                cursor.executemany('INSERT INTO archived_reference_factors (' + ','.join(ARCHIVED_COLUMNS) + ') VALUES ' + _in(ARCHIVED_COLUMNS),
                                   [tuple(canonical(row[column]) if column == 'snapshot' else row[column] for column in ARCHIVED_COLUMNS)
                                    for row in new])
        connection.commit()
    except BaseException:
        try: connection.rollback()
        except Exception: pass
        raise
    return len(new)
