#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import tomllib
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

import requests

from enrichment import (
    MetadataAPI, apply_enrichment, enrich_record, filename_title,
    select_verified_metadata,
)
from matcher import choose_match, parse_search_results
from models import (
    AuthorityCandidateRecord, CatalogRecord, EnrichmentRecord, FolderRecord,
    ImportPlanRecord, ListMembershipRecord, MatchRecord, RecoveryRecord,
    VerifiedMetadataRecord, WorkInventoryRecord,
)
from openlibrary_api import OpenLibraryAPI
from pdf_metadata import discover_folders, scan_library
from recovery import recover_pdf_metadata

LOG = logging.getLogger("library_importer")
HERE = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = HERE / "data"


def read_config(path: Path | None) -> dict[str, Any]:
    config: dict[str, Any] = {}
    candidate = path or HERE / "config.toml"
    if candidate.exists():
        with candidate.open("rb") as handle:
            config = tomllib.load(handle)
    config.setdefault("openlibrary", {})
    config.setdefault("paths", {})
    if os.getenv("OPENLIBRARY_USERNAME"):
        config["openlibrary"]["username"] = os.environ["OPENLIBRARY_USERNAME"]
    if os.getenv("USER_AGENT_EMAIL"):
        config["openlibrary"]["user_agent_email"] = os.environ["USER_AGENT_EMAIL"]
    if os.getenv("LIBRARY_ROOT"):
        config["paths"]["library_root"] = os.environ["LIBRARY_ROOT"]
    return config


def user_agent(config: dict[str, Any]) -> str:
    email = str(config.get("openlibrary", {}).get("user_agent_email", "")).strip()
    contact = f"; contact: {email}" if email else ""
    return f"OpenLibraryLocalImporter/0.1 (personal metadata organizer{contact})"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def catalog_from_row(row: dict[str, str]) -> CatalogRecord:
    values: dict[str, Any] = {key: row.get(key, "") for key in CatalogRecord.fieldnames()}
    for key in ("file_size", "page_count"):
        try:
            values[key] = int(values[key] or 0)
        except ValueError:
            values[key] = 0
    return CatalogRecord(**values)


def make_api(config: dict[str, Any], data_dir: Path, *, authenticated: bool = False) -> OpenLibraryAPI:
    kwargs = {
        "base_url": str(config.get("openlibrary", {}).get("base_url", "https://openlibrary.org")),
        "user_agent": user_agent(config),
        "cache_dir": data_dir / "cache" / "search",
        "delay": float(config.get("openlibrary", {}).get("request_delay_seconds", 0.35)),
    }
    return OpenLibraryAPI.authenticated(**kwargs) if authenticated else OpenLibraryAPI(**kwargs)


