"""Operator-set limits for general agent jobs: each limit's default and ceiling.

box_agent.LIMITS names the limits and the range the service supports. This module decides the default a
request gets and the ceiling it may ask for, in this order: the admin console, the environment, the code.
The console's values are one application record, so every API replica and worker reads the same ones
without a deploy. Nothing here is a secret; credentials stay in the environment (runtime_config).
"""
from copy import deepcopy
import math
import os

from .box_agent import LIMITS, limit_value
from .repository import Conflict, now

KIND, IDENTITY, OWNER = 'agent_limits', 'current', 'admin-console'
FORMAT = 'reveal.agent-limits/1'
FIELDS = ('default', 'ceiling')
MAX_HISTORY = 50

# What the console shows beside each limit.
LABELS = {
    'timeout_seconds': ('Run time', 'seconds', 'Wall-clock time for one run. The Box adapter supports at most one hour.'),
    'max_turns': ('Turns', 'turns', 'Claude Code turns in one run.'),
    'max_budget_usd': ('Budget', 'USD', 'Provider cost of one run.'),
    'tool_calls': ('REVEAL tool calls', 'calls', 'Paper search, paper read and graph tool calls, counted together.'),
    'literature_searches': ('Paper searches', 'searches', 'Europe PMC searches in one run.'),
    'literature_search_results': ('Results per search', 'results', 'Most records one paper search may return.'),
    'literature_papers': ('Papers read', 'papers', 'Distinct papers. Paging through one paper does not use another.'),
    'literature_characters': ('Paper text', 'characters', 'Abstract and full-text characters returned across all reads.'),
}


class LimitsError(ValueError):
    pass


def environment_name(key, field):
    """REVEAL_AGENT_JOB_MAX_BUDGET_USD predates this module; the other names follow it."""
    return 'REVEAL_AGENT_JOB_' + ('MAX_' if field == 'ceiling' else 'DEFAULT_') + key.removeprefix('max_').upper()


def parse(key, raw):
    """A limit value from environment text, or None when it is not one."""
    try:
        value = int(raw) if LIMITS[key]['type'] is int else float(raw)
    except (TypeError, ValueError):
        return None
    return value if limit_value(key, value) else None


def stored(tx):
    row = tx.get(KIND, IDENTITY)
    data = row['data'] if row and isinstance(row.get('data'), dict) and row['data'].get('format') == FORMAT else None
    return (data, row['version']) if data else (None, row['version'] if row else None)


def past_changes(data):
    history = data.get('history') if isinstance(data, dict) else None
    return [entry for entry in history if isinstance(entry, dict)] if isinstance(history, list) else []


def console_values(data):
    """The console's usable values: {key: {field: value}}. Anything out of range is dropped, never applied."""
    values = data.get('values') if isinstance(data, dict) else None
    result = {}
    for key, entry in (values.items() if isinstance(values, dict) else ()):
        if key in LIMITS and isinstance(entry, dict):
            kept = {field: entry[field] for field in FIELDS if field in entry and limit_value(key, entry[field])}
            if kept: result[key] = kept
    return result


def resolve(console, environ=None):
    """Each limit's default and ceiling with where it came from: console, environment or code."""
    environ = os.environ if environ is None else environ
    resolved = {}
    for key, spec in LIMITS.items():
        entry = {}
        for field in FIELDS:
            name = environment_name(key, field)
            from_environment = parse(key, environ[name]) if environ.get(name) else None
            if field in console.get(key, {}):
                entry[field] = {'value': console[key][field], 'source': 'console'}
            elif from_environment is not None:
                entry[field] = {'value': from_environment, 'source': 'environment'}
            else:
                entry[field] = {'value': spec[field], 'source': 'code'}
            entry[field]['environment'] = name
            if environ.get(name) and from_environment is None: entry[field]['ignored_environment_value'] = True
        if entry['default']['value'] > entry['ceiling']['value']:
            # A ceiling lowered below the default (as a budget cap of 0.5 under the 2.00 default) lowers the default too.
            entry['default'] = {**entry['default'], 'value': entry['ceiling']['value'], 'lowered_to_ceiling': True}
        if spec['type'] is float:
            for field in FIELDS: entry[field]['value'] = float(entry[field]['value'])
        resolved[key] = entry
    return resolved


