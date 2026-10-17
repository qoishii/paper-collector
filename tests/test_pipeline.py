import csv
import sqlite3
from pathlib import Path

from whatsapp_paper_collector.models import Outcome, PaperRecord, ResolutionResult
from whatsapp_paper_collector.pipeline import Collector
from whatsapp_paper_collector.storage import LibraryStore


class FakeResolver:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, occurrence) -> ResolutionResult:
        self.calls += 1
        if "uncertain" in occurrence.original_url:
            return ResolutionResult(
                outcome=Outcome.REVIEW,
                final_url=occurrence.original_url,
                review_reason="No scholarly metadata.",
            )
        paper = PaperRecord(
            canonical_key="doi:10.1000/pipeline",
            title="A Pipeline Paper",
            authors=["Alex Smith"],
            publication_year=2026,
            doi="10.1000/pipeline",
            access_status=Outcome.DOWNLOADED_PDF.value,
            landing_url=occurrence.original_url,
        )
        return ResolutionResult(
            outcome=Outcome.DOWNLOADED_PDF,
            paper=paper,
            final_url="https://repository.test/paper.pdf",
            artifact_bytes=b"%PDF-pipeline",
            artifact_extension="pdf",
        )


def test_end_to_end_import_rerun_and_review(tmp_path: Path) -> None:
    chat = tmp_path / "chat.txt"
    chat.write_text(
        "[4/7/26, 9:00:00 AM] Scientist: https://publisher.test/10.1000/pipeline\n"
        "[4/7/26, 9:01:00 AM] Colleague: https://publisher.test/10.1000/pipeline\n"
        "[4/7/26, 9:02:00 AM] Scientist: https://zoom.us/j/123\n"
        "[4/7/26, 9:03:00 AM] Scientist: https://uncertain.test/page\n",
        encoding="utf-8",
    )
    resolver = FakeResolver()
    library = tmp_path / "library"

    with LibraryStore(library) as store:
        collector = Collector(resolver, store)
        first = collector.import_chat(chat)
        second = collector.import_chat(chat)

    assert first == {
        "links": 4,
        "pdfs": 1,
        "fulltexts": 0,
        "abstracts": 0,
        "metadata_only": 0,
        "duplicates": 1,
        "ignored": 1,
        "review": 1,
        "failed": 0,
    }
    assert second["duplicates"] == 2
    assert second["ignored"] == 1
    assert second["review"] == 1
    assert resolver.calls == 3  # One paper and the review link once per import.

    assert (library / "files" / "000001-Smith-2026-a_pipeline_paper.pdf").read_bytes() == b"%PDF-pipeline"
    with sqlite3.connect(library / "library.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM link_occurrences").fetchone()[0] == 3

    with (library / "review.csv").open(encoding="utf-8", newline="") as handle:
        review_rows = list(csv.DictReader(handle))
    assert len(review_rows) == 1
    assert review_rows[0]["original_url"] == "https://uncertain.test/page"

