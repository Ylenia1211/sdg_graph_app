"""API Flask per gestione Knowledge Graph (Stakeholder/Project/Keyword/SDG) e
pipeline di Link Prediction con Neo4j GDS.

Note:
    Alcune funzioni usano "duck typing" sui risultati delle query Neo4j
    (liste di dict). Gli hint riflettono questa scelta.
"""
from __future__ import annotations

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from neo4j_client import run_cypher
from models import StakeholderIn, ProjectIn
from lod import wikidata_qids, wikidata_summary_and_image, dbpedia_resource
import csv, io, json
from typing import Tuple, Dict, Any, List, Optional
from pydantic import ValidationError
import traceback
from helpers_lod import build_kw_lod_rows_from_project_payload, build_kw_lod_rows_from_stakeholder_payload
import re
# --- Helpers JSON-safe -------------------------------------------------
from datetime import date, datetime, time
from decimal import Decimal
import os

app = Flask(__name__)
CORS(app)

# Namespace confermati nel tuo ambiente
LP_BETA = "gds.beta.pipeline.linkPrediction"
LP_ALPHA = "gds.alpha.pipeline.linkPrediction"



PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL_DIR = os.path.join(PROJECT_ROOT, "models_gds")
os.makedirs(DEFAULT_MODEL_DIR, exist_ok=True)


def _to_json_safe(x: Any) -> Any:
    """Converte oggetti Python/Neo4j in tipi JSON-serializzabili.

    Args:
        x: Valore arbitrario (può includere tipi Neo4j/py2neo).

    Returns:
        Un valore compatibile con JSON (dict/list/str/float/int/bool/None).

    Esempi:
        >>> _to_json_safe(datetime(2020,1,1)).startswith("2020-01-01")
        True
        >>> _to_json_safe(Decimal("1.2"))
        1.2
    """
    # tipi base già ok
    if x is None or isinstance(x, (bool, int, float, str)):
        return x
    # datetime / date / time -> ISO8601
    if isinstance(x, (datetime, date, time)):
        try:
            return x.isoformat()
        except Exception:
            return str(x)
    # Decimal -> float (o str se preferisci)
    if isinstance(x, Decimal):
        return float(x)
    # set/tuple -> lista
    if isinstance(x, (set, tuple)):
        return [_to_json_safe(v) for v in x]
    # lista
    if isinstance(x, list):
        return [_to_json_safe(v) for v in x]
    # dict/py2neo/neo4j map-like
    if isinstance(x, dict):
        return {str(k): _to_json_safe(v) for k, v in x.items()}
    # fallback: rappresentazione stringa
    return str(x)


# ---------- GDS namespace detection ----------
def _gds_ns_candidates(feature: str) -> list[str]:
    """Ritorna namespace GDS disponibili che espongono una feature.

    Args:
        feature: Suffisso procedura, es. "model.list".

    Returns:
        Lista ordinata di namespace trovati, es. ["gds", "gds.beta"].
    """
    try:
        rows = run_cypher("SHOW PROCEDURES YIELD name RETURN collect(name) AS names", {})
        names = set((rows[0].get("names") or []))
    except Exception:
        # fallback legacy
        try:
            rows = run_cypher("CALL dbms.procedures() YIELD name RETURN collect(name) AS names", {})
            names = set((rows[0].get("names") or []))
        except Exception:
            names = set()

    candidates: list[str] = []
    for prefix in ("gds", "gds.beta", "gds.alpha"):
        proc = f"{prefix}.{feature}"
        if proc in names:
            candidates.append(prefix)
    return candidates


def _first_ns_or_raise(suffix: str) -> str:
    """Trova il primo namespace (gds/beta/alpha) che espone la procedura richiesta.

    Args:
        suffix: Parte finale del nome, es. "model.store".

    Returns:
        Nome namespace ("gds", "gds.beta" o "gds.alpha").

    Raises:
        RuntimeError: Se nessun namespace espone la procedura.
    """
    # Se l'hai già definita, tieni la tua. Altrimenti questa prova GA → beta → alpha.
    for ns in ("gds", "gds.beta", "gds.alpha"):
        try:
            rows = run_cypher("""
            SHOW PROCEDURES YIELD name
            WHERE name = $full
            RETURN count(*) AS c
            """, {"full": f"{ns}.{suffix}"})
            if rows and rows[0].get("c", 0) > 0:
                return ns
        except Exception:
            pass
    raise RuntimeError(f"Nessun namespace trovato per '{suffix}'")


def _model_list() -> List[Dict[str, Any]]:
    """Elenca modelli nel Model Catalog (senza YIELD espliciti).

    Returns:
        Lista grezza di record del catalog.
    """
    ns = _first_ns_or_raise("model.list")
    cy = f"CALL {ns}.model.list()"
    return run_cypher(cy, {})


def _model_exists(model_name: str) -> bool:
    """Verifica se un modello esiste nel catalog.

    Args:
        model_name: Nome del modello.

    Returns:
        True se il modello è presente, altrimenti False.
    """
    try:
        rows = _model_list()
        names = {r["name"] for r in (rows or []) if "name" in r}
        return model_name in names
    except Exception:
        # ultima spiaggia: prova ogni ns disponibile
        for ns in _gds_ns_candidates("model.list"):
            try:
                rows = run_cypher(f"CALL {ns}.model.list() YIELD name RETURN name", {})
                names = {r["name"] for r in (rows or []) if "name" in r}
                if model_name in names:
                    return True
            except Exception:
                continue
        return False


def _model_store(model_name: str, location: Optional[str] = None) -> Dict[str, Any]:
    """Esegue `gds.model.store` senza affidarsi a YIELD, con fallback di firma.

    Tenta:
      1) `CALL <ns>.model.store(name, path)` (se `location` è fornita)
      2) `CALL <ns>.model.store(name)`

    Args:
        model_name: Nome del modello da salvare.
        location: Percorso base (opzionale) dove scrivere il modello.

    Returns:
        Dizionario informativo, es. {"stored": True, "namespace": "...", "location": "..."}.

    Raises:
        RuntimeError: Se mancano configurazioni richieste (es. store_location).
        Exception: Propaga l'ultimo errore se tutti i tentativi falliscono.
    """
    ns = _first_ns_or_raise("model.store")
    cy_list: List[str] = []
    params: Dict[str, Any] = {"name": model_name}

    if location:
        cy_list.append(f"CALL {ns}.model.store($name, $path)")
        params["path"] = location
    # fallback: solo il nome
    cy_list.append(f"CALL {ns}.model.store($name)")

    last_err: Optional[Exception] = None
    for cy in cy_list:
        try:
            run_cypher(cy, params)
            return {"stored": True, "namespace": ns, "location": location}
        except Exception as e:
            last_err = e
            continue

    # se fallisce per path non configurato, aggiungi hint esplicito
    msg = str(last_err)
    if "store_location" in msg or "path" in msg.lower():
        raise RuntimeError(
            "Model store fallito: configura 'gds.model.store_location' in neo4j.conf "
            "oppure passa un 'location' valido all'endpoint."
        )
    raise last_err  # type: ignore[misc, arg-type]


def _model_load(model_name: str) -> Dict[str, Any]:
    """Carica un modello precedentemente salvato nel catalog in-memory.

    Args:
        model_name: Nome del modello.

    Returns:
        Primo record restituito da `model.load` o {"loaded": model_name} se non ci sono righe.
    """
    ns = _first_ns_or_raise("model.load")
    cy = f"CALL {ns}.model.load($name) YIELD * RETURN *"
    rows = run_cypher(cy, {"name": model_name})
    return rows[0] if rows else {"loaded": model_name}


