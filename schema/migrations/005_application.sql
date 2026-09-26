-- Explicit, additive application migration. No source catalog is modified.
-- A versioned JSON envelope retains the exact public/source payload. Kind and
-- owner are indexed separately; transactional revisions and leases fence writes.
CREATE TABLE IF NOT EXISTS reveal_records (
  kind VARCHAR(40) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  owner_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  version BIGINT NOT NULL DEFAULT 1,
  payload JSON NOT NULL,
  updated_at VARCHAR(32) NOT NULL,
  PRIMARY KEY (kind,id),
  INDEX reveal_owner_kind (owner_id,kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS reveal_transaction_lock (
  id INT PRIMARY KEY,
  revision BIGINT NOT NULL DEFAULT 0
) ENGINE=InnoDB;
INSERT IGNORE INTO reveal_transaction_lock(id,revision) VALUES (1,0);
