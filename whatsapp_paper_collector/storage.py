from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import LinkOccurrence, PaperRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def occurrence_key(occurrence: LinkOccurrence) -> str:
    message = occurrence.message
    value = "\0".join(
        (
            message.timestamp_raw,
            message.sender,
            message.text,
            occurrence.original_url,
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_component(value: str, fallback: str, *, lowercase: bool = False) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    value = re.sub(r"[/\\:*?\"<>|\x00-\x1f]", "", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._-")
    if lowercase:
        value = value.lower()
    return value or fallback


def first_author_surname(authors: list[str]) -> str:
    if not authors:
        return "UnknownAuthor"
    author = authors[0].strip()
    if not author:
        return "UnknownAuthor"
    if "," in author:
        surname = author.split(",", 1)[0]
    else:
        parts = author.split()
        while len(parts) > 1 and parts[-1].rstrip(".").lower() in {"jr", "sr", "ii", "iii", "iv"}:
            parts.pop()
        surname = parts[-1] if parts else ""
    return _safe_component(surname, "UnknownAuthor")


def short_title(title: str, max_words: int = 8, max_length: int = 80) -> str:
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFC", title), flags=re.UNICODE)
    result = "_".join(words[:max_words]).lower()[:max_length].rstrip("_")
    return _safe_component(result, "untitled", lowercase=True)


def artifact_filename(paper_id: int, paper: PaperRecord, extension: str) -> str:
    author = first_author_surname(paper.authors)
    year = str(paper.publication_year) if paper.publication_year else "UnknownYear"
    title = short_title(paper.title)
    extension = extension.lstrip(".").lower()
    return f"{paper_id:06d}-{author}-{year}-{title}.{extension}"


class LibraryStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.files_dir = self.root / "files"
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.root / "library.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "LibraryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                summary_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS papers (
                id INTEGER PRIMARY KEY,
                canonical_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                authors_json TEXT NOT NULL DEFAULT '[]',
                publication_year INTEGER,
                publication_date TEXT NOT NULL DEFAULT '',
                venue TEXT NOT NULL DEFAULT '',
                work_type TEXT NOT NULL DEFAULT '',
                doi TEXT NOT NULL DEFAULT '',
                pmid TEXT NOT NULL DEFAULT '',
                pmcid TEXT NOT NULL DEFAULT '',
                arxiv_id TEXT NOT NULL DEFAULT '',
                openalex_id TEXT NOT NULL DEFAULT '',
                landing_url TEXT NOT NULL DEFAULT '',
                oa_url TEXT NOT NULL DEFAULT '',
                selected_version TEXT NOT NULL DEFAULT '',
                license TEXT NOT NULL DEFAULT '',
                abstract TEXT NOT NULL DEFAULT '',
                access_status TEXT NOT NULL DEFAULT '',
                artifact_path TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                needs_review INTEGER NOT NULL DEFAULT 0,
                metadata_source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS link_occurrences (
                id INTEGER PRIMARY KEY,
                occurrence_key TEXT NOT NULL UNIQUE,
                import_id INTEGER NOT NULL REFERENCES imports(id),
                paper_id INTEGER REFERENCES papers(id),
                timestamp_raw TEXT NOT NULL DEFAULT '',
                timestamp_iso TEXT NOT NULL DEFAULT '',
                sender TEXT NOT NULL DEFAULT '',
                message_text TEXT NOT NULL DEFAULT '',
                original_url TEXT NOT NULL,
                normalized_url TEXT NOT NULL,
                final_url TEXT NOT NULL DEFAULT '',
                result_status TEXT NOT NULL,
                review_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE UNIQUE INDEX IF NOT EXISTS papers_doi_unique
                ON papers(doi) WHERE doi != '';
            CREATE UNIQUE INDEX IF NOT EXISTS papers_openalex_unique
                ON papers(openalex_id) WHERE openalex_id != '';
            CREATE UNIQUE INDEX IF NOT EXISTS papers_pmid_unique
                ON papers(pmid) WHERE pmid != '';
            CREATE UNIQUE INDEX IF NOT EXISTS papers_pmcid_unique
                ON papers(pmcid) WHERE pmcid != '';
            CREATE UNIQUE INDEX IF NOT EXISTS papers_arxiv_unique
                ON papers(arxiv_id) WHERE arxiv_id != '';
            CREATE UNIQUE INDEX IF NOT EXISTS papers_content_hash_unique
                ON papers(content_hash) WHERE content_hash != '';
            CREATE INDEX IF NOT EXISTS occurrences_url_index
                ON link_occurrences(normalized_url);
            """
        )
        self.connection.commit()

    def start_import(self, source_path: Path, source_hash: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO imports(source_path, source_hash, started_at) VALUES (?, ?, ?)",
            (str(source_path.resolve()), source_hash, _now()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_import(self, import_id: int, summary: dict[str, int]) -> None:
        self.connection.execute(
            "UPDATE imports SET finished_at = ?, summary_json = ? WHERE id = ?",
            (_now(), json.dumps(summary, sort_keys=True), import_id),
        )
        self.connection.commit()

    def occurrence_status(self, occurrence: LinkOccurrence) -> str:
        row = self.connection.execute(
            "SELECT result_status FROM link_occurrences WHERE occurrence_key = ?",
            (occurrence_key(occurrence),),
        ).fetchone()
        return str(row["result_status"]) if row else ""

    def complete_paper_for_url(self, normalized_url: str) -> sqlite3.Row | None:
        rows = self.connection.execute(
            """
            SELECT p.* FROM link_occurrences o
            JOIN papers p ON p.id = o.paper_id
            WHERE o.normalized_url = ?
              AND p.access_status IN ('downloaded_pdf', 'saved_fulltext')
              AND p.artifact_path != ''
            ORDER BY p.id LIMIT 1
            """,
            (normalized_url,),
        ).fetchone()
        if rows and (self.root / rows["artifact_path"]).is_file():
            return rows
        return None

    def _find_paper(self, paper: PaperRecord) -> sqlite3.Row | None:
        identifiers = {
            "openalex_id": paper.openalex_id,
            "doi": paper.doi,
            "pmid": paper.pmid,
            "pmcid": paper.pmcid,
            "arxiv_id": paper.arxiv_id,
            "content_hash": paper.content_hash,
            "canonical_key": paper.canonical_key,
        }
        clauses: list[str] = []
        values: list[str] = []
        for field, value in identifiers.items():
            if value:
                clauses.append(f"{field} = ?")
                values.append(value)
        if not clauses:
            return None
        return self.connection.execute(
            f"SELECT * FROM papers WHERE {' OR '.join(clauses)} ORDER BY id LIMIT 1",
            values,
        ).fetchone()

    def upsert_paper(self, paper: PaperRecord) -> tuple[int, bool, str, str]:
        existing = self._find_paper(paper)
        values = asdict(paper)
        values["authors_json"] = json.dumps(paper.authors, ensure_ascii=False)
        values["needs_review"] = int(paper.needs_review)
        values["updated_at"] = _now()

        if existing is None:
            values["created_at"] = values["updated_at"]
            columns = [
                "canonical_key", "title", "authors_json", "publication_year",
                "publication_date", "venue", "work_type", "doi", "pmid", "pmcid",
                "arxiv_id", "openalex_id", "landing_url", "oa_url",
                "selected_version", "license", "abstract", "access_status",
                "artifact_path", "content_hash", "needs_review", "metadata_source",
                "created_at", "updated_at",
            ]
            cursor = self.connection.execute(
                f"INSERT INTO papers ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )
            self.connection.commit()
            return int(cursor.lastrowid), True, "", ""

        merged: dict[str, Any] = dict(existing)
        for field in (
            "title", "publication_year", "publication_date", "venue", "work_type",
            "doi", "pmid", "pmcid", "arxiv_id", "openalex_id", "landing_url",
            "oa_url", "selected_version", "license", "abstract", "content_hash",
            "metadata_source",
        ):
            if values.get(field):
                merged[field] = values[field]
        if paper.authors:
            merged["authors_json"] = values["authors_json"]
        merged["needs_review"] = values["needs_review"]
        merged["updated_at"] = values["updated_at"]
        self.connection.execute(
            """
            UPDATE papers SET
                title = ?, authors_json = ?, publication_year = ?, publication_date = ?,
                venue = ?, work_type = ?, doi = ?, pmid = ?, pmcid = ?, arxiv_id = ?,
                openalex_id = ?, landing_url = ?, oa_url = ?, selected_version = ?,
                license = ?, abstract = ?, content_hash = ?, needs_review = ?,
                metadata_source = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                merged["title"], merged["authors_json"], merged["publication_year"],
                merged["publication_date"], merged["venue"], merged["work_type"],
                merged["doi"], merged["pmid"], merged["pmcid"], merged["arxiv_id"],
                merged["openalex_id"], merged["landing_url"], merged["oa_url"],
                merged["selected_version"], merged["license"], merged["abstract"],
                merged["content_hash"], merged["needs_review"], merged["metadata_source"],
                merged["updated_at"], existing["id"],
            ),
        )
        self.connection.commit()
        return int(existing["id"]), False, str(existing["artifact_path"]), str(existing["access_status"])

    def save_artifact(
        self,
        paper_id: int,
        paper: PaperRecord,
        extension: str,
        *,
        content_bytes: bytes | None = None,
        content_text: str = "",
    ) -> str:
        filename = artifact_filename(paper_id, paper, extension)
        destination = self.files_dir / filename
        temporary = destination.with_suffix(destination.suffix + ".part")
        if content_bytes is not None:
            temporary.write_bytes(content_bytes)
        else:
            temporary.write_text(content_text, encoding="utf-8")
        temporary.replace(destination)
        relative_path = str(destination.relative_to(self.root))
        self.connection.execute(
            "UPDATE papers SET artifact_path = ?, access_status = ?, updated_at = ? WHERE id = ?",
            (relative_path, paper.access_status, _now(), paper_id),
        )
        self.connection.commit()
        return relative_path

    def upsert_occurrence(
        self,
        import_id: int,
        occurrence: LinkOccurrence,
        *,
        paper_id: int | None,
        final_url: str,
        result_status: str,
        review_reason: str = "",
    ) -> None:
        message = occurrence.message
        timestamp_iso = message.timestamp.isoformat() if message.timestamp else ""
        now = _now()
        self.connection.execute(
            """
            INSERT INTO link_occurrences (
                occurrence_key, import_id, paper_id, timestamp_raw, timestamp_iso,
                sender, message_text, original_url, normalized_url, final_url,
                result_status, review_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(occurrence_key) DO UPDATE SET
                import_id = excluded.import_id,
                paper_id = excluded.paper_id,
                final_url = excluded.final_url,
                result_status = excluded.result_status,
                review_reason = excluded.review_reason,
                updated_at = excluded.updated_at
            """,
            (
                occurrence_key(occurrence), import_id, paper_id, message.timestamp_raw,
                timestamp_iso, message.sender, message.text, occurrence.original_url,
                occurrence.normalized_url, final_url, result_status, review_reason,
                now, now,
            ),
        )
        self.connection.commit()

    def export_csvs(self) -> None:
        self._write_query_csv(
            self.root / "papers.csv",
            """
            SELECT p.id, p.title, p.authors_json, p.publication_year, p.venue,
                   p.work_type, p.doi, p.pmid, p.pmcid, p.arxiv_id, p.openalex_id,
                   p.access_status, p.selected_version, p.license, p.landing_url,
                   p.oa_url, p.artifact_path, p.needs_review,
                   COUNT(o.id) AS occurrence_count, MIN(o.timestamp_iso) AS first_seen
            FROM papers p
            LEFT JOIN link_occurrences o ON o.paper_id = p.id
            GROUP BY p.id ORDER BY p.id
            """,
            transform=self._paper_csv_row,
        )
        self._write_query_csv(
            self.root / "occurrences.csv",
            """
            SELECT o.paper_id, o.timestamp_raw, o.timestamp_iso, o.sender,
                   o.message_text, o.original_url, o.final_url, o.result_status
            FROM link_occurrences o
            WHERE o.paper_id IS NOT NULL
            ORDER BY o.timestamp_iso, o.id
            """,
        )
        self._write_query_csv(
            self.root / "review.csv",
            """
            SELECT o.paper_id, o.timestamp_raw, o.sender, o.original_url,
                   o.final_url, o.result_status, o.review_reason
            FROM link_occurrences o
            LEFT JOIN papers p ON p.id = o.paper_id
            WHERE o.review_reason != '' OR COALESCE(p.needs_review, 0) = 1
            ORDER BY o.id
            """,
        )

    @staticmethod
    def _paper_csv_row(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["authors"] = "; ".join(json.loads(result.pop("authors_json") or "[]"))
        return result

    def _write_query_csv(
        self,
        path: Path,
        query: str,
        *,
        transform: Any | None = None,
    ) -> None:
        cursor = self.connection.execute(query)
        fieldnames = [description[0] for description in cursor.description]
        if transform and "authors_json" in fieldnames:
            fieldnames[fieldnames.index("authors_json")] = "authors"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for sqlite_row in cursor:
                row = dict(sqlite_row)
                writer.writerow(transform(row) if transform else row)

