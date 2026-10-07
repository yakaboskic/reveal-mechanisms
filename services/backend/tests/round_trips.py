"""Count Repository leases, statements and unpooled connects per request on SQLite (no MySQL or Redis).

trips() converts them to Aurora round trips with the per-lease protocol constants below, which
test_round_trip_budget.WireConstantsTests pins to the real Pool + Repository: change both together.
Background threads a route starts are not attributed reliably; stub them in the test.
"""
from contextlib import contextmanager
import sys
from unittest.mock import patch

from reveal_backend import runtime_config
from reveal_backend.repository import Repository, Transaction

# Statements SQLite does not send through tx.execute, per lease kind.
OPEN = {'read': 1,     # START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY
        'write': 1,    # SELECT ... FOR UPDATE on the global fence (SQLite uses BEGIN IMMEDIATE)
        'single': 0,   # single_read(): the SELECT opens its own statement-level read view
        'append': 0}   # append(): the INSERT opens its own transaction (autocommit off), no fence
END = 1                # COMMIT, or ROLLBACK for single_read
RELEASE = 0            # a clean lease returns to idle with no command; non-neutral SQL costs a 1-trip reset


class Budget:
    def __init__(self): self.leases, self.unleased, self.connects = [], 0, 0
    def kinds(self): return [kind for kind, _ in self.leases]
    def statements(self): return sum(n for _, n in self.leases) + self.unleased
    def trips(self): return self.statements() + sum(OPEN[kind] + END + RELEASE for kind, _ in self.leases)
    def locked_trips(self):
        """Round trips spent holding the global write fence (FOR UPDATE grant through COMMIT)."""
        return sum(n + END for kind, n in self.leases if kind == 'write')
    def __repr__(self):
        return f'Budget(leases={self.leases}, unleased={self.unleased}, connects={self.connects}, trips={self.trips()})'


@contextmanager
def count_round_trips():
    budget = Budget()
    names = {'read': 'read_transaction', 'write': 'transaction', 'single': 'single_read', 'append': '_append_lease'}
    originals = {kind: getattr(Repository, name) for kind, name in names.items()}
    execute = Transaction.execute

    def lease(kind, original):
        @contextmanager
        def wrapper(self, *args, **kwargs):
            entry = [kind, 0]; budget.leases.append(entry)
            with original(self, *args, **kwargs) as tx:
                tx.round_trip_lease = entry  # attributed by transaction, not by nesting order
                yield tx
        return wrapper

    def counted(self, sql, params=()):
        entry = getattr(self, 'round_trip_lease', None)
        if entry is None: budget.unleased += 1
        else: entry[1] += 1
        return execute(self, sql, params)

    def unpooled(*args, **kwargs):
        budget.connects += 1
        raise AssertionError('unpooled mysql_connection() on a budgeted route')

    # Modules bind mysql_connection by name; patch every binding plus the source for lazy imports.
    targets = [runtime_config] + [module for name, module in list(sys.modules.items())
        if name.startswith('reveal_backend.') and getattr(module, 'mysql_connection', None) is runtime_config.mysql_connection]
    patches = [patch.object(Repository, names[kind], lease(kind, original)) for kind, original in originals.items()]
    patches += [patch.object(Transaction, 'execute', counted)]
    patches += [patch.object(module, 'mysql_connection', unpooled) for module in targets]
    for item in patches: item.start()
    try: yield budget
    finally:
        for item in reversed(patches): item.stop()
