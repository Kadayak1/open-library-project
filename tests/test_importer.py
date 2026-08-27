from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from library_importer import (  # noqa: E402
    approved_work_rows, build_local_mapping, build_work_inventory,
    build_addbook_form, build_import_record, classify_document_type, dedupe_creation_rows,
    completed_work_ids, creation_mode_banner, load_author_aliases, merge_new_decisions,
    edition_attachment_plan, publish_decisions, read_csv, validate_creation_row,
    validate_edition_row,
)
from library_importer import _OL_BOOKLIKE_TYPES as OL_BOOKLIKE_TYPES  # noqa: E402
from enrichment import ExternalMetadata, MetadataAPI, apply_enrichment, enrich_record, external_similarity, filename_title, select_verified_metadata  # noqa: E402
from matcher import choose_match, normalize_text, parse_search_results, score_candidate  # noqa: E402
from models import CatalogRecord, EnrichmentRecord, MatchRecord, VerifiedMetadataRecord  # noqa: E402
from openlibrary_api import OpenLibraryAPI  # noqa: E402
import requests  # noqa: E402
from pdf_metadata import (  # noqa: E402
    discover_pdfs,
    discover_folders,
    extract_isbns,
    is_valid_isbn10,
    is_valid_isbn13,
    normalize_isbn,
)


class ISBNTests(unittest.TestCase):
    def test_normalization_and_checksums(self) -> None:
        self.assertEqual(normalize_isbn("978-0-14-032872-1"), "9780140328721")
        self.assertTrue(is_valid_isbn13("978-0-14-032872-1"))
        self.assertTrue(is_valid_isbn10("0-14-032872-6"))
        self.assertFalse(is_valid_isbn13("9780140328722"))
        self.assertFalse(is_valid_isbn10("0140328727"))

    def test_extract_only_valid_labeled_isbns(self) -> None:
        ten, thirteen = extract_isbns("ISBN: 0-14-032872-6; unrelated 9780140328722")
        self.assertEqual(ten, "0140328726")
        self.assertEqual(thirteen, "9780140328721")


class NormalizationTests(unittest.TestCase):
    def test_accents_punctuation_and_subtitle(self) -> None:
        self.assertEqual(normalize_text("  Teoría—DOTS: Bogotá  "), "teoria dots bogota")
        self.assertEqual(normalize_text("Teoría DOTS: Bogotá", drop_subtitle=True), "teoria dots")


class DiscoveryTests(unittest.TestCase):
    def test_every_pdf_containing_folder_is_its_own_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / "DOTS Colombia"
            child = parent / "Bogota 21 Transit-oriented Metropolis"
            child.mkdir(parents=True)
            (root / "outside.pdf").write_bytes(b"ignored")
            (parent / "parent.pdf").write_bytes(b"pdf")
            (parent / "notes.txt").write_text("ignored")
            (child / "child.PDF").write_bytes(b"pdf")
            empty = parent / "Organizational folder"
            empty.mkdir()
            found = discover_pdfs(root)
            self.assertEqual(
                {(name, path.name) for name, path in found},
                {("DOTS Colombia", "parent.pdf"), ("Bogota 21 Transit-oriented Metropolis", "child.PDF")},
            )
            manifest = {row.list_name: row.pdf_count for row in discover_folders(root)}
            self.assertEqual(manifest["Organizational folder"], 0)

    def test_duplicate_folder_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for branch in ("a", "b"):
                folder = root / branch / "Reports"
                folder.mkdir(parents=True)
                (folder / f"{branch}.pdf").write_bytes(b"pdf")
            with self.assertRaisesRegex(ValueError, "Duplicate list-folder names"):
                discover_pdfs(root)


class MatchingTests(unittest.TestCase):
    def test_exact_isbn_is_high_confidence(self) -> None:
        record = CatalogRecord(detected_title="Fantastic Mr Fox", year="1970", isbn_13="9780140328721")
        doc = {"key": "/works/OL45804W", "title": "Fantastic Mr Fox", "isbn": ["9780140328721"]}
        score, reasons = score_candidate(record, doc)
        self.assertEqual(score, 0.995)
        self.assertEqual(reasons[0], "exact ISBN")
        self.assertEqual(choose_match(record, [doc]).status, "MATCHED")

    def test_conflicting_exact_isbn_requires_review(self) -> None:
        record = CatalogRecord(detected_title="Transit Oriented Development", detected_author="TRB", year="2007", isbn_13="9780309098922")
        doc = {
            "key": "/works/OL1W", "title": "Principles and Practice of Governing of Men",
            "author_name": ["Felix Alonge"], "publish_year": [1992], "isbn": ["9780309098922"],
        }
        self.assertEqual(choose_match(record, [doc]).status, "REVIEW")

    def test_weak_fuzzy_candidate_is_not_found(self) -> None:
        record = CatalogRecord(detected_title="Completely different report")
        doc = {"key": "/works/OL1W", "title": "Unrelated novel"}
        self.assertEqual(choose_match(record, [doc]).status, "NOT_FOUND")

    def test_generic_title_with_year_requires_review(self) -> None:
        record = CatalogRecord(detected_title="Land Lines", year="2010", publisher="Local institute")
        doc = {"key": "/works/OL1W", "title": "Land Lines", "publish_year": [2010], "publisher": ["Other press"]}
        self.assertEqual(choose_match(record, [doc]).status, "REVIEW")

    def test_ambiguous_fuzzy_match_requires_review(self) -> None:
        record = CatalogRecord(detected_title="Transit Oriented Development", detected_author="World Bank")
        docs = [
            {"key": "/works/OL1W", "title": "Transit Oriented Development", "author_name": ["World Bank"]},
            {"key": "/works/OL2W", "title": "Transit-Oriented Development", "author_name": ["The World Bank"]},
        ]
        self.assertEqual(choose_match(record, docs).status, "REVIEW")

    def test_parse_search_results_filters_nonworks(self) -> None:
        payload = {"docs": [{"key": "/works/OL1W"}, {"key": "/books/OL1M"}, {"title": "bad"}]}
        self.assertEqual(parse_search_results(payload), [{"key": "/works/OL1W"}])
        with self.assertRaises(ValueError):
            parse_search_results({"docs": "bad"})


