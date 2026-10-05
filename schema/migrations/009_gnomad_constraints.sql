-- Immutable gnomAD transcript annotations and an explicit per-environment selection.
-- Apply separately from data loading: MySQL DDL commits independently of transactions.
CREATE TABLE IF NOT EXISTS gnomad_constraint_imports (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  source_version VARCHAR(32) COLLATE utf8mb4_bin NOT NULL,
  source_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_url TEXT NOT NULL,
  selection_policy VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  status VARCHAR(16) NOT NULL,
  manifest JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS gnomad_constraint_transcripts (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_row INT UNSIGNED NOT NULL,
  gene_symbol VARCHAR(128) COLLATE utf8mb4_bin NULL,
  gene_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  transcript_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  namespace VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  canonical BOOLEAN NOT NULL,
  mane_select BOOLEAN NOT NULL,
  pli DOUBLE NULL,
  loeuf DOUBLE NULL,
  mis_z DOUBLE NULL,
  lof_oe DOUBLE NULL,
  constraint_flags JSON NOT NULL,
  raw_metrics JSON NOT NULL,
  PRIMARY KEY (import_id, source_row),
  UNIQUE KEY gnomad_transcript_identity (import_id, gene_id, transcript_id),
  INDEX gnomad_transcript_symbol (import_id, gene_symbol),
  CONSTRAINT gnomad_transcript_import_fk FOREIGN KEY (import_id) REFERENCES gnomad_constraint_imports(import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One exact symbol lookup. Ambiguity/missing primary annotations are explicit rows
-- with NULL metrics; no arbitrary gene or transcript wins a symbol collision.
CREATE TABLE IF NOT EXISTS gnomad_gene_constraints (
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_symbol VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
  selection_status VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  selection_reason VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  transcript_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  namespace VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NULL,
  source_row INT UNSIGNED NULL,
  pli DOUBLE NULL,
  loeuf DOUBLE NULL,
  mis_z DOUBLE NULL,
  lof_oe DOUBLE NULL,
  constraint_flags JSON NOT NULL,
  candidate_gene_ids JSON NOT NULL,
  PRIMARY KEY (import_id, gene_symbol),
  INDEX gnomad_gene_pli (import_id, pli, gene_symbol),
  CONSTRAINT gnomad_gene_import_fk FOREIGN KEY (import_id) REFERENCES gnomad_constraint_imports(import_id),
  CONSTRAINT gnomad_gene_transcript_fk FOREIGN KEY (import_id, source_row) REFERENCES gnomad_constraint_transcripts(import_id, source_row)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Local, QA and production share scientific source tables but never activation.
CREATE TABLE IF NOT EXISTS gnomad_constraint_active (
  table_prefix VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  activated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT gnomad_active_import_fk FOREIGN KEY (import_id) REFERENCES gnomad_constraint_imports(import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
