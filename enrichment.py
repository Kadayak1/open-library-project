from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from matcher import choose_match, normalize_text, parse_search_results, score_candidate
from models import CatalogRecord, EnrichmentRecord, MatchRecord, VerifiedMetadataRecord
from pdf_metadata import JUNK_TITLE_RE, is_valid_isbn10, is_valid_isbn13, normalize_isbn

LOG = logging.getLogger(__name__)

BOOKLIKE_TYPES = {
    "book", "monograph", "edited-book", "reference-book", "report", "manual", "text", "dissertation",
}
ARTICLE_TYPES = {
    "journal-article", "proceedings-article", "book-chapter", "reference-entry",
    "conference-paper", "journalarticle", "conferencepaper", "bookchapter", "article",
    "review", "letter", "editorial", "preprint",
}


@dataclass
class ExternalMetadata:
    source: str
    document_type: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    year: str = ""
    isbn_10: str = ""
    isbn_13: str = ""
    doi: str = ""
    url: str = ""
    ocaid: str = ""
    lccn: str = ""


class MetadataAPI:
    """Read-only external metadata client with per-request disk caching."""

    def __init__(self, cache_dir: Path, user_agent: str, delay: float = 0.5) -> None:
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        retry = Retry(
            total=3, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(("GET",)), respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.delay = delay
        self._last = 0.0
        self.google_disabled = False

    def _get(self, provider: str, url: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / provider / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return None if payload == {"_not_found": True} else payload
        remaining = self.delay - (time.monotonic() - self._last)
        if remaining > 0:
            time.sleep(remaining)
        try:
            response = self.session.get(url, params=params, timeout=30)
            self._last = time.monotonic()
            if response.status_code == 404:
                path.write_text(json.dumps({"_not_found": True}), encoding="utf-8")
                return None
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("metadata response is not a JSON object")
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return payload
        except requests.HTTPError as exc:
            if provider == "google_books" and getattr(exc.response, "status_code", None) in (403, 429):
                self.google_disabled = True
                LOG.warning("Google Books rate-limited enrichment; continuing with cached/other sources")
            LOG.debug("%s metadata lookup failed: %s", provider, exc)
            return None
        # Some requests/urllib3 version combinations surface exhausted status
        # retries as an adapter exception outside requests.RequestException.
        # A single public metadata provider must never abort the whole catalog.
        except Exception as exc:
            if provider == "google_books":
                self.google_disabled = True
                LOG.warning("Google Books is unavailable/rate-limited; continuing with cached/other sources")
            LOG.debug("%s metadata lookup failed: %s", provider, exc)
            return None

    def _get_xml(self, provider: str, url: str, params: dict[str, Any]) -> ET.Element | None:
        """Fetch and cache a small, public authority response encoded as XML."""
        key = hashlib.sha256(json.dumps([url, params, "xml"], sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / provider / f"{key}.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            cached = path.read_text(encoding="utf-8")
            if cached == "<!-- not found -->":
                return None
            try:
                return ET.fromstring(cached)
            except ET.ParseError:
                path.unlink(missing_ok=True)
        remaining = self.delay - (time.monotonic() - self._last)
        if remaining > 0:
            time.sleep(remaining)
        try:
            response = self.session.get(url, params=params, timeout=30)
            self._last = time.monotonic()
            if response.status_code == 404:
                path.write_text("<!-- not found -->", encoding="utf-8")
                return None
            response.raise_for_status()
            root = ET.fromstring(response.text)
            path.write_text(response.text, encoding="utf-8")
            return root
        except Exception as exc:
            LOG.debug("%s authority lookup failed: %s", provider, exc)
            return None

    def resolve_doi(self, doi: str) -> ExternalMetadata | None:
        encoded = quote(doi, safe="")
        payload = self._get("crossref", f"https://api.crossref.org/works/{encoded}")
        if payload and isinstance(payload.get("message"), dict):
            return _crossref_metadata(payload["message"], doi)
        payload = self._get("datacite", f"https://api.datacite.org/dois/{encoded}")
        attrs = (payload or {}).get("data", {}).get("attributes", {})
        if not isinstance(attrs, dict) or not attrs:
            return None
        titles = attrs.get("titles") or []
        creators = attrs.get("creators") or []
        identifiers = [item.get("identifier", "") for item in attrs.get("identifiers") or [] if isinstance(item, dict)]
        isbn10, isbn13 = _identifier_pair(identifiers)
        resource_type = attrs.get("types") or {}
        return ExternalMetadata(
            source="datacite",
            document_type=str(resource_type.get("resourceTypeGeneral", "")),
            title=str(titles[0].get("title", "")) if titles and isinstance(titles[0], dict) else "",
            authors=[str(item.get("name", "")) for item in creators if isinstance(item, dict) and item.get("name")],
            publisher=str(attrs.get("publisher", "")), year=str(attrs.get("publicationYear", "")),
            isbn_10=isbn10, isbn_13=isbn13, doi=doi, url=str(attrs.get("url", "")),
        )

    def search_crossref(self, title: str) -> list[ExternalMetadata]:
        if len(normalize_text(title).split()) < 4:
            return []
        payload = self._get(
            "crossref_search", "https://api.crossref.org/works",
            {"query.title": title, "rows": 5},
        )
        items = (payload or {}).get("message", {}).get("items", [])
        return [
            _crossref_metadata(item, str(item.get("DOI", "")))
            for item in items if isinstance(item, dict) and item.get("title")
        ]

    def search_openalex(self, title: str) -> list[ExternalMetadata]:
        if len(normalize_text(title).split()) < 4:
            return []
        params: dict[str, Any] = {"search": title, "per_page": 5}
        api_key = __import__("os").getenv("OPENALEX_API_KEY", "")
        if api_key:
            params["api_key"] = api_key
        payload = self._get("openalex", "https://api.openalex.org/works", params)
        results = []
        for item in (payload or {}).get("results", []) or []:
            if not isinstance(item, dict):
                continue
            authors = []
            for authorship in item.get("authorships") or []:
                author = authorship.get("author", {}) if isinstance(authorship, dict) else {}
                if author.get("display_name"):
                    authors.append(str(author["display_name"]))
            location = item.get("primary_location") or {}
            source = location.get("source") or {} if isinstance(location, dict) else {}
            doi = str(item.get("doi") or "")
            if doi.lower().startswith("https://doi.org/"):
                doi = doi[len("https://doi.org/"):]
            results.append(ExternalMetadata(
                source="openalex", document_type=str(item.get("type", "")),
                title=str(item.get("title") or item.get("display_name") or ""), authors=authors,
                publisher=str(source.get("display_name", "")) if isinstance(source, dict) else "",
                year=str(item.get("publication_year") or ""), doi=doi,
                url=str(location.get("landing_page_url") or item.get("id") or "") if isinstance(location, dict) else str(item.get("id") or ""),
            ))
        return results

    def search_google_books(self, title: str, author: str = "", api_key: str = "") -> list[ExternalMetadata]:
        if self.google_disabled or len(normalize_text(title).split()) < 3:
            return []
        query = f'intitle:"{title}"'
        if author and _reliable_author(author):
            query += f' inauthor:"{author}"'
        params: dict[str, Any] = {"q": query, "maxResults": 5, "projection": "lite", "printType": "books"}
        if api_key:
            params["key"] = api_key
        payload = self._get("google_books", "https://www.googleapis.com/books/v1/volumes", params)
        results = []
        for item in (payload or {}).get("items", []) or []:
            info = item.get("volumeInfo", {}) if isinstance(item, dict) else {}
            identifiers = [entry.get("identifier", "") for entry in info.get("industryIdentifiers") or [] if isinstance(entry, dict)]
            isbn10, isbn13 = _identifier_pair(identifiers)
            results.append(ExternalMetadata(
                source="google_books", document_type="book", title=str(info.get("title", "")),
                authors=[str(a) for a in info.get("authors") or []], publisher=str(info.get("publisher", "")),
                year=_first_year(str(info.get("publishedDate", ""))), isbn_10=isbn10, isbn_13=isbn13,
                url=str(info.get("infoLink", "")),
            ))
        return results

    def search_internet_archive(self, title: str) -> list[ExternalMetadata]:
        """Search existing Internet Archive text metadata without importing anything."""
        if len(normalize_text(title).split()) < 3:
            return []
        escaped = re.sub(r'(["\\])', r"\\\1", title)
        payload = self._get(
            "internet_archive", "https://archive.org/advancedsearch.php",
            {
                "q": f'mediatype:texts AND title:("{escaped}")',
                "fl[]": ["identifier", "title", "creator", "publisher", "date", "year"],
                "rows": 10, "page": 1, "output": "json",
            },
        )
        docs = (payload or {}).get("response", {}).get("docs", [])
        results = []
        for item in docs or []:
            if not isinstance(item, dict) or not item.get("identifier") or not item.get("title"):
                continue
            creators = item.get("creator") or []
            if isinstance(creators, str):
                creators = [creators]
            publishers = item.get("publisher") or []
            if isinstance(publishers, list):
                publisher = "; ".join(str(value) for value in publishers if value)
            else:
                publisher = str(publishers)
            date = str(item.get("date") or item.get("year") or "")
            identifier = str(item["identifier"])
            results.append(ExternalMetadata(
                source="internet_archive", document_type="text",
                title=str(item["title"]), authors=[str(value) for value in creators if value],
                publisher=publisher, year=_first_year(date),
                url=f"https://archive.org/details/{identifier}", ocaid=identifier,
            ))
        return results

    def search_library_of_congress(self, title: str) -> list[ExternalMetadata]:
        """Search the full Library of Congress catalog through its public SRU gateway."""
        if len(normalize_text(title).split()) < 3:
            return []
        cql_title = re.sub(r'["\r\n]+', " ", title).strip()
        root = self._get_xml(
            "library_of_congress", "http://lx2.loc.gov:210/LCDB",
            {
                "version": "1.1", "operation": "searchRetrieve",
                "query": f'dc.title="{cql_title}"', "maximumRecords": 8,
                "recordSchema": "mods",
            },
        )
        if root is None:
            return []
        ns = {
            "zs": "http://www.loc.gov/zing/srw/",
            "mods": "http://www.loc.gov/mods/v3",
        }
        results: list[ExternalMetadata] = []
        for mods in root.findall(".//zs:recordData/mods:mods", ns):
            title_info = next(
                (node for node in mods.findall("mods:titleInfo", ns) if not node.get("type")),
                None,
            )
            if title_info is None:
                continue
            nonsort = title_info.findtext("mods:nonSort", default="", namespaces=ns).strip()
            main_title = title_info.findtext("mods:title", default="", namespaces=ns).strip()
            subtitle = title_info.findtext("mods:subTitle", default="", namespaces=ns).strip()
            candidate_title = " ".join(part for part in (nonsort, main_title) if part).strip()
            if subtitle:
                candidate_title = f"{candidate_title}: {subtitle}"
            if not candidate_title:
                continue
            name_nodes = mods.findall("mods:name", ns)
            primary_names = [node for node in name_nodes if node.get("usage") == "primary"] or name_nodes
            authors = []
            for node in primary_names[:6]:
                parts = [
                    (part.text or "").strip(" ,")
                    for part in node.findall("mods:namePart", ns)
                    if part.get("type") not in {"date", "termsOfAddress"} and (part.text or "").strip()
                ]
                if parts:
                    authors.append(" ".join(parts))
            origin = mods.find("mods:originInfo", ns)
            publisher = year = ""
            if origin is not None:
                publisher = origin.findtext("mods:publisher", default="", namespaces=ns).strip()
                if not publisher:
                    publisher = origin.findtext("mods:agent/mods:namePart", default="", namespaces=ns).strip()
                year = _first_year(origin.findtext("mods:dateIssued", default="", namespaces=ns))
            identifiers = [
                (node.get("type", "").casefold(), (node.text or "").strip())
                for node in mods.findall("mods:identifier", ns)
            ]
            isbn10, isbn13 = _identifier_pair([value for kind, value in identifiers if kind == "isbn"])
            lccn = next((re.sub(r"[^A-Za-z0-9]", "", value) for kind, value in identifiers if kind == "lccn"), "")
            issuance = origin.findtext("mods:issuance", default="", namespaces=ns).casefold() if origin is not None else ""
            results.append(ExternalMetadata(
                source="library_of_congress", document_type="book" if "monograph" in issuance else "text",
                title=candidate_title, authors=authors, publisher=publisher, year=year,
                isbn_10=isbn10, isbn_13=isbn13, lccn=lccn,
                url=f"https://lccn.loc.gov/{lccn}" if lccn else "https://catalog.loc.gov/",
            ))
        return results


def _date_year(message: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = (message.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            return str(parts[0][0])
    return ""


def _crossref_metadata(message: dict[str, Any], doi: str) -> ExternalMetadata:
    titles = message.get("title") or []
    authors = []
    for person in message.get("author") or []:
        name = " ".join(filter(None, (person.get("given"), person.get("family")))).strip()
        if name:
            authors.append(name)
    isbn10, isbn13 = _identifier_pair(message.get("ISBN") or [])
    return ExternalMetadata(
        source="crossref", document_type=str(message.get("type", "")),
        title=str(titles[0]) if titles else "", authors=authors,
        publisher=str(message.get("publisher", "")), year=_date_year(message),
        isbn_10=isbn10, isbn_13=isbn13, doi=doi,
        url=str(message.get("URL", "")),
    )


def _first_year(value: str) -> str:
    match = re.search(r"\b(?:18|19|20)\d{2}\b", value or "")
    return match.group(0) if match else ""


def _identifier_pair(values: list[Any]) -> tuple[str, str]:
    isbn10 = isbn13 = ""
    for value in values:
        normalized = normalize_isbn(str(value))
        if not isbn13 and is_valid_isbn13(normalized):
            isbn13 = normalized
        if not isbn10 and is_valid_isbn10(normalized):
            isbn10 = normalized
    return isbn10, isbn13


def _reliable_author(value: str) -> bool:
    value = (value or "").strip()
    return 3 <= len(value) <= 120 and not re.search(r"https?://|copyright|creative commons|\bthe\b.*\bthat\b", value, re.I)


def filename_title(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    stem = re.sub(r"\s*\((?:copia en conflicto|copy|final)[^)]*\)\s*$", "", stem, flags=re.I)
    # In curated filenames, a leading author and year are commonly followed by the real title.
    year = re.search(r"\b(?:19|20)\d{2}\b", stem)
    if year:
        after = stem[year.end():].strip(" -–—_.")
        if len(normalize_text(after).split()) >= 3:
            stem = after
        elif year.start() == 0:
            stem = stem[year.end():].strip(" -–—_.") or stem
    stem = re.sub(r"\b(?:final|small|main)\b(?:[ _-]*v?\d+)?$", "", stem, flags=re.I).strip(" -_.")
    return stem


def title_variants(record: CatalogRecord) -> list[str]:
    values = [record.detected_title, record.pdf_title, filename_title(record.filename)]
    output = []
    for value in values:
        value = re.sub(r"\s+", " ", value or "").strip()
        if not value or JUNK_TITLE_RE.search(value) or len(normalize_text(value).split()) < 2:
            continue
        if normalize_text(value) not in {normalize_text(existing) for existing in output}:
            output.append(value)
    return output


def _external_doc(meta: ExternalMetadata) -> dict[str, Any]:
    return {
        "key": "/works/OL0W", "title": meta.title, "author_name": meta.authors,
        "publisher": [meta.publisher] if meta.publisher else [],
        "publish_year": [int(meta.year)] if meta.year.isdigit() else [],
        "isbn": [v for v in (meta.isbn_10, meta.isbn_13) if v],
    }


def _meta_score(record: CatalogRecord, meta: ExternalMetadata, title: str | None = None) -> float:
    expected = CatalogRecord(
        detected_title=title or record.detected_title,
        detected_author=record.detected_author,
        publisher=record.publisher, year=record.year,
        isbn_10=record.isbn_10, isbn_13=record.isbn_13,
    )
    return score_candidate(expected, _external_doc(meta))[0]


def _text_similarity(left: str, right: str) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    at, bt = set(a.split()), set(b.split())
    token = len(at & bt) / len(at | bt) if at | bt else 0.0
    return max(sequence, 0.7 * sequence + 0.3 * token)


def external_similarity(record: CatalogRecord, meta: ExternalMetadata, title: str) -> tuple[float, list[str]]:
    title_score = _text_similarity(title, meta.title)
    author_score = max(
        (_text_similarity(record.detected_author, author) for author in meta.authors), default=0.0
    ) if _reliable_author(record.detected_author) else 0.0
    year_match = bool(record.year and meta.year and record.year == meta.year)
    if _reliable_author(record.detected_author):
        score = 0.82 * title_score + 0.13 * author_score + (0.05 if year_match else 0.0)
    else:
        score = 0.94 * title_score + (0.06 if year_match else 0.0)
    reasons = [f"title={title_score:.2f}", f"author={author_score:.2f}"]
    if year_match:
        reasons.append("year=exact")
    return score, reasons


def find_ol_match(ol_api: Any, meta: ExternalMetadata) -> MatchRecord | None:
    record = CatalogRecord(
        detected_title=meta.title, detected_author="; ".join(meta.authors), publisher=meta.publisher,
        year=meta.year, isbn_10=meta.isbn_10, isbn_13=meta.isbn_13,
    )
    docs: dict[str, dict[str, Any]] = {}
    if meta.ocaid:
        for doc in parse_search_results(ol_api.search(f"ocaid:{meta.ocaid}", limit=10)):
            docs[str(doc["key"])] = doc
    if meta.lccn:
        for doc in parse_search_results(ol_api.search(f"lccn:{meta.lccn}", limit=10)):
            docs[str(doc["key"])] = doc
    for isbn in (meta.isbn_13, meta.isbn_10):
        if isbn:
            for doc in parse_search_results(ol_api.search(f"isbn:{isbn}", limit=10)):
                docs[str(doc["key"])] = doc
    if meta.title:
        author = meta.authors[0] if meta.authors else ""
        query = f'title:"{meta.title}"' + (f' author:"{author}"' if author else "")
        for doc in parse_search_results(ol_api.search(query, limit=10)):
            docs[str(doc["key"])] = doc
    match = choose_match(record, list(docs.values()))
    return match if match.status == "MATCHED" else None


def select_verified_metadata(
    record: CatalogRecord, rules: list[VerifiedMetadataRecord],
) -> VerifiedMetadataRecord | None:
    """Select one auditable, folder-scoped metadata rule for a local PDF."""
    folder = unicodedata.normalize("NFC", record.local_folder).casefold()
    filename = unicodedata.normalize("NFC", record.filename).casefold()
    matches = [
        rule for rule in rules
        if unicodedata.normalize("NFC", rule.local_folder).casefold() == folder
        and fnmatchcase(filename, unicodedata.normalize("NFC", rule.filename_pattern).casefold())
    ]
    if len(matches) > 1:
        patterns = ", ".join(repr(rule.filename_pattern) for rule in matches)
        raise ValueError(f"Multiple verified metadata rules match {record.filename!r}: {patterns}")
    return matches[0] if matches else None


def _verified_result(
    result: EnrichmentRecord, rule: VerifiedMetadataRecord, ol_api: Any, duplicate_of: str,
) -> EnrichmentRecord:
    role = rule.publication_role.strip().upper()
    allowed_roles = {"PUBLICATION", "COMPONENT", "ALTERNATE_EDITION", "NONBOOK"}
    if role not in allowed_roles:
        raise ValueError(
            f"Invalid publication_role {rule.publication_role!r} for {rule.filename_pattern!r}; "
            f"expected one of {sorted(allowed_roles)}"
        )
    normalized_isbn10 = normalize_isbn(rule.isbn_10)
    normalized_isbn13 = normalize_isbn(rule.isbn_13)
    if normalized_isbn10 and not is_valid_isbn10(normalized_isbn10):
        raise ValueError(f"Invalid verified ISBN-10 {rule.isbn_10!r} for {rule.filename_pattern!r}")
    if normalized_isbn13 and not is_valid_isbn13(normalized_isbn13):
        raise ValueError(f"Invalid verified ISBN-13 {rule.isbn_13!r} for {rule.filename_pattern!r}")
    meta = ExternalMetadata(
        source="verified_manifest", document_type=rule.document_type,
        title=rule.canonical_title,
        authors=[part.strip() for part in rule.canonical_author.split(";") if part.strip()],
        publisher=rule.canonical_publisher, year=rule.canonical_year,
        isbn_10=normalized_isbn10, isbn_13=normalized_isbn13, doi=rule.doi,
        url=rule.source_url, ocaid=rule.ocaid, lccn=rule.lccn,
    )
    result.source = "verified_manifest"
    result.document_type = rule.document_type
    result.canonical_title = rule.canonical_title
    result.canonical_author = rule.canonical_author
    result.canonical_publisher = rule.canonical_publisher
    result.canonical_year = rule.canonical_year
    result.isbn_10 = normalized_isbn10 or result.isbn_10
    result.isbn_13 = normalized_isbn13 or result.isbn_13
    result.doi = rule.doi or result.doi
    result.ocaid = rule.ocaid or result.ocaid
    result.lccn = rule.lccn or result.lccn
    result.external_url = rule.source_url
    result.parent_publication_id = rule.parent_publication_id
    result.publication_role = role
    result.confidence = "1.000"

    if duplicate_of:
        result.verification_status = "DUPLICATE_LOCAL"
        result.notes = "Byte-identical local PDF; verified metadata links it to the same intellectual work"
        return result

    ol_match = find_ol_match(ol_api, meta)
    if ol_match:
        result.verification_status = "OL_MATCHED"
        result.ol_work_key, result.ol_edition_key = ol_match.ol_work_key, ol_match.ol_edition_key
        result.notes = rule.notes or "Verified metadata resolves to a corroborated Open Library Work"
        return result

    status_by_role = {
        "PUBLICATION": "CREATE_CANDIDATE",
        "COMPONENT": "PARENT_COMPONENT",
        "ALTERNATE_EDITION": "ALTERNATE_EDITION",
        "NONBOOK": "VERIFIED_NONBOOK",
    }
    result.verification_status = status_by_role[role]
    result.notes = rule.notes or {
        "PUBLICATION": "Verified publication metadata, but no Open Library Work was found",
        "COMPONENT": "Local PDF is one component of a verified parent publication",
        "ALTERNATE_EDITION": "Verified alternate language/edition of the same parent publication",
        "NONBOOK": "Verified serial, legal, map, or other non-book document",
    }[role]
    return result


def enrich_record(
    record: CatalogRecord, previous: dict[str, str], ol_api: Any, metadata_api: MetadataAPI,
    google_api_key: str = "", duplicate_of: str = "",
    verified: VerifiedMetadataRecord | None = None,
) -> EnrichmentRecord:
    result = EnrichmentRecord(
        local_folder=record.local_folder, local_path=record.local_path, filename=record.filename,
        previous_status=previous.get("status", ""), doi=record.doi,
        isbn_10=record.isbn_10, isbn_13=record.isbn_13, duplicate_of_path=duplicate_of,
    )
    if previous.get("status", "").upper() in {"MATCHED", "APPROVED"}:
        result.verification_status = "ALREADY_MATCHED"
        result.source = "openlibrary"
        result.canonical_title = previous.get("ol_title", "") or record.detected_title
        result.canonical_author = previous.get("ol_author", "") or record.detected_author
        result.ol_work_key = previous.get("ol_work_key", "")
        result.ol_edition_key = previous.get("ol_edition_key", "")
        result.confidence = previous.get("confidence", "")
        return result

    if verified:
        return _verified_result(result, verified, ol_api, duplicate_of)

    meta = metadata_api.resolve_doi(record.doi) if record.doi else None
    if meta:
        result.source = meta.source
        result.document_type = meta.document_type
        result.canonical_title = meta.title
        result.canonical_author = "; ".join(meta.authors)
        result.canonical_publisher = meta.publisher
        result.canonical_year = meta.year
        result.isbn_10 = meta.isbn_10 or result.isbn_10
        result.isbn_13 = meta.isbn_13 or result.isbn_13
        result.ocaid = meta.ocaid or result.ocaid
        result.lccn = meta.lccn or result.lccn
        result.external_url = meta.url
        result.confidence = "1.000"
        if meta.document_type.casefold() in ARTICLE_TYPES:
            result.verification_status = "VERIFIED_EXTERNAL"
            result.notes = "DOI metadata verifies a non-book publication; do not create it as an Open Library book"
            return result
        ol_match = find_ol_match(ol_api, meta)
        if ol_match:
            result.verification_status = "OL_MATCHED"
            result.ol_work_key, result.ol_edition_key = ol_match.ol_work_key, ol_match.ol_edition_key
            result.notes = f"Exact DOI metadata led to Open Library via {'ISBN' if meta.isbn_10 or meta.isbn_13 else 'title/author'}"
            return result
        if meta.document_type.casefold() in BOOKLIKE_TYPES:
            result.verification_status = "CREATE_CANDIDATE"
            result.notes = "Verified book/report metadata but no Open Library Work found"
            return result

    # Existing local ISBNs get another exact identifier pass even when the first title conflicted.
    if record.isbn_10 or record.isbn_13:
        isbn_meta = ExternalMetadata(
            source="local_isbn", document_type="book", title=record.detected_title,
            authors=[record.detected_author] if record.detected_author else [], publisher=record.publisher,
            year=record.year, isbn_10=record.isbn_10, isbn_13=record.isbn_13,
        )
        ol_match = find_ol_match(ol_api, isbn_meta)
        if ol_match:
            result.verification_status, result.source = "OL_MATCHED", "local_isbn"
            result.ol_work_key, result.ol_edition_key = ol_match.ol_work_key, ol_match.ol_edition_key
            result.canonical_title, result.canonical_author = ol_match.ol_title, ol_match.ol_author
            result.confidence, result.notes = ol_match.confidence, "Checksum-valid ISBN with bibliographic corroboration"
            return result

    variants = title_variants(record)
    best_review: MatchRecord | None = None
    best_review_title = ""
    for title in variants:
        expected = CatalogRecord(
            detected_title=title, detected_author=record.detected_author,
            publisher=record.publisher, year=record.year,
        )
        author = record.detected_author if _reliable_author(record.detected_author) else ""
        query = f'title:"{title}"' + (f' author:"{author}"' if author else "")
        match = choose_match(expected, parse_search_results(ol_api.search(query, limit=10)))
        if match.status == "MATCHED":
            result.verification_status, result.source = "OL_MATCHED", "openlibrary_title"
            result.canonical_title, result.canonical_author = match.ol_title, match.ol_author
            result.ol_work_key, result.ol_edition_key = match.ol_work_key, match.ol_edition_key
            result.confidence, result.notes = match.confidence, f"Matched using title variant: {title}"
            return result
        if match.status == "REVIEW" and (best_review is None or float(match.confidence or 0) > float(best_review.confidence or 0)):
            best_review, best_review_title = match, title

    # Crossref title search can recover a DOI absent from a scholarly PDF. Require an almost exact
    # long title plus separation from the runner-up; author/year corroboration further improves it.
    for title in sorted(variants, key=lambda value: len(normalize_text(value).split()), reverse=True)[:2]:
        crossref_results = metadata_api.search_crossref(title)
        ranked_crossref = sorted(
            ((external_similarity(record, item, title)[0], item, external_similarity(record, item, title)[1]) for item in crossref_results),
            reverse=True, key=lambda entry: entry[0],
        )
        if not ranked_crossref:
            continue
        crossref_score, crossref_meta, crossref_reasons = ranked_crossref[0]
        runner_up = ranked_crossref[1][0] if len(ranked_crossref) > 1 else 0.0
        title_signal = float(crossref_reasons[0].split("=", 1)[1])
        long_title = len(normalize_text(title).split()) >= 6
        corroborated = "year=exact" in crossref_reasons or float(crossref_reasons[1].split("=", 1)[1]) >= 0.75 or long_title
        if crossref_score >= 0.90 and title_signal >= 0.90 and crossref_score - runner_up >= 0.05 and corroborated:
            result.source = "crossref_title"
            result.document_type = crossref_meta.document_type
            result.canonical_title = crossref_meta.title
            result.canonical_author = "; ".join(crossref_meta.authors)
            result.canonical_publisher = crossref_meta.publisher
            result.canonical_year = crossref_meta.year
            result.doi = crossref_meta.doi or result.doi
            result.isbn_10, result.isbn_13 = crossref_meta.isbn_10, crossref_meta.isbn_13
            result.lccn = crossref_meta.lccn or result.lccn
            result.external_url = crossref_meta.url
            result.confidence = f"{crossref_score:.3f}"
            if crossref_meta.document_type.casefold() in ARTICLE_TYPES:
                result.verification_status = "VERIFIED_EXTERNAL"
                result.notes = "Near-exact long-title search recovered authoritative Crossref DOI metadata for a non-book publication"
                return result
            ol_match = find_ol_match(ol_api, crossref_meta)
            if ol_match:
                result.verification_status = "OL_MATCHED"
                result.ol_work_key, result.ol_edition_key = ol_match.ol_work_key, ol_match.ol_edition_key
                result.notes = "Crossref title metadata recovered an identifier resolving to Open Library"
            elif crossref_meta.document_type.casefold() in BOOKLIKE_TYPES:
                result.verification_status = "CREATE_CANDIDATE"
                result.notes = "Crossref verifies a book/report, but no Open Library Work was found"
            else:
                result.verification_status = "NEEDS_REVIEW"
                result.notes = "Crossref strongly corroborates the title, but publication type needs review"
            return result

    # OpenAlex covers scholarly records beyond Crossref. Apply the same strict long-title rule and
    # use it only for classification/identifier recovery, never as an Open Library write source.
    for title in sorted(variants, key=lambda value: len(normalize_text(value).split()), reverse=True)[:1]:
        openalex_results = metadata_api.search_openalex(title)
        ranked_openalex = sorted(
            ((external_similarity(record, item, title)[0], item, external_similarity(record, item, title)[1]) for item in openalex_results),
            reverse=True, key=lambda entry: entry[0],
        )
        if not ranked_openalex:
            continue
        openalex_score, openalex_meta, openalex_reasons = ranked_openalex[0]
        runner_up = ranked_openalex[1][0] if len(ranked_openalex) > 1 else 0.0
        title_signal = float(openalex_reasons[0].split("=", 1)[1])
        long_title = len(normalize_text(title).split()) >= 6
        corroborated = "year=exact" in openalex_reasons or float(openalex_reasons[1].split("=", 1)[1]) >= 0.75 or long_title
        if openalex_score >= 0.90 and title_signal >= 0.90 and openalex_score - runner_up >= 0.05 and corroborated:
            result.source = "openalex_title"
            result.document_type = openalex_meta.document_type
            result.canonical_title = openalex_meta.title
            result.canonical_author = "; ".join(openalex_meta.authors)
            result.canonical_publisher = openalex_meta.publisher
            result.canonical_year = openalex_meta.year
            result.doi = openalex_meta.doi or result.doi
            result.lccn = openalex_meta.lccn or result.lccn
            result.external_url = openalex_meta.url
            result.confidence = f"{openalex_score:.3f}"
            if openalex_meta.document_type.casefold() in ARTICLE_TYPES:
                result.verification_status = "VERIFIED_EXTERNAL"
                result.notes = "Near-exact long-title search verified a non-book scholarly work in OpenAlex"
                return result
            ol_match = find_ol_match(ol_api, openalex_meta)
            if ol_match:
                result.verification_status = "OL_MATCHED"
                result.ol_work_key, result.ol_edition_key = ol_match.ol_work_key, ol_match.ol_edition_key
                result.notes = "OpenAlex metadata recovered an identifier/title resolving to Open Library"
            elif openalex_meta.document_type.casefold() in BOOKLIKE_TYPES:
                result.verification_status = "CREATE_CANDIDATE"
                result.notes = "OpenAlex verifies a book/report/dissertation, but no Open Library Work was found"
            else:
                result.verification_status = "NEEDS_REVIEW"
                result.notes = "OpenAlex strongly corroborates the title, but publication type needs review"
            return result

    # The Library of Congress catalog covers monographs and reports that are absent
    # from DOI-centric services. Treat identical-title editions as one authority
    # cluster so legitimate reprints do not fail a runner-up margin test.
    for title in sorted(variants, key=lambda value: len(normalize_text(value).split()), reverse=True)[:2]:
        loc_results = metadata_api.search_library_of_congress(title)
        ranked_loc = sorted(
            (
                (external_similarity(record, meta, title)[0], meta, external_similarity(record, meta, title)[1])
                for meta in loc_results
            ),
            reverse=True, key=lambda entry: entry[0],
        )
        if not ranked_loc:
            continue
        score, loc_meta, reasons = ranked_loc[0]
        distinct_runner_up = next(
            (
                candidate_score for candidate_score, candidate, _ in ranked_loc[1:]
                if normalize_text(candidate.title) != normalize_text(loc_meta.title)
            ),
            0.0,
        )
        title_signal = float(reasons[0].split("=", 1)[1])
        author_signal = float(reasons[1].split("=", 1)[1])
        exact_year = "year=exact" in reasons
        long_title = len(normalize_text(title).split()) >= 6
        corroborated = exact_year or author_signal >= 0.75 or long_title
        separated = distinct_runner_up == 0.0 or score - distinct_runner_up >= 0.05
        if score >= 0.90 and title_signal >= 0.96 and corroborated and separated:
            result.source = "library_of_congress"
            result.document_type = loc_meta.document_type
            result.canonical_title = loc_meta.title
            result.canonical_author = "; ".join(loc_meta.authors)
            result.canonical_publisher = loc_meta.publisher
            result.canonical_year = loc_meta.year
            result.isbn_10, result.isbn_13 = loc_meta.isbn_10, loc_meta.isbn_13
            result.lccn = loc_meta.lccn
            result.external_url = loc_meta.url
            result.confidence = f"{score:.3f}"
            ol_match = find_ol_match(ol_api, loc_meta)
            if ol_match:
                result.verification_status = "OL_MATCHED"
                result.ol_work_key, result.ol_edition_key = ol_match.ol_work_key, ol_match.ol_edition_key
                result.notes = "Library of Congress identifier and metadata resolved to an existing Open Library Work"
            else:
                result.verification_status = "CREATE_CANDIDATE"
                result.notes = "Library of Congress verifies a monograph/report, but no Open Library Work was found"
            return result

    # Search Google Books only after DOI/ISBN/direct OL and library-catalog paths fail.
    for title in variants[:2]:
        google_results = metadata_api.search_google_books(title, record.detected_author, google_api_key)
        ranked = sorted(((external_similarity(record, meta, title)[0], meta) for meta in google_results), reverse=True, key=lambda pair: pair[0])
        if not ranked:
            continue
        score, google_meta = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        if score >= 0.88 and score - runner_up >= 0.08:
            ol_match = find_ol_match(ol_api, google_meta)
            result.source = "google_books"
            result.document_type = "book"
            result.canonical_title = google_meta.title
            result.canonical_author = "; ".join(google_meta.authors)
            result.canonical_publisher = google_meta.publisher
            result.canonical_year = google_meta.year
            result.isbn_10, result.isbn_13 = google_meta.isbn_10, google_meta.isbn_13
            result.lccn = google_meta.lccn or result.lccn
            result.external_url = google_meta.url
            result.confidence = f"{score:.3f}"
            if ol_match:
                result.verification_status = "OL_MATCHED"
                result.ol_work_key, result.ol_edition_key = ol_match.ol_work_key, ol_match.ol_edition_key
                result.notes = "Google Books corroborated metadata and supplied an identifier resolving to Open Library"
            else:
                result.verification_status = "CREATE_CANDIDATE"
                result.notes = "Google Books strongly corroborated a book, but no Open Library Work was found"
            return result

    # Internet Archive can provide a source record for reports and books that are
    # absent from ISBN-centric catalogs. Exact-title authority matches are useful
    # both for finding an existing OL Work by OCAID and for a reviewed IA import.
    for title in sorted(variants, key=lambda value: len(normalize_text(value).split()), reverse=True)[:2]:
        ia_results = metadata_api.search_internet_archive(title)
        ranked_ia = sorted(
            (
                (external_similarity(record, meta, title)[0], meta, external_similarity(record, meta, title)[1])
                for meta in ia_results
            ),
            reverse=True, key=lambda entry: entry[0],
        )
        if not ranked_ia:
            continue
        score, ia_meta, reasons = ranked_ia[0]
        runner_up = ranked_ia[1][0] if len(ranked_ia) > 1 else 0.0
        title_signal = float(reasons[0].split("=", 1)[1])
        author_signal = float(reasons[1].split("=", 1)[1])
        long_title = len(normalize_text(title).split()) >= 6
        corroborated = "year=exact" in reasons or author_signal >= 0.75 or long_title
        if score >= 0.90 and title_signal >= 0.93 and score - runner_up >= 0.06 and corroborated:
            result.source = "internet_archive"
            result.document_type = "text"
            result.canonical_title = ia_meta.title
            result.canonical_author = "; ".join(ia_meta.authors)
            result.canonical_publisher = ia_meta.publisher
            result.canonical_year = ia_meta.year
            result.external_url = ia_meta.url
            result.ocaid = ia_meta.ocaid
            result.lccn = ia_meta.lccn or result.lccn
            result.confidence = f"{score:.3f}"
            ol_match = find_ol_match(ol_api, ia_meta)
            if ol_match:
                result.verification_status = "OL_MATCHED"
                result.ol_work_key, result.ol_edition_key = ol_match.ol_work_key, ol_match.ol_edition_key
                result.notes = "Exact Internet Archive metadata resolved to an existing Open Library Work"
            else:
                result.verification_status = "NEEDS_REVIEW"
                result.notes = "Exact Internet Archive text record found; review document scope before importing its OCAID"
            return result

    if duplicate_of:
        result.verification_status = "DUPLICATE_LOCAL"
        result.source = "sha256"
        result.notes = "Byte-identical to another local PDF; only one intellectual work should be listed"
    elif best_review:
        result.verification_status = "NEEDS_REVIEW"
        result.source = "openlibrary_title"
        result.canonical_title, result.canonical_author = best_review.ol_title, best_review.ol_author
        result.ol_work_key, result.ol_edition_key = best_review.ol_work_key, best_review.ol_edition_key
        result.confidence = best_review.confidence
        result.notes = f"Plausible candidate from title variant {best_review_title!r}; manual confirmation required"
        result.candidates_json = best_review.candidates_json
    else:
        result.verification_status = "UNIDENTIFIED"
        result.notes = "No sufficiently strong DOI, ISBN, title, or author match"
    return result


def apply_enrichment(base: MatchRecord, enrichment: EnrichmentRecord) -> MatchRecord:
    row = MatchRecord(**base.to_dict())
    status = enrichment.verification_status
    if status == "OL_MATCHED":
        row.status = "MATCHED"
        row.ol_work_key, row.ol_edition_key = enrichment.ol_work_key, enrichment.ol_edition_key
        row.ol_title, row.ol_author = enrichment.canonical_title, enrichment.canonical_author
        row.confidence, row.notes = enrichment.confidence, enrichment.notes
        row.isbn_10, row.isbn_13 = enrichment.isbn_10 or row.isbn_10, enrichment.isbn_13 or row.isbn_13
        row.ocaid = enrichment.ocaid or row.ocaid
        row.lccn = enrichment.lccn or row.lccn
    elif status in {
        "VERIFIED_EXTERNAL", "DUPLICATE_LOCAL", "CREATE_CANDIDATE",
        "PARENT_COMPONENT", "ALTERNATE_EDITION", "VERIFIED_NONBOOK",
    }:
        row.status = status
        row.ol_work_key = row.ol_edition_key = ""
        row.detected_title = enrichment.canonical_title or row.detected_title
        row.detected_author = enrichment.canonical_author or row.detected_author
        row.publisher = enrichment.canonical_publisher or row.publisher
        row.year = enrichment.canonical_year or row.year
        row.isbn_10, row.isbn_13 = enrichment.isbn_10 or row.isbn_10, enrichment.isbn_13 or row.isbn_13
        row.ocaid = enrichment.ocaid or row.ocaid
        row.lccn = enrichment.lccn or row.lccn
        row.confidence, row.notes = enrichment.confidence, enrichment.notes
    elif status == "NEEDS_REVIEW":
        row.status = "REVIEW"
        row.ol_work_key, row.ol_edition_key = enrichment.ol_work_key, enrichment.ol_edition_key
        row.ol_title, row.ol_author = enrichment.canonical_title, enrichment.canonical_author
        row.confidence, row.notes, row.candidates_json = enrichment.confidence, enrichment.notes, enrichment.candidates_json
        row.ocaid = enrichment.ocaid or row.ocaid
        row.lccn = enrichment.lccn or row.lccn
    return row
