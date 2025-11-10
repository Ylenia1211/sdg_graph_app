# lod_linking.py
from __future__ import annotations
import re, time, json, logging
from typing import List, Dict, Iterable, Optional, Tuple
import requests, requests_cache
from rapidfuzz import fuzz
from sentence_transformers import SentenceTransformer, util
import numpy as np

# --- cache HTTP (evita rate limit e accelera)
requests_cache.install_cache("lod_cache", backend="sqlite", expire_after=60*60*24)

# --- HTTP session (User-Agent richiesto da Wikimedia)
_UA = "sdg-graph-app/0.1 (+mailto:cstetrial@gmail.com)"  # <-- metti un contatto reale
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": _UA,
    "Accept": "application/json",
})

def _request_with_retries(method: str, url: str, *, params=None, data=None, headers=None,
                          timeout=20, retries=4, backoff_start=0.8, add_origin=True):
    """
    Wrapper con backoff per 403/429/5xx. Puoi disattivare origin=* con add_origin=False (es. GeoNames).
    """
    params = dict(params or {})
    if add_origin:
        params.setdefault("origin", "*")
    backoff = backoff_start
    last_err = None
    for attempt in range(retries):
        try:
            r = _SESSION.request(method, url, params=params, data=data,
                                 headers=headers, timeout=timeout)
            if r.status_code in (403, 429, 502, 503, 504):
                last_err = requests.HTTPError(f"{r.status_code} for {r.url}", response=r)
                time.sleep(backoff)
                backoff *= 2
                continue
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            time.sleep(backoff)
            backoff *= 2
    if last_err:
        raise last_err

# --- spaCy
import spacy
_NLP = None
def get_nlp():
    global _NLP
    if _NLP is None:
        _NLP = spacy.load("it_core_news_lg", disable=["lemmatizer","textcat"])
    return _NLP

# --- embedding mBERT/USE-multilingual
_EMB = None
def get_encoder():
    global _EMB
    if _EMB is None:
        # multilingue e leggero
        _EMB = SentenceTransformer("sentence-transformers/distiluse-base-multilingual-cased-v2")
    return _EMB

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
DBP_SPOTLIGHT_IT = "https://api.dbpedia-spotlight.org/it/annotate"  # content-type: text/plain o x-www-form-urlencoded

# ----------------------------- PREPROCESS -----------------------------
def clean_text(s: str) -> str:
    s = s or ""
    s = re.sub(r"\s+", " ", s)
    return s.strip()

def extract_candidates(text: str, min_len: int = 3) -> List[str]:
    """
    Keyword candidates = NER (PER/ORG/LOC/GPE/PROD) + noun chunks + sostantivi composti.
    Filtra stopword e roba troppo corta.
    """
    nlp = get_nlp()
    doc = nlp(clean_text(text))

    # entità nominate
    ents = [e.text for e in doc.ents if e.label_ not in {"DATE","TIME","NUM","PERCENT","MONEY"}]

    # frasi nominali
    noun_chunks = [nc.text for nc in doc.noun_chunks]

    # termini (sostantivi/PROPN)
    terms = []
    for t in doc:
        if t.is_stop or t.is_punct or t.like_num:
            continue
        if t.pos_ in {"NOUN","PROPN"}:
            terms.append(t.text)

    cand = set([c.strip() for c in ents + noun_chunks + terms if len(c.strip()) >= min_len])
    # normalizza maiuscole/minuscole con rispetto per acronimi
    out = []
    for c in cand:
        if c.isupper() and len(c) <= 6:  # acronimo
            out.append(c)
        else:
            out.append(c.lower())
    return sorted(set(out))

# ----------------------------- TF-IDF (opzionale su corpus) -----------------------------
def tfidf_top_keywords(texts: Iterable[str], top_k: int = 20) -> List[Tuple[str, float]]:
    """ Passagli (ad es.) tutte le descrizioni: restituisce keyword globali più pesate. """
    from sklearn.feature_extraction.text import TfidfVectorizer
    texts = [clean_text(t) for t in texts if t and t.strip()]
    if not texts:
        return []
    vec = TfidfVectorizer(
        analyzer="word",
        ngram_range=(1,3),
        min_df=2, max_df=0.85,
        stop_words="italian"
    )
    X = vec.fit_transform(texts)
    scores = np.asarray(X.mean(axis=0)).ravel()
    idx = np.argsort(scores)[::-1][:top_k]
    feats = np.array(vec.get_feature_names_out())[idx]
    vals = scores[idx]
    return list(zip(feats.tolist(), vals.tolist()))

