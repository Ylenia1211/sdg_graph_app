"""
predizioni_stakeholder.py — Predizioni di collegamenti tra Stakeholder (Streamlit)
----------------------------------------------------------------------------------

Questa pagina orchestra, tramite pulsanti e form, una pipeline di analisi su grafo
(esposta dal backend in /utils.api e /utils.api_post) per:

1) **Proiezione base + Embedding (FastRP)**
   - Crea/ricrea una proiezione del grafo `kg_sdg` con nodi Stakeholder/Project/Keyword
     e relazioni `participatesIn`, `hasKeyword`, `relatedToKeyword` (tutte undirected lato GDS).
   - Calcola e persiste l'embedding **FastRP** (proprietà `embedding_fastrp`).

2) **Link Prediction (GDS Pipeline)**
   - Materializza **COLLAB** (co-partecipazione a progetti) come archi espliciti.
   - Crea proiezione **LP** (`kg_sdg_lp`) che include l’arco target COLLAB.
   - Configura la pipeline LP, effettua lo **split** e allena un **modello**.
   - Facoltativamente salva/carica/elenca modelli.

3) **Predizioni**
   - **Cosine** su embedding (similarità tra due stakeholder; top-K suggeriti per A).
   - **Modello LP** (top-K suggeriti per A con probabilità).
   - **Jaccard su Progetto** (candidati non ancora coinvolti in un progetto).

Stato (`st.session_state`) utilizzato:
- `lp_graph_name`, `lp_pipeline_name`, `lp_model_name`: ultimi nomi inseriti dall’utente
  per grafo LP, pipeline e modello (riutilizzati tra le azioni).
- `lp_graph_info`: ultimo esito di `/gds/project_lp` (es. conteggio archi COLLAB).
- `lp_build_info`: info/payload di build della pipeline (contiene lo split scelto).
- `lp_train_summary`: riepilogo ultimo training (AUCPR, best model, ecc.).

Input & Validazione:
- Le operazioni chiamano endpoint REST. Errori sono mostrati con `st.error`.
- Per cosine/LP top-K serve l’ID di A; per cosine A↔B servono entrambi; per Jaccard
  serve `project_id`. In assenza, si mostra `st.warning` e si evita la chiamata.
"""

import streamlit as st
import pandas as pd
from typing import Optional
from contextlib import contextmanager
from utils import api, api_post

# =============================================================================
# Config pagina
# =============================================================================
st.set_page_config(page_title="Predizioni Stakeholder", layout="wide")

# =============================================================================
# THEME / STYLE (UI-ONLY)
#  - Qui potresti aggiungere CSS custom per varianti di pulsanti o chip.
# =============================================================================


# =============================================================================
# Helpers
# =============================================================================
@contextmanager
def variant(cls: Optional[str] = None):
    """Context manager per racchiudere elementi UI dentro un <div class="cls">.

    Serve a consentire, tramite CSS esterno, varianti di stile (p.es. .btn-success)
    sui pulsanti figli. Non modifica la logica dell'app: aggiunge solo wrapper HTML.

    Args:
        cls: Nome della/e classe/i CSS da applicare al wrapper <div>.
    """
    if cls:
        st.markdown(f'<div class="{cls}">', unsafe_allow_html=True)
    try:
        yield
    finally:
        if cls:
            st.markdown("</div>", unsafe_allow_html=True)

def chip(text, color="#3c78d8"):
    """Disegna una pill (chip) informativa colorata."""
    st.markdown(
        f'<span class="chip" style="background:{color}">{text}</span>',
        unsafe_allow_html=True
    )


# =============================================================================
# Header
# =============================================================================
st.title("Predizioni: potenziali collegamenti tra Stakeholder")
st.caption(
    "Pipeline: proiezione GDS (Stakeholder–Project–Keyword) → **FastRP** per embedding → "
    "**Cosine similarity** per affinità; **Jaccard** per candidati su un progetto; "
    "**Modello di Link Prediction** (GDS Pipeline) per probabilità di collaborazione."
)

st.divider()
with st.expander("Suggerimenti d'uso", expanded=False):
    st.markdown(
        """
- **Ordine consigliato**:
  1) Crea proiezione base (kg_sdg) → **Calcola FastRP**  
  2) **Materializza COLLAB** → **Crea proiezione LP** → **Configura pipeline** → **Allena modello**  
  3) Vai nelle tab **Embedding / Modello / Jaccard** per confrontare i risultati.
- Il **modello LP** usa la relazione target **COLLAB** (co-partecipazione a progetti) nella proiezione `kg_sdg_lp`.
- La tab **Jaccard** richiede un `Project ID` per generare candidati non ancora coinvolti.
- Lo **split** della pipeline è adattivo in base agli archi COLLAB del grafo LP.
"""
    )

