from __future__ import annotations

import re
from pathlib import Path

from models import CatalogRecord, RecoveryRecord
from pdf_metadata import (
    DOI_RE, detect_language, extract_isbns, infer_author, infer_publisher,
    infer_title, infer_year,
)


REPORT_NUMBER_PATTERNS = (
    re.compile(r"\b(?:TCRP|TRB|SHRP\s*2?|NCHRP|RR|Report|Reporte|Informe|Technical Report)\s*"
               r"(?:No\.?|N[úu]m(?:ero)?\.?|#)?\s*[A-Z0-9][A-Z0-9./-]{1,24}\b", re.I),
    re.compile(r"\bCONPES\s+\d{3,5}\b", re.I),
    re.compile(r"\b(?:Decreto|Resoluci[oó]n|Acuerdo|Ley)\s+(?:No\.?\s*)?\d{2,6}(?:\s+de\s+\d{4})?\b", re.I),
)


def _page_indexes(page_count: int, first_pages: int = 15, last_pages: int = 8) -> list[int]:
    first = range(min(page_count, first_pages))
    last = range(max(0, page_count - last_pages), page_count)
    return sorted(set(first) | set(last))


def _find_doi(text: str) -> str:
    match = DOI_RE.search(text or "")
    if match:
        return match.group(0).rstrip(".,;)")
    # Repair common line wraps around DOI punctuation before one retry.
    compact = re.sub(r"\s*([./:_;-])\s*", r"\1", text or "")
    match = DOI_RE.search(compact)
    return match.group(0).rstrip(".,;)") if match else ""


def _report_numbers(text: str) -> str:
    values = []
    for pattern in REPORT_NUMBER_PATTERNS:
        values.extend(re.sub(r"\s+", " ", match.group(0)).strip() for match in pattern.finditer(text or ""))
    return "; ".join(dict.fromkeys(values))


def recover_pdf_metadata(record: CatalogRecord) -> RecoveryRecord:
    output = RecoveryRecord(
        local_folder=record.local_folder, local_path=record.local_path,
        filename=record.filename,
    )
    try:
        from pypdf import PdfReader

        reader = PdfReader(record.local_path, strict=False)
        indexes = _page_indexes(len(reader.pages))
        page_texts: dict[int, str] = {}
        errors = 0
        for index in indexes:
            try:
                page_texts[index] = reader.pages[index].extract_text() or ""
            except Exception:
                errors += 1
        text = re.sub(r"\x00", "", "\n".join(page_texts[index] for index in indexes if index in page_texts))
        opening_text = "\n".join(page_texts.get(index, "") for index in range(min(8, len(reader.pages))))
        doi_text = "\n".join(page_texts.get(index, "") for index in range(min(3, len(reader.pages))))
        back_text = "\n".join(
            page_texts.get(index, "") for index in range(max(0, len(reader.pages) - 2), len(reader.pages))
        )
        output.pages_examined = ",".join(str(index + 1) for index in indexes)
        output.text_sample = text[:12000]
        meta = reader.metadata or {}
        embedded_title = str(getattr(meta, "title", "") or meta.get("/Title", "") or "")
        embedded_author = str(getattr(meta, "author", "") or meta.get("/Author", "") or "")
        output.recovered_title = infer_title(text, embedded_title)
        output.recovered_author = infer_author(text, embedded_author)
        output.recovered_publisher = infer_publisher(text)
        output.recovered_year = infer_year(text)
        # Checksum-valid but unlabeled numbers are deliberately excluded: references
        # frequently contain other books' ISBNs. A label plus front/back placement is
        # required before an ISBN may enter automated matching.
        output.isbn_10, output.isbn_13 = extract_isbns(opening_text + "\n" + back_text)
        # Likewise, only an opening-page DOI is treated as the document's identifier;
        # later pages commonly contain unrelated bibliography DOIs.
        output.doi = _find_doi(doi_text)
        output.report_numbers = _report_numbers(text)
        output.language = detect_language(text)
        if text.strip():
            output.recovery_status = "TEXT_RECOVERED"
            output.notes = f"Deep extraction completed; {errors} selected pages failed" if errors else "Deep extraction completed"
        else:
            output.recovery_status = "OCR_REQUIRED"
            output.notes = "No embedded text on selected first/last pages"
    except Exception as exc:
        output.recovery_status = "ERROR"
        output.notes = f"{type(exc).__name__}: {exc}"
    return output
