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


def _fetch_filing_exhibits(cik: str, acc_no: str) -> list[dict]:
    """
    发现filing中的exhibit文档（仅使用 index.json）。

    index.json 中每个 item 包含 name, type, size 字段，
    type 字段可区分 exhibit（EX-*）和主文档。

    Returns:
        [{type, filename, url, _is_ex}]
    """
    import json

    cik_clean = cik.lstrip("0")
    acc_no_dashes = acc_no.replace("-", "")
    base_url = (
        f"https://www.sec.gov/Archives/edgar/data"
        f"/{cik_clean}/{acc_no_dashes}"
    )

    json_url = f"{base_url}/index.json"
    all_items = []
    try:
        resp = session.get(json_url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            data = json.loads(resp.text)
            all_items = data.get("directory", {}).get("item", [])
    except Exception:
        pass

    if not all_items:
        return []

    exhibits = []
    for item in all_items:
        fname = item.get("name", "")
        dtype = item.get("type", "")
        if not fname:
            continue

        fname_lower = fname.lower()

        if any(skip in fname_lower for skip in [
            "-index.", "-index-headers.", ".xsd", "_def.xml",
            "_lab.xml", "_pre.xml", "_cal.xml", "FilingSummary.xml",
            "MetaLinks.json", "Show.js", "report.css", "R1.htm",
            ".xbrl.zip", "idea", ".gif", ".jpg", ".jpeg", ".png",
        ]):
            continue

        exhibits.append({
            "type": dtype,
            "filename": fname,
            "url": f"{base_url}/{fname}",
            "_is_ex": _is_exhibit_from_type(dtype, fname),
        })

    return exhibits


def _is_exhibit_from_type(dtype: str, fname: str) -> bool:
    """判断 index.json 中的条目是否为 exhibit（非主文档、非 XBRL）"""
    if not dtype:
        return bool(re.search(r'ex\d+|exhibit|ex-|ex_', fname.lower()))
    dtype_upper = dtype.upper()
    if dtype_upper.startswith("EX-"):
        return True
    if dtype_upper in ("GRAPHIC", "XML", "PDF", "ZIP"):
        return False
    if re.match(r'^\d', dtype_upper):
        return False
    if dtype_upper in ("8-K", "6-K", "10-K", "10-Q", "20-F", "40-F", "S-1", "F-1"):
        return False
    return bool(re.search(r'ex\d+|exhibit|ex-|ex_', fname.lower()))


def download_filings(
    filing_df,
    output_dir,
    progress_callback=None,
    keyword_config=None,
    content_filter_config=None,
):
    """
    下载SEC公告文件。

    对于有keyword_config的doc_type，会下载该filing中所有exhibit文档
    （因为SEC的exhibit描述不包含语义信息，无法通过关键词筛选）。
    如果没有找到exhibit，则回退到下载主文档。

    对于有content_filter_config的doc_type，下载后会检查文件内容是否
    包含财务关键词，不包含的6-K会被过滤掉。

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
        content_filter = content_filter_config.get(doc_type, "") if content_filter_config else ""

        acc_no_dashes = acc.replace("-", "")
        cik_clean = cik.lstrip("0")

        if keyword:
            # ===== EXHIBIT MODE: 下载所有 exhibit 文档 =====
            all_exhibits = _fetch_filing_exhibits(cik, acc)

            # 筛选所有 exhibit（不按关键词过滤，因为SEC描述无语义信息）
            exhibits = [d for d in all_exhibits if d["_is_ex"]]

            for exhibit in exhibits:
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

            if not exhibits and primary_doc:
                # 没有找到exhibit，回退到主文档
                _download_primary(
                    cik_clean, acc_no_dashes, primary_doc,
                    ticker, form_type, date_str,
                    output_dir, downloaded, idx, total, progress_callback,
                )
        else:
            # ===== PRIMARY MODE: 下载主文档 =====
            if not all([cik_clean, acc, primary_doc]):
                continue

            filepath = _download_primary(
                cik_clean, acc_no_dashes, primary_doc,
                ticker, form_type, date_str,
                output_dir, downloaded, idx, total, progress_callback,
            )

            # 6-K内容过滤：检查是否包含财务数据
            if content_filter and filepath and form_type in ("6-K", "6-K/A"):
                if not _check_financial_content(filepath, content_filter):
                    try:
                        filepath.unlink()
                    except Exception:
                        pass
                    if filepath in downloaded:
                        downloaded.remove(filepath)
                    if progress_callback:
                        progress_callback(idx, total,
                            f"{ticker} 6-K skipped (non-financial)")

    return downloaded


def _check_financial_content(filepath: Path, filter_pattern: str) -> bool:
    """检查下载的HTML/PDF文件是否包含财务关键词"""
    try:
        import re as _re
        pattern = _re.compile(filter_pattern, _re.IGNORECASE)

        if filepath.suffix.lower() in (".htm", ".html", ".xhtml"):
            text = filepath.read_text(encoding="utf-8", errors="ignore")
            # 去掉HTML标签，只检查文本内容
            text = _re.sub(r"<[^>]+>", " ", text)
            text = _re.sub(r"\s+", " ", text)
            # 取前50000字符检查
            return bool(pattern.search(text[:50000]))
        elif filepath.suffix.lower() == ".pdf":
            # PDF需要特殊处理，暂时跳过过滤（保留PDF）
            return True
        else:
            # 其他格式保留
            return True
    except Exception:
        return True  # 出错时保留文件


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
        return filepath

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(doc_url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and len(resp.content) > 1000:
                filepath.write_bytes(resp.content)
                downloaded.append(filepath)
                return filepath
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 * (attempt + 1))

    time.sleep(REQUEST_DELAY)
    return None


def _safe_name(name: str) -> str:
    """清理文件名"""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    return name.strip().strip(".")[:200]
