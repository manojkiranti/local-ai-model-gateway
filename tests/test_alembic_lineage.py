"""The migration graph must stay a single line.

Citations forked from `c33c0fd56028` at the same time as the NRB chain and was
deferred (§27 of docs/nrb-integration.md). Un-deferring it means turning the
sibling into a DESCENDANT of the NRB head — never a merge revision, which would
make the graph a diamond that every future branch has to reason about.

These assertions are cheap and they are the whole guard: a second head appearing
off any point other than the current head is the regression §27.5 names.

They are deliberately head-AGNOSTIC. Citations was the head when this file was
written, but pinning that would mean every later migration had to edit the guard,
and a guard you routinely edit stops guarding. What must stay true is the SHAPE:
one head, citations still resting directly on the NRB head, and both chains
ancestors of whatever the head now is.
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


def test_citations_still_sits_directly_on_the_nrb_chain():
    """The §27.4 rebase, asserted independently of what the head is today."""
    scripts = _scripts()
    assert scripts.get_revision(CITATIONS).down_revision == NRB_HEAD


def test_every_earlier_chain_is_an_ancestor_of_the_head():
    """A database upgraded to head gets the NRB schema AND the sources column.

    Generalised from "ancestor of citations": once a revision lands on top of
    citations, the question that matters is whether the chains reach the HEAD.
    """
    scripts = _scripts()
    (head,) = scripts.get_heads()
    chain = {rev.revision for rev in scripts.iterate_revisions(head, "base")}
    assert CITATIONS in chain
    assert NRB_HEAD in chain
    assert BASELINE in chain
