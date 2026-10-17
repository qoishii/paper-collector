from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


_BLOCKED_MEETING_HOSTS = {
    "meet.google.com",
    "teams.live.com",
    "teams.microsoft.com",
    "webex.com",
    "www.webex.com",
    "zoom.com",
    "zoom.us",
}
_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "rss",
}
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)([a-z-]+(?:\.[a-z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
_PMCID_RE = re.compile(r"\b(PMC\d+)\b", re.IGNORECASE)


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"

    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key.startswith("utm_") or lower_key.startswith("__cf_chl_"):
            continue
        if lower_key in _TRACKING_KEYS:
            continue
        query.append((key, value))
    return urlunsplit((scheme, hostname, parts.path or "/", urlencode(query, doseq=True), ""))


def is_meeting_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in _BLOCKED_MEETING_HOSTS)


def extract_doi(value: str) -> str:
    decoded = unquote(value)
    match = _DOI_RE.search(decoded)
    if match:
        return match.group(0).rstrip(".,;:)]}").lower()
    parts = urlsplit(decoded)
    host = (parts.hostname or "").lower()
    if host == "nature.com" or host.endswith(".nature.com"):
        nature_match = re.search(r"/articles/(s[\w.-]+)", parts.path, re.IGNORECASE)
        if nature_match:
            return f"10.1038/{nature_match.group(1).lower()}"
    return ""


def extract_arxiv_id(value: str) -> str:
    match = _ARXIV_RE.search(unquote(value))
    return match.group(1) if match else ""


def extract_pubmed_ids(value: str) -> tuple[str, str]:
    decoded = unquote(value)
    pmcid_match = _PMCID_RE.search(decoded)
    pmcid = pmcid_match.group(1).upper() if pmcid_match else ""
    host = (urlsplit(value).hostname or "").lower()
    path = urlsplit(value).path
    pmid = ""
    if host.endswith("pubmed.ncbi.nlm.nih.gov"):
        match = re.search(r"/(\d+)/?", path)
        if match:
            pmid = match.group(1)
    return pmid, pmcid


def is_probably_direct_pdf(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")
