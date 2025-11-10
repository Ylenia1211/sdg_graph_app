#questo non è nell'applicazione era una page di prova
import json
import time
import pandas as pd
import streamlit as st
from streamlit_app.pages.lod_linking import link_text_to_wikidata, spotlight_it, geonames_search

st.set_page_config(page_title="Annotazione & Linking LOD", layout="wide")
st.subheader("Annotazione & Linking LOD")

# --------------------- STATE INIT ---------------------
if "results" not in st.session_state:
    st.session_state["results"] = None  # dict: {"df_wd":..., "df_dbp":..., "df_geo":..., "run_id": float}
if "keeps_store" not in st.session_state:
    st.session_state["keeps_store"] = {}  # { run_id_str: { row_signature: bool } }
if "choice_per_term" not in st.session_state:
    st.session_state["choice_per_term"] = {}  # { term: option_label }

# --------------------- INPUT UI -----------------------
txt = st.text_area(
    "Incolla la descrizione",
    height=180,
    placeholder="Descrizione dello stakeholder o del progetto..."
)

with st.sidebar:
    st.header("Opzioni")
    min_score = st.slider("Soglia confidenza (Wikidata)", 0.0, 1.0, 0.5, 0.05)
    use_wd = st.checkbox("Usa Wikidata", value=True)
    use_dbp = st.checkbox("Usa DBpedia Spotlight (IT)", value=True)
    use_geo = st.checkbox("Usa GeoNames (solo città italiane)", value=False)
    geonames_username = st.text_input("GeoNames username", value="", help="Necessario per chiamare l'API GeoNames")
    st.caption("Suggerimento: per risultati più ricchi da Wikidata mantieni la soglia tra 0.45–0.6.")

    st.markdown("---")
    st.header("Validazione")
    st.caption("Whitelist = tieni sempre; Blacklist = scarta sempre (priorità più alta).")
    wl_text = st.text_area("Whitelist termini (uno per riga)", height=110, placeholder="es.\nMilano\nPolitecnico di Milano")
    bl_text = st.text_area("Blacklist termini (uno per riga)", height=110, placeholder="es.\nSocietà\nProgetto")
    apply_lists = st.checkbox("Applica whitelist/blacklist automaticamente", value=True)

    st.markdown("---")
    col_sb1, col_sb2 = st.columns(2)
    with col_sb1:
        run = st.button("🔗 Estrai e collega", type="primary")
    with col_sb2:
        clear = st.button("🧹 Svuota risultati")

# --------------------- HELPERS ------------------------
ALL_COLS = ["source","term","label","qid","description","link","score","country","latitude","longitude","types"]

def _ensure_cols(df: pd.DataFrame) -> pd.DataFrame:
    for c in ALL_COLS:
        if c not in df.columns:
            df[c] = None
    return df[ALL_COLS]

def _df_wikidata(rows):
    if not rows:
        return pd.DataFrame(columns=ALL_COLS)
    df = pd.DataFrame(rows)
    df["source"] = "Wikidata"
    df["link"] = df["url"]
    df = df.rename(columns={"qid":"qid", "label":"label", "description":"description", "term":"term"})
    return _ensure_cols(df)

def _df_dbpedia(rows):
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

# --- Persistenza scelte utente / validazione ---
def _row_sigs(df: pd.DataFrame) -> list[str]:
    cols = ["source","term","label","qid","link"]
    cols = [c for c in cols if c in df.columns]
    return (df[cols].fillna("").astype(str).agg(" | ".join, axis=1).tolist())

def _with_keep(df: pd.DataFrame, run_id: str, default=False) -> pd.DataFrame:
    """Aggiunge colonna 'keep' e ripristina da keeps_store[run_id]. default keep=False."""
    df = df.copy()
    if "keep" not in df.columns:
        df["keep"] = bool(default)
    saved = st.session_state["keeps_store"].get(run_id, {})
    if saved:
        sigs = _row_sigs(df)
        df["keep"] = [saved.get(s, k) for s, k in zip(sigs, df["keep"])]
    return df

def _save_keeps(df: pd.DataFrame, run_id: str):
    sigs = _row_sigs(df)
    st.session_state["keeps_store"][run_id] = {s: bool(k) for s, k in zip(sigs, df["keep"])}

