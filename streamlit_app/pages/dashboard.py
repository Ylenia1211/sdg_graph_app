"""
Dashboard Analitica — pages/
----------------------------
Questa pagina Streamlit mostra KPI, distribuzioni, heatmap e trend relativi a
Progetti/Stakeholder/SDG. I dati provengono da endpoint REST (utils.api) e
vengono normalizzati in DataFrame per la visualizzazione con Plotly.

Stato (st.session_state) in questa pagina:
- Non introduce nuove chiavi persistenti. Si usano solo widget standard (radio,
  slider, multiselect) che non richiedono gestione manuale dello stato.
- L’unico stato globale toccato è lo "svuotamento cache" via st.cache_data.clear()
  all’azione del pulsante "Aggiorna dati", seguito da st.rerun() per forzare il refresh.

Input & Validazione:
- I filtri (multiselect per SDG/Settori, radio/slider nelle sezioni) determinano
  eventuali sottoinsiemi dei DataFrame; se un dataset o una colonna manca, il
  codice usa funzioni di normalizzazione (_safe_df, _agg_ts) e blocchi try/except
  per mantenere l’app robusta senza alterare la logica esistente.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
from utils import api

# ----------------------- Page Config -----------------------
st.set_page_config(
    page_title="Dashboard Analitica",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ----------------------- Theme / Helpers -------------------
PALETTE = ["#3c78d8", "#6aa84f", "#e06666", "#8e7cc3", "#ffd966", "#76a5af", "#0ea5e9", "#f59e0b"]
TEMPLATE = "plotly_white"

# 🔁 Force refresh (svuota cache)
col_refresh = st.columns([1, 8, 1])[0]
if col_refresh.button("Aggiorna dati"):
    # NOTE: pulisce la cache dei dati per forzare un nuovo fetch dagli endpoint.
    st.cache_data.clear()
    st.rerun()

if st.button("Torna indietro"):
    # Torna alla home dell'app (non modifica lo stato applicativo).
    st.switch_page("home.py")


def _safe_df(obj, cols=None):
    """Converte un oggetto (lista/dict/None) in DataFrame garantendo colonne opzionali.

    Args:
        obj: oggetto convertibile in DataFrame (es. lista di dict) o None.
        cols: elenco di colonne attese. Se fornite, verranno create se mancanti.

    Returns:
        pd.DataFrame: DataFrame con le colonne richieste (se specificate).
    """
    df = pd.DataFrame(obj or [])
    if cols:
        for c in cols:
            if c not in df.columns:
                df[c] = None
        return df[cols]
    return df


def _agg_ts(df, date_col="date", value_col="value", group_cols=None):
    """Normalizza e aggrega una serie temporale insiemistica.

    Logica:
      - Identifica la colonna data (fallback su ["date","day","dt","timestamp","period","month"]).
      - Identifica la colonna valore (fallback su ["value","n","count","projects","stakeholders"]).
      - Converte la data in datetime, scarta i NaT.
      - Aggrega per data (e per eventuali group_cols) sommandone i valori.

    Args:
        df (pd.DataFrame|None): input raw.
        date_col (str): nome colonna data se già coerente.
        value_col (str): nome colonna valore se già coerente.
        group_cols (list[str]|None): colonne di raggruppamento addizionali.

    Returns:
        pd.DataFrame: colonne ["date","value"] (+ group_cols se presenti), ordinato per date.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["date", "value"] + (group_cols or []))
    out = df.copy()
    # infer date col
    if date_col not in out.columns:
        for c in ["date", "day", "dt", "timestamp", "period", "month"]:
            if c in out.columns:
                date_col = c
                break
    # infer value col
    if value_col not in out.columns:
        for c in ["value", "n", "count", "projects", "stakeholders"]:
            if c in out.columns:
                value_col = c
                break
    out["__date__"] = pd.to_datetime(out[date_col], errors="coerce")
    out = out.dropna(subset=["__date__"])
    if group_cols:
        out = out.groupby(group_cols + ["__date__"], as_index=False)[value_col].sum()
    else:
        out = out.groupby(["__date__"], as_index=False)[value_col].sum()
    out = out.rename(columns={"__date__": "date", value_col: "value"})
    return out.sort_values("date")


def badge(text: str) -> str:
    """Ritorna HTML per etichette-pillola (UI).

    Args:
        text: testo da mostrare nel badge.

    Returns:
        str: HTML pronto per st.markdown(..., unsafe_allow_html=True).
    """
    return (
        '<span style="display:inline-block;background:#f1f5f9;border:1px solid #e5e7eb;'
        'border-radius:999px;padding:4px 10px;font-size:12px;color:#0f172a;'
        'margin-right:6px;margin-bottom:6px">'
        f"{text}</span>"
    )