class FakeAPI:
    def __init__(self, entries=None, seeds=None) -> None:
        self.entries = entries or []
        self.seeds = seeds or set()
        self.create_calls: list[tuple[str, str]] = []
        self.add_calls: list[list[str]] = []

    def list_user_lists(self, username: str):
        return self.entries

    @staticmethod
    def list_id(entry):
        return entry["url"].split("/")[-1]

    def list_seeds(self, username: str, list_id: str):
        return set(self.seeds)

    def create_list(self, username: str, name: str, description: str):
        self.create_calls.append((username, name))
        return "OL999L"

    def add_seeds(self, username: str, list_id: str, keys):
        values = list(keys)
        self.add_calls.append(values)
        return len(values)


class PublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            {"local_folder": "DOTS Colombia", "status": "APPROVED", "ol_work_key": "/works/OL1W"},
            {"local_folder": "DOTS Colombia", "status": "MATCHED", "ol_work_key": "/works/OL1W"},
            {"local_folder": "DOTS Colombia", "status": "APPROVED", "ol_work_key": "/works/OL2W"},
            {"local_folder": "DOTS Colombia", "status": "REVIEW", "ol_work_key": "/works/OL3W"},
        ]

    def test_duplicate_local_and_existing_seeds_are_not_added(self) -> None:
        api = FakeAPI(
            entries=[{"name": "DOTS Colombia", "url": "/people/test/lists/OL10L"}],
            seeds={"/works/OL1W"},
        )
        summary = publish_decisions(api, "test", self.rows, dry_run=False)
        self.assertEqual(api.add_calls, [["/works/OL2W"]])
        self.assertEqual(summary[0]["already"], 1)
        self.assertEqual(summary[0]["unresolved"], 1)

    def test_dry_run_makes_no_write_calls(self) -> None:
        api = FakeAPI()
        summary = publish_decisions(api, "test", self.rows, dry_run=True)
        self.assertEqual(api.create_calls, [])
        self.assertEqual(api.add_calls, [])
        self.assertEqual(summary[0]["action"], "create")

    def test_approved_work_rows_deduplicates(self) -> None:
        works, unresolved = approved_work_rows(self.rows)
        self.assertEqual(works["DOTS Colombia"], ["/works/OL1W", "/works/OL2W"])
        self.assertEqual(unresolved["DOTS Colombia"], 1)

    def test_verified_nonwork_is_not_counted_as_genuinely_unresolved(self) -> None:
        rows = self.rows + [
            {"local_folder": "DOTS Colombia", "status": "VERIFIED_EXTERNAL", "ol_work_key": ""},
            {"local_folder": "DOTS Colombia", "status": "PARENT_COMPONENT", "ol_work_key": ""},
            {"local_folder": "DOTS Colombia", "status": "VERIFIED_NONBOOK", "ol_work_key": ""},
        ]
        summary = publish_decisions(FakeAPI(), "test", rows, dry_run=True)
        self.assertEqual(summary[0]["unresolved"], 1)
        self.assertEqual(summary[0]["resolved_not_publishable"], 3)

    def test_dry_run_reports_local_mapping_without_publishing_local_ids(self) -> None:
        mapping = [
            {
                "list_name": "DOTS Colombia", "local_work_id": "OLP-ONE",
                "openlibrary_action": "READY_TO_ADD",
            },
            {
                "list_name": "DOTS Colombia", "local_work_id": "OLP-TWO",
                "openlibrary_action": "REVIEW_FOR_OL_RECORD",
            },
        ]
        api = FakeAPI()
        summary = publish_decisions(
            api, "test", self.rows, dry_run=True, local_mapping_rows=mapping,
        )
        self.assertEqual(summary[0]["local_works"], 2)
        self.assertEqual(summary[0]["add"], 2)
        self.assertEqual(api.add_calls, [])


class DecisionMergeTests(unittest.TestCase):
    def test_refreshes_untouched_machine_row_but_preserves_manual_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "decisions.csv"
            old = MatchRecord(local_path="a.pdf", status="REVIEW", ol_work_key="/works/OL1W")
            from library_importer import write_csv
            write_csv(path, MatchRecord.fieldnames(), [old.to_dict()])
            new = MatchRecord(local_path="a.pdf", status="MATCHED", ol_work_key="/works/OL2W")
            preserved, added, refreshed = merge_new_decisions(path, [new], [old.to_dict()])
            self.assertEqual((preserved, added, refreshed), (0, 0, 1))
            self.assertEqual(read_csv(path)[0]["ol_work_key"], "/works/OL2W")

            manual = read_csv(path)[0]
            manual["status"] = "APPROVED"
            write_csv(path, MatchRecord.fieldnames(), [manual])
            newer = MatchRecord(local_path="a.pdf", status="NOT_FOUND")
            preserved, _, refreshed = merge_new_decisions(path, [newer], [new.to_dict()])
            self.assertEqual((preserved, refreshed), (1, 0))
            self.assertEqual(read_csv(path)[0]["status"], "APPROVED")