# ----------------------------- WIKIDATA: SEARCH & ENRICH -----------------------------
def wikidata_search(term: str, lang: str = "it", limit: int = 10) -> List[Dict]:
    """API wbsearchentities (veloce) — con UA/origin e retry."""
    params = {
        "action":"wbsearchentities",
        "language":lang,
        "uselang":lang,
        "format":"json",
        "search":term,
        "limit":limit
    }
    r = _request_with_retries("GET", WIKIDATA_API, params=params, timeout=20, retries=4)
    data = r.json()
    results = []
    for it in data.get("search", []):
        results.append({
            "id": it.get("id"),  # es. Q1281037
            "label": it.get("label"),
            "description": it.get("description"),
            "url": it.get("concepturi"),
            "match": it.get("match",{}).get("text"),
        })
    return results

def wikidata_fetch_types(qids: List[str]) -> Dict[str, List[str]]:
    """
    Ottiene istanze (P31) per filtro-disambiguazione. Ritorna {QID: [Qtype...]}.
    """
    if not qids:
        return {}
    qlist = " ".join(f"wd:{q}" for q in qids)
    q = f"""
    SELECT ?item ?type WHERE {{
      VALUES ?item {{ {qlist} }}
      OPTIONAL {{ ?item wdt:P31 ?type . }}
    }}
    """
    headers = {"Accept":"application/sparql-results+json"}
    r = _request_with_retries("GET", WIKIDATA_SPARQL,
                              params={"query": q, "format":"json"},
                              headers=headers, timeout=30, retries=4)
    data = r.json()
    out = {}
    for b in data.get("results", {}).get("bindings", []):
        qid = b["item"]["value"].split("/")[-1]
        typ = b.get("type", {}).get("value")
        typ = typ.split("/")[-1] if typ else None
        out.setdefault(qid, [])
        if typ: out[qid].append(typ)
    return out

# ----------------------------- DISAMBIGUAZIONE -----------------------------
def score_candidate(term: str, cand: Dict, term_vec=None, cand_vec=None) -> float:
    """
    Combina:
    - similarità fuzzy (token_sort_ratio)
    - semantica (cosine embeddings)
    - bonus se lingua/descrizione matchano bene
    """
    s1 = fuzz.token_sort_ratio(term, (cand.get("label") or ""), score_cutoff=0) / 100.0
    s2 = fuzz.partial_ratio(term, (cand.get("description") or ""), score_cutoff=0) / 100.0
    s3 = 0.0
    if term_vec is not None and cand_vec is not None:
        s3 = float(util.cos_sim(term_vec, cand_vec))
    # pesi: semantica > label > descrizione
    return 0.5*s3 + 0.35*s1 + 0.15*s2

def embed_texts(texts: List[str]):
    enc = get_encoder()
    return enc.encode(texts, convert_to_tensor=True, normalize_embeddings=True)

def disambiguate(term: str, candidates: List[Dict]) -> Optional[Dict]:
    if not candidates:
        return None
    # prepara testi per embedding: label + descrizione
    term_vec = embed_texts([term])[0]
    cand_texts = []
    for c in candidates:
        t = c.get("label") or ""
        d = c.get("description") or ""
        cand_texts.append((t + " — " + d).strip(" —"))
    cand_vecs = embed_texts(cand_texts)
    scores = []
    for idx, c in enumerate(candidates):
        s = score_candidate(term, c, term_vec, cand_vecs[idx])
        scores.append((s, c))
    scores.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scores[0]
    best["__score"] = float(best_score)
    return best

# ----------------------------- PIPELINE PRINCIPALE -----------------------------
def link_text_to_wikidata(
    text: str,
    lang: str = "it",
    allow_types: Optional[Iterable[str]] = None,
    min_score: float = 0.45,
    hard_limit_per_term: int = 7,
) -> List[Dict]:
    """
    Ritorna [{term, qid, label, description, url, score}]
    - allow_types: opzionale lista di QID (P31) ammessi (filtra dominio)
    """
    terms = extract_candidates(text)
    out = []
    for t in terms:
        try:
            cands = wikidata_search(t, lang=lang, limit=hard_limit_per_term)
            # filtro per tipo, se richiesto (richiede un colpo SPARQL batch, ottimizziamo dopo)
            if allow_types and cands:
                types_map = wikidata_fetch_types([c["id"] for c in cands])
                cands = [c for c in cands if any(q in set(types_map.get(c["id"], [])) for q in allow_types)]
            best = disambiguate(t, cands)
            if best and best.get("__score", 0) >= min_score:
                out.append({
                    "term": t,
                    "qid": best["id"],
                    "label": best.get("label"),
                    "description": best.get("description"),
                    "url": best.get("url") or (WIKIDATA_ENTITY + best["id"]),
                    "score": round(best["__score"], 3),
                    "source": "Wikidata"
                })
        except Exception as ex:
            logging.exception(f"Errore linking per termine '{t}': {ex}")
            continue
        # throttling  per Wikimedia (~1 req/sec). La cache riduce i colpi successivi.
        time.sleep(1.0)
    # dedup per QID, tieni il migliore
    best_per_q = {}
    for it in out:
        key = (it["qid"])
        if key not in best_per_q or it["score"] > best_per_q[key]["score"]:
            best_per_q[key] = it
    return sorted(best_per_q.values(), key=lambda x: x["score"], reverse=True)

