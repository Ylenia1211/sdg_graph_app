"""
Inserisci / Modifica Progetto (Streamlit)
-----------------------------------------

Pagina per creare o aggiornare un **Project** con:
- campi base (ID, Nome, Localizzazione, Descrizione, SDG)
- selezione Stakeholder da catalogo o input manuale
- gestione Keywords (da DB + nuove manuali)
- estrazione/validazione **LOD** da descrizione (Wikidata, DBpedia Spotlight, GeoNames)
- esportazione dei link LOD validati (CSV/JSON)
- salvataggio verso backend via `api_post("/project", ...)`

Stato (`st.session_state`) usato:
- `lod_results`: cache per ultima estrazione LOD per record_key -> {df_wd, df_dbp, df_geo, run_id}
- `lod_keeps_store`: preferenze “keep” per riga per ogni run_id (persistono tra refresh finché in sessione)
- `lod_export_validated`: ultimo pacchetto esportabile (json/csv) relativo alla vista corrente
- `lod_confirmed`: flag di conferma per record_key; quando True l’editor viene bloccato
- `lod_confirmed_snapshot`: snapshot delle righe confermate per record_key (usata al submit)
- `edit_mode`: True/False per distinguere modifica vs inserimento
- `edit_node`: id logico del nodo da modificare (es. "project:p1")
- `selected_node`: per navigazione post-salvataggio (imposta il dettaglio)

Input & Validazione:
- Campi **obbligatori**: `ID`, `Nome`. In assenza → errori a video e `st.stop()`.
- SDG, Stakeholder, Keywords opzionali. Keywords vengono **deduplicate** preservando l’ordine.
- Estrattori LOD opzionali: errori di rete/servizio vengono mostrati come messaggi non bloccanti.
"""

import time
import json
import numpy as np
import pandas as pd
import streamlit as st
from typing import Optional, List, Dict
from utils import api, api_post
from lod_linking import link_text_to_wikidata, spotlight_it, geonames_search

# ----------------- CONFIG -----------------
st.set_page_config(page_title="Inserisci / Modifica Progetto", layout="centered")

# ----------------- COSTANTI / CONFIGURAZIONE -----------------
# Palette SDG predefinita (usata per la tendina SDG) – estratta in costante per chiarezza.
SDG_OPTIONS = [f"SDG{i}" for i in range(1, 18)]

# Timeout tra chiamate a GeoNames per non saturare la quota API.
GEONAMES_SLEEP_S = 0.35

# ----------------- STATE INIT (riusabili tra più form) -----------------
if "lod_results" not in st.session_state:
    st.session_state["lod_results"] = {}      # { record_key: {"df_wd":..., "df_dbp":..., "df_geo":..., "run_id": str} }
if "lod_keeps_store" not in st.session_state:
    st.session_state["lod_keeps_store"] = {}  # { run_id: { row_sig: bool } }
if "lod_export_validated" not in st.session_state:
    st.session_state["lod_export_validated"] = None  # {"json": str, "csv": str}
if "lod_confirmed" not in st.session_state:
    st.session_state["lod_confirmed"] = {}    # { record_key: bool }
if "lod_confirmed_snapshot" not in st.session_state:
    st.session_state["lod_confirmed_snapshot"] = {}  # { record_key: list[dict] }

# ----------------- KEYWORDS (DB) -----------------
def _parse_kw_csv(s: str) -> List[str]:
    """Parsa una stringa CSV semplice in una lista di keyword pulite."""
    return [k.strip() for k in (s or "").split(",") if k.strip()]

def _normalize_kw_list(raw) -> List[str]:
    """Accetta list[str] o list[dict] e restituisce list[str] (name/label/keyword)."""
    out: List[str] = []
    for x in (raw or []):
        if isinstance(x, str):
            val = x.strip()
        elif isinstance(x, dict):
            val = (x.get("name") or x.get("label") or x.get("keyword") or "").strip()
        else:
            val = ""
        if val:
            out.append(val)
    # dedup preservando ordine
    seen, norm = set(), []
    for k in out:
        if k not in seen:
            seen.add(k); norm.append(k)
    return norm

