"""
Utility LOD: normalizzazione sorgenti, costruzione URI canonici e mapping keyword→LOD.
"""
from __future__ import annotations
from typing import List, Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # solo per type checking; nessuna dipendenza a runtime
    from models import ProjectIn, StakeholderIn


def _norm_source(s: Optional[str]) -> Optional[str]:
    """Normalizza il nome della sorgente LOD su valori canonici.

    Args:
        s: Stringa sorgente (es. "wikidata", "DBPedia", "geo names") o None.

    Returns:
        La forma canonica: "Wikidata", "DBpedia Spotlight", "GeoNames" oppure
        la stringa ripulita originale se non riconosciuta. Restituisce None se `s` è falsy.

    Esempi:
        >>> _norm_source(" wikidata ")
        'Wikidata'
        >>> _norm_source("Geo Names")
        'GeoNames'
        >>> _norm_source(None) is None
        True
    """
    if not s:
        return None
    t = s.strip().lower()
    if "wikidata" in t:
        return "Wikidata"
    if "dbpedia" in t:
        return "DBpedia Spotlight"
    if "geonames" in t or "geo names" in t:
        return "GeoNames"
    return s.strip()


def _canon_uri_from_item(item: Dict[str, Any]) -> Optional[str]:
    """Deriva un URI canonico per una risorsa LOD a partire da un dizionario item.

    L'item può contenere:
      - "link" o "url": se presenti e non vuoti sono usati direttamente.
      - "source" e "qid": per comporre URI Wikidata o GeoNames.
        Per Wikidata, un `qid` privo del prefisso 'Q' verrà normalizzato.
        Per DBpedia è richiesto l'URL esplicito (ritorna None se assente).

    Args:
        item: Dizionario che rappresenta un collegamento LOD.

    Returns:
        URI canonica o None se non ricavabile.
    """
    link = (item.get("link") or item.get("url") or "").strip()
    if link:
        return link

    src = _norm_source(item.get("source"))
    qid = (item.get("qid") or "").strip()

    if src == "Wikidata" and qid:
        q = qid if qid.upper().startswith("Q") else f"Q{qid}"
        return f"https://www.wikidata.org/entity/{q}"

    if src == "GeoNames" and qid:
        return f"https://www.geonames.org/{qid}"

    # DBpedia richiede l'URL esplicito
    return None


def _match_keyword_name(candidate: Optional[str], payload_keywords: List[str]) -> Optional[str]:
    """Trova nel payload la keyword che corrisponde a ``candidate`` (case-insensitive).

    Args:
        candidate: Etichetta/termine da cercare nel payload.
        payload_keywords: Elenco di keyword dell'oggetto (case-insensitive match).

    Returns:
        La keyword esistente nel payload con il suo casing originale, se trovata.
        Se non esiste, restituisce `candidate` ripulita.
        Restituisce None se `candidate` è vuoto o solo spazi.
    """
    if not candidate:
        return None
    cand = candidate.strip()
    if not cand:
        return None
    lut = {k.casefold(): k for k in (payload_keywords or [])}
    return lut.get(cand.casefold(), cand)  # ritorna il nome payload se esiste, altrimenti cand


def build_kw_lod_rows_from_project_payload(data: "ProjectIn") -> List[Dict[str, Any]]:
    """
    Converte ProjectIn → righe per collegare Keyword → LODEntity.
    Formato output: [{keyword, uri, source, label}].

    Usa SOLO `lod_links`, perché lì abbiamo term/label → keyword.
    Le liste riassuntive (`wikidata_qids`, ecc.) non contengono info di keyword,
    quindi qui vengono ignorate per evitare associazioni arbitrarie.

    Args:
        data: Istanza di `ProjectIn` con `keywords` e `lod_links`.

    Returns:
        Lista di dizionari con chiavi: keyword, uri, source, label.
    """
    rows: List[Dict[str, Any]] = []
    payload_kws = data.keywords or []

    for r in (data.lod_links or []):
        d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
        uri = _canon_uri_from_item(d)
        if not uri:
            continue

        kw_name = _match_keyword_name(d.get("term") or d.get("label"), payload_kws)
        if not kw_name:
            continue

        rows.append({
            "keyword": kw_name,
            "uri": uri,
            "source": _norm_source(d.get("source")),
            "label": (d.get("label") or d.get("term") or None),
        })

    # dedup per (keyword, uri)
    out, seen = [], set()
    for r in rows:
        sig = (r["keyword"], r["uri"])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
    return out


def build_kw_lod_rows_from_stakeholder_payload(data: "StakeholderIn") -> List[Dict[str, Any]]:
    """
    Converte StakeholderIn → righe per collegare Keyword → LODEntity.
    Assume `data.keywords` come elenco delle keyword proprie dello stakeholder.

    Args:
        data: Istanza di `StakeholderIn` con `keywords` e `lod_links`.

    Returns:
        Lista di dizionari con chiavi: keyword, uri, source, label.
    """
    rows: List[Dict[str, Any]] = []
    payload_kws = data.keywords or []

    for r in (getattr(data, "lod_links", []) or []):
        d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
        uri = _canon_uri_from_item(d)
        if not uri:
            continue

        kw_name = _match_keyword_name(d.get("term") or d.get("label"), payload_kws)
        if not kw_name:
            continue

        rows.append({
            "keyword": kw_name,
            "uri": uri,
            "source": _norm_source(d.get("source")),
            "label": (d.get("label") or d.get("term") or None),
        })

    # dedup per (keyword, uri)
    out, seen = [], set()
    for r in rows:
        sig = (r["keyword"], r["uri"])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
    return out