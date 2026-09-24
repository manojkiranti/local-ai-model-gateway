"""Which database an NRB script may touch. Pure: a URL in, a name or a refusal out.

The scratch-database rule began as a workaround, not a principle. The dev
database was stamped at a revision that existed only on the citations branch
(docs/nrb-integration.md §9.10), so NRB work *could not* run against it, and
`local_ai_gateway_p4` was created to hold it. §30 resolved the lineage and
every local database now sits at one head — but the rule survived as eleven
copies of the same name check, and it began to push work into the wrong place:
a corpus built for production must be built where production's department ids
live, and p4 has no `nrb` department at all.

`local_ai_gateway_build` is a clone of the production snapshot made for that
job. Its `nrb_files`, department and user ids ARE production's, so a delta built
there inserts without translating a single id — whereas building in p4 would
mean remapping `department_id` on every document and every chunk, under a
composite foreign key that makes a half-done remap easy to commit.

Two allowlists, because the scripts do two different things:

``BUILD_DATABASES``
    Operational work — the pipeline, the corpus enqueue driver, the recovery
    cache. These DO things, and the build clone is a place to do them.
``EVIDENCE_DATABASES``
    Measurement against the frozen cohorts that live in p4 (the 6A benchmark,
    the 6B holdout, the P7 cohort). Re-running one of those against another
    database yields numbers that read as the recorded evidence and are not
    (CLAUDE.md, "the Phase 6B holdout is SPENT EVIDENCE"). They stay p4-only,
    and `tests/test_nrb_dbguard.py` fails if one of them learns another name.

Everything else is refused: the real dev database, the production snapshot
(which must stay a faithful record — the build clone exists so it can), and
anything not written here. Deny by default, exact match, one place to widen.

What this is NOT: a safety boundary for the stages. `scripts/nrb_{sync,fetch,
extract}.py`, the worker and the runner carry no guard and never did. This
stops the entry points an operator actually types from being aimed at the
wrong database; anything subtler belongs in the database's own permissions.
"""

from __future__ import annotations

SCRATCH = "local_ai_gateway_p4"
BUILD = "local_ai_gateway_build"

EVIDENCE_DATABASES: frozenset[str] = frozenset({SCRATCH})
BUILD_DATABASES: frozenset[str] = frozenset({SCRATCH, BUILD})


class RefusedDatabase(Exception):
    """The URL names a database this script may not touch."""

    def __init__(self, name: str, allowed: frozenset[str]) -> None:
        self.name = name
        self.allowed = allowed
        wanted = ", ".join(repr(a) for a in sorted(allowed))
        super().__init__(
            f"DATABASE_URL resolves to database {name!r}, but this script runs "
            f"only against {wanted}."
        )


def database_name(url: str) -> str:
    """The database a SQLAlchemy URL names: its last path segment, query stripped.

    Deliberately the same parse the eleven inline guards used
    (`url.rsplit("/", 1)[-1].split("?")[0]`), so switching a script to this
    module cannot change which database it thinks it was handed.
    """
    return (url or "").rsplit("/", 1)[-1].split("?")[0]


def require(url: str, allowed: frozenset[str]) -> str:
    """Return the database name if ``allowed`` admits it; raise otherwise.

    An EXACT match: `local_ai_gateway_build_old` is a different database, and so
    is `LOCAL_AI_GATEWAY_BUILD` — Postgres folds unquoted identifiers, but a URL
    names the database verbatim, so a case variant is a typo, not a synonym.
    """
    name = database_name(url)
    if not name or name not in allowed:
        raise RefusedDatabase(name, allowed)
    return name
