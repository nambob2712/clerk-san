"""Local pgvector indexing with a deterministic SQLite demo fallback."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.config import Settings, get_settings
from clerksan.db.models import (
    BatchLifecycle,
    Document,
    DocumentClass,
    DocumentFile,
    ExtractedRecord,
    ExtractionBatch,
    ExtractionStatus,
    FileKind,
    Job,
    RecordKind,
)
from clerksan.db.models import (
    Chunk as StoredChunk,
)
from clerksan.ingest.capabilities import (
    LEGACY_COMPAT_EXECUTION_PROFILE,
    UNIVERSAL_SANDBOXED_EXECUTION_PROFILE,
)
from clerksan.ingest.jobs import register_handler
from clerksan.ingest.normalized import NormalizedDocument
from clerksan.llm.client import OllamaClient
from clerksan.search.chunking import Chunk, chunk_document, estimate_tokens
from clerksan.search.embeddings import embed_texts
from clerksan.storage import read_verified_artifact, resolve_storage_path

ReadBytes = Callable[[str], Awaitable[bytes]]
Embedder = Callable[..., Awaitable[list[list[float]]]]


class SearchSchemaUnavailable(RuntimeError):
    """The chunks table has not been installed by the pinned pgvector migration."""


class IndexingError(RuntimeError):
    """A normalized document cannot be transformed into a coherent chunk set."""


class SearchDomain(StrEnum):
    """Authority domains kept separate during semantic retrieval."""

    ALL = "all"
    FINANCIAL = "financial"
    GENERIC = "generic"


class Hit(BaseModel):
    document_id: UUID
    chunk_seq: int = Field(ge=0)
    heading_path: str
    text: str
    distance: float = Field(ge=0)


@dataclass(slots=True)
class SearchDependencies:
    """Explicit local seams for indexing and retrieval tests."""

    settings: Settings
    client: OllamaClient
    read_bytes: ReadBytes
    embedder: Embedder = embed_texts


@dataclass(frozen=True, slots=True)
class _IndexTarget:
    """The immutable artifact and source version an index job is allowed to replace."""

    document: NormalizedDocument
    source_version: int


@dataclass(frozen=True, slots=True)
class _CandidateIndexTarget:
    """One immutable candidate cohort and its exact normalized source."""

    batch: ExtractionBatch
    document: NormalizedDocument
    candidates: tuple[ExtractedRecord, ...]


def build_default_dependencies(settings: Settings | None = None) -> SearchDependencies:
    """Build local-only search dependencies without starting an API or worker."""

    active_settings = settings or get_settings()
    return SearchDependencies(
        settings=active_settings,
        client=OllamaClient(active_settings),
        read_bytes=_storage_reader(active_settings.storage_dir),
    )


async def index_document(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    dependencies: SearchDependencies | None = None,
) -> None:
    """Atomically replace one document's chunks from its immutable normalized artifact."""

    document_id = _document_id(payload)
    job = await _job_for_payload(session, payload, document_id)
    if _job_completed(job):
        return
    owns_client = dependencies is None
    deps = dependencies or build_default_dependencies()
    try:
        target = await _load_index_target(session, document_id, payload, deps)
        if not target.document.embeddable:
            chunks = _discovery_chunks(target.document)
        else:
            chunks = chunk_document(target.document)
        if not chunks:
            replaced = await _replace_chunks(
                session,
                document_id,
                target.source_version,
                [],
                [],
                deps.settings,
            )
        else:
            vectors = await deps.embedder(
                [chunk.text for chunk in chunks],
                deps.client,
                deps.settings,
                purpose="passage",
            )
            if len(chunks) != len(vectors):
                raise IndexingError("embedding count does not match the chunk count")
            replaced = await _replace_chunks(
                session,
                document_id,
                target.source_version,
                chunks,
                vectors,
                deps.settings,
            )
        await _mark_job_completed(
            session,
            job,
            source_version=target.source_version,
            chunks_replaced=replaced,
        )
    finally:
        if owns_client:
            await deps.client.aclose()


