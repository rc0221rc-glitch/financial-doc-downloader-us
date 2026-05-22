"""美股SEC公告下载与表格提取工具 - Streamlit主界面"""

import sys
import zipfile
from pathlib import Path
from datetime import datetime, timedelta

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

from config import DOC_TYPE_LABELS, DOWNLOAD_DIR
from src.company_search_us import search_company, get_stock_list
from src.filing_fetcher_us import fetch_filing_list, download_filings
from src.table_extractor import extract_tables_from_pdf
from src.excel_writer import write_tables_to_excel

st.set_page_config(
    page_title="美股SEC公告下载工具",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------- session state ----------
for key, val in {
    "search_results": [],
    "selected_company": None,
    "doc_types": ["10-K 年度报告", "10-Q 季度报告"],
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
    st.header("⚙️ 关于")
    st.markdown("""
    下载美股上市公司SEC公开文件（10-K、10-Q、8-K等），
    自动提取表格生成Excel。

    **数据来源：** SEC EDGAR
    """)
    st.markdown("---")
    st.caption("注：业绩电话会议纪要(Transcripts)需第三方API，暂通过8-K业绩发布文件获取相关信息。")
    if st.button("🔄 重置"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()

# ==================== 主界面 ====================
st.title("📄 美股上市公司公告下载与表格提取工具")
st.caption("数据来源：SEC EDGAR (sec.gov)  |  支持纽交所、纳斯达克上市公司")

# ---------- 步骤一：搜索公司 ----------
st.header("步骤一：搜索公司")

col1, col2 = st.columns([4, 1])
with col1:
    query = st.text_input(
        "输入公司代码或名称",
        placeholder="例如：AAPL、TSLA 或 Apple、Tesla",
        label_visibility="collapsed",
        key="search_input",
    )
with col2:
    search_btn = st.button("🔍 搜索", use_container_width=True, type="primary")

if search_btn and query.strip():
    with st.spinner("搜索中..."):
        st.session_state.search_results = search_company(query.strip())
    if not st.session_state.search_results:
        st.warning("未找到匹配的公司，请尝试完整的代码或公司名。")

if st.session_state.search_results:
    st.subheader("搜索结果（点击选择）")
    opts = {}
    for r in st.session_state.search_results[:30]:
        cik = r.get("cik", "")
        label = f"{r['ticker']}  |  {r['name'][:80]}  |  CIK:{cik}"
        opts[label] = r

    selected = st.radio("选择公司", list(opts.keys()), label_visibility="collapsed")
    if selected:
        st.session_state.selected_company = opts[selected]

# ---------- 步骤二 & 三：条件与查询 ----------
if st.session_state.selected_company:
    company = st.session_state.selected_company
    st.markdown(f"**已选：** `{company['ticker']}` {company['name'][:100]}（CIK: {company['cik']}）")

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
                cik = company["cik"]
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

    # ---------- 步骤五：下载与提取 ----------
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
            # Phase 1: Download filings
            status_widget.write("📥 下载SEC文件...")

            def dl_prog(cur, total, fname):
                pct = (cur + 1) / max(total, 1) * 0.40
                overall_progress.progress(pct)
                status_widget.write(f"📥 ({cur + 1}/{total}) {fname[:60]}")

            pdfs = download_filings(selected_df, pdf_dir, dl_prog)
            status_widget.write(f"✅ 下载完成：{len(pdfs)}/{len(selected_df)} 份文件")
            overall_progress.progress(0.45)

            # Phase 2: Extract tables from PDFs
            status_widget.write("📊 提取表格（仅处理PDF文件）...")
            all_tables = []
            total_files = len(pdfs)

            for j, file_path in enumerate(pdfs):
                if not file_path.suffix.lower() == ".pdf":
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
                    status_widget.write(f"   {file_path.stem[:60]}: 跳过（非PDF或提取失败）")

            status_widget.write(f"✅ 共提取 {len(all_tables)} 个表格")
            overall_progress.progress(0.80)

            # Phase 3: Generate Excel
            if all_tables:
                status_widget.write("📝 生成Excel文件...")
                excel_files = write_tables_to_excel(
                    all_tables,
                    excel_dir,
                    ticker,
                    "SEC_Filings",
                    str(selected_df.iloc[0].get("filing_date", ""))[:4],
                    "、".join(set(str(x) for x in selected_df["doc_type"].tolist())),
                )
                if excel_files:
                    st.session_state.download_excel_path = str(excel_files[0])
                status_widget.write(f"✅ Excel生成完成")
            else:
                status_widget.write("⚠️ 未提取到表格")
            overall_progress.progress(0.92)

            # Phase 4: Zip all files
            zip_path = out_root / f"{ticker}_SEC_Filings.zip"
            all_files = pdfs
            if all_files:
                status_widget.write("📦 打包文件...")
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in all_files:
                        zf.write(p, p.name)
                status_widget.write(f"✅ 打包完成")
                st.session_state.download_zip_path = str(zip_path)

            overall_progress.progress(1.0)

            st.session_state.results = {
                "file_count": len(pdfs),
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
    "免责声明：本工具从SEC EDGAR (sec.gov) 获取公开披露信息，仅供学习研究使用。"
)
