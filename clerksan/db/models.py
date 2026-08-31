"""Typed ORM mappings for Clerk-san's persisted document lifecycle."""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class DocumentClass(enum.StrEnum):
    """Document vocabulary shared by ingestion, review, and exports."""

    RECEIPT = "receipt"
    INVOICE = "invoice"
    BILL = "bill"
    RECURRING_BILL = "recurring_bill"
    QUOTE = "quote"
    OTHER = "other"


class DocumentStatus(enum.StrEnum):
    UPLOADED = "uploaded"
    NORMALIZED = "normalized"
    EXTRACTED = "extracted"
    IN_REVIEW = "in_review"
    VERIFIED = "verified"
    NEEDS_REPROCESS = "needs_reprocess"
    FAILED = "failed"


class FileKind(enum.StrEnum):
    ORIGINAL = "original"
    PAGE_RENDER = "page_render"
    NORMALIZED = "normalized"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


class IntakeIntent(enum.StrEnum):
    """The user-selected intake path retained with each immutable source."""

    LEGACY_UNSPECIFIED = "legacy_unspecified"
    GENERIC_FILE = "generic_file"
    BILL_SCAN = "bill_scan"


class ExecutionProfile(enum.StrEnum):
    """Execution boundary recorded for every intake and parser job."""

    LEGACY_COMPAT = "legacy_compat"
    UNIVERSAL_SANDBOXED = "universal_sandboxed"


class SourceIntakeState(enum.StrEnum):
    """Processing outcome for one immutable source, separate from review state."""

    QUEUED = "queued"
    PROCESSING = "processing"
    PROCESSED = "processed"
    NEEDS_MAPPING = "needs_mapping"
    STORED_UNPROCESSED = "stored_unprocessed"
    FAILED = "failed"


class ExtractionStatus(enum.StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class RecordKind(enum.StrEnum):
    """Persisted review and retrieval domain for an extracted candidate."""

    FINANCIAL = "financial"
    GENERIC_DOCUMENT = "generic_document"


class FinancialSubtype(enum.StrEnum):
    """Canonical financial meaning supplied by classification or confirmed mapping."""

    TRANSACTION = "transaction"
    RECEIPT = "receipt"
    INVOICE = "invoice"
    BILL = "bill"
    RECURRING_BILL = "recurring_bill"
    QUOTE = "quote"
    OTHER_FINANCIAL = "other_financial"


class BatchLifecycle(enum.StrEnum):
    """Lifecycle of one immutable extraction cohort."""

    OPEN = "open"
    READY_TO_ACTIVATE = "ready_to_activate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class CandidateDecisionAction(enum.StrEnum):
    """Append-only reviewer disposition for one exact batch candidate version."""

    INCLUDE = "include"
    EXCLUDE = "exclude"


class PaymentStatus(enum.StrEnum):
    PAID = "paid"
    UNPAID = "unpaid"
    OVERDUE = "overdue"


class IssuerKind(enum.StrEnum):
    ELECTRIC = "electric"
    GAS = "gas"
    WATER = "water"
    NHI = "nhi"
    TAX = "tax"
    OTHER = "other"


class ExpenseKind(enum.StrEnum):
    """Coarse semantic expense groups, separate from accounting categories."""

    RETAIL = "retail"
    ELECTRICITY = "electricity"
    WATER = "water"
    GAS = "gas"
    TELECOM = "telecom"
    TAX = "tax"
    INSURANCE = "insurance"
    RENT = "rent"
    SUBSCRIPTION = "subscription"
    OTHER = "other"


def _enum_type(enum_type: type[enum.Enum]) -> SqlEnum:
    return SqlEnum(
        enum_type,
        native_enum=False,
        create_constraint=False,
        values_callable=lambda members: [member.value for member in members],
    )


JSON_VALUE = JSON().with_variant(JSONB, "postgresql")
AUDIT_ID = BigInteger().with_variant(Integer, "sqlite")
# The configured embedding pin is reflected in both this mapping and migration 0004.
# SQLite's local demo stores vectors as JSON; production uses pgvector's fixed 768-D type.
EMBEDDING_VALUE = Vector(768).with_variant(JSON, "sqlite")


class Base(DeclarativeBase):
    """Base class for tables maintained by the SQL migration sequence."""


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_class: Mapped[DocumentClass] = mapped_column(
        _enum_type(DocumentClass), nullable=False, default=DocumentClass.OTHER
    )
    status: Mapped[DocumentStatus] = mapped_column(
        _enum_type(DocumentStatus), nullable=False, default=DocumentStatus.UPLOADED
    )
    source_filename: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    files: Mapped[list[DocumentFile]] = relationship(
        back_populates="document", cascade="save-update, merge", order_by="DocumentFile.version"
    )
    extractions: Mapped[list[ExtractedRecord]] = relationship(
        back_populates="document",
        cascade="save-update, merge",
        order_by="ExtractedRecord.created_at",
    )
    verified_records: Mapped[list[VerifiedRecord]] = relationship(
        back_populates="document",
        cascade="save-update, merge",
        order_by="VerifiedRecord.verified_at",
    )
    jobs: Mapped[list[Job]] = relationship(
        back_populates="document", cascade="save-update, merge", order_by="Job.created_at"
    )
    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document", cascade="save-update, merge", order_by="Chunk.seq"
    )
    spreadsheet_rows: Mapped[list[SpreadsheetRow]] = relationship(
        back_populates="document", cascade="save-update, merge"
    )
    embedded_media: Mapped[list[EmbeddedMedia]] = relationship(
        back_populates="document", cascade="save-update, merge"
    )
    recurring_bills: Mapped[list[RecurringBill]] = relationship(
        back_populates="document", cascade="save-update, merge"
    )
    duplicate_flags: Mapped[list[DuplicateFlag]] = relationship(
        back_populates="document",
        cascade="save-update, merge",
        foreign_keys="DuplicateFlag.document_id",
    )
    suspected_by_duplicate_flags: Mapped[list[DuplicateFlag]] = relationship(
        back_populates="suspected_document",
        cascade="save-update, merge",
        foreign_keys="DuplicateFlag.suspected_document_id",
    )
    source_intakes: Mapped[list[SourceIntake]] = relationship(
        back_populates="document",
        cascade="save-update, merge",
        foreign_keys="SourceIntake.document_id",
        order_by="SourceIntake.created_at",
    )