async def index_candidate_batch(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    dependencies: SearchDependencies | None = None,
) -> None:
    """Pre-stage immutable candidate-bound chunks without making them searchable."""

    document_id = _document_id(payload)
    batch_id = _batch_id(payload)
    job = await _job_for_payload(session, payload, document_id)
    if _job_completed(job):
        return
    owns_client = dependencies is None
    deps = dependencies or build_default_dependencies()
    try:
        target = await _load_candidate_index_target(
            session,
            document_id=document_id,
            batch_id=batch_id,
            payload=payload,
            deps=deps,
        )
        candidate_chunks = _candidate_chunks(target)
        vectors = await deps.embedder(
            [chunk.text for _, chunk in candidate_chunks],
            deps.client,
            deps.settings,
            purpose="passage",
        )
        if len(candidate_chunks) != len(vectors):
            raise IndexingError("embedding count does not match the candidate chunk count")
        replaced = await _replace_candidate_chunks(
            session,
            target,
            candidate_chunks,
            vectors,
            deps.settings,
        )
        await _mark_candidate_job_completed(
            session,
            job,
            batch_id=batch_id,
            source_version=target.batch.source_version,
            chunks_replaced=replaced,
            chunk_count=len(candidate_chunks) if replaced else 0,
        )
    finally:
        if owns_client:
            await deps.client.aclose()


async def _job_for_payload(
    session: AsyncSession, payload: dict[str, Any], document_id: UUID
) -> Job | None:
    """Load the leased job so successful derivative work survives a hard kill."""

    job_value = payload.get("_job_id")
    if job_value is None:
        return None
    try:
        job_id = UUID(str(job_value))
    except (TypeError, ValueError) as error:
        raise ValueError("_job_id must be a UUID") from error
    job = await session.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise LookupError(f"job {job_id} no longer exists")
    if job.document_id != document_id:
        raise ValueError("job document_id does not match payload.document_id")
    return job


def _job_completed(job: Job | None) -> bool:
    pipeline = job.payload.get("_pipeline") if job is not None else None
    return isinstance(pipeline, dict) and pipeline.get("completed") is True


async def _mark_job_completed(
    session: AsyncSession,
    job: Job | None,
    *,
    source_version: int,
    chunks_replaced: bool,
) -> None:
    if job is None:
        return
    job.payload = {
        **job.payload,
        "_pipeline": {
            "completed": True,
            "indexed_source_version": source_version,
            "chunks_replaced": chunks_replaced,
        },
    }
    await session.flush()


async def _mark_candidate_job_completed(
    session: AsyncSession,
    job: Job | None,
    *,
    batch_id: UUID,
    source_version: int,
    chunks_replaced: bool,
    chunk_count: int,
) -> None:
    if job is None:
        return
    job.payload = {
        **job.payload,
        "_pipeline": {
            "completed": True,
            "indexed_batch_id": str(batch_id),
            "indexed_source_version": source_version,
            "chunks_replaced": chunks_replaced,
            "chunk_count": chunk_count,
        },
    }
    await session.flush()


async def search(
    session: AsyncSession,
    query: str,
    *,
    k: int = 8,
    doc_class: DocumentClass | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    domain: SearchDomain | str = SearchDomain.ALL,
    dependencies: SearchDependencies | None = None,
) -> list[Hit]:
    """Retrieve locally indexed chunks with parameterized document metadata filters."""

    cleaned_query = query.strip()
    if not cleaned_query:
        raise ValueError("query must not be empty")
    if not 1 <= k <= 100:
        raise ValueError("k must be between 1 and 100")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("date_from must not be after date_to")
    try:
        resolved_domain = domain if isinstance(domain, SearchDomain) else SearchDomain(domain)
    except (TypeError, ValueError) as error:
        raise ValueError("domain must be all, financial, or generic") from error

    owns_client = dependencies is None
    deps = dependencies or build_default_dependencies()
    try:
        vectors = await deps.embedder([cleaned_query], deps.client, deps.settings, purpose="query")
        if len(vectors) != 1:
            raise IndexingError("query embedding did not return exactly one vector")
        if session.get_bind().dialect.name == "postgresql":
            return await _postgres_search(
                session,
                vectors[0],
                k=k,
                doc_class=doc_class,
                date_from=date_from,
                date_to=date_to,
                domain=resolved_domain,
            )
        return await _sqlite_search(
            session,
            vectors[0],
            k=k,
            doc_class=doc_class,
            date_from=date_from,
            date_to=date_to,
            domain=resolved_domain,
        )
    finally:
        if owns_client:
            await deps.client.aclose()


