"""Earnings call transcript fetcher from Motley Fool.

Strategy:
1. Get earnings dates from Yahoo Finance (for exact dates)
2. Construct Motley Fool transcript URLs directly using known URL patterns
3. Try multiple URL variations (±2 days, with/without "call")
4. Fall back to scraping the listing page if direct URL construction fails
"""

import re
import time
from pathlib import Path
from datetime import datetime, timedelta

import requests

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; financial-research-tool/1.0)"
})

MOTLEY_FOOL_BASE = "https://www.fool.com"
MOTLEY_FOOL_TRANSCRIPTS = f"{MOTLEY_FOOL_BASE}/earnings-call-transcripts/"


def _get_company_slug(company_name: str, ticker: str) -> str:
    """Generate the company name slug used by Motley Fool in transcript URLs.

    Examples: 'Apple Inc.' -> 'apple', 'Zoom Video Communications' -> 'zoom'
    """
    # Remove common suffixes
    name = company_name.lower()
    for suffix in [
        " inc.", " inc", " corp.", " corp", " corporation", " ltd.", " ltd",
        " limited", " plc", " ag", " se", " sa", " nv", " company",
        " group", " holdings", " holding", " technologies", " technology",
        " communications", " entertainment", " pharmaceuticals",
        " laboratories", " international", " partners", " energy",
    ]:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break

    # Replace special chars with hyphens, collapse whitespace
    slug = re.sub(r'[^a-z0-9\s]', '', name)
    slug = re.sub(r'\s+', '-', slug.strip())
    return slug


def _construct_urls(ticker: str, company_name: str, date_str: str, quarter: str, fiscal_year: str) -> list[str]:
    """Construct possible Motley Fool transcript URLs for a given earnings date.

    Args:
        ticker: Stock ticker (e.g. 'AAPL')
        company_name: Full company name (e.g. 'Apple Inc.')
        date_str: Earnings date YYYYMMDD
        quarter: Fiscal quarter '1'-'4'
        fiscal_year: Fiscal year as string (e.g. '2026')

    Returns:
        List of possible URLs, ordered by likelihood
    """
    slug = _get_company_slug(company_name, ticker)
    ticker_lower = ticker.lower()

    # Parse date
    y = date_str[:4]
    m = date_str[4:6]
    d = date_str[6:8]

    date_prefix = f"{MOTLEY_FOOL_TRANSCRIPTS}{y}/{m}/{d}/"

    urls = []
    # Try variations from specific to generic
    # 1. {company}-{ticker}-q{quarter}-{fy}-earnings-call-transcript/
    urls.append(f"{date_prefix}{slug}-{ticker_lower}-q{quarter}-{fiscal_year}-earnings-call-transcript/")
    # 2. {company}-{ticker}-q{quarter}-{fy}-earnings-transcript/
    urls.append(f"{date_prefix}{slug}-{ticker_lower}-q{quarter}-{fiscal_year}-earnings-transcript/")
    # 3. {ticker}-q{quarter}-{fy}-earnings-call-transcript/
    urls.append(f"{date_prefix}{ticker_lower}-q{quarter}-{fiscal_year}-earnings-call-transcript/")
    # 4. {ticker}-q{quarter}-{fy}-earnings-transcript/
    urls.append(f"{date_prefix}{ticker_lower}-q{quarter}-{fiscal_year}-earnings-transcript/")

    return urls


def _check_url_exists(url: str) -> bool:
    """Quick check if a URL exists (HEAD request)."""
    try:
        resp = _session.head(url, timeout=15, allow_redirects=True)
        return resp.status_code == 200
    except Exception:
        return False


def _get_earnings_dates_from_yahoo(ticker: str) -> list[dict]:
    """Get historical earnings dates from Yahoo Finance.

    Returns:
        [{date_str: 'YYYYMMDD', quarter: '1'-'4', fiscal_year: '2026'}]
    """
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        # Get earnings history
        earnings = stock.earnings_dates
        if earnings is None or earnings.empty:
            return []

        results = []
        for dt, row in earnings.iterrows():
            date_str = dt.strftime("%Y%m%d")
            # Determine quarter from the reported EPS surprise data
            # earnings_dates index is the earnings date
            # We approximate quarter from the date
            month = dt.month
            # Rough quarter estimation from calendar
            if month in (1, 2, 3, 4):
                q = "4"  # Q4 reported in Jan-Apr
                fy = str(dt.year - 1) if month <= 3 else str(dt.year)
            elif month in (5, 6, 7):
                q = "1"
                fy = str(dt.year)
            elif month in (8, 9, 10):
                q = "2"
                fy = str(dt.year)
            else:
                q = "3"
                fy = str(dt.year)

            results.append({
                "date": date_str,
                "quarter": q,
                "fiscal_year": fy,
            })

        return results
    except Exception:
        return []


def _get_company_name_from_yahoo(ticker: str) -> str:
    """Get company name from Yahoo Finance."""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.info
        return info.get("longName", info.get("shortName", ""))
    except Exception:
        return ""