# ----------------------------- OPZIONALE: DBpedia Spotlight -----------------------------
def spotlight_it(text: str, conf: float = 0.5, supp: int = 0, *,
                 timeout: int = 20, retries: int = 3) -> List[Dict]:
    """
    Ritorna entità annotate da DBpedia Spotlight (IT).
    Output minimale: surfaceForm, URI DBpedia.
    """
    if not text or not text.strip():
        return []

    # Spotlight tende a fallire con testi enormi: taglia a ~50k
    if len(text) > 50000:
        text = text[:50000]

    headers = {
        "Accept": "application/json",
        "User-Agent": _UA
    }
    params = {"confidence": conf, "support": supp}

    last_exc = None
    for attempt in range(retries):
        try:
            # POST corretto: form field 'text'
            r = _SESSION.post(
                DBP_SPOTLIGHT_IT,
                headers=headers,
                params={**params, "origin": "*"},
                data={"text": text},
                timeout=timeout,
            )
            # Se il server rifiuta il content-type/accetta solo GET
            if r.status_code in (400, 406, 415):
                r = _SESSION.get(
                    DBP_SPOTLIGHT_IT,
                    headers=headers,
                    params={**params, "text": text, "origin": "*"},
                    timeout=timeout,
                )

            r.raise_for_status()
            data = r.json() if r.content else {}
            res = []
            for r_ in (data.get("Resources") or []):
                res.append({
                    "term": r_.get("@surfaceForm"),
                    "url": r_.get("@URI"),
                    "source": "DBpedia Spotlight",
                    "types": r_.get("@types", ""),
                    "offset": int(r_.get("@offset", -1)) if r_.get("@offset") else None,
                })
            return res

        except requests.HTTPError as e:
            last_exc = e
            # backoff su rate limit / temporanei
            status = getattr(e.response, "status_code", None)
            if status in (429, 500, 502, 503, 504):
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            time.sleep(0.5 * (2 ** attempt))
            continue

    if last_exc:
        raise last_exc
    return []

# ----------------------------- OPZIONALE: GeoNames (serve username) -----------------------------
def geonames_search(name: str, username: str, max_rows: int = 5, *, strict: bool = True) -> List[Dict]:
    """
    Cerca SOLO città/località popolate italiane su GeoNames e restituisce Country/Latitude/Longitude.
    - strict=True usa name_equals per match esatto; False usa q (ricerca più ampia).
    """
    if not username or not username.strip():
        raise ValueError("GeoNames: username mancante.")

    url = "https://secure.geonames.org/searchJSON"
    params = {
        "username": username,
        "lang": "it",
        "maxRows": max_rows,
        "orderby": "population",
        "featureClass": "P",   # solo 'populated place'
        "country": "IT",       # solo Italia
        "style": "FULL",
        "isNameRequired": True
    }
    if strict:
        params["name_equals"] = name
    else:
        params["q"] = name

    headers = {"User-Agent": _UA, "Accept": "application/json"}

    r = _request_with_retries(
        "GET", url, params=params, headers=headers, timeout=15, retries=3, add_origin=False
    )
    j = r.json()

    out = []
    for g in j.get("geonames", []):
        # doppia garanzia lato risposta
        if g.get("countryCode") != "IT":
            continue
        out.append({
            "term": name,
            "geonameId": g.get("geonameId"),
            "label": g.get("name"),
            "country": g.get("countryName"),
            "latitude": float(g["lat"]) if g.get("lat") else None,
            "longitude": float(g["lng"]) if g.get("lng") else None,
            "url": f"https://www.geonames.org/{g.get('geonameId')}",
            "source": "GeoNames"
        })
    return out

#Util che prende i termini già estratti da extract_candidates e prova GeoNames solo su quelli che hanno l’aria di essere toponimi (prima lettera maiuscola nel testo originale, o più parole). Usa strict=True e fallback soft se non trova nulla:
def geonames_cities_from_text(text: str, username: str, max_rows_per_term: int = 3) -> List[Dict]:
    terms = extract_candidates(text)
    found = []
    for t in terms:
        # euristiche semplici per ridurre il rumore
        if len(t) < 3:
            continue
        # prova match esatto
        rows = geonames_search(t, username=username, max_rows=max_rows_per_term, strict=True)
        if not rows:
            # fallback "soft" se non c'è match esatto
            rows = geonames_search(t, username=username, max_rows=1, strict=False)
        found.extend(rows)
        time.sleep(0.35)  # cortesia API
    # dedup per geonameId
    best = {}
    for r in found:
        gid = r["geonameId"]
        if gid not in best:
            best[gid] = r
    return list(best.values())