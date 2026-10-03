"""Which database may each NRB script touch — and why the answer is two answers.

The scratch-database rule (`local_ai_gateway_p4` only) was born of the Alembic
lineage split (docs/nrb-integration.md §9.10): the dev database was stamped at a
revision that existed only on the citations branch, so NRB work could not run
against it at all. §30 resolved that; every local database is now at one head.
The rule outlived its cause — and then started doing harm: a corpus built for
production has to be built where production's own department ids live, and p4
has no `nrb` department at all. `local_ai_gateway_build` is a clone of the
production snapshot made for exactly this, so its `nrb_files`, department and
user ids ARE production's, and a delta built there inserts without translating
a single id.

So the rule splits in two, and the split is the point:

* OPERATIONAL scripts (the pipeline, the corpus enqueue driver, the recovery
  cache) may run against p4 or the build clone — they do work.
* EVIDENCE scripts stay p4-only — their output is a measurement against frozen
  cohorts that live in p4 (the 6A benchmark, the 6B holdout, the P7 cohort), and
  the same numbers computed against another database would look like the
  recorded evidence without being it (the "holdout is SPENT EVIDENCE" rule).

Everything else — the real dev database, the production snapshot, anything
unnamed — is refused. Deny by default.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.nrb import dbguard

REPO = Path(__file__).resolve().parents[1]

# Unreachable on purpose: a guard that fires must fire BEFORE any connection,
# and one that admits must then fail to connect to nothing. Either way no real
# database is touched by this module.
_DEAD = "postgresql+asyncpg://gateway:x@127.0.0.1:1/"


# --------------------------------------------------------------------------- #
# The pure rule
# --------------------------------------------------------------------------- #


def test_the_build_clone_is_admitted_for_operational_work():
    assert dbguard.require(_DEAD + "local_ai_gateway_build",
                           dbguard.BUILD_DATABASES) == "local_ai_gateway_build"


def test_the_scratch_database_is_still_admitted_for_operational_work():
    """Widening must not narrow: every operational run so far happened in p4."""
    assert dbguard.require(_DEAD + "local_ai_gateway_p4",
                           dbguard.BUILD_DATABASES) == "local_ai_gateway_p4"


def test_the_real_dev_database_is_refused():
    """The thing the rule protects. `local_ai_gateway` carries 11k test
    departments and 26k users of debris; a corpus run there is noise in the
    one database nobody treats as disposable."""
    with pytest.raises(dbguard.RefusedDatabase):
        dbguard.require(_DEAD + "local_ai_gateway", dbguard.BUILD_DATABASES)


def test_the_production_snapshot_is_refused():
    """The build clone exists so the snapshot can stay a faithful record of
    production. Mutating the snapshot would destroy the evidence the whole
    incident investigation (docs/prod-incident-2026-09-20.md) was read from."""
    with pytest.raises(dbguard.RefusedDatabase):
        dbguard.require(_DEAD + "gw_prod_snapshot", dbguard.BUILD_DATABASES)


def test_evidence_stays_scratch_only():
    """A holdout measurement re-run against the build clone would produce
    numbers that read as the recorded evidence and are not."""
    with pytest.raises(dbguard.RefusedDatabase):
        dbguard.require(_DEAD + "local_ai_gateway_build",
                        dbguard.EVIDENCE_DATABASES)
    assert dbguard.require(_DEAD + "local_ai_gateway_p4",
                           dbguard.EVIDENCE_DATABASES) == "local_ai_gateway_p4"


@pytest.mark.parametrize("name", [
    "local_ai_gateway_build_old",
    "local_ai_gateway_p4x",
    "xlocal_ai_gateway_build",
    "LOCAL_AI_GATEWAY_BUILD",
])
def test_the_match_is_exact(name):
    """A name that merely CONTAINS an allowed one is a different database."""
    with pytest.raises(dbguard.RefusedDatabase):
        dbguard.require(_DEAD + name, dbguard.BUILD_DATABASES)


def test_a_query_string_does_not_change_the_database_name():
    assert dbguard.require(_DEAD + "local_ai_gateway_build?ssl=disable",
                           dbguard.BUILD_DATABASES) == "local_ai_gateway_build"


@pytest.mark.parametrize("url", ["", "postgresql+asyncpg://gateway:x@127.0.0.1:1/"])
def test_no_database_name_is_refused(url):
    with pytest.raises(dbguard.RefusedDatabase):
        dbguard.require(url, dbguard.BUILD_DATABASES)


def test_the_refusal_names_what_was_asked_for_and_what_is_allowed():
    """So the operator reading it can tell a typo from a wrong database."""
    with pytest.raises(dbguard.RefusedDatabase) as exc:
        dbguard.require(_DEAD + "local_ai_gateway", dbguard.BUILD_DATABASES)
    msg = str(exc.value)
    assert "'local_ai_gateway'" in msg
    assert "local_ai_gateway_build" in msg and "local_ai_gateway_p4" in msg


def test_the_two_allowlists_are_frozen():
    """A mutable set could be widened at import time by anything that imports it."""
    assert isinstance(dbguard.BUILD_DATABASES, frozenset)
    assert isinstance(dbguard.EVIDENCE_DATABASES, frozenset)
    assert dbguard.EVIDENCE_DATABASES <= dbguard.BUILD_DATABASES


# --------------------------------------------------------------------------- #
# The scripts actually use it — end to end, in a subprocess
# --------------------------------------------------------------------------- #

# The args are whatever gets each script past argparse to its guard. They
# matter more than they look: argparse's OWN error exit code is also 2, the
# same as the guard's, so a missing required flag (`--department`) exits 2
# without the guard ever running. That is why every refusal test below asserts
# on the "refusing to run" message and not on the exit code alone — measured,
# the first draft of this file passed a returncode check that way.
OPERATIONAL = {
    "nrb_pipeline.py": ["--status"],
    "nrb_rag_ingest_corpus.py": ["--department", "nrb", "--report"],
    "nrb_recovery_cache.py": ["--stats"],
    # --out is required; the guard fires (or admits) before anything is written.
    "nrb_prod_scope.py": ["--out", "/dev/null"],
    "nrb_native3_detect.py": ["--department", "nrb", "--out", "/dev/null"],
}


def _run(script: str, args: list[str], dbname: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": _DEAD + dbname}
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / script), *args],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("script,args", OPERATIONAL.items())
def test_an_operational_script_admits_the_build_clone(script, args):
    """Gets PAST the guard — and then fails to connect, because the port is
    dead. That is the proof the guard admitted it and touched nothing."""
    out = _run(script, args, "local_ai_gateway_build")
    assert "refusing to run" not in out.stderr, out.stderr
    assert "database: local_ai_gateway_build" in out.stdout, (out.stdout, out.stderr)


@pytest.mark.parametrize("script,args", OPERATIONAL.items())
def test_an_operational_script_refuses_the_dev_database_before_connecting(script, args):
    out = _run(script, args, "local_ai_gateway")
    assert out.returncode == 2, (out.returncode, out.stderr)
    assert "refusing to run" in out.stderr


EVIDENCE = [
    "nrb_build_lexicon.py", "nrb_holdout_evidence.py", "nrb_holdout_validate.py",
    "nrb_legacy_eval.py", "nrb_native2_compare.py", "nrb_p7_cohort.py",
    "nrb_rag_ingest.py", "nrb_supersession_exercise.py", "nrb_native3_cohort.py",
]


@pytest.mark.parametrize("script", EVIDENCE)
def test_an_evidence_script_never_names_the_build_clone(script):
    """The failure this guards is a blanket search-and-replace that widens every
    guard at once because the operational ones were widened. Each of these
    measures a frozen cohort living in p4; none may learn another database."""
    src = (REPO / "scripts" / script).read_text()
    assert "local_ai_gateway_p4" in src
    assert "local_ai_gateway_build" not in src


# --------------------------------------------------------------------------- #
# nrb_pipeline.py --dry-run without --run-now
# --------------------------------------------------------------------------- #
# Found 2026-09-24 building the production corpus: `--dry-run` is passed ONLY
# to `execute_run`, so without `--run-now` the CLI called `request_run` and
# inserted a REAL `queued` run carrying no dry-run marker at all — run 14 in
# local_ai_gateway_build, requested as a dry run. Any runner that picks it up
# executes it for real, and in production the compose `nrb-runner` service is
# always polling. A flag the user typed must never be silently dropped, and
# for a command whose whole job is "don't do it yet", dropping it inverts the
# request.

def test_pipeline_dry_run_without_run_now_refuses_before_touching_the_database():
    out = _run("nrb_pipeline.py",
               ["--department", "nrb", "--key", "https://x/a.pdf", "--dry-run"],
               "local_ai_gateway_build")
    assert out.returncode == 2, (out.returncode, out.stdout, out.stderr)
    assert "--dry-run" in out.stderr and "--run-now" in out.stderr, out.stderr
    # The dead port proves it: had it tried to queue a run, the error would be
    # a connection failure, not this refusal.
    assert "queued" not in out.stdout


def test_pipeline_dry_run_with_run_now_is_still_accepted():
    """The combination that DOES carry the flag must keep working: it gets past
    argument checking and the guard, then fails on the dead port."""
    out = _run("nrb_pipeline.py",
               ["--department", "nrb", "--key", "https://x/a.pdf",
                "--dry-run", "--run-now"],
               "local_ai_gateway_build")
    assert "only takes effect with --run-now" not in out.stderr
    assert "database: local_ai_gateway_build" in out.stdout, (out.stdout, out.stderr)
