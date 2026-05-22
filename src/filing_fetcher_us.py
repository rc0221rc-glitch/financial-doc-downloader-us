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

        # 按 form type 过滤
        type_df = df[df["form"].isin(forms)].copy()

        # 如果有关键词，进一步过滤（用于8-K exhibits）
        if keyword and not type_df.empty:
            type_df = type_df[type_df["form"].isin(["8-K", "8-K/A"])].copy()

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


def download_filings(
    filing_df: pd.DataFrame,
    output_dir: Path,
    progress_callback=None,
) -> list[Path]:
    """
    下载SEC公告文件。

    Returns:
        成功下载的文件路径列表
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    total = len(filing_df)

    for idx, (_, row) in enumerate(filing_df.iterrows()):
        cik = str(row.get("cik", "")).lstrip("0")
        acc = str(row.get("accession_number", ""))
        primary_doc = str(row.get("primary_document", ""))
        form_type = str(row.get("form", ""))
        ticker = str(row.get("ticker", ""))
        date_str = str(row.get("filing_date", ""))[:10]

        if not all([cik, acc, primary_doc]):
            continue

        # SEC 文档URL
        acc_no_dashes = acc.replace("-", "")
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_no_dashes}/{primary_doc}"

        filename = _safe_name(f"{ticker}_{form_type}_{date_str}_{primary_doc.split('/')[-1]}")
        filepath = output_dir / filename

        if progress_callback:
            progress_callback(idx, total, filename)

        if filepath.exists():
            downloaded.append(filepath)
            continue

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

    return downloaded


def _safe_name(name: str) -> str:
    """清理文件名"""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    return name.strip().strip(".")[:200]
