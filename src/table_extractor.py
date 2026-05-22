"""PDF/HTML表格提取：多策略级联提取"""

import io
import re
from pathlib import Path
from dataclasses import dataclass

import pdfplumber
import pandas as pd

from config import TABLE_TITLE_PATTERNS


@dataclass
class ExtractedTable:
    """提取出的表格及其元数据"""
    page_number: int
    table_index_on_page: int
    data: list[list[str]]
    title: str | None = None
    strategy: str = ""


def extract_tables_from_pdf(
    file_path: Path,
    progress_callback=None,
) -> list[ExtractedTable]:
    """
    从文件提取所有表格（自动检测PDF或HTML）。

    Returns:
        ExtractedTable列表
    """
    suffix = file_path.suffix.lower()
    if suffix in (".htm", ".html", ".xhtml"):
        return _extract_from_html(file_path, progress_callback)
    else:
        return _extract_from_pdf(file_path, progress_callback)


# ==================== HTML 提取 ====================

def _extract_from_html(
    file_path: Path,
    progress_callback=None,
) -> list[ExtractedTable]:
    """从HTML/SEC文件中提取表格"""
    tables: list[ExtractedTable] = []

    try:
        all_tables = pd.read_html(file_path, flavor="lxml")
    except Exception:
        try:
            all_tables = pd.read_html(file_path, flavor="html5lib")
        except Exception:
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    html = f.read()
                all_tables = pd.read_html(io.StringIO(html), flavor="lxml")
            except Exception:
                return tables

    if progress_callback:
        progress_callback(0, len(all_tables))

    for i, df in enumerate(all_tables):
        if df.empty or df.shape[0] < 2 or df.shape[1] < 2:
            continue

        # Convert to list[list[str]]
        data = [[str(c) if pd.notna(c) else "" for c in row] for row in df.values]
        # Add column headers as first row
        headers = [str(c) for c in df.columns]
        full_data = [headers] + data

        if not _is_valid_table(full_data):
            continue

        title = _detect_html_table_title(df, file_path)

        tables.append(ExtractedTable(
            page_number=i + 1,
            table_index_on_page=0,
            data=full_data,
            title=title,
            strategy="html",
        ))

        if progress_callback:
            progress_callback(i + 1, len(all_tables))

    return tables


def _detect_html_table_title(df: pd.DataFrame, file_path: Path) -> str | None:
    """检测HTML表格的标题"""
    # Check column headers for clues
    header_str = " ".join(str(c) for c in df.columns).upper()
    for pattern in TABLE_TITLE_PATTERNS:
        if re.search(pattern, header_str):
            return re.search(pattern, header_str).group(0)

    # Check first column name
    if df.columns[0]:
        first = str(df.columns[0]).strip()
        if len(first) < 60 and first.upper() != "NONE":
            return first[:80]

    return None


# ==================== PDF 提取 ====================

def _extract_from_pdf(
    pdf_path: Path,
    progress_callback=None,
) -> list[ExtractedTable]:
    """从PDF中提取所有表格"""
    tables: list[ExtractedTable] = []

    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            if progress_callback:
                progress_callback(i + 1, total)

            page_tables = _extract_from_page(page, i + 1)
            tables.extend(page_tables)

    return _deduplicate_tables(tables)


def _extract_from_page(page, page_num: int) -> list[ExtractedTable]:
    """多策略提取单页表格"""
    results = []
    table_idx = 0

    try:
        found = page.find_tables()
    except Exception:
        found = []

    for table_obj in found:
        try:
            raw = table_obj.extract()
        except Exception:
            continue
        if not raw or len(raw) < 2:
            continue
        clean = clean_table_data(raw)
        if not _is_valid_table(clean):
            continue

        bbox = table_obj.bbox
        title = _detect_title_from_page(page, bbox)

        results.append(ExtractedTable(
            page_number=page_num,
            table_index_on_page=table_idx,
            data=clean,
            title=title,
            strategy="find_tables",
        ))
        table_idx += 1

    if not results:
        try:
            raw_tables = page.extract_tables({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
            })
        except Exception:
            raw_tables = []

        for raw in raw_tables:
            if not raw or len(raw) < 2:
                continue
            clean = clean_table_data(raw)
            if not _is_valid_table(clean):
                continue
            results.append(ExtractedTable(
                page_number=page_num,
                table_index_on_page=table_idx,
                data=clean,
                title=None,
                strategy="lines",
            ))
            table_idx += 1

    if not results:
        try:
            raw_tables = page.extract_tables({
                "vertical_strategy": "text",
                "horizontal_strategy": "text",
                "snap_tolerance": 3,
                "join_tolerance": 3,
            })
        except Exception:
            raw_tables = []
        for raw in raw_tables:
            if not raw or len(raw) < 2:
                continue
            clean = clean_table_data(raw)
            if not _is_valid_table(clean):
                continue
            results.append(ExtractedTable(
                page_number=page_num,
                table_index_on_page=table_idx,
                data=clean,
                title=None,
                strategy="text",
            ))
            table_idx += 1

    return results


def _detect_title_from_page(page, table_bbox: tuple) -> str | None:
    """从表格上方区域检测标题"""
    x0, top, x1, bottom = table_bbox
    title_top = max(0, top - 100)
    title_bottom = max(0, top - 10)

    try:
        region = page.within_bbox((0, title_top, page.width, title_bottom))
        if region is None:
            return None
        text = region.extract_text()
        if not text:
            return None
        lines = text.split("\n")
    except Exception:
        return None

    candidates = []
    for line in lines:
        line = line.strip()
        if len(line) < 3 or len(line) > 100:
            continue
        if re.match(r'^[\d\s\.\,\、\;\:\-\—\(\)\%，；：、]+$', line):
            continue
        score = _score_title(line)
        if score >= 5:
            candidates.append((score, line))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    if lines:
        for line in reversed(lines):
            line = line.strip()
            if len(line) >= 3:
                return line[:80]
    return None


def _score_title(line: str) -> int:
    """为候选标题行打分"""
    score = 0
    for pattern in TABLE_TITLE_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            score += 10
            break
    if re.search(r"[表况细览总明StatementTable]$", line):
        score += 5
    return score


def clean_table_data(raw_data: list[list[str | None]]) -> list[list[str]]:
    """清洗表格数据"""
    result = []
    for row in raw_data:
        clean_row = [str(c).strip() if c is not None else "" for c in row]
        if any(clean_row):
            result.append(clean_row)
    return result


def _is_valid_table(data: list[list[str]]) -> bool:
    """检查表格是否有效"""
    if len(data) < 2:
        return False
    max_cols = max(len(r) for r in data)
    if max_cols < 2:
        return False
    non_empty = sum(1 for r in data for c in r if c)
    return non_empty >= 4


def _deduplicate_tables(tables: list[ExtractedTable]) -> list[ExtractedTable]:
    """去除同一页上高度相似的表格"""
    if len(tables) <= 1:
        return tables
    keep = []
    for i, t in enumerate(tables):
        is_dup = False
        for j in range(i):
            u = tables[j]
            if t.page_number == u.page_number and _table_similarity(t, u) > 0.9:
                is_dup = True
                break
        if not is_dup:
            keep.append(t)
    return keep


def _table_similarity(a: ExtractedTable, b: ExtractedTable) -> float:
    """计算两个表格的简单相似度"""
    a_text = "".join("".join(r) for r in a.data[:3])
    b_text = "".join("".join(r) for r in b.data[:3])
    if not a_text or not b_text:
        return 0.0
    common = sum(1 for c in a_text if c in b_text)
    return common / max(len(a_text), len(b_text))
