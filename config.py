"""美股版全局配置"""

from pathlib import Path

# SEC EDGAR 文件类型分类
# 用户看到的类别 → SEC form types
DOC_TYPE_MAP = {
    "10-K 年度报告": {
        "forms": ["10-K", "10-K/A", "10-KT", "10-KT/A"],
        "desc": "公司年度报告（Annual Report）",
    },
    "10-Q 季度报告": {
        "forms": ["10-Q", "10-Q/A", "10-QT", "10-QT/A"],
        "desc": "公司季度报告（Quarterly Report）",
    },
    "8-K 重大事项/业绩发布": {
        "forms": ["8-K", "8-K/A"],
        "keyword": "earnings|results|financial|press release|conference call",
        "desc": "重大事项报告，含业绩发布（Current Report）",
    },
    "S-1 招股说明书": {
        "forms": ["S-1", "S-1/A", "F-1", "F-1/A", "S-11", "S-11/A", "F-10"],
        "desc": "IPO招股说明书（Registration Statement）",
    },
    "业绩演示材料 (8-K Exhibits)": {
        "forms": ["8-K", "8-K/A"],
        "keyword": "presentation|slides|deck|conference|investor presentation|earnings call presentation",
        "desc": "业绩说明会PPT（8-K附件中的演示材料）",
    },
}

DOC_TYPE_LABELS = {k: v["desc"] for k, v in DOC_TYPE_MAP.items()}

# SEC API 配置
SEC_BASE_URL = "https://data.sec.gov"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"
SEC_USER_AGENT = "financial-tool/1.0 (rc0221rc@gmail.com)"

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
REQUEST_DELAY = 0.5
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3
MAX_FILINGS_PER_TYPE = 50  # 每种类型最多显示的公告数

# SEC form type → readable name
FORM_NAMES = {
    "10-K": "年度报告", "10-K/A": "年度报告(修订)", "10-KT": "年度报告(过渡期)",
    "10-Q": "季度报告", "10-Q/A": "季度报告(修订)", "10-QT": "季度报告(过渡期)",
    "8-K": "重大事项报告", "8-K/A": "重大事项报告(修订)",
    "S-1": "招股说明书", "S-1/A": "招股说明书(修订)",
    "F-1": "招股说明书(外国)", "F-1/A": "招股说明书(外国·修订)",
    "20-F": "年度报告(外国)", "6-K": "重大事项(外国)",
    "DEF 14A": "股东委托书", "PRE 14A": "股东委托书(初稿)",
    "SC 13G": "大股东持股报告", "SC 13D": "大股东持股报告(主动)",
    "3": "内部人持股报告", "4": "内部人交易报告", "5": "内部人年度报告",
    "S-3": "储架注册", "S-4": "并购注册", "S-8": "员工股权计划",
    "SD": "特殊披露", "11-K": "员工福利计划年报",
}

# 表格标题识别模式（英文）
TABLE_TITLE_PATTERNS = [
    r"(?:CONSOLIDATED|CONDENSED)?\s*(?:STATEMENTS?\s*OF)?\s*(?:BALANCE\s*SHEETS?|FINANCIAL\s*POSITION)",
    r"(?:CONSOLIDATED|CONDENSED)?\s*(?:STATEMENTS?\s*OF)?\s*(?:INCOME|OPERATIONS|EARNINGS|COMPREHENSIVE\s*INCOME)",
    r"(?:CONSOLIDATED|CONDENSED)?\s*(?:STATEMENTS?\s*OF)?\s*CASH\s*FLOWS?",
    r"(?:CONSOLIDATED|CONDENSED)?\s*(?:STATEMENTS?\s*OF)?\s*(?:STOCKHOLDERS|SHAREHOLDERS)?\s*EQUITY",
    r"(?:NOTE|SUMMARY)\s+(?:\d+|OF)\s*.*?(?:FINANCIAL|ACCOUNTING|SIGNIFICANT)",
    r"(?:REVENUE|SALES).*?(?:BY|DISAGGREGATION|SEGMENT|GEOGRAPH)",
    r"(?:SEGMENT|BUSINESS\s*SEGMENT).*?(?:INFORMATION|REPORTING|DATA)",
    r"(?:PROPERTY|PLANT|EQUIPMENT|INTANGIBLE|GOODWILL).*?(?:SCHEDULE|SUMMARY|ROLL)",
    r"(?:DEBT|BORROWINGS?|LONG.TERM).*?(?:SCHEDULE|SUMMARY|MATURITY)",
    r"(?:STOCK.BASED|SHARE.BASED|EQUITY\s*INCENTIVE).*?(?:COMPENSATION|AWARD|ACTIVITY)",
    r"(?:FAIR\s*VALUE|LEVEL\s*\d).*?(?:MEASUREMENT|HIERARCHY|INPUT)",
    r"(?:LEASE|LESSEE|RIGHT.OF.USE).*?(?:SCHEDULE|MATURITY|OBLIGATION)",
    r"(?:INCOME\s*TAX|TAX\s*PROVISION).*?(?:RATE\s*RECONCILIATION|SCHEDULE|SUMMARY)",
    r"(?:EARNINGS|NET\s*INCOME|LOSS)\s*PER\s*SHARE",
    r"(?:ACQUISITION|BUSINESS\s*COMBINATION).*?(?:PURCHASE\s*PRICE|ALLOCATION|PRO\s*FORMA)",
    r"(?:RELATED\s*PARTY|COMMITMENTS?\s*AND\s*CONTINGENC)",
]
