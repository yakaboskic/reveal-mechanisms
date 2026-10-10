"""Shared contract for versioned reference generations (docs/reference-reload.md).

A reference generation is one complete set of served EAGGL factors and CFDE gene sets.
The current data is the *legacy* generation (CFDE-linked factors, model cfde-inc-v2,
identified by its EAGGL->CFDE mapping run). Reloads add *KPN* generations (all EAGGL
factors keyed by KPN.TRAIT ids, model eaggl-capped-v1). Each environment prefix points
at one generation through its `reference_active` record; user work built on an older
generation is archived with the stamp built here, never deleted.

This module is the single source of truth for identifier formats, record kinds and the
archive stamp. It performs no network I/O; SQL helpers take an explicit connection.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

from .repository import canonical, digest, now
from .runtime_config import ROOT

LEGACY_MODEL = 'cfde-inc-v2'
KPN_MODEL = 'eaggl-capped-v1'
MODELS = (LEGACY_MODEL, KPN_MODEL)
LEGACY_KIND = 'legacy-cfde-inc-v2'
KPN_KIND = 'kpn-eaggl-capped'
PROJECTION_SCOPE = 'per_trait'

# Per-prefix <prefix>_records kinds, all owned by the catalog.
ACTIVE_KIND, ACTIVE_ID = 'reference_active', 'active'
CONTROL_KIND, CONTROL_ID = 'reference_control', 'reload'
ARCHIVE_RUN_KIND = 'reference_archive_run'
RELOAD_KIND = 'reference_reload'
CATALOG_OWNER = 'catalog'

ARCHIVE_STATUS = 'archived'
ARCHIVE_REASON = 'reference_generation_superseded'

KPN_TRAIT_RE = re.compile(r'KPN\.TRAIT:(\d{7})')
FACTOR_RE = re.compile(r'Factor[1-9][0-9]*')
PUBLIC_ID_RE = re.compile(r'factor:kpn:(\d{7}):' + re.escape(KPN_MODEL) + r':(Factor[1-9][0-9]*)')
FACTOR_KEY_RE = re.compile(r'(KPN\.TRAIT:\d{7})::(Factor[1-9][0-9]*)')
GENERATION_RE = re.compile(r'[a-f0-9]{64}')


class ReferenceError(ValueError):
    """A reference-generation invariant does not hold."""


# --------------------------------------------------------------------------------------
# Identifiers


def legacy_generation_id(mapping_run_id: str) -> str:
    """The generation id of the pre-reload data, fixed by its EAGGL->CFDE mapping run."""
    if not GENERATION_RE.fullmatch(mapping_run_id or ''):
        raise ReferenceError('Invalid legacy mapping run id')
    return digest(['reference-generation', LEGACY_KIND, mapping_run_id])


def kpn_number(kpn_trait_id: str) -> str:
    match = KPN_TRAIT_RE.fullmatch(kpn_trait_id or '')
    if not match: raise ReferenceError(f'Invalid KPN trait id: {kpn_trait_id!r}')
    return match.group(1)


def factor_key(kpn_trait_id: str, factor: str) -> str:
    """Canonical key and Upstash vector id: KPN.TRAIT:0000398::Factor1."""
    kpn_number(kpn_trait_id)
    if not FACTOR_RE.fullmatch(factor or ''): raise ReferenceError(f'Invalid factor: {factor!r}')
    return f'{kpn_trait_id}::{factor}'


def public_id(kpn_trait_id: str, factor: str) -> str:
    """API source_id with five colon-free segments: factor:kpn:0000398:eaggl-capped-v1:Factor1."""
    factor_key(kpn_trait_id, factor)
    return f'factor:kpn:{kpn_number(kpn_trait_id)}:{KPN_MODEL}:{factor}'


def parse_public_id(identity: str) -> dict:
    match = PUBLIC_ID_RE.fullmatch(identity or '')
    if not match: raise ReferenceError(f'Not a KPN factor id: {identity!r}')
    kpn_trait_id = 'KPN.TRAIT:' + match.group(1)
    return {'kpn_trait_id': kpn_trait_id, 'factor': match.group(2), 'model': KPN_MODEL,
            'factor_key': factor_key(kpn_trait_id, match.group(2))}


def parse_factor_key(key: str) -> dict:
    match = FACTOR_KEY_RE.fullmatch(key or '')
    if not match: raise ReferenceError(f'Not a factor key: {key!r}')
    return {'kpn_trait_id': match.group(1), 'factor': match.group(2)}


def model_of_source_id(identity: str) -> str | None:
    """Model of an EAGGL factor source_id, or None for non-factor ids."""
    parts = (identity or '').split(':')
    if len(parts) == 5 and parts[0] == 'factor' and parts[3] in MODELS: return parts[3]
    return None


def mechanism_node(public: str, phenotype_name: str, kpn_trait_id: str, factor: str, label: str,
                   *, identity_version=1, eaggl_import_id=None) -> dict:
    """The DAPPER Mechanism node of a KPN factor. Callers compute its content-addressed id.

    The catalog and the evidence collector must mint the same node, so both use this.
    Version 1 is retained for historical generations. New import manifests opt
    into version 2, which binds the fitted object to its immutable EAGGL import;
    a routing public id by itself does not identify a fit across imports.
    """
    if identity_version not in (1, 2): raise ReferenceError('Unsupported Mechanism identity version')
    node = {'name': f'{phenotype_name} mechanism {factor}',
            'description': f'EAGGL mechanism {public}. KPN trait {kpn_trait_id} ({phenotype_name}). Source label: {label}.'}
    if identity_version == 2:
        if not isinstance(eaggl_import_id, str) or not GENERATION_RE.fullmatch(eaggl_import_id):
            raise ReferenceError('Version 2 Mechanism identity requires a pinned EAGGL import')
        node['description'] += f' Fitted EAGGL import: {eaggl_import_id}.'
    return node


def archive_id(generation_id: str, source_id: str) -> str:
    """Primary key of archived_reference_factors and the /v1/reference-factors/{id} path id."""
    return digest([generation_id, source_id])


def generation_of_binding(binding: dict) -> str:
    """Reference generation of a frozen catalog binding (draft_binding / request_binding anchor).

    KPN bindings carry reference_generation_id; legacy bindings only mapping_run_id.
    """
    if binding.get('reference_generation_id'): return binding['reference_generation_id']
    if binding.get('mapping_run_id'): return legacy_generation_id(binding['mapping_run_id'])
    raise ReferenceError('Binding has no reference generation')


def generation_of_anchors(anchors) -> str | None:
    """The single generation of a list of anchor bindings, or None when there are none."""
    generations = {generation_of_binding(anchor) for anchor in anchors or []}
    if len(generations) > 1: raise ReferenceError('Anchors span several reference generations')
    return next(iter(generations), None)


def shared_release(anchors) -> str | None:
    """The one release id (stored as reference_generation_id) that every anchor binding was saved under, or None.

    Under the reference release a draft may keep anchors saved under different releases, so unlike
    generation_of_anchors this never raises; a mixed or empty draft pins no release."""
    releases = {(anchor or {}).get('reference_generation_id') for anchor in anchors or []}
    return next(iter(releases)) if len(releases) == 1 else None


# --------------------------------------------------------------------------------------
# Archive stamp


def build_stamp(from_generation: str, to_generation: str, *, reference: dict, gap: dict | None,
                analysis: dict, previous: dict | None = None, at: str | None = None) -> dict:
    """The `archive` block written onto archived rows (shape in docs/reference-reload.md).

    Re-archiving on a later reload keeps the first from-generation, archive time and frozen
    reference/gap/analysis, advances to_reference_generation and appends to history.
    Returns the previous stamp unchanged when it already targets to_generation (idempotent).
    """
    at = at or now()
    if previous:
        if previous.get('to_reference_generation') == to_generation: return previous
        stamp = deepcopy(previous)
        stamp['history'] = list(stamp.get('history', [])) + [
            {'from_reference_generation': previous['to_reference_generation'], 'to_reference_generation': to_generation, 'archived_at': at}]
        stamp['to_reference_generation'] = to_generation
        return stamp
    return {'status': ARCHIVE_STATUS, 'reason': ARCHIVE_REASON, 'archived_at': at,
            'from_reference_generation': from_generation, 'to_reference_generation': to_generation,
            'history': [{'from_reference_generation': from_generation, 'to_reference_generation': to_generation, 'archived_at': at}],
            'reference': reference, 'gap': gap,
            'analysis': {key: analysis.get(key) for key in ('job_id', 'request_id', 'evidence_package_sha256', 'account_id', 'outcome_id')}}


def public_stamp(stamp: dict | None) -> dict | None:
    """The stamp as written onto public copies: no private job/request identifiers."""
    if not stamp: return stamp
    stamp = deepcopy(stamp)
    stamp['analysis'] = dict(stamp.get('analysis') or {}, job_id=None, request_id=None)
    return stamp


def is_archived(block: dict | None) -> bool:
    return bool(block and isinstance(block.get('archive'), dict) and block['archive'].get('status') == ARCHIVE_STATUS)


# --------------------------------------------------------------------------------------
# Per-prefix records: active pointer and reload gate (repository transactions)


def read_active(tx) -> dict | None:
    row = tx.get(ACTIVE_KIND, ACTIVE_ID)
    return row['data'] if row else None


def write_active(tx, generation_id: str, model: str, *, expected_previous: str | None,
                 vector_snapshot_id: str | None = None) -> dict:
    """Compare-and-swap the prefix's active generation."""
    if not GENERATION_RE.fullmatch(generation_id): raise ReferenceError('Invalid generation id')
    if model not in MODELS: raise ReferenceError('Unknown reference model')
    old = read_active(tx)
    previous = old['generation_id'] if old else None
    if previous == generation_id: return old
    if previous != expected_previous: raise ReferenceError('Active reference generation changed; inspect before activation')
    data = {'generation_id': generation_id, 'model': model, 'previous_generation_id': previous,
            'vector_snapshot_id': vector_snapshot_id, 'activated_at': now()}
    tx.put(ACTIVE_KIND, ACTIVE_ID, CATALOG_OWNER, data)
    return data