def _dedup_preserving_order(items: List[str]) -> List[str]:
    """Deduplica una lista mantenendo il primo ordine di apparizione."""
    seen, out = set(), []
    for k in items:
        if k and k not in seen:
            seen.add(k); out.append(k)
    return out

@st.cache_data(ttl=300, show_spinner=False)
def load_keywords_from_db(q: Optional[str] = None, limit: int = 500) -> List[str]:
    """
    Carica le keyword dal backend (endpoint /keywords) e le normalizza.

    Supporta payload:
      - {"keywords":[...]}
      - {"items":[...]}
      - oppure direttamente una lista
    """
    try:
        params = {"limit": limit}
        if q:
            params["q"] = q
        data = api("/keywords", params=params)
        if isinstance(data, list):
            raw = data
        elif isinstance(data, dict):
            raw = data.get("keywords") or data.get("items") or []
        else:
            raw = []
        return _normalize_kw_list(raw)
    except Exception as e:
        st.warning(f"Impossibile caricare le keyword dal database: {e}")
        return []

# ----------------- UTILS (LOD) -----------------
ALL_COLS = ["source","term","label","qid","description","link","score","country","latitude","longitude","types"]

def _ensure_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Garantisce che un DataFrame contenga tutte le colonne ALL_COLS (aggiungendo None)."""
    for c in ALL_COLS:
        if c not in df.columns:
            df[c] = None
    return df[ALL_COLS]

def _df_wikidata(rows):
    """Normalizza le righe Wikidata in DataFrame con schema ALL_COLS."""
    if not rows:
        return pd.DataFrame(columns=ALL_COLS)
    df = pd.DataFrame(rows)
    df["source"] = "Wikidata"
    if "url" in df.columns:
        df["link"] = df["url"]
    df = df.rename(columns={"qid":"qid", "label":"label", "description":"description", "term":"term"})
    return _ensure_cols(df)

def _df_dbpedia(rows):
    """Normalizza le righe DBpedia Spotlight (IT) in DataFrame con schema ALL_COLS."""
    if not rows:
        return pd.DataFrame(columns=ALL_COLS)
    norm = []
    for r in rows:
        uri = r.get("url")
        label = (uri.rsplit("/", 1)[-1].replace("_", " ") if uri else None)
        norm.append({
            "source": "DBpedia Spotlight",
            "term": r.get("term"),
            "label": label,
            "qid": None,
            "description": None,
            "link": uri,
            "score": None,
            "types": r.get("types"),
            "country": None,
            "latitude": None,
            "longitude": None
        })
    return _ensure_cols(pd.DataFrame(norm))

def _df_geonames(rows):
    """Normalizza i risultati GeoNames in DataFrame con schema ALL_COLS."""
    if not rows:
        return pd.DataFrame(columns=ALL_COLS)
    norm = []
    for r in rows:
        norm.append({
            "source": "GeoNames",
            "term": r.get("term"),
            "label": r.get("label"),
            "qid": r.get("geonameId"),
            "description": None,
            "link": r.get("url"),
            "score": None,
            "country": r.get("country"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "types": None
        })
    return _ensure_cols(pd.DataFrame(norm))

def _row_sigs(df: pd.DataFrame) -> List[str]:
    """Calcola una firma di riga per poter salvare e ripristinare il flag 'keep'."""
    cols = ["source","term","label","qid","link"]
    cols = [c for c in cols if c in df.columns]
    return (df[cols].fillna("").astype(str).agg(" | ".join, axis=1).tolist())

def _with_keep(df: pd.DataFrame, run_id: str, default=False) -> pd.DataFrame:
    """Aggiunge/propaga la colonna booleana 'keep' usando lo store di sessione per run_id."""
    df = df.copy()
    if "keep" not in df.columns:
        df["keep"] = bool(default)
    saved = st.session_state["lod_keeps_store"].get(run_id, {})
    if saved:
        sigs = _row_sigs(df)
        df["keep"] = [saved.get(s, k) for s, k in zip(sigs, df["keep"])]
    return df

def _save_keeps(df: pd.DataFrame, run_id: str):
    """Persistenza per-run delle scelte 'keep' nella sessione Streamlit."""
    sigs = _row_sigs(df)
    st.session_state["lod_keeps_store"][run_id] = {s: bool(k) for s, k in zip(sigs, df["keep"])}

def _extract_lod_from_text(text: str, use_wd=True, use_dbp=True, use_geo=False,
                           min_score=0.5, geonames_username=""):
    """Esegue l’estrazione LOD dalla descrizione (Wikidata, DBpedia, opzionale GeoNames).

    Args:
        text: testo sorgente.
        use_wd/use_dbp/use_geo: abilita/disabilita le fonti.
        min_score: soglia minima per Wikidata.
        geonames_username: credenziali GeoNames (obbligatorie se use_geo=True).

    Returns:
        tuple(DataFrame, DataFrame, DataFrame): (df_wd, df_dbp, df_geo) normalizzati.
    """
    df_wd = pd.DataFrame(columns=ALL_COLS)
    df_dbp = pd.DataFrame(columns=ALL_COLS)
    df_geo = pd.DataFrame(columns=ALL_COLS)

    if use_wd:
        try:
            res_wd = link_text_to_wikidata(text, lang="it", min_score=min_score)
            df_wd = _df_wikidata(res_wd)
        except Exception as e:
            st.error(f"Errore Wikidata: {e}")

    if use_dbp:
        try:
            res_dbp = spotlight_it(text, conf=0.4, supp=0)
            df_dbp = _df_dbpedia(res_dbp)
        except Exception as e:
            st.error(f"Errore DBpedia Spotlight: {e}")

    if use_geo:
        if not geonames_username.strip():
            st.info("Inserisci lo username GeoNames per usare GeoNames.")
        else:
            try:
                terms_for_geo = sorted(set(df_wd["term"].dropna())) if not df_wd.empty else []
                if not terms_for_geo:
                    terms_for_geo = list({w.strip(",.;:()") for w in text.split() if len(w) >= 3})[:10]
                rows_geo = []
                for t in terms_for_geo[:15]:
                    r1 = geonames_search(t, username=geonames_username, max_rows=3, strict=True)
                    rows_geo.extend(r1)
                    if not r1:
                        rows_geo.extend(geonames_search(t, username=geonames_username, max_rows=1, strict=False))
                    # Rispetta la quota: sleep configurabile
                    time.sleep(GEONAMES_SLEEP_S)
                df_geo = _df_geonames(rows_geo)
                if not df_geo.empty:
                    df_geo = df_geo.drop_duplicates(subset=["qid"], keep="first")
            except Exception as e:
                st.error(f"Errore GeoNames: {e}")

    return df_wd, df_dbp, df_geo

# ---------- JSON-SAFE HELPERS ----------
def df_to_json_records(df: pd.DataFrame) -> list:
    """Converte un DataFrame in records JSON-safe (NaN/Inf -> None)."""
    if df is None or df.empty:
        return []
    safe = df.copy()
    safe = safe.replace([np.inf, -np.inf], np.nan)
    safe = safe.where(pd.notnull(safe), None)
    return safe.to_dict(orient="records")

def sanitize(obj):
    """Rende JSON-safe qualsiasi struttura (float NaN/Inf -> None)."""
    if isinstance(obj, float):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj

# ---------- LOD URI NORMALIZATION ----------
def _build_uri_from_row(row: dict) -> Optional[str]:
    """Crea una URI canonica a partire da una riga LOD (preferisce 'link', poi costruisce da qid)."""
    src = str(row.get("source") or "").strip().lower()
    link = str(row.get("link") or "").strip()
    qid  = str(row.get("qid") or "").strip()

    if link:
        return link
    if src == "wikidata" and qid:
        q = qid if qid.upper().startswith("Q") else f"Q{qid}"
        return f"https://www.wikidata.org/entity/{q}"
    if src == "geonames" and qid:
        return f"https://www.geonames.org/{qid}"
    return None

def normalize_lod_records(records: List[Dict]) -> List[Dict]:
    """Rende i record LOD coerenti: imposta link/url, label, cast numeri, filtra i vuoti."""
    out: List[Dict] = []
    for r in records or []:
        rr = dict(r)

        # URI canonica (link) + alias url
        uri = _build_uri_from_row(rr)
        rr["link"] = uri
        rr["url"]  = uri

        # coalesce della label
        if not (rr.get("label") or "").strip():
            rr["label"] = rr.get("term")

        # cast numeri
        for k in ("latitude", "longitude", "score"):
            v = rr.get(k, None)
            if v in ("", None):
                rr[k] = None
            else:
                try:
                    rr[k] = float(v)
                except Exception:
                    rr[k] = None

        # pulizia stringhe vuote -> None
        for k in ("term","label","description","country","types","qid","source"):
            if isinstance(rr.get(k), str) and rr[k].strip() == "":
                rr[k] = None

        if rr.get("link"):
            out.append(rr)

    return out

# ---------- STAKEHOLDER CATALOG ----------
@st.cache_data(ttl=60)
def load_stakeholder_catalog() -> pd.DataFrame:
    """Carica un catalogo stakeholder dai vari endpoint noti, normalizzando colonne essenziali."""
    candidates = [
        ("/stakeholders", None),
        ("/nodes", {"type": "stakeholder"}),
        ("/stakeholder", None),
    ]
    last_err = None
    for endpoint, params in candidates:
        try:
            data = api(endpoint, params=params)
            rows = None
            if isinstance(data, dict):
                for k in ("results", "items", "stakeholders", "nodes", "data"):
                    if isinstance(data.get(k), list):
                        rows = data[k]; break
                if rows is None and isinstance(data.get("list"), list):
                    rows = data["list"]
            if rows is None and isinstance(data, list):
                rows = data
            if not rows:
                continue

            df = pd.DataFrame(rows)
            if "id" not in df.columns:
                for cand in ("_id", "sid", "stakeholder_id", "uuid"):
                    if cand in df.columns:
                        df = df.rename(columns={cand: "id"}); break
            if "name" not in df.columns:
                for cand in ("label", "title", "denominazione", "ragione_sociale"):
                    if cand in df.columns:
                        df = df.rename(columns={cand: "name"}); break

            df = df.dropna(subset=["id"]).copy()
            df["id"] = df["id"].astype(str)
            if "name" not in df.columns or df["name"].isna().all():
                df["name"] = df.get("label", df.get("title", df["id"]))
            if "type" not in df.columns:
                for cand in ("category", "tipologia", "kind"):
                    if cand in df.columns:
                        df = df.rename(columns={cand: "type"}); break
            if "type" not in df.columns:
                df["type"] = None

            df = df.drop_duplicates(subset=["id"], keep="first").reset_index(drop=True)
            return df[["id", "name", "type"]]
        except Exception as e:
            last_err = e
            continue

    if last_err:
        st.info(f"Impossibile caricare il catalogo stakeholder dal DB ({last_err}). Uso input manuale come fallback.")
    else:
        st.info("Nessuno stakeholder trovato sul DB. Uso input manuale come fallback.")
    return pd.DataFrame(columns=["id", "name", "type"])

# ----------------- MODALITÀ (create vs edit) -----------------
edit_mode = bool(st.session_state.get("edit_mode"))
edit_node = st.session_state.get("edit_node")  # es. "project:p1"
prefill: Dict[str, Optional[str]] = {}

if edit_mode and isinstance(edit_node, str) and edit_node.startswith("project:"):
    try:
        data = api(f"/node/{edit_node}", params=None)
        p = (data.get("node") or {}).get("p") or {}
        stakeholders = (data.get("node") or {}).get("stakeholders") or []
        prefill = {
            "id": p.get("id", ""),
            "name": p.get("name", ""),
            "location": p.get("location", "") or "",
            "description": p.get("description", "") or "",
            "stakeholders": ", ".join([s.get("id","") for s in stakeholders if s.get("id")]),
            "keywords": ", ".join((data.get("node") or {}).get("keywords") or []),
        }
        prefill_sdgs = (data.get("node") or {}).get("sdgs") or []
    except Exception as e:
        st.error(f"Impossibile caricare i dati per la modifica: {e}")
        edit_mode = False
        prefill = {}
        prefill_sdgs = []
else:
    prefill_sdgs = []

# Pre-parse lista keyword per default del multiselect
prefill_kw_list = _parse_kw_csv(prefill.get("keywords", ""))

st.title("Modifica Progetto" if edit_mode else "➕ Inserimento Progetto")
if edit_mode:
    st.info(f"Stai modificando l'entità **{edit_node}**. L'ID non è modificabile.", icon="ℹ️")

# ---- Catalogo stakeholder dal DB (se disponibile) ----
stk_catalog = load_stakeholder_catalog()
prefill_stk_ids = [s.strip() for s in (prefill.get("stakeholders") or "").split(",") if s.strip()]

# ----------------- FORM -----------------
with st.form("proj_form"):
    col1, col2 = st.columns(2)
    with col1:
        pid = st.text_input("ID*", placeholder="p456", value=prefill.get("id", ""), disabled=edit_mode)
        location = st.text_input("Localizzazione", placeholder="Torino, IT", value=prefill.get("location", ""))
        name = st.text_input("Nome*", placeholder="Smart City X", value=prefill.get("name", ""))
    with col2:
        # Lista SDG estratta in costante (SDG_OPTIONS) per visibilità/config centralizzata.
        sdgs = st.multiselect("SDG", options=SDG_OPTIONS, default=[x for x in prefill_sdgs if x in SDG_OPTIONS])

    desc = st.text_area("Descrizione", value=prefill.get("description", ""))

    # ---- Stakeholder: multiselect da DB con fallback manuale ----
    selected_stakeholder_ids: List[str] = []
    if not stk_catalog.empty:
        def _labelify(row):
            t = f" · {row['type']}" if isinstance(row.get("type"), str) and row["type"].strip() else ""
            return f"{row['name']} [{row['id']}] {t}"

        stk_catalog = stk_catalog.copy()
        stk_catalog["label"] = stk_catalog.apply(_labelify, axis=1)
        options = dict(zip(stk_catalog["label"], stk_catalog["id"]))
        default_labels = [lbl for lbl, sid in options.items() if sid in prefill_stk_ids]

        st.write("### Stakeholder partecipanti")
        selected_labels = st.multiselect(
            "Scegli stakeholder dal DB",
            options=list(options.keys()),
            default=default_labels,
            help="Digita per cercare. I valori salvati saranno gli ID.",
        )
        selected_stakeholder_ids = [options[lbl] for lbl in selected_labels]

        manual_ids = st.text_input("Aggiungi ID manuali (opzionale, separati da virgola)", placeholder="es. s123, s789")
        if manual_ids.strip():
            selected_stakeholder_ids += [s.strip() for s in manual_ids.split(",") if s.strip()]

        seen = set()
        selected_stakeholder_ids = [x for x in selected_stakeholder_ids if not (x in seen or seen.add(x))]
    else:
        stakeholders_text = st.text_input(
            "Stakeholder partecipanti (id, separati da virgola)",
            placeholder="s123, s789",
            value=prefill.get("stakeholders", "")
        )
        selected_stakeholder_ids = [s.strip() for s in stakeholders_text.split(",") if s.strip()]

    # ---- Keywords da DB (+ nuove manuali) ----
    st.markdown("#### Keywords")
    kw_col1, kw_col2 = st.columns([2,1])
    with kw_col1:
        kw_query = st.text_input("Cerca nel database", value="", placeholder="digita per filtrare (facoltativo)")
    with kw_col2:
        st.caption("La lista si aggiorna digitando")

    db_keywords = load_keywords_from_db(q=kw_query or None)

    selected_keywords = st.multiselect(
        "Seleziona da database",
        options=db_keywords,
        default=[k for k in prefill_kw_list if k in db_keywords],
        help="Puoi selezionare più voci dalle keyword già presenti nel database."
    )

    new_keywords_csv = st.text_input(
        "Aggiungi nuove keyword (separate da virgola, opzionale)",
        placeholder="es. smart-city, mobility",
        value=""
    )

    # ----------------- LOD EXPANDER (come per Stakeholder) -----------------
    st.markdown("---")
    with st.expander("🔗 Annotazione & Linking LOD dalla descrizione", expanded=False):
        lcol1, lcol2, lcol3 = st.columns([1,1,1])
        with lcol1:
            min_score = st.slider("Soglia Wikidata", 0.0, 1.0, 0.5, 0.05, key="proj_lod_min_score")
            use_wd = st.checkbox("Usa Wikidata", value=True, key="proj_lod_use_wd")
        with lcol2:
            use_dbp = st.checkbox("Usa DBpedia Spotlight (IT)", value=True, key="proj_lod_use_dbp")
            use_geo = st.checkbox("Usa GeoNames (IT cities)", value=False, key="proj_lod_use_geo")
        with lcol3:
            geonames_username = st.text_input("GeoNames username", value="", key="proj_lod_geo_user")

        record_key = f"project:{prefill.get('id','') or 'new'}"
        res = st.session_state["lod_results"].get(record_key)
        confirmed = bool(st.session_state["lod_confirmed"].get(record_key))

        bcol1, bcol2, bcol3 = st.columns([1,1,1])
        with bcol1:
            run = st.form_submit_button("Estrai da descrizione", use_container_width=True)
        with bcol2:
            clear = st.form_submit_button("Svuota estrazione", use_container_width=True)
        with bcol3:
            apply_kw = st.checkbox("Aggiungi alle keywords", value=False, help="Aggiunge term/label dei link selezionati")
            over_loc = st.checkbox("Sovrascrivi location se trovata", value=False)

        if clear:
            st.session_state["lod_results"].pop(record_key, None)
            st.session_state["lod_export_validated"] = None
            st.session_state["lod_confirmed"].pop(record_key, None)
            st.session_state["lod_confirmed_snapshot"].pop(record_key, None)
            st.success("Estrazione LOD svuotata per questo progetto.")

        if run:
            if not (desc or "").strip():
                st.warning("Compila la descrizione per procedere all'estrazione.")
            else:
                with st.spinner("Analisi e collegamento in corso..."):
                    df_wd, df_dbp, df_geo = _extract_lod_from_text(
                        desc, use_wd=use_wd, use_dbp=use_dbp, use_geo=use_geo,
                        min_score=min_score, geonames_username=geonames_username
                    )
                run_id = f"{record_key}:{time.time()}"
                st.session_state["lod_results"][record_key] = {
                    "df_wd": df_wd, "df_dbp": df_dbp, "df_geo": df_geo, "run_id": run_id
                }
                st.session_state["lod_confirmed"].pop(record_key, None)
                st.session_state["lod_confirmed_snapshot"].pop(record_key, None)
                st.success("Estrazione completata. Seleziona le righe da tenere (keep).")
                res = st.session_state["lod_results"][record_key]
                confirmed = False

        # UI validazione
        if res:
            df_wd = res["df_wd"]; df_dbp = res["df_dbp"]; df_geo = res["df_geo"]; run_id = res["run_id"]
            editor_key = f"proj_lod_editor_{run_id}"

            frames = [df for df in [df_wd, df_dbp, df_geo] if not df.empty]
            if not frames:
                st.info("Nessun link trovato dalle sorgenti selezionate.")
            else:
                df_all = pd.concat(frames, ignore_index=True)
                df_all = _with_keep(df_all, run_id=run_id, default=False)

                st.caption(f"Trovati {len(df_all)} collegamenti da {df_all['source'].nunique()} sorgente(i).")

                ddcol1, ddcol2, ddcol3, ddcol4 = st.columns(4)
                with ddcol1:
                    if st.checkbox("Seleziona tutto (vista)", value=False, key=f"proj_sel_all_{run_id}"):
                        df_all["keep"] = True
                with ddcol2:
                    if st.checkbox("Deseleziona tutto (vista)", value=False, key=f"proj_desel_all_{run_id}"):
                        df_all["keep"] = False
                with ddcol3:
                    min_sc_v = st.number_input("Score minimo WD (vista)", 0.0, 1.0, 0.0, 0.05, key=f"proj_minsc_{run_id}")
                with ddcol4:
                    src_filter = st.selectbox(
                        "Filtra sorgente",
                        ["Tutte"] + sorted(df_all["source"].dropna().unique().tolist()),
                        index=0, key=f"proj_src_{run_id}"
                    )

                view = df_all.copy()
                if min_sc_v > 0:
                    view = view[view["score"].fillna(0) >= min_sc_v]
                if src_filter != "Tutte":
                    view = view[view["source"] == src_filter]

                editor_disabled = True if confirmed else ["source","term","label","qid","description","link","score","country","latitude","longitude","types"]

                df_edit = st.data_editor(
                    view,
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "keep": st.column_config.CheckboxColumn("Keep"),
                        "link": st.column_config.LinkColumn("Link", display_text="Apri"),
                        "score": st.column_config.NumberColumn("Score", help="0..1 (solo Wikidata)", format="%.3f"),
                    },
                    disabled=editor_disabled,
                    key=editor_key,
                )

                if not confirmed:
                    df_all.loc[df_edit.index, "keep"] = df_edit["keep"].values
                    _save_keeps(df_all, run_id)

                kept = df_all[df_all["keep"]].copy()
                st.markdown(f"**Selezionati:** {len(kept)} / {len(df_all)}")

                if kept.empty:
                    st.session_state["lod_export_validated"] = None
                else:
                    kept_clean = kept.drop(columns=["keep"]) if "keep" in kept.columns else kept
                    records_raw = df_to_json_records(kept_clean)
                    records = normalize_lod_records(records_raw)
                    st.session_state["lod_export_validated"] = {
                        "json": json.dumps(records, ensure_ascii=False, indent=2),
                        "csv": pd.DataFrame(records).to_csv(index=False)
                    }

                if apply_kw and not kept.empty:
                    new_kw = []
                    for _, r in kept.iterrows():
                        for cand in [r.get("term"), r.get("label")]:
                            if cand and cand not in new_kw:
                                new_kw.append(cand)
                    st.caption(f"Keywords suggerite da LOD: {', '.join(new_kw[:8])}{'…' if len(new_kw)>8 else ''}")
                if over_loc and not df_geo.empty:
                    geo_label = df_geo["label"].dropna().astype(str).head(1).tolist()
                    if geo_label:
                        st.caption(f"Location suggerita da GeoNames: {geo_label[0]}")

                cva, cvb = st.columns([1,1])
                with cva:
                    confirmed_click = st.form_submit_button("✅ Convalida selezioni", use_container_width=True, disabled=confirmed)
                with cvb:
                    unconfirm_click = st.form_submit_button("❌ Annulla convalida", use_container_width=True, disabled=not confirmed)

                if confirmed_click:
                    st.session_state["lod_confirmed"][record_key] = True
                    snap_df = kept.drop(columns=["keep"]) if "keep" in kept.columns else kept
                    st.session_state["lod_confirmed_snapshot"][record_key] = df_to_json_records(snap_df)
                    st.success("Selezioni convalidate. L'editor è ora bloccato.")
                if unconfirm_click:
                    st.session_state["lod_confirmed"][record_key] = False
                    st.session_state["lod_confirmed_snapshot"].pop(record_key, None)
                    st.info("Convalida annullata. Puoi modificare di nuovo le selezioni.")

    # ----------------- SUBMIT/CANCEL -----------------
    c1, c2 = st.columns([1,1])
    with c1:
        submitted = st.form_submit_button("Salva", use_container_width=True)
    with c2:
        cancel = st.form_submit_button("Annulla", use_container_width=True)

# ----------------- DOWNLOAD (fuori dal form!) -----------------
st.markdown("---")
export_pkg = st.session_state.get("lod_export_validated")
if export_pkg:
    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            "Scarica JSON (validati)",
            data=export_pkg["json"],
            file_name="lod_links_validati_project.json",
            mime="application/json",
            use_container_width=True
        )
    with d2:
        st.download_button(
            "Scarica CSV (validati)",
            data=export_pkg["csv"],
            file_name="lod_links_validati_project.csv",
            mime="text/csv",
            use_container_width=True
        )

# ----------------- AZIONI POST-FORM -----------------
if cancel:
    st.session_state["edit_mode"] = False
    st.session_state.pop("edit_node", None)
    st.switch_page("home.py")

if submitted:
    # Validazione minima
    errs = []
    if not (pid or "").strip():
        errs.append("ID è obbligatorio.")
    if not (name or "").strip():
        errs.append("Nome è obbligatorio.")
    if errs:
        # Stop esplicito per impedire invio verso backend con campi obbligatori mancanti.
        for e in errs:
            st.error(e)
        st.stop()

    payload = {
        "id": pid.strip(),
        "name": name.strip(),
        "description": (desc or "").strip() or None,
        "location": (location or "").strip() or None,
        "stakeholders": selected_stakeholder_ids,
        # keywords = selezionate da DB + nuove manuali, con dedup (niente campo di testo unico)
        "keywords": _dedup_preserving_order(
            (selected_keywords or []) + _parse_kw_csv(new_keywords_csv)
        ),
        "sdgs": sdgs,
        "mode": "update" if edit_mode else "create"
    }

    # *** INTEGRAZIONE LOD NEL PAYLOAD ***
    record_key = f"project:{prefill.get('id','') or 'new'}"
    res = st.session_state["lod_results"].get(record_key)
    if res:
        run_id = res["run_id"]
        df_wd = res["df_wd"]; df_dbp = res["df_dbp"]; df_geo = res["df_geo"]
        frames = [df for df in [df_wd, df_dbp, df_geo] if not df.empty]
        if frames:
            df_all = pd.concat(frames, ignore_index=True)

            if st.session_state["lod_confirmed"].get(record_key) and record_key in st.session_state["lod_confirmed_snapshot"]:
                kept_list = st.session_state["lod_confirmed_snapshot"][record_key]
                kept = pd.DataFrame(kept_list)
            else:
                df_all = _with_keep(df_all, run_id=run_id, default=False)
                kept = df_all[df_all["keep"]].drop(columns=["keep"]).reset_index(drop=True)

            # JSON-safe + normalizzazione URI/label
            lod_records_raw = df_to_json_records(kept)
            lod_records = normalize_lod_records(lod_records_raw)
            payload["lod_links"] = lod_records

            payload["wikidata_qids"] = sorted({str(q) for q in kept.loc[kept["source"]=="Wikidata","qid"].dropna().astype(str)})
            payload["dbpedia_uris"] = sorted({str(u) for u in kept.loc[kept["source"]=="DBpedia Spotlight","link"].dropna().astype(str)})
            payload["geonames_ids"] = sorted({str(g) for g in kept.loc[kept["source"]=="GeoNames","qid"].dropna().astype(str)})

            # arricchisci keywords da link tenuti (senza duplicati)
            extra_kw = []
            for _, r in kept.iterrows():
                for cand in [r.get("term"), r.get("label")]:
                    if cand and cand not in extra_kw:
                        extra_kw.append(cand)
            if extra_kw:
                base_kw = set(payload["keywords"])
                for k in extra_kw:
                    if k not in base_kw:
                        payload["keywords"].append(k)

            # se location vuota e ho geonames, usa prima label
            if (not payload["location"]) and (not df_geo.empty):
                geo_label = df_geo["label"].dropna().astype(str).head(1).tolist()
                if geo_label:
                    payload["location"] = geo_label[0]

    # dedup finale keywords
    payload["keywords"] = _dedup_preserving_order(payload["keywords"])

    # sanitize e POST
    payload = sanitize(payload)
    try:
        r = api_post("/project", payload)
        pid_saved = r.get("id") or pid

        st.session_state["edit_mode"] = False
        st.session_state["selected_node"] = f"project:{pid_saved}"
        try:
            st.query_params.update(node=f"project:{pid_saved}")
        except Exception:
            pass

        st.toast(f"Progetto salvato (id={pid_saved}).", icon="✅")
        st.switch_page("pages/dettaglio_nodo.py")
        st.stop()

    except Exception as e:
        st.error(f"Salvataggio fallito: {e}")