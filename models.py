from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass
class CatalogRecord:
    local_folder: str = ""
    local_path: str = ""
    filename: str = ""
    file_size: int = 0
    page_count: int = 0
    pdf_title: str = ""
    pdf_author: str = ""
    detected_title: str = ""
    detected_author: str = ""
    year: str = ""
    publisher: str = ""
    isbn_10: str = ""
    isbn_13: str = ""
    doi: str = ""
    language: str = ""
    text_sample: str = ""
    scan_error: str = ""

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MatchRecord:
    local_folder: str = ""
    local_path: str = ""
    filename: str = ""
    detected_title: str = ""
    detected_author: str = ""
    isbn_10: str = ""
    isbn_13: str = ""
    doi: str = ""
    ocaid: str = ""
    lccn: str = ""
    year: str = ""
    publisher: str = ""
    ol_work_key: str = ""
    ol_edition_key: str = ""
    ol_title: str = ""
    ol_author: str = ""
    confidence: str = ""
    status: str = ""
    notes: str = ""
    candidates_json: str = ""
    create_record: str = ""

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FolderRecord:
    list_name: str = ""
    local_folder_path: str = ""
    pdf_count: int = 0

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EnrichmentRecord:
    local_folder: str = ""
    local_path: str = ""
    filename: str = ""
    previous_status: str = ""
    verification_status: str = ""
    source: str = ""
    document_type: str = ""
    canonical_title: str = ""
    canonical_author: str = ""
    canonical_publisher: str = ""
    canonical_year: str = ""
    doi: str = ""
    ocaid: str = ""
    lccn: str = ""
    isbn_10: str = ""
    isbn_13: str = ""
    external_url: str = ""
    ol_work_key: str = ""
    ol_edition_key: str = ""
    confidence: str = ""
    duplicate_of_path: str = ""
    parent_publication_id: str = ""
    publication_role: str = ""
    notes: str = ""
    candidates_json: str = ""

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerifiedMetadataRecord:
    local_folder: str = ""
    filename_pattern: str = ""
    parent_publication_id: str = ""
    publication_role: str = ""
    canonical_title: str = ""
    canonical_author: str = ""
    canonical_publisher: str = ""
    canonical_year: str = ""
    isbn_10: str = ""
    isbn_13: str = ""
    doi: str = ""
    ocaid: str = ""
    lccn: str = ""
    document_type: str = ""
    source_url: str = ""
    notes: str = ""

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkInventoryRecord:
    local_work_id: str = ""
    work_group_id: str = ""
    mapping_status: str = ""
    mapping_confidence: str = ""
    identity_source: str = ""
    verification_status: str = ""
    canonical_title: str = ""
    canonical_author: str = ""
    canonical_publisher: str = ""
    canonical_year: str = ""
    isbn_10: str = ""
    isbn_13: str = ""
    doi: str = ""
    ocaid: str = ""
    lccn: str = ""
    primary_identifier_type: str = ""
    primary_identifier: str = ""
    document_type: str = ""
    external_url: str = ""
    ol_work_key: str = ""
    ol_edition_key: str = ""
    openlibrary_action: str = ""
    local_pdf_count: int = 0
    local_folders: str = ""
    local_paths_json: str = ""
    notes: str = ""

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ListMembershipRecord:
    list_name: str = ""
    local_work_id: str = ""
    canonical_title: str = ""
    document_type: str = ""
    mapping_status: str = ""
    mapping_confidence: str = ""
    primary_identifier_type: str = ""
    primary_identifier: str = ""
    ol_work_key: str = ""
    openlibrary_action: str = ""
    local_pdf_count: int = 0
    local_paths_json: str = ""

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecoveryRecord:
    local_folder: str = ""
    local_path: str = ""
    filename: str = ""
    pages_examined: str = ""
    recovered_title: str = ""
    recovered_author: str = ""
    recovered_publisher: str = ""
    recovered_year: str = ""
    isbn_10: str = ""
    isbn_13: str = ""
    doi: str = ""
    report_numbers: str = ""
    language: str = ""
    text_sample: str = ""
    recovery_status: str = ""
    notes: str = ""

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuthorityCandidateRecord:
    local_work_id: str = ""
    local_folders: str = ""
    document_type: str = ""
    canonical_title: str = ""
    canonical_author: str = ""
    canonical_year: str = ""
    source: str = ""
    candidate_title: str = ""
    candidate_author: str = ""
    candidate_publisher: str = ""
    candidate_year: str = ""
    identifier_type: str = ""
    identifier: str = ""
    source_url: str = ""
    title_similarity: str = ""
    author_similarity: str = ""
    confidence: str = ""
    recommendation: str = ""
    notes: str = ""

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImportPlanRecord:
    local_work_id: str = ""
    local_folders: str = ""
    local_pdf_count: int = 0
    canonical_title: str = ""
    canonical_author: str = ""
    canonical_publisher: str = ""
    canonical_year: str = ""
    document_type: str = ""
    verification_status: str = ""
    ol_work_key: str = ""
    external_url: str = ""
    primary_identifier_type: str = ""
    primary_identifier: str = ""
    openlibrary_eligibility: str = ""
    preparation_status: str = ""
    missing_metadata: str = ""
    suggested_queries: str = ""
    notes: str = ""

    @classmethod
    def fieldnames(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
