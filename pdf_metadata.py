from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Iterable

from models import CatalogRecord, FolderRecord

LOG = logging.getLogger(__name__)

JUNK_TITLE_RE = re.compile(
    r"(?:microsoft\s+word|\.(?:docx?|eps|pptx?)\b|final[_ -]?version|untitled|document\d*$)", re.I
)
ISBN_LABEL_RE = re.compile(
    r"ISBN(?:-1[03])?\s*[:#]?\s*((?:97[89][\s-]*)?[0-9][0-9Xx\s-]{8,20})", re.I
)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
YEAR_RE = re.compile(r"\b(?:18|19|20)\d{2}\b")
AUTHOR_RE = re.compile(
    r"^(?:by|por|authors?|autores?|prepared by|elaborado por)\s*[:\-]?\s*(.+)$", re.I
)
PUBLISHER_RE = re.compile(
    r"\b(?:university|press|institute|instituto|foundation|fundaci[oó]n|"
    r"world bank|banco mundial|ministry|ministerio|department|departamento|"
    r"organization|organisation|organizaci[oó]n|commission|comisi[oó]n)\b",
    re.I,
)


def normalize_isbn(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", value or "").upper()


def is_valid_isbn10(value: str) -> bool:
    isbn = normalize_isbn(value)
    if not re.fullmatch(r"\d{9}[\dX]", isbn):
        return False
    total = sum((10 - i) * (10 if char == "X" else int(char)) for i, char in enumerate(isbn))
    return total % 11 == 0


def is_valid_isbn13(value: str) -> bool:
    isbn = normalize_isbn(value)
    if not re.fullmatch(r"\d{13}", isbn):
        return False
    expected = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(isbn[:12])) % 10) % 10
    return expected == int(isbn[-1])


def isbn10_to_isbn13(value: str) -> str:
    isbn = normalize_isbn(value)
    if not is_valid_isbn10(isbn):
        return ""
    stem = "978" + isbn[:9]
    check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(stem)) % 10) % 10
    return stem + str(check)


def extract_isbns(text: str) -> tuple[str, str]:
    isbn10 = ""
    isbn13 = ""
    for match in ISBN_LABEL_RE.finditer(text or ""):
        value = normalize_isbn(match.group(1))
        # Stop common over-capture at the first valid ISBN-sized prefix.
        candidates = [value[:13], value[:10]]
        for candidate in candidates:
            if not isbn13 and is_valid_isbn13(candidate):
                isbn13 = candidate
            if not isbn10 and is_valid_isbn10(candidate):
                isbn10 = candidate
    if isbn10 and not isbn13:
        isbn13 = isbn10_to_isbn13(isbn10)
    return isbn10, isbn13