def read_gate(tx) -> dict | None:
    row = tx.get(CONTROL_KIND, CONTROL_ID)
    return row['data'] if row and row['data'].get('closed') else None


def set_gate(tx, closed: bool, **details) -> dict:
    data = {'closed': bool(closed), 'changed_at': now(), **details}
    tx.put(CONTROL_KIND, CONTROL_ID, CATALOG_OWNER, data)
    return data


# --------------------------------------------------------------------------------------
# Shared scientific tables (explicit DB-API connection; MySQL in deployments)

MIGRATION = ROOT / 'schema/migrations/008_reference_generation.sql'


def migration_statements() -> list[str]:
    sql = '\n'.join(line for line in MIGRATION.read_text().splitlines() if not line.lstrip().startswith('--'))
    return [statement.strip() for statement in sql.split(';') if statement.strip()]


def migrate(connection) -> None:
    """Apply migration 008 (idempotent CREATE TABLE IF NOT EXISTS)."""
    with connection.cursor() as cursor:
        for statement in migration_statements(): cursor.execute(statement)
    connection.commit()


GENERATION_COLUMNS = ('generation_id', 'kind', 'model', 'status', 'eaggl_import_id', 'eaggl_embedding_run_id',
                      'dismech_import_id', 'legacy_mapping_run_id', 'legacy_gene_set_import_id', 'manifest', 'cold_export_ref')


