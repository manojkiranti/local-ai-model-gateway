"""Copy ONE department's corpus (and the NRB catalog) into a fresh release database.

Production gets a brand-new database (docs/prod-incident-2026-09-20.md §9.1), not
a clone of the build database: the build clone still carries the old production
snapshot's users, chats and grants, which must not ship. So the release database
is created empty, migrated to head, and filled from the build database by this
script with exactly what it needs:

    departments          the one department (same id and code)
    nrb_sync_runs,
    nrb_fetch_runs       the parents nrb_sources / nrb_files point at
    nrb_sources,
    nrb_files,
    nrb_source_files     the catalog, so production's runner can keep syncing
    nrb_extractions,
    nrb_recoveries,
    nrb_recovery_units   the recovery cache, so a re-ingest there is warm
    documents,
    document_chunks      the department's corpus, embeddings included

Never copied: users, grants, chats, files, API keys, ingest jobs, pipeline-run
history. Users are created through the app's own register route afterwards, so
their passwords are hashed by the app and pass through nothing else.

Rows move through `psql \\copy` pipes, text format, one table at a time, so a
`vector` needs no client-side codec and a STORED generated column (`tsv`) is
left out of the column list and recomputed by the target. The copy is not one
transaction: if it stops halfway, re-run with `--replace`. Safety rules: the
target must not be the source or the dev database, must be at the SAME Alembic
revision as the source, and must be empty unless `--replace` is given. Every
table is then counted on both sides, and a mismatch exits non-zero.

Usage (credentials come from .env's DATABASE_URL; only the database name changes):
    .venv/bin/python scripts/build_release_db.py --target ai_gateway
    .venv/bin/python scripts/build_release_db.py --target ai_gateway --replace
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.config import get_settings  # noqa: E402

NEVER_TARGET = {"local_ai_gateway", "local_ai_gateway_build", "local_ai_gateway_p4",
                "gw_prod_snapshot"}

# (table, WHERE clause) in foreign-key order: every parent before its children.
# `:dept` is replaced by the department id.
PLAN: list[tuple[str, str]] = [
    ("departments", "id = :dept"),
    ("nrb_sync_runs", "true"),
    ("nrb_fetch_runs", "true"),
    ("nrb_sources", "true"),
    ("nrb_files", "true"),
    ("nrb_source_files", "true"),
    ("nrb_extractions", "true"),
    ("nrb_recoveries", "true"),
    ("nrb_recovery_units", "true"),
    ("documents", "department_id = :dept"),
    ("document_chunks", "department_id = :dept"),
]


class Pg:
    """psql against one database, with the password in the environment only."""

    def __init__(self, url: str, dbname: str) -> None:
        parts = urlsplit(url.replace("+asyncpg", ""))
        self.dbname = dbname
        self._args = ["-h", parts.hostname or "127.0.0.1", "-p", str(parts.port or 5432),
                      "-U", parts.username or "", "-d", dbname, "-v", "ON_ERROR_STOP=1",
                      "-X", "-q"]
        self._env = {**os.environ, "PGPASSWORD": parts.password or ""}

    def argv(self, *extra: str) -> list[str]:
        return ["psql", *self._args, *extra]

    def scalar(self, sql: str) -> str:
        out = subprocess.run(self.argv("-tA", "-c", sql), env=self._env,
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise SystemExit(f"[{self.dbname}] query failed: {out.stderr.strip()}")
        return out.stdout.strip()

    def env(self) -> dict[str, str]:
        return self._env


def copyable_columns(pg: Pg, table: str) -> list[str]:
    """Ordinary columns in table order; generated columns are the target's to compute."""
    rows = pg.scalar(
        "SELECT attname FROM pg_attribute WHERE attrelid = "
        f"'public.{table}'::regclass AND attnum > 0 AND NOT attisdropped "
        "AND attgenerated = '' ORDER BY attnum"
    )
    return rows.split("\n") if rows else []


def copy_table(src: Pg, dst: Pg, table: str, where: str) -> None:
    cols = copyable_columns(dst, table)
    if cols != copyable_columns(src, table):
        raise SystemExit(f"{table}: columns differ between source and target")
    col_list = ", ".join(f'"{c}"' for c in cols)
    out_cmd = src.argv("-c", f"\\copy (SELECT {col_list} FROM {table} WHERE {where}) TO STDOUT")
    in_cmd = dst.argv("-c", f"\\copy {table} ({col_list}) FROM STDIN")
    producer = subprocess.Popen(out_cmd, env=src.env(), stdout=subprocess.PIPE)
    consumer = subprocess.run(in_cmd, env=dst.env(), stdin=producer.stdout,
                              capture_output=True, text=True)
    producer.stdout.close()
    if producer.wait() != 0 or consumer.returncode != 0:
        raise SystemExit(f"{table}: copy failed: {consumer.stderr.strip()}")


