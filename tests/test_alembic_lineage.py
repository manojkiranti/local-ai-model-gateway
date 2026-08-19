"""The migration graph must stay a single line.

Citations forked from `c33c0fd56028` at the same time as the NRB chain and was
deferred (§27 of docs/nrb-integration.md). Un-deferring it means turning the
sibling into a DESCENDANT of the NRB head — never a merge revision, which would
make the graph a diamond that every future branch has to reason about.

These assertions are cheap and they are the whole guard: a second head appearing
off any point other than the current head is the regression §27.5 names.
"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
NRB_HEAD = "f4c1a90b7d62"
CITATIONS = "d4a91f2c7b3e"
BASELINE = "c33c0fd56028"


def _scripts() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_there_is_exactly_one_head():
    assert len(_scripts().get_heads()) == 1


def test_citations_is_the_head_and_sits_on_the_nrb_chain():
    scripts = _scripts()
    assert list(scripts.get_heads()) == [CITATIONS]
    assert scripts.get_revision(CITATIONS).down_revision == NRB_HEAD


def test_every_nrb_revision_is_an_ancestor_of_citations():
    """A database upgraded to head gets the NRB schema AND the sources column."""
    scripts = _scripts()
    chain = {rev.revision for rev in scripts.iterate_revisions(CITATIONS, "base")}
    assert NRB_HEAD in chain
    assert BASELINE in chain
