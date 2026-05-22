"""Earnings call transcript fetcher.

Strategy:
1. Get exact earnings dates from Yahoo Finance
2. Construct direct URLs for known transcript sites (Motley Fool, Stock Analysis)
3. Check URLs with HEAD request; fetch and parse if found
4. Also scrape Stock Analysis transcript archive page for ticker
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

# ---- URL Construction ----

def _company_slug(name: str, ticker: str) -> str:
    """Generate Motley Fool company slug from company name.

    Examples: Apple Inc. -> 'apple', Walmart Inc. -> 'walmart'
    """
    name = name.lower()
    for suffix in [
        " inc.", " inc", " corp.", " corp", " corporation",
        " ltd.", " ltd", " limited", " plc", " ag", " se", " sa",
        " nv", " company", " group", " holdings", " holding",
        " technologies", " technology", " communications",
        " entertainment", " pharmaceuticals", " laboratories",
        " international", " partners", " energy",
    ]:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    slug = re.sub(r'[^a-z0-9\s]', '', name)
    slug = re.sub(r'\s+', '-', slug.strip())
    return slug


def _motley_fool_urls(ticker: str, company_name: str, date_str: str, quarter: str, fy: str) -> list[str]:
    """Construct possible Motley Fool transcript URLs.

    Confirmed pattern: /earnings/call-transcripts/{YYYY}/{MM}/{DD}/{slug}-{ticker}-q{Q}-{FY}-earnings-call-transcript/
    """
    slug = _company_slug(company_name, ticker)
    tk = ticker.lower()
    y, m, d = date_str[:4], date_str[4:6], date_str[6:8]
    base = f"https://www.fool.com/earnings/call-transcripts/{y}/{m}/{d}/"

    return [
        f"{base}{slug}-{tk}-q{quarter}-{fy}-earnings-call-transcript/",
        f"{base}{slug}-{tk}-q{quarter}-{fy}-earnings-transcript/",
        f"{base}{tk}-q{quarter}-{fy}-earnings-call-transcript/",
        f"{base}{tk}-q{quarter}-{fy}-earnings-transcript/",
    ]


# ---- Yahoo Finance Helpers ----

def _get_earnings_info(ticker: str) -> tuple[list[dict], str]:
    """Get historical earnings dates and company name from Yahoo Finance.

    Returns:
        ([{date: 'YYYYMMDD', quarter: 'Q1'-'Q4', fy: '2026'}], company_name)
    """
    results = []
    name = ""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.info
        name = info.get("longName", info.get("shortName", ""))

        # Get earnings dates
        earnings = stock.earnings_dates
        if earnings is not None and not earnings.empty:
            for dt in earnings.index:
                date_str = dt.strftime("%Y%m%d")
                month = dt.month
                # Estimate fiscal quarter from calendar
                q_map = {1: "4", 2: "4", 3: "4", 4: "1", 5: "1", 6: "1",
                         7: "2", 8: "2", 9: "2", 10: "3", 11: "3", 12: "3"}
                q = q_map.get(month, "1")
                fy = str(dt.year - 1 if month <= 3 else dt.year)
                results.append({"date": date_str, "quarter": q, "fy": fy})
    except Exception:
        pass
    return results, name


# ---- Stock Analysis Scraping ----

def _stockanalysis_transcripts(ticker: str) -> list[dict]:
    """Scrape Stock Analysis transcript archive for a ticker.

    Stock Analysis has: stockanalysis.com/stocks/{ticker}/transcripts/
    """
    url = f"https://stockanalysis.com/stocks/{ticker.lower()}/transcripts/"
    try:
        resp = _session.get(url, timeout=30)
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for link in soup.select("a[href*='/transcripts/']"):
            href = link.get("href", "")
            text = link.get_text(strip=True)
            if not href or not text:
                continue
            # Skip the list page itself
            if href.rstrip("/").endswith("/transcripts"):
                continue
            full_url = f"https://stockanalysis.com{href}" if href.startswith("/") else href
            # Extract date and quarter from text like "Q2 2026 Earnings Call"
            date_match = re.search(r'(\d{4})', text)
            quarter_match = re.search(r'(Q\d)\s*(\d{4})', text)
            results.append({
                "title": f"{ticker.upper()} {text} Transcript",
                "url": full_url,
                "source": "Stock Analysis",
                "ticker": ticker.upper(),
                "quarter": f"{quarter_match.group(1)} FY{quarter_match.group(2)}" if quarter_match else "",
            })
        return results
    except Exception:
        return []


# ---- Transcript Finding ----

def find_transcripts(ticker: str, company_name: str, filing_dates: list[str]) -> list[dict]:
    """Find earnings call transcripts using direct URL construction + archive scraping.

    Args:
        ticker: Stock symbol
        company_name: Full company name
        filing_dates: SEC filing dates (YYYYMMDD), used as earnings date candidates

    Returns:
        [{title, url, source, ticker, quarter}]
    """
    found = []
    checked_urls = set()

    # Get Yahoo Finance earnings dates if company name is unknown
    if not company_name:
        yf_dates, company_name = _get_earnings_info(ticker)
    else:
        yf_dates, _ = _get_earnings_info(ticker)

    # Build candidate date list: SEC filings + Yahoo Finance
    candidate_dates = set(filing_dates) if filing_dates else set()
    for yd in yf_dates:
        candidate_dates.add(yd["date"])

    # Step 1: Direct Motley Fool URL construction (limited attempts for speed)
    for date_str in sorted(candidate_dates)[:6]:  # max 6 dates
        dt = datetime.strptime(date_str, "%Y%m%d")
        m = dt.month
        q_map = {1: "4", 2: "4", 3: "4", 4: "1", 5: "1", 6: "1",
                 7: "2", 8: "2", 9: "2", 10: "3", 11: "3", 12: "3"}
        q = q_map.get(m, "1")
        fy = str(dt.year - 1 if m <= 3 else dt.year)

        if not company_name:
            company_name = ticker

        # Try ±1 day offsets, primary quarter/year only, first URL pattern
        for offset in (0, -1, 1):
            var_dt = dt + timedelta(days=offset)
            var_date = var_dt.strftime("%Y%m%d")
            urls = _motley_fool_urls(ticker, company_name, var_date, q, fy)
            url = urls[0] if urls else ""
            if url and url not in checked_urls:
                checked_urls.add(url)
                if _url_exists(url):
                    found.append({
                        "title": f"{ticker.upper()} Q{q} FY{fy} Earnings Call Transcript",
                        "url": url,
                        "source": "Motley Fool",
                        "ticker": ticker.upper(),
                        "quarter": f"Q{q} FY{fy}",
                    })
                    break  # Found one for this date

    # Step 2: Scrape Stock Analysis archive
    sa_results = _stockanalysis_transcripts(ticker)
    for r in sa_results:
        found.append(r)

    # Step 3: Deduplicate by URL
    seen = set()
    unique = []
    for f in found:
        if f["url"] not in seen:
            seen.add(f["url"])
            unique.append(f)

    return unique


def _url_exists(url: str) -> bool:
    """Check if a URL exists (GET with stream to avoid full download)."""
    try:
        resp = _session.get(url, timeout=15, allow_redirects=True, stream=True)
        resp.close()
        return resp.status_code == 200
    except Exception:
        return False


# ---- Transcript Fetching ----

def fetch_transcript(url: str) -> str | None:
    """Fetch and parse transcript content from a URL.

    Returns the transcript text, or None if not found.
    """
    try:
        resp = _session.get(url, timeout=30, allow_redirects=True)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Try known article body selectors
        selectors = [
            "#transcript-panel-full",                           # Stock Analysis
            "div.space-y-6.text-base",                         # Stock Analysis (inner)
            "div.article-body",                                # Motley Fool
            "div.prose",                                       # Generic
            "div.post-content",                                # AlphaStreet, Insider Monkey
            "div.article-content",                             # Benzinga
            "section.article-body",
            "div.transcript-body",
            "div[itemprop='articleBody']",
            "article",
            "div.entry-content",
            "main article",
        ]

        content_div = None
        for sel in selectors:
            content_div = soup.select_one(sel)
            if content_div:
                break

        if not content_div:
            # Fallback: find SMALLEST div with Operator >= 2 (transcript signature)
            candidates = []
            for div in soup.find_all("div"):
                text = div.get_text(strip=True)
                if text.count("Operator") >= 2 and len(text) > 2000:
                    candidates.append((len(text), div))
            if candidates:
                candidates.sort(key=lambda x: x[0])
                content_div = candidates[0][1]

        if not content_div:
            return None

        # Extract paragraphs
        paragraphs = []
        for p in content_div.find_all(["p", "div"]):
            text = p.get_text(strip=True)
            if text and len(text) > 30:
                # Skip nav/ads/cookie notices
                first_20 = text.lower()[:30]
                if any(skip in first_20 for skip in [
                    "cookie", "advertisement", "subscribe",
                    "sign up", "login", "menu", "search",
                    "share this article", "read more",
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


# ---- Main Download Function ----

def download_transcripts(
    ticker: str,
    filing_dates: list[str],
    output_dir: Path,
    progress_callback=None,
    company_name: str = "",
) -> list[Path]:
    """Find and download earnings call transcripts.

    Uses direct URL construction for Motley Fool + Stock Analysis archive.

    Args:
        ticker: Stock ticker symbol
        filing_dates: SEC filing dates (YYYYMMDD)
        output_dir: Directory to save transcripts
        progress_callback: Optional (current, total, msg)
        company_name: Full company name for URL construction

    Returns:
        List of downloaded file paths
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    if progress_callback:
        progress_callback(0, 1, f"Searching transcripts for {ticker}...")

    # Get company name from Yahoo if needed
    if not company_name:
        _, company_name = _get_earnings_info(ticker)

    # Find transcripts
    transcripts = find_transcripts(ticker, company_name, filing_dates or [])

    total = len(transcripts)
    if total == 0:
        if progress_callback:
            progress_callback(0, 1, f"No transcripts found for {ticker}")
        return downloaded

    for i, trans in enumerate(transcripts):
        if progress_callback:
            progress_callback(i, total,
                f"[{trans['source']}] {trans['title'][:50]}")

        try:
            text = fetch_transcript(trans["url"])
            if text and len(text) > 500:
                safe_title = _sanitize(trans["title"])[:60]
                source_tag = trans.get("source", "Web").replace(" ", "_")
                filename = f"{ticker}_{source_tag}_{safe_title}.txt"
                filepath = output_dir / filename

                content = (
                    f"Title: {trans['title']}\n"
                    f"Ticker: {trans['ticker']}\n"
                    f"Source: {trans['source']}\n"
                    f"URL: {trans['url']}\n"
                    + "=" * 60 + "\n\n" + text
                )
                filepath.write_text(content, encoding="utf-8")
                downloaded.append(filepath)
        except Exception:
            continue

        time.sleep(0.3)

    return downloaded


def download_presentations(
    ticker: str,
    output_dir: Path,
    progress_callback=None,
    company_name: str = "",
) -> list[Path]:
    """Presentations are primarily obtained from SEC 8-K exhibits.
    This function is kept for API compatibility but delegates to SEC exhibit download.
    The SEC exhibit download (in filing_fetcher_us.py) handles EX-99.x files
    from 8-K/6-K filings, which are the actual earnings presentations.

    Returns:
        Empty list (presentations come from SEC exhibits)
    """
    if progress_callback:
        progress_callback(0, 1, "Presentations are downloaded from SEC 8-K exhibits")
    return []


def _sanitize(name: str) -> str:
    """Clean filename of invalid characters."""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    return name.strip().strip(".")[:200]
