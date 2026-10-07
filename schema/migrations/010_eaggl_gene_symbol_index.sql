-- Additive index for exact EAGGL symbol lookups (research get_gene_factors and resolve_gene).
-- Apply once through the normal migration procedure; skip it when SHOW INDEX FROM eaggl_genes already
-- lists eaggl_genes_symbol (MySQL 8.0 has no CREATE INDEX IF NOT EXISTS). No rows change.
-- symbol is TEXT utf8mb4_bin, so the index needs a prefix; InnoDB rechecks equality on the full value.
-- Run SET SESSION lock_wait_timeout=10 first, and never during an EAGGL import or a reference purge:
-- the brief exclusive metadata lock queues behind any in-flight eaggl_genes query.
ALTER TABLE eaggl_genes ADD INDEX eaggl_genes_symbol (import_id, symbol(64)), ALGORITHM=INPLACE, LOCK=NONE;
