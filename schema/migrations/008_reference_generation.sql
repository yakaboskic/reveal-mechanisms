-- Reference generations: versioned EAGGL factor + CFDE gene-set reference data that the
-- application serves, loaded additively and switched per environment by the protected
-- `python -m reveal_backend.reference_reload` command (see docs/reference-reload.md).
--
-- Every table is scoped by generation_id. A generation is loaded next to the previous one,
-- each environment's <prefix>_records `reference_active` pointer is switched to it, and only
-- then are retired generations purged. archived_reference_factors is never purged: it holds
-- the frozen factor snapshots that archived scientific accounts and outcomes refer to.
-- Depends on migrations 002 (eaggl_imports) and 003 (dismech_imports) only by convention;
-- no foreign keys point outside this migration, so retired legacy tables can be purged.

CREATE TABLE IF NOT EXISTS reference_generations (
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  model VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  eaggl_import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  eaggl_embedding_run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  dismech_import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  legacy_mapping_run_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  legacy_gene_set_import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  manifest JSON NOT NULL,
  cold_export_ref JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY reference_generation_legacy_mapping (legacy_mapping_run_id),
  INDEX reference_generation_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS kpn_traits (
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  kpn_trait_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  legacy_phenotype_id VARCHAR(191) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  phenotype_name TEXT NOT NULL,
  gwas_source_category VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  trait_group VARCHAR(64) NULL,
  legacy_trait_group VARCHAR(64) NULL,
  trait_type VARCHAR(32) NULL,
  description TEXT NULL,
  is_dichotomous TINYINT NULL,
  is_complex TINYINT NULL,
  n_factors INT UNSIGNED NOT NULL,
  metadata JSON NOT NULL,
  PRIMARY KEY (generation_id, kpn_trait_id),
  UNIQUE KEY kpn_trait_legacy (generation_id, legacy_phenotype_id),
  CONSTRAINT kpn_trait_generation_fk FOREIGN KEY (generation_id) REFERENCES reference_generations(generation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- One served EAGGL factor. factor_key = KPN.TRAIT:NNNNNNN::FactorN (canonical key and Upstash
-- vector id); public_id = factor:kpn:NNNNNNN:<model>:FactorN (API/source_id, 5 colon-free
-- segments); eaggl_factor_id = <EAGGL trait>::FactorN (the atlas id, legacy alias).
CREATE TABLE IF NOT EXISTS reference_factors (
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  factor_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  public_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  eaggl_factor_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  kpn_trait_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  factor_number INT UNSIGNED NOT NULL,
  label TEXT NOT NULL,
  eaggl_import_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  input_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_revision CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  metadata JSON NOT NULL,
  PRIMARY KEY (generation_id, factor_key),
  UNIQUE KEY reference_factor_public (generation_id, public_id),
  UNIQUE KEY reference_factor_eaggl (generation_id, eaggl_factor_id),
  INDEX reference_factor_trait (generation_id, kpn_trait_id),
  CONSTRAINT reference_factor_trait_fk FOREIGN KEY (generation_id, kpn_trait_id) REFERENCES kpn_traits(generation_id, kpn_trait_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- Collection-level metadata only; member gene sets live in cfde_gene_sets.
CREATE TABLE IF NOT EXISTS cfde_gene_set_collections (
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  collection_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  cfde_label VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  library VARCHAR(64) NOT NULL,
  n_sets INT UNSIGNED NOT NULL,
  payload JSON NOT NULL,
  PRIMARY KEY (generation_id, collection_id),
  UNIQUE KEY cfde_collection_label (generation_id, cfde_label),
  CONSTRAINT cfde_collection_generation_fk FOREIGN KEY (generation_id) REFERENCES reference_generations(generation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS cfde_gene_sets (
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_set_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  collection_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_set_name TEXT NOT NULL,
  library VARCHAR(64) NOT NULL,
  n_genes INT UNSIGNED NOT NULL,
  n_genes_in_eaggl_universe INT UNSIGNED NOT NULL,
  legacy_source_key TEXT NULL,
  metadata JSON NOT NULL,
  PRIMARY KEY (generation_id, gene_set_id),
  INDEX cfde_gene_set_collection (generation_id, collection_id),
  CONSTRAINT cfde_gene_set_collection_fk FOREIGN KEY (generation_id, collection_id) REFERENCES cfde_gene_set_collections(generation_id, collection_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- Per-trait projection of every CFDE gene set onto each factor, retained where the gene set
-- ranks <= 50 by joint or marginal loading. Loadings keep eaggl's exact %.4g strings too.
CREATE TABLE IF NOT EXISTS factor_gene_set_projections (
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  scope VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  factor_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  gene_set_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  joint_loading DOUBLE NOT NULL,
  marginal_loading DOUBLE NOT NULL,
  joint_loading_text VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  marginal_loading_text VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  joint_rank INT UNSIGNED NOT NULL,
  marginal_rank INT UNSIGNED NOT NULL,
  is_joint_top_factor TINYINT NOT NULL,
  PRIMARY KEY (generation_id, scope, factor_key, gene_set_id),
  INDEX projection_gene_set (generation_id, gene_set_id),
  INDEX projection_factor_joint (generation_id, scope, factor_key, joint_rank),
  CONSTRAINT projection_factor_fk FOREIGN KEY (generation_id, factor_key) REFERENCES reference_factors(generation_id, factor_key),
  CONSTRAINT projection_gene_set_fk FOREIGN KEY (generation_id, gene_set_id) REFERENCES cfde_gene_sets(generation_id, gene_set_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- An embedding space: vectors are comparable only within one space.
CREATE TABLE IF NOT EXISTS embedding_spaces (
  space_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  model VARCHAR(255) NOT NULL,
  model_revision VARCHAR(255) NOT NULL,
  provider VARCHAR(64) NOT NULL,
  service_url_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  dimensions INT UNSIGNED NOT NULL,
  metric VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  normalization VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  calibration JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- Gene-set and collection vectors of a generation (factor vectors stay in
-- eaggl_name_embeddings, DisMech context vectors in dismech_embedding_vectors).
-- vector is little-endian float32, dimensions from embedding_spaces.
CREATE TABLE IF NOT EXISTS reference_vectors (
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  space_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  input_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  input_text TEXT NOT NULL,
  vector MEDIUMBLOB NOT NULL,
  vector_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (generation_id, source_kind, source_id),
  CONSTRAINT reference_vector_generation_fk FOREIGN KEY (generation_id) REFERENCES reference_generations(generation_id),
  CONSTRAINT reference_vector_space_fk FOREIGN KEY (space_id) REFERENCES embedding_spaces(space_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- MySQL <-> Upstash id map for every uploaded vector, per environment and snapshot.
CREATE TABLE IF NOT EXISTS vector_bindings (
  environment VARCHAR(25) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  namespace VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  vector_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  snapshot_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_kind VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT NOT NULL,
  source_id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  vector_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (environment, namespace, vector_id),
  UNIQUE KEY vector_binding_source (environment, snapshot_id, source_kind, source_id_sha256),
  INDEX vector_binding_generation (generation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

-- Frozen snapshot of one factor as referenced by user work, captured before its generation
-- is purged. Never purged. archive_id = sha256(canonical([generation_id, source_id])).
CREATE TABLE IF NOT EXISTS archived_reference_factors (
  archive_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_id TEXT NOT NULL,
  source_id_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  model VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  factor_id VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  trait VARCHAR(255) NOT NULL,
  kpn_trait_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
  label TEXT NOT NULL,
  snapshot JSON NOT NULL,
  snapshot_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  captured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY archived_factor_source (generation_id, source_id_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
