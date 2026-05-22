"""全球上市公司搜索：按简称或代码查找"""

import json
from pathlib import Path

import pandas as pd

_sec_cache: pd.DataFrame | None = None
_SEC_LIST_FILE = Path(__file__).parent / "stock_list_us.json"


def get_sec_list() -> pd.DataFrame:
    """获取SEC注册公司列表"""
    global _sec_cache
    if _sec_cache is not None:
        return _sec_cache
    with open(_SEC_LIST_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    df["ticker"] = df["ticker"].astype(str).str.upper()
    df["cik"] = df["cik"].astype(str).str.zfill(10)
    _sec_cache = df
    return df


def search_company(query: str, limit: int = 30) -> list[dict]:
    """
    全球公司搜索。SEC列表优先，Yahoo Finance补充。
    对于Yahoo找到的非SEC公司，自动查找其SEC ADR对应。
    """
    if not query or not query.strip():
        return []

    query = query.strip()
    results: dict[str, dict] = {}  # key: ticker or CIK

    sec_df = get_sec_list()
    upper_query = query.upper()

    # ---- 第一步：搜索本地SEC列表 ----
    sec_matches = pd.DataFrame()
    if query.upper() == query and len(query) <= 5:
        sec_matches = sec_df[sec_df["ticker"] == upper_query]
    if sec_matches.empty:
        sec_matches = sec_df[sec_df["name"].str.upper().str.contains(upper_query, na=False)]
    if sec_matches.empty:
        words = upper_query.split()
        for w in words:
            if len(w) >= 2:
                wm = sec_df[sec_df["name"].str.upper().str.contains(w, na=False)]
                if not wm.empty:
                    sec_matches = wm
                    break

    # key by ticker for SEC matches
    for _, row in sec_matches.head(limit).iterrows():
        key = f"sec_{row['ticker']}"
        if key not in results:
            results[key] = {
                "ticker": str(row["ticker"]),
                "name": str(row["name"]),
                "cik": str(row["cik"]),
                "exchange": "SEC",
                "source": "SEC",
                "_priority": 0,
            }

    # ---- 第二步：SEC EDGAR 全文搜索（找没有ticker的SEC注册公司） ----
    if len(results) < 3:
        try:
            sec_cik = _search_sec_edgar(upper_query)
            if sec_cik and f"sec_cik_{sec_cik}" not in results:
                results[f"sec_cik_{sec_cik}"] = {
                    "ticker": f"CIK:{sec_cik}",
                    "name": query,
                    "cik": sec_cik,
                    "exchange": "SEC(无ticker)",
                    "source": "SEC",
                    "_priority": 0,
                }
        except Exception:
            pass

    # ---- 第三步：Yahoo Finance 全球搜索 ----
    try:
        yf_results = _search_yahoo(query, limit)
        for yr in yf_results:
            yticker = yr.get("symbol", "")
            if not yticker:
                continue

            # 去掉已在SEC结果中的
            sec_key = f"sec_{yticker}"
            if sec_key in results:
                continue

            name = yr.get("shortname", yr.get("longname", ""))
            ykey = f"yf_{yticker}"
            if ykey not in results:
                # 尝试匹配SEC CIK
                cik = _find_cik_for_ticker(yticker, sec_df, name)
                results[ykey] = {
                    "ticker": yticker,
                    "name": name,
                    "cik": cik,
                    "exchange": yr.get("exchange", ""),
                    "source": "Yahoo",
                    "_priority": 1 if cik else 2,
                }

            # 如果Yahoo结果没有CIK，尝试通过公司名在SEC列表中找ADR
            if not results[ykey]["cik"] and name:
                adr = _find_adr_in_sec(name, sec_df, yticker)
                if adr:
                    results[ykey]["cik"] = adr
                    results[ykey]["_priority"] = 1
    except Exception:
        pass

    # 排序：SEC优先，有CIK优先
    final = list(results.values())
    final.sort(key=lambda x: (x["_priority"], x["ticker"]))
    # 移除内部字段
    for r in final:
        r.pop("_priority", None)
    return final[:limit]


def _search_yahoo(query: str, limit: int = 15) -> list[dict]:
    """通过 Yahoo Finance 搜索全球公司"""
    from yfinance import Search
    s = Search(query)
    if not s.quotes:
        return []
    results = []
    for q in s.quotes[:limit]:
        if q.get("quoteType", "") not in ("", "EQUITY", "ETF", "ADR", "GDR"):
            continue
        results.append({
            "symbol": q.get("symbol", ""),
            "shortname": q.get("shortname", ""),
            "longname": q.get("longname", ""),
            "exchange": q.get("exchange", q.get("exchDisp", "")),
            "type": q.get("quoteType", ""),
        })
    return results


def _find_cik_for_ticker(ticker: str, sec_df: pd.DataFrame, name: str) -> str:
    """尝试为Yahoo结果查找对应的SEC CIK"""
    exact = sec_df[sec_df["ticker"] == ticker.upper()]
    if not exact.empty:
        return str(exact.iloc[0]["cik"])

    base = ticker.split(".")[0].upper()
    base_match = sec_df[sec_df["ticker"] == base]
    if not base_match.empty:
        return str(base_match.iloc[0]["cik"])

    if name:
        name_upper = name.upper()[:30]
        name_match = sec_df[sec_df["name"].str.upper().str.contains(name_upper[:15], na=False)]
        if not name_match.empty:
            return str(name_match.iloc[0]["cik"])

    return ""


def _find_adr_in_sec(name: str, sec_df: pd.DataFrame, exclude_ticker: str) -> str:
    """通过公司名称在SEC列表中查找ADR/OTC代码"""
    if not name:
        return ""
    # 提取公司核心名称（去掉Corp, Inc, Ltd, AG, S.A.等后缀和交易所后缀）
    core = name.upper()
    for suffix in [".T", ".KS", ".DE", ".SW", ".L", ".PA", ".MI", ".HK", ".TW"]:
        core = core.replace(suffix, "")
    for word in [" CORP", " INC", " LTD", " AG", " S.A.", " SA", " CO., LTD", " CO LTD",
                 " CORPORATION", " COMPANY", " PLC", " SE", " NV", " BHD", " CLASS A",
                 " TECHNOLOGIES", " TECHNOLOGY", " ELECTRONICS", " ELECTRONIC"]:
        core = core.replace(word, "")
    core = core.strip()[:25]
    if len(core) < 5:
        return ""

    # 在SEC列表中搜索
    matches = sec_df[sec_df["name"].str.upper().str.contains(core, na=False)]
    matches = matches[matches["ticker"] != exclude_ticker.upper()]

    if not matches.empty:
        matches = matches.copy()
        matches["_score"] = matches["ticker"].apply(
            lambda x: 10 if (x.isalpha() and 2 <= len(x) <= 5) else 1
        )
        matches = matches.sort_values("_score", ascending=False)
        return str(matches.iloc[0]["cik"])

    return ""


def _search_sec_edgar(query: str) -> str:
    """通过SEC公司搜索API查找CIK（用于没有ticker的外国公司）"""
    import requests
    import re

    try:
        headers = {"User-Agent": "financial-tool/1.0 (rc0221rc@gmail.com)"}
        url = "https://www.sec.gov/cgi-bin/browse-edgar"
        params = {"action": "getcompany", "company": query, "output": "atom", "count": "10"}
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code != 200:
            return ""

        # Extract CIKs from XML (company names may be garbled but CIKs are valid)
        ciks = set()
        for m in re.finditer(r"CIK=(\d+)", resp.text):
            ciks.add(m.group(1).zfill(10))
        for m in re.finditer(r"<cik>(\d+)</cik>", resp.text):
            ciks.add(m.group(1).zfill(10))

        if not ciks:
            return ""

        # Validate each CIK and find best match
        best_cik = ""
        best_score = 0
        upper_query = query.upper()

        for cik in ciks:
            try:
                r = requests.get(
                    f"https://data.sec.gov/submissions/CIK{cik}.json",
                    headers=headers, timeout=10,
                )
                r.raise_for_status()
                cname = r.json().get("name", "").upper()
                # Score: exact match, partial match, or /FI (foreign issuer)
                score = 0
                if upper_query in cname:
                    score = 10
                elif cname in upper_query:
                    score = 8
                words = upper_query.split()
                for w in words:
                    if len(w) >= 3 and w in cname:
                        score += 3
                # Prefer /FI (foreign issuer) — more likely to have 20-F/6-K
                if "/FI" in cname:
                    score += 5
                # Penalize unrelated entities
                if " INC" in cname or " CORP" in cname or " LTD" in cname or " HOLDINGS" in cname:
                    if any(w in cname for w in words):
                        score += 2  # small bonus for matching entity type
                if score > best_score:
                    best_score = score
                    best_cik = cik
            except Exception:
                continue

        return best_cik if best_score >= 3 else ""
    except Exception:
        return ""
