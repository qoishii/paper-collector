# Paper from Link

Paper from Link turns paper links from an exported iPhone WhatsApp chat into a local, searchable paper library. It downloads one best legal open copy when available and otherwise saves the best accessible abstract or metadata record.

It does not bypass paywalls, log into publisher sites, reuse browser cookies, or solve anti-bot challenges.

## Install on macOS

Install Python 3.11 or newer, then run these commands from this project directory:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## Import a chat

Export the WhatsApp chat without media, then run:

```bash
paper-collector import "/path/to/_chat.txt" --library "/path/to/paper-library"
```

The default date order is day/month/year. For a month/day/year export, add `--date-order mdy`.

The library contains:

- `library.sqlite3`: the persistent source of truth.
- `papers.csv`: one row per paper.
- `occurrences.csv`: the sender, timestamp, message, and URL for every paper occurrence.
- `review.csv`: links that could not be confidently resolved or downloaded.
- `files/`: PDFs, open HTML full text, abstracts, and metadata records.

Files use the form `000001-Smith-2026-short_paper_title.pdf`. Only the first author's last name is used; title words are separated with underscores.

Re-importing the same chat does not duplicate completed papers or occurrences. Failed, abstract-only, and metadata-only entries are checked again in case an open copy has become available.

Obvious meeting links such as Zoom, Google Meet, and Teams are ignored without being opened or recorded. Emojis and non-English text are preserved in the database and CSV files.

## Privacy

The original chat, sender names, and message text remain in the local library. Only a paper URL, identifier, or title needed for lookup is sent to the linked website or public scholarly services such as OpenAlex, Europe PMC, and Crossref.

## Development

```bash
python -m pip install -e '.[test]'
pytest
```

Tests mock network requests; they do not download live papers.
