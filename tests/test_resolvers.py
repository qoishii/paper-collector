from datetime import datetime

import httpx

from whatsapp_paper_collector.models import ChatMessage, LinkOccurrence, Outcome
from whatsapp_paper_collector.resolvers import ScholarlyResolver
from whatsapp_paper_collector.urls import normalize_url
from whatsapp_paper_collector.web import WebClient


def _occurrence(url: str) -> LinkOccurrence:
    return LinkOccurrence(
        original_url=url,
        normalized_url=normalize_url(url),
        message=ChatMessage(
            timestamp_raw="4/7/26, 9:00:00 AM",
            timestamp=datetime(2026, 7, 4, 9, 0, 0),
            sender="Scientist",
            text=url,
        ),
    )


def _openalex_work(*, locations=None, abstract=True, work_type="article") -> dict:
    inverted = {"A": [0], "useful": [1], "abstract": [2]} if abstract else None
    return {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1000/demo",
        "display_name": "Single Cell Atlas",
        "publication_year": 2026,
        "publication_date": "2026-07-01",
        "type": work_type,
        "authorships": [{"author": {"display_name": "Alice Smith"}}],
        "primary_location": {
            "landing_page_url": "https://publisher.test/article/10.1000/demo",
            "source": {"display_name": "Test Journal"},
        },
        "abstract_inverted_index": inverted,
        "locations": locations or [],
        "best_oa_location": (locations or [None])[0],
    }


