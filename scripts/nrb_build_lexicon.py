#!/usr/bin/env python
"""Build the frozen English/Nepali vocabulary the Phase 6B conversion guards use.

    DATABASE_URL=postgresql+asyncpg://…/local_ai_gateway_p4 \
        .venv/bin/python scripts/nrb_build_lexicon.py \
            --out docs/nrb/phase6b-lexicon.json

READ-ONLY. It selects `extracted`/`clean` blobs, re-parses them from disk and
counts words. It writes exactly one file: the lexicon named by `--out`. No
database row is touched, no blob is modified, no network request is made.

WHY THE CORPUS AND NOT A DICTIONARY
    Reproducibility (a system word list differs per machine), domain coverage
    (`crore`, `rastra`, `bittiya`, `परिपत्र`, `निर्देशन`), and no third-party
    licence — which matters in a phase whose converter is already GPL-3.

WHY ONLY `clean` BLOBS
    The vocabulary must not be fitted on the population it will judge. The
    evaluation cohort is drawn from `legacy_font_suspected`; this draws only from
    `extracted`/`clean`, so the two are disjoint by construction — the same
    discipline §11.9 demands of the legacy threshold.

WHY SPREADSHEETS ARE EXCLUDED FROM THE ENGLISH SOURCE
    Phase 6A's false negative: `quality.classify` judges a workbook structurally
    and returns before any linguistic rule, so a Preeti-encoded spreadsheet is
    classified `clean`. Letting one into the English source would teach the guard
    that `kfn` and `a}+s` are English words and disable it precisely where it
    matters. PDFs only, and only those with no Devanagari and a near-zero legacy
    ratio.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.nrb import legacy_eval, lexicon as lexicon_mod  # noqa: E402
from app.nrb.extraction import EXTRACTOR_VERSION  # noqa: E402

# An English source document is a PDF the native-1 classifier called `clean`,
# holding no Devanagari and enough text to have a vocabulary.
#
# The legacy bound is the classifier's OWN 0.20: below it native-1 asserts the
# document is not glyph-mapped, and this script has no business second-guessing
# that with a private threshold. It also matters for coverage — at 0.05 only 17
# documents qualified and the lexicon missed everyday words like `turnover` and
# `outstanding`, which let two English table headings through the conversion
# guard. The population is `clean` PDFs, so the risk being managed is a Preeti
# document sneaking in, and Preeti documents are `suspicious` by definition.
ENGLISH_MAX_DEVANAGARI = 0.0
ENGLISH_MAX_LEGACY = 0.20
ENGLISH_MIN_TOKENS = 200

# Over ~30 English documents, 2 is enough to reject one file's private artifacts
# while keeping ordinary vocabulary. The default (3) cost real words.
ENGLISH_MIN_DOCUMENT_FREQUENCY = 2

# A Nepali source document must be substantially real Devanagari.
NEPALI_MIN_DEVANAGARI = 0.30


async def build(out_path: Path, *, extractor_version: str, verbose: bool) -> int:
    from app.db.session import SessionLocal

    async with SessionLocal() as session:
        refs = await legacy_eval.load_blob_refs(
            session, extractor_version=extractor_version,
            statuses=("extracted",), reasons=("clean",),
        )

    english_refs = [
        r for r in refs
        if r.family == "pdf"
        and r.devanagari_ratio <= ENGLISH_MAX_DEVANAGARI
        and r.legacy_line_ratio <= ENGLISH_MAX_LEGACY
        and int(r.metrics.get("token_count") or 0) >= ENGLISH_MIN_TOKENS
    ]
    nepali_refs = [r for r in refs if r.devanagari_ratio >= NEPALI_MIN_DEVANAGARI]

    print(f"english source blobs: {len(english_refs)}")
    print(f"nepali  source blobs: {len(nepali_refs)}")
    if not english_refs or not nepali_refs:
        print("ERROR: no source documents — is DATABASE_URL the scratch DB?",
              file=sys.stderr)
        return 2

    def texts(refs_):
        out = []
        for ref in refs_:
            result = legacy_eval.read_blob_text(ref)
            if result.error:
                if verbose:
                    print(f"  skip {ref.short_sha}: {result.error}")
                continue
            out.append(result.text)
        return out

    english_texts = texts(english_refs)
    nepali_texts = texts(nepali_refs)

    # A word must appear in several DOCUMENTS. The Nepali source is only a handful
    # of blobs, so requiring 3 of 6 would leave a vocabulary too thin to judge
    # anything; 2 is the floor that still rejects one document's private artifacts.
    nepali_min_df = 2 if len(nepali_texts) < 10 else lexicon_mod.MIN_DOCUMENT_FREQUENCY

    english = lexicon_mod.build_lexicon(
        english_texts, [], provenance={},
        min_document_frequency=ENGLISH_MIN_DOCUMENT_FREQUENCY,
    )
    nepali = lexicon_mod.build_lexicon(
        [], nepali_texts, provenance={}, min_document_frequency=nepali_min_df,
    )
    combined = lexicon_mod.build_lexicon(
        english_texts, nepali_texts,
        provenance={
            "source": "nrb_extractions status=extracted reason=clean",
            "extractor_version": extractor_version,
            "english_filter": {
                "family": "pdf",
                "max_devanagari_ratio": ENGLISH_MAX_DEVANAGARI,
                "max_legacy_line_ratio": ENGLISH_MAX_LEGACY,
                "min_token_count": ENGLISH_MIN_TOKENS,
            },
            "nepali_filter": {"min_devanagari_ratio": NEPALI_MIN_DEVANAGARI},
            "english_min_document_frequency": ENGLISH_MIN_DOCUMENT_FREQUENCY,
            "nepali_min_document_frequency": nepali_min_df,
        },
        min_document_frequency=lexicon_mod.MIN_DOCUMENT_FREQUENCY,
    )
    # `build_lexicon` applies ONE document-frequency floor; the Nepali half needs a
    # lower one, so it is rebuilt from the separately-built halves.
    final = lexicon_mod.Lexicon(
        version=lexicon_mod.LEXICON_VERSION,
        english=english.english,
        nepali=nepali.nepali,
        fingerprint=lexicon_mod.lexicon_fingerprint(
            lexicon_mod.LEXICON_VERSION, english.english, nepali.nepali
        ),
        provenance={
            **combined.provenance,
            "english_words": len(english.english),
            "nepali_words": len(nepali.nepali),
            "english_documents": len(english_texts),
            "nepali_documents": len(nepali_texts),
        },
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(final.as_json(), ensure_ascii=False, indent=2, sort_keys=False)
        + "\n",
        encoding="utf-8",
    )
    print(f"\nenglish words : {len(final.english):,}")
    print(f"nepali  words : {len(final.nepali):,}")
    print(f"fingerprint   : {final.fingerprint}")
    print(f"written       : {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/nrb/phase6b-lexicon.json", type=Path)
    parser.add_argument("--extractor-version", default=EXTRACTOR_VERSION)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return asyncio.run(
        build(args.out, extractor_version=args.extractor_version,
              verbose=args.verbose)
    )


if __name__ == "__main__":
    raise SystemExit(main())
