-- Reusable DisMech query vectors; existing scientific imports remain immutable.
CREATE TABLE IF NOT EXISTS dismech_embedding_runs (
  run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  dismech_import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  eaggl_embedding_run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  config JSON NOT NULL,
  dimensions INT UNSIGNED NOT NULL,
  expected_bindings INT UNSIGNED NOT NULL,
  expected_vectors INT UNSIGNED NOT NULL,
  loaded_bindings INT UNSIGNED NOT NULL DEFAULT 0,
  loaded_vectors INT UNSIGNED NOT NULL DEFAULT 0,
  calibration JSON NOT NULL,
  status VARCHAR(16) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (dismech_import_id) REFERENCES dismech_imports(import_id),
  FOREIGN KEY (eaggl_embedding_run_id) REFERENCES eaggl_embedding_runs(run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS dismech_embedding_vectors (
  run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  input_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  input_text LONGTEXT NOT NULL,
  vector MEDIUMBLOB NOT NULL,
  vector_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (run_id, input_sha256),
  FOREIGN KEY (run_id) REFERENCES dismech_embedding_runs(run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS dismech_embedding_inputs (
  run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT COLLATE utf8mb4_bin NOT NULL,
  source_kind VARCHAR(32) NOT NULL,
  source_revision CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  template VARCHAR(64) NOT NULL,
  input_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (run_id, source_id_sha256),
  FOREIGN KEY (run_id) REFERENCES dismech_embedding_runs(run_id),
  FOREIGN KEY (run_id, input_sha256) REFERENCES dismech_embedding_vectors(run_id, input_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
