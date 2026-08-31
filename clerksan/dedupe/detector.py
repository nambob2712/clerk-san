"""Conservative, non-destructive duplicate candidate detection."""

from __future__ import annotations

import datetime as dt
import io
import unicodedata
import warnings
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import imagehash
from PIL import Image, UnidentifiedImageError
from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from clerksan.db.active_records import restrict_to_active_verified
from clerksan.db.models import (
    Document,
    DocumentFile,
    ExtractedRecord,
    ExtractionStatus,
    FileKind,
    VerifiedRecord,
)

ReadBytes = Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True)
class BusinessFingerprint:
    """The three verified/extracted values required for a fuzzy duplicate candidate."""

    transaction_date: dt.date
    total_amount: Decimal
    counterparty: str


@dataclass
class _Candidate:
    document_id: UUID
    evidence: dict[str, Any] = field(default_factory=dict)
    scores: list[float] = field(default_factory=list)

    def add(self, reason: str, score: float, evidence: dict[str, Any]) -> None:
        self.evidence[reason] = evidence
        self.scores.append(score)

    def as_dict(self) -> dict[str, Any]:
        reasons = tuple(self.evidence)
        return {
            "document_id": str(self.document_id),
            "reason": _reason_label(reasons),
            "reasons": list(reasons),
            "score": max(self.scores),
            "evidence": self.evidence,
        }


async def find_duplicates(
    session: AsyncSession,
    document_id: UUID,
    *,
    read_bytes: ReadBytes | None = None,
    phash_distance_threshold: int = 8,
    fuzzy_threshold: float = 90.0,
) -> list[dict[str, Any]]:
    """Return review candidates without altering, deleting, or merging any document.

    Exact source checksums always run.  Perceptual image comparisons run only when
    a storage reader is supplied, keeping this database-only function safe for API
    requests and simple review queries.  Business matching uses a full triple: date
    within one day, exact decimal amount, and a strong counterparty similarity.
    """

    if phash_distance_threshold < 0:
        raise ValueError("phash_distance_threshold must not be negative")
    if not 0 <= fuzzy_threshold <= 100:
        raise ValueError("fuzzy_threshold must be between 0 and 100")

    document_ids = list(
        (await session.scalars(select(Document.id).where(Document.id != document_id))).all()
    )
    if not document_ids:
        return []
    target_files = (
        await session.scalars(select(DocumentFile).where(DocumentFile.document_id == document_id))
    ).all()
    if not target_files:
        return []
    other_files = (
        await session.scalars(
            select(DocumentFile).where(DocumentFile.document_id.in_(document_ids))
        )
    ).all()
    candidates: dict[UUID, _Candidate] = {}

    _add_exact_checksum_candidates(target_files, other_files, candidates)
    if read_bytes is not None:
        await _add_perceptual_candidates(
            target_files,
            other_files,
            candidates,
            read_bytes=read_bytes,
            distance_threshold=phash_distance_threshold,
        )
    await _add_business_candidates(
        session,
        document_id,
        document_ids,
        candidates,
        threshold=fuzzy_threshold,
    )

    return sorted(
        (candidate.as_dict() for candidate in candidates.values()),
        key=lambda candidate: (-candidate["score"], candidate["document_id"]),
    )


def _add_exact_checksum_candidates(
    target_files: Iterable[DocumentFile],
    other_files: Iterable[DocumentFile],
    candidates: dict[UUID, _Candidate],
) -> None:
    target_hashes = {
        artifact.sha256
        for artifact in target_files
        if artifact.kind is FileKind.ORIGINAL and artifact.sha256
    }
    if not target_hashes:
        return
    for artifact in other_files:
        if artifact.kind is not FileKind.ORIGINAL or artifact.sha256 not in target_hashes:
            continue
        candidate = candidates.setdefault(artifact.document_id, _Candidate(artifact.document_id))
        if "exact_sha256" not in candidate.evidence:
            candidate.add("exact_sha256", 1.0, {"sha256": []})
        evidence = candidate.evidence["exact_sha256"]
        if artifact.sha256 not in evidence["sha256"]:
            evidence["sha256"].append(artifact.sha256)


async def _add_perceptual_candidates(
    target_files: Iterable[DocumentFile],
    other_files: Iterable[DocumentFile],
    candidates: dict[UUID, _Candidate],
    *,
    read_bytes: ReadBytes,
    distance_threshold: int,
) -> None:
    target_hash_groups = await _perceptual_hashes(target_files, read_bytes)
    target_hashes = [value for group in target_hash_groups.values() for value in group]
    if not target_hashes:
        return
    other_hashes = await _perceptual_hashes(other_files, read_bytes)
    for candidate_id, hashes in other_hashes.items():
        distance = min(int(left - right) for left in target_hashes for right in hashes)
        if distance > distance_threshold:
            continue
        candidate = candidates.setdefault(candidate_id, _Candidate(candidate_id))
        candidate.add(
            "phash",
            max(0.0, 1.0 - (distance / 64)),
            {"distance": distance, "threshold": distance_threshold},
        )