def policy(tx, environ=None):
    """{limit: {default, ceiling}} for box_agent.freeze_input."""
    data, _ = stored(tx)
    return {key: {field: entry[field]['value'] for field in FIELDS} for key, entry in resolve(console_values(data), environ).items()}


def budget_ceiling(repository, environ=None):
    """The current budget ceiling, for the dispatch-time clamp of a queued job's frozen budget."""
    with repository.read_transaction() as tx:
        return policy(tx, environ)['max_budget_usd']['ceiling']


def view(tx, environ=None):
    """What the admin console reads."""
    data, version = stored(tx)
    console = console_values(data)
    resolved = resolve(console, environ)
    limits = []
    for key, spec in LIMITS.items():
        label, unit, description = LABELS[key]
        limits.append({'key': key, 'label': label, 'unit': unit, 'description': description,
                       'type': 'integer' if spec['type'] is int else 'number',
                       'minimum': spec['minimum'], 'exclusive_minimum': spec['type'] is float, 'maximum': spec['maximum'],
                       'code': {field: spec[field] for field in FIELDS},
                       'console': {field: console.get(key, {}).get(field) for field in FIELDS},
                       **{field: resolved[key][field] for field in FIELDS}})
    return {'format': FORMAT, 'version': version, 'limits': limits,
            'updated_at': data.get('updated_at') if data else None, 'updated_by': data.get('updated_by') if data else None,
            'history': list(reversed(past_changes(data)))}


def change(tx, body, actor, environ=None):
    """Replace the console's values. body: {'expected_version': int|None, 'values': {key: {default?, ceiling?}}}.

    A field left out or null is not set in the console, so the environment or the code decides it. Every value
    must lie in the supported range, and each limit's resulting default may not exceed its resulting ceiling.
    expected_version is the version the editor last read; a concurrent change is refused."""
    if not isinstance(body, dict) or set(body) - {'expected_version', 'values'} or not isinstance(body.get('values'), dict):
        raise LimitsError('Send {"expected_version": …, "values": {limit: {"default": …, "ceiling": …}}}.')
    data, version = stored(tx)
    if body.get('expected_version') != version:
        raise Conflict('These limits changed since you loaded them. Reload, then apply your change again.')
    values = {}
    for key, entry in body['values'].items():
        if key not in LIMITS: raise LimitsError(f'{key} is not an agent job limit.')
        if not isinstance(entry, dict) or set(entry) - set(FIELDS): raise LimitsError(f'{key} takes default and ceiling only.')
        spec, kept = LIMITS[key], {}
        for field in FIELDS:
            value = entry.get(field)
            if value is None: continue
            if spec['type'] is float and type(value) is int: value = float(value)
            if not limit_value(key, value):
                bound = ('above ' if spec['type'] is float else 'from ') + f"{spec['minimum']:g} to {spec['maximum']:g}"
                kind = 'a number' if spec['type'] is float else 'a whole number'
                raise LimitsError(f'{LABELS[key][0]} {field} must be {kind} {bound}.')
            kept[field] = value
        if kept: values[key] = kept
    resolved = resolve(values, environ)
    for key, entry in resolved.items():
        if entry['default'].get('lowered_to_ceiling') and 'default' in values.get(key, {}):
            raise LimitsError(f"{LABELS[key][0]} default {values[key]['default']:g} is above its ceiling {entry['ceiling']['value']:g}.")
    before = console_values(data)
    changes = {key: {field: [before.get(key, {}).get(field), values.get(key, {}).get(field)] for field in FIELDS
                     if before.get(key, {}).get(field) != values.get(key, {}).get(field)} for key in LIMITS}
    changes = {key: entry for key, entry in changes.items() if entry}
    if not changes: return view(tx, environ)
    actor = actor if isinstance(actor, str) and 0 < len(actor) <= 254 else OWNER
    stamp = now()
    history = [*deepcopy(past_changes(data)), {'at': stamp, 'by': actor, 'changes': changes}][-MAX_HISTORY:]
    tx.put(KIND, IDENTITY, OWNER, {'format': FORMAT, 'values': values, 'updated_at': stamp, 'updated_by': actor, 'history': history},
           expected=version)
    return view(tx, environ)