def df_to_csv_download(df: pd.DataFrame, filename: str, label: str):
    """Crea un pulsante di download CSV per il DataFrame fornito.

    Args:
        df: DataFrame da esportare.
        filename: nome file CSV.
        label: etichetta del pulsante.
    """
    st.download_button(
        label=label,
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=filename,
        mime="text/csv",
        use_container_width=True
    )


def _coerce_list(v):
    """Normalizza un valore potenzialmente scalare in lista pulita.

    Regole:
      - None o stringa vuota -> lista vuota
      - lista/tuple/set -> rimuove None e stringhe vuote
      - altro -> lista con il valore

    Args:
        v: valore arbitrario.

    Returns:
        list: lista normalizzata.
    """
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple, set)):
        return [x for x in v if x is not None and str(x) != ""]
    return [v]


# ----------------------- Data Loader -----------------------
@st.cache_data(ttl=300)
def load_data():
    """Carica dati grezzi dagli endpoint e costruisce tutte le tabelle necessarie.

    Fonti:
      - /kpi principale e chiavi annidate (stake_by_sector, proj_by_sector, ...).
      - /sdg_keyword (se disponibile) o fallback da /projects_search e altri endpoint.
      - serie storiche aggregate e derivate per SDG e Settore.

    Ritorna:
        dict con DataFrame:
        {
          "kpi","sb","pb","sdg_sector",
          "projects_ts","stakeholders_ts","kpi_ts_long",
          "proj_by_sector_ts","stake_by_sector_ts",
          "sb_kw","pb_kw","sdg_keyword"
        }
    """
    data = api("/kpi", params=None) or {}

    # tabelle base
    kpi = _safe_df(data.get("kpi"), cols=["sdg", "projects", "stakeholders"])
    sb  = _safe_df(data.get("stake_by_sector"), cols=["sector", "n"])
    pb  = _safe_df(data.get("proj_by_sector"),  cols=["sector", "n"])

    # --- Stakeholder/Progetti per Keyword (se disponibili o derivati) ---
    sb_kw = _safe_df(data.get("stake_by_keyword"), cols=["keyword", "n"])
    pb_kw = _safe_df(data.get("proj_by_keyword"),  cols=["keyword", "n"])

    # Deriva Stakeholder × Keyword se mancante
    if sb_kw.empty:
        for ep in ["/stakeholders_flat", "/stakeholders", "/list/stakeholders"]:
            try:
                rows = api(ep, params=None)
            except Exception:
                continue
            if isinstance(rows, dict):
                for k in ["items", "stakeholders", "results", "data"]:
                    if k in rows and isinstance(rows[k], list):
                        rows = rows[k]
                        break
            if isinstance(rows, list) and rows:
                buf = []
                for s in rows:
                    kws = _coerce_list(s.get("keywords") or s.get("kws"))
                    for kw in kws:
                        buf.append({"keyword": str(kw), "n": 1})
                sb_kw = (
                    pd.DataFrame(buf).groupby("keyword", as_index=False)["n"].sum()
                    if buf else pd.DataFrame(columns=["keyword", "n"])
                )
                break

    # Deriva Progetti × Keyword se mancante
    if pb_kw.empty:
        for ep in ["/projects_flat", "/projects_min", "/projects"]:
            try:
                rows = api(ep, params=None)
            except Exception:
                continue
            if isinstance(rows, dict):
                for k in ["items", "projects", "results", "data"]:
                    if k in rows and isinstance(rows[k], list):
                        rows = rows[k]
                        break
            if isinstance(rows, list) and rows:
                buf = []
                for p in rows:
                    kws = _coerce_list(p.get("keywords") or p.get("kws"))
                    for kw in kws:
                        buf.append({"keyword": str(kw), "n": 1})
                pb_kw = (
                    pd.DataFrame(buf).groupby("keyword", as_index=False)["n"].sum()
                    if buf else pd.DataFrame(columns=["keyword", "n"])
                )
                break

    # --------- SDG × Settore ---------
    sdg_sector = None
    for key in ["sdg_sector", "sdg_by_sector", "proj_sdg_sector", "kpi_sdg_sector"]:
        if key in data:
            sdg_sector = data.get(key)
            break
    sdg_sector = _safe_df(sdg_sector, cols=["sdg", "sector", "n"])

    # Prova a derivarlo dai progetti se mancante
    def _derive_sector_from_projects(records):
        """Deriva matrice SDG×Settore da una lista di progetti con campi sdg/sectors."""
        rows = []
        for p in records or []:
            sdgs = _coerce_list(p.get("sdg")) or _coerce_list(p.get("sdgs")) or _coerce_list(p.get("sdg_codes"))
            sectors = _coerce_list(p.get("sector")) or _coerce_list(p.get("sectors"))
            if not sdgs or not sectors:
                continue
            for s in sdgs:
                for sec in sectors:
                    rows.append({"sdg": str(s), "sector": str(sec), "n": 1})
        df = pd.DataFrame(rows)
        return df.groupby(["sdg", "sector"], as_index=False)["n"].sum() if not df.empty else df

    if sdg_sector.empty:
        for ep in ["/projects_flat", "/projects_min", "/projects"]:
            try:
                projs = api(ep, params=None)
            except Exception:
                continue
            if isinstance(projs, dict):
                for key in ["items", "projects", "data", "results"]:
                    if key in projs and isinstance(projs[key], list):
                        projs = projs[key]
                        break
            if isinstance(projs, list) and projs:
                derived = _derive_sector_from_projects(projs)
                if not derived.empty:
                    sdg_sector = derived
                    break

    # --------- SDG × Keyword (ENDPOINT DEDICATO + FALLBACK) ---------
    # 0) tentativo con endpoint dedicato /sdg_keyword
    sdg_keyword = pd.DataFrame(columns=["sdg", "keyword", "n"])
    try:
        dk = api("/sdg_keyword", params=None) or {}
        sdg_keyword = _safe_df(dk.get("sdg_keyword"), cols=["sdg", "keyword", "n"])
    except Exception:
        pass

    # 1) fallback dal payload /kpi con nomi vari
    if sdg_keyword.empty:
        raw = None
        for key in ["sdg_keyword", "sdg_by_keyword", "keyword_sdg", "kpi_sdg_keyword"]:
            if key in data:
                raw = data.get(key)
                break
        sdg_keyword = _safe_df(raw, cols=["sdg", "keyword", "n"])

    # 2) ulteriore fallback da /projects_search (batching paginato)
    def _fetch_projects_kw_sdg(max_pages=5, page_size=1000):
        """Scarica batch di progetti con keyword e sdg per derivare SDG×Keyword."""
        acc = []
        for i in range(max_pages):
            try:
                res = api("/projects_search", params={"q": "", "limit": page_size, "offset": i*page_size}) or {}
            except Exception:
                break
            items = res.get("projects") or res.get("items") or []
            if not items:
                break
            acc.extend(items)
            tot = res.get("total")
            if isinstance(tot, int) and len(acc) >= tot:
                break
            if len(items) < page_size:
                break
        return acc

    def _derive_kw_from_projects(records):
        """Crea conteggio SDG×Keyword (n) dato un elenco di progetti con campi sdg/keywords."""
        rows = []
        for p in records or []:
            sdgs_l = _coerce_list(p.get("sdg")) or _coerce_list(p.get("sdgs")) or _coerce_list(p.get("sdg_codes"))
            kws_l  = _coerce_list(p.get("keywords")) or _coerce_list(p.get("kws"))
            if not sdgs_l or not kws_l:
                continue
            for s in sdgs_l:
                for kw in kws_l:
                    kws_str = str(kw).strip()
                    if kws_str:
                        rows.append({"sdg": str(s), "keyword": kws_str, "n": 1})
        df = pd.DataFrame(rows)
        return df.groupby(["sdg", "keyword"], as_index=False)["n"].sum() if not df.empty else df

    if sdg_keyword.empty:
        projs_mix = _fetch_projects_kw_sdg(max_pages=5, page_size=1000)
        derived_kw = _derive_kw_from_projects(projs_mix)
        if not derived_kw.empty:
            sdg_keyword = derived_kw
        else:
            # 3) fallback legacy: endpoints vecchi
            for ep in ["/projects_flat", "/projects_min"]:
                try:
                    rows = api(ep, params=None)
                except Exception:
                    continue
                if isinstance(rows, dict):
                    for key in ["items", "projects", "data", "results"]:
                        if key in rows and isinstance(rows[key], list):
                            rows = rows[key]
                            break
                if isinstance(rows, list) and rows:
                    derived_kw = _derive_kw_from_projects(rows)
                    if not derived_kw.empty:
                        sdg_keyword = derived_kw
                        break

    # --------- Time series ---------
    projects_ts     = _safe_df(data.get("projects_ts"))
    stakeholders_ts = _safe_df(data.get("stakeholders_ts"))
    kpi_ts          = _safe_df(data.get("kpi_ts"))

    # fallback: history dentro kpi
    if kpi_ts.empty and not kpi.empty and "history" in kpi.columns:
        blocks = []
        for _, row in kpi.iterrows():
            for h in row.get("history") or []:
                blocks.append({
                    "sdg": row.get("sdg"),
                    "date": h.get("date"),
                    "projects": h.get("projects"),
                    "stakeholders": h.get("stakeholders"),
                })
        kpi_ts = _safe_df(blocks)

    if projects_ts.empty and not kpi_ts.empty and "projects" in kpi_ts.columns:
        projects_ts = kpi_ts.groupby("date", as_index=False)["projects"].sum().rename(columns={"projects": "value"})
    if stakeholders_ts.empty and not kpi_ts.empty and "stakeholders" in kpi_ts.columns:
        stakeholders_ts = kpi_ts.groupby("date", as_index=False)["stakeholders"].sum().rename(columns={"stakeholders": "value"})

    projects_ts     = _agg_ts(projects_ts)
    stakeholders_ts = _agg_ts(stakeholders_ts)

    # long per SDG (TS)
    kpi_ts_long = pd.DataFrame()
    if not kpi_ts.empty and {"sdg", "date"}.issubset(kpi_ts.columns):
        cols_val = [c for c in ["projects", "stakeholders", "value"] if c in kpi_ts.columns]
        for c in cols_val:
            part = kpi_ts[["sdg", "date", c]].dropna().rename(columns={c: "value"})
            part["metric"] = c
            kpi_ts_long = pd.concat([kpi_ts_long, part], ignore_index=True)
        kpi_ts_long["date"] = pd.to_datetime(kpi_ts_long["date"], errors="coerce")
        kpi_ts_long = kpi_ts_long.dropna(subset=["date"]).sort_values(["metric", "sdg", "date"])

    # serie per settore (opzionali)
    proj_by_sector_ts  = _agg_ts(_safe_df(data.get("proj_by_sector_ts")),  group_cols=["sector"])
    stake_by_sector_ts = _agg_ts(_safe_df(data.get("stake_by_sector_ts")), group_cols=["sector"])

    return {
        "kpi": kpi, "sb": sb, "pb": pb,
        "sdg_sector": sdg_sector,
        "projects_ts": projects_ts,
        "stakeholders_ts": stakeholders_ts,
        "kpi_ts_long": kpi_ts_long,
        "proj_by_sector_ts": proj_by_sector_ts,
        "stake_by_sector_ts": stake_by_sector_ts,
        "sb_kw": sb_kw,
        "pb_kw": pb_kw,
        "sdg_keyword": sdg_keyword,  # <<< popolato via /sdg_keyword o fallback
    }


