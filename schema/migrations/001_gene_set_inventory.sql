-- Applied only inside the explicitly selected cyaka_ project database.
CREATE TABLE IF NOT EXISTS dapper_objects (
  id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  class_name VARCHAR(64) CHARACTER SET ascii NOT NULL,
  identity_profile VARCHAR(32) CHARACTER SET ascii NOT NULL,
  payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  payload JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS gene_set_imports (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  model VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  status VARCHAR(16) CHARACTER SET ascii NOT NULL,
  loaded_rows INT UNSIGNED NOT NULL DEFAULT 0,
  expected_rows INT UNSIGNED NOT NULL,
  manifest JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS cfde_gene_set_aliases (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  node_id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  model VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_key TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  node_id TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  dapper_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  provenance JSON NOT NULL,
  PRIMARY KEY (import_id, node_id_sha256),
  INDEX lookup_node (model, node_id_sha256),
  INDEX lookup_dapper (dapper_id),
  FOREIGN KEY (import_id) REFERENCES gene_set_imports(import_id),
  FOREIGN KEY (dapper_id) REFERENCES dapper_objects(id)
) ENGINE=InnoDB;