class EnrichmentTests(unittest.TestCase):
    def test_filename_title_uses_text_after_author_year_prefix(self) -> None:
        self.assertEqual(
            filename_title("MUNOS-RASKIN RAMON 2009 walking accessibility to bus rapid transit.pdf"),
            "walking accessibility to bus rapid transit",
        )

    def test_exact_article_doi_is_verified_external_without_ol_search(self) -> None:
        class Metadata:
            google_disabled = False

            def resolve_doi(self, doi):
                return ExternalMetadata(
                    source="crossref", document_type="journal-article",
                    title="Walking accessibility to bus rapid transit", authors=["Ramon Munoz-Raskin"],
                    publisher="Elsevier", year="2010", doi=doi, url="https://doi.org/example",
                )

        class NoOL:
            def search(self, *args, **kwargs):
                raise AssertionError("journal DOI should not be searched as an Open Library book")

        record = CatalogRecord(local_path="a.pdf", filename="a.pdf", doi="10.1/example")
        result = enrich_record(record, {"status": "NOT_FOUND"}, NoOL(), Metadata())
        self.assertEqual(result.verification_status, "VERIFIED_EXTERNAL")
        self.assertEqual(result.canonical_author, "Ramon Munoz-Raskin")

    def test_external_status_is_not_publishable(self) -> None:
        base = MatchRecord(local_path="a.pdf", status="NOT_FOUND", ol_work_key="/works/OL1W")
        enrichment = EnrichmentRecord(
            verification_status="VERIFIED_EXTERNAL", canonical_title="Verified article",
            canonical_author="An Author", confidence="1.000", notes="DOI verified",
        )
        updated = apply_enrichment(base, enrichment)
        self.assertEqual(updated.status, "VERIFIED_EXTERNAL")
        self.assertEqual(updated.ol_work_key, "")

    def test_external_title_similarity_rewards_exact_long_title(self) -> None:
        record = CatalogRecord(detected_title="A long exact transit oriented development research title", year="2022")
        meta = ExternalMetadata(
            source="crossref", document_type="journal-article",
            title="A long exact transit oriented development research title", year="2022",
        )
        score, reasons = external_similarity(record, meta, record.detected_title)
        self.assertGreaterEqual(score, 0.99)
        self.assertIn("year=exact", reasons)

    def test_library_of_congress_mods_recovers_canonical_book_fields(self) -> None:
        from xml.etree import ElementTree as ET

        payload = ET.fromstring("""<?xml version="1.0"?>
        <zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/"
          xmlns:mods="http://www.loc.gov/mods/v3">
          <zs:records><zs:record><zs:recordData><mods:mods>
            <mods:titleInfo><mods:nonSort>The</mods:nonSort>
              <mods:title>death and life of great American cities</mods:title></mods:titleInfo>
            <mods:name usage="primary"><mods:namePart>Jacobs, Jane,</mods:namePart>
              <mods:namePart type="date">1916-2006</mods:namePart></mods:name>
            <mods:originInfo><mods:issuance>monographic</mods:issuance>
              <mods:agent><mods:namePart>Penguin</mods:namePart></mods:agent>
              <mods:dateIssued>1972</mods:dateIssued></mods:originInfo>
            <mods:identifier type="isbn">9780140206814</mods:identifier>
            <mods:identifier type="lccn">75-316480</mods:identifier>
          </mods:mods></zs:recordData></zs:record></zs:records>
        </zs:searchRetrieveResponse>""")
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataAPI(Path(temp), "test", delay=0)
            client._get_xml = lambda *_args, **_kwargs: payload
            results = client.search_library_of_congress("The Death and Life of Great American Cities")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "The death and life of great American cities")
        self.assertEqual(results[0].authors, ["Jacobs, Jane"])
        self.assertEqual(results[0].publisher, "Penguin")
        self.assertEqual(results[0].year, "1972")
        self.assertEqual(results[0].isbn_13, "9780140206814")
        self.assertEqual(results[0].lccn, "75316480")

    def test_google_provider_failure_disables_only_google(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataAPI(Path(temp), "test", delay=0)
            client.session.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rate limited"))
            self.assertEqual(client.search_google_books("A sufficiently long book title"), [])
            self.assertTrue(client.google_disabled)

    def test_verified_metadata_corrects_bad_printed_isbn_and_matches_ol(self) -> None:
        class OL:
            def search(self, query, limit=10):
                if query == "isbn:9780874200829":
                    return {"docs": [{
                        "key": "/works/OL16560037W", "title": "Growing cooler",
                        "author_name": ["Reid H. Ewing"], "isbn": ["9780874200829"],
                        "edition_key": ["OL16636723M"], "publisher": ["Urban Land Institute"],
                        "publish_year": [2008],
                    }]}
                return {"docs": []}

        class Metadata:
            google_disabled = False

        record = CatalogRecord(
            local_folder="Smart Growth", local_path="growing.pdf", filename="growing.pdf",
            detected_title="Growing Cooler", isbn_13="",
        )
        verified = VerifiedMetadataRecord(
            local_folder="Smart Growth", filename_pattern="growing.pdf",
            parent_publication_id="growing-cooler-2008", publication_role="PUBLICATION",
            canonical_title="Growing Cooler: The Evidence on Urban Development and Climate Change",
            canonical_author="Reid H. Ewing; Keith Bartholomew", canonical_publisher="Urban Land Institute",
            canonical_year="2008", isbn_10="0874200822", isbn_13="9780874200829", document_type="book",
        )
        result = enrich_record(record, {"status": "NOT_FOUND"}, OL(), Metadata(), verified=verified)
        self.assertEqual(result.verification_status, "OL_MATCHED")
        self.assertEqual(result.ol_work_key, "/works/OL16560037W")
        self.assertEqual(result.isbn_13, "9780874200829")

    def test_verified_components_collapse_into_one_inventory_work(self) -> None:
        class OL:
            def search(self, *args, **kwargs):
                return {"docs": []}

        class Metadata:
            google_disabled = False

        rule = VerifiedMetadataRecord(
            local_folder="Manual", filename_pattern="part*.pdf", parent_publication_id="manual-1",
            publication_role="COMPONENT", canonical_title="A Verified Manual",
            canonical_author="A Responsible Organization", document_type="manual",
        )
        records = [
            CatalogRecord(local_folder="Manual", local_path=f"part{i}.pdf", filename=f"part{i}.pdf")
            for i in (1, 2)
        ]
        results = [
            enrich_record(record, {"status": "NOT_FOUND"}, OL(), Metadata(), verified=rule)
            for record in records
        ]
        self.assertTrue(all(result.verification_status == "PARENT_COMPONENT" for result in results))
        inventory = build_work_inventory(records, results)
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0].local_pdf_count, 2)

    def test_verified_rule_is_folder_scoped(self) -> None:
        record = CatalogRecord(local_folder="Manual", filename="part1.pdf")
        rules = [VerifiedMetadataRecord(local_folder="Other", filename_pattern="part*.pdf")]
        self.assertIsNone(select_verified_metadata(record, rules))

    def test_verified_manifest_rejects_bad_isbn_checksum(self) -> None:
        class OL:
            def search(self, *args, **kwargs):
                return {"docs": []}

        class Metadata:
            google_disabled = False

        record = CatalogRecord(local_folder="Books", local_path="book.pdf", filename="book.pdf")
        rule = VerifiedMetadataRecord(
            local_folder="Books", filename_pattern="book.pdf", publication_role="PUBLICATION",
            canonical_title="A Book", isbn_13="9780874200822",
        )
        with self.assertRaisesRegex(ValueError, "Invalid verified ISBN-13"):
            enrich_record(record, {"status": "NOT_FOUND"}, OL(), Metadata(), verified=rule)


