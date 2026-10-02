"""Visual order → logical order. Pure; every case is a string written out.

The probe that preceded this plan (2026-10-02) got the rakar wrong once and the
round-trip gate refused it — these cases are what the reorderer must get right
before the gate is even reached.
"""

from __future__ import annotations

import pytest

from app.nrb.reorder import PREBASE, REPH, reorder

P, R = PREBASE, REPH


@pytest.mark.parametrize("visual, logical", [
    (P + "त", "ति"),                       # the pre-base sign moves after its consonant
    (P + "स्त", "स्ति"),                    # ...after the WHOLE cluster
    (P + "क्ष", "क्षि"),                    # a half-form conjunct is one cluster
    ("प्र" + P + "क", "प्रकि"),             # rakar is not a reph and stays put
    (P + "प्र", "प्रि"),                    # ि before a rakar cluster
    ("क" + "ो" + R, "र्को"),               # reph after a matra moves to the cluster start
    ("क" + R + "ी", "र्की"),               # reph before a post-base matra
    (P + "त" + R, "र्ति"),                 # both: र् first, then the whole cluster, then ि
    ("न" + "े" + R, "र्ने"),               # a matra+reph ligature's token
    ("का" + P + "म", "कामि"),
    ("गदा", "गदा"),                        # no marker, unchanged
    (P + "क़", "क़ि"),                      # a nukta consonant takes the sign after it
])
def test_reorder_moves_each_marker_to_where_it_is_typed(visual, logical):
    out = reorder(visual)
    assert (out.text, out.orphans) == (logical, 0)


@pytest.mark.parametrize("visual", [
    P,                 # nothing to attach to
    P + " ",           # a space is not a cluster
    "क" + P,           # pre-base at the end of a run
    R + "क",           # reph before any cluster
    " " + R,           # reph after a space
])
def test_an_unattachable_marker_is_an_orphan_never_a_guess(visual):
    out = reorder(visual)
    assert out.orphans == 1
    assert P not in out.text and R not in out.text


def test_two_syllables_with_markers_reorder_independently():
    # कीर्ति, visually: क ी ि(pre-base) त reph
    assert reorder("क" + "ी" + P + "त" + R).text == "कीर्ति"


def test_output_is_nfc():
    import unicodedata

    out = reorder(P + "क़")
    assert out.text == unicodedata.normalize("NFC", out.text)
