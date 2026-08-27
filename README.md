# Open Library PDF List Importer

A personal utility that identifies local PDF publications and adds their existing Open Library **Works** to profile lists. It never uploads a PDF. Its workflow is deliberately:

> scan → match → enrich → map → review → publish

Scanning and matching are read-only. Publishing and record creation are separate explicit commands with dry-run modes. No automated test performs a live write.

## Verified Open Library behavior

The implementation was checked against the installed `openlibrary-client` commit `3f131881358d2c0dba5de79d894b80fae24742c6` and the live read APIs on 2026-08-26:

- `olclient.OpenLibrary()` reads stored S3 access/secret credentials from `~/.config/ol.ini`, logs in at `/account/login`, and exposes its authenticated `requests.Session` as `ol.session`.
- The username is decoded locally from the authenticated session cookie. Cookies and credentials are never logged.
- `olclient` has no list wrapper. This project reuses `ol.session` for the documented Lists API.
- Search results are Works by default. Lists accept Work seeds (`/works/OL…W`), so publication uses Work keys while retaining a best Edition key for review.
- `olclient.create_book()` posts to `/books/add` and requires ISBN-10, ISBN-13, LCCN, or OCAID plus a multi-word author. It is not safe for identifier-less institutional reports, which this tool reports and skips.