# ----------------------- Header ---------------------------
st.title("Dashboard Analitica")
st.caption("Sintesi di progetti, stakeholder e copertura SDG — con trend, distribuzioni e heatmap SDG × Settori e **Keywords**.")

# ----------------------- Data -----------------------------
D = load_data()
kpi = D["kpi"]; sb = D["sb"]; pb = D["pb"]

# ----------------------- Filters --------------------------
with st.container():
    f1, f2, f3 = st.columns([2, 2, 6])
    with f1:
        sdg_opts = sorted(kpi["sdg"].dropna().unique().tolist()) if not kpi.empty else []
        sdg_sel = st.multiselect("Filtra SDG", options=sdg_opts, default=sdg_opts[: min(6, len(sdg_opts))])
    with f2:
        sector_opts = sorted(set(sb["sector"].dropna().unique().tolist() + pb["sector"].dropna().unique().tolist()))
        sector_sel = st.multiselect("Filtra Settore", options=sector_opts, default=sector_opts[: min(6, len(sector_opts))])
    with f3:
        st.markdown(
            badge(f"SDG disponibili: {len(sdg_opts)}") + " " +
            badge(f"Settori disponibili: {len(sector_opts)}"),
            unsafe_allow_html=True
        )

# Applica filtri base
kpi_f = kpi[kpi["sdg"].isin(sdg_sel)] if sdg_sel else kpi.copy()
sb_f  = sb[sb["sector"].isin(sector_sel)] if sector_sel else sb.copy()
pb_f  = pb[pb["sector"].isin(sector_sel)] if sector_sel else pb.copy()

