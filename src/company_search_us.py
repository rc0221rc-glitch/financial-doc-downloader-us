"""美股搜索：按代码或公司名查找"""

import json
from pathlib import Path

import pandas as pd

_stock_list_cache: pd.DataFrame | None = None
_STOCK_LIST_FILE = Path(__file__).parent / "stock_list_us.json"


def get_stock_list() -> pd.DataFrame:
    """获取美股全量列表（从本地JSON加载）。返回 DataFrame: ticker, name, cik"""
    global _stock_list_cache
    if _stock_list_cache is not None:
        return _stock_list_cache

    with open(_STOCK_LIST_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    df = pd.DataFrame(data)
    df["ticker"] = df["ticker"].astype(str).str.upper()
    df["cik"] = df["cik"].astype(str).str.zfill(10)
    _stock_list_cache = df
    return df


def search_company(query: str, limit: int = 20) -> list[dict]:
    """按代码或名称搜索美股，返回匹配列表"""
    if not query or not query.strip():
        return []

    query = query.strip()
    df = get_stock_list()

    # 按代码搜索（精确或前缀）
    if query.upper() == query and len(query) <= 5:
        exact_code = df[df["ticker"] == query.upper()]
        if not exact_code.empty:
            return exact_code.head(limit).to_dict(orient="records")
        prefix = df[df["ticker"].str.startswith(query.upper())]
        if not prefix.empty:
            return prefix.head(limit).to_dict(orient="records")

    # 按名称搜索
    upper_query = query.upper()
    exact = df[df["name"].str.upper() == upper_query]
    if not exact.empty:
        return exact.head(limit).to_dict(orient="records")

    substring = df[df["name"].str.upper().str.contains(upper_query, na=False)]
    if not substring.empty:
        return substring.head(limit).to_dict(orient="records")

    # 模糊匹配英文单词
    words = upper_query.split()
    for w in words:
        if len(w) >= 2:
            word_match = df[df["name"].str.upper().str.contains(w, na=False)]
            if not word_match.empty:
                return word_match.head(limit).to_dict(orient="records")

    return []