def discover_folders(root: Path) -> list[FolderRecord]:
    """Return one list-manifest row for every directory below root, even if empty."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Library root is not a directory: {root}")
    records = []
    name_locations: dict[str, set[Path]] = {}
    directories = sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: str(p).casefold())
    for folder in directories:
        list_name = unicodedata.normalize("NFC", folder.name)
        name_locations.setdefault(list_name.casefold(), set()).add(folder)
        pdf_count = sum(
            path.is_file() and path.suffix.casefold() == ".pdf" for path in folder.iterdir()
        )
        records.append(FolderRecord(list_name, str(folder.resolve()), pdf_count))
    duplicates = {name: paths for name, paths in name_locations.items() if len(paths) > 1}
    if duplicates:
        details = "; ".join(
            f"{name}: {', '.join(str(path.relative_to(root)) for path in sorted(paths))}"
            for name, paths in sorted(duplicates.items())
        )
        raise ValueError(f"Duplicate list-folder names would merge distinct lists; rename or disambiguate: {details}")
    return records


def discover_pdfs(root: Path) -> list[tuple[str, Path]]:
    """Return direct-child PDFs for every directory below root.

    Every physical directory containing one or more PDFs is an independent list.
    A parent's PDFs never absorb PDFs from its nested directories.
    """
    found: list[tuple[str, Path]] = []
    for record in discover_folders(root):
        folder = Path(record.local_folder_path)
        pdfs = sorted(
            (path for path in folder.iterdir() if path.is_file() and path.suffix.casefold() == ".pdf"),
            key=lambda p: p.name.casefold(),
        )
        found.extend((record.list_name, path.resolve()) for path in pdfs)
    return found


def _clean_pdf_value(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _useful_lines(text: str) -> list[str]:
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" \t|•")
        if 4 <= len(line) <= 220 and not re.fullmatch(r"[\d\W]+", line):
            lines.append(line)
    return lines


def infer_title(text: str, embedded_title: str) -> str:
    if embedded_title and not JUNK_TITLE_RE.search(embedded_title) and 4 <= len(embedded_title) <= 220:
        return embedded_title.strip()
    lines = _useful_lines(text)[:80]
    if not lines:
        return Path(embedded_title).stem if embedded_title else ""
    banned = re.compile(r"^(?:table of contents|contents|index|copyright|acknowledg|www\.|https?://)", re.I)
    candidates = [line for line in lines if not banned.search(line) and not AUTHOR_RE.match(line)]
    if not candidates:
        return ""
    # Title pages usually place a concise title early. Upper/title case and early position help,
    # while long prose sentences and punctuation-heavy lines are penalized.
    def score(item: tuple[int, str]) -> float:
        i, line = item
        words = line.split()
        casing = 1.0 if line.isupper() or line.istitle() else 0.3
        length = 1.0 if 2 <= len(words) <= 18 else 0.2
        sentence_penalty = 0.7 if line.endswith((".", ";")) else 0.0
        return casing + length + max(0, 1 - i / 35) - sentence_penalty
    return max(enumerate(candidates), key=score)[1]


def infer_author(text: str, embedded_author: str) -> str:
    looks_like_username = bool(re.fullmatch(r"[a-z][a-z0-9._-]+", embedded_author or ""))
    if embedded_author and not looks_like_username and not re.search(r"unknown|author|acrobat|administrator", embedded_author, re.I):
        return embedded_author.strip()
    for line in _useful_lines(text)[:120]:
        match = AUTHOR_RE.match(line)
        if match and 2 <= len(match.group(1).split()) <= 12:
            return match.group(1).strip()
    return ""


def infer_year(text: str) -> str:
    years = [int(y) for y in YEAR_RE.findall((text or "")[:20000])]
    plausible = [y for y in years if 1800 <= y <= 2100]
    return str(plausible[0]) if plausible else ""


def infer_publisher(text: str) -> str:
    for line in _useful_lines(text)[:140]:
        if PUBLISHER_RE.search(line) and len(line.split()) <= 18:
            return line
    return ""


def detect_language(text: str) -> str:
    words = re.findall(r"\b[a-záéíóúüñçãõàèìòùâêîôû]+\b", (text or "").lower())[:1500]
    if len(words) < 30:
        return ""
    sets = {
        "spa": {"de", "la", "el", "los", "las", "para", "que", "del", "una", "por", "con"},
        "eng": {"the", "of", "and", "to", "in", "for", "with", "that", "this", "from"},
        "por": {"de", "o", "a", "os", "as", "para", "que", "uma", "por", "com"},
        "fra": {"de", "la", "le", "les", "des", "pour", "que", "une", "par", "avec"},
    }
    counts = {lang: sum(word in markers for word in words) for lang, markers in sets.items()}
    lang, count = max(counts.items(), key=lambda item: item[1])
    return lang if count >= 5 else ""


def scan_pdf(list_name: str, path: Path, max_pages: int = 8, sample_chars: int = 5000) -> CatalogRecord:
    record = CatalogRecord(
        local_folder=list_name,
        local_path=str(path),
        filename=path.name,
        file_size=path.stat().st_size,
    )
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path), strict=False)
        record.page_count = len(reader.pages)
        meta = reader.metadata or {}
        record.pdf_title = _clean_pdf_value(getattr(meta, "title", "") or meta.get("/Title", ""))
        record.pdf_author = _clean_pdf_value(getattr(meta, "author", "") or meta.get("/Author", ""))
        chunks = []
        for page in reader.pages[:max_pages]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception as exc:  # A broken page should not discard the rest of the PDF.
                LOG.debug("Could not extract page from %s: %s", path, exc)
        text = "\n".join(chunks)
        record.text_sample = re.sub(r"\x00", "", text)[:sample_chars]
        record.detected_title = infer_title(text, record.pdf_title) or path.stem
        record.detected_author = infer_author(text, record.pdf_author)
        record.year = infer_year(text)
        record.publisher = infer_publisher(text)
        record.isbn_10, record.isbn_13 = extract_isbns(text)
        doi = DOI_RE.search(text)
        record.doi = doi.group(0).rstrip(".,;)") if doi else ""
        record.language = detect_language(text)
    except Exception as exc:
        record.scan_error = f"{type(exc).__name__}: {exc}"
        record.detected_title = path.stem
        LOG.warning("Could not scan %s: %s", path, exc)
    return record


def scan_library(root: Path) -> Iterable[CatalogRecord]:
    for list_name, path in discover_pdfs(root):
        yield scan_pdf(list_name, path)
