"""
SDG Graph – Applicazione Streamlit
----------------------------------
Questa app visualizza un grafo di Stakeholder/Progetti/Keyword/SDG con filtri,
esporta i dati e consente azioni contestuali sui nodi.

Stato dell'app (st.session_state) – come viene usato:
- "selected_node": id del nodo selezionato nel grafo (persistente tra i rerun).
- "edit_mode": flag per abilitare la modalità modifica nelle pagine di inserimento.
- "edit_node": id del nodo da modificare nelle pagine di inserimento.
Le chiavi vengono impostate/azzerate dai pulsanti nella sidebar destra (azioni)
e dai pulsanti di modifica/eliminazione nel dettaglio del nodo.

Input & validazione:
- I filtri (selectbox/text_input/multiselect/slider/radio/checkbox) producono
  valori usati per costruire la query API e per la potatura (pruning) del grafo.
- Non viene alterata la logica: si eseguono controlli robusti contro None/chiavi mancanti.
- La stabilizzazione/pruning applica filtri su tipi, gradi e numero max di nodi.
"""

import io, json, zipfile
from collections import Counter, defaultdict

import pandas as pd
import networkx as nx
import matplotlib.pyplot as plt
import streamlit as st
from streamlit_agraph import agraph, Node, Edge, Config

from utils import api, api_post

st.set_page_config(page_title="SDG Graph", layout="wide", initial_sidebar_state="collapsed")

# ======================= CONFIGURAZIONE / COSTANTI =======================
# Colori chip usati per rappresentare i filtri attivi (sia in sidebar che nel main).
chip_colors = {
    "SDG": "#3c78d8",
    "Settore": "#6aa84f",
    "Testo": "#8e7cc3",
    "Tipo Stakeholder": "#e06666",
}

# Palette per tipi di nodo (coerente con la legenda e l'export PNG).
COLOR = {
    "Stakeholder": "#6aa84f",
    "Project": "#3c78d8",
    "Keyword": "#ffd966",
    "SDG": "#e06666",
    "Sector": "#8e7cc3",
    "LODEntity": "#76a5af",
}

# Tipi di archi consentiti (devono essere allineati al backend).
EDGE_TYPES_ALL = [
    "participatesIn",      # Stakeholder -> Project
    "relatedToKeyword",    # Project -> Keyword
    "hasKeyword",          # Stakeholder -> Keyword
    "contributesTo",       # Project -> SDG
    "IN_SECTOR",           # Stakeholder -> Sector
    "linkedTo",            # Keyword -> LODEntity
    "COLLAB",              # Materializzato
    "COLLAB_PRED"          # Predetto
]

# Colori per tipi di archi, utile per leggere il grafo a colpo d'occhio.
EDGE_COLOR = {
    "participatesIn":   "#7a7a7a",
    "relatedToKeyword": "#c19a00",
    "hasKeyword":       "#9b8700",
    "contributesTo":    "#c05050",
    "IN_SECTOR":        "#7b61a8",
    "linkedTo":         "#4b8f99",
    "COLLAB":           "#2f9e44",
    "COLLAB_PRED":      "#66bb6a",
}

# Colori per i badge (etichette) in sidebar/dettaglio.
BADGE_COLOR = {
    "stakeholder": "#6aa84f",
    "project": "#3c78d8",
    "keyword": "#e1c542",
    "sdg": "#e06666",
    "sector": "#8e7cc3",
    "lod": "#76a5af",
}

# Peso per tipologia di nodo usato nello scoring del pruning quando si eccede il limite.
TYPE_WEIGHT = {"Stakeholder": 3, "Project": 2, "Keyword": 1, "SDG": 1, "Sector": 1, "LODEntity": 0}


# ---------------- Helper chip (riusato in sidebar e main) ----------------
def _chip(label: str, value: str, color="#444"):
    """Crea l'HTML di un piccolo 'chip' con etichetta e valore.

    Args:
        label: Etichetta del filtro (es. "SDG").
        value: Valore selezionato (es. "SDG3").
        color: Colore di sfondo del chip (hex).

    Returns:
        Una stringa HTML sicura da iniettare con st.markdown(..., unsafe_allow_html=True).
    """
    return f"""
    <span style="
      display:inline-block;margin:6px 6px 0 0;padding:4px 10px;
      border-radius:999px;background:{color};color:#fff;
      font-weight:600;font-size:12px;letter-spacing:.3px;">
      {label}: {value}
    </span>
    """