# =============================================================================
# Stato GDS (lettura)
# =============================================================================
try:
    gds_info = api("/gds/status", params=None) or {}
except Exception as e:
    st.error(f"Errore nel recupero dello stato GDS: {e}")
    gds_info = {}

exists = bool(gds_info.get("exists"))
nodes = gds_info.get("nodes", 0)
rels = gds_info.get("rels", 0)
emb_present = bool(gds_info.get("hasEmbeddingsMem")) \
    or ("embedding_fastrp" in (gds_info.get("nodeProperties") or [])) \
    or ("embedding_fastrp_mem" in (gds_info.get("nodeProperties") or [])) \
    or (gds_info.get("dbEmbeddingsCount", 0) > 0)

k1, k2, k3 = st.columns(3)
k1.metric("Nodi (kg_sdg)", nodes if exists else 0)
k2.metric("Archi (kg_sdg)", rels if exists else 0)
k3.metric("Embedding FastRP", "OK" if emb_present else "ASSENTI")

st.divider()

# =============================================================================
# Step 1: Init Base (kg_sdg) + FastRP
# =============================================================================
st.subheader("Step 1) Inizializza proiezione base e FastRP")

colA, colB, colC = st.columns([1,1,6], gap="large")
with colA:
    with variant("btn-secondary"):
        if st.button("🔁 Crea/ricrea proiezione base (kg_sdg)", use_container_width=True):
            try:
                r = api_post("/gds/project", {})
                st.success(f"Projection pronta: {r.get('graph','kg_sdg')} (nodi={r.get('nodes')}, archi={r.get('rels')})")
            except Exception as e:
                st.error(f"Errore proiezione: {e}")
with colB:
    with variant("btn-success"):
        if st.button("⚡ Calcola FastRP (embedding_fastrp)", use_container_width=True):
            try:
                r = api_post("/gds/embeddings", {"dim": 64})
                st.success("Embedding calcolati e scritti su `embedding_fastrp`.")
                st.rerun()
            except Exception as e:
                st.error(f"Errore FastRP: {e}")
with colC:
    st.caption(
        "Proiezione: **Stakeholder, Project, Keyword** · Relazioni: **participatesIn**, **hasKeyword**, **relatedToKeyword** "
        "(tutte *UNDIRECTED*). FastRP scrive su `embedding_fastrp`."
    )

st.divider()

# =============================================================================
# Step 2: Link Prediction (GDS Pipeline)
# =============================================================================
st.subheader("Step 2) Modello di Link Prediction (GDS Pipeline)")

# Parametri coerenti (persistiti in sessione per comodità d'uso)
default_lp_graph = st.session_state.get("lp_graph_name", "kg_sdg_lp")
default_lp_name  = st.session_state.get("lp_pipeline_name", "lp-pipeline")
default_model    = st.session_state.get("lp_model_name", f"{default_lp_name}-model")

cparams = st.columns([1,1,1])
with cparams[0]:
    lp_graph = st.text_input("Nome grafo LP", value=default_lp_graph,
                             help="Projection con relazione target COLLAB.",
                             key="lp_graph_name_input")
with cparams[1]:
    lp_name = st.text_input("Nome pipeline LP", value=default_lp_name, key="lp_pipeline_name_input")
with cparams[2]:
    model_name = st.text_input("Nome modello", value=default_model, key="lp_model_name_input")

# Salva in sessione (riutilizzo tra azioni)
st.session_state["lp_graph_name"]    = lp_graph
st.session_state["lp_pipeline_name"] = lp_name
st.session_state["lp_model_name"]    = model_name

st.markdown("---")

# ===== Menu 1: Azioni Pipeline
st.markdown("**Azioni Pipeline**")
action = st.radio(
    "Seleziona un'azione",
    options=[
        "🔗 Materializza COLLAB",
        "📐 Crea proiezione LP",
        "🧪 Configura pipeline LP",
        "🏋️ Allena modello LP",
        "🧼 Reset pipeline"
    ],
    horizontal=True,
    key="pipeline_actions_radio"
)

run_clicked = False
with variant("btn-danger"):
    run_clicked = st.button("Esegui azione selezionata", use_container_width=True, key="btn_run_pipeline_action")