def _model_try(cy_list: List[str], params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Esegue in sequenza più statement Cypher, restituendo il primo che va a buon fine.

    Args:
        cy_list: Lista di statement Cypher da tentare in ordine.
        params: Parametri opzionali.

    Returns:
        Risultato della prima chiamata che non solleva eccezioni.

    Raises:
        Exception: L'ultima eccezione sollevata se nessuno statement riesce.
    """
    last_err: Optional[Exception] = None
    for cy in cy_list:
        try:
            return run_cypher(cy, params or {})
        except Exception as e:
            last_err = e
    raise last_err  # type: ignore[misc, arg-type]


def _model_drop(name: str) -> List[Dict[str, Any]]:
    """Rimuove un modello dal catalog, provando GA → beta → alpha.

    Args:
        name: Nome del modello.

    Returns:
        Risultato della chiamata che ha avuto successo.
    """
    return _model_try([
        "CALL gds.model.drop($name)",
        "CALL gds.beta.model.drop($name)",
        "CALL gds.alpha.model.drop($name)",
    ], {"name": name})


# Lista modelli presenti nel Model Catalog (in-memory)
@app.get("/lp/model/list")
def lp_model_list():
    """Ritorna l'elenco dei modelli nel catalog GDS (JSON-safe, ordinati se possibile)."""
    try:
        rows = _model_list() or []
        # normalizza in JSON-safe
        safe_rows = [_to_json_safe(r) for r in rows]

        # ordina se c'è un campo name/modelName
        key = None
        if safe_rows:
            if "name" in safe_rows[0]:
                key = "name"
            elif "modelName" in safe_rows[0]:
                key = "modelName"
        if key:
            safe_rows = sorted(safe_rows, key=lambda r: str(r.get(key)))

        return jsonify({"models": safe_rows})
    except Exception as e:
        return jsonify({"error": "model_list_failed", "detail": str(e)}), 500
    

# Verifica se un modello esiste in catalog
@app.get("/lp/model/exists")
def lp_model_exists():
    """Verifica presenza modello nel catalog.

    Query:
        name: Nome del modello (default: "lp-pipeline-model").
    """
    name = request.args.get("name", "lp-pipeline-model")
    return jsonify({"name": name, "exists": _model_exists(name)})


def _try_call(cy_list: List[str], params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Prova una lista di chiamate Cypher in ordine finché una va a buon fine.

    Args:
        cy_list: Statement Cypher alternativi.
        params: Parametri opzionali.

    Returns:
        Risultato della prima chiamata riuscita.

    Raises:
        Exception: L'ultima eccezione se nessuna chiamata riesce.
    """
    last_err: Optional[Exception] = None
    for cy in cy_list:
        try:
            return run_cypher(cy, params or {})
        except Exception as e:
            last_err = e
    raise last_err  # type: ignore[misc, arg-type]


@app.post("/lp/model/store")
def lp_model_store():
    """Salva un modello nel filesystem (diverse firme supportate)."""
    payload = request.get_json(force=True) or {}
    model_name = payload.get("modelName")
    location = payload.get("location") or DEFAULT_MODEL_DIR  # default cartella progetto

    if not model_name:
        return jsonify({"error": "missing_modelName"}), 400

    try:
        # Prova firme GA con opzioni in mappa (varie chiavi supportate a seconda versione)
        _try_call([
            "CALL gds.model.store($name, {path: $loc})",
            "CALL gds.model.store($name, {location: $loc})",
            "CALL gds.model.store($name, {basePath: $loc})",
            # Fallback alpha: richiede gds.model.store_location in neo4j.conf
            "CALL gds.alpha.model.store($name)"
        ], {"name": model_name, "loc": location})

        return jsonify({"stored": True, "modelName": model_name, "location": location})
    except Exception as e:
        return jsonify({
            "error": "model_store_failed",
            "detail": str(e),
            "hint": f"Se il fallback alpha è stato usato, serve configurare 'gds.model.store_location' in neo4j.conf. "
                    f"Altrimenti verifica che Neo4j possa scrivere su: {location}"
        }), 500
    

@app.post("/lp/model/load")
def lp_model_load():
    """Carica da disco un modello nel catalogo in-memory.

    Body JSON:
        - modelName (str) obbligatorio
        - location (str) opzionale, default alla cartella di progetto
    """
    payload = request.get_json(force=True) or {}
    model_name = payload.get("modelName")
    location = payload.get("location") or DEFAULT_MODEL_DIR

    if not model_name:
        return jsonify({"error": "missing_modelName"}), 400

    try:
        _try_call([
            "CALL gds.model.load($name, {path: $loc})",
            "CALL gds.model.load($name, {location: $loc})",
            "CALL gds.model.load($name, {basePath: $loc})",
            # Fallback alpha: usa store_location configurato
            "CALL gds.alpha.model.load($name)"
        ], {"name": model_name, "loc": location})

        return jsonify({"loaded": True, "modelName": model_name, "location": location})
    except Exception as e:
        return jsonify({"error": "model_load_failed", "detail": str(e)}), 500
    

@app.post("/lp/model/drop")
def lp_model_drop():
    """Droppa un modello dal catalogo in-memory."""
    payload = request.get_json(force=True) or {}
    name = payload.get("name", "lp-pipeline-model")
    try:
        _model_drop(name)
        return jsonify({"name": name, "dropped": True})
    except Exception as e:
        return jsonify({"error": "model_drop_failed", "detail": str(e)}), 500
    

def _lp_call(base: str, payload: str = "") -> List[Dict[str, Any]]:
    """Esegue una procedura di pipeline LP provando GA → beta → alpha.

    Args:
        base: Nome base della procedura (es. 'create', 'train').
        payload: Stringa argomenti già formattata per Cypher.

    Returns:
        Risultato della prima chiamata riuscita.
    """
    cys = [
        f"CALL gds.pipeline.linkPrediction.{base}({payload})",
        f"CALL gds.beta.pipeline.linkPrediction.{base}({payload})",
        f"CALL gds.alpha.pipeline.linkPrediction.{base}({payload})",
    ]
    return _try_call(cys)


def _lp_train_estimate(graph: str, pipeline: str, model: str, target: str) -> List[Dict[str, Any]]:
    """Wrapper per stima memoria di training LP."""
    return _lp_call(
        "train.estimate",
        f"'{graph}', {{ pipeline: '{pipeline}', modelName: '{model}', targetRelationshipType: '{target}' }}"
    )


def _lp_train(graph: str, pipeline: str, model: str, target: str) -> List[Dict[str, Any]]:
    """Wrapper per train LP."""
    return _lp_call(
        "train",
        f"'{graph}', {{ pipeline: '{pipeline}', modelName: '{model}', targetRelationshipType: '{target}', metrics: ['AUCPR'], randomSeed: 42 }}"
    )



@app.get("/stakeholders_flat")
def stakeholders_flat():
    """Ritorna stakeholder con settore e keywords in forma tabellare (flat)."""
    cy = """
    MATCH (s:Stakeholder)
    OPTIONAL MATCH (s)-[:hasKeyword]->(k:Keyword)
    OPTIONAL MATCH (s)-[:IN_SECTOR]->(sec:Sector)
    RETURN s.id AS id,
           s.name AS name,
           coalesce(sec.name, null) AS sector,
           collect(DISTINCT k.name) AS keywords
    ORDER BY name
    """
    rows = run_cypher(cy, {})
    return {"stakeholders": rows}


@app.get("/keywords/summary")
def keywords_summary():
    """Restituisce conteggi per keyword (stakeholder e progetti)."""
    cy_stk = """
    MATCH (:Stakeholder)-[:hasKeyword]->(k:Keyword)
    RETURN k.name AS keyword, count(*) AS n
    ORDER BY n DESC, keyword
    """
    cy_prj = """
    MATCH (:Project)-[:relatedToKeyword]->(k:Keyword)
    RETURN k.name AS keyword, count(*) AS n
    ORDER BY n DESC, keyword
    """
    return {
        "stakeholders_by_keyword": run_cypher(cy_stk, {}),
        "projects_by_keyword":     run_cypher(cy_prj, {})
    }


@app.get("/graph")
def graph():
    """Costruisce un sub-grafo (nodi/archi) con filtri e (opzionale) shortest path.

    Query string:
        sdg: codice SDG per filtrare progetti.
        sector: nome settore per filtrare stakeholder.
        q: stringa full-text su nomi e keyword.
        stakeholder_type: filtro sul tipo di stakeholder.
        node_types: subset di nodi da includere (Stakeholder,Project,Keyword,SDG).
        path_src/path_tgt: id completi (prefisso:valore) per evidenziare shortest path.
        path_max: massimo numero di hop (default 6).
    """
    # --- helpers locali ---
    def _list_param(req, name):
        vals = req.args.getlist(name)
        if not vals:
            v = req.args.get(name)
            if v:
                vals = [x.strip() for x in v.split(",") if x.strip()]
        return vals

    def _id_to_match(alias: str, idstr: str, paramname: str) -> Tuple[str, Dict[str, str]]:
        try:
            label, raw = idstr.split(":", 1)
        except ValueError:
            raise ValueError(f"id non valido: {idstr}")

        if label == "stakeholder":
            return f"({alias}:Stakeholder {{id:${paramname}}})", {paramname: raw}
        if label == "project":
            return f"({alias}:Project {{id:${paramname}}})", {paramname: raw}
        if label == "keyword":
            return f"({alias}:Keyword {{name:${paramname}}})", {paramname: raw}
        if label == "sdg":
            return f"({alias}:SDG {{code:${paramname}}})", {paramname: raw}
        if label == "sector":
            return f"({alias}:Sector {{name:${paramname}}})", {paramname: raw}
        if label == "lod":
            return f"({alias}:LODEntity {{uri:${paramname}}})", {paramname: raw}
        raise ValueError(f"prefisso non supportato: {label}")

    # --- filtri base ---
    sdg = request.args.get("sdg")
    sector = request.args.get("sector")
    q = request.args.get("q")
    st_type = request.args.get("stakeholder_type")
    node_types = set([t for t in _list_param(request, "node_types")])  # Stakeholder,Project,Keyword,SDG

    # --- path (opzionale) ---
    path_src = request.args.get("path_src")
    path_tgt = request.args.get("path_tgt")
    try:
        path_max = int(request.args.get("path_max", "6"))
    except Exception:
        path_max = 6

    params, where_parts = {}, []

    if sdg:
        where_parts.append(" (p)-[:contributesTo]->(:SDG {code:$sdg}) ")
        params["sdg"] = sdg
    if sector:
        where_parts.append(" (s)-[:IN_SECTOR]->(:Sector {name:$sector}) ")
        params["sector"] = sector
    if st_type:
        where_parts.append(" toLower(s.type) = toLower($stype) ")
        params["stype"] = st_type
    if q:
        where_parts.append("""
            toLower(s.name) CONTAINS toLower($q) OR
            toLower(p.name) CONTAINS toLower($q) OR
            EXISTS { MATCH (p)-[:relatedToKeyword]->(k:Keyword) WHERE toLower(k.name) CONTAINS toLower($q) } OR
            EXISTS { MATCH (s)-[:hasKeyword]->(k2:Keyword) WHERE toLower(k2.name) CONTAINS toLower($q) }
        """)
        params["q"] = q

    where = f"WHERE {' AND '.join([f'({x})' for x in where_parts])}" if where_parts else ""

    # --- sub-grafo Stakeholder <-> Project ---
    cypher = f"""
    MATCH (s:Stakeholder)-[r:participatesIn]->(p:Project)
    {where}
    OPTIONAL MATCH (p)-[:contributesTo]->(g:SDG)
    OPTIONAL MATCH (s)-[:IN_SECTOR]->(sec:Sector)
    RETURN s{{.*, label:'Stakeholder', ntype:'Stakeholder'}} AS s,
           p{{.*, label:'Project', ntype:'Project'}} AS p,
           collect(DISTINCT g.code) AS sdgs,
           sec.name AS sector,
           type(r) AS rel
    LIMIT 1200
    """
    try:
        rows = run_cypher(cypher, params)
    except Exception as e:
        return jsonify({"error": "cypher_failed", "detail": str(e), "phase": "base_graph"}), 500

    nodes: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    for row in rows or []:
        s = row.get("s"); p = row.get("p")
        if not s or not p:
            continue
        s_id = f"stakeholder:{s.get('id')}"
        p_id = f"project:{p.get('id')}"
        if not node_types or "Stakeholder" in node_types:
            nodes[s_id] = {"data": {"id": s_id, "label": s.get("name"), "type": "Stakeholder",
                                    "sector": row.get("sector"), "stype": s.get("type")}}
        if not node_types or "Project" in node_types:
            nodes[p_id] = {"data": {"id": p_id, "label": p.get("name"), "type": "Project",
                                    "sdgs": row.get("sdgs")}}
        edges.append({"data": {"source": s_id, "target": p_id, "label": row.get("rel", "participatesIn")}})

    # --- arricchisci con Keyword ---
    if not node_types or "Keyword" in node_types:
        try:
            krows = run_cypher("""
            MATCH (p:Project)-[:relatedToKeyword]->(k:Keyword)
            RETURN DISTINCT p.id AS pid, k.name AS k
            """)
        except Exception as e:
            return jsonify({"error": "cypher_failed", "detail": str(e), "phase": "keywords"}), 500

        for r in krows or []:
            p_id = f"project:{r.get('pid')}"
            kname = r.get("k")
            if not p_id or not kname:
                continue
            k_id = f"keyword:{kname}"
            if p_id in nodes:
                nodes[k_id] = nodes.get(k_id, {"data": {"id": k_id, "label": kname, "type": "Keyword"}})
                edges.append({"data": {"source": p_id, "target": k_id, "label": "relatedToKeyword"}})

    # --- arricchisci con SDG ---
    if not node_types or "SDG" in node_types:
        try:
            grows = run_cypher("""
            MATCH (p:Project)-[:contributesTo]->(g:SDG)
            RETURN DISTINCT p.id AS pid, g.code AS code
            """)
        except Exception as e:
            return jsonify({"error": "cypher_failed", "detail": str(e), "phase": "sdg"}), 500

        for r in grows or []:
            p_id = f"project:{r.get('pid')}"
            code = r.get("code")
            if not p_id or not code:
                continue
            g_id = f"sdg:{code}"
            if p_id in nodes:
                nodes[g_id] = nodes.get(g_id, {"data": {"id": g_id, "label": code, "type": "SDG"}})
                edges.append({"data": {"source": p_id, "target": g_id, "label": "contributesTo"}})

    # --- evidenzia shortest path (se richiesto) ---
    if path_src and path_tgt:
        try:
            s_clause, s_param = _id_to_match("s", path_src, "sid")
            t_clause, t_param = _id_to_match("t", path_tgt, "tid")
        except ValueError as e:
            return jsonify({"error": "bad_id", "detail": str(e)}), 400

        # clamp per sicurezza (Neo4j non accetta parametro su *..N)
        try:
            pm = int(path_max)
        except Exception:
            pm = 6
        pm = max(1, min(pm, 20))  # limiti ragionevoli

        rel_filter = "participatesIn|relatedToKeyword|contributesTo|hasKeyword|IN_SECTOR|linkedTo"

        cy_path = f"""
        MATCH {s_clause}, {t_clause}
        CALL {{
        WITH s, t
        OPTIONAL MATCH p = shortestPath( (s)-[:{rel_filter}*..{pm}]-(t) )
        RETURN p
        }}
        WITH s, t, p
        WITH (CASE WHEN p IS NULL THEN [s,t] ELSE nodes(p) END) AS ns,
            (CASE WHEN p IS NULL THEN []   ELSE relationships(p) END) AS rs
        UNWIND ns AS n
        WITH DISTINCT n, rs
        WITH
        CASE
            WHEN 'Stakeholder' IN labels(n) THEN {{id:'stakeholder:'+n.id, label:n.name, type:'Stakeholder'}}
            WHEN 'Project'     IN labels(n) THEN {{id:'project:'+n.id,     label:n.name, type:'Project'}}
            WHEN 'Keyword'     IN labels(n) THEN {{id:'keyword:'+n.name,   label:n.name, type:'Keyword'}}
            WHEN 'SDG'         IN labels(n) THEN {{id:'sdg:'+n.code,       label:n.code, type:'SDG'}}
            WHEN 'Sector'      IN labels(n) THEN {{id:'sector:'+n.name,    label:n.name, type:'Sector'}}
            WHEN 'LODEntity'   IN labels(n) THEN {{id:'lod:'+n.uri,        label:n.label, type:'LODEntity'}}
            ELSE {{id: toString(id(n)), label: coalesce(n.name, 'node'), type:'Node'}}
        END AS out_node,
        rs
        WITH collect(out_node) AS nodes_p, rs
        UNWIND rs AS r
        WITH nodes_p, {{
        source: CASE
            WHEN 'Stakeholder' IN labels(startNode(r)) THEN 'stakeholder:'+startNode(r).id
            WHEN 'Project'     IN labels(startNode(r)) THEN 'project:'+startNode(r).id
            WHEN 'Keyword'     IN labels(startNode(r)) THEN 'keyword:'+startNode(r).name
            WHEN 'SDG'         IN labels(startNode(r)) THEN 'sdg:'+startNode(r).code
            WHEN 'Sector'      IN labels(startNode(r)) THEN 'sector:'+startNode(r).name
            WHEN 'LODEntity'   IN labels(startNode(r)) THEN 'lod:'+startNode(r).uri
            ELSE toString(id(startNode(r)))
        END,
        target: CASE
            WHEN 'Stakeholder' IN labels(endNode(r)) THEN 'stakeholder:'+endNode(r).id
            WHEN 'Project'     IN labels(endNode(r)) THEN 'project:'+endNode(r).id
            WHEN 'Keyword'     IN labels(endNode(r)) THEN 'keyword:'+endNode(r).name
            WHEN 'SDG'         IN labels(endNode(r)) THEN 'sdg:'+endNode(r).code
            WHEN 'Sector'      IN labels(endNode(r)) THEN 'sector:'+endNode(r).name
            WHEN 'LODEntity'   IN labels(endNode(r)) THEN 'lod:'+endNode(r).uri
            ELSE toString(id(endNode(r)))
        END,
        type: type(r),
        highlight: true
        }} AS e_p
        WITH nodes_p, collect(e_p) AS edges_p
        RETURN nodes_p AS nodes, edges_p AS edges
        """

        try:
            prow = run_cypher(cy_path, {**s_param, **t_param})
        except Exception as e:
            return jsonify({"error": "cypher_failed", "detail": str(e), "phase": "shortest_path"}), 500

        if prow:
            path_nodes = prow[0].get("nodes") or []
            path_edges = prow[0].get("edges") or []
            for n in path_nodes:
                nid2 = n["id"]
                if nid2 in nodes:
                    nodes[nid2]["data"]["highlight"] = True
                else:
                    n_copy = dict(n); n_copy["highlight"] = True
                    nodes[nid2] = {"data": n_copy}
            for e in path_edges:
                edges.append({"data": {"source": e["source"], "target": e["target"],
                                    "label": e["type"], "highlight": True}})

    return jsonify({"elements": {"nodes": list(nodes.values()), "edges": edges}})


# ---------------- Export per Gephi (CSV) ----------------
@app.get("/export/csv")
def export_csv():
    """Esporta nodi e archi in JSON (shape adatta a CSV successivo)."""
    # Nodo/Arco CSV per import rapido in Gephi
    node_rows = run_cypher("""
    CALL {
      MATCH (s:Stakeholder) RETURN s.id AS id, s.name AS label, 'Stakeholder' AS type
      UNION ALL
      MATCH (p:Project)     RETURN p.id AS id, p.name AS label, 'Project' AS type
      UNION ALL
      MATCH (k:Keyword)     RETURN k.name AS id, k.name AS label, 'Keyword' AS type
      UNION ALL
      MATCH (g:SDG)         RETURN g.code AS id, g.code AS label, 'SDG' AS type
    }
    RETURN id, label, type
    """)
    edge_rows = run_cypher("""
    CALL {
      MATCH (s:Stakeholder)-[:participatesIn]->(p:Project)
      RETURN 'stakeholder:' + s.id AS source, 'project:' + p.id AS target, 'participatesIn' AS type
      UNION ALL
      MATCH (p:Project)-[:relatedToKeyword]->(k:Keyword)
      RETURN 'project:' + p.id AS source, 'keyword:' + k.name AS target, 'relatedToKeyword' AS type
      UNION ALL
      MATCH (p:Project)-[:contributesTo]->(g:SDG)
      RETURN 'project:' + p.id AS source, 'sdg:' + g.code AS target, 'contributesTo' AS type
      UNION ALL
      MATCH (s:Stakeholder)-[:hasKeyword]->(k:Keyword)
      RETURN 'stakeholder:' + s.id AS source, 'keyword:' + k.name AS target, 'hasKeyword' AS type
    }
    RETURN source, target, type
    """)

    # pack in a zip-ish JSON o due CSV separati — qui restituisco JSON {nodes, edges}
    return jsonify({"nodes": node_rows, "edges": edge_rows})


@app.get("/export/nodes.csv")
def export_nodes_csv():
    """Esporta i nodi come CSV (id,label,type)."""
    rows = run_cypher("""
    CALL {
      MATCH (s:Stakeholder) RETURN 'stakeholder:' + s.id AS id, s.name AS label, 'Stakeholder' AS type
      UNION ALL
      MATCH (p:Project)     RETURN 'project:' + p.id AS id, p.name AS label, 'Project' AS type
      UNION ALL
      MATCH (k:Keyword)     RETURN 'keyword:' + k.name AS id, k.name AS label, 'Keyword' AS type
      UNION ALL
      MATCH (g:SDG)         RETURN 'sdg:' + g.code AS id, g.code AS label, 'SDG' AS type
    }
    RETURN id, label, type
    """)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["id","label","type"])
    writer.writeheader()
    for r in rows: writer.writerow(r)
    return Response(output.getvalue(), mimetype="text/csv")


@app.get("/export/edges.csv")
def export_edges_csv():
    """Esporta gli archi come CSV (source,target,type)."""
    rows = run_cypher("""
    CALL {
      MATCH (s:Stakeholder)-[:participatesIn]->(p:Project)
      RETURN 'stakeholder:' + s.id AS source, 'project:' + p.id AS target, 'participatesIn' AS type
      UNION ALL
      MATCH (p:Project)-[:relatedToKeyword]->(k:Keyword)
      RETURN 'project:' + p.id AS source, 'keyword:' + k.name AS target, 'relatedToKeyword' AS type
      UNION ALL
      MATCH (p:Project)-[:contributesTo]->(g:SDG)
      RETURN 'project:' + p.id AS source, 'sdg:' + g.code AS target, 'contributesTo' AS type
      UNION ALL
      MATCH (s:Stakeholder)-[:hasKeyword]->(k:Keyword)
      RETURN 'stakeholder:' + s.id AS source, 'keyword:' + k.name AS target, 'hasKeyword' AS type
    }
    RETURN source, target, type
    """)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["source","target","type"])
    writer.writeheader()
    for r in rows: writer.writerow(r)
    return Response(output.getvalue(), mimetype="text/csv")


# ---------------- Nodo (Dettaglio + LOD) ----------------
@app.get("/node/<path:node_id>")
def node_detail(node_id: str):
    """Dettaglio per nodo (stakeholder/project/keyword/sdg) con LOD best-effort.

    Args:
        node_id: Identificatore con prefisso (es. "project:my-id").
    """
    """
    node_id formati:
      - stakeholder:<id>
      - project:<id>
      - keyword:<name>
      - sdg:<code>
    """
    try:
        label, nid = node_id.split(":", 1)
    except ValueError:
        return jsonify({"error":"invalid id"}), 400

    if label == "stakeholder":
        q = """
        MATCH (s:Stakeholder {id:$id})
        OPTIONAL MATCH (s)-[:participatesIn]->(p:Project)
        OPTIONAL MATCH (s)-[:IN_SECTOR]->(sec:Sector)
        OPTIONAL MATCH (s)-[:hasKeyword]->(k:Keyword)
        RETURN s, collect(DISTINCT p) AS projects,
               sec.name AS sector, collect(DISTINCT k.name) AS keywords
        """
        data = run_cypher(q, {"id": nid})
        if not data: return jsonify({"error":"not found"}), 404
        node = data[0]
        name_for_lod = node["s"]["name"]

        # LOD via keyword collegate
        links = run_cypher("""
            MATCH (s:Stakeholder {id:$sid})-[:hasKeyword]->(k:Keyword)
            OPTIONAL MATCH (k)-[:linkedTo]->(e:LODEntity)
            RETURN collect(DISTINCT {keyword:k.name, entity:e{.*}}) AS lod_links
        """, {"sid": nid})

    elif label == "project":
        q = """
        MATCH (p:Project {id:$id})
        OPTIONAL MATCH (p)<-[:participatesIn]-(s:Stakeholder)
        OPTIONAL MATCH (p)-[:relatedToKeyword]->(k:Keyword)
        OPTIONAL MATCH (p)-[:contributesTo]->(g:SDG)
        RETURN p, collect(DISTINCT s) AS stakeholders,
               collect(DISTINCT k.name) AS keywords, collect(DISTINCT g.code) AS sdgs
        """
        data = run_cypher(q, {"id": nid})
        if not data: return jsonify({"error":"not found"}), 404
        node = data[0]
        name_for_lod = node["p"]["name"]

        links = run_cypher("""
            MATCH (p:Project {id:$pid})-[:relatedToKeyword]->(k:Keyword)
            OPTIONAL MATCH (k)-[:linkedTo]->(e:LODEntity)
            RETURN collect(DISTINCT {keyword:k.name, entity:e{.*}}) AS lod_links
        """, {"pid": nid})

    elif label == "keyword":
        q = """
        MATCH (k:Keyword {name:$name})
        OPTIONAL MATCH (p:Project)-[:relatedToKeyword]->(k)
        OPTIONAL MATCH (s:Stakeholder)-[:hasKeyword]->(k)
        OPTIONAL MATCH (k)-[:linkedTo]->(e:LODEntity)
        RETURN k,
               collect(DISTINCT p) AS projects,
               collect(DISTINCT s) AS stakeholders,
               collect(DISTINCT e{.*}) AS lod_entities
        """
        data = run_cypher(q, {"name": nid})
        if not data: return jsonify({"error":"not found"}), 404
        node = data[0]
        name_for_lod = node["k"]["name"]

        links = [{"keyword": nid, "entity": e} for e in node.get("lod_entities", [])]
        links = [{"lod_links": links}]  # uniform per gestione più sotto

    elif label == "sdg":
        q = """
        MATCH (g:SDG {code:$code})
        OPTIONAL MATCH (p:Project)-[:contributesTo]->(g)
        OPTIONAL MATCH (p)<-[:participatesIn]-(s:Stakeholder)
        RETURN g, collect(DISTINCT p) AS projects, collect(DISTINCT s) AS stakeholders
        """
        data = run_cypher(q, {"code": nid})
        if not data: return jsonify({"error":"not found"}), 404
        node = data[0]
        name_for_lod = node["g"]["code"]
        # Nessun LOD automatico definito per SDG (potresti collegare manualmente a entità esterne)

        links = [{"lod_links": []}]

    else:
        return jsonify({"error":"unsupported label"}), 400

    # LOD best-effort (Wikidata/DBpedia) con fallback
    lod: Dict[str, Any] = {}
    try:
        wd = wikidata_qids(name_for_lod, limit=1)
        if wd:
            qid = wd[0]["id"]
            wdinfo = wikidata_summary_and_image(qid)
            lod.update({
                "wikidata": f"https://www.wikidata.org/wiki/{qid}",
                "dbpedia": dbpedia_resource(name_for_lod),
                "summary": wdinfo.get("description"),
                "image": wdinfo.get("image")
            })
    except Exception:
        pass

    # Entità LOD collegate (da keyword etc.)
    lod["linkedEntities"] = (links[0]["lod_links"] if links else [])

    return jsonify({"node": node, "lod": lod, "label": label})


# ---------------- KPI / Dashboard ----------------
@app.get("/kpi")
def kpi():
    """KPI di riepilogo (per SDG e per settori, incluse matrici SDG×Settore/Keyword)."""
    # KPI per SDG (progetti + stakeholder per lo stesso SDG)
    rows = run_cypher("""
    MATCH (p:Project)-[:contributesTo]->(g:SDG)
    WITH g.code AS sdg, count(DISTINCT p) AS projects
    OPTIONAL MATCH (s:Stakeholder)-[:participatesIn]->(:Project)-[:contributesTo]->(g2:SDG)
    WHERE g2.code = sdg
    WITH sdg, projects, count(DISTINCT s) AS stakeholders
    RETURN sdg, projects, stakeholders
    ORDER BY sdg
    """)

    # Stakeholder per Settore
    by_sector_stake = run_cypher("""
    MATCH (s:Stakeholder)-[:IN_SECTOR]->(sec:Sector)
    RETURN sec.name AS sector, count(s) AS n
    ORDER BY n DESC
    LIMIT 30
    """)

    # Progetti per Settore (via stakeholder che partecipano al progetto)
    by_sector_project = run_cypher("""
    MATCH (p:Project)<-[:participatesIn]-(:Stakeholder)-[:IN_SECTOR]->(sec:Sector)
    RETURN sec.name AS sector, count(DISTINCT p) AS n
    ORDER BY n DESC
    LIMIT 30
    """)

    # 🔹 Matrice SDG × Settori (conteggio DISTINCT progetti)
    sdg_sector = run_cypher("""
    MATCH (sec:Sector)<-[:IN_SECTOR]-(:Stakeholder)-[:participatesIn]->(p:Project)-[:contributesTo]->(g:SDG)
    RETURN g.code AS sdg, sec.name AS sector, count(DISTINCT p) AS n
    ORDER BY sdg, sector
    """)

    # 🔹 SDG × Keyword (conteggio DISTINCT progetti)
    sdg_keyword = run_cypher("""
    MATCH (p:Project)-[:relatedToKeyword]->(k:Keyword)
    MATCH (p)-[:contributesTo]->(g:SDG)
    RETURN g.code AS sdg, k.name AS keyword, count(DISTINCT p) AS n
    ORDER BY sdg, n DESC, keyword
    """)

    return jsonify({
        "kpi": rows,
        "stake_by_sector": by_sector_stake,
        "proj_by_sector": by_sector_project,
        "sdg_sector": sdg_sector,        # per la heatmap
        "sdg_keyword": sdg_keyword,      # aggiunto
    })


#debug
@app.get("/sdg_keyword")
def sdg_keyword_table():
    """Tabella SDG×Keyword (conteggio DISTINCT progetti)."""
    rows = run_cypher("""
    MATCH (p:Project)-[:relatedToKeyword]->(k:Keyword)
    MATCH (p)-[:contributesTo]->(g:SDG)
    RETURN g.code AS sdg, k.name AS keyword, count(DISTINCT p) AS n
    ORDER BY sdg, n DESC, keyword
    """, {})
    return jsonify({"sdg_keyword": rows})


@app.get("/projects_flat")
def projects_flat():
    """Elenco progetti con keywords, settori (via stakeholder), SDG."""
    cy = """
    MATCH (p:Project)
    OPTIONAL MATCH (p)-[:relatedToKeyword]->(k:Keyword)
    OPTIONAL MATCH (p)<-[:participatesIn]-(s:Stakeholder)-[:IN_SECTOR]->(sec:Sector)
    OPTIONAL MATCH (p)-[:contributesTo]->(g:SDG)
    RETURN p.id AS id,
           p.name AS name,
           collect(DISTINCT k.name)   AS keywords,
           collect(DISTINCT sec.name) AS sectors,
           collect(DISTINCT g.code)   AS sdgs
    ORDER BY name
    """
    rows = run_cypher(cy, {})
    return {"projects": rows}


# ---------------- Inserimenti ----------------
@app.post("/stakeholder")
def add_stakeholder():
    """Upsert di uno Stakeholder + (sector, keywords) e link LOD dalle keyword.

    Body JSON conforme a `StakeholderIn`.
    """
    try:
        body = request.get_json(force=True)
        data = StakeholderIn(**body)

        # --- Parametri base per upsert ---
        params = data.model_dump()
        params["keywords"] = params.get("keywords") or []

        # === Upsert Stakeholder + Sector + Keywords (idempotente) ===
        q_base = """
        MERGE (s:Stakeholder {id:$id})
        ON CREATE SET s.name=$name, s.type=$type, s.location=$location, s.description=$description
        ON MATCH  SET s.name=$name, s.type=$type, s.location=$location, s.description=$description

        // Sector: reset & merge
        WITH s, $sector AS sector
        OPTIONAL MATCH (s)-[r:IN_SECTOR]->(:Sector) DELETE r
        FOREACH (_ IN CASE WHEN sector IS NOT NULL AND trim(sector) <> "" THEN [1] ELSE [] END |
          MERGE (sec:Sector {name: sector})
          MERGE (s)-[:IN_SECTOR]->(sec)
        )

        // Keywords: reset & merge
        WITH s, $keywords AS kws
        OPTIONAL MATCH (s)-[rk:hasKeyword]->(:Keyword) DELETE rk
        FOREACH (kw IN (CASE WHEN kws IS NULL THEN [] ELSE kws END) |
          FOREACH (_ IN CASE WHEN kw IS NOT NULL AND trim(kw) <> "" THEN [1] ELSE [] END |
            MERGE (k:Keyword {name: kw})
            MERGE (s)-[:hasKeyword]->(k)
          )
        )

        RETURN s.id AS id
        """
        rows = run_cypher(q_base, params)
        _ = rows[0]["id"] if rows else data.id

        # === Keyword → LOD (dai lod_links del payload) ===
        kw_lod_rows = build_kw_lod_rows_from_stakeholder_payload(data)
        if kw_lod_rows:
            q_kw_lod = """
            UNWIND $rows AS r
            MERGE (k:Keyword {name:r.keyword})
            MERGE (e:LODEntity {uri:r.uri})
              ON CREATE SET e.source = r.source, e.label = r.label
              ON MATCH  SET e.source = coalesce(r.source, e.source),
                          e.label  = coalesce(r.label,  e.label)
            MERGE (k)-[rel:linkedTo]->(e)
              ON CREATE SET rel.source = r.source, rel.label = r.label
              ON MATCH  SET rel.source = coalesce(r.source, rel.source),
                          rel.label  = coalesce(r.label,  rel.label)

            // (opzionale ma sicuro) garantisci hasKeyword allo stakeholder
            WITH r
            MATCH (s:Stakeholder {id:$sid})
            MERGE (k2:Keyword {name:r.keyword})
            MERGE (s)-[:hasKeyword]->(k2)
            """
            run_cypher(q_kw_lod, {"rows": kw_lod_rows, "sid": data.id})

        return jsonify({"status": "ok", "id": data.id})

    except ValidationError as ve:
        return jsonify({"status":"error", "where":"pydantic", "detail": ve.errors()}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status":"error", "where":"server", "detail": str(e)}), 500


@app.post("/project")
def upsert_project():
    """Upsert di un Project + (stakeholders/keywords/SDG) e link LOD dalle keyword.

    Body JSON conforme a `ProjectIn`.
    """
    data = ProjectIn(**request.json)

    params = data.model_dump()
    params["stakeholders"] = params.get("stakeholders") or []
    params["keywords"]     = params.get("keywords") or []
    params["sdgs"]         = params.get("sdgs") or []

    # VALIDAZIONE: tutti gli stakeholder esistono?
    if params["stakeholders"]:
        try:
            existing = run_cypher(
                "MATCH (s:Stakeholder) WHERE s.id IN $ids RETURN collect(s.id) AS ids",
                {"ids": params["stakeholders"]}
            )
            have = set((existing[0]["ids"] if existing else []))
            want = set(params["stakeholders"])
            missing = sorted(list(want - have))
            if missing:
                return jsonify({
                    "error": "unknown_stakeholders",
                    "missing": missing
                }), 400
        except Exception as e:
            return jsonify({"error":"validation_failed","detail":str(e)}), 500

    q = """
    // Upsert progetto
    MERGE (p:Project {id:$id})
    SET  p.name        = $name,
         p.description = $description,
         p.location    = $location
    WITH p, $stakeholders AS stakeholders, $keywords AS keywords, $sdgs AS sdgs

    // --- partecipazioni: reset & ricrea
    OPTIONAL MATCH (p)<-[r:participatesIn]-()
    DELETE r
    WITH p, stakeholders, keywords, sdgs
    UNWIND stakeholders AS sid
      MATCH (s:Stakeholder {id:sid})
      MERGE (s)-[:participatesIn]->(p)
    WITH p, keywords, sdgs

    // --- keyword: reset & ricrea
    OPTIONAL MATCH (p)-[rk:relatedToKeyword]->()
    DELETE rk
    WITH p, keywords, sdgs
    UNWIND keywords AS kw
      MERGE (k:Keyword {name:kw})
      MERGE (p)-[:relatedToKeyword]->(k)
    WITH p, sdgs

    // --- SDG: reset & ricrea
    OPTIONAL MATCH (p)-[rg:contributesTo]->()
    DELETE rg
    WITH p, sdgs
    UNWIND sdgs AS code
      MERGE (g:SDG {code:code})
      MERGE (p)-[:contributesTo]->(g)

    RETURN p.id AS id
    """

    try:
        rows = run_cypher(q, params)
        pid  = rows[0]["id"] if rows else data.id
    except Exception as e:
        return jsonify({"error":"cypher_failed","detail":str(e)}), 500

    # -------- LOD → COLLEGA ALLE KEYWORD --------
    kw_lod_rows = build_kw_lod_rows_from_project_payload(data)
    if kw_lod_rows:
        q_kw_lod = """
        // Per ogni riga, collega Keyword -> LODEntity
        UNWIND $rows AS r
        MERGE (k:Keyword {name:r.keyword})
        MERGE (e:LODEntity {uri:r.uri})
          ON CREATE SET e.source = r.source, e.label = r.label
          ON MATCH  SET e.source = coalesce(r.source, e.source),
                      e.label  = coalesce(r.label,  e.label)
        MERGE (k)-[rel:linkedTo]->(e)
          ON CREATE SET rel.source = r.source, rel.label = r.label
          ON MATCH  SET rel.source = coalesce(r.source, rel.source),
                      rel.label  = coalesce(r.label,  rel.label)
        """
        try:
            run_cypher(q_kw_lod, {"rows": kw_lod_rows})
        except Exception as e:
            # non bloccare il salvataggio del progetto se falliscono i link LOD
            return jsonify({"status":"partial_ok","id": pid, "lod_error": str(e)}), 207

    return jsonify({"status":"ok","id": pid})


@app.post("/delete_node")
def delete_node():
    """Elimina un nodo con id completo (prefisso:valore) e, opzionalmente,
    fa cleanup degli orfani (Keyword/LODEntity/Sector).

    Body JSON:
        id: stringa es. 'stakeholder:s1'
        cleanup: bool (default True)
    """
    data = request.get_json(force=True) or {}
    full_id = data.get("id")
    do_cleanup = bool(data.get("cleanup", True))

    if not full_id:
        return jsonify({"error": "missing_id"}), 400
    try:
        label, raw = full_id.split(":", 1)
    except ValueError:
        return jsonify({"error": "bad_id_format"}), 400

    # pattern MATCH per etichetta
    if label == "stakeholder":
        match = "MATCH (n:Stakeholder {id:$id})"
    elif label == "project":
        match = "MATCH (n:Project {id:$id})"
    elif label == "keyword":
        match = "MATCH (n:Keyword {name:$id})"
    elif label == "sdg":
        match = "MATCH (n:SDG {code:$id})"
    elif label == "sector":
        match = "MATCH (n:Sector {name:$id})"
    elif label == "lod":
        match = "MATCH (n:LODEntity {uri:$id})"
    else:
        return jsonify({"error": "unsupported_label"}), 400

    # elimina nodo (fix: rimosso CALL {...} YIELD)
    cy_delete = f"""
    {match}
    DETACH DELETE n
    """

    # cleanup orfani (semplificato)
    cy_cleanup = """
        MATCH (k:Keyword)
        WHERE NOT ( (:Project)-[:relatedToKeyword]->(k) )
        AND NOT ( (:Stakeholder)-[:hasKeyword]->(k) )
        DETACH DELETE k
        WITH 1 AS _

        MATCH (e:LODEntity)
        WHERE NOT ( (:Keyword)-[:linkedTo]->(e) )
        DETACH DELETE e
        WITH 1 AS _

        MATCH (sec:Sector)
        WHERE NOT ( (:Stakeholder)-[:IN_SECTOR]->(sec) )
        DETACH DELETE sec
    """

    try:
        # conteggio esistenza (più chiaro senza slicing)
        cy_count = f"""
        {match}
        RETURN count(n) AS c
        """
        before = run_cypher(cy_count, {"id": raw})
        existed = (before and before[0].get("c", 0) > 0)

        if existed:
            run_cypher(cy_delete, {"id": raw})

        cleaned: Dict[str, Any] = {}
        if do_cleanup:
            # no-op (se vuoi mantenere il “prima/dopo”)
            run_cypher("RETURN 1 AS _noop", {})
            run_cypher(cy_cleanup, {})
            cleaned = {"keywords": None, "lod_entities": None, "sectors": None}

        return jsonify({"ok": True, "existed": existed, "cleanup": do_cleanup, "cleaned": cleaned})
    except Exception as e:
        return jsonify({"error": "cypher_failed", "detail": str(e)}), 500


# ---------------- Ego-graph locale ----------------
@app.get("/ego/<path:node_id>")
def ego(node_id: str):
    """Costruisce un piccolo ego-graph a profondità controllata.

    Args:
        node_id: id con prefisso (stakeholder:/project:/keyword:/sdg:).

    Query:
        depth: int (default 1)
        include_sector: bool (default true)
        include_lod: bool (default false)
    """
    try:
        label, raw_id = node_id.split(":", 1)
    except ValueError:
        return jsonify({"error": "invalid id"}), 400

    depth = int(request.args.get("depth", "1"))
    include_sector = request.get_json(silent=True)  # type: ignore[assignment]
    include_sector = request.args.get("include_sector", "true").lower() in ("1","true","yes")
    include_lod    = request.args.get("include_lod", "false").lower() in ("1","true","yes")

    if label == "stakeholder":
        seed_match = "MATCH (seed:Stakeholder {id:$val})"
    elif label == "project":
        seed_match = "MATCH (seed:Project {id:$val})"
    elif label == "keyword":
        seed_match = "MATCH (seed:Keyword {name:$val})"
    elif label == "sdg":
        seed_match = "MATCH (seed:SDG {code:$val})"
    else:
        return jsonify({"error":"unsupported label"}), 400

    # elenco tipi SENZA i “:”
    rel_filter = "participatesIn|relatedToKeyword|contributesTo|hasKeyword|IN_SECTOR|linkedTo"

    cypher = f"""
    {seed_match}

    // 1) nodi entro 'depth' hop e i path (nota: un solo ':' davanti al primo tipo)
    OPTIONAL MATCH p=(seed)-[:{rel_filter}*..{depth}]-(n)
    WITH seed, collect(DISTINCT n) AS ns, collect(DISTINCT p) AS paths

    // 2) relazioni dai path
    UNWIND (CASE WHEN size(paths)=0 THEN [NULL] ELSE paths END) AS p2
    WITH seed, ns, p2
    UNWIND (CASE WHEN p2 IS NULL THEN [] ELSE relationships(p2) END) AS r1
    WITH seed, ns, collect(DISTINCT r1) AS rels_base

    // 3) opzionale: IN_SECTOR dal seed
    OPTIONAL MATCH (seed)-[rsec:IN_SECTOR]->(sec:Sector)
    WITH seed, ns, rels_base, collect(DISTINCT rsec) AS rels_sec

    // 4) opzionale: linkedTo dalle keyword collegate al seed
    OPTIONAL MATCH (seed)-[:hasKeyword]->(sk:Keyword)
    OPTIONAL MATCH (seed)-[:relatedToKeyword]->(pk:Keyword)
    WITH seed, ns, rels_base, rels_sec, collect(DISTINCT sk) + collect(DISTINCT pk) AS kws
    UNWIND (CASE WHEN size(kws)=0 THEN [NULL] ELSE kws END) AS kw
    WITH seed, ns, rels_base, rels_sec, kw
    OPTIONAL MATCH (kw)-[lt:linkedTo]->(e:LODEntity)
    WITH seed, ns, rels_base, rels_sec, collect(DISTINCT lt) AS rels_lod

    // 5) combina in base ai flag
    WITH seed, ns,
         rels_base +
         (CASE WHEN $inc_sec THEN rels_sec ELSE [] END) +
         (CASE WHEN $inc_lod THEN rels_lod ELSE [] END) AS allrels

    // 6) rimuovi NULL
    UNWIND (CASE WHEN size(allrels)=0 THEN [NULL] ELSE allrels END) AS rr
    WITH seed, ns, rr
    WHERE rr IS NOT NULL
    WITH seed, ns, collect(DISTINCT rr) AS rels_final

    // 7) set nodi = vicini + seed + estremi relazioni
    WITH seed, ns,
         [x IN rels_final | startNode(x)] AS starts,
         [x IN rels_final | endNode(x)]   AS ends,
         rels_final
    WITH seed, ns + [seed] + starts + ends AS allnodes, rels_final

    // 8) normalizza nodi
    UNWIND allnodes AS an
    WITH DISTINCT an, rels_final
    WITH
      CASE
        WHEN 'Stakeholder' IN labels(an) THEN {{id:'stakeholder:'+an.id, label:an.name, type:'Stakeholder'}}
        WHEN 'Project'     IN labels(an) THEN {{id:'project:'+an.id,     label:an.name, type:'Project'}}
        WHEN 'Keyword'     IN labels(an) THEN {{id:'keyword:'+an.name,   label:an.name, type:'Keyword'}}
        WHEN 'SDG'         IN labels(an) THEN {{id:'sdg:'+an.code,       label:an.code, type:'SDG'}}
        WHEN 'Sector'      IN labels(an) THEN {{id:'sector:'+an.name,    label:an.name, type:'Sector'}}
        WHEN 'LODEntity'   IN labels(an) THEN {{id:'lod:'+an.uri,        label:an.label, type:'LODEntity'}}
        ELSE {{id: toString(id(an)), label: coalesce(an.name, 'node'), type:'Node'}}
      END AS out_node,
      rels_final
    WITH collect(out_node) AS nodes, rels_final

    // 9) normalizza archi
    UNWIND rels_final AS ar
    WITH nodes, {{
      source: CASE
        WHEN 'Stakeholder' IN labels(startNode(ar)) THEN 'stakeholder:'+startNode(ar).id
        WHEN 'Project'     IN labels(startNode(ar)) THEN 'project:'+startNode(ar).id
        WHEN 'Keyword'     IN labels(startNode(ar)) THEN 'keyword:'+startNode(ar).name
        WHEN 'SDG'         IN labels(startNode(ar)) THEN 'sdg:'+startNode(ar).code
        WHEN 'Sector'      IN labels(startNode(ar)) THEN 'sector:'+startNode(ar).name
        WHEN 'LODEntity'   IN labels(startNode(ar)) THEN 'lod:'+startNode(ar).uri
        ELSE toString(id(startNode(ar)))
      END,
      target: CASE
        WHEN 'Stakeholder' IN labels(endNode(ar)) THEN 'stakeholder:'+endNode(ar).id
        WHEN 'Project'     IN labels(endNode(ar)) THEN 'project:'+endNode(ar).id
        WHEN 'Keyword'     IN labels(endNode(ar)) THEN 'keyword:'+endNode(ar).name
        WHEN 'SDG'         IN labels(endNode(ar)) THEN 'sdg:'+endNode(ar).code
        WHEN 'Sector'      IN labels(endNode(ar)) THEN 'sector:'+endNode(endNode(ar)).name
        WHEN 'LODEntity'   IN labels(endNode(ar)) THEN 'lod:'+endNode(ar).uri
        ELSE toString(id(endNode(ar)))
      END,
      type: type(ar)
    }} AS one_edge
    WITH nodes, collect(one_edge) AS edges
    RETURN nodes, edges
    """

    try:
        rows = run_cypher(cypher, {"val": raw_id, "inc_sec": include_sector, "inc_lod": include_lod})
    except Exception as e:
        return jsonify({"error":"cypher_failed", "detail": str(e)}), 500

    if not rows:
        return jsonify({"elements":{"nodes":[], "edges":[]}})

    nodes = [{"data": n} for n in rows[0]["nodes"]]
    edges = [{"data": {"source": e["source"], "target": e["target"], "label": e["type"]}} for e in rows[0]["edges"]]

    # evidenzia il seed
    for n in nodes:
        if n["data"]["id"] == node_id:
            n["data"]["highlight"] = True
            break

    return jsonify({"elements": {"nodes": nodes, "edges": edges}})


@app.post("/lp/pipeline/drop")
def lp_pipeline_drop():
    """Drop esplicito di una pipeline LP (beta)."""
    name = (request.get_json(force=True) or {}).get("name", "lp-pipeline")
    try:
        run_cypher(f"CALL {LP_BETA}.drop($name)", {"name": name})
        return jsonify({"pipeline": name, "dropped": True})
    except Exception as e:
        msg = str(e).lower()
        if any(x in msg for x in ["unknown procedure","procedurenotfound","not found","does not exist","no pipeline"]):
            return jsonify({"pipeline": name, "dropped": False, "noop": True})
        return jsonify({"error":"pipeline_drop_failed","detail":str(e)}), 500


@app.post("/gds/project")
def gds_project():
    """Crea la proiezione generale 'kg_sdg' (Stakeholder/Project/Keyword) in GDS."""
    try:
        run_cypher("CALL gds.graph.drop('kg_sdg', false)", {})
    except Exception:
        pass

    proj = """
    CALL gds.graph.project(
      'kg_sdg',
      ['Stakeholder','Project','Keyword'],
      {
        participatesIn:   {type:'participatesIn',   orientation:'UNDIRECTED'},
        hasKeyword:       {type:'hasKeyword',       orientation:'UNDIRECTED'},
        relatedToKeyword: {type:'relatedToKeyword', orientation:'UNDIRECTED'}
      }
    )
    YIELD graphName, nodeCount, relationshipCount
    RETURN graphName AS graph, nodeCount AS nodes, relationshipCount AS rels
    """
    try:
        rows = run_cypher(proj, {})
        return jsonify(rows[0])
    except Exception as e:
        return jsonify({"error":"gds_project_failed","detail":str(e)}), 500


@app.post("/gds/project_lp")
def gds_project_lp():
    """Crea la proiezione 'kg_sdg_lp' con soli Stakeholder e relazione target COLLAB.

    Richiede che le relazioni :COLLAB siano già materializzate (/labels/write_collab).
    """
    """
    Proiezione LP solo Stakeholder con relazione target COLLAB, esplicitamente UNDIRECTED.
    Richiede che :COLLAB sia già stata materializzata in DB (usa /labels/write_collab prima).
    """
    try:
        run_cypher("CALL gds.graph.drop('kg_sdg_lp', false)", {})
    except Exception:
        pass

    proj = """
    CALL gds.graph.project(
      'kg_sdg_lp',
      ['Stakeholder'],
      {
        COLLAB: { type: 'COLLAB', orientation: 'UNDIRECTED' }
      }
    )
    YIELD graphName, nodeCount, relationshipCount
    RETURN graphName AS graph, nodeCount AS nodes, relationshipCount AS rels
    """
    try:
        rows = run_cypher(proj, {})
        return jsonify(rows[0])
    except Exception as e:
        return jsonify({"error":"gds_project_lp_failed","detail":str(e)}), 500


@app.post("/labels/write_collab")
def write_collab():
    """Materializza relazioni :COLLAB fra stakeholder co-partecipanti a progetti."""
    """
    Crea relazioni :COLLAB fra coppie di Stakeholder che hanno co-partecipato ad almeno un Project.
    Scriviamo una sola direzione (s1 -> s2, con id(s1) < id(s2)): in projection GDS la renderemo UNDIRECTED.
    """
    cy = """
    MATCH (s1:Stakeholder)-[:participatesIn]->(:Project)<-[:participatesIn]-(s2:Stakeholder)
    WHERE id(s1) < id(s2)
    MERGE (s1)-[:COLLAB]->(s2)
    RETURN count(*) AS created
    """
    try:
        rows = run_cypher(cy, {})
        return jsonify(rows[0])
    except Exception as e:
        return jsonify({"error":"write_collab_failed","detail":str(e)}), 500


@app.post("/gds/embeddings")
def gds_embeddings():
    """Calcola embeddings FastRP su 'kg_sdg' (write + mutate) con dimensione configurabile.

    Body JSON:
        dim: int, dimensione embedding (default 64)
    """
    payload = request.get_json(force=True) or {}
    dim = int(payload.get("dim", 64))

    # Assicura projection 'kg_sdg'
    exists = run_cypher("CALL gds.graph.exists('kg_sdg') YIELD exists RETURN exists", {})
    if not (exists and exists[0].get("exists")):
        run_cypher("""
        CALL gds.graph.project(
          'kg_sdg',
          ['Stakeholder','Project','Keyword'],
          {
            participatesIn:   {type:'participatesIn',   orientation:'UNDIRECTED'},
            hasKeyword:       {type:'hasKeyword',       orientation:'UNDIRECTED'},
            relatedToKeyword: {type:'relatedToKeyword', orientation:'UNDIRECTED'}
          }
        )
        """, {})

    # 1) WRITE sul DB
    cy_write = """
    CALL gds.fastRP.write('kg_sdg', {
      writeProperty: 'embedding_fastrp',
      embeddingDimension: $dim
    })
    YIELD nodePropertiesWritten
    RETURN nodePropertiesWritten
    """

    # 2) MUTATE in-memory sulla proiezione (stesso nome proprietà)
    cy_mutate = """
    CALL gds.fastRP.mutate('kg_sdg', {
      mutateProperty: 'embedding_fastrp',
      embeddingDimension: $dim
    })
    YIELD nodePropertiesWritten
    RETURN nodePropertiesWritten
    """

    try:
        res_w = run_cypher(cy_write, {"dim": dim}) or [{"nodePropertiesWritten": 0}]
        res_m = run_cypher(cy_mutate, {"dim": dim}) or [{"nodePropertiesWritten": 0}]
        return jsonify({
            "writeProperty": "embedding_fastrp",
            "written_db": res_w[0]["nodePropertiesWritten"],
            "mutated_mem": res_m[0]["nodePropertiesWritten"]
        })
    except Exception as e:
        return jsonify({"error":"gds_fastrp_failed","detail":str(e)}), 500


@app.get("/predict/stakeholder-sim")
def predict_stakeholder_sim():
    """Similarità cosine tra due stakeholder (con spiegazioni)."""
    a = request.args.get("a"); b = request.args.get("b")
    if not a or not b:
        return jsonify({"error":"missing_params"}), 400

    cy = """
    MATCH (a:Stakeholder {id:$a}), (b:Stakeholder {id:$b})
    WHERE a.embedding_fastrp IS NOT NULL AND b.embedding_fastrp IS NOT NULL
    WITH a, b, gds.similarity.cosine(a.embedding_fastrp, b.embedding_fastrp) AS sim
    // Progetti condivisi
    OPTIONAL MATCH (a)-[:participatesIn]->(p:Project)<-[:participatesIn]-(b)
    WITH a,b,sim, collect(DISTINCT {id:p.id, name:p.name, location:p.location}) AS sharedP
    // Keyword condivise (dirette)
    OPTIONAL MATCH (a)-[:hasKeyword]->(k:Keyword)<-[:hasKeyword]-(b)
    RETURN sim AS similarity, sharedP AS sharedProjects, collect(DISTINCT k.name) AS sharedKeywords
    """
    try:
        row = run_cypher(cy, {"a": a, "b": b})
        r = row[0] if row else {"similarity": None, "sharedProjects": [], "sharedKeywords": []}
        return jsonify({
            "similarity": r["similarity"],
            "explain": {
                "sharedProjects": r["sharedProjects"],
                "sharedKeywords": r["sharedKeywords"]
            }
        })
    except Exception as e:
        return jsonify({"error":"predict_failed","detail":str(e)}), 500


@app.get("/predict_collaboration")
def predict():
    """Predizione candidati Stakeholder per un Project via Jaccard su progetti correlati.

    Query:
        project_id: id del progetto (obbligatorio)
    """
    pid = request.args.get("project_id")
    if not pid:
        return jsonify({"error":"project_id richiesto"}), 400

    cypher = """
    // Progetto di riferimento
    MATCH (p:Project {id:$pid})

    // Progetti "relazionati" al target via keyword (ID numerici!)
    MATCH (p)-[:relatedToKeyword]->(:Keyword)<-[:relatedToKeyword]-(p2:Project)
    WITH p, collect(DISTINCT id(p2)) AS rel_pids

    // Partecipanti attuali al progetto p (da escludere)
    MATCH (p)<-[:participatesIn]-(curr:Stakeholder)
    WITH p, rel_pids, collect(curr) AS current

    // Candidati: stakeholder che hanno lavorato su almeno uno dei progetti relazionati
    MATCH (cand:Stakeholder)-[:participatesIn]->(cp:Project)
    WHERE NOT cand IN current AND id(cp) IN rel_pids
    WITH cand, rel_pids, collect(DISTINCT id(cp)) AS cand_cp_ids

    // Jaccard tra set di progetti del candidato e set di progetti relazionati a p
    WITH cand, gds.similarity.jaccard(cand_cp_ids, rel_pids) AS score
    WHERE score > 0.0
    RETURN cand.id AS stakeholder_id, cand.name AS name, score
    ORDER BY score DESC
    LIMIT 20
    """
    rows = run_cypher(cypher, {"pid": pid})
    return jsonify({"project_id": pid, "predictions": rows})


@app.get("/predict/stakeholder-topk")
def predict_stakeholder_topk():
    """Restituisce top-k stakeholder simili (cosine su FastRP)."""
    a = request.args.get("a")
    k = int(request.args.get("k", "10"))
    if not a:
        return jsonify({"error": "missing_param_a"}), 400

    q = """
    MATCH (a:Stakeholder {id:$a}), (b:Stakeholder)
    WHERE a <> b
      AND a.embedding_fastrp IS NOT NULL
      AND b.embedding_fastrp IS NOT NULL
    WITH a, b, gds.similarity.cosine(a.embedding_fastrp, b.embedding_fastrp) AS sim
    RETURN b.id AS target_id, b.name AS target_name, sim
    ORDER BY sim DESC LIMIT $k
    """
    try:
        rows = run_cypher(q, {"a": a, "k": k})
        return jsonify({"results": rows})
    except Exception as e:
        return jsonify({"error":"topk_failed","detail":str(e)}), 500


@app.get("/gds/status")
def gds_status():
    """Stato della proiezione 'kg_sdg' e delle embedding (in-memory/DB)."""
    try:
        ex = run_cypher("CALL gds.graph.exists('kg_sdg') YIELD exists RETURN exists", {})
        exists = bool(ex and ex[0].get("exists"))
    except Exception as e:
        return jsonify({"error": "exists_failed", "detail": str(e)}), 500

    if not exists:
        return jsonify({"exists": False})

    try:
        rows = run_cypher("""
        CALL gds.graph.list('kg_sdg')
        YIELD graphName, nodeCount, relationshipCount
        RETURN graphName AS graph, nodeCount AS nodes, relationshipCount AS rels
        """, {})
        info = rows[0] if rows else {"graph": "kg_sdg", "nodes": 0, "rels": 0}
    except Exception as e:
        return jsonify({"error": "list_failed", "detail": str(e)}), 500

    # Proprietà in-memory
    try:
        props_row = run_cypher("""
        CALL gds.graph.nodeProperty.stream('kg_sdg')
        YIELD nodeProperty
        RETURN collect(DISTINCT nodeProperty) AS nodeProperties
        """, {})
        node_props = props_row[0].get("nodeProperties") if props_row else []
    except Exception:
        node_props = []

    # Conteggio embedding scritti su DB (solo Stakeholder)
    try:
        db_count_row = run_cypher("""
        MATCH (s:Stakeholder) WHERE s.embedding_fastrp IS NOT NULL
        RETURN count(s) AS cnt
        """, {})
        db_count = int(db_count_row[0]["cnt"]) if db_count_row else 0
    except Exception:
        db_count = 0

    in_mem = set(node_props or [])
    has_mem = ("embedding_fastrp" in in_mem) or ("embedding_fastrp_mem" in in_mem)

    info.update({
        "exists": True,
        "nodeProperties": node_props,
        "hasEmbeddingsMem": has_mem,
        "dbEmbeddingsCount": db_count
    })
    return jsonify(info)


@app.get("/lp/dataset")
def lp_dataset():
    """Crea dataset supervisionato (positivi/negativi) + feature per LP.

    Query:
        limit_pos: limite positivi (default 5000)
        neg_per_pos: rapporto negativi/positivi (default 1.0)
    """
    limit_pos = int(request.args.get("limit_pos", "5000"))
    neg_per_pos = float(request.args.get("neg_per_pos", "1.0"))

    cy = """
    // POSITIVI: coppie con COLLAB (co-progetti)
    CALL {
      MATCH (a:Stakeholder)-[:participatesIn]->(:Project)<-[:participatesIn]-(b:Stakeholder)
      WHERE id(a) < id(b)
      WITH a,b LIMIT $limit_pos
      RETURN a.id AS u, b.id AS v, 1 AS label
    }
    UNION ALL
    // NEGATIVI: coppie non collegate
    CALL {
      MATCH (s:Stakeholder)
      WITH collect(s) AS S
      WITH S, toInteger($limit_pos * $neg_per_pos) AS need
      CALL {
        WITH S, need
        UNWIND range(1, need*3) AS _
        WITH S[toInteger(rand()*size(S))] AS a,
             S[toInteger(rand()*size(S))] AS b
        WHERE id(a) < id(b)
        WITH a,b LIMIT need*5
        OPTIONAL MATCH (a)-[:participatesIn]->(:Project)<-[:participatesIn]-(b)
        WITH a,b, count(*) AS collaborated
        WHERE collaborated = 0
        RETURN a,b LIMIT need
      }
      RETURN a.id AS u, b.id AS v, 0 AS label
    }
    """
    pairs = run_cypher(cy, {"limit_pos": limit_pos, "neg_per_pos": neg_per_pos})

    feat_cy = """
    UNWIND $pairs AS row
    MATCH (a:Stakeholder {id: row.u}), (b:Stakeholder {id: row.v})
    // cosine su FastRP (se esiste)
    WITH row, a, b,
         (CASE WHEN a.embedding_fastrp IS NOT NULL AND b.embedding_fastrp IS NOT NULL
               THEN gds.similarity.cosine(a.embedding_fastrp, b.embedding_fastrp)
               ELSE 0.0 END) AS cos_sim
    // jaccard progetti
    OPTIONAL MATCH (a)-[:participatesIn]->(pa:Project)
    OPTIONAL MATCH (b)-[:participatesIn]->(pb:Project)
    WITH row, cos_sim, collect(DISTINCT pa.id) AS Aproj, collect(DISTINCT pb.id) AS Bproj
    WITH row, cos_sim, gds.similarity.jaccard(Aproj, Bproj) AS jacc_proj
    // jaccard keyword
    OPTIONAL MATCH (a)-[:hasKeyword]->(ka:Keyword)
    OPTIONAL MATCH (b)-[:hasKeyword]->(kb:Keyword)
    WITH row, cos_sim, jacc_proj, collect(DISTINCT ka.name) AS Akw, collect(DISTINCT kb.name) AS Bkw
    WITH row, cos_sim, jacc_proj, gds.similarity.jaccard(Akw, Bkw) AS jacc_kw
    RETURN row.u AS u, row.v AS v, row.label AS label,
           cos_sim AS cos_sim, jacc_proj AS jacc_proj, jacc_kw AS jacc_kw
    """
    feats = run_cypher(feat_cy, {"pairs": pairs})

    return jsonify({"n_rows": len(feats), "rows": feats})


def _sanitize_name(s: str) -> str:
    """Sanitizza nome per usarlo in proprietà/nomi GDS (solo [A-Za-z0-9_])."""
    return re.sub(r"[^A-Za-z0-9_]", "_", s)


def _choose_split_for_lp(graph_name: str) -> Dict[str, Any]:
    """Calcola una configurazione di split *safe* in base al numero di archi LP.

    Requisiti:
      - almeno 1 edge nel test
      - almeno 2 edge nel train
      - validationFolds <= train_edges e >= 2

    Args:
        graph_name: Nome della proiezione LP.

    Returns:
        Dict con chiavi:
          ok (bool), rels (int), testFraction (float), trainFraction (float),
          validationFolds (int) se ok=True; altrimenti ok=False + reason.
    """
    """
    Restituisce uno split *safe* in base al numero di archi della projection LP:
      - almeno 1 edge nel test
      - almeno 2 edge nel train
      - validationFolds <= train_edges e >= 2
    Se rels < 3 non è possibile fare uno split valido → ok=False.
    """
    try:
        rows = run_cypher(f"""
        CALL gds.graph.list('{graph_name}')
        YIELD graphName, relationshipCount
        RETURN relationshipCount AS rels
        """, {})
        rels = int(rows[0]["rels"]) if rows else 0
    except Exception:
        rels = 0

    # con < 3 archi non possiamo avere test>=1 e train>=2
    if rels < 3:
        return dict(ok=False, reason="too_small", rels=rels)

    # assegna il MINIMO indispensabile al test (1 edge)
    test_edges = 1
    train_edges = rels - test_edges

    # folds: almeno 2 e al massimo il # di archi train
    folds = max(2, min(5, train_edges))

    # frazioni coerenti con i conteggi interi scelti
    testFraction = max(1.0 / rels, 0.01)      # garantisce ≥1 in test
    trainFraction = 1.0 - testFraction        # il resto in train

    # ulteriore safety: se train_edges risultante < folds, riduci folds
    if train_edges < folds:
        folds = max(2, train_edges)

    return dict(
        ok=True,
        rels=rels,
        testFraction=testFraction,
        trainFraction=trainFraction,
        validationFolds=folds
    )


@app.post("/lp/pipeline/build")
def lp_pipeline_build():
    """Crea/configura una LP pipeline (beta + RF alpha) con split *safe*."""
    payload = request.get_json(force=True) or {}
    name = payload.get("name", "lp-pipeline")
    graph_name = payload.get("graph", "kg_sdg_lp")

    safe = _sanitize_name(name)
    mutate_prop = f"lp_emb_{safe}"

    def _safe(cy, params=None):
        """Esegue Cypher ignorando errori 'idempotenti' (già esiste/già aggiunto...)."""
        try:
            run_cypher(cy, params or {})
        except Exception as e:
            msg = str(e).lower()
            if any(k in msg for k in [
                "already exists","already added","duplicate","exists with name",
                "mutateproperty","already specified","unrecognized feature",
                "unsupported feature"
            ]):
                return
            raise

    # 1) create (beta)
    try:
        run_cypher(f"CALL {LP_BETA}.create($name)", {"name": name})
    except Exception as e:
        if "already exists" not in str(e).lower():
            return jsonify({"error":"pipeline_create_failed","detail":str(e)}), 500

    # 2) Node property: FastRP (beta) — mappa parametricizzata
    _safe(
        f"CALL {LP_BETA}.addNodeProperty($name, 'fastRP', $cfg)",
        {"name": name, "cfg": {"mutateProperty": mutate_prop, "embeddingDimension": 56, "randomSeed": 42}}
    )

    # 3) Features (beta) — tutte parametriche
    for feat in ("cosine","l2","hadamard"):
        _safe(
            f"CALL {LP_BETA}.addFeature($name, $feat, $cfg)",
            {"name": name, "feat": feat, "cfg": {"nodeProperties": [mutate_prop]}}
        )

    # 4) Split adattivo/safe
    split = _choose_split_for_lp(graph_name)
    if not split.get("ok"):
        return jsonify({
            "pipeline": name, "graph": graph_name, "status": "split_failed",
            "reason": split.get("reason"), "rels": split.get("rels", 0),
            "hint": "Servono almeno 3 archi COLLAB nella projection LP (>=1 test, >=2 train)."
        }), 400

    _safe(
        f"CALL {LP_BETA}.configureSplit($name, $cfg)",
        {"name": name, "cfg": {
            "testFraction": split["testFraction"],
            "trainFraction": split["trainFraction"],
            "validationFolds": split["validationFolds"]
        }}
    )

    # 5) Modelli: LR (beta), RF (alpha)
    _safe(f"CALL {LP_BETA}.addLogisticRegression($name)", {"name": name})
    _safe(f"CALL {LP_ALPHA}.addRandomForest($name, $cfg)", {"name": name, "cfg": {"numberOfDecisionTrees": 10}})

    # 6) LR con grid (beta) — parametricizzata
    _safe(
        f"CALL {LP_BETA}.addLogisticRegression($name, $cfg)",
        {"name": name, "cfg": {"maxEpochs": 500, "penalty": {"range": [1e-4, 1e2]}}}
    )

    return jsonify({
        "pipeline": name,
        "graph": graph_name,
        "status": "configured",
        "namespace": "beta (+ alpha RF)",
        "embeddingProperty": mutate_prop,
        "features": ["cosine","l2","hadamard"],
        "split": {
            "rels": split["rels"],
            "testFraction": split["testFraction"],
            "trainFraction": split["trainFraction"],
            "validationFolds": split["validationFolds"]
        }
    })


@app.post("/lp/train")
def lp_train():
    """Esegue train della LP pipeline e prova lo store del modello (best-effort).

    Body JSON:
        pipeline (default 'lp-pipeline')
        modelName (default 'lp-pipeline-model')
        graph (default 'kg_sdg_lp')
        targetRelationshipType (default 'COLLAB')
    """
    payload = request.get_json(force=True) or {}
    pipeline = payload.get("pipeline", "lp-pipeline")
    model_name = payload.get("modelName", "lp-pipeline-model")
    graph_name = payload.get("graph", "kg_sdg_lp")
    target_rel = payload.get("targetRelationshipType", "COLLAB")

    ex = run_cypher("CALL gds.graph.exists($g) YIELD exists RETURN exists", {"g": graph_name})
    if not (ex and ex[0]["exists"]):
        return jsonify({"error":"missing_projection","detail":f"{graph_name} non esiste"}), 400

    # estimate (best-effort)
    try:
        est = run_cypher(
            f"CALL {LP_BETA}.train.estimate($g, {{ pipeline: $p, modelName: $m, targetRelationshipType: $t }}) "
            "YIELD requiredMemory RETURN requiredMemory",
            {"g": graph_name, "p": pipeline, "m": model_name, "t": target_rel}
        )
        est_mem = est[0]["requiredMemory"] if est else None
    except Exception:
        est_mem = None

    rows = run_cypher(
        f"CALL {LP_BETA}.train($g, {{ pipeline: $p, modelName: $m, targetRelationshipType: $t, metrics: ['AUCPR'], randomSeed: 42 }}) "
        "YIELD modelInfo, modelSelectionStats "
        "RETURN modelInfo, modelSelectionStats",
        {"g": graph_name, "p": pipeline, "m": model_name, "t": target_rel}
    )

    if rows and "modelInfo" in rows[0]:
        mi = rows[0]["modelInfo"]; ms = rows[0].get("modelSelectionStats")
        out = {
            "winningModel": mi.get("bestParameters"),
            "winningType":  mi.get("modelType"),
            "avgTrainScore": mi.get("metrics",{}).get("AUCPR",{}).get("train",{}).get("avg"),
            "outerTrainScore": mi.get("metrics",{}).get("AUCPR",{}).get("outerTrain"),
            "testScore": mi.get("metrics",{}).get("AUCPR",{}).get("test"),
            "validationScores": [c.get("metrics",{}).get("AUCPR",{}).get("validation",{}).get("avg") for c in (ms or {}).get("modelCandidates",[])]
        }
    else:
        out = rows[0] if rows else {}

    # alla fine di lp_train(), prima del return:
    try:
        _model_store(model_name)
        stored = True
    except Exception:
        stored = False

    return jsonify({
        "estimatedMemory": est_mem,
        "trainSummary": out,
        "stored": stored
    })


@app.get("/lp/predict/topk")
def lp_predict_topk():
    """Top-k candidati collegamenti per uno stakeholder, via modello LP."""
    graph_name = request.args.get("graph", "kg_sdg_lp")
    model_name = request.args.get("model", "lp-pipeline-model")
    a = request.args.get("a")
    k = int(request.args.get("k", "20"))
    if not a:
        return jsonify({"error":"missing_param_a"}), 400

    topN = max(k * 3, 50)
    if not _model_exists(model_name):
        try:
            _model_load(model_name)
        except Exception:
            pass  # se fallisce, la predict dirà che non trova nulla

    cy_v1 = f"""
    MATCH (a:Stakeholder {{id:$a}})
    WITH id(a) AS aId
    CALL {LP_BETA}.predict.stream($graph, {{ modelName: $model, topN: $topN }})
    YIELD node1, node2, probability, isExistingRelationship
    WITH aId, node1, node2, probability, isExistingRelationship
    WHERE isExistingRelationship = false AND (node1 = aId OR node2 = aId)
    WITH CASE WHEN node1 = aId THEN node2 ELSE node1 END AS bId, probability
    MATCH (b:Stakeholder) WHERE id(b) = bId
    RETURN b.id AS stakeholder_id, b.name AS name, probability
    ORDER BY probability DESC
    LIMIT $k
    """

    cy_v2 = f"""
    MATCH (a:Stakeholder {{id:$a}})
    WITH id(a) AS aId
    CALL {LP_BETA}.predict.stream($graph, {{ modelName: $model, topN: $topN }})
    YIELD node1, node2, probability
    WITH aId, node1, node2, probability
    WHERE node1 = aId OR node2 = aId
    WITH aId, CASE WHEN node1 = aId THEN node2 ELSE node1 END AS bId, probability
    MATCH (b:Stakeholder) WHERE id(b) = bId
    MATCH (aDb) WHERE id(aDb) = aId
    OPTIONAL MATCH (aDb)-[r:COLLAB]-(b)
    WITH b, probability, count(r) AS rcount
    WHERE rcount = 0
    RETURN b.id AS stakeholder_id, b.name AS name, probability
    ORDER BY probability DESC
    LIMIT $k
    """
    try:
        rows = run_cypher(cy_v1, {"graph": graph_name, "model": model_name, "a": a, "k": k, "topN": topN})
        return jsonify({"a": a, "results": rows})
    except Exception:
        try:
            rows = run_cypher(cy_v2, {"graph": graph_name, "model": model_name, "a": a, "k": k, "topN": topN})
            return jsonify({"a": a, "results": rows, "note": "fallback_no_isExistingRelationship"})
        except Exception as e2:
            return jsonify({"error":"lp_predict_failed","detail":str(e2)}), 500


@app.post("/lp/predict/write")
def lp_predict_write():
    """Scrive in-memory le relazioni predette (COLLAB_PRED) con proprietà 'score'."""
    payload = request.get_json(force=True) or {}
    graph_name = payload.get("graph", "kg_sdg_lp")
    model_name = payload.get("model", "lp-pipeline-model")

    cy = f"""
    CALL {LP_BETA}.predict.mutate($graph, {{
      modelName: $model,
      mutateRelationshipType: 'COLLAB_PRED',
      mutateProperty: 'score'
    }})
    YIELD relationshipsWritten
    RETURN relationshipsWritten
    """
    try:
        rows = run_cypher(cy, {"graph": graph_name, "model": model_name})
        return jsonify(rows[0] if rows else {"relationshipsWritten": 0})
    except Exception as e:
        return jsonify({"error":"lp_predict_write_failed","detail":str(e)}), 500


@app.get("/list/stakeholders")
def list_stakeholders():
    """Elenco minimale (id, name) degli stakeholder ordinati per nome."""
    cy = "MATCH (s:Stakeholder) RETURN s.id AS id, s.name AS name ORDER BY name"
    try:
        rows = run_cypher(cy, {})
        return jsonify({"stakeholders": rows})
    except Exception as e:
        return jsonify({"error":"list_failed","detail":str(e)}), 500
    

@app.get("/stakeholders")
def stakeholders_catalog():
    """Catalogo stakeholder con ricerca/paginazione.

    Query:
      - q: filtro testuale su name/id/type (facoltativo)
      - limit: default 50 (1..200)
      - offset: default 0
    """
    q = (request.args.get("q") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
    except Exception:
        limit = 50
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except Exception:
        offset = 0

    params = {"q": q, "limit": limit, "offset": offset}

    # totale
    cy_total = """
    MATCH (s:Stakeholder)
    WHERE $q = '' OR
          toLower(s.name) CONTAINS toLower($q) OR
          toLower(s.id)   CONTAINS toLower($q) OR
          toLower(coalesce(s.type, '')) CONTAINS toLower($q)
    RETURN count(s) AS total
    """

    # pagina di risultati
    cy_page = """
    MATCH (s:Stakeholder)
    WHERE $q = '' OR
          toLower(s.name) CONTAINS toLower($q) OR
          toLower(s.id)   CONTAINS toLower($q) OR
          toLower(coalesce(s.type, '')) CONTAINS toLower($q)
    RETURN s.id AS id, s.name AS name, s.type AS type
    ORDER BY toLower(name) ASC
    SKIP $offset LIMIT $limit
    """

    try:
        total_row = run_cypher(cy_total, params)
        total = total_row[0]["total"] if total_row else 0
        items = run_cypher(cy_page, params) or []
        return jsonify({"items": items, "total": total})
    except Exception as e:
        return jsonify({"error":"list_failed","detail":str(e)}), 500


@app.get("/list/projects")
def list_projects():
    """Elenco minimale dei progetti per select/autocomplete."""
    """
    Ritorna un elenco minimale dei project per popolare select/autocomplete.
    Shape: {"projects":[{"id":..., "name":..., "code": ...?}]}
    """
    cy = """
    MATCH (p:Project)
    RETURN p.id AS id,
           p.name AS name,
           coalesce(p.code, NULL) AS code
    ORDER BY toLower(name) ASC
    """
    try:
        rows = run_cypher(cy, {})
        return jsonify({"projects": rows})
    except Exception as e:
        return jsonify({"error":"list_projects_failed","detail":str(e)}), 500


@app.get("/projects/search")
def projects_search():
    """Ricerca/paginazione progetti con keyword/SDG aggregati.

    Query:
      - q: filtro testuale (name/id/code/location, + keyword/SDG collegati)
      - limit: default 50 (1..200)
      - offset: default 0
    """
    q = (request.args.get("q") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
    except Exception:
        limit = 50
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except Exception:
        offset = 0

    params = {"q": q, "limit": limit, "offset": offset}

    # dove applicare il filtro (name/id/code/location) + via relazioni (keyword/SDG)
    where_block = """
      $q = '' OR
      toLower(p.name)      CONTAINS toLower($q) OR
      toLower(p.id)        CONTAINS toLower($q) OR
      toLower(coalesce(p.code, ''))     CONTAINS toLower($q) OR
      toLower(coalesce(p.location, '')) CONTAINS toLower($q) OR
      EXISTS {
        MATCH (p)-[:relatedToKeyword]->(k:Keyword)
        WHERE toLower(k.name) CONTAINS toLower($q)
      } OR
      EXISTS {
        MATCH (p)-[:contributesTo]->(g:SDG)
        WHERE toLower(g.code) CONTAINS toLower($q)
      }
    """

    cy_total = f"""
    MATCH (p:Project)
    WHERE {where_block}
    RETURN count(p) AS total
    """

    cy_page = f"""
    MATCH (p:Project)
    WHERE {where_block}
    OPTIONAL MATCH (p)-[:relatedToKeyword]->(k:Keyword)
    OPTIONAL MATCH (p)-[:contributesTo]->(g:SDG)
    RETURN p.id AS id,
           p.name AS name,
           coalesce(p.code, NULL) AS code,
           coalesce(p.location, NULL) AS location,
           collect(DISTINCT k.name) AS keywords,
           collect(DISTINCT g.code) AS sdgs
    ORDER BY toLower(name) ASC
    SKIP $offset LIMIT $limit
    """

    try:
        total_row = run_cypher(cy_total, params)
        total = total_row[0]["total"] if total_row else 0
        items = run_cypher(cy_page, params) or []
        # NB: mantengo la chiave "projects" per coerenza con /list/projects e col tuo front-end
        return jsonify({"projects": items, "total": total})
    except Exception as e:
        return jsonify({"error":"projects_search_failed","detail":str(e)}), 500


@app.get("/keywords")
def keywords_catalog():
    """Catalogo keyword con ricerca/paginazione e conteggi d'uso.

    Query:
      - q: filtro su k.name
      - limit: default 500 (1..1000)
      - offset: default 0
    """
    """
    Catalogo keyword con ricerca/paginazione.
    Query string:
      - q: filtro (substring, case-insensitive) su k.name
      - limit: default 500 (1..1000)
      - offset: default 0
    Risposta:
      {
        "keywords": ["kw1","kw2",...],
        "items": [
          {"name":"kw1","stakeholders":3,"projects":5,"total":8},
          ...
        ],
        "total": N
      }
    """
    q = (request.args.get("q") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", "500")), 1000))
    except Exception:
        limit = 500
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except Exception:
        offset = 0

    params = {"q": q, "limit": limit, "offset": offset}

    cy_total = """
    MATCH (k:Keyword)
    WHERE $q = '' OR toLower(k.name) CONTAINS toLower($q)
    RETURN count(k) AS total
    """

    # Nota: uso pattern comprehension per i conteggi d’uso (più veloce di OPTIONAL MATCH + COUNT DISTINCT)
    cy_page = """
    MATCH (k:Keyword)
    WHERE $q = '' OR toLower(k.name) CONTAINS toLower($q)
    WITH k,
         size( [(k)<-[:hasKeyword]-(:Stakeholder) | 1] ) AS stakeholders,
         size( [(k)<-[:relatedToKeyword]-(:Project)   | 1] ) AS projects
    WITH k.name AS name, stakeholders, projects, (stakeholders + projects) AS total
    ORDER BY total DESC, toLower(name) ASC
    SKIP $offset LIMIT $limit
    RETURN name, stakeholders, projects, total
    """

    try:
        total_row = run_cypher(cy_total, params)
        total = int(total_row[0]["total"]) if total_row else 0

        items = run_cypher(cy_page, params) or []
        keywords_only = [r["name"] for r in items]

        return jsonify({
            "keywords": keywords_only,
            "items": items,
            "total": total
        })
    except Exception as e:
        return jsonify({"error": "keywords_list_failed", "detail": str(e)}), 500


@app.post("/keywords")
def keywords_create():
    """Crea/mergia keyword nel grafo a partire da lista/oggetti semplice.

    Body:
      - {"keywords": ["kw1","kw2", ...]}
      oppure {"items": [{"name":"kw1"}, {"name":"kw2"}]}
    """
    payload = request.get_json(force=True) or {}
    raw = payload.get("keywords")
    if raw is None:
        raw = [x.get("name") for x in (payload.get("items") or []) if isinstance(x, dict)]

    if not raw:
        return jsonify({"error": "missing_keywords"}), 400

    # normalizza/deduplica
    seen = set()
    kws: List[str] = []
    for k in raw:
        if not isinstance(k, str):
            continue
        kk = k.strip()
        if kk and kk not in seen:
            seen.add(kk)
            kws.append(kk)

    if not kws:
        return jsonify({"error": "no_valid_keywords"}), 400

    cy = """
    UNWIND $kws AS kw
    MERGE (k:Keyword {name: kw})
    RETURN count(*) AS touched
    """
    try:
        rows = run_cypher(cy, {"kws": kws})
        touched = int(rows[0]["touched"]) if rows else 0
        return jsonify({"created": touched, "items": kws})
    except Exception as e:
        return jsonify({"error":"keywords_create_failed", "detail": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)