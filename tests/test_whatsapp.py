from datetime import datetime
from pathlib import Path

from whatsapp_paper_collector.urls import extract_doi, is_meeting_url, normalize_url
from whatsapp_paper_collector.whatsapp import extract_links, parse_chat_file, parse_chat_text


FIXTURE = Path(__file__).parent / "fixtures" / "iphone_chat.txt"


def test_sanitized_iphone_fixture() -> None:
    assert b"\r\n" in FIXTURE.read_bytes()
    messages = parse_chat_file(FIXTURE)
    links = extract_links(messages)

    assert len(messages) == 11
    assert len(links) == 10
    assert messages[0].timestamp == datetime(2026, 7, 1, 14, 38, 42)
    assert messages[0].sender == "~\u202fResearcher"
    assert messages[2].sender.startswith("🧪")
    assert "image omitted" in messages[5].text


def test_multiline_multiple_links_and_balanced_parentheses() -> None:
    text = (
        "[4/7/26, 9:00:00\u202fAM] Scientist 🧬: Papers:\n"
        "https://cell.example.test/fulltext/S0092-8674(26)00695-1 and\n"
        "https://doi.org/10.1000/example).\n"
    )
    messages = parse_chat_text(text)
    links = extract_links(messages)

    assert len(messages) == 1
    assert "and\nhttps" in messages[0].text
    assert links[0].original_url.endswith("S0092-8674(26)00695-1")
    assert links[1].original_url == "https://doi.org/10.1000/example"


def test_url_normalization_and_identifiers() -> None:
    url = "HTTPS://Example.COM:443/paper?utm_source=x&id=3&__cf_chl_tk=y#abstract"
    assert normalize_url(url) == "https://example.com/paper?id=3"
    assert extract_doi("https://doi.org/10.1234/ABC.Def") == "10.1234/abc.def"
    assert (
        extract_doi("https://www.nature.com/articles/s41586-026-10604-6")
        == "10.1038/s41586-026-10604-6"
    )


def test_meeting_domains_are_recognized_without_broad_false_positive() -> None:
    assert is_meeting_url("https://lab.zoom.us/j/123")
    assert is_meeting_url("https://meet.google.com/abc-defg-hij")
    assert is_meeting_url("https://teams.microsoft.com/l/meetup-join/example")
    assert not is_meeting_url("https://example.org/paper-about-zoom")
