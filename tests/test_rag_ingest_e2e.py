"""Upload through the API, ingest with the worker, assert searchable chunks.

Skips unless Postgres AND the real embedding model are both available — this is
the test that proves the whole slice, so it must not be faked.
"""

import asyncio
import io
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.ollama.client import OllamaClient
from app.rag import worker

PASSWORD = "supersecret123"
CSV = b"Employee,Department,Days\nAlice,HR,10\nBob,HR,12\nCarol,HR,7\n"


@pytest.fixture(scope="module")
def model_available():
    settings = get_settings()
    try:
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
        names = {m["name"] for m in resp.json().get("models", [])}
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Ollama unreachable: {type(exc).__name__}")
    if settings.rag_embed_model not in names:
        pytest.skip(f"ollama pull {settings.rag_embed_model} first")
    return True


def test_upload_then_ingest_produces_searchable_chunks(model_available):
    settings = get_settings()
    code = f"e2e{uuid.uuid4().hex[:6]}"

    with TestClient(app) as client:
        try:
            client.post("/auth/register",
                        json={"email": "admin@example.com", "password": PASSWORD})
            login = client.post("/auth/login",
                                json={"email": "admin@example.com",
                                      "password": PASSWORD})
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Postgres unreachable: {type(exc).__name__}")
        if login.status_code != 200:
            pytest.skip("admin login failed")
        admin = {"Authorization": f"Bearer {login.json()['access_token']}"}
        if client.get("/users/me", headers=admin).json().get("role") != "admin":
            pytest.skip("admin@example.com is not an admin here")

        client.post("/v1/departments", json={"code": code, "name": "E2E"},
                    headers=admin)
        accepted = client.post(
            f"/v1/departments/{code}/documents",
            files={"file": ("leave.csv", io.BytesIO(CSV), "text/csv")},
            data={"title": "Leave balances"}, headers=admin,
        )
        assert accepted.status_code == 202
        doc_id = accepted.json()["document_id"]
        job_id = accepted.json()["job_id"]

        async def drain():
            engine = create_async_engine(settings.database_url, poolclass=NullPool)
            ollama = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
            try:
                await worker.preflight(ollama, settings)
                for _ in range(10):
                    if not await worker.run_once(engine, ollama, settings):
                        break
            finally:
                await ollama.aclose()
                await engine.dispose()

        asyncio.run(drain())

        job = client.get(f"/v1/ingest-jobs/{job_id}", headers=admin).json()
        assert job["status"] == "succeeded", job.get("error")

        listed = client.get(f"/v1/departments/{code}/documents",
                            headers=admin).json()
        assert listed[0]["status"] == "ready"
        assert listed[0]["chunk_count"] > 0
        assert listed[0]["embed_model"] == settings.rag_embed_model

        async def probe():
            engine = create_async_engine(settings.database_url, poolclass=NullPool)
            try:
                async with engine.begin() as conn:
                    dims = (await conn.execute(text(
                        "SELECT DISTINCT vector_dims(embedding) FROM document_chunks"
                        " WHERE document_id = :d"), {"d": doc_id})).scalars().all()
                    lexical = (await conn.execute(text(
                        "SELECT count(*) FROM document_chunks"
                        " WHERE document_id = :d"
                        "   AND tsv @@ websearch_to_tsquery('english', 'Alice')"),
                        {"d": doc_id})).scalar_one()
                    return dims, lexical
            finally:
                await engine.dispose()

        dims, lexical = asyncio.run(probe())
        assert dims == [1536]
        assert lexical > 0

        client.delete(f"/v1/departments/{code}/documents/{doc_id}", headers=admin)
