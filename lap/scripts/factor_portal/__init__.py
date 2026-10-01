"""Audit portal for the CFDE -> EAGGL supplied-factor projection (../../config/cfde_projection.cfg).

Modelled on `python -m pigean.portal` (packages/pigean, branch feature/results-portal): `build` writes one
SQLite file from the LAP outputs, `serve` hosts it behind a read-only JSON API plus a single-page UI, and
`html` writes the same UI as a static page that calls a `serve` instance at a fixed URL. `export-audit`
flattens the per-factor and per-gene-set audit columns to TSV.

The portal lets a reviewer search traits, factors, gene sets and genes, and for any factor see its EAGGL
gene loadings next to the CFDE gene sets projected onto it (joint and marginal), with the genes that drive
each gene set's score. The marginal loading is clip(x'w / w'w, 0, 1) for a binary membership vector x,
so each member gene g contributes exactly w_g / w'w; `build` recomputes every stored marginal loading
this way and refuses to write the database if one disagrees beyond eaggl's %.4g rounding.

Standard library only; runs on Python 3.9 (the pigean venv the LAP helpers use).
"""

SCHEMA_VERSION = 1