def badge(text: str, kind: str):
    """Mostra un badge colorato coerente al tipo di entità.

    Args:
        text: Testo da mostrare nel badge.
        kind: Tipo entità (stakeholder, project, keyword, sdg, sector, lod).

    Side effects:
        Emette HTML via st.markdown.
    """
    color = BADGE_COLOR.get(kind.lower(), "#999")
    st.markdown(
        f"""
        <span style="
            display:inline-block;
            padding:4px 10px;
            border-radius:999px;
            background:{color};
            color:#fff;
            font-weight:600;
            font-size:12px;
            letter-spacing:0.3px;">
            {text}
        </span>
        """,
        unsafe_allow_html=True
    )


def prune_graph(elements, max_nodes, min_degree, hide_types, keep_edge_types):
    """Applica pruning/stabilizzazione agli elementi del grafo.

    Passi:
      1) Filtra nodi per tipo (hide_types).
      2) Filtra archi mantenendo solo quelli tra nodi rimasti e con etichetta consentita.
      3) Calcola i gradi e rimuove nodi sotto la soglia min_degree.
      4) Se i nodi superano max_nodes, tiene gli 'hub' usando uno score (TYPE_WEIGHT, grado, label).

    Args:
        elements (dict): Dizionario con chiavi "nodes"/"edges" (stile Cytoscape/agraph).
        max_nodes (int): Numero massimo di nodi da visualizzare.
        min_degree (int): Soglia minima di grado nel sottografo filtrato.
        hide_types (list[str]): Tipi di nodo da nascondere (es. ["LODEntity"]).
        keep_edge_types (list[str]|set[str]): Tipi di archi ammessi.

    Returns:
        (elements_pruned, degree_map):
            elements_pruned: dizionario {"nodes": [...], "edges": [...]} prunato.
            degree_map: mappa {node_id: grado} riferita al sottografo finale.
    """
    nodes = elements.get("nodes", [])
    edges = elements.get("edges", [])

    # 1) filtra per tipo di nodo
    keep_ids = set()
    id2node = {}
    for n in nodes:
        d = n["data"]
        t = d.get("type", "")
        if t in (hide_types or []):
            continue
        node_id = d.get("id")
        if node_id is None:
            continue
        id2node[node_id] = n
        keep_ids.add(node_id)

    # 2) filtra archi: per tipi + estremi presenti
    keep_edge_types = set(keep_edge_types or [])
    kept_edges = []
    for e in edges:
        d = e["data"]
        s = d.get("source"); t = d.get("target")
        etype = d.get("label") or d.get("type") or ""
        if s in keep_ids and t in keep_ids:
            if (not keep_edge_types) or (etype in keep_edge_types):
                kept_edges.append(e)

    # 3) calcola grado (sul sotto-grafo corrente) e applica soglia
    deg = defaultdict(int)
    for e in kept_edges:
        d = e["data"]
        deg[d["source"]] += 1
        deg[d["target"]] += 1

    if min_degree > 0:
        keep_ids = {nid for nid in keep_ids if deg.get(nid, 0) >= min_degree}
        kept_edges = [e for e in kept_edges if e["data"]["source"] in keep_ids and e["data"]["target"] in keep_ids]

    # 4) se supero max_nodes → tieni hub importanti
    if len(keep_ids) > max_nodes:
        def score(nid):
            t = id2node[nid]["data"].get("type", "")
            return (TYPE_WEIGHT.get(t, 0), deg.get(nid, 0), id2node[nid]["data"].get("label", ""))
        kept_sorted = sorted(list(keep_ids), key=score, reverse=True)
        trimmed = set(kept_sorted[:max_nodes])
        keep_ids = trimmed
        kept_edges = [e for e in kept_edges if e["data"]["source"] in keep_ids and e["data"]["target"] in keep_ids]

    kept_nodes = [id2node[nid] for nid in keep_ids]
    return {"nodes": kept_nodes, "edges": kept_edges}, deg


