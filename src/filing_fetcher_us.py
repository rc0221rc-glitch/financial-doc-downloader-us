"""SEC EDGAR 公告查询与下载"""

import re
import time
from pathlib import Path

import requests
import pandas as pd

from config import (
    DOC_TYPE_MAP,
    SEC_USER_AGENT,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    MAX_RETRIES,
    MAX_FILINGS_PER_TYPE,
    FORM_NAMES,
)

session = requests.Session()
session.headers.update({"User-Agent": SEC_USER_AGENT})


def fetch_filing_list(
    cik: str,
    ticker: str,
    doc_types: list[str],
    start_date: str,
    end_date: str,
    progress_callback=None,
) -> pd.DataFrame:
    """
    从SEC EDGAR查询公司公告列表。

    Args:
        cik: 10位CIK编号
        ticker: 股票代码
        doc_types: 用户选中的文件类型列表
        start_date: YYYYMMDD
        end_date: YYYYMMDD
        progress_callback: 可选 (current, total, msg)

    Returns:
        DataFrame，列: ticker, company, form, filing_date, accession_number,
              primary_document, doc_type, url
    """
    # 获取所有 filings
    submissions = _get_company_submissions(cik)
    if submissions is None:
        return pd.DataFrame()

    recent = submissions.get("filings", {}).get("recent", {})
    if not recent:
        return pd.DataFrame()

    # 构建 DataFrame
    df = pd.DataFrame({
        "accession_number": recent.get("accessionNumber", []),
        "filing_date": recent.get("filingDate", []),
        "report_date": recent.get("reportDate", []),
        "form": recent.get("form", []),
        "primary_document": recent.get("primaryDocument", []),
        "cik": [cik] * len(recent.get("form", [])),
        "ticker": [ticker] * len(recent.get("form", [])),
    })

    # 也加载 older filings
    older_files = _get_older_filings(cik)
    if older_files:
        older_df = pd.DataFrame(older_files)
        older_df["cik"] = cik
        older_df["ticker"] = ticker
        df = pd.concat([df, older_df], ignore_index=True)

    if df.empty:
        return pd.DataFrame()

    # 过滤日期范围
    df["filing_date_parsed"] = pd.to_datetime(df["filing_date"], errors="coerce")
    sd = pd.Timestamp(start_date)
    ed = pd.Timestamp(end_date) + pd.Timedelta(days=1)
    df = df[(df["filing_date_parsed"] >= sd) & (df["filing_date_parsed"] < ed)]
    df = df.drop(columns=["filing_date_parsed"])

    # 按文件类型过滤
    all_frames = []
    total = len(doc_types)

    for i, doc_type in enumerate(doc_types):
        if progress_callback:
            progress_callback(i + 1, total, f"筛选: {doc_type}")

        config = DOC_TYPE_MAP.get(doc_type, {})
        forms = config.get("forms", [])
        keyword = config.get("keyword", "")

        # 按 form type 过滤（keyword匹配移到了download_filings阶段）
        type_df = df[df["form"].isin(forms)].copy()

        if not type_df.empty:
            type_df["doc_type"] = doc_type
            all_frames.append(type_df.head(MAX_FILINGS_PER_TYPE))

    if not all_frames:
        return pd.DataFrame()

    result = pd.concat(all_frames, ignore_index=True)
    result = result.drop_duplicates(subset=["accession_number", "form"])
    result = result.sort_values("filing_date", ascending=False)
    result["form_name"] = result["form"].map(FORM_NAMES).fillna(result["form"])

    return result.reset_index(drop=True)


def _get_company_submissions(cik: str) -> dict | None:
    """获取公司SEC submissions数据"""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 * (attempt + 1))
    return None


def _get_older_filings(cik: str) -> list[dict] | None:
    """获取更早期的 filings（SEC submissions API 只返回最近的，older filings 需要单独查）"""
    submissions = _get_company_submissions(cik)
    if not submissions:
        return None

    older_files = submissions.get("filings", {}).get("files", [])
    all_older = []
    for file_info in older_files:
        name = file_info.get("name", "")
        if not name:
            continue
        url = f"https://data.sec.gov/submissions/{name}"
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                all_older.extend(data)
        except Exception:
            continue
        time.sleep(0.2)

    return all_older


def _fetch_index_page(cik: str, acc_no: str) -> str | None:
    """获取SEC filing index page（列出所有文档的HTML页面）"""
    cik_clean = cik.lstrip("0")
    acc_no_dashes = acc_no.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_clean}"
        f"/{acc_no_dashes}/{acc_no_dashes}-index.html"
    )
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and len(resp.content) > 500:
                return resp.text
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 * (attempt + 1))
    return None