# ----------------------- KPI Cards (2 righe) ---------------
c1, c2, c3 = st.columns(3)
with c1:
    st.metric("SDG coperti", int(kpi["sdg"].nunique()) if not kpi.empty else 0)
with c2:
    tot_projects = int(kpi["projects"].sum()) if not kpi.empty else 0
    st.metric("Totale Progetti", tot_projects)
with c3:
    tot_stake = int(kpi["stakeholders"].sum()) if not kpi.empty else 0
    st.metric("Totale Stakeholder", tot_stake)

c4, c5, c6 = st.columns(3)
with c4:
    coverage = int((kpi["projects"] > 0).sum()) if not kpi.empty else 0
    st.metric("SDG con almeno 1 progetto", coverage)
with c5:
    avg_proj_per_sdg = float(kpi["projects"].mean()) if not kpi.empty else 0.0
    st.metric("Media Progetti/SDG", f"{avg_proj_per_sdg:.1f}")
with c6:
    avg_stk_per_proj = (tot_stake / tot_projects) if tot_projects > 0 else 0.0
    st.metric("Media Stakeholder/Progetto", f"{avg_stk_per_proj:.2f}")

st.divider()

# ----------------------- Tabs ------------------------------
tab_overview, tab_settori, tab_keywords, tab_trend, tab_dati = st.tabs(
    ["Overview", "Settori", "Keywords", "Trend", "Esporta Dati"]
)

