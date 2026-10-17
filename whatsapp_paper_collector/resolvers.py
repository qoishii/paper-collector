from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from trafilatura import extract as extract_readable_text

from .models import LinkOccurrence, Outcome, PaperRecord, ResolutionResult
from .urls import (
    extract_arxiv_id,
    extract_doi,
    extract_pubmed_ids,
    is_meeting_url,
    normalize_url,
)
from .web import WebClient, WebError, WebResponse


@dataclass(frozen=True)
class ContentCandidate:
    url: str
    version: str = ""
    license: str = ""
    is_pdf: bool = False


_VERSION_RANK = {
    "publishedversion": 30,
    "acceptedversion": 20,
    "submittedversion": 10,
}
_ALLOWED_TYPES = {
    "article",
    "journal-article",
    "proceedings-article",
    "review",
    "review-article",
    "preprint",
    "posted-content",
}
_EXCLUDED_TYPES = {
    "book",
    "book-chapter",
    "dataset",
    "dissertation",
    "editorial",
    "grant",
    "other",
    "paratext",
    "reference-entry",
    "report",
    "standard",
}


def _clean_markup(value: Any) -> str:
    if value is None:
        return ""
    text = BeautifulSoup(html.unescape(str(value)), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _year(value: Any) -> int | None:
    match = re.search(r"\b(18|19|20|21)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _canonical_key(paper: PaperRecord, fallback_url: str) -> str:
    if paper.openalex_id:
        return f"openalex:{paper.openalex_id.rsplit('/', 1)[-1].lower()}"
    if paper.doi:
        return f"doi:{paper.doi.lower()}"
    if paper.pmcid:
        return f"pmcid:{paper.pmcid.lower()}"
    if paper.pmid:
        return f"pmid:{paper.pmid}"
    if paper.arxiv_id:
        return f"arxiv:{paper.arxiv_id.lower()}"
    if paper.content_hash:
        return f"sha256:{paper.content_hash}"
    return f"url:{normalize_url(fallback_url)}"


def _merge_paper(target: PaperRecord, source: PaperRecord) -> None:
    for field in (
        "title", "publication_date", "venue", "work_type", "doi", "pmid", "pmcid",
        "arxiv_id", "openalex_id", "landing_url", "oa_url", "selected_version",
        "license", "metadata_source",
    ):
        if not getattr(target, field) and getattr(source, field):
            setattr(target, field, getattr(source, field))
    if not target.authors and source.authors:
        target.authors = source.authors
    if target.publication_year is None and source.publication_year is not None:
        target.publication_year = source.publication_year
    if len(source.abstract) > len(target.abstract):
        target.abstract = source.abstract


def _candidate_sort_key(candidate: ContentCandidate) -> tuple[int, int]:
    version = candidate.version.replace("_", "").replace("-", "").lower()
    return (_VERSION_RANK.get(version, 0), int(candidate.is_pdf))


def _deduplicate_candidates(candidates: Iterable[ContentCandidate]) -> list[ContentCandidate]:
    by_url: dict[str, ContentCandidate] = {}
    for candidate in candidates:
        if not candidate.url:
            continue
        key = normalize_url(candidate.url)
        existing = by_url.get(key)
        if existing is None or _candidate_sort_key(candidate) > _candidate_sort_key(existing):
            by_url[key] = candidate
    return sorted(by_url.values(), key=_candidate_sort_key, reverse=True)


def _abstract_from_inverted_index(index: Any) -> str:
    if not isinstance(index, dict) or not index:
        return ""
    positions: dict[int, str] = {}
    for word, word_positions in index.items():
        if not isinstance(word_positions, list):
            continue
        for position in word_positions:
            if isinstance(position, int):
                positions[position] = str(word)
    return " ".join(positions[position] for position in sorted(positions))


def _json_ld_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _json_ld_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_ld_nodes(child)


def _json_ld_authors(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        family = str(value.get("familyName") or "")
        given = str(value.get("givenName") or "")
        name = value.get("name") or (f"{family}, {given}" if family and given else family or given)
        return [_clean_markup(name)] if name else []
    if isinstance(value, list):
        result: list[str] = []
        for author in value:
            result.extend(_json_ld_authors(author))
        return [author for author in result if author]
    return []


def parse_scholarly_html(html_text: str, base_url: str) -> tuple[PaperRecord, list[ContentCandidate], bool]:
    soup = BeautifulSoup(html_text, "html.parser")
    meta: dict[str, list[str]] = {}
    for tag in soup.find_all("meta"):
        key = str(tag.get("name") or tag.get("property") or "").lower()
        content = str(tag.get("content") or "").strip()
        if key and content:
            meta.setdefault(key, []).append(content)

    def first(*keys: str) -> str:
        for key in keys:
            values = meta.get(key.lower(), [])
            if values:
                return _clean_markup(values[0])
        return ""

    title = first("citation_title", "dc.title", "og:title")
    authors = [_clean_markup(value) for value in meta.get("citation_author", []) if _clean_markup(value)]
    date = first("citation_publication_date", "citation_date", "dc.date", "article:published_time")
    venue = first("citation_journal_title", "citation_conference_title")
    abstract = first("citation_abstract", "dc.description", "description", "og:description")
    doi = extract_doi(first("citation_doi", "dc.identifier", "dc.identifier.doi"))
    work_type = "article" if title and (authors or doi or meta.get("citation_pdf_url")) else ""
    candidates = [
        ContentCandidate(urljoin(base_url, value), "publishedVersion", "", True)
        for value in meta.get("citation_pdf_url", [])
        if value
    ]

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text() or "null")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _json_ld_nodes(data):
            node_type = node.get("@type", "")
            types = {str(item).lower() for item in (node_type if isinstance(node_type, list) else [node_type])}
            if not types.intersection({"article", "scholarlyarticle", "medicalscholarlyarticle"}):
                continue
            title = title or _clean_markup(node.get("headline") or node.get("name"))
            authors = authors or _json_ld_authors(node.get("author"))
            date = date or _clean_markup(node.get("datePublished"))
            abstract = abstract or _clean_markup(node.get("abstract") or node.get("description"))
            doi = doi or extract_doi(json.dumps(node.get("identifier", "")))
            work_type = work_type or "article"
            encoding = node.get("encoding") or node.get("associatedMedia")
            for media in encoding if isinstance(encoding, list) else [encoding]:
                if isinstance(media, dict):
                    media_url = media.get("contentUrl") or media.get("url")
                    media_type = str(media.get("encodingFormat", "")).lower()
                    if media_url and ("pdf" in media_type or str(media_url).lower().endswith(".pdf")):
                        candidates.append(ContentCandidate(urljoin(base_url, str(media_url)), "publishedVersion", "", True))

    if not abstract:
        abstract_node = soup.select_one("section#abstract, section.abstract, div.abstract, [class*='abstract-content']")
        if abstract_node:
            abstract = _clean_markup(abstract_node.get_text(" ", strip=True))

    paper = PaperRecord(
        canonical_key=f"url:{normalize_url(base_url)}",
        title=title,
        authors=authors,
        publication_year=_year(date),
        publication_date=date,
        venue=venue,
        work_type=work_type,
        doi=doi,
        landing_url=base_url,
        abstract=abstract,
        metadata_source="webpage" if title or doi else "",
    )
    is_scholarly = bool(doi or (title and authors and work_type))
    return paper, candidates, is_scholarly


def format_text_artifact(paper: PaperRecord, body: str, heading: str) -> str:
    fields = [
        ("Title", paper.title or "Unknown"),
        ("Authors", "; ".join(paper.authors) or "Unknown"),
        ("Year", str(paper.publication_year) if paper.publication_year else "Unknown"),
        ("Venue", paper.venue or "Unknown"),
        ("DOI", paper.doi or "Unavailable"),
        ("Source URL", paper.landing_url or paper.oa_url or "Unavailable"),
        ("Access", paper.access_status or "metadata_only"),
        ("Version", paper.selected_version or "Unknown"),
        ("License", paper.license or "Unknown"),
    ]
    header = "\n".join(f"{label}: {value}" for label, value in fields)
    content = body.strip() or "Unavailable."
    return f"{header}\n\n{heading}\n{'=' * len(heading)}\n{content}\n"


class ScholarlyResolver:
    def __init__(self, web: WebClient):
        self.web = web

    def resolve(self, occurrence: LinkOccurrence) -> ResolutionResult:
        original_url = occurrence.original_url
        normalized_url = occurrence.normalized_url
        doi = extract_doi(original_url)
        arxiv_id = extract_arxiv_id(original_url)
        pmid, pmcid = extract_pubmed_ids(original_url)
        paper = PaperRecord(
            canonical_key=f"url:{normalized_url}",
            doi=doi,
            arxiv_id=arxiv_id,
            pmid=pmid,
            pmcid=pmcid,
            landing_url=original_url,
        )
        candidates: list[ContentCandidate] = []
        provider_errors: list[str] = []

        self._enrich_known_identifiers(paper, candidates, provider_errors)

        page_response: WebResponse | None = None
        page_error = ""
        try:
            page_response = self.web.get(normalized_url)
        except WebError as error:
            page_error = str(error)

        if page_response and is_meeting_url(page_response.url):
            return ResolutionResult(outcome=Outcome.IGNORED, final_url=page_response.url)

        if page_response and page_response.is_pdf:
            paper.content_hash = hashlib.sha256(page_response.content).hexdigest()
            paper.canonical_key = _canonical_key(paper, normalized_url)
            paper.access_status = Outcome.DOWNLOADED_PDF.value
            paper.oa_url = page_response.url
            paper.selected_version = paper.selected_version or "unknown"
            if not paper.title:
                paper.needs_review = True
            return ResolutionResult(
                outcome=Outcome.DOWNLOADED_PDF,
                paper=paper,
                final_url=page_response.url,
                artifact_bytes=page_response.content,
                artifact_extension="pdf",
                review_reason="Direct PDF saved, but scholarly metadata was incomplete." if paper.needs_review else "",
            )

        page_is_scholarly = False
        if page_response:
            page_paper, page_candidates, page_is_scholarly = parse_scholarly_html(
                page_response.text, page_response.url
            )
            had_doi = bool(paper.doi)
            _merge_paper(paper, page_paper)
            candidates.extend(page_candidates)
            if paper.doi and not had_doi:
                self._enrich_known_identifiers(paper, candidates, provider_errors)

        if paper.work_type and self._excluded_type(paper.work_type):
            return ResolutionResult(
                outcome=Outcome.IGNORED,
                final_url=page_response.url if page_response else original_url,
                review_reason=f"Excluded scholarly work type: {paper.work_type}",
            )

        if page_response and page_is_scholarly and not (
            paper.doi or paper.pmid or paper.pmcid or paper.arxiv_id or paper.openalex_id
        ):
            title = paper.title or self._html_title(page_response.text)
            if title:
                match, crossref_candidates = self._crossref_title_match(title)
                if match:
                    _merge_paper(paper, match)
                    candidates.extend(crossref_candidates)

        identified = bool(
            paper.doi or paper.pmid or paper.pmcid or paper.arxiv_id or paper.openalex_id or page_is_scholarly
        )
        if not identified:
            reason = page_error or "The webpage did not contain confident scholarly metadata."
            return ResolutionResult(
                outcome=Outcome.REVIEW,
                final_url=page_response.url if page_response else original_url,
                review_reason=reason,
            )

        paper.canonical_key = _canonical_key(paper, normalized_url)
        artifact_error = ""
        for candidate in _deduplicate_candidates(candidates):
            try:
                response = self.web.get(candidate.url)
            except WebError as error:
                artifact_error = str(error)
                continue
            if response.is_pdf:
                paper.content_hash = hashlib.sha256(response.content).hexdigest()
                paper.access_status = Outcome.DOWNLOADED_PDF.value
                paper.oa_url = response.url
                paper.selected_version = candidate.version or paper.selected_version or "unknown"
                paper.license = candidate.license or paper.license
                return ResolutionResult(
                    outcome=Outcome.DOWNLOADED_PDF,
                    paper=paper,
                    final_url=response.url,
                    artifact_bytes=response.content,
                    artifact_extension="pdf",
                )
            if not candidate.is_pdf:
                fulltext = self._extract_fulltext(response.text)
                if fulltext:
                    paper.access_status = Outcome.SAVED_FULLTEXT.value
                    paper.oa_url = response.url
                    paper.selected_version = candidate.version or paper.selected_version or "unknown"
                    paper.license = candidate.license or paper.license
                    return ResolutionResult(
                        outcome=Outcome.SAVED_FULLTEXT,
                        paper=paper,
                        final_url=response.url,
                        artifact_text=format_text_artifact(paper, fulltext, "Full text"),
                        artifact_extension="txt",
                    )

        if page_response and self._is_known_oa_page(page_response.url, candidates):
            fulltext = self._extract_fulltext(page_response.text)
            if fulltext:
                paper.access_status = Outcome.SAVED_FULLTEXT.value
                paper.oa_url = page_response.url
                return ResolutionResult(
                    outcome=Outcome.SAVED_FULLTEXT,
                    paper=paper,
                    final_url=page_response.url,
                    artifact_text=format_text_artifact(paper, fulltext, "Full text"),
                    artifact_extension="txt",
                )

        if paper.abstract:
            paper.access_status = Outcome.SAVED_ABSTRACT.value
            if artifact_error:
                paper.needs_review = True
            return ResolutionResult(
                outcome=Outcome.SAVED_ABSTRACT,
                paper=paper,
                final_url=page_response.url if page_response else original_url,
                artifact_text=format_text_artifact(paper, paper.abstract, "Abstract or preview"),
                artifact_extension="txt",
                review_reason=f"Open full text could not be retrieved: {artifact_error}" if artifact_error else "",
            )

        paper.access_status = Outcome.METADATA_ONLY.value
        reason = ""
        if not paper.title and (page_error or provider_errors):
            paper.needs_review = True
            reason = page_error or provider_errors[0]
        return ResolutionResult(
            outcome=Outcome.METADATA_ONLY,
            paper=paper,
            final_url=page_response.url if page_response else original_url,
            artifact_text=format_text_artifact(paper, "", "Abstract or preview"),
            artifact_extension="txt",
            review_reason=reason,
        )

    def _enrich_known_identifiers(
        self,
        paper: PaperRecord,
        candidates: list[ContentCandidate],
        errors: list[str],
    ) -> None:
        if paper.doi and not paper.openalex_id:
            try:
                openalex, oa_candidates = self._openalex_by_doi(paper.doi)
                if openalex:
                    _merge_paper(paper, openalex)
                    candidates.extend(oa_candidates)
            except WebError as error:
                errors.append(str(error))

        if paper.pmid or paper.pmcid:
            try:
                europe, europe_candidates = self._europe_pmc(pmid=paper.pmid, pmcid=paper.pmcid)
                if europe:
                    _merge_paper(paper, europe)
                    candidates.extend(europe_candidates)
            except WebError as error:
                errors.append(str(error))
        elif paper.doi and not paper.abstract:
            try:
                europe, europe_candidates = self._europe_pmc(doi=paper.doi)
                if europe:
                    _merge_paper(paper, europe)
                    candidates.extend(europe_candidates)
            except WebError:
                pass

        if paper.doi and (not paper.title or not paper.abstract):
            try:
                crossref = self._crossref_by_doi(paper.doi)
                if crossref:
                    _merge_paper(paper, crossref)
            except WebError as error:
                errors.append(str(error))

    def _openalex_by_doi(self, doi: str) -> tuple[PaperRecord | None, list[ContentCandidate]]:
        url = f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='/()')}"
        response = self.web.get(url, accept="application/json")
        data = response.json()
        if not isinstance(data, dict) or not data.get("id"):
            return None, []
        authors = [
            str(authorship.get("author", {}).get("display_name", ""))
            for authorship in data.get("authorships", [])
            if authorship.get("author", {}).get("display_name")
        ]
        primary = data.get("primary_location") or {}
        source = primary.get("source") or {}
        paper = PaperRecord(
            canonical_key="",
            title=_clean_markup(data.get("display_name") or data.get("title")),
            authors=authors,
            publication_year=data.get("publication_year") if isinstance(data.get("publication_year"), int) else None,
            publication_date=str(data.get("publication_date") or ""),
            venue=_clean_markup(source.get("display_name")),
            work_type=str(data.get("type") or ""),
            doi=extract_doi(str(data.get("doi") or doi)),
            openalex_id=str(data.get("id") or ""),
            landing_url=str(primary.get("landing_page_url") or ""),
            abstract=_abstract_from_inverted_index(data.get("abstract_inverted_index")),
            metadata_source="OpenAlex",
        )
        candidates: list[ContentCandidate] = []
        for location in data.get("locations") or []:
            if not location or not location.get("is_oa"):
                continue
            pdf_url = str(location.get("pdf_url") or "")
            landing_url = str(location.get("landing_page_url") or "")
            common = {
                "version": str(location.get("version") or ""),
                "license": str(location.get("license") or ""),
            }
            if pdf_url:
                candidates.append(ContentCandidate(pdf_url, is_pdf=True, **common))
            elif landing_url:
                candidates.append(ContentCandidate(landing_url, is_pdf=False, **common))
        best = data.get("best_oa_location") or {}
        if best:
            paper.oa_url = str(best.get("pdf_url") or best.get("landing_page_url") or "")
            paper.selected_version = str(best.get("version") or "")
            paper.license = str(best.get("license") or "")
            if best.get("pdf_url"):
                candidates.append(
                    ContentCandidate(
                        str(best["pdf_url"]),
                        str(best.get("version") or ""),
                        str(best.get("license") or ""),
                        True,
                    )
                )
            elif best.get("landing_page_url"):
                candidates.append(
                    ContentCandidate(
                        str(best["landing_page_url"]),
                        str(best.get("version") or ""),
                        str(best.get("license") or ""),
                        False,
                    )
                )
        return paper, candidates

    def _europe_pmc(
        self,
        *,
        doi: str = "",
        pmid: str = "",
        pmcid: str = "",
    ) -> tuple[PaperRecord | None, list[ContentCandidate]]:
        if pmcid:
            query = f"PMCID:{pmcid}"
        elif pmid:
            query = f"EXT_ID:{pmid}"
        else:
            query = f'DOI:"{doi}"'
        url = (
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
            f"?query={quote(query)}&format=json&resultType=core&pageSize=1"
        )
        data = self.web.get(url, accept="application/json").json()
        results = data.get("resultList", {}).get("result", []) if isinstance(data, dict) else []
        if not results:
            return None, []
        result = results[0]
        authors = self._europe_pmc_authors(result)
        paper = PaperRecord(
            canonical_key="",
            title=_clean_markup(result.get("title")),
            authors=authors,
            publication_year=_year(result.get("pubYear") or result.get("firstPublicationDate")),
            publication_date=str(result.get("firstPublicationDate") or ""),
            venue=_clean_markup(result.get("journalTitle")),
            work_type="article",
            doi=extract_doi(str(result.get("doi") or doi)),
            pmid=str(result.get("pmid") or pmid),
            pmcid=str(result.get("pmcid") or pmcid).upper(),
            landing_url=str(result.get("journalInfo", {}).get("journal", {}).get("journalUrl") or ""),
            abstract=_clean_markup(result.get("abstractText")),
            metadata_source="Europe PMC",
        )
        candidates: list[ContentCandidate] = []
        if str(result.get("isOpenAccess", "")).upper() == "Y":
            url_list = result.get("fullTextUrlList", {}).get("fullTextUrl", [])
            for item in url_list:
                target = str(item.get("url") or "")
                style = str(item.get("documentStyle") or "").lower()
                if target:
                    candidates.append(
                        ContentCandidate(target, "publishedVersion", str(item.get("availability") or ""), style == "pdf")
                    )
        return paper, candidates

    def _crossref_by_doi(self, doi: str) -> PaperRecord | None:
        url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
        data = self.web.get(url, accept="application/json").json()
        message = data.get("message", {}) if isinstance(data, dict) else {}
        return self._paper_from_crossref(message) if message else None

    def _crossref_title_match(self, title: str) -> tuple[PaperRecord | None, list[ContentCandidate]]:
        url = (
            "https://api.crossref.org/works"
            f"?query.title={quote(title)}&rows=1&select=DOI,title,author,published,type,container-title,abstract,URL"
        )
        try:
            data = self.web.get(url, accept="application/json").json()
        except WebError:
            return None, []
        items = data.get("message", {}).get("items", []) if isinstance(data, dict) else []
        if not items:
            return None, []
        candidate_title = _clean_markup((items[0].get("title") or [""])[0])
        left = re.sub(r"\W+", " ", title).casefold().strip()
        right = re.sub(r"\W+", " ", candidate_title).casefold().strip()
        if not left or SequenceMatcher(None, left, right).ratio() < 0.92:
            return None, []
        paper = self._paper_from_crossref(items[0])
        if not paper or not paper.doi:
            return paper, []
        try:
            openalex, candidates = self._openalex_by_doi(paper.doi)
        except WebError:
            return paper, []
        if openalex:
            _merge_paper(paper, openalex)
        return paper, candidates

    @staticmethod
    def _paper_from_crossref(message: dict[str, Any]) -> PaperRecord:
        authors = []
        for author in message.get("author") or []:
            given = str(author.get("given") or "")
            family = str(author.get("family") or "")
            name = f"{family}, {given}" if family and given else family or given
            if name:
                authors.append(name)
        date_parts = (message.get("published") or {}).get("date-parts") or []
        year = date_parts[0][0] if date_parts and date_parts[0] else None
        title_values = message.get("title") or []
        venue_values = message.get("container-title") or []
        doi = extract_doi(str(message.get("DOI") or ""))
        return PaperRecord(
            canonical_key="",
            title=_clean_markup(title_values[0] if title_values else ""),
            authors=authors,
            publication_year=year if isinstance(year, int) else None,
            venue=_clean_markup(venue_values[0] if venue_values else ""),
            work_type=str(message.get("type") or ""),
            doi=doi,
            landing_url=str(message.get("URL") or ""),
            abstract=_clean_markup(message.get("abstract")),
            metadata_source="Crossref",
        )

    @staticmethod
    def _europe_pmc_authors(result: dict[str, Any]) -> list[str]:
        authors: list[str] = []
        author_list = result.get("authorList", {}).get("author", [])
        for author in author_list:
            family = str(author.get("lastName") or "")
            given = str(author.get("firstName") or author.get("initials") or "")
            name = f"{family}, {given}" if family and given else family or given or str(author.get("fullName") or "")
            if name:
                authors.append(name)
        if authors:
            return authors

        for value in str(result.get("authorString") or "").split(","):
            parts = value.strip().split()
            if len(parts) >= 2 and re.fullmatch(r"[A-Z.-]+", parts[-1]):
                authors.append(f"{' '.join(parts[:-1])}, {parts[-1]}")
            elif parts:
                authors.append(" ".join(parts))
        return authors

    @staticmethod
    def _excluded_type(work_type: str) -> bool:
        normalized = work_type.lower().strip()
        if normalized in _ALLOWED_TYPES or "article" in normalized or "preprint" in normalized:
            return False
        return normalized in _EXCLUDED_TYPES

    @staticmethod
    def _html_title(html_text: str) -> str:
        soup = BeautifulSoup(html_text, "html.parser")
        return _clean_markup(soup.title.string) if soup.title and soup.title.string else ""

    @staticmethod
    def _extract_fulltext(html_text: str) -> str:
        text = extract_readable_text(
            html_text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        ) or ""
        text = text.strip()
        return text if len(text) >= 1500 else ""

    @staticmethod
    def _is_known_oa_page(url: str, candidates: list[ContentCandidate]) -> bool:
        normalized = normalize_url(url)
        return any(not candidate.is_pdf and normalize_url(candidate.url) == normalized for candidate in candidates)