async def _load_index_target(
    session: AsyncSession,
    document_id: UUID,
    payload: dict[str, Any],
    deps: SearchDependencies,
) -> _IndexTarget:
    """Load the precise immutable artifact carried by an index job.

    Legacy callers that supply only a document id still work, but worker-created jobs
    must name both the source version and the normalized checksum.  The latter avoids
    silently indexing a different artifact after a delayed retry.
    """

    requested_source_version = _source_version(payload)
    requested_source_file_id = _source_file_id(payload)
    requested_sha256 = _normalized_sha256(payload)
    execution_profile = _execution_profile(payload)
    if requested_source_version is None and execution_profile != LEGACY_COMPAT_EXECUTION_PROFILE:
        raise IndexingError("universal indexing requires exact source-bound lineage")
    statement = select(DocumentFile).where(
        DocumentFile.document_id == document_id,
        DocumentFile.kind == FileKind.NORMALIZED,
    )
    if requested_source_version is not None:
        source_file_id = await session.scalar(
            select(DocumentFile.id).where(
                DocumentFile.document_id == document_id,
                DocumentFile.kind == FileKind.ORIGINAL,
                DocumentFile.version == requested_source_version,
            )
        )
        if source_file_id is None:
            raise IndexingError(
                f"document {document_id} has no original source version {requested_source_version}"
            )
        if requested_source_file_id is not None and requested_source_file_id != source_file_id:
            raise IndexingError("index job source identity does not match its original")
        statement = statement.where(
            DocumentFile.source_file_id == source_file_id,
            DocumentFile.source_version == requested_source_version,
        )
    if requested_sha256 is not None:
        statement = statement.where(DocumentFile.sha256 == requested_sha256)
    artifact = await session.scalar(statement.order_by(DocumentFile.version.desc()).limit(1))
    if artifact is None:
        if requested_source_version is None:
            raise IndexingError(f"document {document_id} has no normalized artifact")
        raise IndexingError(
            f"document {document_id} has no normalized artifact for source version "
            f"{requested_source_version}"
        )
    try:
        encoded = await read_verified_artifact(
            deps.read_bytes, artifact.content_path, artifact.sha256
        )
        document = NormalizedDocument.model_validate_json(encoded)
    except (OSError, ValueError) as error:
        raise IndexingError(
            f"normalized artifact for document {document_id} is unreadable"
        ) from error
    source_version = (
        requested_source_version
        or artifact.source_version
        or _source_version_from_provenance(artifact.text_provenance)
        or await _current_source_version(session, document_id)
    )
    if source_version is None:
        raise IndexingError(f"document {document_id} has no original artifact")
    return _IndexTarget(document=document, source_version=source_version)