# ======================= OVERVIEW =========================
with tab_overview:
    if kpi_f.empty:
        st.info("Non ci sono dati KPI da visualizzare con i filtri correnti.")
    else:
        g1, g2 = st.columns((2, 1), gap="large")
        with g1:
            st.subheader("Progetti & Stakeholder per SDG")
            fig = px.bar(
                kpi_f.sort_values("sdg"),
                x="sdg", y=["projects", "stakeholders"],
                barmode="group",
                color_discrete_sequence=PALETTE, template=TEMPLATE
            )
            fig.update_layout(legend_title_text="", margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig, use_container_width=True, theme=None)

        with g2:
            st.subheader("Distribuzione progetti (SDG)")
            fig_donut_p = px.pie(
                kpi_f, names="sdg", values="projects",
                hole=0.55, color_discrete_sequence=PALETTE, template=TEMPLATE
            )
            fig_donut_p.update_traces(textposition="inside", textinfo="percent+label")
            fig_donut_p.update_layout(showlegend=False, margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig_donut_p, use_container_width=True, theme=None)

        st.subheader("Incidenza Stakeholder per SDG (percentuale)")
        pct = kpi_f.copy()
        tot_stk_sel = pct["stakeholders"].sum()
        if tot_stk_sel > 0:
            pct["% Stakeholder"] = (pct["stakeholders"] / tot_stk_sel * 100).round(1)
            fig_pct = px.bar(
                pct.sort_values("% Stakeholder", ascending=False),
                x="sdg", y="% Stakeholder",
                text="% Stakeholder",
                color_discrete_sequence=[PALETTE[1]], template=TEMPLATE
            )
            fig_pct.update_traces(texttemplate="%{text}%", textposition="outside")
            fig_pct.update_layout(yaxis_title="", xaxis_title="SDG", margin=dict(l=0, r=0, t=20, b=0))
            st.plotly_chart(fig_pct, use_container_width=True, theme=None)
        else:
            st.info("Nessuno stakeholder nei filtri correnti.")

