# pages/inserisci_stakeholder.py
"""
inserimento_stakeholder.py — Inserisci / Modifica Stakeholder (Streamlit)
-------------------------------------------------------------------------
Pagina per creare o aggiornare uno **Stakeholder** con:
- campi base (ID, Nome, Tipo, Settore, Localizzazione, Descrizione)
- gestione **Keywords** (da DB + nuove manuali, con dedup e ordine preservato)
- estrazione/validazione **LOD** dalla descrizione (Wikidata, DBpedia Spotlight, GeoNames)
- esportazione dei link LOD validati (CSV/JSON)
- salvataggio verso backend via `api_post("/stakeholder", ...)`

Stato (`st.session_state`) usato:
- `lod_results`: cache per ultima estrazione LOD per record_key → {df_wd, df_dbp, df_geo, run_id}
- `lod_keeps_store`: per-run (run_id) le scelte utente *keep* riga-per-riga
- `lod_export_validated`: ultimo pacchetto esportabile (json/csv) relativo alla vista corrente
- `lod_confirmed`: mappa record_key → bool che blocca/sblocca l’editor dopo convalida
- `lod_confirmed_snapshot`: snapshot dei record LOD confermati (usato al submit)
- `edit_mode`: True/False per distinguere modifica vs inserimento
- `edit_node`: id logico del nodo da modificare (es. "stakeholder:s1")
- `selected_node`: per navigazione post-salvataggio (imposta il dettaglio)

Input & Validazione:
- Campi **obbligatori**: `ID`, `Nome`, `Settore`. In assenza → errori a video e `st.stop()`.
- `Tipo` selezionato da lista; Keywords **deduplicate** preservando l’ordine.
- Estrazione LOD opzionale: eventuali errori lato servizi vengono mostrati ma non bloccano il flusso.
"""

import time
import json
import numpy as np
import pandas as pd
import streamlit as st
from utils import api, api_post
from lod_linking import link_text_to_wikidata, spotlight_it, geonames_search
from typing import Optional, List, Dict

# ----------------- CONFIGURAZIONE / COSTANTI -----------------
# Tipologie supportate nel selectbox (estratte in alto per visibilità/configurazione centralizzata)
STAKEHOLDER_TYPES = ["azienda", "ente pubblico", "ong", "università", "startup", "altro"]

# Pausa tra chiamate GeoNames (per rispettare le quote API)
GEONAMES_SLEEP_S = 0.35

# ----------------- KEYWORDS (DB) -----------------
def _parse_kw_csv(s: str) -> List[str]:
    """Parsa una stringa CSV semplice (separata da virgole) in una lista di keyword pulite."""
    return [k.strip() for k in (s or "").split(",") if k.strip()]

def _normalize_kw_list(raw) -> List[str]:
    """Accetta list[str] o list[dict] e restituisce list[str] (name/label/keyword)."""
    out = []
    for x in (raw or []):
        if isinstance(x, str):
            out.append(x.strip())
        elif isinstance(x, dict):
            val = (x.get("name") or x.get("label") or x.get("keyword") or "").strip()
            if val:
                out.append(val)
    # dedup preservando l'ordine
    seen = set()
    norm = []
    for k in out:
        if k not in seen:
            seen.add(k)
            norm.append(k)
    return norm

@st.cache_data(ttl=300, show_spinner=False)
def load_keywords_from_db(q: Optional[str] = None, limit: int = 500) -> List[str]:
    """
    Carica le keyword dal backend tentando i formati più comuni di risposta.
    - Endpoint: `/keywords?limit=..` oppure con filtro `/keywords?q=..&limit=..`
    - Accetta payload: `{"items":[...]}`, `{"keywords":[...]}` oppure direttamente `[...]`.
    """
    try:
        params = {"limit": limit}
        if q:
            params["q"] = q
        data = api("/keywords", params=params)
        # prova varie forme di payload
        raw = data if isinstance(data, list) else (data.get("items") or data.get("keywords") or [])
        return _normalize_kw_list(raw)
    except Exception as e:
        st.warning(f"Impossibile caricare le keyword dal database: {e}")
        return []