def _parse_term_list(text: str) -> set[str]:
    lines = [l.strip().lower() for l in (text or "").splitlines()]
    return {l for l in lines if l}

def _apply_whitelist_blacklist(df: pd.DataFrame, wl: set[str], bl: set[str]) -> pd.DataFrame:
    if df.empty or ("term" not in df.columns):
        return df
    df = df.copy()
    term_lower = df["term"].fillna("").str.lower()
    if wl:
        df.loc[term_lower.isin(wl), "keep"] = True
    if bl:
        df.loc[term_lower.isin(bl), "keep"] = False  # priorità
    return df

# --------------------- ACTIONS ------------------------
if clear:
    st.session_state["results"] = None
    st.session_state["keeps_store"].clear()
    st.session_state["choice_per_term"].clear()
    st.success("Risultati svuotati.")
    st.stop()

if run:
    if not txt.strip():
        st.warning("Inserisci del testo prima di procedere.")
        st.stop()
    with st.spinner("Analisi e collegamento in corso..."):
        df_wd = pd.DataFrame(columns=ALL_COLS)
        df_dbp = pd.DataFrame(columns=ALL_COLS)
        df_geo = pd.DataFrame(columns=ALL_COLS)

        # Wikidata
        if use_wd:
            try:
                res_wd = link_text_to_wikidata(txt, lang="it", min_score=min_score)
                df_wd = _df_wikidata(res_wd)
            except Exception as e:
                st.error(f"Errore Wikidata: {e}")

        # DBpedia Spotlight
        if use_dbp:
            try:
                res_dbp = spotlight_it(txt, conf=0.4, supp=0)
                df_dbp = _df_dbpedia(res_dbp)
            except Exception as e:
                st.error(f"Errore DBpedia Spotlight: {e}")

        # GeoNames
        if use_geo:
            if not geonames_username.strip():
                st.info("Inserisci lo username GeoNames nella sidebar per usare GeoNames.")
            else:
                try:
                    terms_for_geo = sorted(set(df_wd["term"].dropna())) if not df_wd.empty else []
                    if not terms_for_geo:
                        terms_for_geo = list({w.strip(",.;:()") for w in txt.split() if len(w) >= 3})[:10]
                    rows_geo = []
                    for t in terms_for_geo[:15]:
                        r1 = geonames_search(t, username=geonames_username, max_rows=3, strict=True)
                        rows_geo.extend(r1)
                        if not r1:
                            rows_geo.extend(geonames_search(t, username=geonames_username, max_rows=1, strict=False))
                        time.sleep(0.35)
                    df_geo = _df_geonames(rows_geo)
                    if not df_geo.empty:
                        df_geo = df_geo.drop_duplicates(subset=["qid"], keep="first")
                except Exception as e:
                    st.error(f"Errore GeoNames: {e}")

    # salva risultati in state
    run_id = str(time.time())
    st.session_state["results"] = {"df_wd": df_wd, "df_dbp": df_dbp, "df_geo": df_geo, "run_id": run_id}
    # pulisci keeps e scelte relativi a precedenti risultati
    st.session_state["choice_per_term"].clear()
    # NB: non pulisco l’intero keeps_store per conservare storia; userà comunque run_id diverso
    st.success("Estrazione completata. Ora puoi validare i risultati.")

# --------------------- DISPLAY / VALIDATION ------------------------
res = st.session_state["results"]

if not res:
    st.info("Esegui l'estrazione per visualizzare e validare i risultati.")
    st.stop()

df_wd = res["df_wd"]
df_dbp = res["df_dbp"]
df_geo = res["df_geo"]
run_id = res["run_id"]
editor_key = f"editor_all_{run_id}"  # chiave stabile finché non rigeneri i risultati

tabs = st.tabs(["Tutti", "Wikidata", "DBpedia Spotlight", "GeoNames", "Risoluzione per termine"])

