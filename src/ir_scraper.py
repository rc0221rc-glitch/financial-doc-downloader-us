"""Company IR page scraper for earnings presentations.

Strategy:
1. Get company website from Yahoo Finance
2. Try Q4 Inc Events API (used by ~30% of public companies)
3. Try common IR page URL patterns
4. Use DuckDuckGo site-scoped search for presentations
5. Download matching PDFs
"""

import re
import time
from pathlib import Path
from datetime import datetime
from urllib.parse import quote, urlparse

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


def _get_company_info(ticker: str) -> tuple[str, str, str]:
    """Get company name, website domain, and long name from Yahoo Finance."""
    name = ""
    website = ""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.info
        name = info.get("longName", info.get("shortName", ""))
        website = info.get("website", "")
    except Exception:
        pass
    return name, website, name


def _extract_domain(url: str) -> str:
    """Extract domain from URL (e.g., 'https://www.apple.com/' -> 'apple.com')."""
    if not url:
        return ""
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    domain = domain.replace("www.", "")
    return domain.strip("/")


def _try_q4_events(ir_domain: str, ticker: str, target_dates: list[str]) -> list[dict]:
    """Try Q4 Inc Events API for earnings presentations and prepared remarks.

    The Event.svc/GetEventList endpoint returns events with Attachments
    containing presentation slides (PDF) and prepared remarks.

    Returns:
        [{title, url, type, date}]
    """
    results = []
    url = f"https://{ir_domain}/feed/Event.svc/GetEventList"

    try:
        resp = _session.get(url, timeout=15)
        if resp.status_code != 200:
            return results

        data = resp.json()
        events = data.get("GetEventListResult", [])

        # Build date filter: if target_dates provided, match within ±3 days
        if target_dates:
            target_date_objs = set()
            for d in target_dates:
                try:
                    target_date_objs.add(datetime.strptime(d, "%Y%m%d"))
                except ValueError:
                    pass
        else:
            target_date_objs = None

        for ev in events:
            if not isinstance(ev, dict):
                continue

            event_title = ev.get("Title", "")
            start_date = ev.get("StartDate", "")
            # Parse date from format "MM/DD/YYYY HH:MM:SS"
            event_date = ""
            event_dt = None
            if start_date:
                try:
                    event_dt = datetime.strptime(start_date[:10], "%m/%d/%Y")
                    event_date = event_dt.strftime("%Y%m%d")
                except ValueError:
                    pass

            # Date filter: match within ±3 days if target dates provided
            if target_date_objs is not None and event_dt is not None:
                matched = False
                for td in target_date_objs:
                    if abs((event_dt - td).days) <= 3:
                        matched = True
                        break
                if not matched:
                    continue

            # Check attachments for presentation/remarks PDFs
            for att in ev.get("Attachments", []):
                if not isinstance(att, dict):
                    continue

                att_url = att.get("Url", "")
                att_title = att.get("Title", "")
                att_type = att.get("Type", "")
                att_ext = (att.get("Extension", "") or "").upper()

                if not att_url or att_ext != "PDF":
                    continue

                url_lower = att_url.lower()
                title_lower = att_title.lower()

                # Classify attachment
                is_presentation = any(kw in title_lower for kw in [
                    "presentation", "slides", "earnings slides",
                    "earnings presentation",
                ])
                is_prepared = any(kw in title_lower for kw in [
                    "prepared remarks", "prepared remarks",
                ])

                if is_presentation or is_prepared:
                    doc_type = "presentation" if is_presentation else "transcript"
                    results.append({
                        "title": f"{ticker.upper()} {event_title} - {att_title}",
                        "url": att_url,
                        "type": doc_type,
                        "date": event_date,
                    })
                elif att_type == "Presentation":
                    # Presentation type but title might not match keywords
                    results.append({
                        "title": f"{ticker.upper()} {event_title} - {att_title}",
                        "url": att_url,
                        "type": "presentation",
                        "date": event_date,
                    })

            # Also check DocumentPath for events where attachments are empty
            doc_path = ev.get("DocumentPath", "")
            if doc_path and doc_path.lower().endswith(".pdf") and not ev.get("Attachments"):
                # Skip 10-K/10-Q filings
                if not any(kw in doc_path.lower() for kw in ["10-q", "10-k", "10k", "10q", "as-filed"]):
                    results.append({
                        "title": f"{ticker.upper()} {event_title}",
                        "url": doc_path,
                        "type": "presentation",
                        "date": event_date,
                    })

    except Exception:
        pass

    return results