async def _load_candidate_index_target(
    session: AsyncSession,
    *,
    document_id: UUID,
    batch_id: UUID,
    payload: dict[str, Any],
    deps: SearchDependencies,
) -> _CandidateIndexTarget:
    """Load one exact source-bound candidate cohort and its normalized artifact."""

    batch = await session.scalar(
        select(ExtractionBatch).where(
            ExtractionBatch.id == batch_id,
            ExtractionBatch.document_id == document_id,
        )
    )
    if batch is None:
        raise IndexingError(f"candidate batch {batch_id} does not exist")
    if batch.lifecycle not in {
        BatchLifecycle.OPEN,
        BatchLifecycle.READY_TO_ACTIVATE,
    }:
        raise IndexingError("candidate batch is no longer indexable")
    requested_source_version = _source_version(payload)
    if requested_source_version is not None and requested_source_version != batch.source_version:
        raise IndexingError("candidate index job source version does not match its batch")
    requested_source_file_id = _source_file_id(payload)
    if requested_source_file_id is not None and requested_source_file_id != batch.source_file_id:
        raise IndexingError("candidate index job source file does not match its batch")
    requested_sha256 = _normalized_sha256(payload)
    if requested_sha256 is not None and requested_sha256 != batch.normalized_sha256:
        raise IndexingError("candidate index job normalized checksum does not match its batch")

    current_source = await session.scalar(
        select(DocumentFile)
        .where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.ORIGINAL,
        )
        .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
        .limit(1)
    )
    if (
        current_source is None
        or current_source.id != batch.source_file_id
        or current_source.version != batch.source_version
        or current_source.sha256 != batch.source_sha256
    ):
        raise IndexingError("candidate batch source is no longer current")

    artifact = await session.scalar(
        select(DocumentFile)
        .where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.NORMALIZED,
            DocumentFile.source_file_id == batch.source_file_id,
            DocumentFile.source_version == batch.source_version,
            DocumentFile.sha256 == batch.normalized_sha256,
        )
        .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
        .limit(1)
    )
    if artifact is None:
        raise IndexingError("candidate batch has no exact normalized artifact")
    try:
        encoded = await read_verified_artifact(
            deps.read_bytes,
            artifact.content_path,
            artifact.sha256,
        )
        normalized = NormalizedDocument.model_validate_json(encoded)
    except (OSError, ValueError) as error:
        raise IndexingError("candidate batch normalized artifact is unreadable") from error
    if normalized.metadata.sha256 != batch.source_sha256:
        raise IndexingError("candidate batch normalized artifact is bound to another source")

    candidates = tuple(
        (
            await session.scalars(
                select(ExtractedRecord)
                .where(ExtractedRecord.batch_id == batch.id)
                .order_by(
                    ExtractedRecord.candidate_ordinal.asc(),
                    ExtractedRecord.id.asc(),
                )
            )
        ).all()
    )
    if len(candidates) != batch.candidate_count:
        raise IndexingError("candidate batch membership does not reconcile")
    return _CandidateIndexTarget(
        batch=batch,
        document=normalized,
        candidates=candidates,
    )


def _candidate_chunks(
    target: _CandidateIndexTarget,
) -> list[tuple[ExtractedRecord, Chunk]]:
    """Build bounded chunks from immutable candidate payloads, not raw source bytes."""

    chunks: list[tuple[ExtractedRecord, Chunk]] = []
    for candidate in target.candidates:
        if (
            candidate.candidate_key is None
            or candidate.record_kind is None
            or candidate.source_file_id != target.batch.source_file_id
            or candidate.source_version != target.batch.source_version
        ):
            raise IndexingError("candidate batch contains incomplete search lineage")
        body = _candidate_search_text(candidate)
        candidate_document = target.document.model_copy(
            update={
                "markdown_body": body,
                "tables": [],
                "images": [],
                "embeddable": True,
            }
        )
        generated = chunk_document(candidate_document)
        if not generated:
            raise IndexingError("candidate payload produced no searchable content")
        for chunk in generated:
            heading = candidate.source_locator or "candidate"
            if chunk.heading_path:
                heading = f"{heading} > {chunk.heading_path}"
            chunks.append((candidate, chunk.model_copy(update={"heading_path": heading})))
    return chunks


def _candidate_search_text(candidate: ExtractedRecord) -> str:
    payload = candidate.payload
    if candidate.record_kind is RecordKind.GENERIC_DOCUMENT:
        body = payload.get("content_markdown") if isinstance(payload, dict) else None
        if isinstance(body, str) and body.strip():
            return body.strip()
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise IndexingError("candidate payload is not canonical JSON") from error
    if not encoded.strip() or encoded == "{}":
        raise IndexingError("candidate payload has no searchable content")
    return encoded


