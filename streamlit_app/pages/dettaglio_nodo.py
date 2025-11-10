"""
Dettaglio Nodo — pages/dettaglio_nodo.py
----------------------------------------

Pagina Streamlit per esplorare il dettaglio di un nodo (stakeholder, project, keyword, sdg):
- intestazione sticky con info sintetiche, chip e indicatori rapidi
- tabelle di relazioni (progetti, stakeholder, keyword, SDG) con azioni rapide
- sezione LOD (Linked Open Data) con collegamenti e filtri
- calcolo percorso più breve (shortest path) tra due nodi scelti dall’utente
- ego-grafo locale con esportazione PNG/CSV/JSON

Stato (st.session_state) usato:
- "selected_node": id del nodo attivo (impostato qui o dalla home).
- "path_src": id nodo sorgente per calcolo percorso A→B (settato dai pulsanti "A").
- "path_tgt": id nodo destinazione per percorso (settato dai pulsanti "B").
Queste chiavi vengono solo lette/scritte per navigazione e calcolo; non cambiano la logica dei dati.

Input & Validazione:
- Parametri di query: "node" (opzionale). Se mancante, si tenta "selected_node" da sessione.
- Le richieste API possono sollevare requests.HTTPError: gestite con messaggi user-friendly e stop().
- I payload sono normalizzati/controllati (id/stringhe/None) per evitare crash UI.
"""

import io
import json
import requests
import pandas as pd
import matplotlib.pyplot as plt
import networkx as nx
import streamlit as st
from typing import Optional, Tuple, List, Dict
from utils import api
from streamlit_agraph import agraph, Node, Edge, Config

# (opzionale) lettura dimensioni finestra; se non disponibile usiamo fallback
try:
    from streamlit_js_eval import get_window_size
except Exception:  # la libreria potrebbe non esserci, gestiamo gracefully
    get_window_size = None

# -------------------------- Config & Theme --------------------------
st.set_page_config(page_title="Dettaglio Nodo", page_icon="🕸️", layout="wide", initial_sidebar_state="collapsed")

# Global palette (riusa ovunque)
PALETTE = {
    "Stakeholder": "#6aa84f",
    "Project":     "#3c78d8",
    "Keyword":     "#ffd966",
    "SDG":         "#e06666",
    "Sector":      "#8e7cc3",
    "LODEntity":   "#76a5af",
    "Node":        "#999999",
    "Edge":        "#8a8a8a",
    "Accent":      "#0f172a",
    "MutedBg":     "#f6f7fb",
    "CardBg":      "#ffffff",
}

SIZE = {
    "Stakeholder": 24,
    "Project":     22,
    "Keyword":     14,
    "SDG":         16,
    "Sector":      12,
    "LODEntity":   12,
    "Node":        12,
}

