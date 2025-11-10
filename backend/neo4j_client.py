import os
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from graphdatascience import GraphDataScience
from py2neo import Graph

load_dotenv()

NEO4J_URI: str  = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
NEO4J_USER: str = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS: str = os.getenv("NEO4J_PASS", "password")

# Connessione diretta con py2neo (no session context)
driver: Graph = Graph(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS), name="neo4j")

# Client Graph Data Science
gds: GraphDataScience = GraphDataScience(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))


def run_cypher(query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Esegue una query Cypher e restituisce i risultati come lista di dizionari.

    Args:
        query: Stringa Cypher da eseguire.
        params: Parametri opzionali per la query.

    Returns:
        Lista di record, dove ogni record è un dizionario (py2neo `.data()`).
        Restituisce lista vuota se la query non produce righe.

    Esempio:
        >>> run_cypher("MATCH (n:City) RETURN n.name AS name LIMIT 2")
        [{'name': 'Paris'}, {'name': 'Berlin'}]
    """
    return driver.run(query, params).data()
    # Versione session-based (se si volesse usare il driver neo4j ufficiale):
    # with driver.session() as s:
    #     return s.run(query, params or {}).data()