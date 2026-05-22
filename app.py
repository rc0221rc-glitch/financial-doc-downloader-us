"""全球上市公司公告下载与表格提取工具 - Streamlit主界面"""

import sys
import zipfile
from pathlib import Path
from datetime import datetime, timedelta

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

from config import DOC_TYPE_LABELS, DOC_TYPE_MAP, DOWNLOAD_DIR
from src.company_search_global import search_company
from src.filing_fetcher_us import fetch_filing_list, download_filings
from src.transcript_fetcher import download_transcripts
from src.ir_scraper import download_presentations
from src.table_extractor import extract_tables_from_pdf
from src.excel_writer import write_tables_to_excel

st.set_page_config(
    page_title="全球上市公司公告下载",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------- session state ----------
for key, val in {
    "search_results": [],
    "selected_company": None,
    "doc_types": ["年度报告 (10-K / 20-F)", "季度/中期报告 (10-Q / 6-K)"],
    "date_range": (
        datetime.now().date() - timedelta(days=3 * 365),
        datetime.now().date(),
    ),
    "filing_df": None,
    "results": None,
    "download_excel_path": None,
    "download_zip_path": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = val


# ==================== 侧边栏 ====================
with st.sidebar:
    st.header("🌍 关于")
    st.markdown("""
    下载全球上市公司公开披露文件，自动提取表格生成Excel。

    **数据来源：**
    - **SEC EDGAR**：美国及在美上市外国公司
    - 包含 10-K、10-Q、8-K、20-F、6-K、S-1、F-1 等

    **搜索范围：** 全球（SEC注册 + Yahoo Finance）
    """)
    st.markdown("---")
    st.caption("注：仅在日本等本地上市、未在美国发行ADR的公司，SEC档案可能有限。可尝试搜索公司名查找ADR代码。")
    if st.button("🔄 重置"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()

# ==================== 主界面 ====================
st.title("🌍 全球上市公司公告下载与表格提取工具")
st.caption("数据来源：SEC EDGAR (sec.gov)  +  Yahoo Finance 全球搜索  |  支持美股及全球SEC注册公司")

# ---------- 步骤一：搜索公司 ----------
st.header("步骤一：搜索公司")

col1, col2 = st.columns([4, 1])
with col1:
    query = st.text_input(
        "输入公司代码或名称（支持全球公司）",
        placeholder="例如：AAPL、TSLA、IFNNY、3436.T、Infineon、Sumco、Toyota",
        label_visibility="collapsed",
        key="search_input",
    )
with col2:
    search_btn = st.button("🔍 搜索", use_container_width=True, type="primary")

if search_btn and query.strip():
    with st.spinner("全球搜索中..."):
        st.session_state.search_results = search_company(query.strip())
    if not st.session_state.search_results:
        st.warning("未找到匹配的公司。请尝试完整代码或公司英文名。")

if st.session_state.search_results:
    st.subheader(f"搜索结果（共 {len(st.session_state.search_results)} 个，点击选择）")
    opts = {}
    for r in st.session_state.search_results[:30]:
        cik = r.get("cik", "")
        source_tag = "SEC" if r.get("source") == "SEC" else "Yahoo"
        has_cik = "✓" if cik else "✗"
        label = f"{r['ticker']}  |  {r['name'][:80]}  |  {r.get('exchange','?')}  |  SEC:{has_cik}"
        opts[label] = r

    selected = st.radio("选择公司", list(opts.keys()), label_visibility="collapsed")
    if selected:
        st.session_state.selected_company = opts[selected]

# ---------- 步骤二 & 三：条件与查询 ----------
if st.session_state.selected_company:
    company = st.session_state.selected_company
    cik = company.get("cik", "")
    has_filings = bool(cik)

    st.markdown(f"**已选：** `{company['ticker']}` {company['name'][:100]}")
    if cik:
        st.caption(f"CIK: {cik} | 来源: {company.get('source','?')} | 交易所: {company.get('exchange','?')}")
    else:
        st.warning("⚠️ 该公司未在SEC注册，无法下载SEC文件。请尝试搜索公司名查找其ADR代码（如 Infineon → IFNNY，Toyota → TM）。")

    if has_filings:
        st.header("步骤二：选择条件")

        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("开始日期", value=st.session_state.date_range[0])
        with col2:
            end_date = st.date_input("结束日期", value=st.session_state.date_range[1])
        st.session_state.date_range = (start_date, end_date)

        st.markdown("**文件类型（可多选）：**")
        doc_types = []
        for key, label in DOC_TYPE_LABELS.items():
            if st.checkbox(f"{key} — {label}", value=key in st.session_state.doc_types, key=f"dt_{key}"):
                doc_types.append(key)
        st.session_state.doc_types = doc_types

        st.header("步骤三：查询SEC公告")
        if st.button("📋 查询公告列表", type="primary", disabled=not doc_types):
            if not doc_types:
                st.warning("请至少选择一种文件类型。")
            else:
                with st.spinner("正在查询SEC EDGAR..."):
                    ticker = company["ticker"]
                    sd = start_date.strftime("%Y%m%d")
                    ed = end_date.strftime("%Y%m%d")

                    pbar = st.progress(0)
                    stat = st.empty()

                    def on_prog(cur, total, msg=""):
                        pbar.progress(cur / max(total, 1))
                        stat.text(msg)

                    df = fetch_filing_list(cik, ticker, doc_types, sd, ed, on_prog)
                    st.session_state.filing_df = df
                    pbar.empty()
                    stat.empty()

                    if df.empty:
                        st.warning("未找到符合条件的SEC公告。")
                    else:
                        st.rerun()

# ---------- 步骤四：选择公告 ----------
if st.session_state.filing_df is not None and not st.session_state.filing_df.empty:
    df = st.session_state.filing_df
    st.header("步骤四：选择要下载的公告")
    st.markdown(f"共找到 **{len(df)}** 份公告")

    if "doc_type" in df.columns:
        tc = df["doc_type"].value_counts()
        cols = st.columns(len(tc))
        for i, (t, c) in enumerate(tc.items()):
            with cols[i]:
                st.metric(t, f"{c} 份")

    all_checked = st.checkbox("全选 / 取消全选", value=False, key="select_all")

    selected_indices = []
    for i, (_, row) in enumerate(df.iterrows()):
        title = f"[{row.get('filing_date', '')[:10]}] [{row.get('form', '')}] {row.get('form_name', '')}"
        checked = st.checkbox(title, value=all_checked, key=f"fl_{i}")
        if checked:
            selected_indices.append(i)

    st.header("步骤五：下载与提取表格")
    if st.button("📥 开始下载并提取表格", type="primary", disabled=not selected_indices):
        selected_df = df.iloc[selected_indices].reset_index(drop=True)
        ticker = company["ticker"]

        out_root = DOWNLOAD_DIR / ticker
        pdf_dir = out_root / "SEC_Filings"
        excel_dir = out_root / "Excel"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        excel_dir.mkdir(parents=True, exist_ok=True)

        st.session_state.download_excel_path = None
        st.session_state.download_zip_path = None

        status_widget = st.status("正在处理...", expanded=True)
        overall_progress = st.progress(0)

        try:
            status_widget.write("📥 下载SEC文件...")

            def dl_prog(cur, total, fname):
                pct = (cur + 1) / max(total, 1) * 0.40
                overall_progress.progress(pct)
                status_widget.write(f"📥 ({cur + 1}/{total}) {fname[:60]}")

            keyword_config = {}
            content_filter_config = {}
            for dt in set(selected_df["doc_type"].tolist()):
                cfg = DOC_TYPE_MAP.get(dt, {})
                if "keyword" in cfg:
                    keyword_config[dt] = cfg["keyword"]
                if "content_filter" in cfg:
                    content_filter_config[dt] = cfg["content_filter"]

            files = download_filings(
                selected_df, pdf_dir, dl_prog,
                keyword_config=keyword_config,
                content_filter_config=content_filter_config,
            )
            status_widget.write(f"✅ 下载完成：{len(files)}/{len(selected_df)} 份文件")
            overall_progress.progress(0.45)

            # Compute filing dates: SEC filings + Yahoo Finance earnings dates
            all_filing_dates = sorted(set(
                str(d)[:10].replace("-", "") for d in df["filing_date"]
            ))
            # Also get Yahoo Finance earnings dates (critical for non-US companies)
            try:
                import yfinance as yf
                stock = yf.Ticker(ticker)
                earnings = stock.earnings_dates
                if earnings is not None and not earnings.empty:
                    for dt in earnings.index:
                        all_filing_dates.append(dt.strftime("%Y%m%d"))
                all_filing_dates = sorted(set(all_filing_dates))
            except Exception:
                pass

            # Transcript search (based on step 2 checkbox, not step 4 selection)
            transcript_files = []
            if "业绩电话会纪要" in st.session_state.doc_types:
                status_widget.write("🎙️ 搜索业绩电话会纪要...")
                trans_dir = out_root / "Transcripts"
                transcript_files = download_transcripts(
                    ticker, all_filing_dates, trans_dir,
                    company_name=company.get("name", ""),
                    progress_callback=lambda cur, total, msg: status_widget.write(
                        f"🎙️ {msg}"
                    ),
                )
                if transcript_files:
                    files.extend(transcript_files)
                    status_widget.write(f"🎙️ 找到 {len(transcript_files)} 份电话会纪要")
                else:
                    status_widget.write("🎙️ 未找到电话会纪要")

            # Presentation search (based on step 2 checkbox, not step 4 selection)
            if "业绩演示材料" in st.session_state.doc_types:
                status_widget.write("📊 搜索业绩演示材料...")
                pres_dir = out_root / "Presentations"
                pres_files = download_presentations(
                    ticker, pres_dir,
                    company_name=company.get("name", ""),
                    target_dates=all_filing_dates,
                    progress_callback=lambda cur, total, msg: status_widget.write(
                        f"📊 {msg}"
                    ),
                )
                if pres_files:
                    files.extend(pres_files)
                    status_widget.write(f"📊 找到 {len(pres_files)} 份演示材料")
                else:
                    status_widget.write("📊 未找到演示材料")

            status_widget.write("📊 提取表格...")
            all_tables = []
            total_files = len(files)

            for j, file_path in enumerate(files):
                if file_path.suffix.lower() not in (".pdf", ".htm", ".html", ".xhtml"):
                    continue
                try:
                    def ext_prog(cur, total):
                        pct = 0.45 + (j / max(total_files, 1)) * 0.25
                        pct += (cur / max(total, 1)) * (0.25 / max(total_files, 1))
                        overall_progress.progress(min(pct, 0.75))

                    tables = extract_tables_from_pdf(file_path, ext_prog)
                    all_tables.extend(tables)
                    status_widget.write(f"   {file_path.stem[:60]}: {len(tables)} 个表格")
                except Exception:
                    status_widget.write(f"   {file_path.stem[:60]}: 跳过")

            status_widget.write(f"✅ 共提取 {len(all_tables)} 个表格")
            overall_progress.progress(0.80)

            if all_tables:
                status_widget.write("📝 生成Excel文件...")
                excel_files = write_tables_to_excel(
                    all_tables, excel_dir, ticker, "SEC_Filings",
                    str(selected_df.iloc[0].get("filing_date", ""))[:4],
                    "、".join(set(str(x) for x in selected_df["doc_type"].tolist())),
                )
                if excel_files:
                    st.session_state.download_excel_path = str(excel_files[0])
                status_widget.write(f"✅ Excel生成完成")
            else:
                status_widget.write("⚠️ 未提取到表格")
            overall_progress.progress(0.92)

            zip_path = out_root / f"{ticker}_SEC_Filings.zip"
            all_downloaded = files
            if all_downloaded:
                status_widget.write("📦 打包文件...")
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in all_downloaded:
                        zf.write(p, p.name)
                st.session_state.download_zip_path = str(zip_path)

            overall_progress.progress(1.0)

            st.session_state.results = {
                "file_count": len(files),
                "table_count": len(all_tables),
                "excel_count": 1 if st.session_state.download_excel_path else 0,
            }

            status_widget.update(label="✅ 处理完成！", state="complete")

        except Exception as e:
            status_widget.update(label=f"❌ 出错", state="error")
            st.error(f"处理出错：{e}")
            import traceback
            st.code(traceback.format_exc())

        overall_progress.empty()

# ---------- Non-SEC path: Transcript & Presentation search for companies without SEC filings ----------
if (st.session_state.selected_company
        and not (st.session_state.filing_df is not None and not st.session_state.filing_df.empty)
        and any(dt in st.session_state.doc_types for dt in ["业绩电话会纪要", "业绩演示材料"])):
    company = st.session_state.selected_company
    ticker = company["ticker"]

    st.header("搜索电话会纪要与演示材料")
    st.info(f"该公司在SEC的档案有限，将直接从互联网搜索 **{ticker}** 的电话会纪要与演示材料。")

    if st.button("🔍 搜索电话会纪要与演示材料", type="primary"):
        out_root = DOWNLOAD_DIR / ticker
        trans_dir = out_root / "Transcripts"
        pres_dir = out_root / "Presentations"
        trans_dir.mkdir(parents=True, exist_ok=True)
        pres_dir.mkdir(parents=True, exist_ok=True)

        st.session_state.download_excel_path = None
        st.session_state.download_zip_path = None

        status_widget = st.status("正在搜索...", expanded=True)
        all_files = []

        try:
            # Get Yahoo Finance earnings dates
            import yfinance as yf
            yahoo_dates = []
            try:
                stock = yf.Ticker(ticker)
                earnings = stock.earnings_dates
                if earnings is not None and not earnings.empty:
                    for dt in earnings.index:
                        yahoo_dates.append(dt.strftime("%Y%m%d"))
                yahoo_dates = sorted(set(yahoo_dates))
                status_widget.write(f"从Yahoo Finance获取到 {len(yahoo_dates)} 个业绩日期")
            except Exception:
                status_widget.write("无法从Yahoo Finance获取业绩日期，将使用最近日期搜索")

            if not yahoo_dates:
                from datetime import datetime
                today = datetime.now()
                for i in range(12):
                    dt = today - timedelta(days=90 * i)
                    yahoo_dates.append(dt.strftime("%Y%m%d"))
                yahoo_dates.sort()

            # Transcript search
            if "业绩电话会纪要" in st.session_state.doc_types:
                status_widget.write("🎙️ 搜索业绩电话会纪要...")
                transcript_files = download_transcripts(
                    ticker, yahoo_dates, trans_dir,
                    company_name=company.get("name", ""),
                    progress_callback=lambda cur, total, msg: status_widget.write(
                        f"🎙️ {msg}"
                    ),
                )
                if transcript_files:
                    all_files.extend(transcript_files)
                    status_widget.write(f"🎙️ 找到 {len(transcript_files)} 份电话会纪要")
                else:
                    status_widget.write("🎙️ 未找到电话会纪要")

            # Presentation search
            if "业绩演示材料" in st.session_state.doc_types:
                status_widget.write("📊 搜索业绩演示材料...")
                pres_files = download_presentations(
                    ticker, pres_dir,
                    company_name=company.get("name", ""),
                    target_dates=yahoo_dates,
                    progress_callback=lambda cur, total, msg: status_widget.write(
                        f"📊 {msg}"
                    ),
                )
                if pres_files:
                    all_files.extend(pres_files)
                    status_widget.write(f"📊 找到 {len(pres_files)} 份演示材料")
                else:
                    status_widget.write("📊 未找到演示材料")

            # Package results
            if all_files:
                zip_path = out_root / f"{ticker}_Transcripts_Presentations.zip"
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in all_files:
                        zf.write(p, p.name)
                st.session_state.download_zip_path = str(zip_path)
                st.session_state.results = {
                    "file_count": len(all_files),
                    "table_count": 0,
                    "excel_count": 0,
                }

            status_widget.update(label="✅ 搜索完成！", state="complete")

        except Exception as e:
            status_widget.update(label=f"❌ 出错", state="error")
            st.error(f"搜索出错：{e}")
            import traceback
            st.code(traceback.format_exc())

# ---------- 结果展示 & 下载 ----------
if st.session_state.results:
    r = st.session_state.results
    st.success("### ✅ 处理完成！")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("下载文件", f"{r['file_count']} 份")
    with c2:
        st.metric("提取表格", f"{r['table_count']} 个")
    with c3:
        st.metric("生成Excel", f"{r['excel_count']} 个")

    st.markdown("### 📥 下载文件到本地")

    btn_col1, btn_col2 = st.columns(2)

    with btn_col1:
        if st.session_state.download_excel_path:
            excel_path = Path(st.session_state.download_excel_path)
            if excel_path.exists():
                with open(excel_path, "rb") as f:
                    st.download_button(
                        label=f"📊 下载Excel表格 ({excel_path.name})",
                        data=f,
                        file_name=excel_path.name,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        type="primary",
                        use_container_width=True,
                    )

    with btn_col2:
        if st.session_state.download_zip_path:
            zip_path = Path(st.session_state.download_zip_path)
            if zip_path.exists():
                with open(zip_path, "rb") as f:
                    st.download_button(
                        label=f"📦 下载全部文件包 ({zip_path.name})",
                        data=f,
                        file_name=zip_path.name,
                        mime="application/zip",
                        use_container_width=True,
                    )

    st.caption("提示：手机端点击下载按钮后，文件会保存到浏览器的下载目录中。")

st.markdown("---")
st.caption(
    "免责声明：本工具从SEC EDGAR获取公开披露信息，仅供学习研究使用。"
)