def _generation_row(row) -> dict:
    data = dict(zip(GENERATION_COLUMNS, row))
    for key in ('manifest', 'cold_export_ref'):
        if isinstance(data[key], (str, bytes)): data[key] = json.loads(data[key])
    return data


def get_generation(connection, generation_id: str) -> dict | None:
    with connection.cursor() as cursor:
        cursor.execute('SELECT ' + ','.join(GENERATION_COLUMNS) + ' FROM reference_generations WHERE generation_id=%s', (generation_id,))
        row = cursor.fetchone()
    return _generation_row(row) if row else None


def list_generations(connection) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute('SELECT ' + ','.join(GENERATION_COLUMNS) + ' FROM reference_generations ORDER BY created_at, generation_id')
        return [_generation_row(row) for row in cursor.fetchall()]


def set_generation_status(connection, generation_id: str, status: str, expected: tuple[str, ...] | None = None) -> None:
    if status not in ('loading', 'complete', 'superseded', 'retired', 'failed'): raise ReferenceError('Invalid generation status')
    with connection.cursor() as cursor:
        if expected:
            cursor.execute('UPDATE reference_generations SET status=%s WHERE generation_id=%s AND status IN (' +
                           ','.join(['%s'] * len(expected)) + ')', (status, generation_id, *expected))
        else:
            cursor.execute('UPDATE reference_generations SET status=%s WHERE generation_id=%s', (status, generation_id))
        if cursor.rowcount != 1 and not (expected and status in expected):
            raise ReferenceError(f'Generation {generation_id} could not move to {status}')
    connection.commit()


def register_legacy_generation(connection, *, mapping_run_id: str, eaggl_import_id: str, gene_set_import_id: str,
                               eaggl_embedding_run_id: str | None, dismech_import_id: str | None,
                               status: str = 'complete') -> str:
    """Record the pre-reload data as a generation (idempotent), so archives can name it."""
    generation_id = legacy_generation_id(mapping_run_id)
    manifest = {'format': 'reveal.reference-generation/1', 'kind': LEGACY_KIND, 'model': LEGACY_MODEL,
                'legacy_mapping_run_id': mapping_run_id, 'eaggl_import_id': eaggl_import_id,
                'legacy_gene_set_import_id': gene_set_import_id}
    with connection.cursor() as cursor:
        cursor.execute('INSERT IGNORE INTO reference_generations(generation_id,kind,model,status,eaggl_import_id,eaggl_embedding_run_id,'
                       'dismech_import_id,legacy_mapping_run_id,legacy_gene_set_import_id,manifest) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                       (generation_id, LEGACY_KIND, LEGACY_MODEL, status, eaggl_import_id, eaggl_embedding_run_id,
                        dismech_import_id, mapping_run_id, gene_set_import_id, canonical(manifest)))
    connection.commit()
    return generation_id