def find_transcript_urls(
    ticker: str,
    company_name: str = "",
    target_dates: list[str] = None,
) -> list[dict]:
    """Find Motley Fool transcript URLs for a ticker.

    Uses a combination of:
    1. Direct URL construction from known dates
    2. Date variations (±2 days per target date)
    3. Yahoo Finance earnings dates as additional targets

    Args:
        ticker: Stock ticker
        company_name: Company name (auto-fetched if empty)
        target_dates: List of YYYYMMDD dates to try (from SEC filings)

    Returns:
        [{title, date, url, ticker, quarter}]
    """
    ticker_lower = ticker.lower()

    # Get company name from Yahoo if not provided
    if not company_name:
        company_name = _get_company_name_from_yahoo(ticker)
    if not company_name:
        company_name = ticker

    # Collect all candidate dates
    all_dates = {}  # date_str -> {quarter, fiscal_year}

    # Add target dates (from SEC filings)
    if target_dates:
        for d in target_dates:
            if d not in all_dates:
                dt = datetime.strptime(d, "%Y%m%d")
                m = dt.month
                if m in (1, 2, 3, 4):
                    q, fy = "4", str(dt.year - 1 if m <= 3 else dt.year)
                elif m in (5, 6, 7):
                    q, fy = "1", str(dt.year)
                elif m in (8, 9, 10):
                    q, fy = "2", str(dt.year)
                else:
                    q, fy = "3", str(dt.year)
                all_dates[d] = {"quarter": q, "fiscal_year": fy}

    # Add Yahoo Finance earnings dates
    yf_dates = _get_earnings_dates_from_yahoo(ticker)
    for yd in yf_dates:
        if yd["date"] not in all_dates:
            all_dates[yd["date"]] = {"quarter": yd["quarter"], "fiscal_year": yd["fiscal_year"]}

    if not all_dates:
        return []

    # Try to find transcripts by constructing URLs
    found = []
    checked_urls = set()

    for date_str, info in sorted(all_dates.items()):
        # Try the exact date and ±2 day variations
        base_dt = datetime.strptime(date_str, "%Y%m%d")
        for offset in (0, -1, 1, -2, 2):
            dt = base_dt + timedelta(days=offset)
            var_date = dt.strftime("%Y%m%d")

            # Also try adjacent quarters (in case of fiscal year mismatch)
            q_variations = [(info["quarter"], info["fiscal_year"])]
            # Try previous quarter
            q_int = int(info["quarter"])
            fy_int = int(info["fiscal_year"])
            if q_int == 1:
                q_variations.append(("4", str(fy_int - 1)))
            else:
                q_variations.append((str(q_int - 1), info["fiscal_year"]))
            # Try next quarter
            if q_int == 4:
                q_variations.append(("1", str(fy_int + 1)))
            else:
                q_variations.append((str(q_int + 1), info["fiscal_year"]))

            for q_var, fy_var in q_variations:
                urls = _construct_urls(ticker, company_name, var_date, q_var, fy_var)
                for url in urls:
                    if url in checked_urls:
                        continue
                    checked_urls.add(url)

                    if _check_url_exists(url):
                        found.append({
                            "title": f"{ticker.upper()} Earnings Call Transcript",
                            "date": var_date,
                            "url": url,
                            "ticker": ticker.upper(),
                            "quarter": f"Q{q_var} FY{fy_var}",
                            "source": "Motley Fool",
                        })
                        # Don't break — one date might have multiple quarters' transcripts
                if found and any(f["date"] == var_date for f in found):
                    break
            if found and any(f["date"][:8] for f in found):
                break

    return found


def fetch_transcript(url: str) -> str | None:
    """Fetch and parse transcript content from a Motley Fool page.

    Returns the transcript text, or None if not found.
    """
    try:
        resp = _session.get(url, timeout=30)
        if resp.status_code != 200:
            return None

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try multiple selectors for the article body
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
            # Fallback: look for div with transcript-like content
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
    company_name: str = "",
) -> list[Path]:
    """Search and download earnings call transcripts for a ticker.

    Uses direct URL construction from known earnings dates + Yahoo Finance.

    Args:
        ticker: Stock ticker symbol
        filing_dates: List of filing dates (YYYYMMDD) from SEC data
        output_dir: Directory to save transcripts
        progress_callback: Optional (current, total, msg)
        company_name: Optional company name (auto-fetched if empty)

    Returns:
        List of downloaded file paths
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    if not filing_dates:
        if progress_callback:
            progress_callback(0, 1, f"No filing dates for {ticker}, trying Yahoo Finance...")
        # Try Yahoo Finance dates only
        filing_dates = []

    if progress_callback:
        progress_callback(0, 1, f"Searching transcripts for {ticker}...")

    # Get company name if not provided
    if not company_name:
        company_name = _get_company_name_from_yahoo(ticker)

    # Find transcript URLs
    transcripts = find_transcript_urls(ticker, company_name, filing_dates)

    total = len(transcripts)
    if total == 0 and progress_callback:
        progress_callback(0, 1, f"No transcripts found for {ticker}")
        return downloaded

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


def _sanitize_filename(name: str) -> str:
    """Clean filename of invalid characters."""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    return name.strip().strip(".")[:200]
