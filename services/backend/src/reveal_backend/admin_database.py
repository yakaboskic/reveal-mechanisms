"""Read-only table inspection with bounded values and primary-key pagination."""
import base64
import hashlib
import hmac
import json
import os
import re
import time
from .auth import Problem
from .repository import application_sql
from .runtime_config import ROOT

PREVIEW = 400
CHUNK = 32768
SCALARS = {'String', 'Int', 'BigInt', 'Float', 'Decimal', 'Boolean', 'DateTime', 'Json', 'Bytes'}
# Project lifecycle metadata separately: a payload preview can end long before
# these fields, and downloading entire scientific documents just for dates is costly.
PAYLOAD_TIMES = (
    'created_at', 'updated_at', 'started_at', 'completed_at', 'occurred_at', 'observed_at',
    'generated_at', 'generated_at_time', 'first_minted_at', 'published_at', 'expires_at', 'lease_until',
    'summary.created_at', 'summary.updated_at', 'attribution.observed_at',
    'document.generated_at_time', 'me.workspace_expires_at', 'remote_handle.created_at',
    *(f'remote_handle.timings.{phase}_at' for phase in ('prepared', 'running', 'terminal', 'captured', 'deleted')),
)


def select_timestamps(tx, table):
    if table != 'reveal_records': return "'{}'"
    fields = []
    for path in PAYLOAD_TIMES:
        value = "JSON_EXTRACT(payload,'$."+path+"')"
        if not tx.sqlite: value = 'JSON_UNQUOTE('+value+')'
        # Bounded even if a malformed record stores a document in a time field.
        value = 'SUBSTR(CAST('+value+(' AS TEXT)' if tx.sqlite else ' AS CHAR CHARACTER SET utf8mb4)')+',1,64)'
        fields.extend(("'payload."+path+"'", value))
    return 'JSON_OBJECT('+','.join(fields)+')'


def timestamps(columns, cells, projected):
    values = {c['name']: cells[c['name']]['text'] for c in columns
              if c['type'] == 'DateTime' or c['name'] == 'updated_at'}
    values.update(json.loads(projected))
    return {name: value for name, value in values.items() if value is not None and value != 'null'}


def tables():
    """The checked-in Prisma schema is the identifier allowlist, never browser input."""
    result = {}
    schema = (ROOT / 'schema/prisma/schema.prisma').read_text()
    for model, body in re.findall(r'model\s+(\w+)\s*\{([^}]+)\}', schema):
        mapped = re.search(r'@@map\("([a-z_]+)"\)', body)
        if not mapped: continue
        columns, primary = [], []
        for line in body.splitlines():
            field = re.match(r'\s*(\w+)\s+(\w+)(\??)(.*)$', line)
            if not field or field[2] not in SCALARS or field[4].startswith('[]'): continue
            name, kind, optional, attributes = field.groups()
            physical = re.search(r'(?<!@)@map\("(\w+)"\)', attributes)
            columns.append({'name': physical[1] if physical else name, 'field': name,
                            'type': kind, 'nullable': bool(optional)})
            if re.search(r'(?<!@)@id\b', attributes): primary.append(name)
        compound = re.search(r'@@id\(\[([^\]]+)\]', body)
        if compound: primary = [part.strip() for part in compound[1].split(',')]
        names = {c['field']: c['name'] for c in columns}
        result[mapped[1]] = {'model': model, 'columns': columns, 'primary_key': [names[p] for p in primary]}
    return result


def identifier(name):
    # Even schema-defined identifiers must fit the supported SQL identifier grammar.
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name): raise ValueError('Unsupported schema identifier')
    return '`'+name+'`'


def schema_for(tx, name):
    schema = tables().get(name)
    if not schema: raise Problem(404, 'TABLE_NOT_FOUND', 'This table is not in the application schema.')
    actual = application_sql(name, tx.table_prefix)  # the environment's own records and reference release tables
    if tx.sqlite:
        present = tx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=%s", (actual,)).fetchone()
    else:
        present = tx.execute('SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s', (actual,)).fetchone()
    if not present: raise Problem(404, 'TABLE_NOT_FOUND', 'This table is not present in the database.')
    if not schema['primary_key']: raise Problem(422, 'PRIMARY_KEY_REQUIRED', 'This table needs a primary key for stable inspection.')
    return schema