# ======================= SETTORI ==========================
with tab_settori:
    st.subheader("Distribuzioni per Settore / Keyword")

    a, b = st.columns(2, gap="large")

    # ==== Stakeholder ====
    with a:
        st.markdown("**Stakeholder: distribuzione**")
        grp_stk = st.radio("Raggruppa per", ["Settore", "Keyword"], horizontal=True, key="grp_stk_radio")
        topn_stk = st.slider("Top-N", 5, 30, 15, 1, key="topn_stk_slider")

        if grp_stk == "Settore":
            df_plot = sb_f.rename(columns={"sector": "label", "n": "value"})
        else:
            sb_kw_local = D.get("sb_kw", pd.DataFrame())
            df_plot = sb_kw_local.rename(columns={"keyword": "label", "n": "value"}) if not sb_kw_local.empty else pd.DataFrame(columns=["label", "value"])

        if df_plot.empty:
            st.info("Nessun dato disponibile.")
        else:
            df_plot = df_plot.sort_values("value", ascending=False).head(topn_stk)
            fig_sb_any = px.bar(
                df_plot, x="label", y="value",
                color_discrete_sequence=[PALETTE[0]], template=TEMPLATE
            )
            fig_sb_any.update_layout(xaxis_title=grp_stk, yaxis_title="Stakeholder", margin=dict(l=0, r=0, t=20, b=0))
            st.plotly_chart(fig_sb_any, use_container_width=True, theme=None)

    # ==== Progetti ====
    with b:
        st.markdown("**Progetti: distribuzione**")
        grp_prj = st.radio("Raggruppa per", ["Settore", "Keyword"], horizontal=True, key="grp_prj_radio")
        topn_prj = st.slider("Top-N ", 5, 30, 15, 1, key="topn_prj_slider")

        if grp_prj == "Settore":
            df_plot = pb_f.rename(columns={"sector": "label", "n": "value"})
        else:
            pb_kw_local = D.get("pb_kw", pd.DataFrame())
            df_plot = pb_kw_local.rename(columns={"keyword": "label", "n": "value"}) if not pb_kw_local.empty else pd.DataFrame(columns=["label", "value"])

        if df_plot.empty:
            st.info("Nessun dato disponibile.")
        else:
            df_plot = df_plot.sort_values("value", ascending=False).head(topn_prj)
            fig_pb_any = px.bar(
                df_plot, x="label", y="value",
                color_discrete_sequence=[PALETTE[2]], template=TEMPLATE
            )
            fig_pb_any.update_layout(xaxis_title=grp_prj, yaxis_title="Progetti", margin=dict(l=0, r=0, t=20, b=0))
            st.plotly_chart(fig_pb_any, use_container_width=True, theme=None)

    st.markdown("---")
    st.subheader("Heatmap SDG × Settori")

    H = D["sdg_sector"].copy()
    if not H.empty:
        # Applica filtri globali
        if sdg_sel:
            H = H[H["sdg"].isin(sdg_sel)]
        if sector_sel:
            H = H[H["sector"].isin(sector_sel)]

        if H.empty:
            st.info("Nessun dato per la heatmap con i filtri correnti.")
        else:
            # pivot
            mat = H.pivot_table(index="sdg", columns="sector", values="n", aggfunc="sum", fill_value=0)
            mode = st.radio(
                "Modalità valori",
                options=["Assoluto", "% per SDG (riga)", "% per Settore (colonna)"],
                index=0,
                horizontal=True
            )

            mat_disp = mat.copy()
            hover_suffix = ""
            if mode == "% per SDG (riga)":
                row_sum = mat_disp.sum(axis=1).replace(0, pd.NA)
                mat_disp = (mat_disp.div(row_sum, axis=0) * 100).round(1)
                hover_suffix = "%"
            elif mode == "% per Settore (colonna)":
                col_sum = mat_disp.sum(axis=0).replace(0, pd.NA)
                mat_disp = (mat_disp.div(col_sum, axis=1) * 100).round(1)
                hover_suffix = "%"

            fig_hm = px.imshow(
                mat_disp,
                color_continuous_scale="Blues",
                aspect="auto",
                labels=dict(x="Settore", y="SDG", color="Valore"),
                text_auto=True
            )
            fig_hm.update_layout(
                margin=dict(l=0, r=0, t=20, b=0),
                coloraxis_colorbar=dict(title="Valore"),
                xaxis_title="Settore", yaxis_title="SDG"
            )
            fig_hm.update_traces(
                hovertemplate="SDG: %{y}<br>Settore: %{x}<br>Valore: %{z}" + hover_suffix + "<extra></extra>"
            )
            st.plotly_chart(fig_hm, use_container_width=True, theme=None)

            # Download matrice
            st.caption("Scarica matrice")
            st.download_button(
                "⬇️ Esporta matrice (CSV)",
                data=mat_disp.reset_index().rename_axis(None, axis=1).to_csv(index=False).encode("utf-8"),
                file_name="heatmap_sdg_settori.csv",
                mime="text/csv",
            )
    else:
        st.info("Per la heatmap serve una tabella con colonne `sdg, sector, n` (es. chiave `sdg_sector`).")

