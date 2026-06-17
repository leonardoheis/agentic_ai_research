from __future__ import annotations

import contextlib
import os
import pathlib
import re
import time
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET
import fitz  # PyMuPDF
import requests
import wikipedia
from dotenv import load_dotenv
from pdfminer.high_level import extract_text_to_fp
from requests.adapters import HTTPAdapter
from tavily import TavilyClient
from tavily.errors import BadRequestError, UsageLimitExceededError
from urllib3.util.retry import Retry

load_dotenv()


def _build_session(
    user_agent: str = "LF-ADP-Agent/1.0 (mailto:your.email@example.com)",
) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    })
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_redirect=False,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


session = _build_session()


# ----- Utilities -----
def ensure_pdf_url(abs_or_pdf_url: str) -> str:
    url = abs_or_pdf_url.strip().replace("http://", "https://")
    if "/pdf/" in url and url.endswith(".pdf"):
        return url
    url = url.replace("/abs/", "/pdf/")
    if not url.endswith(".pdf"):
        url += ".pdf"
    return url


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def clean_text(s: str) -> str:
    s = re.sub(r"-\n", "", s)  # "transfor-\nmers" -> "transformers"
    s = re.sub(r"\r\n|\r", "\n", s)  # normaliza saltos
    s = re.sub(r"[ \t]+", " ", s)  # colapsa espacios
    s = re.sub(r"\n{3,}", "\n\n", s)  # no más de 1 línea en blanco seguida
    return s.strip()


