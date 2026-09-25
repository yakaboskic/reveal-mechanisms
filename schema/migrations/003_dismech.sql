-- Source projections of pinned DisMech exports, scoped to an immutable import.
-- DAPPER identities are not assigned by this migration or importer.
CREATE TABLE IF NOT EXISTS dismech_imports (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  source_commit VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  status VARCHAR(16) NOT NULL,
  manifest JSON NOT NULL,
  source_files JSON NOT NULL,
  schema_vocabularies JSON NOT NULL,
  progress JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dismech_documents (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT NOT NULL,
  name TEXT NOT NULL,
  kind VARCHAR(64) NOT NULL,
  source_file TEXT NOT NULL,
  source_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY (import_id, id_sha256),
  INDEX dismech_document_kind (import_id, kind),
  FOREIGN KEY (import_id) REFERENCES dismech_imports(import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dismech_mechanisms (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  document_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT NOT NULL,
  source_pointer TEXT NOT NULL,
  name TEXT,
  description LONGTEXT,
  payload JSON NOT NULL,
  PRIMARY KEY (import_id, id_sha256),
  FOREIGN KEY (import_id, document_sha256) REFERENCES dismech_documents(import_id, id_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dismech_causal_edges (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  mechanism_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT NOT NULL,
  target_resolution VARCHAR(64) NOT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY (import_id, id_sha256),
  FOREIGN KEY (import_id, mechanism_sha256) REFERENCES dismech_mechanisms(import_id, id_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dismech_hypotheses (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  document_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT NOT NULL,
  source_pointer TEXT NOT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY (import_id, id_sha256),
  FOREIGN KEY (import_id, document_sha256) REFERENCES dismech_documents(import_id, id_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dismech_ontology_terms (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT NOT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY (import_id, id_sha256),
  FOREIGN KEY (import_id) REFERENCES dismech_imports(import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dismech_vocabulary (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT NOT NULL,
  kind VARCHAR(64) NOT NULL,
  label TEXT NOT NULL,
  term_id TEXT,
  payload JSON NOT NULL,
  PRIMARY KEY (import_id, id_sha256),
  INDEX dismech_vocabulary_kind (import_id, kind),
  FOREIGN KEY (import_id) REFERENCES dismech_imports(import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- Gaps are the source discussions where is_gap=1. Other discussion kinds remain
-- available, and absent source statuses remain NULL rather than becoming OPEN.
CREATE TABLE IF NOT EXISTS dismech_discussions (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  document_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT NOT NULL,
  discussion_id TEXT,
  source_pointer TEXT NOT NULL,
  kind VARCHAR(64),
  status VARCHAR(64),
  is_gap BOOLEAN NOT NULL,
  has_stable_source_id BOOLEAN NOT NULL,
  prompt LONGTEXT NOT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY (import_id, id_sha256),
  INDEX dismech_gap_filter (import_id, is_gap, kind, status),
  FOREIGN KEY (import_id, document_sha256) REFERENCES dismech_documents(import_id, id_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dismech_gap_attachments (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gap_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  attachment_index INT UNSIGNED NOT NULL,
  source_reference TEXT NOT NULL,
  resolution VARCHAR(64) NOT NULL,
  target_document_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
  target_mechanism_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
  target_id TEXT,
  target_pointer TEXT,
  target_kind VARCHAR(64),
  payload JSON NOT NULL,
  PRIMARY KEY (import_id, gap_sha256, attachment_index),
  FOREIGN KEY (import_id, gap_sha256) REFERENCES dismech_discussions(import_id, id_sha256),
  FOREIGN KEY (import_id, target_document_sha256) REFERENCES dismech_documents(import_id, id_sha256),
  FOREIGN KEY (import_id, target_mechanism_sha256) REFERENCES dismech_mechanisms(import_id, id_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