def _dedup_preserving_order(items: List[str]) -> List[str]:
    """Deduplica una lista mantenendo l’ordine di prima occorrenza."""
    seen = set()
    out: List[str] = []
    for k in items:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out

# ----------------- PAGE CONFIG -----------------
st.set_page_config(page_title="Inserisci / Modifica Stakeholder", layout="centered")

# ----------------- STATE INIT -----------------
if "lod_results" not in st.session_state:
    st.session_state["lod_results"] = {}  # { record_key: {"df_wd":..., "df_dbp":..., "df_geo":..., "run_id": str} }
if "lod_keeps_store" not in st.session_state:
    st.session_state["lod_keeps_store"] = {}  # { run_id: { row_sig: bool } }
if "lod_export_validated" not in st.session_state:
    st.session_state["lod_export_validated"] = None  # {"json": str, "csv": str}
if "lod_confirmed" not in st.session_state:
    st.session_state["lod_confirmed"] = {}  # { record_key: bool }
if "lod_confirmed_snapshot" not in st.session_state:
    st.session_state["lod_confirmed_snapshot"] = {}  # { record_key: list[dict] }

# ----------------- UTILS (LOD) -----------------
ALL_COLS = ["source","term","label","qid","description","link","score","country","latitude","longitude","types"]

def _ensure_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Garantisce che un DataFrame contenga tutte le colonne in ALL_COLS (riempie con None)."""
    for c in ALL_COLS:
        if c not in df.columns:
            df[c] = None
    return df[ALL_COLS]

def _df_wikidata(rows):
    """Normalizza risultati Wikidata nello schema ALL_COLS."""
    if not rows:
        return pd.DataFrame(columns=ALL_COLS)
    df = pd.DataFrame(rows)
    df["source"] = "Wikidata"
    if "url" in df.columns:
        df["link"] = df["url"]
    df = df.rename(columns={"qid":"qid", "label":"label", "description":"description", "term":"term"})
    return _ensure_cols(df)

def _df_dbpedia(rows):
    """Normalizza risultati DBpedia Spotlight (IT) nello schema ALL_COLS."""
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
    """Normalizza risultati GeoNames nello schema ALL_COLS."""
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

def _row_sigs(df: pd.DataFrame) -> list:
    """Crea firme riga-per-riga (source/term/label/qid/link) per ripristinare il flag 'keep'."""
    cols = ["source","term","label","qid","link"]
    cols = [c for c in cols if c in df.columns]
    return (df[cols].fillna("").astype(str).agg(" | ".join, axis=1).tolist())

def _with_keep(df: pd.DataFrame, run_id: str, default=False) -> pd.DataFrame:
    """Applica/propaga la colonna booleana 'keep' usando lo store in sessione per run_id."""
    df = df.copy()
    if "keep" not in df.columns:
        df["keep"] = bool(default)
    saved = st.session_state["lod_keeps_store"].get(run_id, {})
    if saved:
        sigs = _row_sigs(df)
        df["keep"] = [saved.get(s, k) for s, k in zip(sigs, df["keep"])]
    return df

def _save_keeps(df: pd.DataFrame, run_id: str):
    """Persistenza per-run delle scelte 'keep' (in `st.session_state`)."""
    sigs = _row_sigs(df)
    st.session_state["lod_keeps_store"][run_id] = {s: bool(k) for s, k in zip(sigs, df["keep"])}

def _extract_lod_from_text(text: str, use_wd=True, use_dbp=True, use_geo=False,
                           min_score=0.5, geonames_username=""):
    """Estrae riferimenti LOD dalla descrizione (Wikidata, DBpedia e opzionale GeoNames)."""
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
                    # Rispetta le quote: pausa configurabile
                    time.sleep(GEONAMES_SLEEP_S)
                df_geo = _df_geonames(rows_geo)
                if not df_geo.empty:
                    df_geo = df_geo.drop_duplicates(subset=["qid"], keep="first")
            except Exception as e:
                st.error(f"Errore GeoNames: {e}")

    return df_wd, df_dbp, df_geo

# ---------- JSON-SAFE HELPERS ----------
def df_to_json_records(df: pd.DataFrame) -> list:
    """Converte un DataFrame in records JSON-safe (NaN/Inf → None)."""
    if df is None or df.empty:
        return []
    safe = df.copy()
    safe = safe.replace([np.inf, -np.inf], np.nan)
    safe = safe.where(pd.notnull(safe), None)
    return safe.to_dict(orient="records")

def sanitize(obj):
    """Rende JSON-safe qualsiasi struttura (float NaN/Inf → None ricorsivo)."""
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
    """Costruisce una URI canonica privilegiando `link`; in fallback usa (source, qid)."""
    src = str(row.get("source") or "").strip().lower()
    link = str(row.get("link") or "").strip()
    qid  = str(row.get("qid") or "").strip()

    # se ho già un link valido, uso quello
    if link:
        return link

    # fallback: costruisci da source + qid
    if src == "wikidata" and qid:
        q = qid if qid.upper().startswith("Q") else f"Q{qid}"
        return f"https://www.wikidata.org/entity/{q}"
    if src == "geonames" and qid:
        return f"https://www.geonames.org/{qid}"

    # DBpedia senza link non è ricostruibile in modo affidabile
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

        # tieni solo i record che hanno davvero una URI
        if rr.get("link"):
            out.append(rr)

    return out

# ----------------- MODALITÀ (create vs edit) -----------------
edit_mode = bool(st.session_state.get("edit_mode"))
edit_node = st.session_state.get("edit_node")  # es. "stakeholder:s1"
prefill = {}

if edit_mode and isinstance(edit_node, str) and edit_node.startswith("stakeholder:"):
    try:
        data = api(f"/node/{edit_node}", params=None)
        s = (data.get("node") or {}).get("s") or {}
        prefill = {
            "id": s.get("id", ""),
            "name": s.get("name", ""),
            "type": s.get("type", "altro"),
            "sector": (data.get("node") or {}).get("sector") or "",
            "location": s.get("location", "") or "",
            "description": s.get("description", "") or "",
            "keywords": ", ".join((data.get("node") or {}).get("keywords") or []),
        }
        prefill["keywords_list"] = _parse_kw_csv(prefill.get("keywords", ""))

    except Exception as e:
        st.error(f"Impossibile caricare i dati per la modifica: {e}")
        edit_mode = False
        prefill = {}

st.title("Modifica Stakeholder" if edit_mode else "➕ Inserimento Stakeholder")
if edit_mode:
    st.info(f"Stai modificando l'entità **{edit_node}**. L'ID non è modificabile.", icon="ℹ️")

# ----------------- FORM -----------------
with st.form("stake_form"):
    col1, col2 = st.columns(2)
    with col1:
        sid = st.text_input(
            "ID*",
            placeholder="s123",
            value=prefill.get("id", ""),
            disabled=edit_mode
        )
        name = st.text_input(
            "Nome*",
            placeholder="ACME Foundation",
            value=prefill.get("name", "")
        )
        stype = st.selectbox(
            "Tipo*",
            STAKEHOLDER_TYPES,
            index=(STAKEHOLDER_TYPES.index(prefill.get("type","altro"))
                   if prefill.get("type") in STAKEHOLDER_TYPES else STAKEHOLDER_TYPES.index("altro"))
        )
    with col2:
        sector = st.text_input("Settore*", placeholder="Energy", value=prefill.get("sector", ""))
        location = st.text_input("Localizzazione", placeholder="Milano, IT", value=prefill.get("location", ""))
    desc = st.text_area("Descrizione", value=prefill.get("description", ""))
    st.markdown("#### Keywords")

    kw_col1, kw_col2 = st.columns([2,1])
    with kw_col1:
        kw_query = st.text_input(
            "Cerca nel database",
            value="",
            placeholder="digita per filtrare (facoltativo)"
        )
    # ⚠️ Dentro un form si usa solo form_submit_button (non st.button)
    with kw_col2:
        kw_reload = st.form_submit_button("🔄 Aggiorna lista", use_container_width=True)

    # Carica lista dal DB (cache 5 minuti). Aggiorna se digiti o se premi il pulsante.
    db_keywords = load_keywords_from_db(q=kw_query if (kw_query or kw_reload) else None)

    selected_keywords = st.multiselect(
        "Seleziona da database",
        options=db_keywords,
        default=[k for k in prefill.get("keywords_list", []) if k in db_keywords],
        help="Puoi selezionare più voci dalle keyword già presenti nel database."
    )

    new_keywords_csv = st.text_input(
        "Aggiungi nuove keyword (separate da virgola)",
        placeholder="es. renewable-energy, smart-grid",
        value=""  # non precompilo: il prefill lo metto nella multiselect
    )

    # Mantengo anche una versione CSV per debug/retrocompatibilità
    keywords = ", ".join(_dedup_preserving_order(selected_keywords + _parse_kw_csv(new_keywords_csv)))

    # ----------------- LOD EXPANDER -----------------
    st.markdown("---")
    with st.expander("🔗 Annotazione & Linking LOD dalla descrizione", expanded=False):
        lcol1, lcol2, lcol3 = st.columns([1,1,1])
        with lcol1:
            min_score = st.slider("Soglia Wikidata", 0.0, 1.0, 0.5, 0.05, key="lod_min_score")
            use_wd = st.checkbox("Usa Wikidata", value=True, key="lod_use_wd")
        with lcol2:
            use_dbp = st.checkbox("Usa DBpedia Spotlight (IT)", value=True, key="lod_use_dbp")
            use_geo = st.checkbox("Usa GeoNames (IT cities)", value=False, key="lod_use_geo")
        with lcol3:
            geonames_username = st.text_input("GeoNames username", value="", key="lod_geo_user")

        # Chiave per salvare i risultati di questo record (id in edit o "new")
        record_key = f"stakeholder:{prefill.get('id','') or 'new'}"
        res = st.session_state["lod_results"].get(record_key)
        confirmed = bool(st.session_state["lod_confirmed"].get(record_key))

        bcol1, bcol2, bcol3 = st.columns([1,1,1])
        with bcol1:
            run = st.form_submit_button("Estrai da descrizione", use_container_width=True)
        with bcol2:
            clear = st.form_submit_button("Svuota estrazione", use_container_width=True)
        with bcol3:
            # solo preview visuale per l'utente
            apply_kw = st.checkbox("Aggiungi alle keywords", value=False, help="Aggiunge term/label dei link selezionati")
            over_loc = st.checkbox("Sovrascrivi location se trovata", value=False)

        if clear:
            st.session_state["lod_results"].pop(record_key, None)
            st.session_state["lod_export_validated"] = None
            st.session_state["lod_confirmed"].pop(record_key, None)
            st.session_state["lod_confirmed_snapshot"].pop(record_key, None)
            st.success("Estrazione LOD svuotata per questo stakeholder.")

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
                # reset stato di convalida su nuovo run
                st.session_state["lod_confirmed"].pop(record_key, None)
                st.session_state["lod_confirmed_snapshot"].pop(record_key, None)
                st.success("Estrazione completata. Seleziona le righe da tenere (keep).")
                res = st.session_state["lod_results"][record_key]
                confirmed = False

        # UI validazione se ho risultati
        if res:
            df_wd = res["df_wd"]; df_dbp = res["df_dbp"]; df_geo = res["df_geo"]; run_id = res["run_id"]
            editor_key = f"lod_editor_{run_id}"

            frames = [df for df in [df_wd, df_dbp, df_geo] if not df.empty]
            if not frames:
                st.info("Nessun link trovato dalle sorgenti selezionate.")
            else:
                df_all = pd.concat(frames, ignore_index=True)
                df_all = _with_keep(df_all, run_id=run_id, default=False)

                st.caption(f"Trovati {len(df_all)} collegamenti da {df_all['source'].nunique()} sorgente(i).")

                # Filtri e selezioni massime (semplici)
                ddcol1, ddcol2, ddcol3, ddcol4 = st.columns(4)
                with ddcol1:
                    if st.checkbox("Seleziona tutto (vista)", value=False, key=f"sel_all_{run_id}"):
                        df_all["keep"] = True
                with ddcol2:
                    if st.checkbox("Deseleziona tutto (vista)", value=False, key=f"desel_all_{run_id}"):
                        df_all["keep"] = False
                with ddcol3:
                    min_sc_v = st.number_input("Score minimo WD (vista)", 0.0, 1.0, 0.0, 0.05, key=f"minsc_{run_id}")
                with ddcol4:
                    src_filter = st.selectbox(
                        "Filtra sorgente",
                        ["Tutte"] + sorted(df_all["source"].dropna().unique().tolist()),
                        index=0, key=f"src_{run_id}"
                    )

                view = df_all.copy()
                if min_sc_v > 0:
                    view = view[view["score"].fillna(0) >= min_sc_v]
                if src_filter != "Tutte":
                    view = view[view["source"] == src_filter]

                # Se già confermato, rendo l'editor non editabile
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

                # Propaga keep dalla vista a df_all solo se non confermato
                if not confirmed:
                    df_all.loc[df_edit.index, "keep"] = df_edit["keep"].values
                    _save_keeps(df_all, run_id)

                kept = df_all[df_all["keep"]].copy()
                st.markdown(f"**Selezionati:** {len(kept)} / {len(df_all)}")

                # Prepara pacchetto export
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

                # Preview di cosa verrebbe propagato
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

                # ----------------- CONVALIDA / SBLOCCA -----------------
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
            file_name="lod_links_validati.json",
            mime="application/json",
            use_container_width=True
        )
    with d2:
        st.download_button(
            "Scarica CSV (validati)",
            data=export_pkg["csv"],
            file_name="lod_links_validati.csv",
            mime="text/csv",
            use_container_width=True
        )

# ----------------- AZIONI POST-FORM -----------------
if 'cancel' in locals() and cancel:
    st.session_state["edit_mode"] = False
    st.session_state.pop("edit_node", None)
    st.switch_page("home.py")

if 'submitted' in locals() and submitted:
    # Validazione minima
    errors = []
    if not (sid or "").strip():
        errors.append("ID è obbligatorio.")
    if not (name or "").strip():
        errors.append("Nome è obbligatorio.")
    if not (sector or "").strip():
        errors.append("Settore è obbligatorio.")
    if errors:
        for e in errors:
            st.error(e)
        st.stop()

    payload = {
        "id": sid.strip(),
        "name": name.strip(),
        "type": stype.strip(),
        "sector": sector.strip(),
        "location": (location or "").strip() or None,
        "description": (desc or "").strip() or None,
        "keywords": _dedup_preserving_order(
            (selected_keywords or []) + _parse_kw_csv(new_keywords_csv)
        ),
        "mode": "update" if edit_mode else "create"
    }

    # *** INTEGRAZIONE LOD NEL PAYLOAD ***
    record_key = f"stakeholder:{prefill.get('id','') or 'new'}"
    res = st.session_state["lod_results"].get(record_key)
    if res:
        run_id = res["run_id"]
        df_wd = res["df_wd"]; df_dbp = res["df_dbp"]; df_geo = res["df_geo"]
        frames = [df for df in [df_wd, df_dbp, df_geo] if not df.empty]
        if frames:
            df_all = pd.concat(frames, ignore_index=True)

            # Se convalidato, usa snapshot; altrimenti riprendi lo stato keep corrente
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
            payload["keywords"] = _dedup_preserving_order(payload["keywords"])

            # se location vuota, usa GeoNames (prima label)
            if (not payload["location"]) and (not df_geo.empty):
                geo_label = df_geo["label"].dropna().astype(str).head(1).tolist()
                if geo_label:
                    payload["location"] = geo_label[0]

    # Sanitize finale del payload (qualsiasi NaN/Inf residuo -> None)
    payload = sanitize(payload)

    # Chiamata backend + navigazione
    try:
        r = api_post("/stakeholder", payload)
        sid_saved = r.get("id") or sid

        # salva stato per la pagina di dettaglio
        st.session_state["edit_mode"] = False
        st.session_state["selected_node"] = f"stakeholder:{sid_saved}"

        # (opzionale) passa anche in querystring, utile come fallback
        try:
            st.query_params.update(node=f"stakeholder:{sid_saved}")
        except Exception:
            pass

        st.toast(f"Stakeholder salvato (id={sid_saved}).", icon="✅")

        # NAVIGA SUBITO alla pagina di dettaglio
        st.switch_page("pages/dettaglio_nodo.py")
        st.stop()

    except Exception as e:
        st.error(f"Salvataggio fallito: {e}")