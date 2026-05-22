"""Earnings call transcript & presentation fetcher.

Strategy:
1. Use DuckDuckGo search to find transcript/presentation URLs
2. Fetch and parse content from multiple sources (Motley Fool, Stock Analysis, etc.)
3. Save as text files
"""

import re
import time
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
})
_session.timeout = 30

# Known transcript sources (checked in order)
TRANSCRIPT_SOURCES = [
    {
        "name": "Motley Fool",
        "url_pattern": "fool.com/earnings/call-transcripts/",
        "article_selector": "div.article-body",
    },
    {
        "name": "Stock Analysis",
        "url_pattern": "stockanalysis.com/stocks/",
        "article_selector": "div.prose",
    },
    {
        "name": "Benzinga",
        "url_pattern": "benzinga.com/",
        "article_selector": "div.article-content",
    },
    {
        "name": "AlphaStreet",
        "url_pattern": "alphastreet.com/",
        "article_selector": "div.post-content",
    },
    {
        "name": "Insider Monkey",
        "url_pattern": "insidermonkey.com/",
        "article_selector": "div.article-content",
    },
]


def _duckduckgo_search(query: str, max_results: int = 10) -> list[dict]:
    """Search via DuckDuckGo HTML (no JS, no API key needed).

    Returns:
        [{title, url, snippet}]
    """
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    try:
        resp = _session.get(url, timeout=30)
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for item in soup.select(".result"):
            link = item.select_one(".result__a")
            snippet_el = item.select_one(".result__snippet")
            if link and link.get("href"):
                href = link["href"]
                # DuckDuckGo wraps URLs in redirect, extract real URL
                real_url = _extract_ddg_url(href)
                if real_url:
                    results.append({
                        "title": link.get_text(strip=True),
                        "url": real_url,
                        "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                    })
            if len(results) >= max_results:
                break
        return results
    except Exception:
        return []


def _extract_ddg_url(href: str) -> str:
    """Extract real URL from DuckDuckGo redirect URL."""
    # DDG HTML version uses uddg= param or direct link
    from urllib.parse import urlparse, parse_qs
    if href.startswith("http") and "duckduckgo.com" not in href:
        return href
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    if "uddg" in params:
        return params["uddg"][0]
    return ""


def search_transcripts(ticker: str, company_name: str = "") -> list[dict]:
    """Search for earnings call transcripts using DuckDuckGo.

    Args:
        ticker: Stock ticker
        company_name: Full company name (for better search results)

    Returns:
        [{title, url, source, date_estimated}]
    """
    all_results = []

    # Build search queries
    company = company_name or ticker
    queries = [
        f"{ticker} {company} earnings call transcript",
        f"{company} Q1 {datetime.now().year} earnings call transcript",
        f"{ticker} quarterly earnings conference call transcript",
    ]

    # Also try current and previous year
    current_year = datetime.now().year
    for year in (current_year, current_year - 1):
        for q in ("Q1", "Q2", "Q3", "Q4"):
            queries.append(f"{company} {q} {year} earnings call transcript")

    seen_urls = set()
    for query in queries[:6]:  # Limit to 6 queries
        results = _duckduckgo_search(query, max_results=10)
        for r in results:
            url = r["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Check if URL looks like a transcript
            url_lower = url.lower()
            is_transcript = any(kw in url_lower for kw in [
                "transcript", "earnings-call", "earnings_call",
                "earningscall", "call-transcript",
            ])
            if not is_transcript:
                # Also check title
                title_lower = r["title"].lower()
                is_transcript = any(kw in title_lower for kw in [
                    "transcript", "earnings call", "earnings conference",
                ])

            if is_transcript:
                # Determine source
                source = "Web"
                for src_info in TRANSCRIPT_SOURCES:
                    if src_info["url_pattern"] in url_lower:
                        source = src_info["name"]
                        break

                all_results.append({
                    "title": r["title"],
                    "url": url,
                    "source": source,
                    "ticker": ticker.upper(),
                })

        time.sleep(0.5)

    return all_results


def search_presentations(ticker: str, company_name: str = "") -> list[dict]:
    """Search for earnings presentations using DuckDuckGo.

    Returns:
        [{title, url, source}]
    """
    all_results = []
    company = company_name or ticker
    queries = [
        f"{company} investor presentation pdf",
        f"{company} earnings presentation slides",
        f"{ticker} quarterly earnings presentation",
        f"{company} investor relations earnings slides",
    ]

    seen_urls = set()
    for query in queries[:3]:
        results = _duckduckgo_search(query, max_results=10)
        for r in results:
            url = r["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            url_lower = url.lower()
            title_lower = r["title"].lower()
            is_presentation = any(kw in url_lower or kw in title_lower for kw in [
                "presentation", "slides", "deck", "investor",
                "earnings-slides", "investor-presentation",
            ])
            # Must be PDF or point to a presentation page
            if is_presentation and any(kw in url_lower for kw in [
                ".pdf", "presentation", "slides", "deck",
            ]):
                all_results.append({
                    "title": r["title"],
                    "url": url,
                    "source": "Web",
                    "ticker": ticker.upper(),
                })
        time.sleep(0.5)

    return all_results


def fetch_transcript(url: str, source: str = "") -> str | None:
    """Fetch and parse transcript content from a known source URL.

    Returns the transcript text, or None if not found.
    """
    try:
        resp = _session.get(url, timeout=30, allow_redirects=True)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Find source-specific selector
        selector = None
        for src_info in TRANSCRIPT_SOURCES:
            if source and src_info["name"] == source:
                selector = src_info["article_selector"]
                break

        content_div = None
        if selector:
            content_div = soup.select_one(selector)

        if not content_div:
            # Try generic selectors
            for sel in [
                "div.article-body", "div.article-content",
                "section.article-body", "div.transcript-body",
                "div[itemprop='articleBody']", "div#article-body",
                "div.prose", "div.post-content", "article",
                "div.entry-content", "main article",
            ]:
                content_div = soup.select_one(sel)
                if content_div:
                    break

        if not content_div:
            # Fallback: find div with Operator mentions (transcript pattern)
            for div in soup.find_all("div"):
                text = div.get_text(strip=True)
                if text.count("Operator") >= 2 and len(text) > 2000:
                    content_div = div
                    break

        if not content_div:
            return None

        # Extract paragraphs
        paragraphs = []
        for p in content_div.find_all(["p", "div"]):
            text = p.get_text(strip=True)
            if text and len(text) > 30:
                # Skip navigation, ads, etc.
                if any(skip in text.lower()[:20] for skip in [
                    "cookie", "advertisement", "subscribe", "sign up",
                    "login", "menu", "search", "share this",
                ]):
                    continue
                paragraphs.append(text)

        if not paragraphs:
            text = content_div.get_text(strip=True)
            if len(text) > 500:
                paragraphs = [text]

        return "\n\n".join(paragraphs) if paragraphs else None

    except Exception:
        return None


def download_transcripts(
    ticker: str,
    filing_dates: list[str],
    output_dir: Path,
    progress_callback=None,
    company_name: str = "",
) -> list[Path]:
    """Search and download earnings call transcripts.

    Uses DuckDuckGo search to find transcripts from multiple sources.

    Args:
        ticker: Stock ticker symbol
        filing_dates: Filing dates (unused in search mode, kept for API compat)
        output_dir: Directory to save transcripts
        progress_callback: Optional (current, total, msg)
        company_name: Company name for better search results

    Returns:
        List of downloaded file paths
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    if progress_callback:
        progress_callback(0, 1, f"Searching the web for {ticker} transcripts...")

    # Get company name from Yahoo if not provided
    if not company_name:
        try:
            import yfinance as yf
            stock = yf.Ticker(ticker)
            info = stock.info
            company_name = info.get("longName", info.get("shortName", ""))
        except Exception:
            company_name = ticker

    # Search for transcripts
    transcripts = search_transcripts(ticker, company_name)

    if not transcripts:
        if progress_callback:
            progress_callback(0, 1, f"No transcripts found for {ticker}")
        return downloaded

    total = len(transcripts)
    for i, trans in enumerate(transcripts):
        if progress_callback:
            progress_callback(i, total,
                f"[{trans['source']}] {trans['title'][:50]}")

        try:
            text = fetch_transcript(trans["url"], trans.get("source", ""))
            if text and len(text) > 500:
                safe_title = _sanitize_filename(trans["title"])[:60]
                source_tag = trans.get("source", "Web")
                filename = f"{ticker}_{source_tag}_transcript_{safe_title}.txt"
                filepath = output_dir / filename

                content = f"Title: {trans['title']}\n"
                content += f"Ticker: {trans['ticker']}\n"
                content += f"Source: {trans['source']}\n"
                content += f"URL: {trans['url']}\n"
                content += "=" * 60 + "\n\n"
                content += text

                filepath.write_text(content, encoding="utf-8")
                downloaded.append(filepath)
        except Exception:
            continue

        time.sleep(0.5)

    return downloaded


def download_presentations(
    ticker: str,
    output_dir: Path,
    progress_callback=None,
    company_name: str = "",
) -> list[Path]:
    """Search and download earnings presentations from the web.

    Currently saves URLs as text references (PDFs can be downloaded separately).

    Returns:
        List of downloaded file paths
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    if progress_callback:
        progress_callback(0, 1, f"Searching the web for {ticker} presentations...")

    if not company_name:
        try:
            import yfinance as yf
            stock = yf.Ticker(ticker)
            info = stock.info
            company_name = info.get("longName", info.get("shortName", ""))
        except Exception:
            company_name = ticker

    presentations = search_presentations(ticker, company_name)

    if not presentations:
        if progress_callback:
            progress_callback(0, 1, f"No presentations found for {ticker}")
        return downloaded

    total = len(presentations)
    for i, pres in enumerate(presentations):
        if progress_callback:
            progress_callback(i, total,
                f"[Presentation] {pres['title'][:50]}")

        try:
            url = pres["url"]
            # Try to download PDFs directly
            if url.lower().endswith(".pdf"):
                resp = _session.get(url, timeout=60, allow_redirects=True)
                if resp.status_code == 200 and len(resp.content) > 5000:
                    safe_title = _sanitize_filename(pres["title"])[:60]
                    filename = f"{ticker}_presentation_{safe_title}.pdf"
                    filepath = output_dir / filename
                    filepath.write_bytes(resp.content)
                    downloaded.append(filepath)
            else:
                # Save as URL reference
                safe_title = _sanitize_filename(pres["title"])[:60]
                filename = f"{ticker}_presentation_link_{safe_title}.txt"
                filepath = output_dir / filename
                filepath.write_text(
                    f"Title: {pres['title']}\n"
                    f"URL: {url}\n"
                    f"Source: {pres['source']}\n"
                    f"Ticker: {pres['ticker']}\n"
                    f"\nNote: This is a link reference. Download the presentation from the URL above.\n",
                    encoding="utf-8",
                )
                downloaded.append(filepath)
        except Exception:
            continue

        time.sleep(0.5)

    return downloaded


def _sanitize_filename(name: str) -> str:
    """Clean filename of invalid characters."""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    return name.strip().strip(".")[:200]