def reset_sequence(dst: Pg, table: str) -> None:
    # Only tables with an `id` column have one; a link table like
    # nrb_source_files is keyed on its pair and pg_get_serial_sequence would raise.
    seq = dst.scalar(
        f"SELECT pg_get_serial_sequence('public.{table}', 'id') FROM pg_attribute "
        f"WHERE attrelid = 'public.{table}'::regclass AND attname = 'id' AND NOT attisdropped"
    )
    if seq:
        dst.scalar(f"SELECT setval('{seq}', GREATEST((SELECT max(id) FROM {table}), 1))")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", default="local_ai_gateway_build")
    ap.add_argument("--target", required=True)
    ap.add_argument("--department", default="nrb", help="department CODE to carry over")
    ap.add_argument("--replace", action="store_true",
                    help="empty the copied tables in the target first")
    args = ap.parse_args()

    if args.target in NEVER_TARGET or args.target == args.source:
        raise SystemExit(f"refusing to write into {args.target!r}")
    url = get_settings().database_url
    src, dst = Pg(url, args.source), Pg(url, args.target)

    head_src = src.scalar("SELECT version_num FROM alembic_version")
    head_dst = dst.scalar("SELECT version_num FROM alembic_version")
    if head_src != head_dst:
        raise SystemExit(f"schema revisions differ: source {head_src}, target {head_dst}")
    dept = src.scalar(f"SELECT id FROM departments WHERE code = '{args.department}'")
    if not dept:
        raise SystemExit(f"department {args.department!r} not in {args.source}")

    # The department row is kept across rebuilds: once users exist, their grants
    # and chats reference it, and truncating it would need CASCADE -- which would
    # silently delete them. A rebuild replaces the NRB tables and this
    # department's documents only.
    have_dept = dst.scalar(
        f"SELECT count(*) FROM departments WHERE id = {dept} AND code = '{args.department}'"
    ) == "1"
    nrb_tables = [t for t, _ in PLAN if t.startswith("nrb_")]
    dept_docs = f"department_id = {dept}"
    occupied = [t for t in nrb_tables if dst.scalar(f"SELECT count(*) FROM {t}") != "0"]
    if dst.scalar(f"SELECT count(*) FROM documents WHERE {dept_docs}") != "0":
        occupied.append(f"documents of {args.department}")
    if occupied and not args.replace:
        raise SystemExit(f"target is not empty ({', '.join(occupied)}); use --replace")
    if args.replace:
        dst.scalar(f"DELETE FROM document_chunks WHERE {dept_docs}")
        dst.scalar("DELETE FROM ingest_jobs WHERE document_id IN "
                   f"(SELECT id FROM documents WHERE {dept_docs})")
        dst.scalar(f"DELETE FROM documents WHERE {dept_docs}")
        dst.scalar(f"TRUNCATE {', '.join(reversed(nrb_tables))}")

    print(f"{args.source} -> {args.target}  (schema {head_src}, department "
          f"{args.department}#{dept})")
    for table, where in PLAN:
        where = where.replace(":dept", dept)
        if table == "departments" and have_dept:
            print(f"  {table:20} {'1':>7} rows  kept")
            continue
        copy_table(src, dst, table, where)
        reset_sequence(dst, table)
        n_src = src.scalar(f"SELECT count(*) FROM {table} WHERE {where}")
        n_dst = dst.scalar(f"SELECT count(*) FROM {table} WHERE {where}")
        mark = "ok" if n_src == n_dst else "MISMATCH"
        print(f"  {table:20} {n_dst:>7} rows  {mark}")
        if n_src != n_dst:
            raise SystemExit(f"{table}: source {n_src} vs target {n_dst}")

    route_split = ("SELECT string_agg(route || ':' || n, ' ' ORDER BY route) FROM "
                   "(SELECT coalesce(metadata->>'route', '-') AS route, count(*) AS n "
                   "FROM document_chunks WHERE department_id = {d} GROUP BY 1) s")
    checks = {
        "route split": (src.scalar(route_split.format(d=dept)),
                        dst.scalar(route_split.format(d=dept))),
        "chunks without an embedding": ("0", dst.scalar(
            f"SELECT count(*) FROM document_chunks WHERE {dept_docs} AND embedding IS NULL")),
        "ready documents without chunks": ("0", dst.scalar(
            f"SELECT count(*) FROM documents d WHERE d.{dept_docs} AND d.status = 'ready' "
            "AND NOT EXISTS (SELECT 1 FROM document_chunks c WHERE c.document_id = d.id)")),
    }
    failed = False
    for name, (want, got) in checks.items():
        ok = want == got
        failed |= not ok
        print(f"  {name:32} {'ok' if ok else 'MISMATCH'}  {got.replace(chr(10), ' ')}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
