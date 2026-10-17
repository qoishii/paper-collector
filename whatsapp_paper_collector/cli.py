from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .pipeline import Collector
from .resolvers import ScholarlyResolver
from .storage import LibraryStore
from .web import WebClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-collector",
        description="Collect scholarly papers linked in an exported WhatsApp chat.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    import_parser = subparsers.add_parser("import", help="Import a WhatsApp .txt export")
    import_parser.add_argument("chat", type=Path, help="Path to the exported WhatsApp .txt file")
    import_parser.add_argument(
        "--library",
        type=Path,
        default=Path("paper-library"),
        help="Library directory (default: ./paper-library)",
    )
    import_parser.add_argument(
        "--date-order",
        choices=("dmy", "mdy"),
        default="dmy",
        help="Date order used by the chat export (default: dmy)",
    )
    return parser


def _progress(index: int, total: int, url: str, status: str) -> None:
    host = urlsplit(url).hostname or url
    print(f"[{index}/{total}] {host}: {status}")


def _print_summary(summary: dict[str, int], library: Path) -> None:
    print("\nImport complete")
    print(f"Library: {library.resolve()}")
    print(f"Links found: {summary['links']}")
    print(f"PDFs downloaded: {summary['pdfs']}")
    print(f"Open full texts saved: {summary['fulltexts']}")
    print(f"Abstracts saved: {summary['abstracts']}")
    print(f"Metadata-only records: {summary['metadata_only']}")
    print(f"Duplicates: {summary['duplicates']}")
    print(f"Ignored links: {summary['ignored']}")
    print(f"Review items: {summary['review']}")
    print(f"Failed links: {summary['failed']}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "import":
        return 2
    if not args.chat.is_file():
        print(f"error: chat file does not exist: {args.chat}", file=sys.stderr)
        return 1
    if args.chat.suffix.lower() != ".txt":
        print("error: the WhatsApp export must be a .txt file", file=sys.stderr)
        return 1

    try:
        with WebClient() as web, LibraryStore(args.library) as store:
            resolver = ScholarlyResolver(web)
            summary = Collector(resolver, store).import_chat(
                args.chat,
                date_order=args.date_order,
                progress=_progress,
            )
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    _print_summary(summary, args.library)
    return 0
