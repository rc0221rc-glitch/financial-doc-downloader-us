"""Earnings call transcript fetcher.

Strategy:
1. Get exact earnings dates from Yahoo Finance
2. Construct direct URLs for known transcript sites (Motley Fool, Stock Analysis)
3. Try Q4 Events API for prepared remarks (used by ~30% of public companies)
4. Scrape Stock Analysis transcript archive page for ticker
5. DuckDuckGo search fallback for non-US companies
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
    suffixes = [
        " inc.", " inc", " corp.", " corp", " corporation",
        " ltd.", " ltd", " limited", " plc", " ag", " se", " sa",
        " nv", " company", " group", " holdings", " holding",
        " technologies", " technology", " communications",
        " entertainment", " pharmaceuticals", " laboratories",
        " international", " partners", " energy",
    ]
    # Strip all matching suffixes (multiple may apply)
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                changed = True
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


# ---- Q4 Events API for Transcripts ----

def _try_q4_transcripts(ir_domain: str, ticker: str) -> list[dict]:
    """Try Q4 Inc Events API for prepared remarks / transcripts.

    Returns:
        [{title, url, source, ticker, quarter}]
    """
    results = []
    url = f"https://{ir_domain}/feed/Event.svc/GetEventList"

    try:
        resp = _session.get(url, timeout=15)
        if resp.status_code != 200:
            return results

        data = resp.json()
        events = data.get("GetEventListResult", [])

        for ev in events:
            if not isinstance(ev, dict):
                continue

            event_title = ev.get("Title", "")
            start_date = ev.get("StartDate", "")
            event_date = ""
            if start_date:
                try:
                    dt = datetime.strptime(start_date[:10], "%m/%d/%Y")
                    event_date = dt.strftime("%Y%m%d")
                except ValueError:
                    pass

            for att in ev.get("Attachments", []):
                if not isinstance(att, dict):
                    continue

                att_url = att.get("Url", "")
                att_title = att.get("Title", "")
                att_ext = (att.get("Extension", "") or "").upper()

                if not att_url or att_ext != "PDF":
                    continue

                title_lower = att_title.lower()
                is_remarks = any(kw in title_lower for kw in [
                    "prepared remarks", "remarks", "transcript",
                    "earnings call", "conference call",
                ])

                if is_remarks:
                    # Extract quarter from event title
                    q_match = re.search(r'(Q\d)\s*FY(\d{2})', event_title, re.IGNORECASE)
                    quarter_str = f"{q_match.group(1)} FY{q_match.group(2)}" if q_match else ""

                    results.append({
                        "title": f"{ticker.upper()} {event_title} - {att_title}",
                        "url": att_url,
                        "source": "IR (Q4)",
                        "ticker": ticker.upper(),
                        "quarter": quarter_str,
                        "date": event_date,
                        "_is_pdf": True,
                    })

    except Exception:
        pass

    return results


# ---- DDG Search for Transcripts ----

def _ddg_search_transcripts(domain: str, ticker: str, company_name: str) -> list[dict]:
    """DuckDuckGo search for earnings call transcripts across multiple sources."""
    if not company_name:
        return []

    results = []
    search_terms = company_name or ticker
    tk = ticker.upper()

    # Phase 1: Broad web search (catches MarketBeat, Investing.com, GuruFocus, etc.)
    broad_queries = [
        f'{search_terms} {ticker} earnings call transcript Q',
        f'{search_terms} quarterly earnings call transcript',
    ]
    for query in broad_queries:
        _do_ddg_transcript_query(query, results, tk)
        if results:
            break

    # Phase 2: Site-scoped searches for known quality sources
    site_queries = [
        f'site:seekingalpha.com {search_terms} earnings call transcript',
        f'site:fool.com {search_terms} earnings call transcript',
        f'site:marketbeat.com {search_terms} earnings call transcript',
        f'site:investing.com {search_terms} earnings call transcript',
        f'site:gurufocus.com {search_terms} {ticker} earnings',
    ]
    for query in site_queries:
        _do_ddg_transcript_query(query, results, tk)

    return results


def _do_ddg_transcript_query(query: str, results: list[dict], ticker_label: str):
    """Execute a single DDG query for transcripts and append matching results."""
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
        resp = _session.get(url, timeout=30)
        if resp.status_code != 200:
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        for item in soup.select(".result")[:10]:
            link = item.select_one(".result__a")
            if not link or not link.get("href"):
                continue

            href = link["href"]
            real_url = _extract_ddg_url(href)
            if not real_url:
                continue

            title = link.get_text(strip=True)
            title_lower = title.lower()
            url_lower = real_url.lower()

            is_transcript = any(kw in title_lower for kw in [
                "transcript", "earnings call", "conference call",
                "prepared remarks", "q&a",
            ])
            is_earnings = any(kw in title_lower for kw in [
                "earnings", "quarterly results", "q1", "q2", "q3", "q4",
                "financial results", "investor call",
            ])

            if (is_transcript or is_earnings) and not url_lower.endswith(".pdf"):
                results.append({
                    "title": f"{ticker_label} {title[:80]}",
                    "url": real_url,
                    "source": "Web Search",
                    "ticker": ticker_label,
                    "quarter": "",
                })

        time.sleep(0.5)

    except Exception:
        pass


def _extract_ddg_url(href: str) -> str:
    """Extract real URL from DuckDuckGo redirect."""
    from urllib.parse import parse_qs, urlparse as _urlparse
    if "duckduckgo.com" not in href.lower():
        return href
    parsed = _urlparse(href)
    params = parse_qs(parsed.query)
    if "uddg" in params:
        return params["uddg"][0]
    return ""


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

        # Try ±1 day offsets, primary quarter/year, ALL URL patterns
        found_for_date = False
        for offset in (0, -1, 1):
            if found_for_date:
                break
            var_dt = dt + timedelta(days=offset)
            var_date = var_dt.strftime("%Y%m%d")
            urls = _motley_fool_urls(ticker, company_name, var_date, q, fy)
            for url in urls:
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
                        found_for_date = True
                        break  # Found one for this date

    # Step 2: Try Q4 Events API for prepared remarks
    # Build IR domains from multiple sources (don't rely solely on Yahoo)
    ir_domains = []
    # From Yahoo Finance website
    company_website = ""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.info
        company_website = info.get("website", "")
        if company_website:
            from urllib.parse import urlparse
            parsed = urlparse(company_website)
            domain = (parsed.netloc or parsed.path).replace("www.", "").strip("/")
            if domain:
                ir_domains.append(f"investor.{domain}")
                ir_domains.append(f"ir.{domain}")
    except Exception:
        pass

    # Ticker-based patterns (always try these)
    tk = ticker.lower()
    ir_domains.append(f"investor.{tk}.com")
    ir_domains.append(f"ir.{tk}.com")

    # Company name pattern
    if company_name:
        name_slug = re.sub(r'[^a-z0-9]', '', company_name.lower())[:20]
        ir_domains.append(f"investor.{name_slug}.com")
        ir_domains.append(f"ir.{name_slug}.com")

    # Try all IR domains until one works
    seen_ir = set()
    for ir_domain in ir_domains:
        if ir_domain in seen_ir:
            continue
        seen_ir.add(ir_domain)
        q4_trans = _try_q4_transcripts(ir_domain, ticker)
        if q4_trans:
            for r in q4_trans:
                found.append(r)
            break  # Found working Q4 endpoint

    # Step 3: Scrape Stock Analysis archive
    sa_results = _stockanalysis_transcripts(ticker)
    for r in sa_results:
        found.append(r)

    # Step 4: DDG search fallback (for non-US / non-SA companies)
    if not found:
        ddg_domain = ""
        if company_website:
            from urllib.parse import urlparse as _up
            p = _up(company_website)
            ddg_domain = (p.netloc or p.path).replace("www.", "").strip("/")
        ddg_results = _ddg_search_transcripts(ddg_domain, ticker, company_name)
        for r in ddg_results:
            found.append(r)

    # Step 5: Deduplicate by URL
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

def _extract_transcript_text(html_content: str) -> str | None:
    """Parse and extract transcript content from HTML.

    Returns the transcript text, or None if not found.
    """
    soup = BeautifulSoup(html_content, "html.parser")

    # Try known article body selectors
    selectors = [
        "#transcript-panel-full",                           # Stock Analysis
        "div.space-y-6.text-base",                         # Stock Analysis (inner)
        "div.article-body",                                # Motley Fool
        "div.prose",                                       # Generic
        "div.post-content",                                # AlphaStreet, Insider Monkey
        "div.article-content",                             # Benzinga, MarketBeat
        "section.article-body",
        "div.transcript-body",
        "div[itemprop='articleBody']",
        "article",
        "div.entry-content",
        "main article",
        "div.WYSIWYG",                                     # Investing.com
        "div.article_wrapper",
        "div.content-section",
        "#article_text",
        "div.article-text",
        "#content-body",
    ]

    content_div = None
    for sel in selectors:
        content_div = soup.select_one(sel)
        if content_div:
            break

    if not content_div:
        # Fallback 1: find divs with "Operator" (conference call signature)
        candidates = []
        for div in soup.find_all("div"):
            text = div.get_text(strip=True)
            if text.count("Operator") >= 2 and len(text) > 2000:
                candidates.append((len(text), div))
        if candidates:
            candidates.sort(key=lambda x: x[0])
            content_div = candidates[0][1]

    if not content_div:
        # Fallback 2: find largest text block with transcript keywords
        keywords = ["earnings call", "conference call", "transcript",
                     "ladies and gentlemen", "good morning", "good afternoon",
                     "welcome to the", "thank you for joining",
                     "i'd like to", "my name is"]
        best_div = None
        best_score = 0
        for div in soup.find_all(["div", "article", "section"]):
            text = div.get_text(strip=True)
            if len(text) < 2000:
                continue
            score = sum(1 for kw in keywords if kw in text.lower())
            if score > best_score:
                best_score = score
                best_div = div
        if best_div and best_score >= 2:
            content_div = best_div

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
            url = trans["url"]
            is_pdf = url.lower().endswith(".pdf") or trans.get("_is_pdf")

            if is_pdf:
                # PDF transcript (e.g., Q4 prepared remarks) — download directly
                resp = _session.get(url, timeout=60, allow_redirects=True)
                if resp.status_code == 200 and len(resp.content) > 5000:
                    safe_title = _sanitize(trans["title"])[:60]
                    source_tag = trans.get("source", "Web").replace(" ", "_")
                    filename = f"{ticker}_{source_tag}_{safe_title}.pdf"
                    filepath = output_dir / filename
                    filepath.write_bytes(resp.content)
                    downloaded.append(filepath)
            else:
                # HTML transcript — fetch page and try to extract text
                try:
                    resp = _session.get(url, timeout=60, allow_redirects=True)
                    if resp.status_code != 200:
                        continue
                    html_content = resp.text
                except Exception:
                    continue

                text = _extract_transcript_text(html_content)
                safe_title = _sanitize(trans["title"])[:60]
                source_tag = trans.get("source", "Web").replace(" ", "_")

                if text and len(text) > 500:
                    filename = f"{ticker}_{source_tag}_{safe_title}.txt"
                    filepath = output_dir / filename
                    content = (
                        f"Title: {trans['title']}\n"
                        f"Ticker: {trans['ticker']}\n"
                        f"Source: {trans['source']}\n"
                        f"URL: {url}\n"
                        + "=" * 60 + "\n\n" + text
                    )
                    filepath.write_text(content, encoding="utf-8")
                    downloaded.append(filepath)
                else:
                    # Text extraction failed — save full HTML page for browser viewing
                    filename = f"{ticker}_{source_tag}_{safe_title}.html"
                    filepath = output_dir / filename
                    filepath.write_text(html_content, encoding="utf-8")
                    downloaded.append(filepath)
        except Exception:
            continue

        time.sleep(0.3)

    return downloaded


def _sanitize(name: str) -> str:
    """Clean filename of invalid characters."""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    return name.strip().strip(".")[:200]
