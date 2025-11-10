from pydantic import BaseModel, Field, AnyUrl
from typing import Literal, Optional, List, Union


class LodLink(BaseModel):
    """Rappresenta un collegamento a una risorsa Linked Open Data (Wikidata, DBpedia Spotlight, GeoNames).

    Attributi:
        source (Literal): Sorgente LOD. Uno tra "Wikidata", "DBpedia Spotlight", "GeoNames".
        term (Optional[str]): Termine originale estratto o cercato.
        label (Optional[str]): Etichetta leggibile.
        qid (Optional[Union[str,int]]): Identificatore della risorsa.
        description (Optional[str]): Descrizione breve.
        link (Optional[Union[str,AnyUrl]]): URL canonico della risorsa.
        score (Optional[float]): Punteggio di confidenza (se fornito dalla sorgente).
        country (Optional[str]): Paese associato (ISO-3166 alfa-2 consigliato, ma non imposto).
        latitude (Optional[float]): Latitudine decimale.
        longitude (Optional[float]): Longitudine decimale.
        types (Optional[Union[str, List[str]]]): Tipi/ontologie associati.

    Esempio:
        >>> LodLink(source='Wikidata', qid='Q90', label='Paris')
    """
    source: Literal["Wikidata", "DBpedia Spotlight", "GeoNames"]
    term: Optional[str] = None
    label: Optional[str] = None
    qid: Optional[Union[str, int]] = None  # QID Wikidata o geonameId
    description: Optional[str] = None
    link: Optional[Union[str, AnyUrl]] = None  # URL Wikidata/DBpedia/GeoNames
    score: Optional[float] = None
    country: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    types: Optional[Union[str, List[str]]] = None


class StakeholderIn(BaseModel):
    """Rappresenta uno stakeholder (attore) con metadati opzionali e collegamenti LOD.

    Ambiguità note:
        - type (str):  (es. 'azienda', 'ONG', 'PA').
        - sector (str): un codice/tassonomia; mantenuto come str.

    Attributi:
        id (str): Identificatore interno.
        name (str): Nome visualizzato.
        type (str): Tipo di stakeholder. Vedi ambiguità.
        sector (str): Settore di appartenenza. Vedi ambiguità.
        location (Optional[str]): Localizzazione testuale.
        description (Optional[str]): Descrizione libera.
        keywords (List[str]): Parole chiave.
        lod_links (List[LodLink]): Collegamenti LOD.
        wikidata_qids (List[str]): QID Wikidata collegati.
        dbpedia_uris (List[str]): URI DBpedia collegati.
        geonames_ids (List[str]): ID GeoNames collegati.
        mode (Optional[str]): Modalità opzionale usata dal frontend.

    Esempio:
        >>> StakeholderIn(id='st1', name='Green Co', type='azienda', sector='energia')
    """
    id: str
    name: str
    type: str
    sector: str
    location: Optional[str] = None
    description: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)

    # --- nuovi campi per LOD ---
    lod_links: List[LodLink] = Field(default_factory=list)
    wikidata_qids: List[str] = Field(default_factory=list)
    dbpedia_uris: List[str] = Field(default_factory=list)
    geonames_ids: List[str] = Field(default_factory=list)
    # Modalità opzionale usata dal frontend
    mode: Optional[str] = None


class ProjectIn(BaseModel):
    """Rappresenta un progetto e le sue relazioni con gli stakeholder, con metadati e LOD.

    Ambiguità note:
        - stakeholders (List[str]): contiene ID di stakeholder; non è un riferimento tipizzato forte.
        - sdgs (List[str]): attesi codici tipo 'SDG7', ma lasciati generici.

    Attributi:
        id (str): Identificatore del progetto.
        name (str): Nome del progetto.
        description (Optional[str]): Descrizione.
        location (Optional[str]): Localizzazione.
        stakeholders (List[str]): Elenco di ID di stakeholder.
        keywords (List[str]): Parole chiave.
        sdgs (List[str]): Obiettivi di sviluppo sostenibile associati (es. "SDG7").
        lod_links (List[LodLink]): Collegamenti LOD.
        wikidata_qids (List[str]): QID Wikidata collegati.
        dbpedia_uris (List[str]): URI DBpedia collegati.
        geonames_ids (List[str]): ID GeoNames collegati.
        mode (Optional[str]): Modalità opzionale usata dal frontend.

    Esempio:
        >>> ProjectIn(id='p1', name='Solar Farm', stakeholders=['st1'], sdgs=['SDG7'])
    """
    id: str
    name: str
    description: Optional[str] = None
    location: Optional[str] = None
    stakeholders: List[str] = Field(default_factory=list)  # stakeholder ids
    keywords: List[str] = Field(default_factory=list)
    sdgs: List[str] = Field(default_factory=list)  # es. ["SDG7","SDG13"]

    # --- nuovi campi per LOD ---
    lod_links: List[LodLink] = Field(default_factory=list)
    wikidata_qids: List[str] = Field(default_factory=list)
    dbpedia_uris: List[str] = Field(default_factory=list)
    geonames_ids: List[str] = Field(default_factory=list)
    # Modalità opzionale usata dal frontend
    mode: Optional[str] = None