def expression(table, column):
    value = identifier(column['name'])
    if table == 'reveal_records' and column['name'] == 'payload':
        # Queue/attempt lease credentials must never leave the backend, including
        # previews, cell chunks and filtering. Other record contents stay intact.
        value = "JSON_REMOVE("+value+",'$.token')"
    return value


def text_expression(tx, table, column):
    value = expression(table, column)
    if column['type'] == 'Bytes': return value
    return 'CAST('+value+(' AS TEXT)' if tx.sqlite else ' AS CHAR CHARACTER SET utf8mb4)')


def select_cells(tx, table, columns, length):
    sql = []
    for column in columns:
        value = text_expression(tx, table, column)
        binary = column['type'] == 'Bytes'
        size = ('LENGTH' if tx.sqlite or binary else 'CHAR_LENGTH')+'('+value+')'
        preview = 'SUBSTR('+value+',1,'+str(length)+')'
        if binary: preview = 'CASE WHEN '+value+' IS NULL THEN NULL ELSE HEX('+preview+') END'
        sql.extend((preview, size))
    return ','.join(sql)


def cell(column, text, length):
    binary = column['type'] == 'Bytes'
    loaded = len(text or '') // (2 if binary else 1)
    return {'text': text, 'length': length, 'loaded': loaded, 'truncated': length is not None and loaded < length,
            'binary': binary, 'json': column['type'] == 'Json'}


def unpack(columns, values):
    return {column['name']: cell(column, values[2*i], values[2*i+1]) for i, column in enumerate(columns)}


def read_key(value, primary):
    try:
        key = json.loads(value) if isinstance(value, str) else value
        if not isinstance(key, dict) or set(key) != set(primary): raise ValueError()
        if any(not isinstance(v, str) or len(v) > 512 for v in key.values()): raise ValueError()
        return key
    except (ValueError, TypeError):
        raise Problem(422, 'INVALID_ROW_KEY', 'Supply the complete primary key for this table.') from None