def _resolver(handler) -> tuple[ScholarlyResolver, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    web = WebClient(client=client, retries=0, retry_delay=0)
    return ScholarlyResolver(web), client


def test_openalex_oa_pdf_is_downloaded() -> None:
    location = {
        "is_oa": True,
        "pdf_url": "https://repository.test/paper.pdf",
        "landing_page_url": "https://repository.test/item",
        "version": "acceptedVersion",
        "license": "cc-by",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json=_openalex_work(locations=[location]))
        if request.url.host == "publisher.test":
            return httpx.Response(200, text="<html><meta name='citation_doi' content='10.1000/demo'></html>")
        if request.url.host == "repository.test":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7 paper")
        raise AssertionError(f"Unexpected request: {request.url}")

    resolver, client = _resolver(handler)
    try:
        result = resolver.resolve(_occurrence("https://publisher.test/article/10.1000/demo"))
    finally:
        client.close()

    assert result.outcome == Outcome.DOWNLOADED_PDF
    assert result.artifact_bytes == b"%PDF-1.7 paper"
    assert result.paper is not None
    assert result.paper.title == "Single Cell Atlas"
    assert result.paper.authors == ["Alice Smith"]
    assert result.paper.selected_version == "acceptedVersion"
    assert result.paper.license == "cc-by"


def test_open_html_fulltext_is_saved_as_text() -> None:
    location = {
        "is_oa": True,
        "pdf_url": None,
        "landing_page_url": "https://repository.test/fulltext",
        "version": "publishedVersion",
        "license": "cc-by",
    }
    paragraphs = "".join(
        f"<p>Section {number}. " + ("Detailed scientific findings and methods. " * 15) + "</p>"
        for number in range(8)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json=_openalex_work(locations=[location]))
        if request.url.host == "publisher.test":
            return httpx.Response(200, text="<html><meta name='citation_doi' content='10.1000/demo'></html>")
        if request.url.host == "repository.test":
            return httpx.Response(200, text=f"<html><body><article><h1>Paper</h1>{paragraphs}</article></body></html>")
        raise AssertionError(f"Unexpected request: {request.url}")

    resolver, client = _resolver(handler)
    try:
        result = resolver.resolve(_occurrence("https://publisher.test/article/10.1000/demo"))
    finally:
        client.close()

    assert result.outcome == Outcome.SAVED_FULLTEXT
    assert result.artifact_extension == "txt"
    assert "Full text" in result.artifact_text
    assert "Detailed scientific findings" in result.artifact_text


def test_paywalled_page_saves_abstract() -> None:
    page = """
    <html><head>
      <meta name="citation_title" content="A Paywalled Study">
      <meta name="citation_author" content="Jamie Jones">
      <meta name="citation_doi" content="10.1000/demo">
      <meta name="citation_abstract" content="This is the accessible abstract.">
    </head><body><p>Purchase access to read the article.</p></body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "publisher.test":
            return httpx.Response(200, text=page)
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json=_openalex_work(abstract=False))
        raise AssertionError(f"Unexpected request: {request.url}")

    resolver, client = _resolver(handler)
    try:
        result = resolver.resolve(_occurrence("https://publisher.test/article/paywalled"))
    finally:
        client.close()

    assert result.outcome == Outcome.SAVED_ABSTRACT
    assert "This is the accessible abstract." in result.artifact_text
    assert "Purchase access" not in result.artifact_text


def test_pubmed_uses_europe_pmc_open_pdf() -> None:
    europe_result = {
        "resultList": {
            "result": [
                {
                    "title": "Biomedical Discovery",
                    "authorString": "Lee K, Patel R",
                    "authorList": {
                        "author": [
                            {"lastName": "Lee", "firstName": "Kai"},
                            {"lastName": "Patel", "firstName": "Ravi"},
                        ]
                    },
                    "pubYear": "2024",
                    "journalTitle": "Biomedical Journal",
                    "abstractText": "An abstract.",
                    "pmid": "12345678",
                    "pmcid": "PMC999",
                    "doi": "10.1000/biomed",
                    "isOpenAccess": "Y",
                    "fullTextUrlList": {
                        "fullTextUrl": [
                            {
                                "url": "https://europe.test/paper.pdf",
                                "documentStyle": "pdf",
                                "availability": "Open access",
                            }
                        ]
                    },
                }
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.ebi.ac.uk":
            return httpx.Response(200, json=europe_result)
        if request.url.host == "pubmed.ncbi.nlm.nih.gov":
            return httpx.Response(200, text="<html><title>PubMed</title></html>")
        if request.url.host == "europe.test":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-biomed")
        raise AssertionError(f"Unexpected request: {request.url}")

    resolver, client = _resolver(handler)
    try:
        result = resolver.resolve(_occurrence("https://pubmed.ncbi.nlm.nih.gov/12345678/"))
    finally:
        client.close()

    assert result.outcome == Outcome.DOWNLOADED_PDF
    assert result.paper is not None
    assert result.paper.pmid == "12345678"
    assert result.paper.pmcid == "PMC999"
    assert result.paper.authors[0] == "Lee, Kai"


def test_direct_unknown_pdf_is_saved_and_flagged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-unknown")

    resolver, client = _resolver(handler)
    try:
        result = resolver.resolve(_occurrence("https://files.example.test/document.pdf"))
    finally:
        client.close()

    assert result.outcome == Outcome.DOWNLOADED_PDF
    assert result.paper is not None and result.paper.needs_review
    assert "metadata was incomplete" in result.review_reason


def test_blocked_or_unidentifiable_page_goes_to_review() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Blocked")

    resolver, client = _resolver(handler)
    try:
        result = resolver.resolve(_occurrence("https://unknown.example.test/resource"))
    finally:
        client.close()

    assert result.outcome == Outcome.REVIEW
    assert "HTTP 403" in result.review_reason


def test_crossref_title_match_is_conservative() -> None:
    page = """
    <html><head>
      <meta name="citation_title" content="An Exact Scholarly Title">
      <meta name="citation_author" content="Taylor Green">
      <meta name="citation_abstract" content="A preview.">
    </head></html>
    """
    crossref = {
        "message": {
            "items": [
                {
                    "DOI": "10.1000/matched",
                    "title": ["An Exact Scholarly Title"],
                    "author": [{"given": "Taylor", "family": "Green"}],
                    "published": {"date-parts": [[2023]]},
                    "type": "journal-article",
                    "container-title": ["Journal"],
                    "abstract": "A preview.",
                    "URL": "https://doi.org/10.1000/matched",
                }
            ]
        }
    }
    openalex = _openalex_work(abstract=True)
    openalex["doi"] = "https://doi.org/10.1000/matched"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "landing.example.test":
            return httpx.Response(200, text=page)
        if request.url.host == "api.crossref.org":
            return httpx.Response(200, json=crossref)
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json=openalex)
        raise AssertionError(f"Unexpected request: {request.url}")

    resolver, client = _resolver(handler)
    try:
        result = resolver.resolve(_occurrence("https://landing.example.test/paper"))
    finally:
        client.close()

    assert result.outcome == Outcome.SAVED_ABSTRACT
    assert result.paper is not None
    assert result.paper.doi == "10.1000/matched"
