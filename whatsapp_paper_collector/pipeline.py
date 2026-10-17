from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from .models import LinkOccurrence, Outcome, ResolutionResult, Resolver
from .storage import LibraryStore
from .urls import is_meeting_url
from .whatsapp import extract_links, parse_chat_file


ProgressCallback = Callable[[int, int, str, str], None]


def _empty_summary() -> dict[str, int]:
    return {
        "links": 0,
        "pdfs": 0,
        "fulltexts": 0,
        "abstracts": 0,
        "metadata_only": 0,
        "duplicates": 0,
        "ignored": 0,
        "review": 0,
        "failed": 0,
    }


class Collector:
    def __init__(self, resolver: Resolver, store: LibraryStore):
        self.resolver = resolver
        self.store = store

    def import_chat(
        self,
        chat_path: Path,
        *,
        date_order: str = "dmy",
        progress: ProgressCallback | None = None,
    ) -> dict[str, int]:
        raw_bytes = chat_path.read_bytes()
        source_hash = hashlib.sha256(raw_bytes).hexdigest()
        import_id = self.store.start_import(chat_path, source_hash)
        messages = parse_chat_file(chat_path, date_order=date_order)
        occurrences = extract_links(messages)
        summary = _empty_summary()
        summary["links"] = len(occurrences)

        for index, occurrence in enumerate(occurrences, start=1):
            if is_meeting_url(occurrence.normalized_url):
                summary["ignored"] += 1
                self._progress(progress, index, len(occurrences), occurrence.original_url, "ignored meeting link")
                continue

            previous_status = self.store.occurrence_status(occurrence)
            if previous_status in {Outcome.DOWNLOADED_PDF.value, Outcome.SAVED_FULLTEXT.value, Outcome.DUPLICATE.value}:
                summary["duplicates"] += 1
                self._progress(progress, index, len(occurrences), occurrence.original_url, "already imported")
                continue

            complete_paper = self.store.complete_paper_for_url(occurrence.normalized_url)
            if complete_paper is not None:
                self.store.upsert_occurrence(
                    import_id,
                    occurrence,
                    paper_id=int(complete_paper["id"]),
                    final_url=str(complete_paper["oa_url"] or complete_paper["landing_url"]),
                    result_status=Outcome.DUPLICATE.value,
                )
                summary["duplicates"] += 1
                self._progress(progress, index, len(occurrences), occurrence.original_url, "linked to existing paper")
                continue

            try:
                result = self.resolver.resolve(occurrence)
                status = self._store_result(import_id, occurrence, result)
            except Exception as error:  # An individual bad link must not stop the import.
                reason = f"{type(error).__name__}: {error}"
                self.store.upsert_occurrence(
                    import_id,
                    occurrence,
                    paper_id=None,
                    final_url=occurrence.original_url,
                    result_status=Outcome.FAILED.value,
                    review_reason=reason,
                )
                summary["failed"] += 1
                summary["review"] += 1
                self._progress(progress, index, len(occurrences), occurrence.original_url, "failed; added to review")
                continue

            if status == Outcome.DOWNLOADED_PDF:
                summary["pdfs"] += 1
            elif status == Outcome.SAVED_FULLTEXT:
                summary["fulltexts"] += 1
            elif status == Outcome.SAVED_ABSTRACT:
                summary["abstracts"] += 1
            elif status == Outcome.METADATA_ONLY:
                summary["metadata_only"] += 1
            elif status == Outcome.DUPLICATE:
                summary["duplicates"] += 1
            elif status == Outcome.IGNORED:
                summary["ignored"] += 1
            elif status == Outcome.REVIEW:
                summary["review"] += 1
            if result.review_reason and status not in {Outcome.REVIEW, Outcome.IGNORED}:
                summary["review"] += 1
            self._progress(progress, index, len(occurrences), occurrence.original_url, status.value)

        self.store.finish_import(import_id, summary)
        self.store.export_csvs()
        return summary

    def _store_result(
        self,
        import_id: int,
        occurrence: LinkOccurrence,
        result: ResolutionResult,
    ) -> Outcome:
        if result.outcome == Outcome.IGNORED:
            return Outcome.IGNORED

        if result.paper is None:
            self.store.upsert_occurrence(
                import_id,
                occurrence,
                paper_id=None,
                final_url=result.final_url or occurrence.original_url,
                result_status=result.outcome.value,
                review_reason=result.review_reason,
            )
            return result.outcome

        paper_id, _, existing_artifact, existing_status = self.store.upsert_paper(result.paper)
        status = result.outcome
        already_best = existing_artifact and (
            existing_status == Outcome.DOWNLOADED_PDF.value
            or existing_status == Outcome.SAVED_FULLTEXT.value and status != Outcome.DOWNLOADED_PDF
        )
        if already_best:
            status = Outcome.DUPLICATE
        elif result.artifact_extension:
            self.store.save_artifact(
                paper_id,
                result.paper,
                result.artifact_extension,
                content_bytes=result.artifact_bytes,
                content_text=result.artifact_text,
            )

        self.store.upsert_occurrence(
            import_id,
            occurrence,
            paper_id=paper_id,
            final_url=result.final_url or occurrence.original_url,
            result_status=status.value,
            review_reason=result.review_reason,
        )
        return status

    @staticmethod
    def _progress(
        callback: ProgressCallback | None,
        index: int,
        total: int,
        url: str,
        status: str,
    ) -> None:
        if callback:
            callback(index, total, url, status)