# -------------------------- CSS --------------------------
st.markdown(
    """
    <style>
      .app-card {
        background: var(--card-bg, #ffffff);
        border: 1px solid #eceef3;
        border-radius: 14px;
        padding: 14px 16px;
        box-shadow: 0 2px 10px rgba(16,24,40,0.04);
        margin-bottom: 10px;
      }
      .sticky-hero { position: sticky; top: 0; z-index: 10; background: var(--card-bg, #ffffff); }
      .hero-title { margin: 0; padding: 0; line-height: 1.2; }
      .chip {
        display:inline-block; padding: 3px 8px; border-radius: 999px;
        background:#f1f5f9; border:1px solid #e2e8f0;
        font-size: 12px; color:#0f172a; margin-right:6px; margin-top:6px;
      }
      .stats { display:flex; gap:10px; flex-wrap: wrap; margin-top:8px; }
      .stat {
        flex:1 1 120px; min-width:120px;
        border:1px solid #eceef3; border-radius:12px; padding:8px 10px;
        text-align:center; background:#fafbff;
      }
      .stat .value { font-size:18px; font-weight:700; line-height:1; }
      .stat .label { font-size:12px; color:#475569; }
      .hero-actions { display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
      .legend-dot { width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:6px; }
      .stMetric { text-align:center; }
      .dataframe { border-radius:10px !important; overflow: hidden; }
      footer, #MainMenu { visibility: hidden; }
      .full-bleed { width: 100vw; position: relative; left: 50%; right: 50%; margin-left: -50vw; margin-right: -50vw; }
      .download-row { display:flex; gap:8px; flex-wrap:wrap; margin:8px 0 4px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown("""
<style>
html, body,
[data-testid="stAppViewContainer"],
main, .main,
.main .block-container { background: var(--card-bg, #ffffff) !important; }
.main .block-container { padding-top: 0 !important; }
.block-container > :first-child { margin-top: 0 !important; }
.app-card.sticky-hero { margin-top: 0 !important; border-top: 0; }
.app-card.sticky-hero .hero-title { margin-top: 0 !important; }
div[data-testid="stDecoration"] { display: none !important; }
header[data-testid="stHeader"] {
  background: transparent !important; box-shadow: none !important;
  height: 0 !important; min-height: 0 !important; padding: 0 !important;
  margin: 0 !important; overflow: hidden !important;
}
div[data-testid="stToolbar"] { display: none !important; }
.app-card.sticky-hero {
  position: sticky; top: env(safe-area-inset-top, 0px);
  transform: translateZ(0); will-change: transform; z-index: 10;
}
</style>
""", unsafe_allow_html=True)

# -------------------------- Helpers --------------------------
def _norm_source(s: str) -> str:
    """Normalizza i nomi di fonte LOD in etichette canoniche.

    Args:
        s: nome della fonte dal payload.

    Returns:
        "Wikidata" | "DBpedia Spotlight" | "GeoNames" | stringa ripulita | "—" se vuota.
    """
    if not s:
        return "—"
    s_low = str(s).strip().lower()
    if "wikidata" in s_low: return "Wikidata"
    if "dbpedia" in s_low:  return "DBpedia Spotlight"
    if "geonames" in s_low or "geo names" in s_low: return "GeoNames"
    return s.strip()

def chip(text):
    """Ritorna HTML per un piccolo badge ('chip') neutro."""
    return f'<span class="chip">{text}</span>'

def df_projects_with_rows(lst):
    """Crea DataFrame + righe strutturate per una lista di progetti.

    Args:
        lst: lista di dict con chiavi "id","name","location".

    Returns:
        (DataFrame, list[dict]): tabella e righe ripassate all'UI/azioni.
    """
    if not lst: return pd.DataFrame(columns=["ID","Nome","Location"]), []
    rows = [{"ID":x.get("id"), "Nome":x.get("name"), "Location":x.get("location")} for x in lst]
    return pd.DataFrame(rows), rows

def df_stakeholders_with_rows(lst):
    """Crea DataFrame + righe per lista stakeholder (id, name, type)."""
    if not lst: return pd.DataFrame(columns=["ID","Nome","Tipo"]), []
    rows = [{"ID":x.get("id"), "Nome":x.get("name"), "Tipo":x.get("type")} for x in lst]
    return pd.DataFrame(rows), rows

def df_simple_col(name, values):
    """Crea DataFrame a singola colonna a partire da una lista di valori."""
    rows = [{name: v} for v in (values or [])]
    return pd.DataFrame(rows), rows

def tag_list(lst):
    """Rende una lista di tag come stringa HTML di chip (ordinati, unici)."""
    vals = sorted({str(x) for x in (lst or [])})
    if not vals: return "—"
    return " ".join([chip(v) for v in vals])

def action_buttons_for_rows(rows, build_node_id, open_label="Apri", add_to_path=True, prefix="section"):
    """Crea un insieme di popover/expander per ogni riga con pulsanti di azione.

    Args:
        rows: lista di righe (dict) da cui costruire i popover/expander.
        build_node_id: funzione lambda r -> "type:id" usata per apertura dettaglio.
        open_label: etichetta per il pulsante di apertura dettaglio.
        add_to_path: se True, mostra pulsanti "A"/"B" per percorso.
        prefix: prefisso univoco per le chiavi dei widget.

    Side effects:
        - Può chiamare _open_detail e _set_path in risposta ai click.
    """
    if not rows: return
    has_popover = hasattr(st, "popover")
    st.caption("Azioni per elemento:")
    grid = st.columns(3, gap="small")
    for i, r in enumerate(rows):
        node_id = build_node_id(r)
        unique = f"{prefix}__{node_id}__{i}"
        title = f"⚙️ {r.get('Nome') or r.get('ID') or r}"
        with grid[i % 3]:
            ctx = st.popover(title) if has_popover else st.expander(title, expanded=False)
            with ctx:
                st.caption("Scegli un'azione")
                if st.button(f"🔎 {open_label}", use_container_width=True, key=f"open__{unique}"):
                    _open_detail(node_id)
                if add_to_path:
                    if st.button("A", use_container_width=True, key=f"addA__{unique}"):
                        _set_path("A", node_id)
                    if st.button("B", use_container_width=True, key=f"addB__{unique}"):
                        _set_path("B", node_id)

def _value_or_dash(x):
    """Restituisce x se valorizzato, altrimenti un trattino '—' per la UI."""
    return x if (x is not None and str(x).strip() != "") else "—"

def _hero_stats(label, data, node):
    """Determina fino a tre indicatori sintetici per l'header in base al tipo.

    Args:
        label: tipo principale ('stakeholder'|'project'|'keyword'|'sdg').
        data: payload completo del nodo (dal backend).
        node: sotto-dizionario specifico del tipo (es. data['node']['s']).

    Returns:
        list[tuple]: [(valore, etichetta), ...] (max 3 voci).
    """
    if label == "stakeholder":
        return [
            (len(data["node"].get("projects") or []), "📁 Progetti"),
            (len(data["node"].get("keywords") or []), "🏷️ Keyword"),
            (_value_or_dash(data["node"].get("sector")), "🏭 Settore"),
        ]
    if label == "project":
        return [
            (len(data["node"].get("stakeholders") or []), "👥 Stakeholder"),
            (len(data["node"].get("keywords") or []), "🏷️ Keyword"),
            (_value_or_dash(node.get("location")), "📍 Location"),
        ]
    if label == "keyword":
        return [
            (len(data["node"].get("projects") or []), "📁 Progetti"),
            (len(data["node"].get("stakeholders") or []), "👥 Stakeholder"),
        ]
    if label == "sdg":
        return [
            (len(data["node"].get("projects") or []), "📁 Progetti"),
            (len(data["node"].get("stakeholders") or []), "👥 Stakeholder"),
            (_value_or_dash(data["node"].get("code") or (node.get('code') if node else None)), "🎯 SDG"),
        ]
    return []

# ---------- CSV/JSON helpers ----------
def _csv_bytes(df: pd.DataFrame) -> bytes:
    """Converte un DataFrame in CSV UTF-8 con BOM (compatibilità Excel)."""
    return df.to_csv(index=False).encode("utf-8-sig")

def _csv_button(df: pd.DataFrame, label_btn: str, fname: str, key: str):
    """Crea un pulsante di download CSV con parametri già impostati."""
    st.download_button(
        label_btn,
        data=_csv_bytes(df),
        file_name=fname,
        mime="text/csv",
        use_container_width=True,
        key=key
    )

# ---------- FLATTEN elements (per export robusto) ----------
def _flatten_elements(elements: dict) -> Tuple[List[Dict], List[Dict]]:
    """Ritorna (flat_nodes, flat_edges) con campi stringificati e sicuri dal payload backend.

    Args:
        elements: dict compatibile Cytoscape/agraph con chiavi "nodes"/"edges".

    Returns:
        (list[dict], list[dict]): nodi (id,label,type,highlight) e archi (source,target,label) puliti.
    """
    flat_nodes, flat_edges = [], []
    for n in (elements or {}).get("nodes", []) or []:
        d = (n or {}).get("data") or {}
        nid = str(d.get("id", "") or "")
        if not nid:
            continue
        flat_nodes.append({
            "id": nid,
            "label": str(d.get("label", nid) or nid),
            "type": str(d.get("type", "Node") or "Node"),
            "highlight": bool(d.get("highlight", False)),
        })
    for e in (elements or {}).get("edges", []) or []:
        d = (e or {}).get("data") or {}
        s = str(d.get("source", "") or ""); t = str(d.get("target", "") or "")
        if not s or not t:
            continue
        flat_edges.append({
            "source": s,
            "target": t,
            "label": str(d.get("label", "") or ""),
        })
    return flat_nodes, flat_edges

# ---------- PNG renderer (indipendente da streamlit-agraph) ----------
def _ego_png_from_flat(flat_nodes: List[Dict], flat_edges: List[Dict], title: str = "") -> Optional[bytes]:
    """Renderizza un PNG dell’ego-grafo usando networkx/matplotlib.

    Args:
        flat_nodes: nodi (id,label,type,highlight).
        flat_edges: archi (source,target,label).
        title: titolo opzionale per il grafico.

    Returns:
        bytes|None: contenuto PNG o None se render fallisce (gestito gracefully).
    """
    try:
        G = nx.DiGraph()
        for n in flat_nodes:
            G.add_node(n["id"], type=n["type"], label=n["label"])
        for e in flat_edges:
            G.add_edge(e["source"], e["target"])

        pos = nx.spring_layout(G, seed=42)

        def _color(t): return PALETTE.get(t, PALETTE["Node"])
        def _size(t):  return max(80, SIZE.get(t, 12) * 20)

        sizes = [_size(G.nodes[n]["type"]) for n in G.nodes()]
        colors = [_color(G.nodes[n]["type"]) for n in G.nodes()]
        labels = {n: G.nodes[n]["label"] for n in G.nodes()}

        fig, ax = plt.subplots(figsize=(10, 7), dpi=150)
        ax.axis("off")
        if title: ax.set_title(title, fontsize=10)

        nx.draw_networkx_edges(G, pos, alpha=0.35, arrows=True, arrowstyle="-|>", arrowsize=10, width=1)
        nx.draw_networkx_nodes(G, pos, node_size=sizes, node_color=colors, linewidths=0.5, edgecolors="#333", alpha=0.95)
        nx.draw_networkx_labels(G, pos, labels=labels, font_size=7)

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.05)
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception:
        return None

# -------------------------- Init percorso --------------------------
# NOTE: inizializziamo le chiavi di percorso se assenti, per evitare KeyError in UI.
if "path_src" not in st.session_state:
    st.session_state["path_src"] = None
if "path_tgt" not in st.session_state:
    st.session_state["path_tgt"] = None

def _set_path(role: str, node_id: str):
    """Imposta il nodo sorgente/destinazione nel percorso (A/B) in sessione."""
    if role == "A":
        st.session_state["path_src"] = node_id
    else:
        st.session_state["path_tgt"] = node_id
    st.toast(f"Aggiunto al percorso {role}: {node_id}")

def _open_detail(node_id: str):
    """Apre il dettaglio di un nodo impostandolo come selezionato e cambiando pagina."""
    st.session_state["selected_node"] = node_id
    st.switch_page("pages/dettaglio_nodo.py")

# -------------------------- Selezione nodo --------------------------
qid = None
try:
    qid = st.query_params.get("node", None)
except Exception:
    pass

nid = qid or st.session_state.get("selected_node")
if not nid:
    st.warning("Nessun nodo selezionato. Torna alla Home e scegli un elemento.")
    st.stop()

# -------------------------- Fetch nodo --------------------------
try:
    data = api(f"/node/{nid}", params=None)
except requests.HTTPError as e:
    st.error(f"Errore backend su /node: {e}")
    st.stop()

label = data.get("label")  # stakeholder | project | keyword | sdg
lod   = data.get("lod", {}) or {}

# -------------------------- Header compatto (sticky) --------------------------
with st.container():
    st.markdown('<div class="app-card sticky-hero">', unsafe_allow_html=True)
    cols = st.columns([7,5], vertical_alignment="top")

    # ---- LEFT: titolo + chips + stats
    with cols[0]:
        if label == "stakeholder":
            node = data["node"]["s"]
            title = node.get("name", "(senza nome)")
            chips_html = " ".join([
                chip(f"Tipo: {node.get('type','-')}"),
                chip(f"Settore: {data['node'].get('sector','-')}"),
                chip(f"Location: {node.get('location','-')}"),
            ])
        elif label == "project":
            node = data["node"]["p"]
            title = node.get("name","(senza nome)")
            chips_html = chip(f"Location: {node.get('location','-')}")
        elif label == "keyword":
            node = data["node"]["k"]
            title = node.get("name","(keyword)")
            chips_html = ""
        elif label == "sdg":
            node = data["node"]["g"]
            title = f"{node.get('code','SDG')} — {node.get('label','')}"
            chips_html = ""

        st.markdown(f"<h3 class='hero-title'>{title}</h3>", unsafe_allow_html=True)
        if chips_html:
            st.markdown(chips_html, unsafe_allow_html=True)

        stats = _hero_stats(label, data, node)
        if stats:
            st.markdown("<div class='stats'>", unsafe_allow_html=True)
            for val, lbl in stats:
                st.markdown(
                    f"<div class='stat'><div class='value'>{val}</div><div class='label'>{lbl}</div></div>",
                    unsafe_allow_html=True
                )
            st.markdown("</div>", unsafe_allow_html=True)

    # ---- RIGHT: info sintetica + azioni rapide
    with cols[1]:
        st.markdown("#### Info")
        st.write(f"**Tipo:** {label.upper()}")
        has_desc = bool(
            (label == "stakeholder" and data['node']['s'].get('description')) or
            (label == "project" and data['node']['p'].get('description'))
        )
        if has_desc:
            with st.expander("Mostra descrizione"):
                st.write(data['node']['s']['description'] if label=="stakeholder" else data['node']['p']['description'])

        st.markdown("<div class='hero-actions'>", unsafe_allow_html=True)
        if st.button("Aggiungi a A", key="hero_add_A"): _set_path("A", nid)
        if st.button("Aggiungi a B", key="hero_add_B"): _set_path("B", nid)
        st.code(nid, language="text")
        st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)

# -------------------------- Tabs --------------------------
tab_overview, tab_rel, tab_lod, tab_path, tab_ego = st.tabs(
    ["Panoramica", "Relazioni", "LOD", "Percorso", "Ego-grafo"]
)

# ===== Panoramica =====
with tab_overview:
    if label == "stakeholder":
        st.markdown("**Keyword**")
        st.markdown(tag_list(data["node"]["keywords"]), unsafe_allow_html=True)

        st.markdown("**Progetti**")
        dfp, rows = df_projects_with_rows(data["node"]["projects"])
        st.dataframe(
            dfp, use_container_width=True, hide_index=True, height=280,
            column_config={
                "ID": st.column_config.TextColumn("ID", width="small"),
                "Nome": st.column_config.TextColumn("Nome", width="large"),
                "Location": st.column_config.TextColumn("Location", width="medium"),
            }
        )
        _csv_button(dfp, "Scarica CSV progetti", f"overview_projects_{nid.replace(':','-')}.csv", key=f"dl_overview_proj_{nid}")
        action_buttons_for_rows(rows, lambda r: f"project:{r['ID']}", open_label="Apri Progetto", prefix=f"overview_proj_{nid}")

    elif label == "project":
        colA, colB = st.columns(2)
        with colA:
            st.markdown("**Stakeholder**")
            dfs, rows = df_stakeholders_with_rows(data["node"]["stakeholders"])
            st.dataframe(
                dfs, use_container_width=True, hide_index=True, height=280,
                column_config={
                    "ID": st.column_config.TextColumn("ID", width="small"),
                    "Nome": st.column_config.TextColumn("Nome", width="large"),
                    "Tipo": st.column_config.TextColumn("Tipo", width="small"),
                }
            )
            _csv_button(dfs, "Scarica CSV stakeholder", f"overview_stakeholders_{nid.replace(':','-')}.csv", key=f"dl_overview_stk_{nid}")
            action_buttons_for_rows(rows, lambda r: f"stakeholder:{r['ID']}", open_label="Apri Stakeholder", prefix=f"overview_stk_{nid}")
        with colB:
            st.markdown("**Keyword**")
            kw_df, _ = df_simple_col("Keyword", data["node"]["keywords"])
            st.dataframe(kw_df, use_container_width=True, hide_index=True, height=134)
            _csv_button(kw_df, "CSV keyword", f"overview_keywords_{nid.replace(':','-')}.csv", key=f"dl_overview_kw_{nid}")

            st.markdown("**SDG**")
            sdg_df, _ = df_simple_col("SDG", data["node"]["sdgs"])
            st.dataframe(sdg_df, use_container_width=True, hide_index=True, height=134)
            _csv_button(sdg_df, "CSV SDG", f"overview_sdgs_{nid.replace(':','-')}.csv", key=f"dl_overview_sdg_{nid}")

    elif label == "keyword":
        colA, colB = st.columns(2)
        with colA:
            st.markdown("**Progetti collegati**")
            dfp, rows = df_projects_with_rows(data["node"]["projects"])
            st.dataframe(dfp, use_container_width=True, hide_index=True, height=280)
            _csv_button(dfp, "CSV progetti", f"overview_kw_projects_{nid.replace(':','-')}.csv", key=f"dl_overview_kw_proj_{nid}")
            action_buttons_for_rows(rows, lambda r: f"project:{r['ID']}", open_label="Apri Progetto", prefix=f"overview_kw_proj_{nid}")
        with colB:
            st.markdown("**Stakeholder collegati**")
            dfs, rows = df_stakeholders_with_rows(data["node"]["stakeholders"])
            st.dataframe(dfs, use_container_width=True, hide_index=True, height=280)
            _csv_button(dfs, "CSV stakeholder", f"overview_kw_stakeholders_{nid.replace(':','-')}.csv", key=f"dl_overview_kw_stk_{nid}")
            action_buttons_for_rows(rows, lambda r: f"stakeholder:{r['ID']}", open_label="Apri Stakeholder", prefix=f"overview_kw_stk_{nid}")

    elif label == "sdg":
        colA, colB = st.columns(2)
        with colA:
            st.markdown("**Progetti (contributesTo)**")
            dfp, rows = df_projects_with_rows(data["node"]["projects"])
            st.dataframe(dfp, use_container_width=True, hide_index=True, height=280)
            _csv_button(dfp, "CSV progetti", f"overview_sdg_projects_{nid.replace(':','-')}.csv", key=f"dl_overview_sdg_proj_{nid}")
            action_buttons_for_rows(rows, lambda r: f"project:{r['ID']}", open_label="Apri Progetto", prefix=f"overview_sdg_proj_{nid}")
        with colB:
            st.markdown("**Stakeholder (via progetti)**")
            dfs, rows = df_stakeholders_with_rows(data["node"]["stakeholders"])
            st.dataframe(dfs, use_container_width=True, hide_index=True, height=280)
            _csv_button(dfs, "CSV stakeholder", f"overview_sdg_stakeholders_{nid.replace(':','-')}.csv", key=f"dl_overview_sdg_stk_{nid}")
            action_buttons_for_rows(rows, lambda r: f"stakeholder:{r['ID']}", open_label="Apri Stakeholder", prefix=f"overview_sdg_stk_{nid}")

# ===== Relazioni =====
with tab_rel:
    if label == "stakeholder":
        st.markdown("#### Progetti (participatesIn)")
        dfp, rows = df_projects_with_rows(data["node"]["projects"])
        st.dataframe(dfp, use_container_width=True, hide_index=True, height=280)
        _csv_button(dfp, "CSV progetti", f"rel_stk_projects_{nid.replace(':','-')}.csv", key=f"dl_rel_stk_proj_{nid}")
        action_buttons_for_rows(rows, lambda r: f"project:{r['ID']}", open_label="Apri Progetto", prefix=f"rel_stk_proj_{nid}")

        st.markdown("#### Keyword (hasKeyword)")
        kw_df, kw_rows = df_simple_col("Keyword", data["node"]["keywords"])
        st.dataframe(kw_df, use_container_width=True, hide_index=True, height=240)
        _csv_button(kw_df, "CSV keyword", f"rel_stk_keywords_{nid.replace(':','-')}.csv", key=f"dl_rel_stk_kw_{nid}")
        action_buttons_for_rows(kw_rows, lambda r: f"keyword:{r['Keyword']}", open_label="Apri Keyword", prefix=f"rel_stk_kw_{nid}")

    elif label == "project":
        st.markdown("#### Stakeholder (participatesIn)")
        dfs, rows = df_stakeholders_with_rows(data["node"]["stakeholders"])
        st.dataframe(dfs, use_container_width=True, hide_index=True, height=280)
        _csv_button(dfs, "CSV stakeholder", f"rel_proj_stakeholders_{nid.replace(':','-')}.csv", key=f"dl_rel_proj_stk_{nid}")
        action_buttons_for_rows(rows, lambda r: f"stakeholder:{r['ID']}", open_label="Apri Stakeholder", prefix=f"rel_proj_stk_{nid}")

        colA, colB = st.columns(2)
        with colA:
            st.markdown("#### Keyword (relatedToKeyword)")
            kw_df, kw_rows = df_simple_col("Keyword", data["node"]["keywords"])
            st.dataframe(kw_df, use_container_width=True, hide_index=True, height=240)
            _csv_button(kw_df, "⬇️ CSV keyword", f"rel_proj_keywords_{nid.replace(':','-')}.csv", key=f"dl_rel_proj_kw_{nid}")
            action_buttons_for_rows(kw_rows, lambda r: f"keyword:{r['Keyword']}", open_label="Apri Keyword", prefix=f"rel_proj_kw_{nid}")
        with colB:
            st.markdown("#### SDG (contributesTo)")
            sdg_df, sdg_rows = df_simple_col("SDG", data["node"]["sdgs"])
            st.dataframe(sdg_df, use_container_width=True, hide_index=True, height=240)
            _csv_button(sdg_df, "⬇️ CSV SDG", f"rel_proj_sdgs_{nid.replace(':','-')}.csv", key=f"dl_rel_proj_sdg_{nid}")
            action_buttons_for_rows(sdg_rows, lambda r: f"sdg:{r['SDG']}", open_label="Apri SDG", prefix=f"rel_proj_sdg_{nid}")

    elif label == "keyword":
        st.markdown("#### Progetti (relatedToKeyword)")
        dfp, rows = df_projects_with_rows(data["node"]["projects"])
        st.dataframe(dfp, use_container_width=True, hide_index=True, height=280)
        _csv_button(dfp, "⬇️ CSV progetti", f"rel_kw_projects_{nid.replace(':','-')}.csv", key=f"dl_rel_kw_proj_{nid}")
        action_buttons_for_rows(rows, lambda r: f"project:{r['ID']}", open_label="Apri Progetto", prefix=f"rel_kw_proj_{nid}")

        st.markdown("#### Stakeholder (hasKeyword)")
        dfs, rows = df_stakeholders_with_rows(data["node"]["stakeholders"])
        st.dataframe(dfs, use_container_width=True, hide_index=True, height=280)
        _csv_button(dfs, "⬇️ CSV stakeholder", f"rel_kw_stakeholders_{nid.replace(':','-')}.csv", key=f"dl_rel_kw_stk_{nid}")
        action_buttons_for_rows(rows, lambda r: f"stakeholder:{r['ID']}", open_label="Apri Stakeholder", prefix=f"rel_kw_stk_{nid}")

    elif label == "sdg":
        st.markdown("#### Progetti (contributesTo)")
        dfp, rows = df_projects_with_rows(data["node"]["projects"])
        st.dataframe(dfp, use_container_width=True, hide_index=True, height=280)
        _csv_button(dfp, "⬇️ CSV progetti", f"rel_sdg_projects_{nid.replace(':','-')}.csv", key=f"dl_rel_sdg_proj_{nid}")
        action_buttons_for_rows(rows, lambda r: f"project:{r['ID']}", open_label="Apri Progetto", prefix=f"rel_sdg_proj_{nid}")

        st.markdown("#### Stakeholder (via progetti)")
        dfs, rows = df_stakeholders_with_rows(data["node"]["stakeholders"])
        st.dataframe(dfs, use_container_width=True, hide_index=True, height=280)
        _csv_button(dfs, "⬇️ CSV stakeholder", f"rel_sdg_stakeholders_{nid.replace(':','-')}.csv", key=f"dl_rel_sdg_stk_{nid}")
        action_buttons_for_rows(rows, lambda r: f"stakeholder:{r['ID']}", open_label="Apri Stakeholder", prefix=f"rel_sdg_stk_{nid}")

# ===== LOD =====
with tab_lod:
    left, right = st.columns([1,2], gap="large")

    def _safe_str(x, dash="—"):
        """Converte un valore in stringa ripulita; ritorna dash se falsy."""
        return str(x).strip() if x else dash

    with left:
        st.markdown("#### Riferimenti")

        img = lod.get("image")
        if isinstance(img, str) and img.strip():
            st.image(img, use_column_width=True)
        else:
            st.caption("Nessuna immagine disponibile.")

        wd_primary = lod.get("wikidata")
        dbp_primary = lod.get("dbpedia")
        geo_primary = None

        links = lod.get("linkedEntities") or []
        rows = []
        first_by_source = {}

        for it in links:
            e = (it or {}).get("entity") or {}
            src_norm = _norm_source(e.get("source"))
            uri = _safe_str(e.get("uri"), dash="")
            lbl = _safe_str(e.get("label"))
            kw  = _safe_str(it.get("keyword"))
            if uri and src_norm not in first_by_source:
                first_by_source[src_norm] = uri
            rows.append({"Keyword": kw, "Fonte": src_norm, "Label LOD": lbl, "URI": uri})

        if not wd_primary:  wd_primary = first_by_source.get("Wikidata")
        if not dbp_primary: dbp_primary = first_by_source.get("DBpedia Spotlight")
        geo_primary = first_by_source.get("GeoNames")

        col1, col2 = st.columns(2)
        with col1:
            if wd_primary: st.link_button("Apri Wikidata", wd_primary, use_container_width=True)
            else:          st.button("Wikidata — n/d", disabled=True, use_container_width=True)
        with col2:
            if dbp_primary: st.link_button("Apri DBpedia", dbp_primary, use_container_width=True)
            else:           st.button("DBpedia — n/d", disabled=True, use_container_width=True)

        if geo_primary:
            st.link_button("Apri GeoNames", geo_primary, use_container_width=True)
        else:
            st.button("GeoNames — n/d", disabled=True, use_container_width=True)

        with st.expander("URI (copia/incolla)", expanded=False):
            st.code(f"Wikidata: {_safe_str(wd_primary)}\nDBpedia: {_safe_str(dbp_primary)}\nGeoNames: {_safe_str(geo_primary)}", language="text")

        if lod.get("summary"):
            with st.expander("Riassunto", expanded=True):
                st.write(lod["summary"])
        else:
            st.caption("Nessun riassunto disponibile.")

        st.download_button(
            "⬇️ Scarica JSON LOD",
            data=json.dumps(lod or {}, ensure_ascii=False, indent=2),
            file_name=f"lod_{nid.replace(':','-')}.json",
            mime="application/json",
            use_container_width=True
        )

    with right:
        st.markdown("#### Entità esterne collegate (da keyword)")
        df_links = pd.DataFrame(rows, columns=["Keyword","Fonte","Label LOD","URI"])

        if df_links.empty:
            st.info("Nessuna entità LOD collegata.")
        else:
            f1, f2, f3 = st.columns([1,1,2])
            with f1:
                all_sources_raw = [s for s in df_links["Fonte"].dropna().unique().tolist() if s and s != "—"]
                order_pref = ["Wikidata", "DBpedia Spotlight", "GeoNames"]
                all_sources = sorted(all_sources_raw, key=lambda x: (order_pref.index(x) if x in order_pref else 999, x))
                sel_sources = st.multiselect("Fonte", options=all_sources, default=all_sources)
            with f2:
                all_kw = sorted([k for k in df_links["Keyword"].dropna().unique() if k and k != "—"])
                sel_kw = st.multiselect("Keyword", options=all_kw)
            with f3:
                search = st.text_input("Cerca nella label LOD", placeholder="es. solar, hydrogen, …")
                only_uri = st.checkbox("Solo con URI valido", value=False)

            mask = pd.Series([True]*len(df_links))
            if sel_sources: mask &= df_links["Fonte"].isin(sel_sources)
            if sel_kw:      mask &= df_links["Keyword"].isin(sel_kw)
            if only_uri:    mask &= df_links["URI"].astype(str).str.len() > 0
            if search:      mask &= df_links["Label LOD"].str.contains(search, case=False, na=False)

            df_view = df_links[mask].copy()

            st.caption("Fonti (conteggi nel dataset filtrato)")
            counts = df_view["Fonte"].value_counts().to_dict()
            if counts:
                chips_html = " ".join([f'<span class="chip">{src}: {cnt}</span>' for src, cnt in counts.items()])
                st.markdown(chips_html, unsafe_allow_html=True)
            else:
                st.caption("— Nessun risultato con i filtri correnti —")

            show_chart = st.checkbox("Mostra grafico delle fonti", value=bool(counts))
            if show_chart and counts:
                st.bar_chart(pd.Series(counts).sort_values(ascending=False))

            st.dataframe(
                df_view,
                use_container_width=True,
                hide_index=True,
                height=360,
                column_config={
                    "Keyword": st.column_config.TextColumn("Keyword", width="medium"),
                    "Fonte": st.column_config.TextColumn("Fonte", width="small"),
                    "Label LOD": st.column_config.TextColumn("Label LOD", width="large"),
                    "URI": st.column_config.LinkColumn("URI", display_text="Apri", width="small"),
                }
            )
            _csv_button(df_view, "CSV entità LOD filtrate", f"lod_links_{nid.replace(':','-')}.csv", key=f"dl_lod_links_{nid}")

# ===== Percorso (Shortest Path) =====
with tab_path:
    st.markdown("#### Calcolo percorso più breve (tra A e B)")

    c = st.columns(2)
    with c[0]:
        st.markdown("**Nodo A (sorgente)**")
        st.code(st.session_state.get("path_src") or "—", language="text")
        if st.session_state.get("path_src") and st.button("Rimuovi A", key="remove_A"):
            st.session_state["path_src"] = None
    with c[1]:
        st.markdown("**Nodo B (target)**")
        st.code(st.session_state.get("path_tgt") or "—", language="text")
        if st.session_state.get("path_tgt") and st.button("Rimuovi B", key="remove_B"):
            st.session_state["path_tgt"] = None

    controls = st.columns([1,2,1])
    with controls[0]:
        path_max = st.slider("Lunghezza max", 2, 10, 6, key="path_max_slider")
    with controls[1]:
        st.caption("Suggerimento: aggiungi i nodi al percorso usando i pulsanti nelle tabelle o sul nodo corrente.")
    with controls[2]:
        ready = bool(st.session_state.get("path_src") and st.session_state.get("path_tgt"))
        compute = st.button("🚀 Calcola percorso", type="primary", disabled=not ready, use_container_width=True, key="compute_path")

    if compute and ready:
        try:
            resp = api("/graph", params={
                "path_src": st.session_state["path_src"],
                "path_tgt": st.session_state["path_tgt"],
                "path_max": path_max
            })
        except requests.HTTPError as e:
            st.error(f"Errore backend su /graph (shortest path): {e}")
        else:
            els = (resp or {}).get("elements") or {}
            raw_nodes = els.get("nodes") or []
            raw_edges = els.get("edges") or []

            if not raw_edges:
                st.warning("Nessun percorso trovato tra A e B con i parametri attuali.")
            else:
                nodes_all: Dict[str, Dict] = {}
                for n in raw_nodes:
                    d = (n or {}).get("data") or {}
                    nid_safe = str(d.get("id", "") or "")
                    if nid_safe:
                        nodes_all[nid_safe] = d

                edges_path = [e for e in raw_edges if ((e or {}).get("data") or {}).get("highlight")]
                if not edges_path:
                    edges_path = raw_edges

                node_ids = set()
                for e in edges_path:
                    de = (e or {}).get("data") or {}
                    s = str(de.get("source", "") or "")
                    t = str(de.get("target", "") or "")
                    if s: node_ids.add(s)
                    if t: node_ids.add(t)

                step_rows = []
                for e in edges_path:
                    de = (e or {}).get("data") or {}
                    s_id = str(de.get("source", "") or "")
                    t_id = str(de.get("target", "") or "")
                    rel  = str(de.get("label", "") or "")
                    s_lbl = str((nodes_all.get(s_id, {}) or {}).get("label", s_id))
                    t_lbl = str((nodes_all.get(t_id, {}) or {}).get("label", t_id))
                    if s_id and t_id:
                        step_rows.append({"Da": s_lbl, "→ Relazione": rel, "A": t_lbl})

                if step_rows:
                    st.markdown("**Passi del percorso**")
                    df_steps = pd.DataFrame(step_rows)
                    st.dataframe(df_steps, use_container_width=True, hide_index=True, height=220)
                    _csv_button(df_steps, "⬇️ CSV passi percorso", f"path_steps_{nid.replace(':','-')}.csv", key=f"dl_path_steps_{nid}")
                else:
                    st.info("Percorso calcolato, ma non ci sono step da mostrare (controlla i dati di edges).")

                g_nodes, g_edges = [], []
                for nid_path in sorted(node_ids):
                    d = (nodes_all.get(nid_path) or {})
                    typ = d.get("type") or "Node"
                    lbl = str(d.get("label") or d.get("id") or nid_path)
                    size = int(SIZE.get(typ, 12))
                    if nid_path in (st.session_state.get("path_src"), st.session_state.get("path_tgt")):
                        size += 8
                    g_nodes.append(Node(id=str(nid_path), label=lbl, size=size, color=PALETTE.get(typ, PALETTE["Node"]), title=f"{typ}: {lbl}"))

                for e in edges_path:
                    de = (e or {}).get("data") or {}
                    s = str(de.get("source", "") or "")
                    t = str(de.get("target", "") or "")
                    if not s or not t:
                        continue
                    g_edges.append(Edge(source=s, target=t, label=str(de.get("label", "") or ""), color=PALETTE["Edge"]))

                if not g_nodes or not g_edges:
                    st.warning("Niente da visualizzare nel grafo (nodi/archi vuoti).")
                else:
                    cfg = Config(width=1100, height=420, directed=True, physics=True, hierarchical=False, nodeHighlightBehavior=True, highlightColor="#111")
                    try:
                        agraph(nodes=g_nodes, edges=g_edges, config=cfg)
                    except Exception as ex:
                        st.exception(ex)
                        st.info("Verifica che: 1) ID univoci; 2) nessun edge con source/target vuoti; 3) versione 'streamlit-agraph' compatibile.")

# ===== Ego-grafo =====
with tab_ego:
    st.markdown("#### Ego-grafo locale")
    controls = st.columns([1,1,2])
    with controls[0]:
        depth = st.slider("Profondità (hop)", 1, 3, 1, key="ego_depth")
    with controls[1]:
        inc_sector = st.checkbox("Includi Sector", value=True, key="ego_inc_sector")
        inc_lod    = st.checkbox("Includi LOD", value=False, key="ego_inc_lod")

    try:
        ego = api(f"/ego/{nid}", params={
            "depth": depth,
            "include_sector": str(inc_sector).lower(),
            "include_lod": str(inc_lod).lower()
        })
    except requests.HTTPError as e:
        st.error(f"Errore backend su /ego: {e}")
        st.stop()

    egoe = ego.get("elements", {}) or {}

    nodes, edges = [], []
    for n in egoe.get("nodes", []) or []:
        d = (n or {}).get("data") or {}
        typ = d.get("type") or "Node"
        nid_safe = str(d.get("id", "") or "")
        if not nid_safe: continue
        lbl = str(d.get("label") or nid_safe)
        size = int(SIZE.get(typ, 12)) + (8 if d.get("highlight") else 0)
        nodes.append(Node(id=nid_safe, label=lbl, size=size, color=PALETTE.get(typ, PALETTE["Node"]), title=f"{typ}: {lbl}"))

    for e in egoe.get("edges", []) or []:
        d = (e or {}).get("data") or {}
        s = str(d.get("source", "") or ""); t = str(d.get("target", "") or "")
        if not s or not t: continue
        edges.append(Edge(source=s, target=t, label=str(d.get("label","") or ""), color=PALETTE["Edge"]))

    if nodes and edges:
        # ======== DIMENSIONI DINAMICHE PER USARE TUTTO LO SCHERMO ========
        if get_window_size:
            try:
                wsize = get_window_size() or {}
                vw = int(wsize.get("width", 1280))
                vh = int(wsize.get("height", 800))
            except Exception:
                vw, vh = 1280, 800
        else:
            vw, vh = 1280, 800  # fallback senza js

        RESERVED_Y = 280
        graph_h = max(420, vh - RESERVED_Y)
        graph_w = max(900, vw - 24)

        with st.expander("Opzioni grafico", expanded=False):
            graph_h = st.slider("Altezza grafico (px)", 420, 1600, graph_h, step=10, key="ego_h")
            physics_on = st.toggle("Fisica (layout dinamico)", value=True, key="ego_phys")
        if "ego_phys" not in st.session_state:
            physics_on = True

        cfg = Config(
            width=int(graph_w),
            height=int(graph_h),
            directed=True,
            physics=bool(physics_on),
            hierarchical=False,
            nodeHighlightBehavior=True,
            highlightColor="#111",
        )

        st.markdown('<div class="full-bleed">', unsafe_allow_html=True)
        try:
            agraph(nodes=nodes, edges=edges, config=cfg)
        except Exception as ex:
            st.exception(ex)
            st.info("Verifica che: 1) ID univoci; 2) nessun edge con source/target vuoti; 3) versione 'streamlit-agraph' compatibile.")
        st.markdown('</div>', unsafe_allow_html=True)

        # ================== DOWNLOAD EGO-GRAFO (basato su payload raw) ==================
        st.divider()
        st.markdown("##### Download ego-grafo visualizzato")

        flat_nodes, flat_edges = _flatten_elements(egoe)
        df_nodes_ego = pd.DataFrame(flat_nodes)  # id, label, type, highlight
        df_edges_ego = pd.DataFrame(flat_edges)  # source, target, label

        col_csv1, col_csv2, col_png, col_json = st.columns([1,1,1,1])
        with col_csv1:
            _csv_button(df_nodes_ego, "Nodi (CSV)", f"ego_nodes_{nid.replace(':','-')}_d{depth}.csv", key=f"dl_ego_nodes_{nid}_{depth}")
        with col_csv2:
            _csv_button(df_edges_ego, "Archi (CSV)", f"ego_edges_{nid.replace(':','-')}_d{depth}.csv", key=f"dl_ego_edges_{nid}_{depth}")

        ego_json_bytes = json.dumps(egoe, ensure_ascii=False, indent=2).encode("utf-8")
        with col_json:
            st.download_button(
                "⬇️ Grafo (JSON)", data=ego_json_bytes,
                file_name=f"ego_graph_{nid.replace(':','-')}_d{depth}.json",
                mime="application/json", use_container_width=True,
                key=f"dl_ego_json_{nid}_{depth}"
            )

        png_bytes = _ego_png_from_flat(flat_nodes, flat_edges, title=f"Ego: {nid} (depth={depth})")
        with col_png:
            st.download_button(
                "🖼️ Grafo (PNG)",
                data=(png_bytes or b""),
                file_name=f"ego_graph_{nid.replace(':','-')}_d{depth}.png",
                mime="image/png",
                disabled=(png_bytes is None),
                use_container_width=True,
                key=f"dl_ego_png_{nid}_{depth}"
            )
    else:
        st.info("Ego-grafo non disponibile per questo nodo con i parametri attuali.")