class LocalMappingTests(unittest.TestCase):
    def test_every_group_gets_stable_id_and_direct_folder_membership(self) -> None:
        records = [
            CatalogRecord(
                local_folder="Parent", local_path="/old/a.pdf", filename="A useful report.pdf",
                detected_title="A Useful Report", detected_author="Research Institute",
                year="2020", text_sample="x" * 200,
            ),
            CatalogRecord(
                local_folder="Nested", local_path="/old/b.pdf", filename="Policy slides.pdf",
                detected_title="Policy Slides", text_sample="x" * 200,
            ),
        ]
        results = [
            EnrichmentRecord(local_path="/old/a.pdf", verification_status="UNIDENTIFIED"),
            EnrichmentRecord(local_path="/old/b.pdf", verification_status="UNIDENTIFIED"),
        ]
        digests = {"/old/a.pdf": "digest-a", "/old/b.pdf": "digest-b"}
        inventory, memberships = build_local_mapping(records, results, digests)
        self.assertEqual(len(inventory), 2)
        self.assertEqual({row.list_name for row in memberships}, {"Parent", "Nested"})
        self.assertTrue(all(row.local_work_id.startswith("OLP-") for row in inventory))
        self.assertTrue(all(row.mapping_status == "LOCAL_PROVISIONAL" for row in inventory))

        moved = [CatalogRecord(**record.to_dict()) for record in records]
        moved[0].local_path = "/new/a.pdf"
        moved_results = [EnrichmentRecord(**result.to_dict()) for result in results]
        moved_results[0].local_path = "/new/a.pdf"
        moved_digests = {"/new/a.pdf": "digest-a", "/old/b.pdf": "digest-b"}
        moved_inventory, _ = build_local_mapping(moved, moved_results, moved_digests)
        self.assertEqual(
            {row.canonical_title: row.local_work_id for row in inventory},
            {row.canonical_title: row.local_work_id for row in moved_inventory},
        )

    def test_split_components_become_one_work_and_one_membership(self) -> None:
        records = [
            CatalogRecord(local_folder="Manual", local_path=f"part{i}.pdf", filename=f"part{i}.pdf")
            for i in (1, 2)
        ]
        results = [
            EnrichmentRecord(
                local_path=f"part{i}.pdf", verification_status="PARENT_COMPONENT",
                source="verified_manifest", parent_publication_id="manual-1",
                canonical_title="A Verified Manual", document_type="manual", confidence="1.000",
            )
            for i in (1, 2)
        ]
        inventory, memberships = build_local_mapping(
            records, results, {"part1.pdf": "a", "part2.pdf": "b"},
        )
        self.assertEqual(len(inventory), 1)
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0].local_pdf_count, 2)
        self.assertEqual(inventory[0].openlibrary_action, "CREATE_RECORD_CANDIDATE")

    def test_verified_manifest_author_survives_the_junk_creator_filter(self) -> None:
        records = [CatalogRecord(local_folder="Otras", local_path="ents.pdf", filename="ents.pdf")]
        long_author = "; ".join([
            "Ministerio de Transporte de Colombia",
            "Ministerio de Ambiente y Desarrollo Sostenible",
            "Ministerio de Minas y Energia",
            "Departamento Nacional de Planeacion",
            "Unidad de Planeacion Minero Energetica",
        ])
        results = [EnrichmentRecord(
            local_path="ents.pdf", verification_status="CREATE_CANDIDATE",
            source="verified_manifest", canonical_title="Estrategia Nacional",
            canonical_author=long_author, canonical_publisher="Gobierno de Colombia",
            canonical_year="2022", document_type="report",
            external_url="https://example.org/ents", confidence="1.000",
        )]
        inventory, _ = build_local_mapping(records, results, {"ents.pdf": "d"})
        self.assertEqual(inventory[0].canonical_author, long_author)

    def test_noisy_pdf_author_is_still_discarded(self) -> None:
        records = [CatalogRecord(
            local_folder="Otras", local_path="noisy.pdf", filename="noisy.pdf",
            pdf_author="Microsoft Word - generated by Acrobat Distiller",
        )]
        results = [EnrichmentRecord(local_path="noisy.pdf", verification_status="UNIDENTIFIED")]
        inventory, _ = build_local_mapping(records, results, {"noisy.pdf": "d"})
        self.assertEqual(inventory[0].canonical_author, "")

    def test_monograph_like_types_are_eligible_for_the_book_catalog(self) -> None:
        for doc_type in (
            "working-paper", "white-paper", "discussion-paper", "research-brief",
            "guidebook", "guidelines", "consultant-report", "capstone", "teaching-case",
        ):
            self.assertIn(doc_type, OL_BOOKLIKE_TYPES, doc_type)
        for doc_type in ("article", "presentation", "legal-document", "map"):
            self.assertNotIn(doc_type, OL_BOOKLIKE_TYPES, doc_type)

    def test_document_type_routing_is_conservative(self) -> None:
        legal = CatalogRecord(filename="Decreto 497 de 2023.pdf", local_folder="PMMS")
        article = CatalogRecord(filename="Journal cities 2012 TOD Denver.pdf", local_folder="DOTS")
        self.assertEqual(classify_document_type(legal, "Decreto 497 de 2023"), "legal-document")
        self.assertEqual(classify_document_type(article, "TOD Denver"), "journal-article")