async def _perceptual_hashes(
    files: Iterable[DocumentFile], read_bytes: ReadBytes
) -> dict[UUID, list[imagehash.ImageHash]]:
    hashes: dict[UUID, list[imagehash.ImageHash]] = {}
    for artifact in files:
        if artifact.kind not in {FileKind.ORIGINAL, FileKind.PAGE_RENDER}:
            continue
        if not artifact.mime.startswith("image/"):
            continue
        try:
            value = _perceptual_hash(await read_bytes(artifact.content_path))
        except (OSError, ValueError):
            continue
        hashes.setdefault(artifact.document_id, []).append(value)
    return hashes


def _perceptual_hash(image_bytes: bytes) -> imagehash.ImageHash:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.load()
                return imagehash.phash(image.convert("RGB"))
        except (UnidentifiedImageError, OSError) as error:
            raise ValueError("unreadable image artifact") from error
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
            raise ValueError("image artifact exceeds Pillow safety limits") from error


async def _add_business_candidates(
    session: AsyncSession,
    document_id: UUID,
    other_document_ids: list[UUID],
    candidates: dict[UUID, _Candidate],
    *,
    threshold: float,
) -> None:
    fingerprints = await _business_fingerprints(session, [document_id, *other_document_ids])
    target = fingerprints.get(document_id)
    if target is None:
        return
    for candidate_id in other_document_ids:
        candidate_fingerprint = fingerprints.get(candidate_id)
        if candidate_fingerprint is None:
            continue
        evidence = _business_match(target, candidate_fingerprint, threshold)
        if evidence is None:
            continue
        candidate = candidates.setdefault(candidate_id, _Candidate(candidate_id))
        candidate.add("fuzzy", evidence["counterparty_similarity"] / 100, evidence)


async def _business_fingerprints(
    session: AsyncSession, document_ids: list[UUID]
) -> dict[UUID, BusinessFingerprint]:
    fingerprints: dict[UUID, BusinessFingerprint] = {}
    verified_records = (
        await session.scalars(
            restrict_to_active_verified(
                select(VerifiedRecord)
                .where(VerifiedRecord.document_id.in_(document_ids))
                .order_by(
                    VerifiedRecord.document_id,
                    VerifiedRecord.verified_at.desc(),
                    VerifiedRecord.id.desc(),
                )
            )
        )
    ).all()
    for record in verified_records:
        if record.document_id not in fingerprints:
            counterparty = _normalize_counterparty(record.counterparty)
            if counterparty:
                fingerprints[record.document_id] = BusinessFingerprint(
                    transaction_date=record.transaction_date,
                    total_amount=record.total_amount,
                    counterparty=counterparty,
                )

    missing_ids = [document_id for document_id in document_ids if document_id not in fingerprints]
    if not missing_ids:
        return fingerprints
    extracted_records = (
        await session.scalars(
            select(ExtractedRecord)
            .where(
                ExtractedRecord.document_id.in_(missing_ids),
                ExtractedRecord.status.in_(
                    (ExtractionStatus.PENDING_REVIEW, ExtractionStatus.APPROVED)
                ),
            )
            .order_by(
                ExtractedRecord.document_id,
                ExtractedRecord.created_at.desc(),
                ExtractedRecord.id.desc(),
            )
        )
    ).all()
    for record in extracted_records:
        if record.document_id in fingerprints:
            continue
        fingerprint = _fingerprint_from_payload(record.payload)
        if fingerprint is not None:
            fingerprints[record.document_id] = fingerprint
    return fingerprints


def _fingerprint_from_payload(payload: dict[str, Any]) -> BusinessFingerprint | None:
    transaction_date = _as_date(_payload_value(payload, "transaction_date", "date"))
    total_amount = _as_decimal(_payload_value(payload, "total_amount", "amount", "total"))
    counterparty = _payload_value(payload, "counterparty", "vendor", "merchant_name")
    if transaction_date is None or total_amount is None or not isinstance(counterparty, str):
        return None
    normalized_counterparty = _normalize_counterparty(counterparty)
    if not normalized_counterparty:
        return None
    return BusinessFingerprint(transaction_date, total_amount, normalized_counterparty)


def _payload_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, dict) and "value" in value:
            return value["value"]
        return value
    return None


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str):
        return None
    try:
        if "T" in value:
            return dt.datetime.fromisoformat(value).date()
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = str(value).strip().replace(",", "").replace("円", "").replace("¥", "")
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


def _normalize_counterparty(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _business_match(
    target: BusinessFingerprint, candidate: BusinessFingerprint, threshold: float
) -> dict[str, Any] | None:
    date_delta = abs((target.transaction_date - candidate.transaction_date).days)
    if date_delta > 1 or target.total_amount != candidate.total_amount:
        return None
    similarity = float(fuzz.ratio(target.counterparty, candidate.counterparty))
    if similarity < threshold:
        return None
    return {
        "date_delta_days": date_delta,
        "total_amount": str(target.total_amount),
        "counterparty_similarity": similarity,
    }


def _reason_label(reasons: tuple[str, ...]) -> str:
    if set(reasons) == {"phash", "fuzzy"}:
        return "both"
    if len(reasons) == 1:
        return reasons[0]
    return "+".join(reasons)
