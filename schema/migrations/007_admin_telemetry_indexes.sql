-- Additive indexes for the admin console's latest-record and per-kind queries.
-- Apply once through the normal migration procedure. No records are modified.
CREATE INDEX reveal_recent_records ON reveal_records (updated_at, kind, id);
CREATE INDEX reveal_kind_recent ON reveal_records (kind, updated_at, id);