# --- Tutti (validazione riga-per-riga con 'keep') ---
with tabs[0]:
    frames = [df for df in [df_wd, df_dbp, df_geo] if not df.empty]
    if not frames:
        st.info("Nessun link trovato dalle sorgenti selezionate.")
    else:
        df_all = pd.concat(frames, ignore_index=True)
        df_all = _with_keep(df_all, run_id=run_id, default=False)

        # Applica whitelist/blacklist se richiesto
        wl = _parse_term_list(wl_text)
        bl = _parse_term_list(bl_text)
        if apply_lists:
            df_all = _apply_whitelist_blacklist(df_all, wl, bl)

        st.success(f"Trovati {len(df_all)} collegamenti da {df_all['source'].nunique()} sorgente(i).")
        st.caption(f"Whitelist: {len(wl)} termini • Blacklist: {len(bl)} termini")

        # Filtri rapidi (sulla vista)
        with st.expander("Filtri rapidi"):
            colf1, colf2, colf3 = st.columns(3)
            with colf1:
                srcs = ["Tutti"] + sorted(df_all["source"].dropna().unique().tolist())
                src_sel = st.selectbox("Sorgente", srcs, index=0)
            with colf2:
                min_sc = st.slider("Score minimo (solo Wikidata)", 0.0, 1.0, 0.0, 0.05)
            with colf3:
                only_geo_it = st.checkbox("Solo elementi con country valorizzato", value=False)

        df_view = df_all.copy()
        if src_sel != "Tutti":
            df_view = df_view[df_view["source"] == src_sel]
        if min_sc > 0:
            df_view = df_view[(df_view["score"].fillna(0) >= min_sc)]
        if only_geo_it:
            df_view = df_view[df_view["country"].notna() & (df_view["country"] != "")]

        # Azioni di massa sulla vista
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            if st.button("Seleziona tutto (vista)"):
                df_all.loc[df_view.index, "keep"] = True
        with c2:
            if st.button("Deseleziona tutto (vista)"):
                df_all.loc[df_view.index, "keep"] = False
        with c3:
            if st.button("Ripristina default (False)"):
                df_all["keep"] = False
        with c4:
            if st.button("Applica whitelist/blacklist ora"):
                df_all = _apply_whitelist_blacklist(df_all, wl, bl)

        # Editor interattivo (abilito solo 'keep')
        df_edit = st.data_editor(
            df_view,
            hide_index=True,
            use_container_width=True,
            column_config={
                "keep": st.column_config.CheckboxColumn("Keep", help="Spunta per tenere la riga"),
                "link": st.column_config.LinkColumn("Link", display_text="Apri"),
                "score": st.column_config.NumberColumn("Score", help="0..1 (solo Wikidata)", format="%.3f"),
                "country": st.column_config.TextColumn("Country"),
                "latitude": st.column_config.NumberColumn("Latitude", format="%.5f"),
                "longitude": st.column_config.NumberColumn("Longitude", format="%.5f"),
            },
            disabled=["source","term","label","qid","description","link","score","country","latitude","longitude","types"],
            key=editor_key,
        )

        # Propaga le modifiche dalla vista alla tabella completa
        df_all.loc[df_edit.index, "keep"] = df_edit["keep"].values

        # Salva stato 'keep' legato al run_id
        _save_keeps(df_all, run_id=run_id)

        # Deriva i validati
        df_keep = df_all[df_all["keep"]].copy()
        st.markdown(f"**Selezionati:** {len(df_keep)} / {len(df_all)}")

        # Download solo validati
        colx, coly = st.columns(2)
        with colx:
            st.download_button(
                "Scarica JSON (validati)",
                data=json.dumps(df_keep.drop(columns=["keep"]).to_dict(orient="records"), ensure_ascii=False, indent=2),
                file_name="lod_links_validati.json",
                mime="application/json"
            )
        with coly:
            st.download_button(
                "Scarica CSV (validati)",
                data=df_keep.drop(columns=["keep"]).to_csv(index=False),
                file_name="lod_links_validati.csv",
                mime="text/csv"
            )

# --- Wikidata ---
with tabs[1]:
    if df_wd.empty:
        st.info("Nessun risultato da Wikidata.")
    else:
        st.dataframe(
            df_wd, hide_index=True, use_container_width=True,
            column_config={"link": st.column_config.LinkColumn("Wikidata", display_text="Apri"),
                           "score": st.column_config.NumberColumn("Score", help="0..1", format="%.3f")}
        )
        st.download_button(
            "Scarica JSON (Wikidata)",
            data=df_wd.to_json(orient="records", force_ascii=False, indent=2),
            file_name="wikidata_links.json",
            mime="application/json"
        )