class DocumentFile(Base):
    __tablename__ = "document_files"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="document_files_document_id_version_key"),
        UniqueConstraint("id", "document_id", name="document_files_id_document_id_key"),
        UniqueConstraint(
            "id",
            "document_id",
            "version",
            name="document_files_id_document_id_version_key",
        ),
        UniqueConstraint(
            "id",
            "document_id",
            "version",
            "sha256",
            name="document_files_exact_source_identity_key",
        ),
        ForeignKeyConstraint(
            ("source_file_id", "document_id", "source_version"),
            ("document_files.id", "document_files.document_id", "document_files.version"),
            ondelete="RESTRICT",
            name="document_files_source_identity_fkey",
        ),
        Index("document_files_document_created_idx", "document_id", "created_at"),
        Index(
            "document_files_latest_original_idx",
            "document_id",
            "version",
            postgresql_where=text("kind = 'original'"),
            sqlite_where=text("kind = 'original'"),
        ),
        Index(
            "document_files_original_sha256_key",
            "document_id",
            "sha256",
            unique=True,
            postgresql_where=text("kind = 'original'"),
            sqlite_where=text("kind = 'original'"),
        ),
        Index(
            "document_files_page_render_slot_key",
            "source_file_id",
            "source_version",
            "page_number",
            unique=True,
            postgresql_where=text("kind = 'page_render' AND source_file_id IS NOT NULL"),
            sqlite_where=text("kind = 'page_render' AND source_file_id IS NOT NULL"),
        ),
        Index(
            "document_files_preview_manifest_key",
            "source_file_id",
            "source_version",
            unique=True,
            postgresql_where=text(
                "mime = 'application/vnd.clerksan.preview-manifest+json' "
                "AND source_file_id IS NOT NULL"
            ),
            sqlite_where=text(
                "mime = 'application/vnd.clerksan.preview-manifest+json' "
                "AND source_file_id IS NOT NULL"
            ),
        ),
        CheckConstraint("version > 0", name="document_files_version_positive"),
        CheckConstraint(
            "source_version IS NULL OR source_version > 0",
            name="document_files_source_version_positive",
        ),
        CheckConstraint(
            "(source_file_id IS NULL) = (source_version IS NULL)",
            name="document_files_source_identity_complete",
        ),
        CheckConstraint(
            "page_number IS NULL OR (kind = 'page_render' AND page_number > 0)",
            name="document_files_page_number_shape",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[FileKind] = mapped_column(_enum_type(FileKind), nullable=False)
    source_file_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_version: Mapped[int | None] = mapped_column(Integer)
    page_number: Mapped[int | None] = mapped_column(Integer)
    content_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    source_filename: Mapped[str] = mapped_column(Text, nullable=False)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    text_provenance: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="files")


class SourceIntake(Base):
    """One durable intake projection for one exact immutable original source."""

    __tablename__ = "source_intakes"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "source_file_id",
            "source_version",
            "source_sha256",
            name="source_intakes_source_identity_key",
        ),
        UniqueConstraint(
            "id",
            "document_id",
            "source_file_id",
            "source_version",
            "source_sha256",
            name="source_intakes_id_source_identity_key",
        ),
        UniqueConstraint(
            "upload_idempotency_key",
            name="source_intakes_upload_idempotency_key_key",
        ),
        ForeignKeyConstraint(
            ("source_file_id", "document_id", "source_version", "source_sha256"),
            (
                "document_files.id",
                "document_files.document_id",
                "document_files.version",
                "document_files.sha256",
            ),
            ondelete="RESTRICT",
            name="source_intakes_exact_source_fkey",
        ),
        Index("source_intakes_document_created_idx", "document_id", "created_at"),
        Index("source_intakes_state_updated_idx", "state", "updated_at"),
        CheckConstraint("source_version > 0", name="source_intakes_source_version_positive"),
        CheckConstraint(
            "state IN ('queued', 'processing', 'processed', 'needs_mapping', "
            "'stored_unprocessed', 'failed')",
            name="source_intakes_state_check",
        ),
        CheckConstraint("length(source_sha256) = 64", name="source_intakes_sha256_length"),
        CheckConstraint("version > 0", name="source_intakes_version_positive"),
        CheckConstraint(
            "(upload_idempotency_key IS NULL AND intent_digest IS NULL) OR "
            "(upload_idempotency_key IS NOT NULL AND intent_digest IS NOT NULL)",
            name="source_intakes_idempotency_binding_complete",
        ),
        CheckConstraint(
            "intent_digest IS NULL OR length(intent_digest) = 64",
            name="source_intakes_intent_digest_length",
        ),
        CheckConstraint(
            "registry_digest IS NULL OR length(registry_digest) = 64",
            name="source_intakes_registry_digest_length",
        ),
        CheckConstraint(
            "capabilities_digest IS NULL OR length(capabilities_digest) = 64",
            name="source_intakes_capabilities_digest_length",
        ),
        CheckConstraint(
            "requirements_digest IS NULL OR length(requirements_digest) = 64",
            name="source_intakes_requirements_digest_length",
        ),
        CheckConstraint(
            "intake_intent IN ('legacy_unspecified', 'generic_file', 'bill_scan')",
            name="source_intakes_intake_intent_check",
        ),
        CheckConstraint(
            "(execution_profile = 'legacy_compat' AND sandbox_verified = false) OR "
            "(execution_profile = 'universal_sandboxed' AND sandbox_verified = true)",
            name="source_intakes_execution_sandbox_consistent",
        ),
        CheckConstraint(
            "duplicate_of_document_id IS NULL OR duplicate_of_document_id <> document_id",
            name="source_intakes_duplicate_not_self",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    source_file_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    duplicate_of_document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT")
    )
    detected_family: Mapped[str | None] = mapped_column(Text)
    detected_format: Mapped[str | None] = mapped_column(Text)
    canonical_mime: Mapped[str | None] = mapped_column(Text)
    detection_evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_VALUE, nullable=False, default=list, server_default=text("'[]'")
    )
    policy_version: Mapped[str] = mapped_column(
        Text, nullable=False, default="legacy-compat-v1", server_default=text("'legacy-compat-v1'")
    )
    registry_digest: Mapped[str | None] = mapped_column(String(64))
    capabilities_digest: Mapped[str | None] = mapped_column(String(64))
    requirements_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default=text("'4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'"),
    )
    required_components: Mapped[list[str]] = mapped_column(
        JSON_VALUE, nullable=False, default=list, server_default=text("'[]'")
    )
    intake_intent: Mapped[IntakeIntent] = mapped_column(
        _enum_type(IntakeIntent),
        nullable=False,
        default=IntakeIntent.LEGACY_UNSPECIFIED,
        server_default=text("'legacy_unspecified'"),
    )
    state: Mapped[SourceIntakeState] = mapped_column(
        _enum_type(SourceIntakeState),
        nullable=False,
        default=SourceIntakeState.QUEUED,
        server_default=text("'queued'"),
    )
    reason_code: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    failure_phase: Mapped[str | None] = mapped_column(Text)
    execution_profile: Mapped[ExecutionProfile] = mapped_column(
        _enum_type(ExecutionProfile),
        nullable=False,
        default=ExecutionProfile.LEGACY_COMPAT,
        server_default=text("'legacy_compat'"),
    )
    sandbox_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    upload_idempotency_key: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    intent_digest: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    document: Mapped[Document] = relationship(
        back_populates="source_intakes", foreign_keys=[document_id]
    )


