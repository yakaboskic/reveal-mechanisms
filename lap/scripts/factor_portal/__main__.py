"""CLI: python -m factor_portal {build,export-audit,serve,html} (run with PYTHONPATH=lap/scripts)."""

import argparse
import logging
import os
import sqlite3
import sys

from projection_workflow import WorkflowError, write_tsv

from . import build as build_module
from . import queries
from .assets import render_page

LOG = logging.getLogger("factor_portal")
DEFAULT_TITLE = "EAGGL x CFDE projection audit"

FACTOR_AUDIT_COLUMNS = ["factor_id", "trait", "kpn_trait_id", "phenotype_name", "factor", "label", "n_nonzero",
                        "top_joint_gene_set_id", "top_joint_gene_set", "top_joint_loading", "top_marginal_gene_set_id",
                        "top_marginal_gene_set", "top_marginal_loading", "jm_top_overlap", "own_top_frac", "hub_frac",
                        "sig_overlap_frac", "median_fold_enrichment", "dominant_library", "dominant_library_frac",
                        "flags"]
GENE_SET_AUDIT_COLUMNS = ["gene_set_id", "gene_set_name", "collection_id", "cfde_label", "library", "n_genes",
                          "n_universe", "n_top_joint_factors", "n_top_joint_traits", "n_top_any_factors", "is_hub"]


def _cell(value):
    if value is None:
        return "NA"
    if isinstance(value, float):
        return "%.6g" % value
    return value


def cmd_build(args):
    build_module.build(args)


def cmd_export_audit(args):
    conn = queries.open_database(args.db)
    meta = queries.read_meta(conn)
    factors = queries.list_factors(conn)
    write_tsv(args.output_factor_audit_file, FACTOR_AUDIT_COLUMNS,
              [{c: _cell(f[c]) for c in FACTOR_AUDIT_COLUMNS} for f in factors])
    hub_min = meta["thresholds"]["hub_min_factors"]
    rows = []
    for r in conn.execute("SELECT %s FROM gene_sets ORDER BY n_top_joint_factors DESC, n_top_any_factors DESC, "
                          "gene_set_name, gene_set_id" % ", ".join(GENE_SET_AUDIT_COLUMNS[:-1])):
        row = {c: _cell(r[c]) for c in GENE_SET_AUDIT_COLUMNS[:-1]}
        row["is_hub"] = int(r["n_top_joint_factors"] >= hub_min)
        rows.append(row)
    write_tsv(args.output_gene_set_audit_file, GENE_SET_AUDIT_COLUMNS, rows)
    LOG.info("wrote %d factor rows (%d flagged) and %d gene-set rows", len(factors),
             sum(1 for f in factors if f["flags"]), len(rows))


def cmd_serve(args):
    from .server import serve
    serve(args.db, host=args.host, port=args.port, title=args.title, cors_origin=args.cors_origin)


def cmd_html(args):
    queries.open_database(args.db).close()  # the page is only useful against this database
    with open(args.output_html, "w", encoding="utf-8") as fh:
        fh.write(render_page(args.title, api_base=args.api_url))
    LOG.info("wrote %s for the API at %s; serve the database with: PYTHONPATH=%s python -m factor_portal serve "
             "--db %s --port <port>", args.output_html, args.api_url,
             os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.abspath(args.db))


def build_parser():
    parser = argparse.ArgumentParser(prog="factor_portal", description=__doc__)
    parser.add_argument("--log-file", default=None, help="Also write the log here")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="Build the portal SQLite from the LAP outputs")
    build_module.add_arguments(p)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("export-audit", help="Write the per-factor and per-gene-set audit columns as TSV")
    p.add_argument("--db", required=True)
    p.add_argument("--output-factor-audit-file", required=True)
    p.add_argument("--output-gene-set-audit-file", required=True)
    p.set_defaults(func=cmd_export_audit)

    p = sub.add_parser("serve", help="Serve the UI and the read-only JSON API")
    p.add_argument("--db", required=True)
    p.add_argument("--host", default="127.0.0.1", help="Use 0.0.0.0 only on a trusted network (no authentication)")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--title", default=DEFAULT_TITLE)
    p.add_argument("--cors-origin", default="*", help="Access-Control-Allow-Origin for static copies ('' disables)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("html", help="Write the UI as a static page that calls a running `serve`")
    p.add_argument("--db", required=True, help="The database the page is for (checked, not embedded)")
    p.add_argument("--api-url", required=True, help="Base URL of the `serve` instance, e.g. http://localhost:8766")
    p.add_argument("--title", default=DEFAULT_TITLE)
    p.add_argument("--output-html", required=True)
    p.set_defaults(func=cmd_html)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    handlers = [logging.StreamHandler(sys.stderr)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, mode="w", encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(asctime)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)
    try:
        args.func(args)
    except (WorkflowError, sqlite3.Error, RuntimeError, FileNotFoundError) as exc:
        LOG.error("%s failed: %s", args.command, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
