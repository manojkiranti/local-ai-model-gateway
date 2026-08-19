"""Every process that touches a shared tree must mount the same volume.

The NRB blob store is written by `nrb-runner` (the Phase 5 fetch), read by
`worker` during recovery/ingest, and — since citations — read by `gateway` to
serve a source's download. A container-local directory gives each of them a
private, empty copy, and the failure is silent in the §18 way: the runner reports
a successful fetch, the worker records "blob missing in filestore" and still
succeeds the job, and the gateway 404s a document it lists as ready.

This is §28 follow-through. Removing the RAG_DOCS_DIR copy is what stopped
masking it.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
# Every service that reads or writes the NRB blob tree.
SHARING = ("gateway", "worker", "nrb-runner")
MOUNT_POINT = "/app/nrb_files"


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def test_the_nrb_files_volume_is_declared():
    assert "nrb_files" in (compose()["volumes"] or {})


@pytest.mark.parametrize("service", SHARING)
def test_the_service_mounts_the_nrb_volume(service):
    mounts = [m.split(":")[0] for m in compose()["services"][service].get("volumes", [])]
    assert "nrb_files" in mounts, f"{service} does not mount nrb_files"


def test_they_all_mount_it_at_the_same_path():
    """Different mount points would be the same bug wearing a disguise."""
    services = compose()["services"]
    targets = {
        m.split(":")[1]
        for name in SHARING
        for m in services[name].get("volumes", [])
        if m.startswith("nrb_files:")
    }
    assert targets == {MOUNT_POINT}


def test_the_docker_env_template_sets_nrb_files_dir():
    """A volume nobody points NRB_FILES_DIR at is decoration: the default is the
    relative "nrb_files", which resolves against the repo root INSIDE the image
    (/app/nrb_files) — so the template must agree with the mount point."""
    template = (COMPOSE.parent / ".env.docker.example").read_text()
    assert "NRB_FILES_DIR=" in template