def _try_ir_urls(domain: str, company_name: str, ticker: str) -> str:
    """Try common IR page URL patterns to find the IR site.

    Returns the working IR page URL, or empty string.
    """
    if not domain:
        return ""

    # Remove 'www.' for subdomain construction
    clean_domain = domain.replace("www.", "")

    candidates = [
        f"https://investor.{clean_domain}",
        f"https://ir.{clean_domain}",
        f"https://{clean_domain}/investors",
        f"https://{clean_domain}/investor-relations",
        f"https://{clean_domain}/investor",
        f"https://investors.{clean_domain}",
    ]

    for url in candidates:
        try:
            resp = _session.get(url, timeout=15, allow_redirects=True)
            if resp.status_code == 200:
                return resp.url  # Return resolved URL
        except Exception:
            continue

    return ""


def _scrape_ir_page(ir_url: str, ticker: str) -> list[dict]:
    """Directly scrape the IR page for presentation PDF links.

    Looks for links on the IR page that point to PDFs with presentation-related
    link text or URLs.
    """
    results = []
    try:
        resp = _session.get(ir_url, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return results

        soup = BeautifulSoup(resp.text, "html.parser")
        seen_urls = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            url_lower = href.lower()
            text_lower = text.lower()

            # Only interested in PDFs
            if not url_lower.endswith(".pdf"):
                continue

            # Check if presentation-related
            pres_keywords = [
                "presentation", "slides", "deck", "earnings",
                "quarterly", "supplement", "investor",
            ]
            has_kw = any(kw in url_lower or kw in text_lower for kw in pres_keywords)
            if not has_kw:
                continue

            # Skip SEC filings
            if any(kw in url_lower for kw in ["10-q", "10-k", "10k", "10q", "as-filed", "exhibit"]):
                continue

            # Resolve relative URLs
            if href.startswith("/"):
                from urllib.parse import urljoin
                full_url = urljoin(ir_url, href)
            elif href.startswith("http"):
                full_url = href
            else:
                full_url = urljoin(ir_url, href)

            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            # Determine type from link text
            is_remarks = any(kw in text_lower for kw in ["prepared remarks", "remarks", "transcript"])
            doc_type = "transcript" if is_remarks else "presentation"

            results.append({
                "title": f"{ticker.upper()} {text}" if text else f"{ticker.upper()} Presentation",
                "url": full_url,
                "type": doc_type,
                "date": "",
            })

    except Exception:
        pass

    return results


def _search_ir_for_presentations(ir_url: str, ticker: str, company_name: str) -> list[dict]:
    """Use DuckDuckGo to search the IR site for presentation PDFs."""
    if not ir_url:
        return []

    domain = _extract_domain(ir_url)
    if not domain:
        return []

    results = []
    queries = [
        f"site:{domain} presentation earnings PDF",
        f"site:{domain} quarterly presentation slides",
    ]

    for query in queries[:2]:
        try:
            url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
            resp = _session.get(url, timeout=30)
            if resp.status_code != 200:
                continue

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
                url_lower = real_url.lower()

                has_pres = any(kw in url_lower for kw in [
                    "presentation", "slides", "deck", "earnings",
                ])
                is_pdf = url_lower.endswith(".pdf")

                if has_pres or (is_pdf and "quarterly" in title.lower()):
                    results.append({
                        "title": title,
                        "url": real_url,
                        "type": "presentation",
                        "date": "",
                    })

            time.sleep(0.5)

        except Exception:
            continue

    return results


def _extract_ddg_url(href: str) -> str:
    """Extract real URL from DuckDuckGo redirect."""
    from urllib.parse import parse_qs, urlparse
    if "duckduckgo.com" not in href.lower():
        return href
    parsed = urlparse(href)
    params = parse_qs(parsed.query)
    if "uddg" in params:
        return params["uddg"][0]
    return ""


def find_presentations(ticker: str, company_name: str = "", target_dates: list[str] = None) -> list[dict]:
    """Find earnings presentations from company IR pages.

    Args:
        ticker: Stock ticker
        company_name: Full company name (auto-fetched if empty)
        target_dates: Optional list of YYYYMMDD dates to match

    Returns:
        [{title, url, type, date}]
    """
    if not company_name:
        company_name, website, _ = _get_company_info(ticker)
    else:
        _, website, _ = _get_company_info(ticker)

    domain = _extract_domain(website)
    all_results = []

    # Step 1: Try Q4 Events API on multiple domain patterns
    ir_domains_to_try = []
    if domain:
        clean = domain.replace("www.", "")
        ir_domains_to_try.append(f"investor.{clean}")
        ir_domains_to_try.append(f"ir.{clean}")
        ir_domains_to_try.append(f"investors.{clean}")
        # Also try ticker-based domain (common for Q4)
        ir_domains_to_try.append(f"investor.{ticker.lower()}.com")

    # Deduplicate while preserving order
    seen_domains = set()
    for d in ir_domains_to_try:
        if d not in seen_domains:
            seen_domains.add(d)
            q4_results = _try_q4_events(d, ticker, target_dates or [])
            if q4_results:
                all_results.extend(q4_results)
                break  # Found a working Q4 endpoint

    # Step 2: Find the actual IR page
    ir_url = _try_ir_urls(domain, company_name, ticker)

    # If no IR URL found from domain, try ticker-based patterns
    if not ir_url:
        extra_candidates = [
            f"https://investor.{ticker.lower()}.com",
            f"https://ir.{ticker.lower()}.com",
        ]
        for url in extra_candidates:
            try:
                resp = _session.get(url, timeout=15, allow_redirects=True)
                if resp.status_code == 200:
                    ir_url = resp.url
                    break
            except Exception:
                continue

    # Step 3: Directly scrape IR page for presentation PDFs
    if ir_url:
        scrape_results = _scrape_ir_page(ir_url, ticker)
        all_results.extend(scrape_results)

    # Step 4: DDG search (always run as additional fallback)
    if ir_url:
        search_results = _search_ir_for_presentations(ir_url, ticker, company_name)
        all_results.extend(search_results)
    elif domain:
        # Even without IR URL, try site-scoped search on the domain
        search_results = _search_ir_for_presentations(f"https://{domain}", ticker, company_name)
        all_results.extend(search_results)

    # Deduplicate by URL
    seen = set()
    unique = []
    for r in all_results:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)

    return unique