if run_clicked:
    if action == "🔗 Materializza COLLAB":
        try:
            r = api_post("/labels/write_collab", {})
            created = r.get("created", 0)
            if created == 0:
                st.warning("Nessuna nuova COLLAB creata (forse già presenti).")
            else:
                st.success(f"Relazioni COLLAB create: {created}")
        except Exception as e:
            st.error(f"Errore materializzazione COLLAB: {e}")

    elif action == "📐 Crea proiezione LP":
        try:
            r = api_post("/gds/project_lp", {})
            st.session_state["lp_graph_info"] = r
            rels_lp = r.get("rels", 0)
            st.success(f"LP projection: {r.get('graph')} (nodi={r.get('nodes')}, archi target={rels_lp})")
            if rels_lp == 0:
                st.info("La proiezione ha 0 archi COLLAB. Assicurati di aver materializzato COLLAB prima.")
        except Exception as e:
            st.error(f"Errore project_lp: {e}")

    elif action == "🧪 Configura pipeline LP":
        try:
            r = api_post("/lp/pipeline/build", {"name": lp_name, "graph": lp_graph}, timeout=300)
            if r.get("status") == "split_failed":
                st.error(
                    f"Split non valido (rels={r.get('rels', 0)}). "
                    f"{r.get('hint', 'Aumenta le relazioni COLLAB e riprova.')}"
                )
            else:
                st.session_state["lp_build_info"] = r
                st.success(f"Pipeline configurata: {r.get('pipeline')}")
        except Exception as e:
            st.error(f"Errore pipeline build: {e}")

    elif action == "🏋️ Allena modello LP":
        lp_info = st.session_state.get("lp_graph_info", {})
        rels_lp = lp_info.get("rels", None)
        if rels_lp is None:
            st.warning("Crea prima la proiezione LP (bottone '📐 Crea proiezione LP').")
        elif rels_lp == 0:
            st.error("La proiezione LP ha 0 archi COLLAB: materializza COLLAB e ricrea la proiezione.")
        else:
            try:
                with st.spinner("⏳ Training del modello in corso..."):
                    res = api_post(
                        "/lp/train",
                        {
                            "pipeline": st.session_state["lp_pipeline_name"],
                            "modelName": st.session_state["lp_model_name"],
                            "graph": st.session_state["lp_graph_name"],
                            "targetRelationshipType": "COLLAB",
                        },
                        timeout=900,
                    )
                if isinstance(res, dict) and res.get("status") == "split_failed":
                    st.error(
                        f"Split non valido (rels={res.get('rels', 0)}). "
                        f"{res.get('hint', 'Aumenta le relazioni COLLAB prima di allenare.')}"
                    )
                else:
                    st.session_state["lp_train_summary"] = res or {}
                    st.success("✅ Training completato.")
            except Exception as e:
                st.error(f"Errore training: {e}")

    elif action == "🧼 Reset pipeline":
        try:
            r = api_post("/lp/pipeline/drop", {"name": lp_name})
            if r.get("dropped") or r.get("noop"):
                st.success("Reset eseguito / non necessario.")
            else:
                st.info("Pipeline non presente.")
        except Exception as e:
            st.warning(f"Drop non supportato nella tua GDS: {e}")

st.markdown("")

# ===== Menu 2: Gestione Modello (facoltativo)
st.markdown("**Gestione modello (facoltativo)**")
model_action = st.radio(
    "Seleziona operazione modello",
    options=["💾 Salva su disco", "📦 Carica modello", "📋 Lista modelli"],
    horizontal=True,
    key="model_actions_radio"
)

with variant("btn-danger"):
    run_model_clicked = st.button("Esegui operazione modello", use_container_width=True, key="btn_run_model_action")

if run_model_clicked:
    if model_action == "💾 Salva su disco":
        try:
            r = api_post("/lp/model/store", {"modelName": model_name})
            st.success(f"Modello salvato: {r}")
        except Exception as e:
            st.error(f"Errore salvataggio modello: {e}")

    elif model_action == "📦 Carica modello":
        try:
            r = api_post("/lp/model/load", {"modelName": model_name})
            st.success(f"Modello caricato: {r}")
        except Exception as e:
            st.error(f"Errore caricamento modello: {e}")

    elif model_action == "📋 Lista modelli":
        try:
            r = api("/lp/model/list")
            models = r.get("models", r) if isinstance(r, dict) else r
            if models:
                df = pd.DataFrame(models)
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.info("Nessun modello in catalogo.")
        except Exception as e:
            st.error(f"Errore lista modelli: {e}")