class CreationRecordTests(unittest.TestCase):
    """Open Library's import API accepts identifier-less records; so must we."""

    GREY_LITERATURE = {
        "detected_title": "Lineamientos para una Política Nacional DOTS",
        "detected_author": "Consorcio SIGMA Gestión de Proyectos – Despacio",
        "publisher": "Findeter", "year": "2020",
        "isbn_10": "", "isbn_13": "", "lccn": "", "ocaid": "",
        "local_folder": "Proyecto", "filename": "lineamientos.pdf",
    }

    def test_complete_record_without_any_identifier_is_valid(self) -> None:
        self.assertEqual(validate_creation_row(dict(self.GREY_LITERATURE)), [])

    def test_record_missing_publisher_or_date_is_rejected(self) -> None:
        for field in ("publisher", "year", "detected_author", "detected_title"):
            row = dict(self.GREY_LITERATURE)
            row[field] = ""
            self.assertTrue(validate_creation_row(row), field)

    def test_bare_record_with_a_strong_identifier_is_valid(self) -> None:
        row = {
            "detected_title": "Growing Cooler", "detected_author": "", "publisher": "",
            "year": "", "isbn_13": "9780874200829", "isbn_10": "", "lccn": "", "ocaid": "",
        }
        self.assertEqual(validate_creation_row(row), [])

    def test_ocaid_alone_is_not_a_strong_identifier_for_import(self) -> None:
        row = {
            "detected_title": "Something", "detected_author": "", "publisher": "",
            "year": "", "isbn_13": "", "isbn_10": "", "lccn": "", "ocaid": "somebook00",
        }
        self.assertTrue(validate_creation_row(row))

    def test_addbook_form_uses_the_current_field_names(self) -> None:
        # Open Library's /books/add handler reads book_title (it overwrites
        # `title` with it) and the unflattened author_names--N / authors--N--author--key.
        form = build_addbook_form(dict(self.GREY_LITERATURE), external_url="https://x.test/a")
        self.assertEqual(form["book_title"], self.GREY_LITERATURE["detected_title"])
        self.assertEqual(form["author_names--0"], self.GREY_LITERATURE["detected_author"])
        self.assertEqual(form["authors--0--author--key"], "__new__")
        self.assertEqual(form["publisher"], "Findeter")
        self.assertEqual(form["publish_date"], "2020")
        self.assertEqual(form["web_book_url"], "https://x.test/a")
        self.assertEqual(form["_save"], "")

    def test_addbook_form_omits_identifier_fields_when_there_is_none(self) -> None:
        form = build_addbook_form(dict(self.GREY_LITERATURE))
        self.assertEqual(form.get("id_name", ""), "")
        self.assertEqual(form.get("id_value", ""), "")

    def test_addbook_form_carries_one_identifier_when_present(self) -> None:
        form = build_addbook_form(dict(self.GREY_LITERATURE, isbn_13="9780874200829"))
        self.assertEqual(form["id_name"], "isbn_13")
        self.assertEqual(form["id_value"], "9780874200829")

    def test_addbook_form_indexes_every_author(self) -> None:
        row = dict(self.GREY_LITERATURE, detected_author="Ada Lovelace; Alan Turing")
        form = build_addbook_form(row)
        self.assertEqual(form["author_names--0"], "Ada Lovelace")
        self.assertEqual(form["author_names--1"], "Alan Turing")
        self.assertEqual(form["authors--1--author--key"], "__new__")

    def test_addbook_test_mode_flag_is_opt_in(self) -> None:
        self.assertEqual(build_addbook_form(dict(self.GREY_LITERATURE))["_test"], "false")
        self.assertEqual(
            build_addbook_form(dict(self.GREY_LITERATURE), test=True)["_test"], "true",
        )

    def test_import_record_shape_matches_the_openlibrary_schema(self) -> None:
        row = dict(self.GREY_LITERATURE, doi="10.1000/xyz", ocaid="")
        record = build_import_record(row, "mysource", local_work_id="OLP-ABC")
        self.assertEqual(record["title"], self.GREY_LITERATURE["detected_title"])
        self.assertEqual(record["authors"], [{"name": self.GREY_LITERATURE["detected_author"]}])
        self.assertEqual(record["publishers"], ["Findeter"])
        self.assertEqual(record["publish_date"], "2020")
        self.assertEqual(record["source_records"], ["mysource:OLP-ABC"])
        self.assertNotIn("isbn_10", record)
        self.assertNotIn("isbn_13", record)

    def test_import_record_carries_identifiers_as_lists_when_present(self) -> None:
        row = dict(self.GREY_LITERATURE, isbn_13="9780874200829", lccn="2007034556")
        record = build_import_record(row, "mysource", local_work_id="OLP-ABC")
        self.assertEqual(record["isbn_13"], ["9780874200829"])
        self.assertEqual(record["lccn"], ["2007034556"])

    def test_components_of_one_work_yield_a_single_import(self) -> None:
        rows = [
            {"local_path": f"/atlas/part{i}.pdf", "detected_title": "Atlas", "filename": f"p{i}.pdf"}
            for i in (1, 2, 3)
        ]
        work_ids = {f"/atlas/part{i}.pdf": "OLP-ATLAS" for i in (1, 2, 3)}
        deduped = dedupe_creation_rows(rows, work_ids)
        self.assertEqual([row["local_path"] for row, _ in deduped], ["/atlas/part1.pdf"])
        self.assertEqual(deduped[0][1], "OLP-ATLAS")

    def test_distinct_works_are_all_kept(self) -> None:
        rows = [
            {"local_path": "/a.pdf", "detected_title": "A", "filename": "a.pdf"},
            {"local_path": "/b.pdf", "detected_title": "B", "filename": "b.pdf"},
        ]
        work_ids = {"/a.pdf": "OLP-A", "/b.pdf": "OLP-B"}
        self.assertEqual(len(dedupe_creation_rows(rows, work_ids)), 2)

    def test_rows_without_a_known_work_id_fall_back_to_title(self) -> None:
        rows = [
            {"local_path": "/x.pdf", "detected_title": "Same Title", "filename": "x.pdf"},
            {"local_path": "/y.pdf", "detected_title": "same title", "filename": "y.pdf"},
        ]
        self.assertEqual(len(dedupe_creation_rows(rows, {})), 1)

    def test_multiple_publishers_are_split_on_semicolons(self) -> None:
        row = dict(self.GREY_LITERATURE, publisher="Center for Clean Air Policy (CCAP); Findeter")
        record = build_import_record(row, "mysource", local_work_id="OLP-ABC")
        self.assertEqual(record["publishers"], ["Center for Clean Air Policy (CCAP)", "Findeter"])

    def test_multiple_authors_are_split_on_semicolons(self) -> None:
        row = dict(self.GREY_LITERATURE, detected_author="Ada Lovelace; Alan Turing")
        record = build_import_record(row, "mysource", local_work_id="OLP-ABC")
        self.assertEqual(record["authors"], [{"name": "Ada Lovelace"}, {"name": "Alan Turing"}])


