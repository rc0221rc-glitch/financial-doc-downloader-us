"""Earnings call transcript fetcher from Motley Fool."""

import re
import time
from pathlib import Path
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; financial-research-tool/1.0)"
})

MOTLEY_FOOL_BASE = "https://www.fool.com"
MOTLEY_FOOL_TRANSCRIPTS = f"{MOTLEY_FOOL_BASE}/earnings-call-transcripts/"


def search_transcripts(ticker: str, start_date: str, end_date: str) -> list[dict]:
    """Search for earnings call transcripts on Motley Fool.

    Scrapes the transcript listing pages and filters by ticker and date range.

    Args:
        ticker: Stock ticker symbol
        start_date: YYYYMMDD
        end_date: YYYYMMDD

    Returns:
        list of {title, date, url, ticker, quarter}
    """
    results = []
    ticker_lower = ticker.lower()
    sd = datetime.strptime(start_date, "%Y%m%d")
    ed = datetime.strptime(end_date, "%Y%m%d")

    for page in range(1, 6):
        if page == 1:
            url = MOTLEY_FOOL_TRANSCRIPTS
        else:
            url = f"{MOTLEY_FOOL_TRANSCRIPTS}page/{page}/"

        try:
            resp = _session.get(url, timeout=30)
            if resp.status_code != 200:
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            links = soup.select("a[href*='/earnings/call-transcripts/']")

            page_has_ticker = False
            for link in links:
                href = link.get("href", "")
                if not href or "/page/" in href:
                    continue

                url_lower = href.lower()
                if f"-{ticker_lower}-" not in url_lower and f"/{ticker_lower}-" not in url_lower:
                    continue

                page_has_ticker = True

                date_str = _extract_date_from_url(href)
                if not date_str:
                    continue

                try:
                    article_date = datetime.strptime(date_str, "%Y%m%d")
                except ValueError:
                    continue

                if article_date < sd or article_date > ed:
                    continue

                quarter = _extract_quarter_from_url(href)
                title = link.get_text(strip=True)
                if not title:
                    title = f"{ticker.upper()} Earnings Call Transcript"

                full_url = href if href.startswith("http") else MOTLEY_FOOL_BASE + href

                results.append({
                    "title": title,
                    "date": date_str,
                    "url": full_url,
                    "ticker": ticker.upper(),
                    "quarter": quarter,
                    "source": "Motley Fool",
                })

            if not page_has_ticker:
                break

        except Exception:
            break

        time.sleep(0.5)

    return results


def fetch_transcript(url: str) -> str | None:
    """Fetch and parse transcript content from a Motley Fool page.

    Returns the transcript text, or None if not found.
    """
    try:
        resp = _session.get(url, timeout=30)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        selectors = [
            "div.article-body",
            "div.article-content",
            "section.article-body",
            "div.transcript-body",
            "div[itemprop='articleBody']",
            "div#article-body",
            "div.earnings-transcript-body",
        ]

        content_div = None
        for sel in selectors:
            content_div = soup.select_one(sel)
            if content_div:
                break

        if not content_div:
            for div in soup.find_all("div"):
                if div.get_text(strip=True).count("Operator") >= 2:
                    content_div = div
                    break

        if not content_div:
            return None

        paragraphs = []
        for p in content_div.find_all(["p", "div"]):
            text = p.get_text(strip=True)
            if text and len(text) > 30:
                paragraphs.append(text)

        if not paragraphs:
            text = content_div.get_text(strip=True)
            paragraphs = [text]

        return "\n\n".join(paragraphs)

    except Exception:
        return None


def download_transcripts(
    ticker: str,
    filing_dates: list[str],
    output_dir: Path,
    progress_callback=None,
) -> list[Path]:
    """Search and download earnings call transcripts for a ticker.

    Args:
        ticker: Stock ticker symbol
        filing_dates: List of filing dates (YYYYMMDD) to search around
        output_dir: Directory to save transcripts
        progress_callback: Optional (current, total, msg)

    Returns:
        List of downloaded file paths
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    if not filing_dates:
        return downloaded

    min_date = min(filing_dates)
    max_date = max(filing_dates)

    sd = datetime.strptime(min_date, "%Y%m%d") - timedelta(days=2)
    ed = datetime.strptime(max_date, "%Y%m%d") + timedelta(days=2)

    search_start = sd.strftime("%Y%m%d")
    search_end = ed.strftime("%Y%m%d")

    if progress_callback:
        progress_callback(0, 1, f"Searching transcripts for {ticker}...")

    transcripts = search_transcripts(ticker, search_start, search_end)

    total = len(transcripts)
    for i, trans in enumerate(transcripts):
        if progress_callback:
            progress_callback(i, total, f"Downloading: {trans['title'][:60]}")

        try:
            text = fetch_transcript(trans["url"])
            if text:
                safe_title = _sanitize_filename(trans["title"])[:80]
                filename = f"{ticker}_{trans['date']}_transcript_{safe_title}.txt"
                filepath = output_dir / filename

                content = f"Title: {trans['title']}\n"
                content += f"Ticker: {trans['ticker']}\n"
                content += f"Date: {trans['date']}\n"
                content += f"Quarter: {trans.get('quarter', 'N/A')}\n"
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


def _extract_date_from_url(url: str) -> str:
    """Extract date as YYYYMMDD from Motley Fool transcript URL."""
    m = re.search(r'/call-transcripts/(\d{4})/(\d{2})/(\d{2})/', url)
    if m:
        return m.group(1) + m.group(2) + m.group(3)
    return ""


def _extract_quarter_from_url(url: str) -> str:
    """Extract quarter info from Motley Fool transcript URL."""
    m = re.search(r'q(\d)-(\d{4})-earnings', url.lower())
    if m:
        return f"Q{m.group(1)} FY{m.group(2)}"
    return ""


def _sanitize_filename(name: str) -> str:
    """Clean filename of invalid characters."""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    return name.strip().strip(".")[:200]