# --- Split scelto (se disponibile) ---
build_info = st.session_state.get("lp_build_info", {})
if build_info.get("split"):
    s = build_info["split"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Train fraction", s.get("trainFraction"))
    m2.metric("Test fraction", s.get("testFraction"))
    m3.metric("Validation folds", s.get("validationFolds"))
    m4.metric("Archi COLLAB (LP)", s.get("rels"))

# --- Output training (ultimo risultato) ---
train_box = st.expander("Dettagli ultimo training (AUCPR / best model)", expanded=False)
with train_box:
    ts = st.session_state.get("lp_train_summary", {})
    if ts:
        st.json(ts)
    else:
        st.caption("— Nessun training eseguito in questa sessione —")

st.divider()

# =============================================================================
# Step 3: Predizioni
# =============================================================================
st.subheader("Step 3) Valuta possibili collaborazioni")

# Lista stakeholders (fallback a input manuale se l'endpoint non esiste)
stakeholders = []
try:
    stakeholders = (api("/list/stakeholders", params=None) or {}).get("stakeholders", [])
except Exception:
    stakeholders = []

if stakeholders:
    options = {f"{s.get('name','(senza nome)')} — {s['id']}": s["id"] for s in stakeholders}
    left, right = st.columns(2)
    with left:
        a_label = st.selectbox("Stakeholder A", options=list(options.keys()), key="sel_stk_a")
        a_id = options[a_label]
    with right:
        b_label = st.selectbox("Stakeholder B", options=list(options.keys()), key="sel_stk_b")
        b_id = options[b_label]
else:
    st.info("Non ho una lista stakeholders. Inserisci gli ID manualmente.")
    left, right = st.columns(2)
    with left:
        a_id = st.text_input("Stakeholder A (id)", key="stakeholder_a_id")
    with right:
        b_id = st.text_input("Stakeholder B (id)", key="stakeholder_b_id")

tabs = st.tabs(["🔹 Embedding (Cosine)", "🟣 Modello LP (probabilità)", "🟡 Jaccard su Progetto"])

# --- Tab 1: Cosine
with tabs[0]:
    colX, colY = st.columns([1,1])
    with colX:
        with variant():
            if st.button("Predici A↔B (cosine)", use_container_width=True, key="btn_cos_ab"):
                if not a_id or not b_id:
                    st.warning("Fornisci entrambi gli ID.")
                else:
                    try:
                        res = api("/predict/stakeholder-sim", params={"a": a_id, "b": b_id})
                        sim = res.get("similarity")
                        shared = res.get("explain", {})
                        st.markdown("#### Risultato A↔B")
                        chip(
                            f"Cosine similarity: {sim:.4f}" if sim is not None else "Cosine similarity: N/A",
                            "#6aa84f" if sim is not None and sim >= 0.5 else "#f39c12",
                        )
                        st.caption("Valori più alti indicano maggiore affinità potenziale (embedding FastRP).")
                        colL, colR = st.columns(2)
                        with colL:
                            st.markdown("**Progetti condivisi**")
                            projs = shared.get("sharedProjects", [])
                            if projs:
                                st.dataframe(pd.DataFrame(projs), use_container_width=True, hide_index=True)
                            else:
                                st.caption("— Nessuno —")
                        with colR:
                            st.markdown("**Keyword condivise**")
                            kws = shared.get("sharedKeywords", [])
                            if kws:
                                st.dataframe(pd.DataFrame([{"Keyword": k} for k in kws]),
                                             use_container_width=True, hide_index=True)
                            else:
                                st.caption("— Nessuna —")
                    except Exception as e:
                        st.error(f"Errore predizione A↔B: {e}")
    with colY:
        topk = st.number_input("Top-K consigli per A (cosine)", min_value=1, max_value=50, value=10, step=1,
                               key="cos_topk_input")
        with variant("btn-success"):
            if st.button("Suggerisci Top-K per A (cosine)", use_container_width=True, key="btn_cos_topk"):
                if not a_id:
                    st.warning("Specifica l'ID di A.")
                else:
                    try:
                        rec = api("/predict/stakeholder-topk", params={"a": a_id, "k": int(topk)})
                        rows = rec.get("results", [])
                        if rows:
                            df = pd.DataFrame(rows)
                            st.dataframe(df, use_container_width=True, hide_index=True)
                        else:
                            st.caption("Nessun suggerimento disponibile (controlla projection/embedding).")
                    except Exception as e:
                        st.error(f"Errore Top-K cosine: {e}")

# --- Tab 2: Modello LP
with tabs[1]:
    st.caption("Usa il modello addestrato dalla pipeline GDS per predire la probabilità di collaborazione.")
    mcol1, mcol2 = st.columns([1,1])
    with mcol1:
        model_name_tab = st.text_input("Nome modello", value=st.session_state.get("lp_model_name", "lp-pipeline-model"),
                                       key="lp_model_name_tab")
        k_model = st.number_input("Top-K per A (modello)", min_value=1, max_value=50, value=20, step=1,
                                  key="model_topk_input")
    with mcol2:
        graph_name_tab = st.text_input("Nome grafo LP", value=st.session_state.get("lp_graph_name", "kg_sdg_lp"),
                                       key="lp_graph_name_tab")
        st.write("")
    with variant("btn-success"):
        if st.button("Suggerisci Top-K per A (modello)", use_container_width=True, key="btn_model_topk"):
            if not a_id:
                st.warning("Specifica l'ID di A.")
            else:
                try:
                    res = api("/lp/predict/topk", params={
                        "a": a_id, "k": int(k_model),
                        "graph": graph_name_tab, "model": model_name_tab
                    })
                    rows = res.get("results", [])
                    if rows:
                        df = pd.DataFrame(rows)
                        df["probability"] = df["probability"].astype(float)
                        st.dataframe(df, use_container_width=True, hide_index=True)
                    else:
                        st.caption("Nessuna predizione (verifica: proiezione LP, pipeline, training).")
                except Exception as e:
                    st.error(f"Errore Top-K modello: {e}")

# --- Tab 3: Jaccard
with tabs[2]:
    st.caption("Dato un **progetto**, suggerisce stakeholder candidati che non vi partecipano, "
               "ordinati per similarità **Jaccard** su set di progetti correlati.")

    @st.cache_data(ttl=60)
    def fetch_projects():
        """Recupera l’elenco progetti.
        Tenta prima `/list/projects`; in fallback usa `/projects/search?q=&limit=500`."""
        try:
            r = api("/list/projects", params=None) or {}
            projects = r.get("projects", []) or []
            if projects:
                return projects
        except Exception:
            pass
        try:
            r = api("/projects/search", params={"q": "", "limit": 500}) or {}
            return r.get("projects", []) or []
        except Exception:
            return []

    def label_project(p):
        """Costruisce un’etichetta leggibile per selectbox progetti (Titolo [CODE] — id)."""
        code = p.get("code") or p.get("shortCode") or ""
        name = p.get("name") or p.get("title") or "(senza titolo)"
        pid  = p.get("id") or p.get("uuid") or ""
        left = name if not code else f"{name} [{code}]"
        return f"{left} — {pid}"

    projects = fetch_projects()

    selected_pid = None
    if projects:
        # Filtro client-side per liste lunghe
        q = st.text_input("Cerca progetto (titolo/codice, filtro locale)", value="")
        if q.strip():
            qlow = q.lower()
            filtered = [
                p for p in projects
                if qlow in (p.get("name","") or p.get("title","")).lower()
                or qlow in (p.get("code","") or p.get("shortCode","")).lower()
                or qlow in str(p.get("id","")).lower()
            ]
        else:
            filtered = projects

        if not filtered:
            st.info("Nessun progetto corrispondente al filtro. Prova a cambiare la ricerca.")
        else:
            options = {label_project(p): (p.get("id") or p.get("uuid")) for p in filtered}
            proj_label = st.selectbox("Seleziona progetto", list(options.keys()), index=0, key="sel_project_for_jaccard")
            selected_pid = options[proj_label]
    else:
        st.warning("Non sono riuscito a ottenere la lista dei progetti dal backend.")
        selected_pid = st.text_input("Project ID (inserimento manuale)", value="", key="jaccard_project_id_manual")

    with variant("btn-warning"):
        if st.button("Suggerisci candidati per Progetto (Jaccard)", use_container_width=True, key="btn_jaccard_pid"):
            pid = (selected_pid or "").strip()
            if not pid:
                st.warning("Seleziona o inserisci un Project ID.")
            else:
                try:
                    rec = api("/predict_collaboration", params={"project_id": pid})
                    rows = (rec or {}).get("predictions", [])
                    if rows:
                        df = pd.DataFrame(rows)
                        st.dataframe(df, use_container_width=True, hide_index=True)
                    else:
                        st.caption("Nessun candidato trovato per il progetto selezionato.")
                except Exception as e:
                    st.error(f"Errore predizione Jaccard: {e}")






