from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger(__name__)


class OpenLibraryAPI:
    def __init__(
        self,
        *,
        base_url: str = "https://openlibrary.org",
        user_agent: str = "OpenLibraryLocalImporter/0.1 (personal metadata organizer)",
        cache_dir: Path | None = None,
        delay: float = 0.35,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        retry = Retry(
            total=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(("GET", "HEAD")),
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = max(0.0, delay)
        self._last_request = 0.0

    @classmethod
    def authenticated(cls, **kwargs: Any) -> "OpenLibraryAPI":
        # olclient's retry callback can include serialized login arguments in
        # exception details. Never allow dependency logging to disclose them.
        logging.getLogger("backoff").setLevel(logging.CRITICAL)
        logging.getLogger("openlibrary").setLevel(logging.CRITICAL)
        try:
            from olclient import OpenLibrary
        except ImportError as exc:
            raise RuntimeError("olclient is required for authenticated commands") from exc
        client = OpenLibrary(base_url=kwargs.get("base_url", "https://openlibrary.org"))
        kwargs["session"] = client.session
        api = cls(**kwargs)
        api._olclient = client
        return api

    def _pace(self) -> None:
        remaining = self.delay - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None, cache: bool = False) -> dict[str, Any]:
        url = self.base_url + path
        cache_path = None
        if cache and self.cache_dir:
            key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
            cache_path = self.cache_dir / f"{key}.json"
            if cache_path.exists():
                return json.loads(cache_path.read_text(encoding="utf-8"))
        self._pace()
        LOG.debug("GET %s params=%s", url, params)
        response = self.session.get(url, params=params, timeout=30)
        self._last_request = time.monotonic()
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Malformed JSON object from {path}")
        if cache_path:
            cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload

    @staticmethod
    def _normalize_person(name: str) -> str:
        return " ".join(name.split()).casefold()

    def find_author(self, name: str) -> dict[str, Any]:
        """Resolve an author name to an existing Open Library author key.

        Only an exact name match (case and whitespace insensitive) is linked.
        A near miss is reported instead of guessed: mis-attributing a book to the
        wrong person is worse than creating a duplicate author record.
        """
        name = name.strip()
        if not name:
            return {"key": "", "candidates": []}
        url = self.base_url + "/authors/_autocomplete"
        params = {"q": name, "limit": 10}
        cache_path = None
        if self.cache_dir:
            digest = hashlib.sha256(
                json.dumps([url, params], sort_keys=True).encode()
            ).hexdigest()
            cache_path = self.cache_dir / f"author-{digest}.json"
            if cache_path.exists():
                return json.loads(cache_path.read_text(encoding="utf-8"))
        self._pace()
        response = self.session.get(url, params=params, timeout=30)
        self._last_request = time.monotonic()
        response.raise_for_status()
        payload = response.json()
        candidates = payload if isinstance(payload, list) else []
        wanted = self._normalize_person(name)
        result: dict[str, Any] = {"key": "", "candidates": []}
        for candidate in candidates:
            candidate_name = str(candidate.get("name", ""))
            if self._normalize_person(candidate_name) == wanted:
                result = {"key": str(candidate.get("key", "")), "candidates": []}
                break
            result["candidates"].append(candidate_name)
        if cache_path:
            cache_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return result

    def search(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        fields = "key,title,author_name,first_publish_year,edition_key,isbn,publisher,publish_year"
        return self._get_json("/search.json", params={"q": query, "fields": fields, "limit": limit}, cache=True)

    def username(self) -> str:
        raw = self.session.cookies.get("session") or ""
        decoded = unquote(raw)
        if decoded.startswith("/people/"):
            return decoded[len("/people/"):].split(",", 1)[0]
        return ""

    def list_user_lists(self, username: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        path = f"/people/{quote(username)}/lists.json"
        params: dict[str, Any] | None = {"limit": 100, "offset": 0}
        while path:
            payload = self._get_json(path, params=params)
            entries.extend(e for e in payload.get("entries", []) if isinstance(e, dict))
            next_path = payload.get("links", {}).get("next")
            path, params = (str(next_path), None) if next_path else ("", None)
        return entries

    @staticmethod
    def list_id(entry: dict[str, Any]) -> str:
        return str(entry.get("url", "")).rstrip("/").split("/")[-1]

    def list_seeds(self, username: str, list_id: str) -> set[str]:
        path = f"/people/{quote(username)}/lists/{quote(list_id)}/seeds.json"
        params: dict[str, Any] | None = {"limit": 100, "offset": 0}
        seeds: set[str] = set()
        while path:
            payload = self._get_json(path, params=params)
            seeds.update(str(e.get("url", "")) for e in payload.get("entries", []) if isinstance(e, dict))
            next_path = payload.get("links", {}).get("next")
            path, params = (str(next_path), None) if next_path else ("", None)
        return seeds

    def create_list(self, username: str, name: str, description: str = "") -> str:
        url = f"{self.base_url}/people/{quote(username)}/lists"
        LOG.debug("POST %s (create list %r)", url, name)
        response = self.session.post(url, json={"name": name, "description": description}, timeout=30)
        response.raise_for_status()
        location = response.headers.get("Location", "")
        list_id = urlparse(location).path.rstrip("/").split("/")[-1]
        if not list_id.endswith("L"):
            try:
                body = response.json()
                list_id = str(body.get("key", "")).rstrip("/").split("/")[-1]
            except Exception:
                pass
        if not list_id.endswith("L"):
            raise RuntimeError("List was created but its Open Library list ID was not returned")
        return list_id

    def add_seeds(self, username: str, list_id: str, keys: Iterable[str], *, batch_size: int = 50) -> int:
        unique = list(dict.fromkeys(key for key in keys if key))
        url = f"{self.base_url}/people/{quote(username)}/lists/{quote(list_id)}/seeds.json"
        added = 0
        for offset in range(0, len(unique), batch_size):
            batch = unique[offset:offset + batch_size]
            LOG.debug("POST %s (%d seeds)", url, len(batch))
            response = self.session.post(url, json={"add": [{"key": key} for key in batch]}, timeout=30)
            response.raise_for_status()
            added += len(batch)
        return added

    def add_book(self, form: dict[str, str]) -> dict[str, str]:
        """Create a Work and Edition through Open Library's /books/add form.

        This is the route an ordinary logged-in account may use; identifiers are
        optional and Open Library runs its own duplicate matching. Set the form's
        `_test` field to "true" to ask Open Library what it would match without
        writing anything.
        """
        if getattr(self, "_olclient", None) is None:
            raise RuntimeError("Authenticated olclient session required")
        self._pace()
        # olclient pins Content-Type: application/json on the shared session, so a
        # form-encoded body would otherwise contradict its own header and be
        # rejected at Open Library's edge before the handler ever sees it.
        response = self.session.post(
            f"{self.base_url}/books/add", data=form, timeout=60, allow_redirects=True,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        )
        body = response.text or ""
        if not response.ok:
            # Non-standard codes here come from Open Library's edge, not the
            # application, so surface enough of the reply to tell them apart.
            raise RuntimeError(
                f"Open Library rejected the /books/add POST with HTTP {response.status_code} "
                f"(content-type {response.headers.get('Content-Type', 'unknown')!r}). "
                f"First 300 characters of the reply: {body[:300]!r}"
            )
        if form.get("_test") == "true":
            matched = re.search(r"/(?:books|works)/(OL\d+[MW])", body)
            return {
                "test": "true",
                "outcome": "matched" if matched else "would_create",
                "matched_key": matched.group(1) if matched else "",
            }
        edition = re.search(r"/books/(OL\d+M)", response.url or "")
        if not edition:
            raise RuntimeError(
                f"Open Library did not return a new edition key (landed on {response.url!r})"
            )
        work = re.search(r'"/works/(OL\d+W)"', body) or re.search(r"/works/(OL\d+W)", body)
        return {
            "edition_key": f"/books/{edition.group(1)}",
            "work_key": f"/works/{work.group(1)}" if work else "",
        }

    def import_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Submit one JSON record to Open Library's import API.

        Unlike olclient's /books/add wrapper this accepts records with no ISBN,
        which is the only workable route for grey literature. Requires an
        account with import privileges; Open Library answers 403 otherwise.
        """
        if getattr(self, "_olclient", None) is None:
            raise RuntimeError("Authenticated olclient session required")
        self._pace()
        response = self.session.post(
            f"{self.base_url}/api/import",
            data=json.dumps(record),
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        if response.status_code == 403:
            raise PermissionError(
                "Open Library refused the import (403). The account needs import "
                "privileges; apply for a bot account and a source_records prefix."
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise RuntimeError(f"Import rejected: {payload}")
        return payload

    def create_book(self, row: dict[str, str]) -> tuple[str, str]:
        """Legacy olclient /books/add wrapper. Requires an ISBN/LCCN/OCAID, so it
        cannot be used for identifier-less records; prefer import_record."""
        from olclient import common

        ol = getattr(self, "_olclient", None)
        if ol is None:
            raise RuntimeError("Authenticated olclient session required")
        author = common.Author(name=row["detected_author"])
        book = common.Book(
            title=row["detected_title"], authors=[author], publisher=row.get("publisher", ""),
            publish_date=row.get("year", ""),
        )
        if row.get("isbn_13"):
            book.add_id("isbn_13", row["isbn_13"])
        elif row.get("isbn_10"):
            book.add_id("isbn_10", row["isbn_10"])
        elif row.get("lccn"):
            book.add_id("lccn", row["lccn"])
        elif row.get("ocaid"):
            book.add_id("ocaid", row["ocaid"])
        edition = ol.create_book(book)
        return f"/works/{edition.work_olid}", f"/books/{edition.olid}"