References: [Search API](https://openlibrary.org/dev/docs/api/search), [Lists API](https://openlibrary.org/dev/docs/api/lists), [current bulk Work-seed guidance](https://github.com/internetarchive/openlibrary/wiki/creating-community-lists), and [openlibrary-client](https://github.com/internetarchive/openlibrary-client).

## Installation

Requires Python 3.11+ (the code uses stdlib `tomllib`). From the repository root:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Configure the official client interactively. This stores its normal S3 keys outside the project; never copy them into `config.toml`:

```bash
.venv/bin/ol --configure --email you@example.com
```

Copy `config.example.toml` to `config.toml`, then set your public username, contact email, and the local folder tree you want to scan (`library_root` — it does not need to live inside this repo). `OPENLIBRARY_USERNAME`, `USER_AGENT_EMAIL`, and `LIBRARY_ROOT` override TOML values from the environment. `config.toml` is gitignored; never commit it.

Verify the setup:

```bash
.venv/bin/python library_importer.py auth-check
```

## Folder-to-list rule

Every directory below the supplied root becomes a separate list, including an organizational directory with no direct PDFs. Descendant PDFs belong only to their own containing directory:

```text
DOTS Colombia/
├── colombia.pdf       → list "DOTS Colombia"
└── Bogota 21 Transit-oriented Metropolis/
    └── bogota.pdf     → list "Bogota 21 Transit-oriented Metropolis"
```

The scan writes `data/lists.csv` as the complete folder/list manifest, including empty folders, and `data/catalog.csv` for PDFs. PDFs placed directly in the supplied root are ignored. Non-PDF files are ignored. Duplicate directory basenames in different branches are rejected because two distinct local folders cannot safely map to the same profile-list name.

## Milestone 1: scan

```bash
.venv/bin/python library_importer.py scan "/absolute/path/to/your/pdf/library"
```

The path is any local folder tree of PDFs and doesn't need to live inside this repo; it can be omitted if `library_root` is already set in `config.toml`.

The scanner reads up to eight initial pages with `pypdf`, without OCR. It writes `data/catalog.csv` with file facts, embedded metadata, inferred title/author/year/publisher, checksum-validated ISBNs, DOI, language heuristic, and a short text sample. Broken/encrypted PDFs remain in the catalog with `scan_error` populated.

Embedded titles resembling Word filenames, `final_version`, `Untitled`, and similar junk are rejected. Inference is intentionally heuristic; review the catalog rather than treating it as authoritative.

## Milestone 2: match and review

```bash
.venv/bin/python library_importer.py match
.venv/bin/python library_importer.py enrich
.venv/bin/python library_importer.py map
.venv/bin/python library_importer.py review-summary
```

Matching tries valid ISBN, DOI text, title+author, then title+year. Search JSON is cached under `data/cache/search` to reduce repeat traffic. Outputs are:

- `matches.csv`: replaceable machine output from the latest run.
- `decisions.csv`: human source of truth. Existing rows are never overwritten by `match`.
- `not_found.csv`: latest machine-generated missing report.
- `enrichment.csv`: provenance-rich DOI, ISBN, external-type, duplicate, and secondary-title findings.
- `enriched_matches.csv`: latest machine decisions after enrichment.
- `verified_metadata.csv`: auditable folder/filename rules for metadata confirmed from title pages or authoritative catalogs.
- `work_inventory.csv`: canonical local-work catalog with stable `OLP-…` IDs. It collapses split chapters, duplicate PDFs, shared DOIs, and shared Open Library Works while retaining mapping confidence and provenance.
- `list_membership.csv`: one row per local Work and direct containing-folder list. It maps every intellectual work even when no Open Library Work exists.

Open `decisions.csv` in Excel. For a manual match, set `status` to `APPROVED` and enter a canonical `/works/OL123W` in `ol_work_key`. Leave uncertain rows as `REVIEW`. Valid machine classifications are `MATCHED`, `REVIEW`, `NOT_FOUND`, and `ERROR`.

### Confidence logic

- A checksum-valid exact ISBN shared by the local record and candidate scores `0.995`, but becomes `MATCHED` only when title, author, publisher, or year also corroborates it. A bibliographically conflicting ISBN remains `REVIEW` because source PDFs and Open Library records can both contain bad identifiers.
- Otherwise title similarity is the anchor (sequence plus token overlap), with author as the strongest corroboration and smaller publisher/year contributions.
- A non-ISBN candidate becomes `MATCHED` only at `>= 0.90`, with a margin of at least `0.08` over the runner-up, and with corroboration from author, publisher, or year plus a non-generic title of at least three words.
- Plausible weaker or competing candidates at `>= 0.65` remain `REVIEW`; lower fuzzy results are `NOT_FOUND`. Up to five candidates and their scores are stored as JSON in the CSV.

This deliberately produces false negatives rather than risky false positives.

### Source-agnostic local mapping

`map` is fully local and read-only with respect to Open Library. It hashes the source files to create stable `OLP-…` Work IDs, selects the best available title, assigns a broad document type, and maps each Work to the list represented by its direct containing folder. Content hashes make IDs independent of absolute file paths. Split components and byte-identical duplicates remain one intellectual Work.

The output deliberately separates two facts:

- `mapping_status` says whether identity is linked to Open Library, corroborated by another authority, or still a provisional local identity.
- `openlibrary_action` says whether the Work is ready to add, is a candidate for reviewed record creation, needs bibliographic review, or belongs outside Open Library's book-oriented scope.

All catalog PDFs must appear exactly once across the membership rows at the file level, every Work must have a unique stable ID and at least one membership, and duplicate Work/list pairs are rejected before either CSV is written. A provisional local mapping is useful for organizing the collection but is not represented as an externally verified bibliographic identification.

### Secondary enrichment

`enrich` resolves exact DOI metadata through Crossref with a DataCite fallback, searches Crossref and OpenAlex for missing scholarly identifiers, recovers ISBNs where deposited, retries Open Library with corrected metadata, checks alternate filename-derived titles, queries Google Books conservatively, and detects byte-identical local PDFs. All responses are cached under `data/cache/enrichment` or `data/cache/search`.

The enriched decision statuses distinguish `VERIFIED_EXTERNAL` journal/conference material, `VERIFIED_NONBOOK` serial/legal/map material, `DUPLICATE_LOCAL`, `PARENT_COMPONENT`, `ALTERNATE_EDITION`, `CREATE_CANDIDATE`, `REVIEW`, and genuinely `NOT_FOUND` rows. Only high-confidence metadata that resolves to a corroborated Open Library Work becomes `MATCHED`. Untouched machine decisions are refreshed; any human-edited row is preserved.

`verified_metadata.csv` is checked just as strictly as extracted data: malformed ISBN checksums are rejected, rules are scoped to both folder and filename pattern, and overlapping rules fail rather than silently choosing one. `PUBLICATION` rules are checked against Open Library and become either `MATCHED` or `CREATE_CANDIDATE`; `COMPONENT` rules group chapter PDFs under one parent publication; `ALTERNATE_EDITION` groups language/edition variants; and `NONBOOK` prevents newsletters, maps, and legal instruments from being mistaken for books.

After adding or correcting verified rules, refresh only those rules while reusing the completed external enrichment for every other file:

```bash
.venv/bin/python library_importer.py enrich --verified-only
```

Google Books may rate-limit anonymous traffic. Rerun later to use cached progress, or set `GOOGLE_BOOKS_API_KEY` in the environment. The key must not be placed in `config.toml` and is never written to CSV or logs.

OpenAlex works anonymously for small runs; `OPENALEX_API_KEY` may be supplied through the environment for a larger API budget. It is likewise never stored or logged.

## Milestone 3: lists and publication

Always preview first:

```bash
.venv/bin/python library_importer.py publish --dry-run
```

The plan authenticates, verifies the username, reads every existing list and seed, then reports lists to create/reuse, local Work coverage, existing Open Library Works, and additions. It separates locally mapped Works from items that are ready for Open Library publication. Dry-run never calls POST and local `OLP-…` identifiers are never sent to Open Library.

After reviewing the plan and giving explicit approval for the first live write:

```bash
.venv/bin/python library_importer.py publish
```

Publication accepts only rows whose status is `MATCHED` or `APPROVED` and whose key is a canonical Work key. It normalizes Unicode list names, rejects duplicate existing list names, creates missing lists, deduplicates local Work keys, reads current seeds, skips existing seeds, and submits additions in batches of 50. Re-running is idempotent.

## Milestone 4: approved missing records

Record creation is optional and intentionally narrow. Set `create_record=YES` only after bibliographic review, then preview:

```bash
.venv/bin/python library_importer.py create-missing --approved-only --dry-run
```

The row must contain a reliable title, multi-word author/responsible organization, publication year or publisher, and ISBN/LCCN. Immediately before creation the tool searches the identifier again. Existing identifiers and unsupported institutional reports are skipped. A real run is:

```bash
.venv/bin/python library_importer.py create-missing --approved-only
```

No PDF bytes, cover, text sample, or local path are ever sent to Open Library. Creation sends only reviewed bibliographic fields supported by `olclient`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover ISBN checksums, normalization, nested folder discovery, ambiguous confidence, Search API parsing, duplicate suppression, stable local IDs, exact file/list coverage, and proof that dry-run makes no write calls. HTTP-facing behaviors use fakes; tests never access or modify the live account.

## Operational notes

- Use `--verbose` before the command for request/matching diagnostics; secrets are never included.
- The `backoff` and `openlibrary` dependency loggers are forcibly silenced because current `olclient` failed-login callbacks can include serialized credential arguments. The importer reports only a credential-free connection error.
- GET requests retry 429/5xx responses with exponential backoff and honor `Retry-After`.
- A descriptive User-Agent and configurable delay are used.
- Keep `data/decisions.csv` backed up. It is the publication source of truth.
- Never run real `publish` or `create-missing` until their dry-run output has been reviewed.
