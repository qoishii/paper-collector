from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx


class WebError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    content: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    @property
    def is_pdf(self) -> bool:
        return self.content_type == "application/pdf" or self.content.lstrip().startswith(b"%PDF-")

    @property
    def text(self) -> str:
        content_type = self.headers.get("content-type", "")
        charset_match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
        encodings = [charset_match.group(1).strip("\"'")] if charset_match else []
        encodings.extend(("utf-8", "windows-1252"))
        for encoding in encodings:
            try:
                return self.content.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class WebClient:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 25.0,
        max_bytes: int = 100 * 1024 * 1024,
        retries: int = 1,
        retry_delay: float = 0.5,
    ):
        self._owns_client = client is None
        self.client = client or httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={
                "User-Agent": "WhatsAppPaperCollector/0.1",
                "Accept": "text/html,application/xhtml+xml,application/pdf,application/json;q=0.9,*/*;q=0.5",
            },
        )
        self.max_bytes = max_bytes
        self.retries = retries
        self.retry_delay = retry_delay

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "WebClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, url: str, *, accept: str = "") -> WebResponse:
        last_error: Exception | None = None
        headers = {"Accept": accept} if accept else None
        for attempt in range(self.retries + 1):
            self.client.cookies.clear()
            try:
                with self.client.stream("GET", url, headers=headers) as response:
                    if response.status_code in self.RETRY_STATUSES and attempt < self.retries:
                        response.read()
                        time.sleep(self.retry_delay)
                        continue
                    if response.status_code >= 400:
                        raise WebError(f"HTTP {response.status_code} for {url}")
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > self.max_bytes:
                        raise WebError(f"Response exceeds {self.max_bytes} bytes: {url}")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise WebError(f"Response exceeds {self.max_bytes} bytes: {url}")
                        chunks.append(chunk)
                    result = WebResponse(
                        url=str(response.url),
                        status_code=response.status_code,
                        headers={key.lower(): value for key, value in response.headers.items()},
                        content=b"".join(chunks),
                    )
                    self.client.cookies.clear()
                    return result
            except (httpx.TransportError, ValueError) as error:
                last_error = error
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                    continue
                break
        self.client.cookies.clear()
        raise WebError(str(last_error) if last_error else f"Unable to fetch {url}")
