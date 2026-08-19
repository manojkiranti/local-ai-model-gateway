"""The committed env templates must stay in step with `Settings`.

Two distinct failure modes, both silent at runtime because `Settings` is
configured `extra="ignore"`:

1. A NEW setting lands in `app/config.py` and nobody adds it to `.env.example`,
   so the one file a deployer reads no longer describes the knobs that exist.
2. A setting is REMOVED or renamed but a template keeps setting it. Nothing
   errors — the line is simply ignored — so the deployment looks configured
   while running on the default. `AGENT_NUM_CTX` was exactly this: the `/v1`
   surface has no `num_ctx`, the key lived on in `.env.docker.example`, and the
   context length silently stayed at Ollama's 4096.

`.env.docker.example` is deliberately NOT required to list every setting — it is
a container-network-aware delta, not the catalogue — but it must not carry keys
that no longer mean anything.
"""

import re
from pathlib import Path

import pytest

from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = (".env.example", ".env.docker.example")

# Keys a template may legitimately set that are not gateway Settings fields —
# e.g. config read by a different process. Keep this list short and justified.
FOREIGN_KEYS: frozenset[str] = frozenset(
    {
        # A Docker BUILD arg, not a Settings field. It cannot be a runtime setting:
        # it decides whether the image is built WITH the OCR stack, and env_file is
        # read at runtime — which is exactly the confusion .env.example documents at
        # length beside it. It belongs in the template as documentation.
        "INSTALL_OCR",
    }
)


def setting_keys() -> set[str]:
    """Every `Settings` field as its environment-variable name."""
    return {name.upper() for name in Settings.model_fields}


def template_keys(filename: str) -> set[str]:
    """`KEY=` assignments in a template, ignoring comments and blank lines."""
    text = (REPO_ROOT / filename).read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=", text, re.MULTILINE))


def test_env_example_documents_every_setting():
    """`.env.example` is the catalogue — a new setting must be listed there."""
    missing = sorted(setting_keys() - template_keys(".env.example"))
    assert not missing, (
        "settings exist in app/config.py but are absent from .env.example: "
        f"{missing}"
    )


@pytest.mark.parametrize("filename", TEMPLATES)
def test_template_sets_no_unknown_keys(filename):
    """A template key that isn't a setting is silently ignored — catch it here."""
    unknown = sorted(template_keys(filename) - setting_keys() - FOREIGN_KEYS)
    assert not unknown, (
        f"{filename} sets keys that are not app/config.py settings, so they are "
        f"silently ignored at runtime: {unknown}"
    )