async def _replace_chunks(
    session: AsyncSession,
    document_id: UUID,
    source_version: int,
    chunks: list[Chunk],
    vectors: list[list[float]],
    settings: Settings,
) -> bool:
    if len(chunks) != len(vectors):
        raise IndexingError("chunk and embedding counts differ")
    model, digest, dimension = _embedding_pin(settings)
    if any(len(vector) != dimension for vector in vectors):
        raise IndexingError("embedding vector does not match the pinned dimension")
    try:
        # A job handler normally owns the outer transaction.  The savepoint also
        # keeps a direct caller from committing the deletion if one replacement
        # insert fails part-way through on SQLite.
        async with session.begin_nested():
            if await _locked_current_source_version(session, document_id) != source_version:
                return False
            await session.execute(
                text("DELETE FROM chunks WHERE document_id = :document_id"),
                {"document_id": _document_parameter(session, document_id)},
            )
            for chunk, vector in zip(chunks, vectors, strict=True):
                await _insert_chunk(session, document_id, chunk, vector, model, digest)
    except SQLAlchemyError as error:
        if "chunks" in str(error).lower():
            raise SearchSchemaUnavailable(
                "search schema is unavailable; apply the pinned chunks migration before indexing"
            ) from error
        raise
    return True


async def _replace_candidate_chunks(
    session: AsyncSession,
    target: _CandidateIndexTarget,
    chunks: list[tuple[ExtractedRecord, Chunk]],
    vectors: list[list[float]],
    settings: Settings,
) -> bool:
    """Replace only one hidden batch manifest under the current-source lock."""

    if len(chunks) != len(vectors):
        raise IndexingError("candidate chunk and embedding counts differ")
    model, digest, dimension = _embedding_pin(settings)
    if any(len(vector) != dimension for vector in vectors):
        raise IndexingError("embedding vector does not match the pinned dimension")
    try:
        async with session.begin_nested():
            if (
                await _locked_current_source_version(session, target.batch.document_id)
                != target.batch.source_version
            ):
                return False
            current_source = await session.scalar(
                select(DocumentFile).where(
                    DocumentFile.id == target.batch.source_file_id,
                    DocumentFile.document_id == target.batch.document_id,
                    DocumentFile.kind == FileKind.ORIGINAL,
                    DocumentFile.version == target.batch.source_version,
                    DocumentFile.sha256 == target.batch.source_sha256,
                )
            )
            if current_source is None:
                return False
            batch = await session.scalar(
                select(ExtractionBatch)
                .where(ExtractionBatch.id == target.batch.id)
                .with_for_update()
            )
            if batch is None or batch.lifecycle not in {
                BatchLifecycle.OPEN,
                BatchLifecycle.READY_TO_ACTIVATE,
            }:
                return False
            await session.execute(delete(StoredChunk).where(StoredChunk.batch_id == batch.id))
            for (candidate, chunk), vector in zip(chunks, vectors, strict=True):
                assert candidate.candidate_key is not None
                assert candidate.record_kind is not None
                chunk_digest = _text_sha256(chunk.text)
                chunk_id = uuid.uuid5(
                    uuid.UUID("63b2b2e9-af24-4436-a061-293763efc56a"),
                    f"{batch.id}:{candidate.id}:{chunk.seq}:{chunk_digest}:{digest}",
                )
                session.add(
                    StoredChunk(
                        id=chunk_id,
                        document_id=batch.document_id,
                        batch_id=batch.id,
                        extraction_id=candidate.id,
                        record_kind=candidate.record_kind,
                        source_file_id=batch.source_file_id,
                        source_version=batch.source_version,
                        candidate_key=candidate.candidate_key,
                        seq=chunk.seq,
                        heading_path=chunk.heading_path,
                        text=chunk.text,
                        embedding=vector,
                        embed_model=model,
                        embed_model_digest=digest,
                        token_count=chunk.token_count,
                    )
                )
            await session.flush()
    except SQLAlchemyError as error:
        if "chunks" in str(error).lower():
            raise SearchSchemaUnavailable(
                "search schema is unavailable; apply the pinned chunks migration before indexing"
            ) from error
        raise
    return True


