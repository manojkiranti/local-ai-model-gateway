"""Give EXISTING accounts the default departments (`DEFAULT_DEPARTMENTS`).

New accounts get them at creation (`app/auth/router._grant_default_departments`).
This is for accounts created before a default existed — e.g. the bank's first
logins on the brand-new database of 2026-09-29, which shipped with no users, so
every first login became a member with no grant and nobody saw the NRB tab.

Same function as account creation (`rag_repo.grant_default_departments`):
insert-if-absent, `viewer`, `granted_by` NULL, active users and active
departments only. An existing grant is never changed, so it is safe to re-run.

Usage (DATABASE_URL from .env, or override it for another database):
    .venv/bin/python scripts/grant_default_departments.py            # dry run
    .venv/bin/python scripts/grant_default_departments.py --apply
    ... --departments nrb,it                                          # override the setting
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.rag import repository as rag_repo  # noqa: E402

MISSING = text(
    "SELECT d.code, count(*) FROM users u CROSS JOIN departments d "
    "WHERE d.code = ANY(:codes) AND d.is_active AND u.is_active AND NOT EXISTS "
    "(SELECT 1 FROM user_departments ud WHERE ud.user_id = u.id AND ud.department_id = d.id) "
    "GROUP BY d.code ORDER BY d.code"
)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the grants (default: dry run)")
    ap.add_argument("--departments", default=None,
                    help="comma-separated codes; default: the DEFAULT_DEPARTMENTS setting")
    args = ap.parse_args()

    settings = get_settings()
    codes = ([c.strip().lower() for c in args.departments.split(",") if c.strip()]
             if args.departments is not None else settings.default_department_codes)
    database = settings.database_url.rsplit("/", 1)[-1].split("?")[0]
    print(f"database: {database}   departments: {', '.join(codes) or '(none)'}")
    if not codes:
        print("nothing to grant: no default departments configured")
        return 0

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as session:
            known = (await session.execute(
                text("SELECT code FROM departments WHERE code = ANY(:c) AND is_active"),
                {"c": codes})).scalars().all()
            for code in codes:
                if code not in known:
                    print(f"  {code}: no ACTIVE department with this code — skipped")
            if not known:
                print("nothing to grant: none of these departments exists and is active")
                return 0
            active = (await session.execute(
                text("SELECT count(*) FROM users WHERE is_active"))).scalar_one()
            print(f"  {active} active account(s)")
            missing = (await session.execute(MISSING, {"codes": codes})).all()
            for code, n in missing:
                print(f"  {code}: {n} active account(s) without it")
            if not missing:
                print("  nothing missing" if active else "  no accounts yet")
            if not args.apply:
                print("(dry run — nothing written; re-run with --apply)")
                return 0
            added = await rag_repo.grant_default_departments(session, codes=codes)
            await session.commit()
            print(f"granted {added} viewer grant(s)")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
