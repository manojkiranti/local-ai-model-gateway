"""Upload-route integration for images (.png/.jpg/.webp/.tif/.bmp), against real
Postgres. Skips cleanly if the DB is unreachable.

Mirrors test_document_upload.py: a TestClient per test and a local _auth().
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from starlette.testclient import TestClient

from app.main import app

OWNER = "imgup-owner@example.com"
PASSWORD = "supersecret123"

PNG_CT = "image/png"


def _auth(client, email):
    err = resp = None
    try:
        client.post("/auth/register", json={"email": email, "password": PASSWORD})
        resp = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    except Exception as exc:  # noqa: BLE001
        err = exc
    if err is not None:
        pytest.skip(f"Postgres unreachable: {type(err).__name__}")
    if resp.status_code != 200:
        pytest.skip(f"auth failed (login -> {resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _upload(client, headers, name, data, ctype):
    return client.post("/v1/files", files={"file": (name, data, ctype)}, headers=headers)


def _png_bytes(size=(120, 80), colour=(200, 200, 200), fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format=fmt)
    return buf.getvalue()


def test_png_upload_is_accepted():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "shot.png", _png_bytes((1240, 800)), PNG_CT)
        assert up.status_code == 201, up.text
        body = up.json()
        assert body["media_type"] == "image/png"
        assert body["source"] == "uploaded"
        assert body["summary"] == {
            "kind": "PNG image", "width": 1240, "height": 800, "frames": 1,
        }


def test_jpeg_upload_is_accepted():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "photo.jpg", _png_bytes(fmt="JPEG"), "image/jpeg")
        assert up.status_code == 201, up.text
        assert up.json()["media_type"] == "image/jpeg"


def test_the_rejection_message_names_images():
    """The human-readable allowlist in router.py is a separate string from
    ingest.UPLOAD_TYPES; adding a family without updating it tells the user
    images are unsupported while the route happily accepts them."""
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "a.rtf", b"{\\rtf1}", "application/rtf")
        assert up.status_code == 400
        detail = up.json()["detail"]
        assert ".png" in detail and ".jpg" in detail


def test_a_non_image_named_png_is_refused():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "evil.png", b"MZ\x90\x00 this is a binary", PNG_CT)
        assert up.status_code == 400
        assert "could not read the file" in up.json()["detail"]


def test_a_format_outside_the_allowlist_is_refused_even_though_pillow_reads_it():
    """Defence in depth on Pillow's DECODER surface. A GIF renamed .png is a
    perfectly valid image Pillow will happily open — but GIF is not a format
    this route accepts, and every extra decoder reachable from an upload is
    extra attack surface for a library with a history of decoder CVEs."""
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "sneaky.png", _png_bytes(fmt="GIF"), PNG_CT)
        assert up.status_code == 400
        assert "could not read the file" in up.json()["detail"]


def test_svg_is_refused():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>'
        assert _upload(client, owner, "a.svg", svg, "image/svg+xml").status_code == 400


def test_gif_extension_is_refused():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        assert _upload(client, owner, "a.gif", _png_bytes(fmt="GIF"), "image/gif").status_code == 400


def test_a_pixel_bomb_is_refused_with_400(monkeypatch):
    """Small on the wire, enormous decoded — past both the 10 MB cap and the
    OOXML zip guard. Must be a 400, never a 500 or an OOM."""
    from app.files import images

    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 1000)
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "bomb.png", _png_bytes((200, 200)), PNG_CT)
        assert up.status_code == 400
        assert "too large" in up.json()["detail"]


def test_a_refused_image_leaves_nothing_on_disk():
    from app.files.store import file_store

    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        before = set()
        base = file_store.base_dir
        if base.exists():
            before = {p for p in base.rglob("*") if p.is_file()}
        up = _upload(client, owner, "evil.png", b"not an image", PNG_CT)
        assert up.status_code == 400
        after = {p for p in base.rglob("*") if p.is_file()} if base.exists() else set()
        assert after == before


def test_an_empty_image_is_refused():
    with TestClient(app) as client:
        owner = _auth(client, OWNER)
        up = _upload(client, owner, "empty.png", b"", PNG_CT)
        assert up.status_code == 400
        assert "empty" in up.json()["detail"]