# ---------------- Sidebar (filtri + controlli visualizzazione) ----------------
with st.sidebar:
    st.header("🔍 Filtri di ricerca")
    # Input utente (validazione: Streamlit gestisce i tipi; qui usiamo stringhe vuote come 'nessun filtro').
    sdg = st.selectbox("SDG", options=[""] + [f"SDG{i}" for i in range(1, 18)])
    sector = st.text_input("Settore")
    q = st.text_input("Parole chiave / testo")
    stype = st.text_input("Tipo Stakeholder (es. ente pubblico)")

    # Badge filtri attivi (SIDEBAR)
    sidebar_chips = []
    if sdg:    sidebar_chips.append(_chip("SDG", sdg, chip_colors["SDG"]))
    if sector: sidebar_chips.append(_chip("Settore", sector, chip_colors["Settore"]))
    if q:      sidebar_chips.append(_chip("Testo", q, chip_colors["Testo"]))
    if stype:  sidebar_chips.append(_chip("Tipo Stakeholder", stype, chip_colors["Tipo Stakeholder"]))

    st.markdown("---")
    st.caption("Filtri attivi")
    if sidebar_chips:
        st.markdown("".join(sidebar_chips), unsafe_allow_html=True)
    else:
        st.caption("Nessuno")

    # --- Controlli visualizzazione / stabilità ---
    st.markdown("---")
    st.header("🧭 Visualizzazione")
    viz_mode = st.radio("Modalità grafo", ["Completo", "Ego (locale)"], horizontal=True)
    max_nodes = st.slider("Limite nodi visualizzati", 50, 2000, 300, 50,
                          help="Se il grafo supera il limite, vengono tenuti per primi gli hub più connessi.")
    min_degree = st.slider("Grado minimo (filtra nodi poco connessi)", 0, 20, 0, 1,
                           help="Rimuove i nodi con grado inferiore alla soglia (dopo aver applicato i filtri).")
    hide_types = st.multiselect(
        "Nascondi tipi di nodo",
        ["Keyword", "SDG", "LODEntity", "Sector"],
        default=["LODEntity"],
        help="Utile per ridurre rumore: Keyword/LODEntity sono spesso numerosi."
    )
    edge_types_selected = st.multiselect(
        "Tipi di archi da mostrare",
        EDGE_TYPES_ALL,
        default=["participatesIn", "relatedToKeyword", "hasKeyword", "contributesTo"],
        help="Restringi la visualizzazione agli archi rilevanti."
    )
    hierarchical = st.checkbox("Layout gerarchico (più stabile)", value=False,
                               help="Più leggibile con vista filtrata; meno adatto a grafi generali.")
    ego_depth = 1
    if viz_mode == "Ego (locale)":
        ego_depth = st.slider("Profondità ego-grafo", 1, 4, 2,
                              help="1=vicini diretti; 2..4 includono amici degli amici.")

# Parametri API /graph (costruiti solo se i filtri non sono vuoti).
params = {}
if sdg: params["sdg"] = sdg
if sector: params["sector"] = sector
if q: params["q"] = q
if stype: params["stakeholder_type"] = stype

# ---------------- Query backend ----------------
try:
    if viz_mode == "Ego (locale)":
        # Usa il nodo selezionato in precedenza come seed; fallback: primo Stakeholder disponibile.
        # NOTE st.session_state["selected_node"] persiste la selezione dell’utente.
        seed = st.session_state.get("selected_node")
        if not seed:
            data_full = api("/graph", params=params)
            els_full = data_full["elements"]
            seed = next((n["data"]["id"] for n in els_full.get("nodes", [])
                         if n["data"].get("type") == "Stakeholder"), None)
        if seed:
            ego_params = {"depth": ego_depth, "include_sector": True, "include_lod": False}
            data = api(f"/ego/{seed}", params=ego_params)
            elements = data["elements"]
        else:
            st.info("Seleziona o applica filtri per identificare un nodo di partenza. Mostro la vista completa ridotta.")
            data = api("/graph", params=params)
            elements = data["elements"]
    else:
        data = api("/graph", params=params)
        elements = data["elements"]
except Exception as e:
    st.error(f"Errore nel caricamento del grafo: {e}")
    st.stop()

# ---------------- Pruning / stabilizzazione ----------------
original_nodes = len(elements.get("nodes", []))
original_edges = len(elements.get("edges", []))
elements_pruned, degree_map = prune_graph(
    elements,
    max_nodes=max_nodes,
    min_degree=min_degree,
    hide_types=hide_types,
    keep_edge_types=edge_types_selected
)
elements = elements_pruned