# ======================= KEYWORDS (NUOVO) =================
with tab_keywords:
    st.subheader("Keywords su SDG")

    KW = D.get("sdg_keyword", pd.DataFrame()).copy()
    if KW.empty:
        st.info("Non ci sono dati SDG × Keyword. Assicurati che /sdg_keyword o /projects_search ritornino campi 'keyword' e 'sdg'.")
    else:
        # Diagnostica rapida
        st.caption(f"Righe: {len(KW)} — SDG unici: {KW['sdg'].nunique()} — Keyword uniche: {KW['keyword'].nunique()}")

        # Filtri locali per la tab
        colk = st.columns([2, 2, 2, 2])
        with colk[0]:
            sdg_k_opts = sorted(KW["sdg"].dropna().unique().tolist())
            sdg_k_sel = st.multiselect("SDG", options=sdg_k_opts, default=sdg_k_opts[:10])
        with colk[1]:
            txt_kw = st.text_input("Cerca keyword", placeholder="es. solar, hydrogen…").strip()
        with colk[2]:
            topN = st.slider("Top-N keyword (per heatmap)", 5, 50, 20, 1)
        with colk[3]:
            mode_kw = st.selectbox("Modalità", ["Assoluto", "% per SDG (riga)"], index=0)

        # Applica filtri
        if sdg_k_sel:
            KW = KW[KW["sdg"].isin(sdg_k_sel)]
        if txt_kw:
            KW = KW[KW["keyword"].str.contains(txt_kw, case=False, na=False)]

        if KW.empty:
            st.info("Nessun dato con i filtri impostati.")
        else:
            # Seleziona le Top-N keyword globali (sul dataset filtrato)
            top_keywords = (
                KW.groupby("keyword", as_index=False)["n"].sum()
                .sort_values("n", ascending=False)
                .head(topN)["keyword"].tolist()
            )
            KWh = KW[KW["keyword"].isin(top_keywords)].copy()

            # pivot SDG × Keyword
            matkw = KWh.pivot_table(index="sdg", columns="keyword", values="n", aggfunc="sum", fill_value=0)

            # percentuale per riga (SDG)
            mat_disp = matkw.copy()
            suffix = ""
            if mode_kw == "% per SDG (riga)":
                row_sum = mat_disp.sum(axis=1).replace(0, pd.NA)
                mat_disp = (mat_disp.div(row_sum, axis=0) * 100).round(1)
                suffix = "%"

            st.markdown("**Heatmap SDG × Keyword (Top-N)**")
            fig_kw = px.imshow(
                mat_disp,
                color_continuous_scale="Oranges",
                aspect="auto",
                labels=dict(x="Keyword", y="SDG", color="Valore"),
                text_auto=True
            )
            fig_kw.update_layout(margin=dict(l=0, r=0, t=20, b=0))
            fig_kw.update_traces(
                hovertemplate="SDG: %{y}<br>Keyword: %{x}<br>Valore: %{z}" + suffix + "<extra></extra>"
            )
            st.plotly_chart(fig_kw, use_container_width=True, theme=None)

            # Bar: Top keyword per un singolo SDG
            st.markdown("---")
            st.subheader("Top Keyword per SDG")
            colb = st.columns([2, 1])
            with colb[0]:
                sdg_pick_kw = st.selectbox("SDG", sorted(KW["sdg"].dropna().unique().tolist()))
            with colb[1]:
                topN_bar = st.slider("Top-N (bar)", 5, 40, 15, 1)

            KWs = KW[KW["sdg"] == sdg_pick_kw].groupby("keyword", as_index=False)["n"].sum()
            if KWs.empty:
                st.info("Nessuna keyword per lo SDG selezionato.")
            else:
                KWs = KWs.sort_values("n", ascending=False).head(topN_bar)
                fig_bar_kw = px.bar(
                    KWs, x="keyword", y="n",
                    color_discrete_sequence=[PALETTE[4]], template=TEMPLATE,
                    title=f"Top Keyword — {sdg_pick_kw}"
                )
                fig_bar_kw.update_layout(margin=dict(l=0, r=0, t=40, b=0), xaxis_title="", yaxis_title="Occorrenze")
                st.plotly_chart(fig_bar_kw, use_container_width=True, theme=None)

            # Download CSV (dataset filtrato corrente)
            st.caption("Scarica dati Keyword × SDG (filtri correnti)")
            st.download_button(
                "⬇️ Esporta SDG×Keyword (CSV)",
                data=KW.to_csv(index=False).encode("utf-8"),
                file_name="sdg_keyword_filtrato.csv",
                mime="text/csv",
            )

