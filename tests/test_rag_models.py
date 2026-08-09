"""Schema-shape tests for the RAG models. Pure metadata assertions — no DB.

These lock the structural decisions that are easy to break silently: the
composite FK that makes a chunk's department provably its document's, the
partial unique indexes whose WHERE clauses are the whole point, and the CHECK
constraints those predicates depend on.
"""

# Import order matters: ChatSession.department_id points at departments.id by
# name, so the rag tables must be registered before the mapper is configured.
from app.rag import models as rag
from app.history.models import ChatSession


def test_embed_dim_is_1536():
    assert rag.EMBED_DIM == 1536


def test_chunk_has_composite_fk_to_document_and_department():
    """The load-bearing invariant: a chunk cannot claim a foreign department."""
    fks = list(rag.DocumentChunk.__table__.foreign_key_constraints)
    composite = [fk for fk in fks if len(fk.columns) == 2]
    assert len(composite) == 1, "expected exactly one composite FK"
    fk = composite[0]
    assert {c.name for c in fk.columns} == {"document_id", "department_id"}
    assert {e.column.name for e in fk.elements} == {"id", "department_id"}
    assert fk.elements[0].column.table.name == "documents"
    assert fk.ondelete == "CASCADE"


def test_documents_expose_the_composite_fk_target():
    """documents needs UNIQUE(id, department_id) or the composite FK cannot exist."""
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in rag.Document.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("department_id", "id") in uniques


def test_documents_dedup_index_excludes_archived():
    idx = next(i for i in rag.Document.__table__.indexes
               if i.name == "ux_documents_active_content")
    assert idx.unique is True
    assert {c.name for c in idx.columns} == {"department_id", "content_hash"}
    where = str(idx.dialect_options["postgresql"]["where"])
    assert "archived" in where


def test_ingest_jobs_allow_only_one_active_job_per_document():
    idx = next(i for i in rag.IngestJob.__table__.indexes
               if i.name == "ux_ingest_jobs_active_document")
    assert idx.unique is True
    assert {c.name for c in idx.columns} == {"document_id"}
    where = str(idx.dialect_options["postgresql"]["where"])
    assert "queued" in where and "running" in where


def test_chunk_embedding_is_not_nullable_and_1536_wide():
    col = rag.DocumentChunk.__table__.c.embedding
    assert col.nullable is False
    assert col.type.dim == rag.EMBED_DIM


def test_tsv_is_a_stored_generated_column_using_english():
    col = rag.DocumentChunk.__table__.c.tsv
    assert col.computed is not None
    assert col.computed.persisted is True
    assert "english" in str(col.computed.sqltext)


def test_metadata_column_is_named_metadata_but_attribute_is_meta():
    """SQLAlchemy reserves `metadata` on the declarative class."""
    assert rag.Document.meta.property.columns[0].name == "metadata"
    assert rag.DocumentChunk.meta.property.columns[0].name == "metadata"


def test_department_deletes_are_restricted_not_nulled():
    """Deleting a department must never silently reclassify history."""
    doc_fk = next(fk for fk in rag.Document.__table__.c.department_id.foreign_keys)
    assert doc_fk.ondelete == "RESTRICT"
    sess_fk = next(fk for fk in ChatSession.__table__.c.department_id.foreign_keys)
    assert sess_fk.ondelete == "RESTRICT"


def test_chat_session_department_is_nullable_for_general_chat():
    assert ChatSession.__table__.c.department_id.nullable is True


def _checks(table):
    return {
        c.name: str(c.sqltext)
        for c in table.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }


def test_status_vocabularies_are_closed_by_check_constraints():
    """The partial indexes key off exact strings — a typo'd status would match
    no predicate and silently bypass them."""
    doc = _checks(rag.Document.__table__)
    assert "archived" in doc["ck_documents_status"]
    assert "pending" in doc["ck_documents_status"]
    assert "upload" in doc["ck_documents_source"]

    job = _checks(rag.IngestJob.__table__)
    assert "queued" in job["ck_ingest_jobs_status"]
    assert "running" in job["ck_ingest_jobs_status"]


def test_document_stores_a_relative_storage_key_not_a_path():
    cols = rag.Document.__table__.c
    assert "storage_key" in cols
    assert "path" not in cols  # host-specific absolute paths are not portable


def test_vector_and_lexical_indexes_are_declared_on_the_model():
    """Declared so autogenerate never proposes dropping them, even though the
    migration creates them by hand."""
    by_name = {i.name: i for i in rag.DocumentChunk.__table__.indexes}
    hnsw = by_name["ix_chunks_embedding"]
    assert hnsw.dialect_options["postgresql"]["using"] == "hnsw"
    assert hnsw.dialect_options["postgresql"]["ops"] == {
        "embedding": "vector_cosine_ops"
    }
    assert by_name["ix_chunks_tsv"].dialect_options["postgresql"]["using"] == "gin"
