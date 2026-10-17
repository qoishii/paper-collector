from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol


@dataclass(frozen=True)
class ChatMessage:
    timestamp_raw: str
    timestamp: datetime | None
    sender: str
    text: str


@dataclass(frozen=True)
class LinkOccurrence:
    original_url: str
    normalized_url: str
    message: ChatMessage


@dataclass
class PaperRecord:
    canonical_key: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    publication_year: int | None = None
    publication_date: str = ""
    venue: str = ""
    work_type: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    arxiv_id: str = ""
    openalex_id: str = ""
    landing_url: str = ""
    oa_url: str = ""
    selected_version: str = ""
    license: str = ""
    abstract: str = ""
    access_status: str = ""
    artifact_path: str = ""
    content_hash: str = ""
    needs_review: bool = False
    metadata_source: str = ""


class Outcome(str, Enum):
    DOWNLOADED_PDF = "downloaded_pdf"
    SAVED_FULLTEXT = "saved_fulltext"
    SAVED_ABSTRACT = "saved_abstract"
    METADATA_ONLY = "metadata_only"
    REVIEW = "review"
    IGNORED = "ignored"
    FAILED = "failed"
    DUPLICATE = "duplicate"


@dataclass
class ResolutionResult:
    outcome: Outcome
    paper: PaperRecord | None = None
    final_url: str = ""
    artifact_bytes: bytes | None = None
    artifact_text: str = ""
    artifact_extension: str = ""
    review_reason: str = ""


class Resolver(Protocol):
    def resolve(self, occurrence: LinkOccurrence) -> ResolutionResult:
        """Resolve one URL into a paper artifact or a review outcome."""

