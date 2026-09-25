-- Source-specific import projections; no inferred CFDE or DAPPER equivalence.
CREATE TABLE IF NOT EXISTS eaggl_imports (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  source_namespace VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  source_version VARCHAR(255) COLLATE utf8mb4_bin NOT NULL,
  status VARCHAR(16) NOT NULL,
  manifest JSON NOT NULL,
  progress JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS eaggl_genes (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_index INT UNSIGNED NOT NULL,
  symbol TEXT COLLATE utf8mb4_bin NOT NULL,
  PRIMARY KEY (import_id, gene_index),
  FOREIGN KEY (import_id) REFERENCES eaggl_imports(import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS eaggl_factors (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  factor_index INT UNSIGNED NOT NULL,
  factor_id TEXT COLLATE utf8mb4_bin NOT NULL,
  factor_id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  trait TEXT COLLATE utf8mb4_bin NOT NULL,
  label TEXT NOT NULL,
  input_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  metadata JSON NOT NULL,
  PRIMARY KEY (import_id, factor_index),
  UNIQUE KEY factor_lookup (import_id, factor_id_sha256),
  FOREIGN KEY (import_id) REFERENCES eaggl_imports(import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS eaggl_gene_loadings (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  factor_index INT UNSIGNED NOT NULL,
  gene_index INT UNSIGNED NOT NULL,
  loading DOUBLE NOT NULL,
  PRIMARY KEY (import_id, factor_index, gene_index),
  FOREIGN KEY (import_id, factor_index) REFERENCES eaggl_factors(import_id, factor_index),
  FOREIGN KEY (import_id, gene_index) REFERENCES eaggl_genes(import_id, gene_index),
  CHECK (loading > 0 AND loading <= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS eaggl_graph_nodes (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  node_index INT UNSIGNED NOT NULL,
  node_id VARCHAR(255) COLLATE utf8mb4_bin NOT NULL,
  factor_index INT UNSIGNED,
  payload JSON NOT NULL,
  PRIMARY KEY (import_id, node_index),
  UNIQUE KEY node_lookup (import_id, node_id),
  FOREIGN KEY (import_id) REFERENCES eaggl_imports(import_id),
  FOREIGN KEY (import_id, factor_index) REFERENCES eaggl_factors(import_id, factor_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS eaggl_graph_edges (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  parent_index INT UNSIGNED NOT NULL,
  child_index INT UNSIGNED NOT NULL,
  PRIMARY KEY (import_id, parent_index, child_index),
  FOREIGN KEY (import_id, parent_index) REFERENCES eaggl_graph_nodes(import_id, node_index),
  FOREIGN KEY (import_id, child_index) REFERENCES eaggl_graph_nodes(import_id, node_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS eaggl_embedding_runs (
  run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  config JSON NOT NULL,
  dimensions INT UNSIGNED NOT NULL,
  expected_rows INT UNSIGNED NOT NULL,
  loaded_rows INT UNSIGNED NOT NULL DEFAULT 0,
  status VARCHAR(16) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (import_id) REFERENCES eaggl_imports(import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS eaggl_name_embeddings (
  run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  input_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  input_text TEXT NOT NULL,
  vector MEDIUMBLOB NOT NULL,
  vector_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (run_id, input_sha256),
  FOREIGN KEY (run_id) REFERENCES eaggl_embedding_runs(run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