def _text_sha256(value: str) -> str:
    """Return a stable content identity without storing source bytes."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _insert_chunk(
    session: AsyncSession,
    document_id: UUID,
    chunk: Chunk,
    vector: list[float],
    model: str,
    digest: str,
) -> None:
    params = {
        "id": str(uuid.uuid4()),
        "document_id": _document_parameter(session, document_id),
        "seq": chunk.seq,
        "heading_path": chunk.heading_path,
        "text": chunk.text,
        "embed_model": model,
        "embed_model_digest": digest,
        "token_count": chunk.token_count,
    }
    if session.get_bind().dialect.name == "postgresql":
        params["embedding"] = _vector_literal(vector)
        statement = text(
            """
            INSERT INTO chunks (
                id, document_id, seq, heading_path, text, embedding,
                embed_model, embed_model_digest, token_count
            ) VALUES (
                CAST(:id AS uuid), CAST(:document_id AS uuid), :seq, :heading_path, :text,
                CAST(:embedding AS vector), :embed_model, :embed_model_digest, :token_count
            )
            """
        )
    else:
        params["embedding"] = json.dumps(vector, separators=(",", ":"))
        statement = text(
            """
            INSERT INTO chunks (
                id, document_id, seq, heading_path, text, embedding,
                embed_model, embed_model_digest, token_count
            ) VALUES (
                :id, :document_id, :seq, :heading_path, :text, :embedding,
                :embed_model, :embed_model_digest, :token_count
            )
            """
        )
    await session.execute(statement, params)


async def _postgres_search(
    session: AsyncSession,
    vector: list[float],
    *,
    k: int,
    doc_class: DocumentClass | None,
    date_from: dt.date | None,
    date_to: dt.date | None,
    domain: SearchDomain,
) -> list[Hit]:
    filters, params = _search_filters(doc_class, date_from, date_to, domain)
    params.update({"embedding": _vector_literal(vector), "limit": k})
    statement = text(
        f"""
        SELECT c.document_id, c.seq, c.heading_path, c.text,
               c.embedding <=> CAST(:embedding AS vector) AS distance
          FROM chunks AS c
          JOIN documents AS d ON d.id = c.document_id
         WHERE 1 = 1 {filters}
         ORDER BY c.embedding <=> CAST(:embedding AS vector), c.document_id, c.seq
         LIMIT :limit
        """
    )
    try:
        rows = (await session.execute(statement, params)).mappings().all()
    except SQLAlchemyError as error:
        raise SearchSchemaUnavailable(
            "search schema is unavailable; apply the pinned chunks migration before querying"
        ) from error
    return [_hit_from_row(row) for row in rows]


async def _sqlite_search(
    session: AsyncSession,
    vector: list[float],
    *,
    k: int,
    doc_class: DocumentClass | None,
    date_from: dt.date | None,
    date_to: dt.date | None,
    domain: SearchDomain,
) -> list[Hit]:
    filters, params = _search_filters(doc_class, date_from, date_to, domain)
    statement = text(
        f"""
        SELECT c.document_id, c.seq, c.heading_path, c.text, c.embedding
          FROM chunks AS c
          JOIN documents AS d ON d.id = c.document_id
         WHERE 1 = 1 {filters}
        """
    )
    try:
        rows = (await session.execute(statement, params)).mappings().all()
    except SQLAlchemyError as error:
        raise SearchSchemaUnavailable(
            "search schema is unavailable; create the local chunks table before querying"
        ) from error
    hits = [
        Hit(
            document_id=UUID(str(row["document_id"])),
            chunk_seq=int(row["seq"]),
            heading_path=str(row["heading_path"]),
            text=str(row["text"]),
            distance=_cosine_distance(vector, _sqlite_vector(row["embedding"])),
        )
        for row in rows
    ]
    return sorted(hits, key=lambda hit: (hit.distance, str(hit.document_id), hit.chunk_seq))[:k]


def _search_filters(
    doc_class: DocumentClass | None,
    date_from: dt.date | None,
    date_to: dt.date | None,
    domain: SearchDomain,
) -> tuple[str, dict[str, Any]]:
    active_candidate = """
        EXISTS (
            SELECT 1
              FROM extraction_batches AS b
              JOIN extracted_records AS ce
                ON ce.id = c.extraction_id
               AND ce.batch_id = c.batch_id
               AND ce.document_id = c.document_id
             WHERE b.id = c.batch_id
               AND b.document_id = c.document_id
               AND b.lifecycle = 'active'
               AND ce.status = 'approved'
        )
    """
    active_legacy_financial = """
        EXISTS (
            SELECT 1
              FROM verified_records AS lv
              JOIN extracted_records AS le
                ON le.id = lv.extracted_id
               AND le.document_id = lv.document_id
             WHERE lv.document_id = c.document_id
               AND le.batch_id IS NULL
               AND le.status = 'approved'
        )
    """
    if domain is SearchDomain.GENERIC:
        filters = (
            " AND c.batch_id IS NOT NULL"
            " AND c.record_kind = 'generic_document'"
            f" AND {active_candidate}"
        )
    elif domain is SearchDomain.FINANCIAL:
        filters = (
            " AND ((c.batch_id IS NULL"
            f" AND {active_legacy_financial})"
            " OR (c.batch_id IS NOT NULL AND c.record_kind = 'financial'"
            f" AND {active_candidate}))"
        )
    else:
        filters = f" AND (c.batch_id IS NULL OR {active_candidate})"
    params: dict[str, Any] = {}
    if doc_class is not None:
        filters += " AND d.document_class = :doc_class"
        params["doc_class"] = doc_class.value
    if date_from is not None or date_to is not None:
        filters += """
            AND EXISTS (
                SELECT 1
                  FROM verified_records AS v
                  JOIN extracted_records AS e
                    ON e.id = v.extracted_id
                   AND e.document_id = v.document_id
                 WHERE v.document_id = c.document_id
                   AND e.status = :active_extraction_status
                   AND (:date_from IS NULL OR v.transaction_date >= :date_from)
                   AND (:date_to IS NULL OR v.transaction_date <= :date_to)
            )
        """
        params["date_from"] = date_from.isoformat() if date_from else None
        params["date_to"] = date_to.isoformat() if date_to else None
        params["active_extraction_status"] = ExtractionStatus.APPROVED.value
    return filters, params


def _hit_from_row(row: Any) -> Hit:
    return Hit(
        document_id=UUID(str(row["document_id"])),
        chunk_seq=int(row["seq"]),
        heading_path=str(row["heading_path"]),
        text=str(row["text"]),
        distance=float(row["distance"]),
    )


def _sqlite_vector(raw: Any) -> list[float]:
    try:
        decoded = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as error:
        raise IndexingError("stored SQLite embedding is not valid JSON") from error
    if not isinstance(decoded, list) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) for value in decoded
    ):
        raise IndexingError("stored SQLite embedding is not numeric")
    return [float(value) for value in decoded]


def _cosine_distance(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise IndexingError("stored embedding dimension differs from the query dimension")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise IndexingError("cannot compare a zero-magnitude embedding")
    similarity = sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
    return max(0.0, 1.0 - similarity)


def _embedding_pin(settings: Settings) -> tuple[str, str, int]:
    if not settings.embed_model or not settings.embed_model_digest or not settings.embed_dim:
        raise IndexingError("indexing requires a complete local embedding model pin")
    return settings.embed_model, settings.embed_model_digest, settings.embed_dim


def _discovery_chunks(document: NormalizedDocument) -> list[Chunk]:
    """Index only bounded sheet descriptions when spreadsheet rows are staged as SQL data."""

    descriptions = document.metadata.extra.get("sheet_descriptions")
    if not isinstance(descriptions, list):
        return []
    chunks: list[Chunk] = []
    for index, description in enumerate(descriptions):
        if not isinstance(description, str) or not description.strip():
            continue
        text = description.strip()
        chunks.append(
            Chunk(
                seq=len(chunks),
                heading_path=f"sheet discovery {index + 1}",
                text=text,
                token_count=estimate_tokens(text),
            )
        )
    return chunks


def _vector_literal(vector: list[float]) -> str:
    if any(not math.isfinite(value) for value in vector):
        raise IndexingError("embedding contains a non-finite value")
    return "[" + ",".join(format(value, ".17g") for value in vector) + "]"


def _source_version(payload: dict[str, Any]) -> int | None:
    value = payload.get("source_version")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("payload.source_version must be a positive integer")
    try:
        version = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("payload.source_version must be a positive integer") from error
    if version < 1 or str(version) != str(value).strip():
        raise ValueError("payload.source_version must be a positive integer")
    return version


def _source_file_id(payload: dict[str, Any]) -> UUID | None:
    value = payload.get("source_file_id")
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError("payload.source_file_id must be a UUID") from error


def _normalized_sha256(payload: dict[str, Any]) -> str | None:
    value = payload.get("normalized_sha256")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("payload.normalized_sha256 must be a SHA-256 hex digest")
    digest = value.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("payload.normalized_sha256 must be a SHA-256 hex digest")
    return digest


def _execution_profile(payload: dict[str, Any]) -> str:
    value = payload.get("_execution_profile", LEGACY_COMPAT_EXECUTION_PROFILE)
    if value not in {
        LEGACY_COMPAT_EXECUTION_PROFILE,
        UNIVERSAL_SANDBOXED_EXECUTION_PROFILE,
    }:
        raise ValueError("payload execution profile is invalid")
    return str(value)


def _normalized_provenance(source_version: int) -> str:
    return f"normalized_document:source_version:{source_version}"


def _source_version_from_provenance(provenance: str | None) -> int | None:
    prefix = "normalized_document:source_version:"
    if not isinstance(provenance, str) or not provenance.startswith(prefix):
        return None
    value = provenance.removeprefix(prefix)
    if not value.isdecimal() or int(value) < 1:
        return None
    return int(value)


async def _current_source_version(session: AsyncSession, document_id: UUID) -> int | None:
    return await session.scalar(
        select(DocumentFile.version)
        .where(
            DocumentFile.document_id == document_id,
            DocumentFile.kind == FileKind.ORIGINAL,
        )
        .order_by(DocumentFile.version.desc(), DocumentFile.id.desc())
        .limit(1)
    )


async def _locked_current_source_version(session: AsyncSession, document_id: UUID) -> int:
    """Serialize final replacement with raw-source version changes.

    PostgreSQL uses the document row lock shared by ``append_raw_source``.  SQLite
    ignores ``FOR UPDATE``, so a no-op document update acquires its writer lock before
    the version check.  The outer handler transaction retains either lock until the
    replacement is committed.
    """

    if session.get_bind().dialect.name == "sqlite":
        await session.execute(
            text("UPDATE documents SET id = id WHERE id = :document_id"),
            {"document_id": _document_parameter(session, document_id)},
        )
    else:
        document = await session.scalar(
            select(Document.id).where(Document.id == document_id).with_for_update()
        )
        if document is None:
            raise IndexingError(f"document {document_id} no longer exists")
    source_version = await _current_source_version(session, document_id)
    if source_version is None:
        raise IndexingError(f"document {document_id} has no original artifact")
    return source_version


def _document_id(payload: dict[str, Any]) -> UUID:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a JSON object")
    try:
        return UUID(str(payload["document_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("payload.document_id must be a UUID") from error


def _batch_id(payload: dict[str, Any]) -> UUID:
    try:
        return UUID(str(payload["batch_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("payload.batch_id must be a UUID") from error


def _document_parameter(session: AsyncSession, document_id: UUID) -> str:
    """Match SQLAlchemy's compact UUID storage when the local demo uses SQLite."""

    return document_id.hex if session.get_bind().dialect.name == "sqlite" else str(document_id)


def _storage_reader(storage_dir: Path) -> ReadBytes:
    async def read(content_path: str) -> bytes:
        path = resolve_storage_path(storage_dir, content_path)
        return await asyncio.to_thread(path.read_bytes)

    return read


register_handler("index_document", index_document)
register_handler("index_candidate_batch", index_candidate_batch)