class UploadIdempotencyReservation(Base):
    """Writer-locking upload key reservation bound atomically to one intake."""

    __tablename__ = "upload_idempotency_reservations"
    __table_args__ = (
        UniqueConstraint(
            "source_intake_id",
            name="upload_idempotency_reservations_source_intake_id_key",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="upload_idempotency_reservations_sha256_length",
        ),
        CheckConstraint(
            "length(intent_digest) = 64",
            name="upload_idempotency_reservations_intent_digest_length",
        ),
    )

    upload_idempotency_key: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_intake_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_intakes.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmbeddedMedia(Base):
    """A source-linked image extracted from a bounded OOXML archive."""

    __tablename__ = "embedded_media"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "source_version",
            "sha256",
            name="embedded_media_document_source_sha256_key",
        ),
        Index("embedded_media_document_idx", "document_id", "created_at"),
        Index(
            "embedded_media_document_source_idx",
            "document_id",
            "source_version",
            "created_at",
        ),
        CheckConstraint("length(sha256) = 64", name="embedded_media_sha256_length"),
        CheckConstraint("source_version > 0", name="embedded_media_source_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    source_location: Mapped[str] = mapped_column(Text, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    ocr_engine: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="embedded_media")


class SchemaMapping(Base):
    """One immutable, bounded mapping contract for a structural schema."""

    __tablename__ = "schema_mappings"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "mapping_version",
            "table_locator",
            "schema_fingerprint",
            name="schema_mappings_exact_contract_key",
        ),
        UniqueConstraint("mapping_digest", name="schema_mappings_mapping_digest_key"),
        Index("schema_mappings_schema_idx", "schema_fingerprint", "created_at"),
        CheckConstraint("mapping_version > 0", name="schema_mappings_version_positive"),
        CheckConstraint(
            "length(schema_fingerprint) = 64",
            name="schema_mappings_schema_fingerprint_length",
        ),
        CheckConstraint(
            "length(mapping_digest) = 64",
            name="schema_mappings_mapping_digest_length",
        ),
        CheckConstraint(
            "length(table_locator) BETWEEN 1 AND 1024",
            name="schema_mappings_table_locator_length",
        ),
        CheckConstraint(
            "length(created_by) BETWEEN 1 AND 255",
            name="schema_mappings_created_by_length",
        ),
        CheckConstraint(
            "record_kind IN ('financial', 'generic_document')",
            name="schema_mappings_record_kind_check",
        ),
        CheckConstraint(
            "(record_kind = 'financial' AND financial_subtype IS NOT NULL) OR "
            "(record_kind = 'generic_document' AND financial_subtype IS NULL)",
            name="schema_mappings_financial_subtype_shape",
        ),
        CheckConstraint(
            "financial_subtype IS NULL OR financial_subtype IN ("
            "'transaction', 'receipt', 'invoice', 'bill', 'recurring_bill', "
            "'quote', 'other_financial')",
            name="schema_mappings_financial_subtype_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    table_locator: Mapped[str] = mapped_column(Text, nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    record_kind: Mapped[RecordKind] = mapped_column(_enum_type(RecordKind), nullable=False)
    financial_subtype: Mapped[FinancialSubtype | None] = mapped_column(_enum_type(FinancialSubtype))
    field_rules: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    required_fields: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    mapping_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    mapping_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MappingSet(Base):
    """An immutable complete locator disposition bound to one exact source."""

    __tablename__ = "mapping_sets"
    __table_args__ = (
        UniqueConstraint(
            "source_intake_id",
            "set_digest",
            name="mapping_sets_source_intake_digest_key",
        ),
        UniqueConstraint(
            "id",
            "version",
            "set_digest",
            "source_intake_id",
            "document_id",
            "source_file_id",
            "source_version",
            "source_sha256",
            name="mapping_sets_exact_identity_key",
        ),
        ForeignKeyConstraint(
            (
                "source_intake_id",
                "document_id",
                "source_file_id",
                "source_version",
                "source_sha256",
            ),
            (
                "source_intakes.id",
                "source_intakes.document_id",
                "source_intakes.source_file_id",
                "source_intakes.source_version",
                "source_intakes.source_sha256",
            ),
            ondelete="RESTRICT",
            name="mapping_sets_exact_source_fkey",
        ),
        Index("mapping_sets_source_created_idx", "source_intake_id", "created_at"),
        CheckConstraint("source_version > 0", name="mapping_sets_source_version_positive"),
        CheckConstraint("version > 0", name="mapping_sets_version_positive"),
        CheckConstraint("length(source_sha256) = 64", name="mapping_sets_source_sha256_length"),
        CheckConstraint(
            "length(structure_fingerprint) = 64",
            name="mapping_sets_structure_fingerprint_length",
        ),
        CheckConstraint("length(set_digest) = 64", name="mapping_sets_digest_length"),
        CheckConstraint(
            "length(created_by) BETWEEN 1 AND 255",
            name="mapping_sets_created_by_length",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_intake_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_file_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    structure_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    set_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MappingSetEntry(Base):
    """One ordered mapped-or-explicitly-ignored locator in a mapping set."""

    __tablename__ = "mapping_set_entries"
    __table_args__ = (
        UniqueConstraint("mapping_set_id", "ordinal", name="mapping_set_entries_set_ordinal_key"),
        UniqueConstraint(
            "mapping_set_id", "table_locator", name="mapping_set_entries_set_locator_key"
        ),
        ForeignKeyConstraint(
            ("mapping_id", "mapping_version", "table_locator", "schema_fingerprint"),
            (
                "schema_mappings.id",
                "schema_mappings.mapping_version",
                "schema_mappings.table_locator",
                "schema_mappings.schema_fingerprint",
            ),
            ondelete="RESTRICT",
            name="mapping_set_entries_exact_mapping_fkey",
        ),
        CheckConstraint(
            "ordinal >= 0 AND ordinal < 256",
            name="mapping_set_entries_ordinal_bounded",
        ),
        CheckConstraint(
            "length(table_locator) BETWEEN 1 AND 1024",
            name="mapping_set_entries_table_locator_length",
        ),
        CheckConstraint(
            "length(schema_fingerprint) = 64",
            name="mapping_set_entries_schema_fingerprint_length",
        ),
        CheckConstraint(
            "((mapping_id IS NOT NULL AND mapping_version IS NOT NULL "
            "AND ignore_reason IS NULL) OR "
            "(mapping_id IS NULL AND mapping_version IS NULL "
            "AND ignore_reason IS NOT NULL))",
            name="mapping_set_entries_mapping_xor_ignore",
        ),
        CheckConstraint(
            "mapping_version IS NULL OR mapping_version > 0",
            name="mapping_set_entries_mapping_version_positive",
        ),
        CheckConstraint(
            "ignore_reason IS NULL OR length(ignore_reason) BETWEEN 1 AND 2048",
            name="mapping_set_entries_ignore_reason_length",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    mapping_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("mapping_sets.id", ondelete="RESTRICT"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    table_locator: Mapped[str] = mapped_column(Text, nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    mapping_version: Mapped[int | None] = mapped_column(Integer)
    ignore_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExtractionBatch(Base):
    """One immutable, exact-source cohort of zero or more review candidates."""

    __tablename__ = "extraction_batches"
    __table_args__ = (
        UniqueConstraint("id", "document_id", name="extraction_batches_id_document_id_key"),
        UniqueConstraint(
            "id",
            "document_id",
            "source_file_id",
            "source_version",
            name="extraction_batches_candidate_source_key",
        ),
        UniqueConstraint(
            "source_intake_id",
            "idempotency_key",
            name="extraction_batches_source_idempotency_key",
        ),
        ForeignKeyConstraint(
            (
                "source_intake_id",
                "document_id",
                "source_file_id",
                "source_version",
                "source_sha256",
            ),
            (
                "source_intakes.id",
                "source_intakes.document_id",
                "source_intakes.source_file_id",
                "source_intakes.source_version",
                "source_intakes.source_sha256",
            ),
            ondelete="RESTRICT",
            name="extraction_batches_exact_source_fkey",
        ),
        ForeignKeyConstraint(
            (
                "mapping_set_id",
                "mapping_set_version",
                "mapping_set_digest",
                "source_intake_id",
                "document_id",
                "source_file_id",
                "source_version",
                "source_sha256",
            ),
            (
                "mapping_sets.id",
                "mapping_sets.version",
                "mapping_sets.set_digest",
                "mapping_sets.source_intake_id",
                "mapping_sets.document_id",
                "mapping_sets.source_file_id",
                "mapping_sets.source_version",
                "mapping_sets.source_sha256",
            ),
            ondelete="RESTRICT",
            name="extraction_batches_exact_mapping_set_fkey",
        ),
        Index("extraction_batches_source_created_idx", "source_intake_id", "created_at"),
        Index(
            "extraction_batches_one_active_document_key",
            "document_id",
            unique=True,
            postgresql_where=text("lifecycle = 'active'"),
            sqlite_where=text("lifecycle = 'active'"),
        ),
        CheckConstraint("source_version > 0", name="extraction_batches_source_version_positive"),
        CheckConstraint(
            "candidate_count >= 0",
            name="extraction_batches_candidate_count_nonnegative",
        ),
        CheckConstraint("version > 0", name="extraction_batches_version_positive"),
        CheckConstraint(
            "length(source_sha256) = 64", name="extraction_batches_source_sha256_length"
        ),
        CheckConstraint(
            "length(normalized_sha256) = 64",
            name="extraction_batches_normalized_sha256_length",
        ),
        CheckConstraint(
            "length(structure_fingerprint) = 64",
            name="extraction_batches_structure_fingerprint_length",
        ),
        CheckConstraint(
            "length(reconciliation_digest) = 64",
            name="extraction_batches_reconciliation_digest_length",
        ),
        CheckConstraint(
            "mapping_set_digest IS NULL OR length(mapping_set_digest) = 64",
            name="extraction_batches_mapping_set_digest_length",
        ),
        CheckConstraint(
            "(mapping_set_id IS NULL AND mapping_set_version IS NULL "
            "AND mapping_set_digest IS NULL) OR "
            "(mapping_set_id IS NOT NULL AND mapping_set_version IS NOT NULL "
            "AND mapping_set_digest IS NOT NULL)",
            name="extraction_batches_mapping_set_identity_complete",
        ),
        CheckConstraint(
            "mapping_set_version IS NULL OR mapping_set_version > 0",
            name="extraction_batches_mapping_set_version_positive",
        ),
        CheckConstraint(
            "intake_intent IN ('legacy_unspecified', 'generic_file', 'bill_scan')",
            name="extraction_batches_intake_intent_check",
        ),
        CheckConstraint(
            "lifecycle IN ('open', 'ready_to_activate', 'active', 'superseded', 'rejected')",
            name="extraction_batches_lifecycle_check",
        ),
        CheckConstraint(
            "length(producer) BETWEEN 1 AND 255",
            name="extraction_batches_producer_length",
        ),
        CheckConstraint(
            "length(producer_version) BETWEEN 1 AND 255",
            name="extraction_batches_producer_version_length",
        ),
        CheckConstraint("length(origin) BETWEEN 1 AND 64", name="extraction_batches_origin_length"),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name="extraction_batches_idempotency_key_length",
        ),
        CheckConstraint(
            "activation_vector_sha256 IS NULL OR length(activation_vector_sha256) = 64",
            name="extraction_batches_activation_vector_length",
        ),
        CheckConstraint(
            "activated_by IS NULL OR length(trim(activated_by)) BETWEEN 1 AND 255",
            name="extraction_batches_activated_by_length",
        ),
        CheckConstraint(
            "(activation_vector_sha256 IS NULL AND activated_by IS NULL "
            "AND activated_at IS NULL AND activation_included_count IS NULL "
            "AND activation_excluded_count IS NULL AND accepted_exclusions = false "
            "AND accepted_empty = false) OR "
            "(activation_vector_sha256 IS NOT NULL AND activated_by IS NOT NULL "
            "AND activated_at IS NOT NULL AND activation_included_count IS NOT NULL "
            "AND activation_excluded_count IS NOT NULL)",
            name="extraction_batches_activation_metadata_complete",
        ),
        CheckConstraint(
            "activation_included_count IS NULL OR activation_included_count >= 0",
            name="extraction_batches_activation_included_nonnegative",
        ),
        CheckConstraint(
            "activation_excluded_count IS NULL OR activation_excluded_count >= 0",
            name="extraction_batches_activation_excluded_nonnegative",
        ),
        CheckConstraint(
            "activation_vector_sha256 IS NULL OR "
            "activation_included_count + activation_excluded_count = candidate_count",
            name="extraction_batches_activation_counts_reconcile",
        ),
        CheckConstraint(
            "activation_vector_sha256 IS NULL OR "
            "((candidate_count = 0 AND accepted_empty = true) OR "
            "(candidate_count > 0 AND accepted_empty = false))",
            name="extraction_batches_empty_activation_consent",
        ),
        CheckConstraint(
            "activation_vector_sha256 IS NULL OR "
            "((activation_excluded_count > 0 AND accepted_exclusions = true) OR "
            "(activation_excluded_count = 0 AND accepted_exclusions = false))",
            name="extraction_batches_exclusion_activation_consent",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_intake_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_file_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    structure_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_set_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    mapping_set_version: Mapped[int | None] = mapped_column(Integer)
    mapping_set_digest: Mapped[str | None] = mapped_column(String(64))
    producer: Mapped[str] = mapped_column(Text, nullable=False)
    producer_version: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    intake_intent: Mapped[IntakeIntent] = mapped_column(_enum_type(IntakeIntent), nullable=False)
    lifecycle: Mapped[BatchLifecycle] = mapped_column(
        _enum_type(BatchLifecycle), nullable=False, default=BatchLifecycle.OPEN
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    producer_job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"))
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reconciliation_counts: Mapped[dict[str, int]] = mapped_column(JSON_VALUE, nullable=False)
    reconciliation_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    activation_vector_sha256: Mapped[str | None] = mapped_column(String(64))
    activated_by: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activation_included_count: Mapped[int | None] = mapped_column(Integer)
    activation_excluded_count: Mapped[int | None] = mapped_column(Integer)
    accepted_exclusions: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    accepted_empty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExtractedRecord(Base):
    __tablename__ = "extracted_records"
    __table_args__ = (
        UniqueConstraint("id", "document_id", name="extracted_records_id_document_id_key"),
        UniqueConstraint("id", "batch_id", name="extracted_records_id_batch_id_key"),
        ForeignKeyConstraint(
            ("source_file_id", "document_id"),
            ("document_files.id", "document_files.document_id"),
            ondelete="RESTRICT",
            name="extracted_records_source_file_document_fkey",
        ),
        ForeignKeyConstraint(
            ("batch_id", "document_id", "source_file_id", "source_version"),
            (
                "extraction_batches.id",
                "extraction_batches.document_id",
                "extraction_batches.source_file_id",
                "extraction_batches.source_version",
            ),
            ondelete="RESTRICT",
            name="extracted_records_exact_batch_source_fkey",
        ),
        UniqueConstraint(
            "id",
            "batch_id",
            "document_id",
            "candidate_key",
            "record_kind",
            "source_file_id",
            "source_version",
            name="extracted_records_chunk_lineage_key",
        ),
        Index("extracted_records_document_created_idx", "document_id", "created_at"),
        Index(
            "extracted_records_batch_ordinal_key",
            "batch_id",
            "candidate_ordinal",
            unique=True,
            postgresql_where=text("batch_id IS NOT NULL"),
            sqlite_where=text("batch_id IS NOT NULL"),
        ),
        Index(
            "extracted_records_batch_candidate_key",
            "batch_id",
            "candidate_key",
            unique=True,
            postgresql_where=text("batch_id IS NOT NULL"),
            sqlite_where=text("batch_id IS NOT NULL"),
        ),
        CheckConstraint("version > 0", name="extracted_records_version_positive"),
        CheckConstraint("source_version > 0", name="extracted_records_source_version_positive"),
        CheckConstraint(
            "candidate_ordinal IS NULL OR candidate_ordinal > 0",
            name="extracted_records_candidate_ordinal_positive",
        ),
        CheckConstraint(
            "candidate_key IS NULL OR length(candidate_key) = 64",
            name="extracted_records_candidate_key_length",
        ),
        CheckConstraint(
            "row_fingerprint IS NULL OR length(row_fingerprint) = 64",
            name="extracted_records_row_fingerprint_length",
        ),
        CheckConstraint(
            "record_kind IS NULL OR record_kind IN ('financial', 'generic_document')",
            name="extracted_records_record_kind_check",
        ),
        CheckConstraint(
            "financial_subtype IS NULL OR financial_subtype IN ("
            "'transaction', 'receipt', 'invoice', 'bill', 'recurring_bill', "
            "'quote', 'other_financial')",
            name="extracted_records_financial_subtype_check",
        ),
        CheckConstraint(
            "batch_id IS NULL OR ((record_kind = 'financial' AND financial_subtype IS NOT NULL) "
            "OR (record_kind = 'generic_document' AND financial_subtype IS NULL))",
            name="extracted_records_financial_subtype_shape",
        ),
        CheckConstraint(
            "(batch_id IS NULL AND candidate_ordinal IS NULL AND candidate_key IS NULL "
            "AND record_kind IS NULL AND financial_subtype IS NULL "
            "AND source_locator IS NULL AND row_fingerprint IS NULL "
            "AND validation_issues IS NULL AND evidence_group_keys IS NULL) OR "
            "(batch_id IS NOT NULL AND candidate_ordinal IS NOT NULL "
            "AND candidate_key IS NOT NULL AND record_kind IS NOT NULL "
            "AND source_locator IS NOT NULL AND row_fingerprint IS NOT NULL "
            "AND validation_issues IS NOT NULL AND evidence_group_keys IS NOT NULL)",
            name="extracted_records_batch_lineage_complete",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    source_file_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    candidate_ordinal: Mapped[int | None] = mapped_column(Integer)
    candidate_key: Mapped[str | None] = mapped_column(String(64))
    record_kind: Mapped[RecordKind | None] = mapped_column(_enum_type(RecordKind))
    financial_subtype: Mapped[FinancialSubtype | None] = mapped_column(_enum_type(FinancialSubtype))
    source_locator: Mapped[str | None] = mapped_column(Text)
    row_fingerprint: Mapped[str | None] = mapped_column(String(64))
    validation_issues: Mapped[list[str] | None] = mapped_column(JSON_VALUE)
    evidence_group_keys: Mapped[list[str] | None] = mapped_column(JSON_VALUE)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    field_confidences: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    source_spans: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ExtractionStatus] = mapped_column(
        _enum_type(ExtractionStatus), nullable=False, default=ExtractionStatus.PENDING_REVIEW
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reviewer: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="extractions")
    verified_records: Mapped[list[VerifiedRecord]] = relationship(
        primaryjoin=(
            "and_(ExtractedRecord.id == VerifiedRecord.extracted_id, "
            "ExtractedRecord.document_id == VerifiedRecord.document_id)"
        ),
        foreign_keys="[VerifiedRecord.extracted_id, VerifiedRecord.document_id]",
        viewonly=True,
    )


class CandidateReviewDecision(Base):
    """One immutable reviewer decision for one exact candidate revision."""

    __tablename__ = "candidate_review_decisions"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "batch_id",
            "extraction_id",
            name="candidate_review_decisions_exact_identity_key",
        ),
        UniqueConstraint(
            "batch_id",
            "extraction_id",
            "decision_revision",
            name="candidate_review_decisions_linear_revision_key",
        ),
        UniqueConstraint(
            "supersedes_decision_id",
            name="candidate_review_decisions_one_successor_key",
        ),
        ForeignKeyConstraint(
            ("extraction_id", "batch_id"),
            ("extracted_records.id", "extracted_records.batch_id"),
            ondelete="RESTRICT",
            name="candidate_review_decisions_exact_candidate_fkey",
        ),
        ForeignKeyConstraint(
            ("supersedes_decision_id", "batch_id", "extraction_id"),
            (
                "candidate_review_decisions.id",
                "candidate_review_decisions.batch_id",
                "candidate_review_decisions.extraction_id",
            ),
            ondelete="RESTRICT",
            name="candidate_review_decisions_exact_predecessor_fkey",
        ),
        Index(
            "candidate_review_decisions_latest_idx",
            "batch_id",
            "extraction_id",
            "decision_revision",
        ),
        Index("candidate_review_decisions_batch_created_idx", "batch_id", "created_at"),
        CheckConstraint(
            "decision_revision > 0",
            name="candidate_review_decisions_revision_positive",
        ),
        CheckConstraint(
            "expected_extraction_version > 0",
            name="candidate_review_decisions_expected_version_positive",
        ),
        CheckConstraint(
            "action IN ('include', 'exclude')",
            name="candidate_review_decisions_action_check",
        ),
        CheckConstraint(
            "corrected_financial_subtype IS NULL OR corrected_financial_subtype IN ("
            "'transaction', 'receipt', 'invoice', 'bill', 'recurring_bill', "
            "'quote', 'other_financial')",
            name="candidate_review_decisions_subtype_check",
        ),
        CheckConstraint(
            "corrected_payload IS NULL OR length(CAST(corrected_payload AS VARCHAR)) <= 65536",
            name="candidate_review_decisions_payload_bounded",
        ),
        CheckConstraint(
            "exclusion_reason IS NULL OR length(trim(exclusion_reason)) BETWEEN 1 AND 2048",
            name="candidate_review_decisions_reason_length",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 255",
            name="candidate_review_decisions_actor_length",
        ),
        CheckConstraint(
            "(action = 'include' AND exclusion_reason IS NULL) OR "
            "(action = 'exclude' AND corrected_payload IS NULL "
            "AND corrected_financial_subtype IS NULL AND exclusion_reason IS NOT NULL)",
            name="candidate_review_decisions_action_payload_shape",
        ),
        CheckConstraint(
            "(decision_revision = 1 AND supersedes_decision_id IS NULL) OR "
            "(decision_revision > 1 AND supersedes_decision_id IS NOT NULL)",
            name="candidate_review_decisions_predecessor_shape",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_batches.id", ondelete="RESTRICT"), nullable=False
    )
    extraction_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    decision_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_extraction_version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_decision_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    action: Mapped[CandidateDecisionAction] = mapped_column(
        _enum_type(CandidateDecisionAction), nullable=False
    )
    corrected_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    corrected_financial_subtype: Mapped[FinancialSubtype | None] = mapped_column(
        _enum_type(FinancialSubtype)
    )
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VerifiedRecord(Base):
    __tablename__ = "verified_records"
    __table_args__ = (
        UniqueConstraint("extracted_id", name="verified_records_extracted_id_key"),
        ForeignKeyConstraint(
            ("extracted_id", "document_id"),
            ("extracted_records.id", "extracted_records.document_id"),
            ondelete="RESTRICT",
            name="verified_records_extracted_document_fkey",
        ),
        Index("verified_records_transaction_date_idx", "transaction_date"),
        Index("verified_records_total_amount_idx", "total_amount"),
        Index("verified_records_counterparty_idx", "counterparty"),
        Index("verified_records_expense_kind_date_idx", "expense_kind", "transaction_date"),
        Index("verified_records_due_date_idx", "due_date"),
        Index(
            "verified_records_combined_search_idx",
            "counterparty",
            "transaction_date",
            "total_amount",
        ),
        Index("verified_records_document_idx", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    extracted_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    counterparty: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str | None] = mapped_column(String(16))
    category: Mapped[str | None] = mapped_column(Text)
    expense_kind: Mapped[ExpenseKind | None] = mapped_column(_enum_type(ExpenseKind))
    due_date: Mapped[date | None] = mapped_column(Date)
    registration_number: Mapped[str | None] = mapped_column(String(14))
    tax_8_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    tax_10_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    reviewer: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="verified_records")
    extraction: Mapped[ExtractedRecord] = relationship(
        primaryjoin=(
            "and_(VerifiedRecord.extracted_id == ExtractedRecord.id, "
            "VerifiedRecord.document_id == ExtractedRecord.document_id)"
        ),
        foreign_keys=[extracted_id, document_id],
        viewonly=True,
    )
    recurring_bills: Mapped[list[RecurringBill]] = relationship(
        back_populates="verified_record", cascade="save-update, merge"
    )


class AuditEntry(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("audit_log_table_row_at_idx", "table_name", "row_pk", "at"),)

    id: Mapped[int] = mapped_column(AUDIT_ID, Identity(), primary_key=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    row_pk: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    field: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "idempotency_key",
            name="jobs_document_id_idempotency_key_key",
        ),
        Index("jobs_document_created_idx", "document_id", "created_at"),
        CheckConstraint("attempts >= 0", name="jobs_attempts_nonnegative"),
        CheckConstraint(
            "execution_profile IN ('legacy_compat', 'universal_sandboxed')",
            name="jobs_execution_profile_check",
        ),
        CheckConstraint(
            "registry_digest IS NULL OR length(registry_digest) = 64",
            name="jobs_registry_digest_length",
        ),
        CheckConstraint(
            "capabilities_digest IS NULL OR length(capabilities_digest) = 64",
            name="jobs_capabilities_digest_length",
        ),
        CheckConstraint(
            "requirements_digest IS NULL OR length(requirements_digest) = 64",
            name="jobs_requirements_digest_length",
        ),
        CheckConstraint(
            "intake_intent IN ('legacy_unspecified', 'generic_file', 'bill_scan')",
            name="jobs_intake_intent_check",
        ),
        CheckConstraint(
            "(execution_profile = 'legacy_compat' AND sandbox_verified = false) OR "
            "(execution_profile = 'universal_sandboxed' AND sandbox_verified = true)",
            name="jobs_execution_sandbox_consistent",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    execution_profile: Mapped[ExecutionProfile] = mapped_column(
        _enum_type(ExecutionProfile),
        nullable=False,
        default=ExecutionProfile.LEGACY_COMPAT,
        server_default=text("'legacy_compat'"),
    )
    sandbox_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    registry_digest: Mapped[str | None] = mapped_column(String(64))
    capabilities_digest: Mapped[str | None] = mapped_column(String(64))
    requirements_digest: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        server_default=text("'4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'"),
    )
    required_components: Mapped[list[str]] = mapped_column(
        JSON_VALUE, nullable=False, default=list, server_default=text("'[]'")
    )
    intake_intent: Mapped[IntakeIntent] = mapped_column(
        _enum_type(IntakeIntent),
        nullable=False,
        default=IntakeIntent.LEGACY_UNSPECIFIED,
        server_default=text("'legacy_unspecified'"),
    )
    status: Mapped[JobStatus] = mapped_column(
        _enum_type(JobStatus), nullable=False, default=JobStatus.QUEUED
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="jobs")


class WorkerCapabilityLease(Base):
    """Expiring worker evidence for one canonical capability registry."""

    __tablename__ = "worker_capability_leases"
    __table_args__ = (
        Index("worker_capability_leases_expires_idx", "expires_at"),
        CheckConstraint(
            "length(registry_digest) = 64",
            name="worker_capability_leases_registry_digest_length",
        ),
        CheckConstraint(
            "length(capabilities_digest) = 64",
            name="worker_capability_leases_capabilities_digest_length",
        ),
        CheckConstraint(
            "expires_at > heartbeat_at",
            name="worker_capability_leases_expiry_after_heartbeat",
        ),
    )

    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    registry_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    capabilities_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    sandbox_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Chunk(Base):
    """A model-pinned semantic chunk derived from one immutable document version."""

    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ("batch_id", "document_id", "source_file_id", "source_version"),
            (
                "extraction_batches.id",
                "extraction_batches.document_id",
                "extraction_batches.source_file_id",
                "extraction_batches.source_version",
            ),
            ondelete="RESTRICT",
            name="chunks_exact_batch_source_fkey",
        ),
        ForeignKeyConstraint(
            (
                "extraction_id",
                "batch_id",
                "document_id",
                "candidate_key",
                "record_kind",
                "source_file_id",
                "source_version",
            ),
            (
                "extracted_records.id",
                "extracted_records.batch_id",
                "extracted_records.document_id",
                "extracted_records.candidate_key",
                "extracted_records.record_kind",
                "extracted_records.source_file_id",
                "extracted_records.source_version",
            ),
            ondelete="RESTRICT",
            name="chunks_exact_candidate_lineage_fkey",
        ),
        Index("chunks_document_seq_idx", "document_id", "seq"),
        Index(
            "chunks_legacy_document_seq_key",
            "document_id",
            "seq",
            unique=True,
            postgresql_where=text("batch_id IS NULL"),
            sqlite_where=text("batch_id IS NULL"),
        ),
        Index(
            "chunks_batch_candidate_seq_key",
            "batch_id",
            "extraction_id",
            "seq",
            unique=True,
            postgresql_where=text("batch_id IS NOT NULL"),
            sqlite_where=text("batch_id IS NOT NULL"),
        ),
        CheckConstraint("seq >= 0", name="chunks_seq_nonnegative"),
        CheckConstraint("token_count > 0", name="chunks_token_count_positive"),
        CheckConstraint(
            "record_kind IS NULL OR record_kind IN ('financial', 'generic_document')",
            name="chunks_record_kind_check",
        ),
        CheckConstraint(
            "source_version IS NULL OR source_version > 0",
            name="chunks_source_version_positive",
        ),
        CheckConstraint(
            "(batch_id IS NULL AND extraction_id IS NULL AND record_kind IS NULL "
            "AND source_file_id IS NULL AND source_version IS NULL AND candidate_key IS NULL) "
            "OR (batch_id IS NOT NULL AND extraction_id IS NOT NULL "
            "AND record_kind IS NOT NULL AND source_file_id IS NOT NULL "
            "AND source_version IS NOT NULL AND candidate_key IS NOT NULL)",
            name="chunks_candidate_lineage_complete",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    extraction_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    record_kind: Mapped[RecordKind | None] = mapped_column(_enum_type(RecordKind))
    source_file_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_version: Mapped[int | None] = mapped_column(Integer)
    candidate_key: Mapped[str | None] = mapped_column(String(64))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(EMBEDDING_VALUE, nullable=False)
    embed_model: Mapped[str] = mapped_column(Text, nullable=False)
    embed_model_digest: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="chunks")


class SpreadsheetRow(Base):
    """One SQL-stageable spreadsheet row; it is intentionally never a RAG chunk."""

    __tablename__ = "spreadsheet_rows"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "source_version",
            "source_location",
            "row_index",
            name="spreadsheet_rows_document_source_version_row_key",
        ),
        Index("spreadsheet_rows_document_source_idx", "document_id", "source_location"),
        Index(
            "spreadsheet_rows_document_version_source_idx",
            "document_id",
            "source_version",
            "source_location",
        ),
        CheckConstraint("row_index > 0", name="spreadsheet_rows_row_index_positive"),
        CheckConstraint("source_version > 0", name="spreadsheet_rows_source_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_location: Mapped[str] = mapped_column(Text, nullable=False)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    values: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    value_types: Mapped[dict[str, str]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="spreadsheet_rows")


class Issuer(Base):
    """A normalized recurring-bill issuer."""

    __tablename__ = "issuers"
    __table_args__ = (UniqueConstraint("name", name="issuers_name_key"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[IssuerKind] = mapped_column(
        _enum_type(IssuerKind), nullable=False, default=IssuerKind.OTHER
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    recurring_bills: Mapped[list[RecurringBill]] = relationship(
        back_populates="issuer",
        cascade="save-update, merge",
        order_by="RecurringBill.billing_period",
    )


class RecurringBill(Base):
    """A reviewed bill period linked to its verified source record."""

    __tablename__ = "recurring_bills"
    __table_args__ = (
        UniqueConstraint("verified_record_id", name="recurring_bills_verified_record_id_key"),
        Index(
            "recurring_bills_active_issuer_period_key",
            "issuer_id",
            "billing_period",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
            sqlite_where=text("superseded_at IS NULL"),
        ),
        Index("recurring_bills_issuer_period_idx", "issuer_id", "billing_period"),
        Index("recurring_bills_payment_due_idx", "payment_status", "due_date"),
        CheckConstraint("amount > 0", name="recurring_bills_amount_positive"),
        CheckConstraint(
            "consumption_value IS NULL OR consumption_value > 0",
            name="recurring_bills_consumption_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    issuer_id: Mapped[UUID] = mapped_column(
        ForeignKey("issuers.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    verified_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("verified_records.id", ondelete="RESTRICT"), nullable=False
    )
    billing_period: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    payment_status: Mapped[PaymentStatus] = mapped_column(
        _enum_type(PaymentStatus), nullable=False, default=PaymentStatus.UNPAID
    )
    consumption_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    consumption_unit: Mapped[str | None] = mapped_column(Text)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer: Mapped[str | None] = mapped_column(Text)
    review_corrections: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    issuer: Mapped[Issuer] = relationship(back_populates="recurring_bills")
    document: Mapped[Document] = relationship(back_populates="recurring_bills")
    verified_record: Mapped[VerifiedRecord] = relationship(back_populates="recurring_bills")


class DuplicateFlag(Base):
    """Non-destructive evidence that two documents may represent the same source."""

    __tablename__ = "duplicate_flags"
    __table_args__ = (
        ForeignKeyConstraint(
            ("source_file_id", "document_id", "source_version"),
            ("document_files.id", "document_files.document_id", "document_files.version"),
            ondelete="RESTRICT",
            name="duplicate_flags_exact_source_fkey",
        ),
        ForeignKeyConstraint(
            (
                "extraction_id",
                "batch_id",
                "document_id",
                "candidate_key",
                "record_kind",
                "source_file_id",
                "source_version",
            ),
            (
                "extracted_records.id",
                "extracted_records.batch_id",
                "extracted_records.document_id",
                "extracted_records.candidate_key",
                "extracted_records.record_kind",
                "extracted_records.source_file_id",
                "extracted_records.source_version",
            ),
            ondelete="RESTRICT",
            name="duplicate_flags_exact_candidate_fkey",
        ),
        Index("duplicate_flags_document_idx", "document_id", "created_at"),
        Index(
            "duplicate_flags_document_scope_key",
            "document_id",
            "suspected_document_id",
            unique=True,
            postgresql_where=text("source_file_id IS NULL"),
            sqlite_where=text("source_file_id IS NULL"),
        ),
        Index(
            "duplicate_flags_source_scope_key",
            "document_id",
            "suspected_document_id",
            "source_file_id",
            "source_version",
            unique=True,
            postgresql_where=text("source_file_id IS NOT NULL AND batch_id IS NULL"),
            sqlite_where=text("source_file_id IS NOT NULL AND batch_id IS NULL"),
        ),
        Index(
            "duplicate_flags_candidate_scope_key",
            "document_id",
            "suspected_document_id",
            "batch_id",
            "extraction_id",
            unique=True,
            postgresql_where=text("batch_id IS NOT NULL"),
            sqlite_where=text("batch_id IS NOT NULL"),
        ),
        CheckConstraint(
            "document_id <> suspected_document_id", name="duplicate_flags_distinct_documents"
        ),
        CheckConstraint("score >= 0 AND score <= 1", name="duplicate_flags_score_range"),
        CheckConstraint(
            "(source_file_id IS NULL AND source_version IS NULL "
            "AND batch_id IS NULL AND extraction_id IS NULL "
            "AND candidate_key IS NULL AND record_kind IS NULL) OR "
            "(source_file_id IS NOT NULL AND source_version IS NOT NULL "
            "AND batch_id IS NULL AND extraction_id IS NULL "
            "AND candidate_key IS NULL AND record_kind IS NULL) OR "
            "(source_file_id IS NOT NULL AND source_version IS NOT NULL "
            "AND batch_id IS NOT NULL AND extraction_id IS NOT NULL "
            "AND candidate_key IS NOT NULL AND record_kind IS NOT NULL)",
            name="duplicate_flags_scope_shape",
        ),
        CheckConstraint(
            "source_version IS NULL OR source_version > 0",
            name="duplicate_flags_source_version_positive",
        ),
        CheckConstraint(
            "candidate_key IS NULL OR length(candidate_key) = 64",
            name="duplicate_flags_candidate_key_length",
        ),
        CheckConstraint(
            "record_kind IS NULL OR record_kind IN ('financial', 'generic_document')",
            name="duplicate_flags_record_kind_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    suspected_document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    source_file_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_version: Mapped[int | None] = mapped_column(Integer)
    batch_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    extraction_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    candidate_key: Mapped[str | None] = mapped_column(String(64))
    record_kind: Mapped[RecordKind | None] = mapped_column(_enum_type(RecordKind))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    document: Mapped[Document] = relationship(
        back_populates="duplicate_flags", foreign_keys=[document_id]
    )
    suspected_document: Mapped[Document] = relationship(
        back_populates="suspected_by_duplicate_flags", foreign_keys=[suspected_document_id]
    )