n_nodes = len(elements.get("nodes", []))
n_edges = len(elements.get("edges", []))

# ---------------- KPI ----------------
m1, m2, m3 = st.columns(3)
m1.metric("🔹 Nodi", n_nodes)
m2.metric("🔸 Archi", n_edges)
active_filters = sum(bool(params.get(k)) for k in ("sdg", "sector", "q", "stakeholder_type"))
m3.metric("🎛️ Filtri attivi", active_filters)

# Notifica pruning
if n_nodes < original_nodes or n_edges < original_edges:
    st.info(
        f"Vista ridotta: {n_nodes}/{original_nodes} nodi e {n_edges}/{original_edges} archi mostrati "
        f"(limite={max_nodes}, grado≥{min_degree}, nascosti={', '.join(hide_types) if hide_types else '—'}, "
        f"archi={', '.join(edge_types_selected) if edge_types_selected else 'tutti'})."
    )

# === Badge con tipo/i di filtro attivo (MAIN) ===
main_chips = []
if params.get("sdg"):
    main_chips.append(_chip("SDG", params["sdg"], chip_colors["SDG"]))
if params.get("sector"):
    main_chips.append(_chip("Settore", params["sector"], chip_colors["Settore"]))
if params.get("q"):
    main_chips.append(_chip("Testo", params["q"], chip_colors["Testo"]))
if params.get("stakeholder_type"):
    main_chips.append(_chip("Tipo Stakeholder", params["stakeholder_type"], chip_colors["Tipo Stakeholder"]))
if main_chips:
    st.markdown("".join(main_chips), unsafe_allow_html=True)
else:
    st.caption("Filtri attivi: nessuno")

# --- Ripartizione per tipo (compatta) ---
type_counts = Counter(n["data"].get("type", "Node") for n in elements.get("nodes", []))
if type_counts:
    st.caption("Distribuzione nodi per tipo")
    preferred_order = ["Stakeholder", "Project", "Keyword", "SDG", "Sector", "LODEntity"]
    ordered_types = [t for t in preferred_order if t in type_counts] + \
                    [t for t in type_counts.keys() if t not in preferred_order]
    cols = st.columns(min(6, len(ordered_types)))
    for i, t in enumerate(ordered_types[:6]):
        with cols[i]:
            st.markdown(
                f"""
                <div style="padding:10px 12px;border-radius:12px;border:1px solid #eee;background:#fafafa">
                    <div style="font-size:12px;color:#666;">{t}</div>
                    <div style="font-size:22px;font-weight:700;color:{COLOR.get(t,'#333')};">{type_counts[t]}</div>
                </div>
                """,
                unsafe_allow_html=True
            )

# ---------------- Costruzione grafo (dimensione = f(grado)) ----------------
nodes, edges = [], []
for n in elements.get("nodes", []):
    d = n["data"]
    nid = d["id"]
    ntype = d.get("type", "")
    label = d.get("label", nid)
    deg = degree_map.get(nid, 0)
    base = 12 if ntype not in ("Stakeholder", "Project") else 16
    size = min(base + int(deg ** 0.7), 40)  # crescita sublineare con il grado
    nodes.append(Node(
        id=nid, label=label,
        size=size,
        color=COLOR.get(ntype, "#999"),
        title=f"{ntype}: {label} (grado {deg})"
    ))
for e in elements.get("edges", []):
    d = e["data"]
    etype = d.get("label") or d.get("type") or ""
    edges.append(Edge(
        source=d["source"],
        target=d["target"],
        label=etype,
        color=EDGE_COLOR.get(etype, "#b0b0b0")
    ))

config = Config(
    width=1100,
    height=700,
    directed=True,
    physics=True,
    hierarchical=hierarchical,
    nodeHighlightBehavior=True,
    highlightColor="#111",
    collapsible=False
)

