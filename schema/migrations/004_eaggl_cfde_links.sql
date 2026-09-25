-- Application routing by exact trait and factor number, irrespective of labels
-- or gene agreement. Depends on migrations 001 and 002, not DisMech migration 003.
CREATE TABLE IF NOT EXISTS eaggl_cfde_link_runs (
  run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  eaggl_import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_set_import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  model VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  match_method VARCHAR(64) CHARACTER SET ascii NOT NULL,
  status VARCHAR(16) CHARACTER SET ascii NOT NULL,
  manifest JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY link_run_eaggl (run_id, eaggl_import_id),
  UNIQUE KEY link_run_gene_sets (run_id, gene_set_import_id),
  INDEX link_run_source (eaggl_import_id),
  INDEX link_run_gene_set_import (gene_set_import_id),
  CONSTRAINT link_run_source_fk FOREIGN KEY (eaggl_import_id) REFERENCES eaggl_imports(import_id),
  CONSTRAINT link_run_gene_sets_fk FOREIGN KEY (gene_set_import_id) REFERENCES gene_set_imports(import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS eaggl_cfde_factor_links (
  run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  factor_index INT UNSIGNED NOT NULL,
  eaggl_import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  cfde_node_id TEXT NOT NULL,
  cfde_node_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  cfde_trait TEXT NOT NULL,
  cfde_factor_number INT UNSIGNED NOT NULL,
  cfde_trait_group VARCHAR(64) NOT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY (run_id, factor_index),
  UNIQUE KEY cfde_factor_link_target (run_id, cfde_node_sha256),
  INDEX cfde_factor_link_run (run_id, eaggl_import_id),
  INDEX cfde_factor_link_source (eaggl_import_id, factor_index),
  CONSTRAINT factor_link_run_fk FOREIGN KEY (run_id, eaggl_import_id) REFERENCES eaggl_cfde_link_runs(run_id, eaggl_import_id),
  CONSTRAINT factor_link_source_fk FOREIGN KEY (eaggl_import_id, factor_index) REFERENCES eaggl_factors(import_id, factor_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- CFDE metadata's ranked top_gene_sets summary, not the complete loading matrix.
-- A missing catalog alias is retained with resolved_alias_sha256=NULL.
CREATE TABLE IF NOT EXISTS eaggl_cfde_gene_set_links (
  run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  factor_index INT UNSIGNED NOT NULL,
  gene_set_rank INT UNSIGNED NOT NULL,
  gene_set_import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_key TEXT NOT NULL,
  node_id TEXT NOT NULL,
  node_id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  resolved_alias_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
  PRIMARY KEY (run_id, factor_index, gene_set_rank),
  INDEX cfde_gene_set_link_run (run_id, gene_set_import_id),
  INDEX cfde_gene_set_link_alias (gene_set_import_id, resolved_alias_sha256),
  CONSTRAINT gene_set_link_factor_fk FOREIGN KEY (run_id, factor_index) REFERENCES eaggl_cfde_factor_links(run_id, factor_index),
  CONSTRAINT gene_set_link_run_fk FOREIGN KEY (run_id, gene_set_import_id) REFERENCES eaggl_cfde_link_runs(run_id, gene_set_import_id),
  CONSTRAINT gene_set_link_alias_fk FOREIGN KEY (gene_set_import_id, resolved_alias_sha256) REFERENCES cfde_gene_set_aliases(import_id, node_id_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