def _parse_filing_documents(html: str, cik: str, acc_no: str) -> list[dict]:
    """解析filing index page，提取所有文档（包括exhibits）"""
    from lxml import html as lhtml

    try:
        tree = lhtml.fromstring(html)
    except Exception:
        return []

    tables = tree.xpath("//table[contains(@class,'tableFile')]")
    if not tables:
        return []

    docs = []
    for table in tables:
        rows = table.xpath(".//tr")
        for row in rows:
            cells = row.xpath(".//td")
            if len(cells) < 4:
                continue

            doc_type = cells[3].text_content().strip()
            description = cells[1].text_content().strip()

            links = cells[2].cssselect("a")
            if not links:
                continue
            filename = links[0].text_content().strip()
            href = links[0].get("href", "")
            if not href:
                continue

            cik_clean = cik.lstrip("0")
            acc_no_dashes = acc_no.replace("-", "")
            base_url = (
                f"https://www.sec.gov/Archives/edgar/data"
                f"/{cik_clean}/{acc_no_dashes}/"
            )
            if href.startswith("http"):
                url = href
            else:
                url = base_url + (href.split("/")[-1] if "/" in href else href)

            docs.append({
                "type": doc_type,
                "description": description,
                "filename": filename,
                "url": url,
            })

    return docs


def download_filings(
    filing_df: pd.DataFrame,
    output_dir: Path,
    progress_callback=None,
    keyword_config: dict[str, str] | None = None,
) -> list[Path]:
    """
    下载SEC公告文件。

    对于有keyword_config的doc_type，会从filing index page中查找
    匹配关键词的EX-* exhibit并下载（用于业绩演示材料、电话会纪要等）。

    Returns:
        成功下载的文件路径列表
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    total = len(filing_df)

    for idx, (_, row) in enumerate(filing_df.iterrows()):
        cik = str(row.get("cik", ""))
        acc = str(row.get("accession_number", ""))
        primary_doc = str(row.get("primary_document", ""))
        form_type = str(row.get("form", ""))
        ticker = str(row.get("ticker", ""))
        date_str = str(row.get("filing_date", ""))[:10]
        doc_type = str(row.get("doc_type", ""))
        keyword = keyword_config.get(doc_type, "") if keyword_config else ""

        acc_no_dashes = acc.replace("-", "")
        cik_clean = cik.lstrip("0")

        if keyword:
            # ===== EXHIBIT MODE: 查找匹配关键词的EX-* exhibit =====
            html = _fetch_index_page(cik, acc)
            if html:
                all_docs = _parse_filing_documents(html, cik, acc)
                pattern = re.compile(keyword, re.IGNORECASE)
                matching = [
                    d for d in all_docs
                    if d["type"].upper().startswith("EX-")
                    and pattern.search(d["description"])
                ]
                for exhibit in matching:
                    safe_fname = _safe_name(
                        f"{ticker}_{form_type}_{date_str}"
                        f"_{exhibit['type']}_{exhibit['filename']}"
                    )
                    filepath = output_dir / safe_fname

                    if progress_callback:
                        progress_callback(idx, total,
                            f"{ticker} {exhibit['type']}: {exhibit['filename'][:50]}")

                    if filepath.exists():
                        downloaded.append(filepath)
                        continue

                    for attempt in range(MAX_RETRIES):
                        try:
                            resp = session.get(exhibit["url"], timeout=REQUEST_TIMEOUT)
                            if resp.status_code == 200 and len(resp.content) > 1000:
                                filepath.write_bytes(resp.content)
                                downloaded.append(filepath)
                                break
                        except Exception:
                            if attempt < MAX_RETRIES - 1:
                                time.sleep(2 * (attempt + 1))

                    time.sleep(REQUEST_DELAY)

                if not matching and primary_doc:
                    # 没有匹配的exhibit，回退到主文档
                    _download_primary(
                        cik_clean, acc_no_dashes, primary_doc,
                        ticker, form_type, date_str,
                        output_dir, downloaded, idx, total, progress_callback,
                    )
            elif primary_doc:
                # index page获取失败，回退
                _download_primary(
                    cik_clean, acc_no_dashes, primary_doc,
                    ticker, form_type, date_str,
                    output_dir, downloaded, idx, total, progress_callback,
                )
        else:
            # ===== PRIMARY MODE: 下载主文档 =====
            if not all([cik_clean, acc, primary_doc]):
                continue
            _download_primary(
                cik_clean, acc_no_dashes, primary_doc,
                ticker, form_type, date_str,
                output_dir, downloaded, idx, total, progress_callback,
            )

    return downloaded


def _download_primary(
    cik: str, acc_no_dashes: str, primary_doc: str,
    ticker: str, form_type: str, date_str: str,
    output_dir: Path, downloaded: list, idx: int, total: int,
    progress_callback=None,
):
    """下载filing主文档（原有逻辑）"""
    doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dashes}/{primary_doc}"
    filename = _safe_name(f"{ticker}_{form_type}_{date_str}_{primary_doc.split('/')[-1]}")
    filepath = output_dir / filename

    if progress_callback:
        progress_callback(idx, total, filename)

    if filepath.exists():
        downloaded.append(filepath)
        return

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(doc_url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and len(resp.content) > 1000:
                filepath.write_bytes(resp.content)
                downloaded.append(filepath)
                break
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 * (attempt + 1))

    time.sleep(REQUEST_DELAY)


def _safe_name(name: str) -> str:
    """清理文件名"""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    return name.strip().strip(".")[:200]