def sign_cursor(value):
    encoded = base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode().rstrip('=')
    signature = hmac.new(os.environ['REVEAL_GATEWAY_SECRET'].encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded+'.'+signature


def read_cursor(value, scope, primary):
    try:
        if len(value) > 8192: raise ValueError()
        encoded, signature = value.split('.')
        expected = hmac.new(os.environ['REVEAL_GATEWAY_SECRET'].encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected): raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
        if data['scope'] != scope or data['expires'] < time.time(): raise ValueError()
        return read_key(data['key'], primary)
    except (ValueError, KeyError, TypeError, Problem):
        raise Problem(422, 'INVALID_CURSOR', 'This page cursor expired or belongs to another table or filter. Return to the first page.') from None


def query(tx, sql, args=()):
    # Bound scans for contains filters on large scientific tables. No write SQL
    # or user-provided SQL fragments are accepted anywhere in this module.
    if not tx.sqlite: sql = sql.replace('SELECT ', 'SELECT /*+ MAX_EXECUTION_TIME(5000) */ ', 1)
    try: return tx.execute(sql, args)
    except Exception as exc:
        if getattr(exc, 'args', (None,))[0] in (3024, 1969):
            raise Problem(504, 'INSPECTION_TIMEOUT', 'The table scan took too long. Try an exact match on a primary-key column.') from None
        raise


def inspect_table(repository, table, *, limit=25, cursor=None, column=None, operator='contains', q=''):
    if not 1 <= limit <= 50 or len(q) > 256 or operator not in ('equals', 'contains'):
        raise Problem(422, 'INVALID_FILTER', 'Choose 1–50 rows and a filter of at most 256 characters.')
    with repository.read_transaction(utc=True) as tx:
        schema = schema_for(tx, table); columns = schema['columns']; primary = schema['primary_key']
        by_name = {c['name']: c for c in columns}
        if column and (column not in by_name or by_name[column]['type'] in ('Bytes', 'Json')):
            raise Problem(422, 'INVALID_COLUMN', 'Choose a scalar column for filtering.')
        if q and not column: raise Problem(422, 'INVALID_FILTER', 'Choose a column to search.')
        where, args = [], []
        if q:
            if operator == 'equals':
                where.append(identifier(column)+'=%s'); args.append(q)
            else:
                where.append(text_expression(tx, table, by_name[column])+" LIKE %s ESCAPE '!'")
                args.append('%'+q.replace('!', '!!').replace('%', '!%').replace('_', '!_')+'%')
        scope = [table, limit, column, operator, q]
        if cursor:
            last = read_cursor(cursor, scope, primary)
            branches = []
            for i, name in enumerate(primary):
                branches.append('('+' AND '.join([identifier(p)+'=%s' for p in primary[:i]]+[identifier(name)+'>%s'])+')')
                args.extend(last[p] for p in primary[:i+1])
            where.append('('+' OR '.join(branches)+')')
        sql = 'SELECT '+','.join(identifier(p) for p in primary)+','+select_cells(tx, table, columns, PREVIEW)+','+select_timestamps(tx, table)+' FROM '+identifier(table)
        if where: sql += ' WHERE '+' AND '.join(where)
        sql += ' ORDER BY '+','.join(identifier(p) for p in primary)+' LIMIT %s'
        args.append(limit+1)
        values = query(tx, sql, args).fetchall()
        rows = []
        for row in values[:limit]:
            cells = unpack(columns, row[len(primary):])
            rows.append({'key': {p: str(row[i]) for i, p in enumerate(primary)}, 'cells': cells,
                         'timestamps': timestamps(columns, cells, row[-1])})
        next_cursor = sign_cursor({'scope': scope, 'key': rows[-1]['key'], 'expires': time.time()+1800}) if len(values) > limit else None
        return {'table': table, **schema, 'rows': rows, 'next_cursor': next_cursor, 'limit': limit}


def inspect_row(repository, table, key):
    with repository.read_transaction(utc=True) as tx:
        schema = schema_for(tx, table); primary = schema['primary_key']; key = read_key(key, primary)
        where = ' AND '.join(identifier(p)+'=%s' for p in primary)
        row = query(tx, 'SELECT '+select_cells(tx, table, schema['columns'], CHUNK)+','+select_timestamps(tx, table)+' FROM '+identifier(table)+' WHERE '+where,
                    [key[p] for p in primary]).fetchone()
        if row is None: raise Problem(404, 'ROW_NOT_FOUND', 'This row no longer exists. Refresh the table.')
        cells = unpack(schema['columns'], row)
        return {'table': table, 'key': key, 'columns': schema['columns'], 'cells': cells,
                'timestamps': timestamps(schema['columns'], cells, row[-1])}


def inspect_cell(repository, table, key, column, offset=0, digest=None):
    if not 0 <= offset <= 100_000_000: raise Problem(422, 'INVALID_OFFSET', 'Invalid cell offset.')
    with repository.read_transaction(utc=True) as tx:
        schema = schema_for(tx, table); primary = schema['primary_key']; key = read_key(key, primary)
        field = next((c for c in schema['columns'] if c['name'] == column), None)
        if field is None: raise Problem(422, 'INVALID_COLUMN', 'Unknown column.')
        value = text_expression(tx, table, field); binary = field['type'] == 'Bytes'
        # Hash only the selected field to detect updates between chunks. SQLite
        # provides a local equivalent for tests; production computes it in MySQL.
        if tx.sqlite: tx.connection.create_function('MD5', 1, lambda v: hashlib.md5(v if isinstance(v, bytes) else str(v).encode()).hexdigest() if v is not None else None)
        piece = 'SUBSTR('+value+',%s,%s)'
        if binary: piece = 'CASE WHEN '+value+' IS NULL THEN NULL ELSE HEX('+piece+') END'
        size = ('LENGTH' if tx.sqlite or binary else 'CHAR_LENGTH')+'('+value+')'
        where = ' AND '.join(identifier(p)+'=%s' for p in primary)
        row = query(tx, 'SELECT '+piece+','+size+',MD5('+value+') FROM '+identifier(table)+' WHERE '+where,
                    [offset+1, CHUNK, *[key[p] for p in primary]]).fetchone()
        if row is None: raise Problem(404, 'ROW_NOT_FOUND', 'This row no longer exists. Refresh the table.')
        if offset and (not digest or digest != row[2]):
            raise Problem(409, 'CELL_CHANGED', 'This value changed while you were reading it. Reload the row.')
        result = cell(field, row[0], row[1])
        result['loaded'] += offset
        result['truncated'] = row[1] is not None and result['loaded'] < row[1]
        return {**result, 'digest': row[2]}