def fetch_pdf_bytes(pdf_url: str, timeout: int = 90) -> bytes:
    r = session.get(pdf_url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.content


def _extract_with_pymupdf(pdf_bytes: bytes, max_pages: int | None) -> str:
    out: list[str] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        n = len(doc)
        limit = n if max_pages is None else min(max_pages, n)
        out.extend(doc.load_page(i).get_text("text") for i in range(limit))
    return "\n".join(out)


def _extract_with_pdfminer(pdf_bytes: bytes) -> str:
    buf_in = BytesIO(pdf_bytes)
    buf_out = BytesIO()
    extract_text_to_fp(buf_in, buf_out)
    return buf_out.getvalue().decode("utf-8", errors="ignore")


def pdf_bytes_to_text(pdf_bytes: bytes, max_pages: int | None = None) -> str:
    with contextlib.suppress(ImportError, OSError):
        return _extract_with_pymupdf(pdf_bytes, max_pages)

    try:
        return _extract_with_pdfminer(pdf_bytes)
    except (ImportError, OSError) as e:
        msg = f"PDF text extraction failed: {e}"
        raise RuntimeError(msg) from e


def maybe_save_pdf(pdf_bytes: bytes, dest_dir: str, filename: str) -> str:
    dest_path = pathlib.Path(dest_dir)
    dest_path.mkdir(exist_ok=True, parents=True)
    path = dest_path / _safe_filename(filename)
    path.write_bytes(pdf_bytes)
    return str(path)


# ----- arXiv search -----

_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass
class _PdfConfig:
    extract_text: bool = True
    max_pages: int = 6
    text_chars: int = 5000
    save_full_text: bool = False
    sleep_seconds: float = 1.0


def _parse_arxiv_entry(entry: Element) -> dict[str, Any]:
    """Extract metadata from a single Atom <entry> element.

    Returns:
        dict: Keys: title, authors, published, url, summary, link_pdf.
    """
    ns = _ARXIV_NS
    title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
    published = (entry.findtext("atom:published", default="", namespaces=ns) or "")[:10]
    url_abs = entry.findtext("atom:id", default="", namespaces=ns) or ""
    abstract = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()

    authors = [
        nm
        for a in entry.findall("atom:author", ns)
        if (nm := a.findtext("atom:name", default="", namespaces=ns))
    ]

    link_pdf = next(
        (
            lnk.attrib.get("href")
            for lnk in entry.findall("atom:link", ns)
            if lnk.attrib.get("title") == "pdf"
        ),
        None,
    )
    if not link_pdf and url_abs:
        link_pdf = ensure_pdf_url(url_abs)

    return {
        "title": title,
        "authors": authors,
        "published": published,
        "url": url_abs,
        "summary": abstract,
        "link_pdf": link_pdf,
    }


def _enrich_item_with_pdf(item: dict[str, Any], cfg: _PdfConfig) -> None:
    """Fetch the PDF for *item* and overwrite its summary with extracted text (in-place)."""
    link_pdf = item.get("link_pdf")
    if not link_pdf:
        return

    pdf_bytes: bytes | None = None
    try:
        pdf_bytes = fetch_pdf_bytes(link_pdf, timeout=90)
        time.sleep(cfg.sleep_seconds)
    except requests.exceptions.RequestException as e:
        item["pdf_error"] = f"PDF fetch failed: {e}"

    if not cfg.extract_text or not pdf_bytes:
        return

    try:
        text = pdf_bytes_to_text(pdf_bytes, max_pages=cfg.max_pages)
        text = clean_text(text) if text else ""
        if text:
            item["summary"] = text if cfg.save_full_text else text[: cfg.text_chars]
    except RuntimeError as e:
        item["text_error"] = f"Text extraction failed: {e}"


def _collect_arxiv_items(root: Element, cfg: _PdfConfig, *, enrich: bool) -> list[dict[str, Any]]:
    """Parse all Atom entries and optionally enrich each with PDF text.

    Returns:
        list[dict[str, Any]]: One dict per arXiv entry with metadata (and summary if enriched).
    """
    out: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", _ARXIV_NS):
        item = _parse_arxiv_entry(entry)
        if enrich:
            _enrich_item_with_pdf(item, cfg)
        out.append(item)
    return out


def arxiv_search_tool(
    query: str,
    max_results: int = 3,
) -> list[dict[str, Any]]:
    """Search arXiv and return results with full-text summaries extracted from PDFs.

    Returns:
        list[dict[str, Any]]: One dict per result with keys: title, authors, published,
            url, summary, link_pdf. On failure returns a single-element list with an
            ``error`` key.
    """
    cfg = _PdfConfig()
    enrich = True  # fetch PDF and extract text

    api_url = (
        "https://export.arxiv.org/api/query"
        f"?search_query=all:{quote(query)}&start=0&max_results={max_results}"
    )

    try:
        resp = session.get(api_url, timeout=60)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        return [{"error": f"arXiv API request failed: {e}"}]

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        return [{"error": f"arXiv API XML parse failed: {e}"}]
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        return [{"error": f"Unexpected error: {e}"}]
    else:
        return _collect_arxiv_items(root, cfg, enrich=enrich)


# ---- Tool def ----
arxiv_tool_def = {
    "type": "function",
    "function": {
        "name": "arxiv_search_tool",
        "description": "Searches arXiv and (internally) fetches PDFs to memory and extracts text.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords."},
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
}


def tavily_search_tool(
    query: str, max_results: int = 5, *, include_images: bool = False
) -> list[dict[str, Any]]:
    """
    Perform a search using the Tavily API.
    Args:
        query (str): The search query.
        max_results (int): Number of results to return (default 5).
        include_images (bool): Whether to include image results.
    Returns:
        List[dict]: A list of dictionaries with keys like 'title', 'content', and 'url'.
    Raises:
        ValueError: If the Tavily API key is not found in environment variables.
    """
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        msg = "TAVILY_API_KEY not found in environment variables."
        raise ValueError(msg)

    client = TavilyClient(api_key, api_base_url=os.getenv("DLAI_TAVILY_BASE_URL"))

    try:
        response = client.search(
            query=query, max_results=max_results, include_images=include_images
        )
    except BadRequestError as e:
        return [{"error": str(e)}]
    except UsageLimitExceededError as e:
        return [{"error": str(e)}]
    else:
        results = [
            {
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "url": r.get("url", ""),
            }
            for r in response.get("results", [])
        ]
        if include_images:
            results.extend({"image_url": img_url} for img_url in response.get("images", []))
        return results


tavily_tool_def = {
    "type": "function",
    "function": {
        "name": "tavily_search_tool",
        "description": "Performs a general-purpose web search using the Tavily API.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords for retrieving information from the web.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "default": 5,
                },
                "include_images": {
                    "type": "boolean",
                    "description": "Whether to include image results.",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    },
}

# Wikipedia search tool


def wikipedia_search_tool(query: str, sentences: int = 5) -> list[dict[str, Any]]:
    """
    Searches Wikipedia for a summary of the given query.

    Args:
        query (str): Search query for Wikipedia.
        sentences (int): Number of sentences to include in the summary.

    Returns:
        List[Dict]: A list with a single dictionary containing title, summary, and URL.
    """
    try:
        page_title = wikipedia.search(query)[0]
        page = wikipedia.page(page_title)
        summary = wikipedia.summary(page_title, sentences=sentences)

    except wikipedia.exceptions.DisambiguationError as e:
        return [{"error": f"Disambiguation error: {e}"}]
    except wikipedia.exceptions.PageError as e:
        return [{"error": f"Page error: {e}"}]
    else:
        return [{"title": page.title, "summary": summary, "url": page.url}]


# Tool definition
wikipedia_tool_def = {
    "type": "function",
    "function": {
        "name": "wikipedia_search_tool",
        "description": "Searches for a Wikipedia article summary by query string.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords for the Wikipedia article.",
                },
                "sentences": {
                    "type": "integer",
                    "description": "Number of sentences in the summary.",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}


# Tool mapping
tool_mapping = {
    "tavily_search_tool": tavily_search_tool,
    "arxiv_search_tool": arxiv_search_tool,
    "wikipedia_search_tool": wikipedia_search_tool,
}
