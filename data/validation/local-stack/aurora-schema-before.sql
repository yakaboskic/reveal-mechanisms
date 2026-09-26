CREATE TABLE `cfde_gene_set_aliases` (
  `import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `node_id_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `model` varchar(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `source_key` text CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `node_id` text CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `dapper_id` varchar(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `provenance` json NOT NULL,
  PRIMARY KEY (`import_id`,`node_id_sha256`),
  KEY `lookup_node` (`model`,`node_id_sha256`),
  KEY `lookup_dapper` (`dapper_id`),
  CONSTRAINT `cfde_gene_set_aliases_ibfk_1` FOREIGN KEY (`import_id`) REFERENCES `gene_set_imports` (`import_id`),
  CONSTRAINT `cfde_gene_set_aliases_ibfk_2` FOREIGN KEY (`dapper_id`) REFERENCES `dapper_objects` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `dapper_objects` (
  `id` varchar(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `class_name` varchar(64) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
  `identity_profile` varchar(32) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
  `payload_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `payload` json NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `eaggl_cfde_factor_links` (
  `run_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `factor_index` int unsigned NOT NULL,
  `eaggl_import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `cfde_node_id` text COLLATE utf8mb4_bin NOT NULL,
  `cfde_node_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `cfde_trait` text COLLATE utf8mb4_bin NOT NULL,
  `cfde_factor_number` int unsigned NOT NULL,
  `cfde_trait_group` varchar(64) COLLATE utf8mb4_bin NOT NULL,
  `payload` json NOT NULL,
  PRIMARY KEY (`run_id`,`factor_index`),
  UNIQUE KEY `cfde_factor_link_target` (`run_id`,`cfde_node_sha256`),
  KEY `cfde_factor_link_run` (`run_id`,`eaggl_import_id`),
  KEY `cfde_factor_link_source` (`eaggl_import_id`,`factor_index`),
  CONSTRAINT `factor_link_run_fk` FOREIGN KEY (`run_id`, `eaggl_import_id`) REFERENCES `eaggl_cfde_link_runs` (`run_id`, `eaggl_import_id`),
  CONSTRAINT `factor_link_source_fk` FOREIGN KEY (`eaggl_import_id`, `factor_index`) REFERENCES `eaggl_factors` (`import_id`, `factor_index`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `eaggl_cfde_gene_set_links` (
  `run_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `factor_index` int unsigned NOT NULL,
  `gene_set_rank` int unsigned NOT NULL,
  `gene_set_import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `source_key` text COLLATE utf8mb4_bin NOT NULL,
  `node_id` text COLLATE utf8mb4_bin NOT NULL,
  `node_id_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `resolved_alias_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL,
  PRIMARY KEY (`run_id`,`factor_index`,`gene_set_rank`),
  KEY `cfde_gene_set_link_run` (`run_id`,`gene_set_import_id`),
  KEY `cfde_gene_set_link_alias` (`gene_set_import_id`,`resolved_alias_sha256`),
  CONSTRAINT `gene_set_link_alias_fk` FOREIGN KEY (`gene_set_import_id`, `resolved_alias_sha256`) REFERENCES `cfde_gene_set_aliases` (`import_id`, `node_id_sha256`),
  CONSTRAINT `gene_set_link_factor_fk` FOREIGN KEY (`run_id`, `factor_index`) REFERENCES `eaggl_cfde_factor_links` (`run_id`, `factor_index`),
  CONSTRAINT `gene_set_link_run_fk` FOREIGN KEY (`run_id`, `gene_set_import_id`) REFERENCES `eaggl_cfde_link_runs` (`run_id`, `gene_set_import_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `eaggl_cfde_link_runs` (
  `run_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `eaggl_import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `gene_set_import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `model` varchar(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `match_method` varchar(64) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
  `status` varchar(16) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
  `manifest` json NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`run_id`),
  UNIQUE KEY `link_run_eaggl` (`run_id`,`eaggl_import_id`),
  UNIQUE KEY `link_run_gene_sets` (`run_id`,`gene_set_import_id`),
  KEY `link_run_source` (`eaggl_import_id`),
  KEY `link_run_gene_set_import` (`gene_set_import_id`),
  CONSTRAINT `link_run_gene_sets_fk` FOREIGN KEY (`gene_set_import_id`) REFERENCES `gene_set_imports` (`import_id`),
  CONSTRAINT `link_run_source_fk` FOREIGN KEY (`eaggl_import_id`) REFERENCES `eaggl_imports` (`import_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE `eaggl_embedding_runs` (
  `run_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `config` json NOT NULL,
  `dimensions` int unsigned NOT NULL,
  `expected_rows` int unsigned NOT NULL,
  `loaded_rows` int unsigned NOT NULL DEFAULT '0',
  `status` varchar(16) NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`run_id`),
  KEY `import_id` (`import_id`),
  CONSTRAINT `eaggl_embedding_runs_ibfk_1` FOREIGN KEY (`import_id`) REFERENCES `eaggl_imports` (`import_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `eaggl_factors` (
  `import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `factor_index` int unsigned NOT NULL,
  `factor_id` text CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `factor_id_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `trait` text CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `label` text NOT NULL,
  `input_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `metadata` json NOT NULL,
  PRIMARY KEY (`import_id`,`factor_index`),
  UNIQUE KEY `factor_lookup` (`import_id`,`factor_id_sha256`),
  CONSTRAINT `eaggl_factors_ibfk_1` FOREIGN KEY (`import_id`) REFERENCES `eaggl_imports` (`import_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `eaggl_gene_loadings` (
  `import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `factor_index` int unsigned NOT NULL,
  `gene_index` int unsigned NOT NULL,
  `loading` double NOT NULL,
  PRIMARY KEY (`import_id`,`factor_index`,`gene_index`),
  KEY `import_id` (`import_id`,`gene_index`),
  CONSTRAINT `eaggl_gene_loadings_ibfk_1` FOREIGN KEY (`import_id`, `factor_index`) REFERENCES `eaggl_factors` (`import_id`, `factor_index`),
  CONSTRAINT `eaggl_gene_loadings_ibfk_2` FOREIGN KEY (`import_id`, `gene_index`) REFERENCES `eaggl_genes` (`import_id`, `gene_index`),
  CONSTRAINT `eaggl_gene_loadings_chk_1` CHECK (((`loading` > 0) and (`loading` <= 1)))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `eaggl_genes` (
  `import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `gene_index` int unsigned NOT NULL,
  `symbol` text CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  PRIMARY KEY (`import_id`,`gene_index`),
  CONSTRAINT `eaggl_genes_ibfk_1` FOREIGN KEY (`import_id`) REFERENCES `eaggl_imports` (`import_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `eaggl_graph_edges` (
  `import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `parent_index` int unsigned NOT NULL,
  `child_index` int unsigned NOT NULL,
  PRIMARY KEY (`import_id`,`parent_index`,`child_index`),
  KEY `import_id` (`import_id`,`child_index`),
  CONSTRAINT `eaggl_graph_edges_ibfk_1` FOREIGN KEY (`import_id`, `parent_index`) REFERENCES `eaggl_graph_nodes` (`import_id`, `node_index`),
  CONSTRAINT `eaggl_graph_edges_ibfk_2` FOREIGN KEY (`import_id`, `child_index`) REFERENCES `eaggl_graph_nodes` (`import_id`, `node_index`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `eaggl_graph_nodes` (
  `import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `node_index` int unsigned NOT NULL,
  `node_id` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `factor_index` int unsigned DEFAULT NULL,
  `payload` json NOT NULL,
  PRIMARY KEY (`import_id`,`node_index`),
  UNIQUE KEY `node_lookup` (`import_id`,`node_id`),
  KEY `import_id` (`import_id`,`factor_index`),
  CONSTRAINT `eaggl_graph_nodes_ibfk_1` FOREIGN KEY (`import_id`) REFERENCES `eaggl_imports` (`import_id`),
  CONSTRAINT `eaggl_graph_nodes_ibfk_2` FOREIGN KEY (`import_id`, `factor_index`) REFERENCES `eaggl_factors` (`import_id`, `factor_index`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `eaggl_imports` (
  `import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `source_namespace` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `source_version` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `status` varchar(16) NOT NULL,
  `manifest` json NOT NULL,
  `progress` json NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`import_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `eaggl_name_embeddings` (
  `run_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `input_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `input_text` text NOT NULL,
  `vector` mediumblob NOT NULL,
  `vector_sha256` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  PRIMARY KEY (`run_id`,`input_sha256`),
  CONSTRAINT `eaggl_name_embeddings_ibfk_1` FOREIGN KEY (`run_id`) REFERENCES `eaggl_embedding_runs` (`run_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `gene_set_imports` (
  `import_id` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `model` varchar(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `status` varchar(16) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
  `loaded_rows` int unsigned NOT NULL DEFAULT '0',
  `expected_rows` int unsigned NOT NULL,
  `manifest` json NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`import_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
