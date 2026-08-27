from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from models import CatalogRecord, MatchRecord
from pdf_metadata import normalize_isbn


def normalize_text(value: str, *, drop_subtitle: bool = False) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.casefold()
    if drop_subtitle:
        value = re.split(r"\s*[:;–—]\s*", value, maxsplit=1)[0]
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _similarity(left: str, right: str) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    at, bt = set(a.split()), set(b.split())
    jaccard = len(at & bt) / len(at | bt) if at | bt else 0.0
    return max(sequence, 0.65 * sequence + 0.35 * jaccard)


def _values(doc: dict[str, Any], name: str) -> list[str]:
    value = doc.get(name, [])
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def score_candidate(record: CatalogRecord | dict[str, Any], doc: dict[str, Any]) -> tuple[float, list[str]]:
    get = record.get if isinstance(record, dict) else lambda key, default="": getattr(record, key, default)
    local_isbns = {normalize_isbn(get("isbn_10")), normalize_isbn(get("isbn_13"))} - {""}
    remote_isbns = {normalize_isbn(v) for v in _values(doc, "isbn")}
    title = _similarity(get("detected_title"), str(doc.get("title", "")))
    # Subtitles are frequently missing on one side; score both forms.
    title = max(
        title,
        _similarity(normalize_text(get("detected_title"), drop_subtitle=True), normalize_text(str(doc.get("title", "")), drop_subtitle=True)),
    )
    authors = _values(doc, "author_name")
    author = max((_similarity(get("detected_author"), value) for value in authors), default=0.0)
    publishers = _values(doc, "publisher")
    publisher = max((_similarity(get("publisher"), value) for value in publishers), default=0.0)
    years = set(_values(doc, "publish_year")) | set(_values(doc, "first_publish_year"))
    year_match = bool(get("year") and str(get("year")) in years)

    signals = [f"title={title:.2f}", f"author={author:.2f}"]
    if publisher:
        signals.append(f"publisher={publisher:.2f}")
    if year_match:
        signals.append("year=exact")
    if local_isbns & remote_isbns:
        return 0.995, ["exact ISBN", *signals]

    # Title is the anchor. Author is strongest corroboration; year/publisher provide smaller
    # boosts for institutional publications whose author field is often empty.
    score = 0.67 * title + 0.23 * author + 0.06 * publisher + (0.04 if year_match else 0.0)
    if not get("detected_author"):
        score = 0.82 * title + 0.12 * publisher + (0.06 if year_match else 0.0)
    return min(score, 0.99), signals


def parse_search_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    docs = payload.get("docs", [])
    if not isinstance(docs, list):
        raise ValueError("Open Library search response has no docs list")
    return [doc for doc in docs if isinstance(doc, dict) and str(doc.get("key", "")).startswith("/works/")]


def choose_match(record: CatalogRecord, docs: list[dict[str, Any]]) -> MatchRecord:
    result = MatchRecord(**{name: getattr(record, name) for name in (
        "local_folder", "local_path", "filename", "detected_title", "detected_author",
        "isbn_10", "isbn_13", "doi", "year", "publisher"
    )})
    ranked = []
    for doc in docs:
        score, reasons = score_candidate(record, doc)
        ranked.append((score, doc, reasons))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        result.status = "NOT_FOUND"
        result.notes = "No Open Library candidates returned"
        return result

    score, best, reasons = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    result.ol_work_key = str(best.get("key", ""))
    editions = _values(best, "edition_key")
    result.ol_edition_key = f"/books/{editions[0].split('/')[-1]}" if editions else ""
    result.ol_title = str(best.get("title", ""))
    result.ol_author = "; ".join(_values(best, "author_name"))
    result.confidence = f"{score:.3f}"
    margin = score - runner_up
    exact_isbn = bool(reasons and reasons[0] == "exact ISBN")
    numeric_signals = {}
    for reason in reasons:
        if "=" in reason:
            name, value = reason.split("=", 1)
            try:
                numeric_signals[name] = float(value)
            except ValueError:
                pass
    isbn_corroborated = exact_isbn and (
        numeric_signals.get("title", 0.0) >= 0.45
        or numeric_signals.get("author", 0.0) >= 0.65
        or numeric_signals.get("publisher", 0.0) >= 0.60
        or "year=exact" in reasons
    )
    # High-confidence non-ISBN matches require both >= .90 and clear separation. Everything
    # plausible but weaker remains REVIEW by design.
    title_word_count = len(normalize_text(getattr(record, "detected_title", "")).split())
    non_isbn_corroborated = (
        numeric_signals.get("author", 0.0) >= 0.55
        or numeric_signals.get("publisher", 0.0) >= 0.55
        or ("year=exact" in reasons and title_word_count >= 3)
    )
    if isbn_corroborated or (
        not exact_isbn and score >= 0.90 and margin >= 0.08 and non_isbn_corroborated
    ):
        result.status = "MATCHED"
    elif exact_isbn or score >= 0.65:
        result.status = "REVIEW"
    else:
        result.status = "NOT_FOUND"
    result.notes = "; ".join(reasons + [f"runner_up={runner_up:.2f}", f"margin={margin:.2f}"])
    result.candidates_json = json.dumps([
        {
            "work_key": doc.get("key", ""),
            "title": doc.get("title", ""),
            "author": _values(doc, "author_name"),
            "score": round(candidate_score, 3),
        }
        for candidate_score, doc, _ in ranked[:5]
    ], ensure_ascii=False)
    return result