class _CapturingSession(requests.Session):
    """Real session, real header merging, no network."""

    def __init__(self, body: bytes = b"", url: str = "") -> None:
        super().__init__()
        self.sent: requests.PreparedRequest | None = None
        self._body, self._url = body, url

    def send(self, request, **kwargs):  # type: ignore[override]
        self.sent = request
        response = requests.Response()
        response.status_code = 200
        response.url = self._url
        response._content = self._body
        response.request = request
        return response


class AuthorResolutionTests(unittest.TestCase):
    """Open Library skips duplicate-matching entirely when any author is __new__,
    so authors must be resolved to existing keys before a book is submitted."""

    def _api(self, payload):
        session = _CapturingSession(body=json.dumps(payload).encode(), url="")
        api = OpenLibraryAPI(session=session, delay=0.0)
        return api

    def test_exact_name_resolves_to_the_existing_author(self) -> None:
        api = self._api([{"key": "/authors/OL232672A", "name": "Robert Cervero"}])
        found = api.find_author("robert  cervero")
        self.assertEqual(found["key"], "/authors/OL232672A")

    def test_near_miss_is_reported_but_not_linked(self) -> None:
        api = self._api([{"key": "/authors/OL267851A", "name": "Reid H. Ewing"}])
        found = api.find_author("Reid Ewing")
        self.assertEqual(found["key"], "")
        self.assertEqual(found["candidates"], ["Reid H. Ewing"])

    def test_no_candidates_means_no_key(self) -> None:
        api = self._api([])
        found = api.find_author("Secretaría Distrital de Movilidad de Bogotá")
        self.assertEqual(found["key"], "")
        self.assertEqual(found["candidates"], [])


class ResumeTests(unittest.TestCase):
    """A 57-record batch must be resumable: never re-create what already landed."""

    WORK_IDS = {"/a.pdf": "OLP-A", "/b.pdf": "OLP-B", "/a2.pdf": "OLP-A"}

    def test_a_work_with_a_key_counts_as_done(self) -> None:
        rows = [
            {"local_path": "/a.pdf", "detected_title": "A", "ol_work_key": "/works/OL1W",
             "ol_edition_key": "/books/OL1M"},
            {"local_path": "/b.pdf", "detected_title": "B", "ol_work_key": "", "ol_edition_key": ""},
        ]
        self.assertEqual(completed_work_ids(rows, self.WORK_IDS), {"OLP-A"})

    def test_any_component_carrying_a_key_marks_the_whole_work_done(self) -> None:
        rows = [
            {"local_path": "/a.pdf", "detected_title": "A", "ol_work_key": "", "ol_edition_key": ""},
            {"local_path": "/a2.pdf", "detected_title": "A", "ol_work_key": "/works/OL1W",
             "ol_edition_key": ""},
        ]
        self.assertEqual(completed_work_ids(rows, self.WORK_IDS), {"OLP-A"})

    def test_nothing_done_is_an_empty_set(self) -> None:
        rows = [{"local_path": "/b.pdf", "detected_title": "B", "ol_work_key": "", "ol_edition_key": ""}]
        self.assertEqual(completed_work_ids(rows, self.WORK_IDS), set())

    def test_completed_keys_line_up_with_the_dedupe_keys(self) -> None:
        # The skip set is useless unless it uses the same key the loop iterates on.
        rows = [{"local_path": "/a.pdf", "detected_title": "A", "ol_work_key": "/works/OL1W",
                 "ol_edition_key": ""}]
        from library_importer import _creation_key
        done = completed_work_ids(rows, self.WORK_IDS)
        deduped = dedupe_creation_rows(rows, self.WORK_IDS)
        self.assertEqual({_creation_key(row, self.WORK_IDS) for row, _ in deduped}, done)

    def test_rows_without_a_known_work_id_still_resume_by_title(self) -> None:
        rows = [{"local_path": "/x.pdf", "detected_title": "Same", "ol_work_key": "/works/OL9W",
                 "ol_edition_key": ""}]
        self.assertEqual(completed_work_ids(rows, {}), {"title:same"})


