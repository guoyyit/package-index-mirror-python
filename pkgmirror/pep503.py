"""Minimal PEP 503 helpers: link rewriting and fragment hash parsing."""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, unquote, urldefrag, urljoin, urlsplit


def parse_fragment_hash(url: str) -> tuple[str, str] | None:
    """Extract ``#sha256=...`` style hash from a URL fragment.

    The chosen digest is the first recognised algorithm among
    ``sha512``, ``=sha256`` and ``md5`` (strongest first).
    """
    _base, fragment = urldefrag(url)
    values: dict[str, str] = {}
    for key, value in parse_qsl(fragment, keep_blank_values=True):
        values[key.lower()] = value
    for algorithm in ("sha512", "sha256", "md5"):
        if values.get(algorithm):
            return algorithm, values[algorithm]
    return None


class _LinkRewriter(HTMLParser):
    """Rewrite anchor hrefs of a simple-index page to go via the proxy."""

    def __init__(self, page_url: str, base_url: str) -> None:
        super().__init__(convert_charrefs=False)
        self._page_url = page_url
        self._base_url = base_url.rstrip("/")
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit(tag, attrs, self_closing=True)

    def _emit(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> None:
        rendered = [tag]
        for name, value in attrs:
            if name == "href" and value is not None:
                value = self._rewrite_href(value)
            if value is None:
                rendered.append(name)
            else:
                rendered.append(f'{name}="{_escape_attr(value)}"')
        suffix = " />" if self_closing else ">"
        self.parts.append("<" + " ".join(rendered) + suffix)

    def _rewrite_href(self, href: str) -> str:
        absolute = urljoin(self._page_url, href)
        split = urlsplit(absolute)
        path = split.path
        # Artifact download: any file-like link not ending in "/"
        if path and not path.endswith("/"):
            return (
                f"{self._base_url}/artifacts/?url={quote(absolute, safe='')}"
            )
        # Package index link
        normalized = path.rstrip("/")
        package = normalized.rsplit("/", 1)[-1]
        if package:
            return f"{self._base_url}/simple/{package}/"
        return href

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")


def _escape_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;")


def rewrite_index_page(
    html: bytes,
    *,
    page_url: str,
    proxy_base: str,
    content_encoding: str | None = None,
) -> bytes:
    """Rewrite all hrefs in a simple-index page to point at this proxy."""
    text = html.decode("utf-8", errors="replace")
    parser = _LinkRewriter(page_url=page_url, base_url=proxy_base)
    parser.feed(text)
    return "".join(parser.parts).encode("utf-8")


def normalize_package(name: str) -> str:
    return unquote(name).strip("/").lower()
