import requests
from typing import List, Dict, Optional


def wikidata_qids(name: str, limit: int = 2) -> List[Dict[str, Optional[str]]]:
    """Effettua una ricerca su Wikidata e restituisce un elenco di possibili entità.

    Args:
        name: Nome da cercare su Wikidata (ricerca testuale).
        limit: Numero massimo di risultati da restituire.

    Returns:
        Lista di dizionari, ciascuno contenente:
        - ``id``: QID dell’entità (es. "Q90"),
        - ``label``: Etichetta leggibile (es. "Paris"),
        - ``desc``: Descrizione breve (se disponibile).

    Esempio:
        >>> wikidata_qids("Paris", limit=1)
        [{'id': 'Q90', 'label': 'Paris', 'desc': 'capital and largest city of France'}]
    """
    url = "https://www.wikidata.org/w/api.php"
    r = requests.get(
        url,
        params={
            "action": "wbsearchentities",
            "search": name,
            "language": "en",
            "format": "json",
            "limit": limit,
        },
        timeout=10,
    )
    hits = r.json().get("search", [])
    return [
        {"id": h["id"], "label": h.get("label"), "desc": h.get("description")}
        for h in hits
    ]


def wikidata_summary_and_image(qid: str) -> Dict[str, Optional[str]]:
    """Recupera da Wikidata: label, descrizione e immagine (se presente).

    Args:
        qid: Identificatore Wikidata (es. "Q90").

    Returns:
        Dizionario con:
        - ``label``: Etichetta dell'entità in inglese.
        - ``description``: Descrizione in inglese (se presente).
        - ``image``: URL dell'immagine (se esiste).
        Se l'entità non è trovata, restituisce ``{}``.

    Esempio:
        >>> wikidata_summary_and_image("Q90")["label"]
        'Paris'
    """
    sparql = f"""
    SELECT ?label ?desc ?image WHERE {{
      wd:{qid} rdfs:label ?label FILTER (lang(?label)="en").
      OPTIONAL {{ wd:{qid} schema:description ?desc FILTER (lang(?desc)="en") }}.
      OPTIONAL {{ wd:{qid} wdt:P18 ?image }}.
    }} LIMIT 1
    """
    r = requests.get(
        "https://query.wikidata.org/sparql",
        headers={"Accept": "application/sparql-results+json"},
        params={"query": sparql},
        timeout=15,
    )
    b = r.json().get("results", {}).get("bindings", [])
    if not b:
        return {}

    rec = b[0]
    return {
        "label": rec.get("label", {}).get("value"),
        "description": rec.get("desc", {}).get("value"),
        "image": rec.get("image", {}).get("value"),
    }


def dbpedia_resource(name: str) -> str:
    """Restituisce l’URI DBpedia corrispondente a un nome.

    Args:
        name: Nome da convertire (spazi consentiti).

    Returns:
        URL DBpedia nel formato:
        ``https://dbpedia.org/resource/<Nome_con_sostituzione_spazi>``

    Esempio:
        >>> dbpedia_resource("New York City")
        'https://dbpedia.org/resource/New_York_City'
    """
    return f"https://dbpedia.org/resource/{name.strip().replace(' ', '_')}"