class BatchResilienceTests(unittest.TestCase):
    """A failure at record N must keep the first N-1, and resume must not duplicate."""

    FIELDS = MatchRecord.fieldnames()

    def _workspace(self):
        import csv as _csv
        tmp = Path(tempfile.mkdtemp())
        rows = []
        for i in (1, 2, 3):
            row = {f: "" for f in self.FIELDS}
            row.update({
                "local_folder": "F", "local_path": f"/p{i}.pdf", "filename": f"p{i}.pdf",
                "detected_title": f"Title {i}", "detected_author": f"Author {i}",
                "publisher": "Pub", "year": "2020", "status": "CREATE_CANDIDATE",
                "create_record": "YES",
            })
            rows.append(row)
        with (tmp / "decisions.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            writer = _csv.DictWriter(fh, fieldnames=self.FIELDS)
            writer.writeheader(); writer.writerows(rows)
        inventory = [{
            "local_work_id": f"OLP-{i}", "external_url": "", "local_paths_json": f'["/p{i}.pdf"]',
        } for i in (1, 2, 3)]
        with (tmp / "work_inventory.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            writer = _csv.DictWriter(fh, fieldnames=list(inventory[0]))
            writer.writeheader(); writer.writerows(inventory)
        return tmp

    def _run(self, tmp, failing_titles):
        import library_importer as li

        class FakeAPI:
            def find_author(self, name):
                return {"key": "", "candidates": []}

            def search(self, *a, **k):
                return {"docs": []}

            def add_book(self, form):
                if form["book_title"] in failing_titles:
                    raise RuntimeError("Open Library rejected the POST with HTTP 503")
                n = form["book_title"].split()[-1]
                return {"work_key": f"/works/OL{n}W", "edition_key": f"/books/OL{n}M"}

        original = li.make_api
        li.make_api = lambda *a, **k: FakeAPI()
        try:
            args = argparse.Namespace(
                approved_only=True, data_dir=tmp, dry_run=False,
                route="addbook", match_check=False,
            )
            li.command_create_missing(args, {})
        finally:
            li.make_api = original
        return read_csv(tmp / "decisions.csv")

    def test_failure_midway_keeps_earlier_successes_on_disk(self) -> None:
        tmp = self._workspace()
        rows = self._run(tmp, failing_titles={"Title 2"})
        by_title = {r["detected_title"]: r for r in rows}
        self.assertEqual(by_title["Title 1"]["ol_work_key"], "/works/OL1W")
        self.assertEqual(by_title["Title 3"]["ol_work_key"], "/works/OL3W")
        self.assertEqual(by_title["Title 2"]["ol_work_key"], "")
        self.assertIn("still pending", by_title["Title 2"]["notes"])

    def test_rerun_retries_only_the_failure(self) -> None:
        tmp = self._workspace()
        self._run(tmp, failing_titles={"Title 2"})
        rows = self._run(tmp, failing_titles=set())
        by_title = {r["detected_title"]: r for r in rows}
        self.assertEqual(by_title["Title 2"]["ol_work_key"], "/works/OL2W")
        # The two that already landed keep their original keys, not new ones.
        self.assertEqual(by_title["Title 1"]["ol_work_key"], "/works/OL1W")
        self.assertEqual(by_title["Title 3"]["ol_work_key"], "/works/OL3W")

    def test_a_fully_completed_batch_creates_nothing_on_rerun(self) -> None:
        tmp = self._workspace()
        self._run(tmp, failing_titles=set())
        created_marker = {"count": 0}

        import library_importer as li

        class CountingAPI:
            def find_author(self, name): return {"key": "", "candidates": []}
            def search(self, *a, **k): return {"docs": []}
            def add_book(self, form):
                created_marker["count"] += 1
                return {"work_key": "/works/OLXW", "edition_key": "/books/OLXM"}

        original = li.make_api
        li.make_api = lambda *a, **k: CountingAPI()
        try:
            li.command_create_missing(argparse.Namespace(
                approved_only=True, data_dir=tmp, dry_run=False,
                route="addbook", match_check=False), {})
        finally:
            li.make_api = original
        self.assertEqual(created_marker["count"], 0)


class AuthorAliasTests(unittest.TestCase):
    """Human-approved corporate headings win over autocomplete guessing."""

    HEADER = "local_name,openlibrary_name,openlibrary_key,openlibrary_works,notes\n"

    def _dir(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "author_aliases.csv").write_text(self.HEADER + body, encoding="utf-8")
        return tmp

    def test_alias_maps_a_local_name_to_an_openlibrary_key(self) -> None:
        data = self._dir("Despacio,Despacio.org,/authors/OL16572654A,1,verified\n")
        self.assertEqual(load_author_aliases(data), {"Despacio": "/authors/OL16572654A"})

    def test_rows_without_a_key_are_ignored(self) -> None:
        data = self._dir("Someone,Something,,0,unresolved\n")
        self.assertEqual(load_author_aliases(data), {})

    def test_a_missing_table_is_not_an_error(self) -> None:
        self.assertEqual(load_author_aliases(Path(tempfile.mkdtemp())), {})


class AddBookAuthorKeyTests(unittest.TestCase):
    ROW = {
        "detected_title": "T", "detected_author": "Robert Cervero; Nueva Entidad",
        "publisher": "P", "year": "2020",
    }

    def test_resolved_authors_are_linked_and_the_rest_are_new(self) -> None:
        form = build_addbook_form(
            dict(self.ROW), author_keys={"Robert Cervero": "/authors/OL232672A"},
        )
        self.assertEqual(form["authors--0--author--key"], "/authors/OL232672A")
        self.assertEqual(form["authors--1--author--key"], "__new__")

    def test_without_resolution_every_author_is_new(self) -> None:
        form = build_addbook_form(dict(self.ROW))
        self.assertEqual(form["authors--0--author--key"], "__new__")
        self.assertEqual(form["authors--1--author--key"], "__new__")


class CreationBannerTests(unittest.TestCase):
    """The banner must never claim a write is happening when none is."""

    def test_match_check_does_not_claim_to_be_creating(self) -> None:
        banner = creation_mode_banner(dry_run=False, match_check=True)
        self.assertNotIn("Creating", banner)
        self.assertIn("nothing is written", banner)

    def test_dry_run_says_so(self) -> None:
        self.assertIn("DRY RUN", creation_mode_banner(dry_run=True, match_check=False))
        self.assertIn("DRY RUN", creation_mode_banner(dry_run=True, match_check=True))

    def test_only_a_real_run_announces_creation(self) -> None:
        self.assertIn("Creating", creation_mode_banner(dry_run=False, match_check=False))


class AddBookRequestTests(unittest.TestCase):
    """olclient pins Content-Type: application/json on the shared session, so a
    form POST must override it per-request or the body contradicts its header."""

    EDITION_URL = "https://openlibrary.org/books/OL111M/Title/edit?mode=add-work"

    def _api(self, body: bytes = b'"/works/OL222W"', url: str = EDITION_URL):
        session = _CapturingSession(body=body, url=url)
        session.headers.update({"Content-Type": "application/json"})  # what olclient does
        api = OpenLibraryAPI(session=session, delay=0.0)
        api._olclient = object()
        return api, session

    def test_form_post_is_sent_as_form_encoded_not_json(self) -> None:
        api, session = self._api()
        api.add_book({"book_title": "X", "_test": "false"})
        self.assertEqual(
            session.sent.headers["Content-Type"], "application/x-www-form-urlencoded",
        )
        self.assertEqual(session.sent.body, "book_title=X&_test=false")

    def test_form_post_does_not_ask_for_a_json_response(self) -> None:
        api, session = self._api()
        api.add_book({"book_title": "X", "_test": "false"})
        self.assertNotIn("application/json", session.sent.headers.get("Accept", ""))

    def test_a_rejected_post_explains_itself(self) -> None:
        session = _CapturingSession(body=b"<html>blocked by edge</html>", url=self.EDITION_URL)
        session.headers.update({"Content-Type": "application/json"})
        api = OpenLibraryAPI(session=session, delay=0.0)
        api._olclient = object()

        original_send = session.send

        def rejecting_send(request, **kwargs):
            response = original_send(request, **kwargs)
            response.status_code = 460
            return response

        session.send = rejecting_send  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError) as caught:
            api.add_book({"book_title": "X", "_test": "false"})
        message = str(caught.exception)
        self.assertIn("460", message)
        self.assertIn("blocked by edge", message)

    def test_new_edition_and_work_keys_are_returned(self) -> None:
        api, _ = self._api()
        keys = api.add_book({"book_title": "X", "_test": "false"})
        self.assertEqual(keys["edition_key"], "/books/OL111M")
        self.assertEqual(keys["work_key"], "/works/OL222W")

    def test_match_check_reports_without_claiming_a_creation(self) -> None:
        api, _ = self._api(body=b'Matched <a href="/works/OL999W">', url="https://openlibrary.org/books/add")
        outcome = api.add_book({"book_title": "X", "_test": "true"})
        self.assertEqual(outcome["outcome"], "matched")
        self.assertEqual(outcome["matched_key"], "OL999W")

    def test_match_check_reports_a_would_be_creation(self) -> None:
        api, _ = self._api(body=b"No match found", url="https://openlibrary.org/books/add")
        self.assertEqual(api.add_book({"book_title": "X", "_test": "true"})["outcome"], "would_create")


if __name__ == "__main__":
    unittest.main()


class EditionAttachmentTests(unittest.TestCase):
    """Attaching an alternate edition to a Work that already exists.

    Open Library's addbook handler reads `work` back out of the posted form
    (addbook.py: `work_key = i.get("work")` inside find_matches). When that key
    resolves, the handler returns `edition or work`: an existing edition means
    Case 3 and nothing new is written, a bare work means Case 2 and a new
    edition is saved under it. Emitting the field is therefore the whole
    difference between a second edition and a duplicate work.
    """

    ROW = {
        "detected_title": "Bogotá 21", "detected_author": "Gregor Wessels",
        "publisher": "Fundación Despacio", "year": "2012",
    }

    def test_parent_work_key_is_posted_as_the_work_field(self) -> None:
        form = build_addbook_form(dict(self.ROW), parent_work_key="/works/OL45930591W")
        self.assertEqual(form["work"], "/works/OL45930591W")

    def test_without_a_parent_no_work_field_is_sent(self) -> None:
        form = build_addbook_form(dict(self.ROW))
        self.assertNotIn(
            "work", form,
            "An empty work field would make find_matches treat it as a check-page reply",
        )

    def test_parent_attachment_still_links_resolved_authors(self) -> None:
        # A single __new__ author sets created_author, and addbook then skips
        # find_matches entirely -- which would ignore the work key and fork a
        # duplicate Work instead of adding an edition.
        form = build_addbook_form(
            dict(self.ROW), parent_work_key="/works/OL45930591W",
            author_keys={"Gregor Wessels": "/authors/OL9106169A"},
        )
        self.assertEqual(form["authors--0--author--key"], "/authors/OL9106169A")


class EditionSafetyTests(unittest.TestCase):
    """An edition attachment must never be attempted with an unresolved author."""

    def test_unresolved_author_blocks_the_attachment(self) -> None:
        problems = validate_edition_row(
            {"detected_title": "T", "detected_author": "A Person", "publisher": "P", "year": "2012"},
            parent_work_key="/works/OL1W", author_keys={},
        )
        self.assertTrue(any("author" in p for p in problems), problems)

    def test_missing_parent_blocks_the_attachment(self) -> None:
        problems = validate_edition_row(
            {"detected_title": "T", "detected_author": "A Person", "publisher": "P", "year": "2012"},
            parent_work_key="", author_keys={"A Person": "/authors/OL1A"},
        )
        self.assertTrue(any("parent" in p for p in problems), problems)

    def test_fully_resolved_row_is_allowed(self) -> None:
        problems = validate_edition_row(
            {"detected_title": "T", "detected_author": "A Person", "publisher": "P", "year": "2012"},
            parent_work_key="/works/OL1W", author_keys={"A Person": "/authors/OL1A"},
        )
        self.assertEqual(problems, [])


class EditionPlanTests(unittest.TestCase):
    """Pairing each ALTERNATE_EDITION row with the parent Work already on Open Library."""

    PARENT = {
        "local_path": "/a/parent.pdf", "filename": "parent.pdf", "local_folder": "F",
        "detected_title": "Bogotá 21", "detected_author": "Gregor Wessels",
        "publisher": "Fundación Despacio", "year": "2012",
        "status": "APPROVED", "ol_work_key": "/works/OL91W", "ol_edition_key": "/books/OL91M",
    }
    ALTERNATE = {
        "local_path": "/a/spanish.pdf", "filename": "spanish.pdf", "local_folder": "F",
        "detected_title": "Bogotá 21 (español)", "detected_author": "Gregor Wessels",
        "publisher": "Fundación Despacio", "year": "2012",
        "status": "ALTERNATE_EDITION", "ol_work_key": "", "ol_edition_key": "",
    }
    WORK_IDS = {"/a/parent.pdf": "w1", "/a/spanish.pdf": "w1"}
    KEYS = {"Gregor Wessels": "/authors/OL9106169A"}

    def test_alternate_inherits_the_parent_work_key(self) -> None:
        plan = edition_attachment_plan(
            [dict(self.PARENT), dict(self.ALTERNATE)], self.WORK_IDS, self.KEYS,
        )
        self.assertEqual(len(plan), 1)
        row, parent_key, problems = plan[0]
        self.assertEqual(row["filename"], "spanish.pdf")
        self.assertEqual(parent_key, "/works/OL91W")
        self.assertEqual(problems, [])

    def test_parent_row_itself_is_not_replanned(self) -> None:
        plan = edition_attachment_plan(
            [dict(self.PARENT), dict(self.ALTERNATE)], self.WORK_IDS, self.KEYS,
        )
        self.assertNotIn("parent.pdf", [row["filename"] for row, _, _ in plan])

    def test_alternate_without_a_created_parent_is_blocked(self) -> None:
        orphan_parent = dict(self.PARENT, ol_work_key="", ol_edition_key="")
        plan = edition_attachment_plan(
            [orphan_parent, dict(self.ALTERNATE)], self.WORK_IDS, self.KEYS,
        )
        _, parent_key, problems = plan[0]
        self.assertEqual(parent_key, "")
        self.assertTrue(any("parent" in p for p in problems), problems)

    def test_unresolved_author_is_reported_as_a_blocker(self) -> None:
        plan = edition_attachment_plan(
            [dict(self.PARENT), dict(self.ALTERNATE)], self.WORK_IDS, author_keys={},
        )
        _, _, problems = plan[0]
        self.assertTrue(any("author" in p for p in problems), problems)