def download_presentations(
    ticker: str,
    output_dir: Path,
    progress_callback=None,
    company_name: str = "",
    target_dates: list[str] = None,
) -> list[Path]:
    """Find and download earnings presentations from company IR pages.

    Args:
        ticker: Stock ticker symbol
        output_dir: Directory to save presentations
        progress_callback: Optional (current, total, msg)
        company_name: Full company name
        target_dates: Optional list of YYYYMMDD dates

    Returns:
        List of downloaded file paths
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    if progress_callback:
        progress_callback(0, 1, f"Getting company info for {ticker}...")

    if not company_name:
        company_name, website, _ = _get_company_info(ticker)
    else:
        _, website, _ = _get_company_info(ticker)

    domain = _extract_domain(website)
    if progress_callback:
        if domain:
            progress_callback(0, 1, f"IR domain: {domain} — searching for presentations...")
        else:
            progress_callback(0, 1, f"No website from Yahoo, trying fallback search...")

    presentations = find_presentations(ticker, company_name, target_dates)

    if not presentations:
        if progress_callback:
            msg = f"No presentations found for {ticker}"
            if not domain:
                msg += " (company website not available from Yahoo Finance)"
            progress_callback(0, 1, msg)
        return downloaded

    total = len(presentations)
    for i, pres in enumerate(presentations):
        if progress_callback:
            progress_callback(i, total, f"[IR] {pres['title'][:50]}")

        try:
            url = pres["url"]
            resp = _session.get(url, timeout=60, allow_redirects=True)
            if resp.status_code == 200 and len(resp.content) > 5000:
                safe_title = _sanitize(pres["title"])[:60]
                doc_type = pres.get("type", "")
                # Use different extensions based on type
                if url.lower().endswith(".pdf"):
                    filename = f"{ticker}_IR_{safe_title}.pdf"
                else:
                    filename = f"{ticker}_IR_{safe_title}.html"

                # Separate transcripts from presentations
                if doc_type == "transcript":
                    # Save as text (try to extract from PDF or save link)
                    filename = f"{ticker}_IR_Transcript_{safe_title}.pdf"
                    filepath = output_dir / filename
                    filepath.write_bytes(resp.content)
                else:
                    filepath = output_dir / filename
                    filepath.write_bytes(resp.content)

                downloaded.append(filepath)
        except Exception:
            continue

        time.sleep(0.3)

    return downloaded


def _sanitize(name: str) -> str:
    """Clean filename of invalid characters."""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    return name.strip().strip(".")[:200]