# ---------------- Helper export ----------------
def build_export_artifacts(elements, degree_map, color_map):
    """Costruisce artefatti di export coerenti con la vista corrente.

    Crea:
      - ZIP contenente nodes.csv/edges.csv
      - JSON del grafo (nodes/edges)
      - PNG del grafo via networkx con layout deterministico

    Args:
        elements (dict): elementi del grafo già prunati ({"nodes": [...], "edges": [...]}).
        degree_map (dict): mappa {node_id: grado} per dimensionare i nodi nell'anteprima PNG.
        color_map (dict): mappa {node_type: hex_color} per colorare i nodi nell'anteprima PNG.

    Returns:
        tuple(io.BytesIO, io.BytesIO, io.BytesIO): (zip_buf, json_buf, png_buf)
    """
    # --- DataFrame nodi/archi ---
    nodes_rows = []
    for n in elements.get("nodes", []):
        d = n["data"]
        nodes_rows.append({
            "id": d.get("id"),
            "label": d.get("label"),
            "type": d.get("type"),
            "degree": int(degree_map.get(d.get("id"), 0))
        })
    edges_rows = []
    for e in elements.get("edges", []):
        d = e["data"]
        edges_rows.append({
            "source": d.get("source"),
            "target": d.get("target"),
            "label": d.get("label", "")
        })

    df_nodes = pd.DataFrame(nodes_rows, columns=["id","label","type","degree"])
    df_edges = pd.DataFrame(edges_rows, columns=["source","target","label"])

    # --- ZIP CSV ---
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("nodes.csv", df_nodes.to_csv(index=False))
        zf.writestr("edges.csv", df_edges.to_csv(index=False))
    zip_buf.seek(0)

    # --- JSON ---
    graph_json = {
        "nodes": [n["data"] for n in elements.get("nodes", [])],
        "edges": [e["data"] for e in elements.get("edges", [])]
    }
    json_bytes = io.BytesIO(json.dumps(graph_json, ensure_ascii=False, indent=2).encode("utf-8"))

    # --- PNG con networkx (layout deterministico) ---
    G = nx.Graph()
    for r in nodes_rows:
        if r["id"] is not None:
            G.add_node(r["id"], **r)
    for r in edges_rows:
        if r["source"] is not None and r["target"] is not None:
            G.add_edge(r["source"], r["target"], label=r.get("label",""))

    if len(G) == 0:
        fig, ax = plt.subplots(figsize=(12, 8))
        ax.text(0.5, 0.5, "Grafo vuoto", ha="center", va="center")
        ax.axis("off")
        png_buf = io.BytesIO()
        fig.savefig(png_buf, format="png", dpi=150)
        plt.close(fig)
        png_buf.seek(0)
        return zip_buf, json_bytes, png_buf

    pos = nx.spring_layout(G, seed=42)  # deterministico

    sizes = []
    colors = []
    for nid, data in G.nodes(data=True):
        deg = data.get("degree", 0)
        base = 120 if data.get("type") in ("Stakeholder","Project") else 80
        sizes.append(min(base + int(40 * (deg ** 0.7)), 400))
        colors.append(color_map.get(data.get("type"), "#999"))

    fig, ax = plt.subplots(figsize=(12, 8))
    nx.draw_networkx_edges(G, pos, ax=ax, alpha=0.35, width=1)
    nx.draw_networkx_nodes(G, pos, ax=ax, node_size=sizes, node_color=colors, linewidths=0.5, edgecolors="#333")

    labels_to_draw = {n: G.nodes[n].get("label", n) for n in G.nodes if G.nodes[n].get("type") in ("Stakeholder","Project")}
    nx.draw_networkx_labels(G, pos, labels=labels_to_draw, font_size=9)
    ax.axis("off")
    fig.tight_layout()
    png_buf = io.BytesIO()
    fig.savefig(png_buf, format="png", dpi=150)
    plt.close(fig)
    png_buf.seek(0)

    return zip_buf, json_bytes, png_buf


# ---------------- Layout ----------------
col1, col2 = st.columns([3, 1], gap="large")

with col1:
    # Ritorno di agraph: può essere dict (con chiavi 'selected_node'/'selected') o string.
    ret = agraph(nodes=nodes, edges=edges, config=config)

    st.markdown("### Export del grafo visualizzato")
    zip_buf, json_buf, png_buf = build_export_artifacts(elements, degree_map, COLOR)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button(
            "⬇️ CSV (nodes+edges.zip)",
            data=zip_buf,
            file_name="graph_export_csv.zip",
            mime="application/zip",
            use_container_width=True
        )
    with c2:
        st.download_button(
            "⬇️ JSON",
            data=json_buf,
            file_name="graph_export.json",
            mime="application/json",
            use_container_width=True
        )
    with c3:
        st.download_button(
            "⬇️ PNG (snapshot)",
            data=png_buf,
            file_name="graph_snapshot.png",
            mime="image/png",
            use_container_width=True
        )