def command_auth_check(args: argparse.Namespace, config: dict[str, Any]) -> int:
    try:
        api = make_api(config, args.data_dir, authenticated=True)
        username = api.username()
        if not username:
            print("Authentication cookie is present, but the username could not be determined.")
            return 1
        configured = str(config.get("openlibrary", {}).get("username", "")).strip()
        print(f"Authenticated Open Library user: {username}")
        if configured and configured != username:
            print(f"WARNING: configured username {configured!r} differs from authenticated user {username!r}.")
            return 1
        return 0
    except Exception as exc:
        print(f"Authentication unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def command_scan(args: argparse.Namespace, config: dict[str, Any]) -> int:
    root_value = args.library_root or config.get("paths", {}).get("library_root")
    if not root_value:
        raise ValueError("Provide a library path or set paths.library_root / LIBRARY_ROOT")
    root = Path(root_value).expanduser()
    folders = discover_folders(root)
    records = []
    for index, record in enumerate(scan_library(root), start=1):
        records.append(record.to_dict())
        if args.verbose:
            LOG.info("Scanned %d: %s / %s", index, record.local_folder, record.filename)
    output = args.data_dir / "catalog.csv"
    write_csv(output, CatalogRecord.fieldnames(), records)
    lists_path = args.data_dir / "lists.csv"
    write_csv(lists_path, FolderRecord.fieldnames(), (folder.to_dict() for folder in folders))
    errors = sum(bool(row["scan_error"]) for row in records)
    print(f"Scanned {len(records)} PDFs across {len(folders)} folders/lists -> {output}")
    print(f"List manifest -> {lists_path}")
    if errors:
        print(f"{errors} PDFs had extraction errors; see scan_error in the catalog.")
    return 0


def _search_queries(record: CatalogRecord) -> list[str]:
    queries = []
    if record.isbn_13:
        queries.append(f"isbn:{record.isbn_13}")
    if record.isbn_10:
        queries.append(f"isbn:{record.isbn_10}")
    if record.doi:
        queries.append(f'"{record.doi}"')
    safe_title = record.detected_title.replace('"', " ").strip()
    safe_author = record.detected_author.replace('"', " ").strip()
    if safe_title and safe_author:
        queries.append(f'title:"{safe_title}" author:"{safe_author}"')
    if safe_title:
        extra = f" publish_year:{record.year}" if record.year else ""
        queries.append(f'title:"{safe_title}"{extra}')
    return list(dict.fromkeys(queries))


def match_one(api: OpenLibraryAPI, record: CatalogRecord, verbose: bool = False) -> MatchRecord:
    docs_by_key: dict[str, dict[str, Any]] = {}
    errors = []
    for query in _search_queries(record):
        try:
            docs = parse_search_results(api.search(query, limit=10))
            for doc in docs:
                docs_by_key.setdefault(str(doc["key"]), doc)
            if verbose:
                LOG.info("%s: query %r returned %d works", record.filename, query, len(docs))
            # An ISBN search yielding a candidate is definitive enough to stop extra calls.
            if query.startswith("isbn:") and docs:
                break
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            LOG.warning("Search failed for %s: %s", record.filename, exc)
    result = choose_match(record, list(docs_by_key.values()))
    if errors and not docs_by_key:
        result.status = "ERROR"
        result.notes = "; ".join(errors)
    return result


def merge_new_decisions(
    decisions_path: Path, matches: list[MatchRecord], previous_matches: list[dict[str, str]]
) -> tuple[int, int, int]:
    existing = read_csv(decisions_path)
    existing_by_key = {row.get("local_path", ""): row for row in existing}
    previous_by_key = {row.get("local_path", ""): row for row in previous_matches}
    output = []
    added = refreshed = preserved = 0
    seen = set()
    for match in matches:
        key = match.local_path
        new_row = match.to_dict()
        seen.add(key)
        old_decision = existing_by_key.get(key)
        if old_decision is None:
            output.append(new_row)
            added += 1
        elif old_decision == previous_by_key.get(key):
            # It still exactly equals the prior machine row, so no human cell was changed.
            output.append(new_row)
            refreshed += 1
        else:
            output.append(old_decision)
            preserved += 1
    # Preserve reviewed rows whose local file has since disappeared; silently deleting a decision
    # would be more surprising than retaining an orphan for manual cleanup.
    for row in existing:
        if row.get("local_path", "") not in seen:
            output.append(row)
            preserved += 1
    write_csv(decisions_path, MatchRecord.fieldnames(), output)
    return preserved, added, refreshed


def command_match(args: argparse.Namespace, config: dict[str, Any]) -> int:
    catalog_path = args.data_dir / "catalog.csv"
    rows = read_csv(catalog_path)
    if not rows:
        raise ValueError(f"No catalog rows found in {catalog_path}; run scan first")
    api = make_api(config, args.data_dir)
    matches = []
    for index, row in enumerate(rows, start=1):
        record = catalog_from_row(row)
        matches.append(match_one(api, record, args.verbose))
        if index % 10 == 0:
            LOG.info("Matched %d/%d", index, len(rows))
    matches_path = args.data_dir / "matches.csv"
    decisions_path = args.data_dir / "decisions.csv"
    previous_matches = read_csv(matches_path)
    write_csv(matches_path, MatchRecord.fieldnames(), (m.to_dict() for m in matches))
    preserved, added, refreshed = merge_new_decisions(decisions_path, matches, previous_matches)
    not_found = [m.to_dict() for m in matches if m.status == "NOT_FOUND"]
    write_csv(args.data_dir / "not_found.csv", MatchRecord.fieldnames(), not_found)
    counts = Counter(m.status for m in matches)
    print(f"Matched {len(matches)} catalog rows -> {matches_path}")
    print("  " + ", ".join(f"{status}: {count}" for status, count in sorted(counts.items())))
    print(f"Decisions: preserved {preserved} human-edited rows; refreshed {refreshed} untouched machine rows; added {added} new rows -> {decisions_path}")
    return 0


def command_review_summary(args: argparse.Namespace, _config: dict[str, Any]) -> int:
    path = args.data_dir / "decisions.csv"
    rows = read_csv(path)
    if not rows:
        raise ValueError(f"No decisions found in {path}; run match first")
    overall = Counter((row.get("status") or "BLANK").upper() for row in rows)
    print(f"Review file: {path}")
    print("Overall: " + ", ".join(f"{k}={v}" for k, v in sorted(overall.items())))
    by_folder: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_folder[row.get("local_folder", "(unknown)")][(row.get("status") or "BLANK").upper()] += 1
    for folder, counts in sorted(by_folder.items()):
        print(f"  {folder}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def _match_from_row(row: dict[str, str]) -> MatchRecord:
    return MatchRecord(**{key: row.get(key, "") for key in MatchRecord.fieldnames()})


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_from_row(row: dict[str, str]) -> VerifiedMetadataRecord:
    return VerifiedMetadataRecord(**{
        key: row.get(key, "") for key in VerifiedMetadataRecord.fieldnames()
    })


def _enrichment_from_row(row: dict[str, str]) -> EnrichmentRecord:
    return EnrichmentRecord(**{
        key: row.get(key, "") for key in EnrichmentRecord.fieldnames()
    })


def _recovery_from_row(row: dict[str, str]) -> RecoveryRecord:
    return RecoveryRecord(**{
        key: row.get(key, "") for key in RecoveryRecord.fieldnames()
    })


def _apply_recovery(record: CatalogRecord, recovery: RecoveryRecord | None) -> CatalogRecord:
    if recovery is None:
        return record
    if recovery.isbn_10 and not record.isbn_10:
        record.isbn_10 = recovery.isbn_10
    if recovery.isbn_13 and not record.isbn_13:
        record.isbn_13 = recovery.isbn_13
    if recovery.doi and not record.doi:
        record.doi = recovery.doi
    if recovery.recovered_year and not record.year:
        record.year = recovery.recovered_year
    if recovery.recovered_publisher and not record.publisher:
        record.publisher = recovery.recovered_publisher
    if recovery.recovered_author and not _plausible_creator(record.detected_author):
        record.detected_author = recovery.recovered_author
    if _title_score(recovery.recovered_title, "pdf_text") > _title_score(record.detected_title, "pdf_text") + 4:
        record.detected_title = recovery.recovered_title
    return record


def command_recover_metadata(args: argparse.Namespace, _config: dict[str, Any]) -> int:
    catalog_rows = read_csv(args.data_dir / "catalog.csv")
    if not catalog_rows:
        raise ValueError("Run scan before recover-metadata")
    results = []
    for index, row in enumerate(catalog_rows, start=1):
        result = recover_pdf_metadata(catalog_from_row(row))
        results.append(result)
        if args.verbose and index % 10 == 0:
            LOG.info("Deep-scanned %d/%d PDFs", index, len(catalog_rows))
    output = args.data_dir / "recovered_metadata.csv"
    write_csv(output, RecoveryRecord.fieldnames(), (row.to_dict() for row in results))
    counts = Counter(row.recovery_status for row in results)
    identifiers = sum(bool(row.isbn_10 or row.isbn_13 or row.doi) for row in results)
    print(f"Deep-scanned {len(results)} PDFs -> {output}")
    print("  " + ", ".join(f"{status}={count}" for status, count in sorted(counts.items())))
    print(f"  {identifiers} PDFs contain a checksum-valid ISBN or DOI in selected first/last pages")
    return 0


def _work_groups(
    catalog: list[CatalogRecord], results: list[EnrichmentRecord],
) -> dict[str, list[tuple[CatalogRecord, EnrichmentRecord]]]:
    if len(catalog) != len(results):
        raise ValueError("Catalog and enrichment rows must have identical grain")
    groups: dict[str, list[tuple[CatalogRecord, EnrichmentRecord]]] = defaultdict(list)
    for record, result in zip(catalog, results):
        if result.ol_work_key:
            group_id = f"ol:{result.ol_work_key}"
        elif result.parent_publication_id:
            group_id = f"parent:{result.parent_publication_id}"
        elif result.doi:
            group_id = f"doi:{result.doi.casefold()}"
        elif result.duplicate_of_path:
            group_id = f"local:{result.duplicate_of_path}"
        else:
            group_id = f"local:{record.local_path}"
        groups[group_id].append((record, result))
    return groups


def _stable_local_work_id(
    members: list[tuple[CatalogRecord, EnrichmentRecord]],
    file_digests: dict[str, str] | None,
) -> str:
    """Create an opaque ID from content digests, independent of external matches."""
    values = {
        (file_digests or {}).get(record.local_path)
        or unicodedata.normalize("NFC", record.local_path)
        for record, _ in members
    }
    digest = hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()
    return f"OLP-{digest[:16].upper()}"


_GENERIC_TITLE_RE = re.compile(
    r"^(?:document|untitled|un[ií]on europea|c[aá]mara de comercio de bogot[aá]|"
    r"final|report|informe|presentaci[oó]n|powerpoint)$", re.I,
)


def _clean_title_candidate(value: str) -> str:
    value = value or ""
    # A few PDFs expose UTF-8 title bytes decoded as Latin-1 (e.g. "AnÃ¡lisis").
    if re.search(r"Ã.|Â.|â[\x80-\xbf]", value):
        try:
            repaired = value.encode("latin-1").decode("utf-8")
            if repaired.count("Ã") + repaired.count("Â") < value.count("Ã") + value.count("Â"):
                value = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    if re.fullmatch(r"(?:/g\d+)+", value.strip(), re.I):
        return ""
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    if re.search(r"\.(?:docx?|pptx?|pdf)$", value, re.I):
        value = re.sub(r"\.(?:docx?|pptx?|pdf)$", "", value, flags=re.I)
        value = value.replace("_", " ")
        value = re.sub(r"\s+", " ", value).strip(" ._-")
    return value


def _title_score(value: str, source: str) -> float:
    value = _clean_title_candidate(value)
    if not value:
        return -100.0
    words = re.findall(r"[A-Za-zÀ-ÿ0-9]+", value)
    score = {"pdf_text": 12.0, "pdf_metadata": 10.0, "filename": 8.0}.get(source, 0.0)
    score += min(len(words), 18) * 1.8
    if 3 <= len(words) <= 20:
        score += 5.0
    if len(words) <= 5 and value.isupper():
        score -= 8.0
    letters = [char for char in value if char.isalpha() and ord(char) < 128]
    if value.isupper() and len(words) >= 6 and letters:
        vowel_share = sum(char in "AEIOUY" for char in letters) / len(letters)
        if vowel_share < 0.18:
            return -90.0
    if _GENERIC_TITLE_RE.fullmatch(value):
        score -= 18.0
    return score


def _best_local_title(
    members: list[tuple[CatalogRecord, EnrichmentRecord]],
) -> tuple[str, str]:
    for _, result in members:
        if result.canonical_title.strip():
            return re.sub(r"\s+", " ", result.canonical_title).strip(), result.source or "enrichment"
    candidates: list[tuple[float, str, str]] = []
    for record, _ in members:
        for value, source in (
            (record.detected_title, "pdf_text"),
            (record.pdf_title, "pdf_metadata"),
            (filename_title(record.filename), "filename"),
        ):
            cleaned = _clean_title_candidate(value)
            candidates.append((_title_score(cleaned, source), cleaned, source))
    _, title, source = max(candidates, default=(-100.0, "Untitled local publication", "filename"))
    return title or "Untitled local publication", source


def _plausible_creator(value: str) -> bool:
    value = re.sub(r"\s+", " ", value or "").strip()
    return bool(value) and len(value) <= 160 and len(value.split()) <= 15 and not re.search(
        r"https?://|www\.|acrobat|administrator|unknown|created by|generated by|"
        r"microsoft|^author$|^user$", value, re.I,
    )


def _identity_value_and_origin(
    members: list[tuple[CatalogRecord, EnrichmentRecord]], result_name: str,
    *record_names: str,
) -> tuple[str, str]:
    # Prefer any authority-enriched value in the group before falling back to
    # noisier per-PDF extraction from a representative component.
    for _, result in members:
        value = str(getattr(result, result_name, "") or "").strip()
        if value:
            return value, result.source or "authority"
    for record, result in members:
        for record_name in record_names:
            value = str(getattr(record, record_name, "") or "").strip()
            if value:
                return value, "pdf_metadata"
    return "", ""


def _first_identity_value(
    members: list[tuple[CatalogRecord, EnrichmentRecord]], result_name: str,
    *record_names: str,
) -> str:
    return _identity_value_and_origin(members, result_name, *record_names)[0]


def classify_document_type(record: CatalogRecord, title: str, existing: str = "") -> str:
    if existing.strip():
        return existing.strip().casefold()
    value = unicodedata.normalize(
        "NFKD", " ".join((record.filename, record.local_folder, title))
    ).encode("ascii", "ignore").decode("ascii").casefold()
    if record.doi or re.search(r"\bjournal\b|1-s2\.0|\barticle\b|\bet al\b", value):
        return "journal-article"
    if re.search(r"\b(?:decreto|resolucion|ley|acuerdo)\b|\bconpes\b", value):
        return "legal-document"
    if record.local_folder.casefold() == "carto" or re.search(r"\bmapa\b|\bcartograf", value):
        return "map"
    if re.search(r"\bpresentaci[oó]n\b|\bpresentation\b|\bslides?\b|\bppt\b", value):
        return "presentation"
    if re.search(r"\bthesis\b|\btesis\b|\bdissertation\b", value):
        return "dissertation"
    if re.search(r"\bstandard\b|\bestandar\b|\bnorma\b", value):
        return "standard"
    if re.search(r"\bmanual\b|\bguidebook\b|\bhandbook\b|\bguia\b", value):
        return "manual"
    if re.search(r"\b(?:plan|pot|dts)\b|planeamiento|ordenamiento territorial", value):
        return "planning-document"
    if re.search(
        r"\breport\b|\binforme\b|\bdiagnostico\b|\bestrategia\b|\bestudio\b|"
        r"research paper|working paper|technical report|\bpolicy\b|\bpolitica\b",
        value,
    ):
        return "report"
    if record.isbn_10 or record.isbn_13:
        return "book"
    return "publication"


def _mapping_status(status: str, ol_work_key: str, source: str) -> str:
    if ol_work_key:
        return "OPENLIBRARY_LINKED"
    if status not in {"UNIDENTIFIED", "ERROR", ""} and source:
        return "AUTHORITY_MAPPED"
    return "LOCAL_PROVISIONAL"


def _mapping_confidence(
    members: list[tuple[CatalogRecord, EnrichmentRecord]], status: str,
    title: str, author: str, publisher: str, year: str, identifier: str,
) -> str:
    verified = [result.confidence for _, result in members if result.confidence]
    if status not in {"UNIDENTIFIED", "ERROR", ""} and verified:
        try:
            return f"{max(float(value) for value in verified):.3f}"
        except ValueError:
            pass
    words = len(re.findall(r"[A-Za-zÀ-ÿ0-9]+", title))
    score = 0.32 + min(words, 10) * 0.025
    score += 0.12 if author else 0.0
    score += 0.07 if publisher else 0.0
    score += 0.08 if year else 0.0
    score += 0.12 if identifier else 0.0
    if any(record.scan_error or len(record.text_sample.strip()) < 100 for record, _ in members):
        score -= 0.12
    return f"{max(0.200, min(score, 0.840)):.3f}"


def _primary_identifier(
    local_work_id: str, ol_work_key: str, doi: str, isbn_13: str,
    isbn_10: str, lccn: str, ocaid: str, external_url: str,
) -> tuple[str, str]:
    if ol_work_key:
        return "openlibrary_work", ol_work_key
    if doi:
        return "doi", doi
    if isbn_13:
        return "isbn_13", isbn_13
    if isbn_10:
        return "isbn_10", isbn_10
    if lccn:
        return "lccn", lccn
    if ocaid:
        return "ocaid", ocaid
    if external_url:
        return "url", external_url
    return "local_work", local_work_id


def _openlibrary_action(status: str, document_type: str, ol_work_key: str) -> str:
    if ol_work_key:
        return "READY_TO_ADD"
    if status in {"CREATE_CANDIDATE", "PARENT_COMPONENT"}:
        return "CREATE_RECORD_CANDIDATE"
    if document_type in {
        "journal-article", "proceedings-article", "article", "map", "legal-document",
        "serial-issue", "presentation",
    } or status in {"VERIFIED_EXTERNAL", "VERIFIED_NONBOOK"}:
        return "EXTERNAL_OR_LOCAL_ONLY"
    return "REVIEW_FOR_OL_RECORD"


def build_local_mapping(
    catalog: list[CatalogRecord], results: list[EnrichmentRecord],
    file_digests: dict[str, str] | None = None,
) -> tuple[list[WorkInventoryRecord], list[ListMembershipRecord]]:
    """Collapse files into works and map every work to each direct containing-folder list."""
    groups = _work_groups(catalog, results)

    status_rank = {
        "OL_MATCHED": 0, "ALREADY_MATCHED": 0, "CREATE_CANDIDATE": 1,
        "VERIFIED_EXTERNAL": 2, "VERIFIED_NONBOOK": 3, "PARENT_COMPONENT": 4,
        "ALTERNATE_EDITION": 5, "DUPLICATE_LOCAL": 6, "NEEDS_REVIEW": 7,
        "UNIDENTIFIED": 8, "ERROR": 9,
    }
    output: list[WorkInventoryRecord] = []
    memberships: list[ListMembershipRecord] = []
    for group_id, members in sorted(groups.items(), key=lambda item: item[0].casefold()):
        representative_record, representative = min(
            members, key=lambda pair: status_rank.get(pair[1].verification_status, 99)
        )
        status = representative.verification_status
        if status in {"OL_MATCHED", "ALREADY_MATCHED"}:
            status = "MATCHED"
        local_work_id = _stable_local_work_id(members, file_digests)
        title, title_source = _best_local_title(members)
        author, author_origin = _identity_value_and_origin(
            members, "canonical_author", "detected_author", "pdf_author",
        )
        # The creator filter guards against junk scraped from PDF metadata; a
        # hand-audited manifest value is never discarded by it, because long
        # corporate and multi-author credits are legitimate there.
        if author_origin != "verified_manifest" and not _plausible_creator(author):
            author = ""
        publisher = _first_identity_value(members, "canonical_publisher", "publisher")
        year = _first_identity_value(members, "canonical_year", "year")
        isbn_10 = _first_identity_value(members, "isbn_10", "isbn_10")
        isbn_13 = _first_identity_value(members, "isbn_13", "isbn_13")
        doi = _first_identity_value(members, "doi", "doi")
        ocaid = _first_identity_value(members, "ocaid")
        lccn = _first_identity_value(members, "lccn")
        external_url = _first_identity_value(members, "external_url")
        ol_work_key = _first_identity_value(members, "ol_work_key")
        ol_edition_key = _first_identity_value(members, "ol_edition_key")
        source = representative.source or title_source
        document_type = classify_document_type(
            representative_record, title, _first_identity_value(members, "document_type")
        )
        mapping_status = _mapping_status(status, ol_work_key, representative.source)
        identifier_type, identifier = _primary_identifier(
            local_work_id, ol_work_key, doi, isbn_13, isbn_10, lccn, ocaid, external_url,
        )
        confidence = _mapping_confidence(
            members, representative.verification_status, title, author, publisher,
            year, "" if identifier_type == "local_work" else identifier,
        )
        action = _openlibrary_action(status, document_type, ol_work_key)
        notes = list(dict.fromkeys(result.notes for _, result in members if result.notes))
        inventory_record = WorkInventoryRecord(
            local_work_id=local_work_id, work_group_id=group_id,
            mapping_status=mapping_status, mapping_confidence=confidence,
            identity_source=source, verification_status=status,
            canonical_title=title, canonical_author=author,
            canonical_publisher=publisher, canonical_year=year,
            isbn_10=isbn_10, isbn_13=isbn_13, doi=doi, ocaid=ocaid, lccn=lccn,
            primary_identifier_type=identifier_type, primary_identifier=identifier,
            document_type=document_type, external_url=external_url,
            ol_work_key=ol_work_key, ol_edition_key=ol_edition_key,
            openlibrary_action=action, local_pdf_count=len(members),
            local_folders="; ".join(sorted({record.local_folder for record, _ in members}, key=str.casefold)),
            local_paths_json=json.dumps([record.local_path for record, _ in members], ensure_ascii=False),
            notes=" | ".join(notes),
        )
        output.append(inventory_record)
        by_folder: dict[str, list[str]] = defaultdict(list)
        for record, _ in members:
            by_folder[unicodedata.normalize("NFC", record.local_folder)].append(record.local_path)
        for folder, paths in sorted(by_folder.items(), key=lambda item: item[0].casefold()):
            memberships.append(ListMembershipRecord(
                list_name=folder, local_work_id=local_work_id, canonical_title=title,
                document_type=document_type, mapping_status=mapping_status,
                mapping_confidence=confidence, primary_identifier_type=identifier_type,
                primary_identifier=identifier, ol_work_key=ol_work_key,
                openlibrary_action=action, local_pdf_count=len(paths),
                local_paths_json=json.dumps(paths, ensure_ascii=False),
            ))
    memberships.sort(key=lambda row: (row.list_name.casefold(), row.canonical_title.casefold(), row.local_work_id))
    return output, memberships


def build_work_inventory(
    catalog: list[CatalogRecord], results: list[EnrichmentRecord],
    file_digests: dict[str, str] | None = None,
) -> list[WorkInventoryRecord]:
    """Backward-compatible inventory-only wrapper around the local mapping builder."""
    return build_local_mapping(catalog, results, file_digests)[0]


def _write_local_mapping(
    data_dir: Path, catalog: list[CatalogRecord], results: list[EnrichmentRecord],
    file_digests: dict[str, str],
) -> tuple[list[WorkInventoryRecord], list[ListMembershipRecord]]:
    inventory, memberships = build_local_mapping(catalog, results, file_digests)
    inventory_ids = [row.local_work_id for row in inventory]
    if len(inventory_ids) != len(set(inventory_ids)):
        raise RuntimeError("Stable local Work IDs collided; mapping was not written")
    membership_keys = [(row.list_name, row.local_work_id) for row in memberships]
    if len(membership_keys) != len(set(membership_keys)):
        raise RuntimeError("Duplicate list/Work membership detected; mapping was not written")
    referenced_ids = {row.local_work_id for row in memberships}
    if referenced_ids != set(inventory_ids):
        raise RuntimeError("Every local Work must belong to at least one direct-folder list")
    mapped_paths = {
        path
        for row in memberships
        for path in json.loads(row.local_paths_json)
    }
    catalog_paths = {record.local_path for record in catalog}
    if mapped_paths != catalog_paths:
        raise RuntimeError("List mapping does not cover the catalog exactly")
    write_csv(
        data_dir / "work_inventory.csv", WorkInventoryRecord.fieldnames(),
        (row.to_dict() for row in inventory),
    )
    write_csv(
        data_dir / "list_membership.csv", ListMembershipRecord.fieldnames(),
        (row.to_dict() for row in memberships),
    )
    return inventory, memberships


def _digest_catalog(catalog: list[CatalogRecord]) -> dict[str, str]:
    digests: dict[str, str] = {}
    for record in catalog:
        try:
            digests[record.local_path] = _file_digest(record.local_path)
        except OSError as exc:
            LOG.warning("Could not hash %s: %s", record.filename, exc)
    return digests


def _print_mapping_summary(
    inventory: list[WorkInventoryRecord], memberships: list[ListMembershipRecord],
    manifest_rows: list[dict[str, str]], data_dir: Path,
) -> None:
    mapping_counts = Counter(row.mapping_status for row in inventory)
    type_counts = Counter(row.document_type for row in inventory)
    action_counts = Counter(row.openlibrary_action for row in inventory)
    manifest_names = {
        unicodedata.normalize("NFC", row.get("list_name", "").strip())
        for row in manifest_rows if row.get("list_name", "").strip()
    }
    populated = {row.list_name for row in memberships}
    print(f"Mapped {len(inventory)} intellectual works to {len(memberships)} list memberships")
    print(f"  Lists: {len(manifest_names)} total; {len(populated)} populated; {len(manifest_names - populated)} empty")
    print("  Mapping: " + ", ".join(f"{key}={value}" for key, value in sorted(mapping_counts.items())))
    print("  Open Library route: " + ", ".join(f"{key}={value}" for key, value in sorted(action_counts.items())))
    print("  Document types: " + ", ".join(f"{key}={value}" for key, value in sorted(type_counts.items())))
    print(f"  Canonical catalog -> {data_dir / 'work_inventory.csv'}")
    print(f"  Folder/list map -> {data_dir / 'list_membership.csv'}")


def command_map(args: argparse.Namespace, _config: dict[str, Any]) -> int:
    catalog_rows = read_csv(args.data_dir / "catalog.csv")
    enrichment_rows = read_csv(args.data_dir / "enrichment.csv")
    if not catalog_rows or not enrichment_rows:
        raise ValueError("Run scan, match, and enrich before map")
    catalog = [catalog_from_row(row) for row in catalog_rows]
    enrichment_by_path = {
        row.get("local_path", ""): _enrichment_from_row(row)
        for row in enrichment_rows
    }
    missing = [record.local_path for record in catalog if record.local_path not in enrichment_by_path]
    extra = set(enrichment_by_path) - {record.local_path for record in catalog}
    if missing or extra:
        raise ValueError(
            f"Catalog/enrichment path mismatch: missing={len(missing)}, extra={len(extra)}; rerun enrich"
        )
    results = [enrichment_by_path[record.local_path] for record in catalog]
    inventory, memberships = _write_local_mapping(
        args.data_dir, catalog, results, _digest_catalog(catalog),
    )
    _print_mapping_summary(
        inventory, memberships, read_csv(args.data_dir / "lists.csv"), args.data_dir,
    )
    return 0


_OL_OUTSIDE_SCOPE_TYPES = {
    "journal-article", "proceedings-article", "article", "newspaper-article",
    "map", "legal-document", "serial-issue", "presentation",
}
_OL_BOOKLIKE_TYPES = {
    "book", "report", "manual", "standard", "planning-document", "dissertation",
    # Monograph-like grey literature: separately issued, citable, and routinely
    # catalogued as books rather than as parts of a serial.
    "working-paper", "white-paper", "discussion-paper", "research-brief",
    "guidebook", "guidelines", "consultant-report", "capstone", "teaching-case",
}


def command_plan_import(args: argparse.Namespace, _config: dict[str, Any]) -> int:
    rows = read_csv(args.data_dir / "work_inventory.csv")
    if not rows:
        raise ValueError("Run map before plan-import")
    output: list[ImportPlanRecord] = []
    for row in rows:
        doc_type = row.get("document_type", "").casefold().strip()
        ol_key = row.get("ol_work_key", "").strip()
        status = row.get("verification_status", "").upper().strip()
        title = row.get("canonical_title", "").strip()
        author = row.get("canonical_author", "").strip()
        publisher = row.get("canonical_publisher", "").strip()
        year = row.get("canonical_year", "").strip()
        external_url = row.get("external_url", "").strip()
        identifier_type = row.get("primary_identifier_type", "").strip()
        identifier = row.get("primary_identifier", "").strip()
        missing = []
        if not title:
            missing.append("title")
        if not author:
            missing.append("author/responsible organization")
        if not publisher:
            missing.append("publisher")
        if not year:
            missing.append("publication year")
        has_authority = bool(external_url or (identifier and identifier_type != "local_work"))
        if ol_key:
            eligibility, preparation = "ELIGIBLE", "READY_TO_LIST"
        elif doc_type in _OL_OUTSIDE_SCOPE_TYPES or status in {"VERIFIED_EXTERNAL", "VERIFIED_NONBOOK"}:
            eligibility, preparation = "OUTSIDE_BOOK_CATALOG_SCOPE", "KEEP_EXTERNAL_OR_LOCAL"
        elif doc_type in _OL_BOOKLIKE_TYPES:
            eligibility = "ELIGIBLE_BOOKLIKE"
            if status in {"CREATE_CANDIDATE", "PARENT_COMPONENT"} and has_authority and not missing:
                preparation = "READY_FOR_IMPORT_PREVIEW"
            elif not has_authority:
                preparation = "NEEDS_AUTHORITY_SOURCE"
            elif missing:
                preparation = "NEEDS_METADATA_REVIEW"
            else:
                preparation = "READY_FOR_HUMAN_REVIEW"
        else:
            eligibility = "TYPE_REVIEW_REQUIRED"
            preparation = "NEEDS_TYPE_AND_AUTHORITY_REVIEW"
        queries = [f'"{title}"'] if title else []
        if title and author:
            queries.append(f'"{title}" "{author.split(";")[0].strip()}"')
        output.append(ImportPlanRecord(
            local_work_id=row.get("local_work_id", ""),
            local_folders=row.get("local_folders", ""),
            local_pdf_count=int(row.get("local_pdf_count", "0") or 0),
            canonical_title=title, canonical_author=author,
            canonical_publisher=publisher, canonical_year=year,
            document_type=doc_type, verification_status=status,
            ol_work_key=ol_key, external_url=external_url,
            primary_identifier_type=identifier_type, primary_identifier=identifier,
            openlibrary_eligibility=eligibility, preparation_status=preparation,
            missing_metadata="; ".join(missing), suggested_queries=" | ".join(queries),
            notes=row.get("notes", ""),
        ))
    path = args.data_dir / "import_plan.csv"
    write_csv(path, ImportPlanRecord.fieldnames(), (row.to_dict() for row in output))
    eligibility_counts = Counter(row.openlibrary_eligibility for row in output)
    preparation_counts = Counter(row.preparation_status for row in output)
    print(f"Planned {len(output)} local works -> {path}")
    print("  Eligibility: " + ", ".join(f"{key}={value}" for key, value in sorted(eligibility_counts.items())))
    print("  Preparation: " + ", ".join(f"{key}={value}" for key, value in sorted(preparation_counts.items())))
    return 0


def command_enrich(args: argparse.Namespace, config: dict[str, Any]) -> int:
    catalog_rows = read_csv(args.data_dir / "catalog.csv")
    match_rows = read_csv(args.data_dir / "matches.csv")
    if not catalog_rows or not match_rows:
        raise ValueError("Run scan and match before enrich")
    catalog = [catalog_from_row(row) for row in catalog_rows]
    recovery_by_path = {
        row.get("local_path", ""): _recovery_from_row(row)
        for row in read_csv(args.data_dir / "recovered_metadata.csv")
    }
    catalog = [
        _apply_recovery(record, recovery_by_path.get(record.local_path))
        for record in catalog
    ]
    base_by_path = {row.get("local_path", ""): row for row in match_rows}
    verified_rules = [
        _verified_from_row(row)
        for row in read_csv(args.data_dir / "verified_metadata.csv")
    ]
    existing_enrichment_by_path = {
        row.get("local_path", ""): row
        for row in read_csv(args.data_dir / "enrichment.csv")
    }
    if args.verified_only and not existing_enrichment_by_path:
        raise ValueError("enrich --verified-only requires an existing enrichment.csv")
    ol_api = make_api(config, args.data_dir)
    metadata_api = MetadataAPI(
        args.data_dir / "cache" / "enrichment", user_agent(config),
        delay=float(config.get("openlibrary", {}).get("request_delay_seconds", 0.5)),
    )
    google_key = os.getenv("GOOGLE_BOOKS_API_KEY", "")

    file_digests = _digest_catalog(catalog)
    by_hash: dict[str, list[CatalogRecord]] = defaultdict(list)
    for record in catalog:
        digest = file_digests.get(record.local_path)
        if digest:
            by_hash[digest].append(record)
    duplicate_of: dict[str, str] = {}
    for group in by_hash.values():
        if len(group) > 1:
            primary = group[0].local_path
            duplicate_of.update({record.local_path: primary for record in group[1:]})

    results_by_path: dict[str, EnrichmentRecord] = {}
    primaries = [record for record in catalog if record.local_path not in duplicate_of]
    for index, record in enumerate(primaries, start=1):
        previous = base_by_path.get(record.local_path, {})
        verified = select_verified_metadata(record, verified_rules)
        if args.verified_only and not verified:
            existing = existing_enrichment_by_path.get(record.local_path)
            if not existing:
                raise ValueError(f"No prior enrichment row to reuse for {record.local_path}")
            results_by_path[record.local_path] = _enrichment_from_row(existing)
            continue
        try:
            results_by_path[record.local_path] = enrich_record(
                record, previous, ol_api, metadata_api, google_key,
                verified=verified,
            )
        except Exception as exc:
            LOG.warning("Enrichment failed for %s: %s", record.filename, exc)
            results_by_path[record.local_path] = EnrichmentRecord(
                local_folder=record.local_folder, local_path=record.local_path, filename=record.filename,
                previous_status=previous.get("status", ""), verification_status="ERROR",
                doi=record.doi, isbn_10=record.isbn_10, isbn_13=record.isbn_13,
                notes=f"{type(exc).__name__}: {exc}",
            )
        if index % 10 == 0:
            LOG.info("Enriched %d/%d primary PDFs", index, len(primaries))

    for record in (item for item in catalog if item.local_path in duplicate_of):
        primary_path = duplicate_of[record.local_path]
        primary = results_by_path[primary_path]
        verified = select_verified_metadata(record, verified_rules)
        if args.verified_only and not verified:
            existing = existing_enrichment_by_path.get(record.local_path)
            if not existing:
                raise ValueError(f"No prior enrichment row to reuse for {record.local_path}")
            results_by_path[record.local_path] = _enrichment_from_row(existing)
            continue
        if primary.verification_status in {"OL_MATCHED", "ALREADY_MATCHED"} and primary.ol_work_key:
            duplicate = EnrichmentRecord(**primary.to_dict())
            duplicate.local_folder, duplicate.local_path, duplicate.filename = record.local_folder, record.local_path, record.filename
            duplicate.previous_status = base_by_path.get(record.local_path, {}).get("status", "")
            duplicate.duplicate_of_path = primary_path
            duplicate.verification_status = "OL_MATCHED"
            duplicate.notes = "Byte-identical PDF inherited the primary file's verified Open Library Work"
            results_by_path[record.local_path] = duplicate
        else:
            try:
                results_by_path[record.local_path] = enrich_record(
                    record, base_by_path.get(record.local_path, {}), ol_api, metadata_api,
                    google_key, duplicate_of=primary_path,
                    verified=verified,
                )
            except Exception as exc:
                results_by_path[record.local_path] = EnrichmentRecord(
                    local_folder=record.local_folder, local_path=record.local_path, filename=record.filename,
                    previous_status=base_by_path.get(record.local_path, {}).get("status", ""),
                    verification_status="ERROR", duplicate_of_path=primary_path,
                    notes=f"{type(exc).__name__}: {exc}",
                )

    results = [results_by_path[record.local_path] for record in catalog]
    enrichment_path = args.data_dir / "enrichment.csv"
    write_csv(enrichment_path, EnrichmentRecord.fieldnames(), (result.to_dict() for result in results))
    inventory, memberships = _write_local_mapping(
        args.data_dir, catalog, results, file_digests,
    )
    inventory_path = args.data_dir / "work_inventory.csv"

    previous_enriched_path = args.data_dir / "enriched_matches.csv"
    previous_machine = read_csv(previous_enriched_path) or match_rows
    enriched_matches = []
    for record, result in zip(catalog, results):
        base = _match_from_row(base_by_path.get(record.local_path, {}))
        enriched_matches.append(apply_enrichment(base, result))
    preserved, added, refreshed = merge_new_decisions(
        args.data_dir / "decisions.csv", enriched_matches, previous_machine,
    )
    write_csv(previous_enriched_path, MatchRecord.fieldnames(), (row.to_dict() for row in enriched_matches))
    counts = Counter(result.verification_status for result in results)
    inventory_counts = Counter(row.verification_status for row in inventory)
    print(f"Enriched {len(results)} PDFs -> {enrichment_path}")
    print("  " + ", ".join(f"{status}: {count}" for status, count in sorted(counts.items())))
    print(f"Estimated {len(inventory)} intellectual works -> {inventory_path}")
    print("  " + ", ".join(f"{status}: {count}" for status, count in sorted(inventory_counts.items())))
    print(f"Mapped those works to {len(memberships)} direct-folder memberships -> {args.data_dir / 'list_membership.csv'}")
    print(f"Decisions: preserved {preserved} human-edited rows; refreshed {refreshed}; added {added}")
    if metadata_api.google_disabled:
        print("Google Books rate-limited uncached requests; rerun later or configure GOOGLE_BOOKS_API_KEY to continue those lookups.")
    return 0


def _name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value or "").casefold().strip()


RESOLVED_NOT_PUBLISHABLE = {
    "VERIFIED_EXTERNAL", "DUPLICATE_LOCAL", "CREATE_CANDIDATE",
    "PARENT_COMPONENT", "ALTERNATE_EDITION", "VERIFIED_NONBOOK",
}


def approved_work_rows(rows: list[dict[str, str]]) -> tuple[dict[str, list[str]], dict[str, int]]:
    works: dict[str, list[str]] = defaultdict(list)
    unresolved: dict[str, int] = defaultdict(int)
    for row in rows:
        folder = unicodedata.normalize("NFC", row.get("local_folder", "").strip())
        status = row.get("status", "").upper().strip()
        key = row.get("ol_work_key", "").strip()
        if folder and status in {"MATCHED", "APPROVED"} and re.fullmatch(r"/works/OL\d+W", key):
            works[folder].append(key)
        elif folder and status not in RESOLVED_NOT_PUBLISHABLE:
            unresolved[folder] += 1
    return {folder: list(dict.fromkeys(keys)) for folder, keys in works.items()}, dict(unresolved)


def publish_decisions(
    api: OpenLibraryAPI, username: str, rows: list[dict[str, str]], *, dry_run: bool,
    folder_names: list[str] | None = None,
    local_mapping_rows: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    works, unresolved = approved_work_rows(rows)
    all_folders = sorted(set(folder_names or []) | set(works) | set(unresolved), key=str.casefold)
    existing_entries = api.list_user_lists(username)
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in existing_entries:
        by_name[_name_key(str(entry.get("name", "")))].append(entry)
    summary = []
    status_by_folder: dict[str, Counter[str]] = defaultdict(Counter)
    resolved_not_publishable: dict[str, int] = defaultdict(int)
    local_work_ids: dict[str, set[str]] = defaultdict(set)
    local_actions: dict[str, Counter[str]] = defaultdict(Counter)
    for row in local_mapping_rows or []:
        folder = unicodedata.normalize("NFC", row.get("list_name", "").strip())
        local_id = row.get("local_work_id", "").strip()
        if folder and local_id:
            local_work_ids[folder].add(local_id)
            local_actions[folder][row.get("openlibrary_action", "UNSPECIFIED") or "UNSPECIFIED"] += 1
    for row in rows:
        folder = unicodedata.normalize("NFC", row.get("local_folder", "").strip())
        status = (row.get("status") or "BLANK").upper()
        key = row.get("ol_work_key", "").strip()
        if folder and not (status in {"MATCHED", "APPROVED"} and re.fullmatch(r"/works/OL\d+W", key)):
            status_by_folder[folder][status] += 1
            if status in RESOLVED_NOT_PUBLISHABLE:
                resolved_not_publishable[folder] += 1
    for folder in all_folders:
        matches = by_name.get(_name_key(folder), [])
        if len(matches) > 1:
            raise RuntimeError(f"Multiple existing Open Library lists have the name {folder!r}; resolve manually")
        list_id = api.list_id(matches[0]) if matches else ""
        action = "existing" if list_id else "create"
        current = api.list_seeds(username, list_id) if list_id else set()
        desired = works.get(folder, [])
        missing = [key for key in desired if key not in current]
        already = len(desired) - len(missing)
        if not dry_run:
            if not list_id:
                list_id = api.create_list(username, folder, "Imported from a reviewed local PDF bibliography; PDFs are not uploaded.")
                action = "created"
            api.add_seeds(username, list_id, missing)
        summary.append({
            "folder": folder, "action": action, "list_id": list_id,
            "already": already, "add": len(missing), "unresolved": unresolved.get(folder, 0),
            "resolved_not_publishable": resolved_not_publishable.get(folder, 0),
            "skipped_statuses": dict(status_by_folder.get(folder, {})),
            "local_works": len(local_work_ids.get(folder, set())),
            "local_actions": dict(local_actions.get(folder, {})),
        })
    return summary


def _print_publish_summary(summary: list[dict[str, Any]], dry_run: bool) -> None:
    print("DRY RUN — no writes were made" if dry_run else "Publish complete")
    mapped_memberships = sum(item.get("local_works", 0) for item in summary)
    if mapped_memberships:
        print(f"Local catalog coverage: {mapped_memberships} Work-to-list memberships")
        route_totals: Counter[str] = Counter()
        for item in summary:
            route_totals.update(item.get("local_actions", {}))
        print(
            "Open Library routes: "
            + ", ".join(f"{action}={count}" for action, count in sorted(route_totals.items()))
        )
    for item in summary:
        list_label = f" {item['list_id']}" if item["list_id"] else ""
        verb = "would create" if dry_run and item["action"] == "create" else item["action"]
        print(f"\n{item['folder']}\n  {verb} list{list_label}")
        if item.get("local_works", 0):
            print(f"  {item['local_works']} local works mapped to this list")
        print(f"  {item['already']} already present; {item['add']} {'would be added' if dry_run else 'added'}")
        print(
            f"  PDF linkage: {item['unresolved']} rows still lack a confirmed Open Library Work; "
            f"{item.get('resolved_not_publishable', 0)} rows are mapped but have no publishable Work key"
        )
        if item.get("skipped_statuses"):
            breakdown = ", ".join(f"{status}={count}" for status, count in sorted(item["skipped_statuses"].items()))
            print(f"  not publishable yet: {breakdown}")
        if item.get("local_actions"):
            routes = ", ".join(
                f"{action}={count}" for action, count in sorted(item["local_actions"].items())
            )
            print(f"  local mapping routes: {routes}")


def command_publish(args: argparse.Namespace, config: dict[str, Any]) -> int:
    rows = read_csv(args.data_dir / "decisions.csv")
    if not rows:
        raise ValueError("No decisions.csv rows; scan, match, and review first")
    api = make_api(config, args.data_dir, authenticated=True)
    authenticated_username = api.username()
    configured_username = str(config.get("openlibrary", {}).get("username", "")).strip()
    username = configured_username or authenticated_username
    if not username or (authenticated_username and username != authenticated_username):
        raise RuntimeError("Authenticated user does not match configured Open Library username")
    manifest = read_csv(args.data_dir / "lists.csv")
    folder_names = [unicodedata.normalize("NFC", row.get("list_name", "").strip()) for row in manifest]
    if not folder_names:
        raise ValueError("No lists.csv manifest; rerun scan so empty/nested folders are represented")
    local_mapping_rows = read_csv(args.data_dir / "list_membership.csv")
    if not local_mapping_rows:
        raise ValueError("No list_membership.csv; run map before publish")
    summary = publish_decisions(
        api, username, rows, dry_run=args.dry_run, folder_names=folder_names,
        local_mapping_rows=local_mapping_rows,
    )
    _print_publish_summary(summary, args.dry_run)
    if args.dry_run:
        print("\nReview this plan and obtain explicit approval before running publish without --dry-run.")
    return 0


# Open Library's import API accepts a record under either of two shapes:
# a "complete book" (title, authors, publishers, publish_date) which needs no
# identifier at all, or a bare record carrying a strong identifier. Most Latin
# American government reports, theses and consultancy deliverables have no ISBN,
# and the complete-book shape is the correct route for them.
_STRONG_IMPORT_IDS = ("isbn_13", "isbn_10", "lccn")


def _has_strong_identifier(row: dict[str, str]) -> bool:
    return any(row.get(name, "").strip() for name in _STRONG_IMPORT_IDS)


def validate_creation_row(row: dict[str, str]) -> list[str]:
    """Report why a row cannot be submitted to Open Library's import API."""
    problems = []
    if not row.get("detected_title", "").strip():
        problems.append("missing title")
    if _has_strong_identifier(row):
        return problems
    # No ISBN/LCCN, so the record must satisfy the complete-book shape instead.
    for field, label in (
        ("detected_author", "author/responsible organization"),
        ("publisher", "publisher"),
        ("year", "publication date"),
    ):
        if not row.get(field, "").strip():
            problems.append(f"missing {label} (required when there is no ISBN or LCCN)")
    return problems


def _split_agents(value: str) -> list[str]:
    """Split a semicolon-separated people/organisation credit into a list."""
    return [part.strip() for part in value.split(";") if part.strip()]


def _creation_key(row: dict[str, str], work_ids: dict[str, str]) -> str:
    """Identify the intellectual work a decision row belongs to."""
    work_id = work_ids.get(row.get("local_path", ""), "")
    return work_id or "title:" + row.get("detected_title", "").strip().casefold()


def dedupe_creation_rows(
    rows: list[dict[str, str]], work_ids: dict[str, str],
) -> list[tuple[dict[str, str], str]]:
    """Collapse per-PDF decision rows to one row per intellectual work.

    decisions.csv has a row per PDF, but a split manual or multi-volume report is
    one Open Library Work. Importing each component separately would create
    duplicate records, so only the first PDF of each work is submitted.
    """
    seen: set[str] = set()
    unique: list[tuple[dict[str, str], str]] = []
    for row in rows:
        key = _creation_key(row, work_ids)
        if key in seen:
            continue
        seen.add(key)
        unique.append((row, work_ids.get(row.get("local_path", ""), "")))
    return unique


def completed_work_ids(
    rows: list[dict[str, str]], work_ids: dict[str, str],
) -> set[str]:
    """Works that already carry an Open Library key from an earlier run.

    A batch run must be resumable: re-submitting a work that already landed would
    create a duplicate, so anything already keyed is skipped.
    """
    return {
        _creation_key(row, work_ids)
        for row in rows
        if row.get("ol_work_key", "").strip() or row.get("ol_edition_key", "").strip()
    }


def load_author_aliases(data_dir: Path) -> dict[str, str]:
    """Load the human-approved map from local author names to Open Library keys.

    Open Library catalogues corporate bodies as `Jurisdiction. Body`, so plain
    names like "Departamento Nacional de Planeación" do not match by autocomplete
    and would create duplicates of long-established headings.
    """
    rows = read_csv(data_dir / "author_aliases.csv")
    return {
        row["local_name"].strip(): row["openlibrary_key"].strip()
        for row in rows
        if row.get("local_name", "").strip() and row.get("openlibrary_key", "").strip()
    }


def build_addbook_form(
    row: dict[str, str], *, external_url: str = "", test: bool = False,
    author_keys: dict[str, str] | None = None, parent_work_key: str = "",
) -> dict[str, str]:
    """Build the /books/add form body Open Library's addbook handler expects.

    This is the route an ordinary logged-in account can use: identifiers are
    optional there, unlike /api/import which is gated on bot privileges. Field
    names follow templates/books/add.html and author-autocomplete.html -- the
    handler reads `book_title` (it overwrites `title` with it) and unflattens
    `author_names--N` / `authors--N--author--key`.
    """
    form = {
        "book_title": row.get("detected_title", "").strip(),
        "publisher": "; ".join(_split_agents(row.get("publisher", ""))),
        "publish_date": row.get("year", "").strip(),
        "web_book_url": external_url.strip(),
        "id_name": "",
        "id_value": "",
        "_save": "",
        "_test": "true" if test else "false",
    }
    # An author left as __new__ makes Open Library skip duplicate matching for the
    # whole book (addbook.py: `match = None if created_author else find_matches`),
    # so resolved keys matter for de-duplication, not just for author hygiene.
    resolved = author_keys or {}
    for index, name in enumerate(_split_agents(row.get("detected_author", ""))):
        form[f"author_names--{index}"] = name
        form[f"authors--{index}--author--key"] = resolved.get(name) or "__new__"
    for name in _STRONG_IMPORT_IDS:
        value = row.get(name, "").strip()
        if value:
            form["id_name"], form["id_value"] = name, value
            break
    # find_matches reads `work` straight back off the form. Present and
    # resolvable, the handler adds an edition to that Work; absent, it searches
    # by title and author instead. An empty string would be read as the
    # check-page "none-of-these" reply, so the field is omitted when unused.
    if parent_work_key.strip():
        form["work"] = parent_work_key.strip()
    return form


def validate_edition_row(
    row: dict[str, str], *, parent_work_key: str, author_keys: dict[str, str],
) -> list[str]:
    """Report why an alternate edition cannot be attached to an existing Work.

    Open Library only routes a posted `work` key through find_matches when the
    form created no author (addbook.py: `match = None if created_author else
    self.find_matches(i)`). An unresolved author therefore does not merely leave
    a thin author record behind -- it makes the handler ignore the parent and
    fork a duplicate Work. Both conditions are hard blocks.
    """
    problems = validate_creation_row(row)
    if not re.fullmatch(r"/works/OL\d+W", parent_work_key.strip()):
        problems.append("missing parent work key")
    unresolved = [
        name for name in _split_agents(row.get("detected_author", ""))
        if not author_keys.get(name)
    ]
    if unresolved:
        problems.append("unresolved author would fork a duplicate work: " + "; ".join(unresolved))
    return problems


def edition_attachment_plan(
    rows: list[dict[str, str]], work_ids: dict[str, str], author_keys: dict[str, str],
) -> list[tuple[dict[str, str], str, list[str]]]:
    """Pair every ALTERNATE_EDITION row with the parent Work already on Open Library.

    An alternate edition shares its local work group with the parent PDF, so the
    parent's Open Library key is the one the row must be attached to. Rows whose
    parent was never created, or whose authors did not resolve, are returned with
    their blockers rather than silently dropped.
    """
    key_by_group: dict[str, str] = {}
    for row in rows:
        key = row.get("ol_work_key", "").strip()
        group = work_ids.get(row.get("local_path", ""), "")
        if key and group and group not in key_by_group:
            key_by_group[group] = key
    plan = []
    for row in rows:
        if row.get("status", "").strip().upper() != "ALTERNATE_EDITION":
            continue
        group = work_ids.get(row.get("local_path", ""), "")
        parent_key = key_by_group.get(group, "")
        problems = validate_edition_row(
            row, parent_work_key=parent_key, author_keys=author_keys,
        )
        plan.append((row, parent_key, problems))
    return plan


def build_import_record(
    row: dict[str, str], source_prefix: str, *, local_work_id: str = "",
) -> dict[str, Any]:
    """Build one Open Library /api/import JSON record from a decision row."""
    identifier = local_work_id or row.get("local_path", "") or row.get("filename", "")
    record: dict[str, Any] = {
        "title": row.get("detected_title", "").strip(),
        "source_records": [f"{source_prefix}:{identifier}"],
    }
    authors = _split_agents(row.get("detected_author", ""))
    if authors:
        record["authors"] = [{"name": name} for name in authors]
    publishers = _split_agents(row.get("publisher", ""))
    if publishers:
        record["publishers"] = publishers
    if row.get("year", "").strip():
        record["publish_date"] = row["year"].strip()
    for name in _STRONG_IMPORT_IDS:
        value = row.get(name, "").strip()
        if value:
            record[name] = [value]
    if row.get("ocaid", "").strip():
        record["ocaid"] = row["ocaid"].strip()
    return record


def creation_mode_banner(*, dry_run: bool, match_check: bool) -> str:
    """Describe what this create-missing run will actually do to Open Library."""
    if dry_run:
        return "DRY RUN — no writes were made"
    if match_check:
        return "MATCH CHECK — asking Open Library what each record matches; nothing is written"
    return "Creating explicitly approved records"


def command_create_missing(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if not args.approved_only:
        raise ValueError("create-missing requires --approved-only")
    path = args.data_dir / "decisions.csv"
    rows = read_csv(path)
    proposed = [row for row in rows if row.get("create_record", "").strip().upper() == "YES"]
    print(creation_mode_banner(dry_run=args.dry_run, match_check=args.match_check))

    route = getattr(args, "route", "addbook")
    prefix = str(config.get("openlibrary", {}).get("import_source_prefix", "")).strip()
    if route == "import":
        print("Route: /api/import (requires Open Library bot or API-usergroup privileges)")
        if not prefix:
            if not args.dry_run:
                raise ValueError(
                    "The import route needs openlibrary.import_source_prefix in config.toml: "
                    "the provenance prefix Open Library assigned this account for source_records"
                )
            prefix = "UNREGISTERED_PREFIX"
            print(
                "WARNING: no openlibrary.import_source_prefix is configured. The preview below "
                "uses a placeholder; set a real one before a live import run."
            )
    else:
        print("Route: /books/add (any logged-in account; identifiers optional)")

    work_ids: dict[str, str] = {}
    external_urls: dict[str, str] = {}
    for work in read_csv(args.data_dir / "work_inventory.csv"):
        external_urls[work.get("local_work_id", "")] = work.get("external_url", "")
        for local_path in json.loads(work.get("local_paths_json") or "[]"):
            work_ids[local_path] = work.get("local_work_id", "")

    if not proposed:
        print("No rows have create_record=YES.")
        eligible = [
            row for row in rows
            if row.get("status", "").strip().upper() in {"CREATE_CANDIDATE", "PARENT_COMPONENT"}
        ]
        ready = [row for row in eligible if not validate_creation_row(row)]
        print(
            f"  {len(ready)} of {len(eligible)} create-eligible rows would pass import "
            f"validation today; set create_record=YES on the ones you approve."
        )
        return 0

    # A dry run only ever reads, so it must not present credentials. A
    # match-check does POST, but Open Library writes nothing for _test=true.
    api = make_api(config, args.data_dir, authenticated=not args.dry_run or args.match_check)
    unique = dedupe_creation_rows(proposed, work_ids)
    skipped = len(proposed) - len(unique)
    if skipped:
        print(f"  {skipped} approved rows are further PDFs of an already-listed work; "
              f"{len(unique)} distinct works remain")

    author_keys: dict[str, str] = {}
    near_misses: dict[str, list[str]] = {}
    if route == "addbook":
        names = sorted({
            name for row, _ in unique
            for name in _split_agents(row.get("detected_author", ""))
        })
        aliases = load_author_aliases(args.data_dir)
        print(f"  resolving {len(names)} distinct author names against Open Library"
              f" ({len(aliases)} approved aliases loaded)...")
        aliased = 0
        for name in names:
            if name in aliases:
                author_keys[name] = aliases[name]
                aliased += 1
                continue
            found = api.find_author(name)
            if found["key"]:
                author_keys[name] = found["key"]
            elif found["candidates"]:
                near_misses[name] = found["candidates"][:3]
        print(f"  {len(author_keys)} linked to existing authors "
              f"({aliased} via the approved alias table, {len(author_keys) - aliased} by exact name); "
              f"{len(names) - len(author_keys)} would be created as new")
        for name, candidates in sorted(near_misses.items()):
            print(f"    near miss, left as new: {name!r} vs {candidates}")
    already_done = completed_work_ids(rows, work_ids)
    if already_done and not args.match_check:
        print(f"  {len(already_done)} works already carry an Open Library key and are skipped")
    created = failed = 0
    failures: list[str] = []
    changed = False
    for row, local_work_id in unique:
        label = f"{row.get('local_folder')} / {row.get('filename')}"
        if _creation_key(row, work_ids) in already_done and not args.match_check:
            continue
        problems = validate_creation_row(row)
        if problems:
            print(f"SKIP {label}: " + "; ".join(problems))
            continue
        # Final duplicate check by identifier before any creation.
        identifier_query = ""
        if row.get("isbn_13") or row.get("isbn_10"):
            identifier_query = f"isbn:{row.get('isbn_13') or row.get('isbn_10')}"
        elif row.get("lccn"):
            identifier_query = f"lccn:{row['lccn']}"
        elif row.get("ocaid"):
            identifier_query = f"ocaid:{row['ocaid']}"
        if identifier_query:
            docs = parse_search_results(api.search(identifier_query, limit=5))
            if docs:
                print(f"SKIP {label}: identifier already resolves to {docs[0].get('key')}")
                continue
        try:
            if route == "import":
                record = build_import_record(row, prefix, local_work_id=local_work_id)
                if args.dry_run:
                    print(f"WOULD IMPORT {label}")
                    print("  " + json.dumps(record, ensure_ascii=False, sort_keys=True))
                    continue
                payload = api.import_record(record)
                row["ol_work_key"] = payload.get("work", {}).get("key", "")
                row["ol_edition_key"] = payload.get("edition", {}).get("key", "")
            else:
                form = build_addbook_form(
                    row, external_url=external_urls.get(local_work_id, ""),
                    test=args.match_check, author_keys=author_keys,
                )
                if args.dry_run and not args.match_check:
                    print(f"WOULD ADD {label}")
                    print("  " + json.dumps(form, ensure_ascii=False, sort_keys=True))
                    continue
                if args.match_check:
                    outcome = api.add_book(form)
                    print(f"{outcome['outcome'].upper():<13} {label} {outcome['matched_key']}".rstrip())
                    continue
                keys = api.add_book(form)
                row["ol_work_key"] = keys.get("work_key", "")
                row["ol_edition_key"] = keys.get("edition_key", "")
        except Exception as exc:  # one bad record must not abandon the batch
            failed += 1
            reason = f"{type(exc).__name__}: {exc}"
            failures.append(f"{label}: {reason}")
            row["notes"] = f"Creation failed, still pending: {reason}"[:500]
            changed = True
            write_csv(path, MatchRecord.fieldnames(), rows)
            print(f"FAILED  {label}: {reason}")
            continue
        row["status"] = "APPROVED"
        row["notes"] = "Created after explicit create_record=YES review"
        created += 1
        changed = True
        # Persist after every success so an interrupted batch resumes instead of
        # re-creating everything it already landed.
        write_csv(path, MatchRecord.fieldnames(), rows)
        print(f"CREATED {label}: {row['ol_work_key']} / {row['ol_edition_key']}")

    if changed:
        write_csv(path, MatchRecord.fieldnames(), rows)
    if not args.dry_run and not args.match_check:
        print(f"\nCreated {created}; failed {failed}.")
        for failure in failures:
            print(f"  still pending — {failure}")
        if failed:
            print("Re-run the same command to retry only what is still pending.")
    return 0


def _resolve_authors(
    api: "OpenLibraryAPI", names: list[str], data_dir: Path,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Link local author names to existing Open Library authors."""
    author_keys: dict[str, str] = {}
    near_misses: dict[str, list[str]] = {}
    aliases = load_author_aliases(data_dir)
    print(f"  resolving {len(names)} distinct author names against Open Library"
          f" ({len(aliases)} approved aliases loaded)...")
    aliased = 0
    for name in names:
        if name in aliases:
            author_keys[name] = aliases[name]
            aliased += 1
            continue
        found = api.find_author(name)
        if found["key"]:
            author_keys[name] = found["key"]
        elif found["candidates"]:
            near_misses[name] = found["candidates"][:3]
    print(f"  {len(author_keys)} linked to existing authors "
          f"({aliased} via the approved alias table, {len(author_keys) - aliased} by exact name); "
          f"{len(names) - len(author_keys)} unresolved")
    for name, candidates in sorted(near_misses.items()):
        print(f"    near miss: {name!r} vs {candidates}")
    return author_keys, near_misses


def command_add_editions(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Attach each ALTERNATE_EDITION PDF to the Work its parent already occupies."""
    path = args.data_dir / "decisions.csv"
    rows = read_csv(path)
    work_ids: dict[str, str] = {}
    external_urls: dict[str, str] = {}
    for work in read_csv(args.data_dir / "work_inventory.csv"):
        external_urls[work.get("local_work_id", "")] = work.get("external_url", "")
        for local_path in json.loads(work.get("local_paths_json") or "[]"):
            work_ids[local_path] = work.get("local_work_id", "")

    if args.dry_run:
        print("DRY RUN — no writes were made")
    elif args.match_check:
        print("MATCH CHECK — asking Open Library what each edition would attach to; "
              "nothing is written")
    else:
        print("Attaching alternate editions to their existing Works")

    api = make_api(config, args.data_dir, authenticated=not args.dry_run or args.match_check)
    candidates = [r for r in rows if r.get("status", "").strip().upper() == "ALTERNATE_EDITION"]
    names = sorted({n for r in candidates for n in _split_agents(r.get("detected_author", ""))})
    author_keys: dict[str, str] = {}
    if not args.dry_run or args.match_check:
        author_keys, _ = _resolve_authors(api, names, args.data_dir)

    plan = edition_attachment_plan(rows, work_ids, author_keys)
    attached = failed = 0
    changed = False
    for row, parent_key, problems in plan:
        label = f"{row.get('local_folder')} / {row.get('filename')}"
        if row.get("ol_edition_key", "").strip():
            print(f"SKIP {label}: already attached as {row['ol_edition_key']}")
            continue
        if problems:
            print(f"BLOCKED {label}: " + "; ".join(problems))
            continue
        form = build_addbook_form(
            row, external_url=external_urls.get(work_ids.get(row.get("local_path", ""), ""), ""),
            test=args.match_check, author_keys=author_keys, parent_work_key=parent_key,
        )
        if args.dry_run and not args.match_check:
            print(f"WOULD ATTACH {label} -> {parent_key}")
            print("  " + json.dumps(form, ensure_ascii=False, sort_keys=True))
            continue
        try:
            outcome = api.add_book(form)
            if args.match_check:
                # A /works/ match means addbook would add an edition to that work;
                # a /books/ match means it already has an equivalent edition and
                # would write nothing at all.
                matched = outcome["matched_key"]
                verdict = (
                    "WOULD ATTACH EDITION" if matched.endswith("W")
                    else "ALREADY HAS THIS EDITION" if matched.endswith("M")
                    else "WOULD FORK A NEW WORK"
                )
                print(f"{verdict:26} {label} {matched}")
                continue
            row["ol_work_key"] = parent_key
            row["ol_edition_key"] = outcome.get("edition_key", "")
            row["status"] = "APPROVED"
            row["notes"] = f"Alternate edition attached to {parent_key}"
            attached += 1
            changed = True
            write_csv(path, MatchRecord.fieldnames(), rows)
            print(f"ATTACHED {label}: {row['ol_edition_key']} under {parent_key}")
        except Exception as exc:  # one bad record must not abandon the batch
            failed += 1
            print(f"FAILED  {label}: {type(exc).__name__}: {exc}")
    if changed:
        write_csv(path, MatchRecord.fieldnames(), rows)
    if not args.dry_run and not args.match_check:
        print(f"\nAttached {attached}; failed {failed}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Match local PDF bibliographies to reviewed Open Library lists")
    parser.add_argument("--config", type=Path, help="TOML configuration path (default: config.toml beside script)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Catalog, decisions, and cache directory")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth-check", help="Verify the configured olclient session")
    scan = sub.add_parser("scan", help="Treat every directory below the root as its own list")
    scan.add_argument("library_root", nargs="?", type=Path)
    sub.add_parser("recover-metadata", help="Deep-scan first/last PDF pages for identifiers and metadata")
    sub.add_parser("match", help="Search Open Library and generate review files")
    enrich = sub.add_parser("enrich", help="Resolve DOI/ISBN/title metadata through read-only secondary sources")
    enrich.add_argument(
        "--verified-only", action="store_true",
        help="Refresh verified_metadata.csv rules while reusing all other existing enrichment rows",
    )
    sub.add_parser("map", help="Build stable local Work IDs and direct-folder list memberships")
    sub.add_parser("plan-import", help="Build an offline Open Library eligibility and research plan")
    sub.add_parser("review-summary", help="Summarize decisions.csv")
    publish = sub.add_parser("publish", help="Create/reuse lists and add reviewed Work keys")
    publish.add_argument("--dry-run", action="store_true", help="Show the plan without POST requests")
    create = sub.add_parser("create-missing", help="Create only explicitly approved, sufficiently identified records")
    create.add_argument("--approved-only", action="store_true", help="Required safety acknowledgement")
    create.add_argument("--dry-run", action="store_true", help="Show proposed creations without POST requests")
    create.add_argument(
        "--route", choices=("addbook", "import"), default="addbook",
        help="addbook: /books/add, usable by any logged-in account, identifiers optional (default). "
             "import: /api/import, needs Open Library bot or API-usergroup privileges",
    )
    create.add_argument(
        "--match-check", action="store_true",
        help="addbook route only: ask Open Library what each record would match, writing nothing",
    )
    editions = sub.add_parser(
        "add-editions",
        help="Attach ALTERNATE_EDITION PDFs as further editions of their existing Work",
    )
    editions.add_argument("--dry-run", action="store_true", help="Show the forms without POST requests")
    editions.add_argument(
        "--match-check", action="store_true",
        help="Ask Open Library what each edition would attach to, writing nothing",
    )
    return parser


COMMANDS = {
    "auth-check": command_auth_check,
    "scan": command_scan,
    "recover-metadata": command_recover_metadata,
    "match": command_match,
    "enrich": command_enrich,
    "map": command_map,
    "plan-import": command_plan_import,
    "review-summary": command_review_summary,
    "publish": command_publish,
    "create-missing": command_create_missing,
    "add-editions": command_add_editions,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    # olclient's login retry callback includes request arguments in exception
    # details. Those arguments contain credentials, so these dependency loggers
    # must remain silent even in --verbose mode. Our own exception handler emits
    # a concise credential-free error instead.
    logging.getLogger("backoff").setLevel(logging.CRITICAL)
    logging.getLogger("openlibrary").setLevel(logging.CRITICAL)
    # pypdf may emit hundreds of object-repair warnings for a recoverable file. Keep normal
    # output concise; --verbose restores its diagnostics.
    if not args.verbose:
        logging.getLogger("pypdf").setLevel(logging.ERROR)
    args.data_dir = args.data_dir.expanduser().resolve()
    try:
        return COMMANDS[args.command](args, read_config(args.config))
    except (ValueError, RuntimeError, requests.RequestException) as exc:
        LOG.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.error("Interrupted; no remaining operations were attempted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
