import csv
from datetime import datetime

from whatsapp_paper_collector.models import ChatMessage, LinkOccurrence, PaperRecord
from whatsapp_paper_collector.storage import LibraryStore, artifact_filename


def _occurrence(url: str, message: str = "Paper") -> LinkOccurrence:
    chat_message = ChatMessage(
        timestamp_raw="4/7/26, 9:00:00 AM",
        timestamp=datetime(2026, 7, 4, 9, 0, 0),
        sender="Scientist 🧬",
        text=f"{message}: {url}",
    )
    return LinkOccurrence(url, url, chat_message)


def test_requested_filename_format_and_fallbacks() -> None:
    paper = PaperRecord(
        canonical_key="doi:10.1000/test",
        title="A Single Cell Atlas of the Brain",
        authors=["Alex Smith"],
        publication_year=2026,
    )
    assert artifact_filename(1, paper, "pdf") == "000001-Smith-2026-a_single_cell_atlas_of_the_brain.pdf"

    compound = PaperRecord(
        canonical_key="doi:10.1000/compound",
        title="Compound Surname",
        authors=["de Silva, Ana"],
        publication_year=2024,
    )
    assert artifact_filename(3, compound, "pdf") == "000003-de_Silva-2024-compound_surname.pdf"

    unknown = PaperRecord(canonical_key="url:test")
    assert artifact_filename(2, unknown, ".txt") == "000002-UnknownAuthor-UnknownYear-untitled.txt"


def test_sqlite_deduplication_provenance_and_csv_exports(tmp_path) -> None:
    with LibraryStore(tmp_path / "library") as store:
        import_id = store.start_import(tmp_path / "chat.txt", "hash")
        paper = PaperRecord(
            canonical_key="doi:10.1000/test",
            title="Protein Folding",
            authors=["Jane Jones"],
            publication_year=2025,
            doi="10.1000/test",
            access_status="downloaded_pdf",
        )
        paper_id, created, _, _ = store.upsert_paper(paper)
        same_id, created_again, _, _ = store.upsert_paper(paper)
        assert created is True
        assert created_again is False
        assert same_id == paper_id

        path = store.save_artifact(paper_id, paper, "pdf", content_bytes=b"%PDF-test")
        first = _occurrence("https://doi.org/10.1000/test")
        second = _occurrence("https://publisher.test/paper", "Same paper elsewhere")
        store.upsert_occurrence(import_id, first, paper_id=paper_id, final_url=first.original_url, result_status="downloaded_pdf")
        store.upsert_occurrence(import_id, second, paper_id=paper_id, final_url=second.original_url, result_status="duplicate")
        store.finish_import(import_id, {"pdfs": 1})
        store.export_csvs()

        assert path == "files/000001-Jones-2025-protein_folding.pdf"
        assert (store.root / path).read_bytes() == b"%PDF-test"

    with (tmp_path / "library" / "papers.csv").open(encoding="utf-8", newline="") as handle:
        papers = list(csv.DictReader(handle))
    with (tmp_path / "library" / "occurrences.csv").open(encoding="utf-8", newline="") as handle:
        occurrences = list(csv.DictReader(handle))
    assert len(papers) == 1
    assert papers[0]["authors"] == "Jane Jones"
    assert papers[0]["occurrence_count"] == "2"
    assert len(occurrences) == 2
    assert occurrences[0]["sender"] == "Scientist 🧬"