with col2:
    st.subheader("Dettaglio nodo selezionato")
    selected_id = None
    # Normalizzazione dell'output di agraph:
    if isinstance(ret, dict):
        selected_id = ret.get("selected_node") or ret.get("selected")
    elif isinstance(ret, str) and ret and ret != "Nothing selected":
        selected_id = ret

    if selected_id:
        st.code(selected_id)
        # Persistenza della selezione tra rerun:
        # - Chiave "selected_node" letta anche nella modalità "Ego (locale)" per avviare l'ego-grafo.
        st.session_state["selected_node"] = selected_id

        # Tipo del nodo per abilitare/disabilitare le azioni (non modifica la logica).
        node_label = None
        try:
            node_data = api(f"/node/{selected_id}", params=None)
            node_label = node_data.get("label")  # 'stakeholder' | 'project' | 'keyword' | 'sdg' | 'sector' | 'lod'
        except Exception:
            st.caption("Impossibile leggere il tipo del nodo per le azioni.")

        # Badge tipo nodo
        if node_label:
            st.write("Tipo:", end=" ")
            badge(node_label.upper(), node_label)
        else:
            st.caption("Tipo: —")

        st.divider()

        # --- Azioni sul nodo (solo Stakeholder/Project) ---
        if node_label in ("stakeholder", "project"):
            st.markdown("#### Azioni")
            if st.button("Apri dettaglio nodo", use_container_width=True, key="btn_open_detail"):
                st.switch_page("pages/dettaglio_nodo.py")

            if st.button("Modifica", use_container_width=True, key="btn_edit_selected"):
                # Stato di editing condiviso con le pagine di inserimento.
                st.session_state["edit_node"] = selected_id   # id su cui aprire il form
                st.session_state["edit_mode"] = True          # abilita modalità modifica
                if node_label == "stakeholder":
                    st.switch_page("pages/inserimento_stakeholder.py")
                else:
                    st.switch_page("pages/inserimento_progetto.py")

            with st.expander("Elimina nodo", expanded=False):
                st.warning("Questa azione è irreversibile. Conferma per eliminare il nodo e le sue relazioni.")
                sure = st.checkbox("Confermo di voler eliminare definitivamente questo nodo.", key="confirm_delete")
                if st.button("Elimina definitivamente", use_container_width=True, disabled=not sure, key="btn_delete_selected"):
                    try:
                        api_post("/delete_node", {"id": selected_id})
                        st.success("Nodo eliminato con successo.")
                        # Pulizia stato per evitare riferimenti a nodi eliminati:
                        st.session_state.pop("selected_node", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Eliminazione fallita: {e}")
        else:
            st.info("Modifica ed eliminazione disponibili solo per **Stakeholder** e **Project**.")

        # Azione di ricalcolo in modalità Ego basata sul nodo selezionato.
        if viz_mode == "Ego (locale)":
            if st.button("Mostra ego-grafo da questo nodo", use_container_width=True, key="btn_show_ego"):
                st.session_state["selected_node"] = selected_id
                st.rerun()
    else:
        st.caption("Clicca un nodo nel grafo per vederne i dettagli.")

    st.divider()
    st.subheader("Azioni")
    if st.button("Inserisci nuovo Stakeholder", use_container_width=True):
        # Disattiva edit_mode per garantire nuovo inserimento pulito.
        st.session_state["edit_mode"] = False
        st.session_state.pop("edit_node", None)
        st.switch_page("pages/inserimento_stakeholder.py")

    if st.button("Inserisci nuovo Progetto", use_container_width=True):
        st.session_state["edit_mode"] = False
        st.session_state.pop("edit_node", None)
        st.switch_page("pages/inserimento_progetto.py")

    if st.button("Dashboard KPI", use_container_width=True):
        st.switch_page("pages/dashboard.py")

    if st.button("Predizione Connessioni tra Stakeholder", use_container_width=True):
        st.switch_page("pages/predizioni_stakeholder.py")