# ======================= TREND (lineplot) =================
with tab_trend:
    st.subheader("Andamento nel tempo")
    ts_cols = st.columns(2, gap="large")

    # Totale Progetti nel tempo
    with ts_cols[0]:
        if not D["projects_ts"].empty:
            fig_lp = px.line(
                D["projects_ts"], x="date", y="value",
                markers=True, color_discrete_sequence=[PALETTE[0]], template=TEMPLATE,
                title="Totale Progetti"
            )
            fig_lp.update_layout(margin=dict(l=0, r=0, t=40, b=0), xaxis_title="", yaxis_title="")
            st.plotly_chart(fig_lp, use_container_width=True, theme=None)
        else:
            st.info("Serie storica Progetti non disponibile.")

    # Totale Stakeholder nel tempo
    with ts_cols[1]:
        if not D["stakeholders_ts"].empty:
            fig_ls = px.line(
                D["stakeholders_ts"], x="date", y="value",
                markers=True, color_discrete_sequence=[PALETTE[1]], template=TEMPLATE,
                title="Totale Stakeholder"
            )
            fig_ls.update_layout(margin=dict(l=0, r=0, t=40, b=0), xaxis_title="", yaxis_title="")
            st.plotly_chart(fig_ls, use_container_width=True, theme=None)
        else:
            st.info("Serie storica Stakeholder non disponibile.")

    st.markdown("—")
    st.subheader("Trend per SDG")
    if not D["kpi_ts_long"].empty:
        sdg_opts_tr = sorted(D["kpi_ts_long"]["sdg"].dropna().unique().tolist())
        metric_opts = ["projects", "stakeholders"]
        col_sel = st.columns(3)
        with col_sel[0]:
            sdg_pick = st.multiselect("SDG", sdg_opts_tr, default=sdg_opts_tr[:min(6, len(sdg_opts_tr))])
        with col_sel[1]:
            metric_pick = st.selectbox("Metrica", metric_opts, index=0)
        with col_sel[2]:
            smooth = st.checkbox("Media mobile (7)", value=False)

        dplot = D["kpi_ts_long"].copy()
        dplot = dplot[dplot["metric"] == metric_pick]
        if sdg_pick:
            dplot = dplot[dplot["sdg"].isin(sdg_pick)]

        if dplot.empty:
            st.info("Nessun dato da mostrare con i filtri selezionati.")
        else:
            dplot = dplot.sort_values(["sdg", "date"])
            ycol = "value"
            title_suffix = ""
            if smooth:
                dplot["value_sma"] = dplot.groupby("sdg")["value"].transform(lambda s: s.rolling(7, min_periods=1).mean())
                ycol = "value_sma"
                title_suffix = " (SMA 7)"
            fig_sdgl = px.line(
                dplot, x="date", y=ycol, color="sdg",
                color_discrete_sequence=PALETTE, template=TEMPLATE,
                title=f"Andamento {metric_pick}{title_suffix} per SDG"
            )
            fig_sdgl.update_layout(margin=dict(l=0, r=0, t=40, b=0), xaxis_title="", yaxis_title="")
            st.plotly_chart(fig_sdgl, use_container_width=True, theme=None)
    else:
        st.info("Serie storiche per SDG non disponibili.")

    st.markdown("—")
    st.subheader("Trend per Settore (se disponibile)")
    two = st.columns(2, gap="large")
    with two[0]:
        if not D["proj_by_sector_ts"].empty:
            fig_ps = px.line(
                D["proj_by_sector_ts"], x="date", y="value", color="sector",
                color_discrete_sequence=PALETTE, template=TEMPLATE,
                title="Progetti per Settore"
            )
            fig_ps.update_layout(margin=dict(l=0, r=0, t=40, b=0), xaxis_title="", yaxis_title="")
            st.plotly_chart(fig_ps, use_container_width=True, theme=None)
        else:
            st.info("Serie storiche Progetti × Settore non disponibili.")
    with two[1]:
        if not D["stake_by_sector_ts"].empty:
            fig_ss = px.line(
                D["stake_by_sector_ts"], x="date", y="value", color="sector",
                color_discrete_sequence=PALETTE, template=TEMPLATE,
                title="Stakeholder per Settore"
            )
            fig_ss.update_layout(margin=dict(l=0, r=0, t=40, b=0), xaxis_title="", yaxis_title="")
            st.plotly_chart(fig_ss, use_container_width=True, theme=None)
        else:
            st.info("Serie storiche Stakeholder × Settore non disponibili.")

# ======================= ESPORTA DATI =====================
with tab_dati:
    st.subheader("Tabelle e download")
    t1, t2, t3 = st.columns(3)
    with t1:
        st.caption("KPI per SDG")
        st.dataframe(kpi, use_container_width=True, hide_index=True)
        if not kpi.empty:
            df_to_csv_download(kpi, "kpi_sdg.csv", "Scarica KPI (CSV)")
    with t2:
        st.caption("Stakeholder per Settore")
        st.dataframe(sb, use_container_width=True, hide_index=True)
        if not sb.empty:
            df_to_csv_download(sb, "stakeholder_per_settore.csv", "Scarica Stakeholder/Settore (CSV)")
    with t3:
        st.caption("Progetti per Settore")
        st.dataframe(pb, use_container_width=True, hide_index=True)
        if not pb.empty:
            df_to_csv_download(pb, "progetti_per_settore.csv", "Scarica Progetti/Settore (CSV)")

    st.markdown("---")
    st.caption("SDG × Keyword (dataset completo)")
    KW_all = D.get("sdg_keyword", pd.DataFrame())
    st.dataframe(KW_all, use_container_width=True, hide_index=True)
    if not KW_all.empty:
        df_to_csv_download(KW_all, "sdg_keyword.csv", "Scarica SDG×Keyword (CSV)")