# --- DBpedia Spotlight ---
with tabs[2]:
    if df_dbp.empty:
        st.info("Nessun risultato da DBpedia Spotlight.")
    else:
        st.dataframe(
            df_dbp, hide_index=True, use_container_width=True,
            column_config={"link": st.column_config.LinkColumn("DBpedia", display_text="Apri")}
        )
        st.download_button(
            "Scarica JSON (DBpedia Spotlight)",
            data=df_dbp.to_json(orient="records", force_ascii=False, indent=2),
            file_name="dbpedia_links.json",
            mime="application/json"
        )

# --- GeoNames ---
with tabs[3]:
    if df_geo.empty:
        st.info("Nessun risultato da GeoNames.")
    else:
        st.dataframe(
            df_geo, hide_index=True, use_container_width=True,
            column_config={"link": st.column_config.LinkColumn("GeoNames", display_text="Apri"),
                           "country": st.column_config.TextColumn("Country"),
                           "latitude": st.column_config.NumberColumn("Latitude", format="%.5f"),
                           "longitude": st.column_config.NumberColumn("Longitude", format="%.5f")}
        )
        st.download_button(
            "Scarica JSON (GeoNames)",
            data=df_geo.to_json(orient="records", force_ascii=False, indent=2),
            file_name="geonames_links.json",
            mime="application/json"
        )

# --- Risoluzione per termine ---
with tabs[4]:
    frames = [df for df in [df_wd, df_dbp, df_geo] if not df.empty]
    if not frames:
        st.info("Nessun link per creare preferenze per termine.")
    else:
        df_all2 = pd.concat(frames, ignore_index=True)
        if "term" not in df_all2.columns or df_all2["term"].isna().all():
            st.info("Non sono presenti valori 'term' utilizzabili.")
        else:
            df_all2["_ord"] = (
                (df_all2["source"].eq("Wikidata")).astype(int) * 1000
                + df_all2["score"].fillna(0)
            )
            df_all2 = df_all2.sort_values(["term","_ord"], ascending=[True, False])
            terms = [t for t in df_all2["term"].dropna().unique().tolist() if t]

            for t in terms:
                subset = df_all2[df_all2["term"] == t][["source","label","qid","link","score","types"]].copy()
                subset["opt"] = subset.apply(lambda r: f"{r['source']} • {r['label']} • {r['qid'] or ''}".strip(), axis=1)
                opts = subset["opt"].tolist()
                default = st.session_state["choice_per_term"].get(t, (opts[0] if opts else ""))
                idx = opts.index(default) if default in opts else 0
                choice = st.selectbox(f"Seleziona collegamento per **{t}**", opts, index=idx, key=f"choice_{run_id}_{t}")
                st.session_state["choice_per_term"][t] = choice
                with st.expander("Dettagli correnti", expanded=False):
                    st.dataframe(subset, hide_index=True, use_container_width=True)

            # costruisci dataframe finale 1-per-term
            rows = []
            for t, opt in st.session_state["choice_per_term"].items():
                s = df_all2[df_all2["term"] == t].copy()
                s["opt"] = s.apply(lambda r: f"{r['source']} • {r['label']} • {r['qid'] or ''}".strip(), axis=1)
                pick = s[s["opt"] == opt].head(1).drop(columns=["_ord","opt"])
                if not pick.empty:
                    rows.append(pick.iloc[0].to_dict())
            df_one_per_term = pd.DataFrame(rows)

            st.markdown(f"**Selezionati:** {len(df_one_per_term)} termini")
            st.dataframe(
                df_one_per_term,
                hide_index=True,
                use_container_width=True,
                column_config={"link": st.column_config.LinkColumn("Link", display_text="Apri")}
            )
            st.download_button(
                "Scarica JSON (1-per-term)",
                data=json.dumps(df_one_per_term.to_dict(orient="records"), ensure_ascii=False, indent=2),
                file_name="lod_links_one_per_term.json",
                mime="application/json"
            )
