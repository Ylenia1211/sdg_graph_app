# load_demo_with_py2neo.py
# ------------------------------------------------------------
# Caricamento dati demo nel Knowledge Graph con py2neo (Neo4j 5)
# Requisiti: pip install py2neo python-dotenv
# Variabili: NEO4J_URI, NEO4J_USER, NEO4J_PASS (facoltative)
# ------------------------------------------------------------
import os
from py2neo import Graph
from dotenv import load_dotenv, find_dotenv

# Carica .env se presente (prova sia relative path sia default)
load_dotenv(find_dotenv() or "../.env")

NEO4J_URI  = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASS", "master-cross-arrest")
RESET_ALL  = os.getenv("RESET_ALL", "false").lower() in ("1","true","yes")

print(f"Connecting to {NEO4J_URI} as {NEO4J_USER} ...")
graph = Graph(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

# ------------------------- RESET (opzionale) -------------------------
if RESET_ALL:
    print("RESET: deleting all nodes and relationships...")
    graph.run("MATCH (n) DETACH DELETE n")

# ------------------------- CONSTRAINTS / INDEX -------------------------
print("Creating constraints & indexes (IF NOT EXISTS)...")

# === Uniqueness constraints (identità delle entità) ===
graph.run("""
CREATE CONSTRAINT stakeholder_id IF NOT EXISTS
FOR (s:Stakeholder) REQUIRE s.id IS UNIQUE
""")
graph.run("""
CREATE CONSTRAINT project_id IF NOT EXISTS
FOR (p:Project) REQUIRE p.id IS UNIQUE
""")
graph.run("""
CREATE CONSTRAINT sdg_code IF NOT EXISTS
FOR (g:SDG) REQUIRE g.code IS UNIQUE
""")
graph.run("""
CREATE CONSTRAINT sector_name IF NOT EXISTS
FOR (sec:Sector) REQUIRE sec.name IS UNIQUE
""")
graph.run("""
CREATE CONSTRAINT keyword_name IF NOT EXISTS
FOR (k:Keyword) REQUIRE k.name IS UNIQUE
""")
graph.run("""
CREATE CONSTRAINT lod_uri IF NOT EXISTS
FOR (e:LODEntity) REQUIRE e.uri IS UNIQUE
""")

# === Property existence (SOLO dove è davvero necessario) ===
graph.run("""
CREATE CONSTRAINT stakeholder_name_exists IF NOT EXISTS
FOR (s:Stakeholder) REQUIRE s.name IS NOT NULL
""")
# Se 'type' non è sempre presente per Stakeholder, commenta il vincolo seguente:
graph.run("""
CREATE CONSTRAINT stakeholder_type_exists IF NOT EXISTS
FOR (s:Stakeholder) REQUIRE s.type IS NOT NULL
""")
graph.run("""
CREATE CONSTRAINT project_name_exists IF NOT EXISTS
FOR (p:Project) REQUIRE p.name IS NOT NULL
""")
# Sector/Keyword hanno già UNIQUE su name; l'existence su name è ridondante ma innocua
graph.run("""
CREATE CONSTRAINT sector_name_exists IF NOT EXISTS
FOR (sec:Sector) REQUIRE sec.name IS NOT NULL
""")
graph.run("""
CREATE CONSTRAINT keyword_name_exists IF NOT EXISTS
FOR (k:Keyword) REQUIRE k.name IS NOT NULL
""")
# ID/codici già garantiti dai UNIQUE — questi existence sono opzionali:
graph.run("""
CREATE CONSTRAINT stakeholder_id_exists IF NOT EXISTS
FOR (s:Stakeholder) REQUIRE s.id IS NOT NULL
""")
graph.run("""
CREATE CONSTRAINT project_id_exists IF NOT EXISTS
FOR (p:Project) REQUIRE p.id IS NOT NULL
""")
graph.run("""
CREATE CONSTRAINT sdg_code_exists IF NOT EXISTS
FOR (g:SDG) REQUIRE g.code IS NOT NULL
""")
graph.run("""
CREATE CONSTRAINT lod_uri_exists IF NOT EXISTS
FOR (e:LODEntity) REQUIRE e.uri IS NOT NULL
""")

# === Full-text indexes per ricerche libere ===
graph.run("""
CREATE FULLTEXT INDEX ft_stakeholder_name_desc IF NOT EXISTS
FOR (s:Stakeholder) ON EACH [s.name, s.description]
""")
graph.run("""
CREATE FULLTEXT INDEX ft_project_name_desc IF NOT EXISTS
FOR (p:Project) ON EACH [p.name, p.description, p.location]
""")
graph.run("""
CREATE FULLTEXT INDEX ft_keyword_name IF NOT EXISTS
FOR (k:Keyword) ON EACH [k.name]
""")
# LODEntity può non avere label/source → fulltext utile ma non obbligatorio
graph.run("""
CREATE FULLTEXT INDEX ft_lod_label_source IF NOT EXISTS
FOR (e:LODEntity) ON EACH [e.label, e.source]
""")

# ------------------------- DATASETS -------------------------
SDGS = [
  ("SDG1","No Poverty"),("SDG2","Zero Hunger"),("SDG3","Good Health and Well-being"),
  ("SDG4","Quality Education"),("SDG5","Gender Equality"),("SDG6","Clean Water and Sanitation"),
  ("SDG7","Affordable and Clean Energy"),("SDG8","Decent Work and Economic Growth"),
  ("SDG9","Industry, Innovation and Infrastructure"),("SDG10","Reduced Inequalities"),
  ("SDG11","Sustainable Cities and Communities"),("SDG12","Responsible Consumption and Production"),
  ("SDG13","Climate Action"),("SDG14","Life Below Water"),("SDG15","Life on Land"),
  ("SDG16","Peace, Justice and Strong Institutions"),("SDG17","Partnerships for the Goals"),
]

SECTORS = ["Energy","Mobility","Health","Education","Agriculture","ICT","Environment","Finance"]

KEYWORDS = [
 "solar","efficiency","smart-grid","green-hydrogen","emissions","iot","mobility","sensors",
 "ai","telemedicine","remote-learning","fintech","microcredit","circular-economy","recycling",
 "water","irrigation","smart-city","renewable-energy"
]

LODS = [
  ("wikidata","http://www.wikidata.org/entity/Q132701","Solar energy"),
  ("dbpedia","http://dbpedia.org/resource/Renewable_energy","Renewable energy"),
  ("wikidata","http://www.wikidata.org/entity/Q3196","Hydrogen"),
  ("dbpedia","http://dbpedia.org/resource/Internet_of_things","Internet of things"),
  ("geonames","https://www.geonames.org/3169070","Torino"),
  ("geonames","https://www.geonames.org/3181928","Milano"),
]

STAKEHOLDERS = [
  ("s1","ACME Energy","azienda","Energy","Milano, IT","Utility focalizzata su rinnovabili",
   ["solar","smart-grid","efficiency","renewable-energy"]),
  ("s2","GreenMovers","startup","Mobility","Torino, IT","Soluzioni di mobilità elettrica",
   ["iot","mobility","sensors"]),
  ("s3","Health4All NGO","ong","Health","Roma, IT","Accesso a sanità territoriale",
   ["telemedicine"]),
  ("s4","UniTech","università","ICT","Pisa, IT","Dip. Ingegneria con focus AI/IoT",
   ["ai","iot"]),
  ("s5","City of Rivertown","ente pubblico","Environment","Rivertown, IT","Comune impegnato in smart city",
   ["smart-city","recycling","circular-economy"]),
  ("s6","AgriNova","azienda","Agriculture","Parma, IT","Tecnologie irrigazione di precisione",
   ["water","irrigation","sensors"]),
  ("s7","BlueFinance","azienda","Finance","Milano, IT","Fintech per microcredito",
   ["fintech","microcredit"]),
  ("s8","HydroFuture","startup","Energy","Bologna, IT","Progetti green hydrogen",
   ["green-hydrogen","emissions","mobility"]),
]

PROJECTS = [
  ("p1","Solar Schools","Installazione fotovoltaico su scuole","Torino, IT",
   ["solar","smart-grid","efficiency","renewable-energy"], ["SDG7","SDG11","SDG13"]),
  ("p2","City e-Mobility","Rete colonnine e gestione flotte e-bus","Rivertown, IT",
   ["mobility","iot","sensors","smart-city"], ["SDG11","SDG9","SDG13"]),
  ("p3","TeleHealth Rural","Telemedicina per aree rurali","Molise, IT",
   ["telemedicine","ai"], ["SDG3","SDG9"]),
  ("p4","Smart Irrigation","Irrigazione di precisione con IoT","Puglia, IT",
   ["water","irrigation","iot","sensors"], ["SDG6","SDG2","SDG9"]),
  ("p5","Circular City Lab","Piattaforma economia circolare","Rivertown, IT",
   ["circular-economy","recycling","smart-city"], ["SDG12","SDG11"]),
  ("p6","Hydrogen Pilot","Pilota idrogeno verde per trasporto","Bologna, IT",
   ["green-hydrogen","emissions","mobility"], ["SDG7","SDG13","SDG9"]),
]

PARTICIPATIONS = [
  ("s1","p1"),("s4","p1"),
  ("s2","p2"),("s5","p2"),("s1","p2"),
  ("s3","p3"),("s4","p3"),
  ("s6","p4"),("s4","p4"),
  ("s5","p5"),("s2","p5"),
  ("s8","p6"),("s2","p6"),("s1","p6"),
]

KEYWORD_LOD_LINKS = [
  ("solar","http://www.wikidata.org/entity/Q132701"),
  ("renewable-energy","http://dbpedia.org/resource/Renewable_energy"),
  ("iot","http://dbpedia.org/resource/Internet_of_things"),
  ("green-hydrogen","http://www.wikidata.org/entity/Q3196"),
  ("smart-city","http://dbpedia.org/resource/Smart_city"),
  ("mobility","http://dbpedia.org/resource/Urban_mobility"),
]

# ------------------------- LOAD FUNCTIONS -------------------------
def load_sdgs(graph: Graph):
    tx = graph.begin()
    tx.run("""
    UNWIND $rows AS row
    MERGE (g:SDG {code: row.code})
      ON CREATE SET g.label = row.label
      ON MATCH  SET g.label = coalesce(row.label, g.label)
    """, parameters={"rows":[{"code":c,"label":l} for c,l in SDGS]})
    graph.commit(tx)

def load_sectors(graph: Graph):
    tx = graph.begin()
    tx.run("""
    UNWIND $rows AS name
    MERGE (:Sector {name: name})
    """, parameters={"rows": SECTORS})
    graph.commit(tx)

def load_keywords(graph: Graph):
    tx = graph.begin()
    tx.run("""
    UNWIND $rows AS name
    MERGE (:Keyword {name: name})
    """, parameters={"rows": KEYWORDS})
    graph.commit(tx)

def load_lod_entities(graph: Graph):
    tx = graph.begin()
    tx.run("""
    UNWIND $rows AS r
    MERGE (e:LODEntity {uri: r.uri})
      ON CREATE SET e.source = r.source, e.label = r.label
      ON MATCH  SET e.source = coalesce(r.source, e.source),
                  e.label  = coalesce(r.label, e.label)
    """, parameters={"rows":[{"source":s,"uri":u,"label":l} for s,u,l in LODS]})
    graph.commit(tx)

def load_stakeholders(graph: Graph):
    tx = graph.begin()
    # nodi + IN_SECTOR
    tx.run("""
    UNWIND $rows AS r
    MERGE (s:Stakeholder {id: r.id})
      ON CREATE SET s.name=r.name, s.type=r.type, s.location=r.location, s.description=r.description
      ON MATCH  SET s.name=r.name, s.type=r.type, s.location=r.location, s.description=r.description
    MERGE (sec:Sector {name: r.sector})
    MERGE (s)-[:IN_SECTOR]->(sec)
    """, parameters={"rows":[{
        "id":sid, "name":name, "type":typ, "sector":sector,
        "location":loc, "description":desc
    } for sid,name,typ,sector,loc,desc,_ in STAKEHOLDERS]})
    # hasKeyword
    tx.run("""
    UNWIND $rows AS r
    MATCH (s:Stakeholder {id:r.id})
    UNWIND r.keywords AS kw
    MERGE (k:Keyword {name: kw})
    MERGE (s)-[:hasKeyword]->(k)
    """, parameters={"rows":[{"id":sid, "keywords":kws} for sid,_,_,_,_,_,kws in STAKEHOLDERS]})
    graph.commit(tx)

def load_projects(graph: Graph):
    tx = graph.begin()
    # nodi
    tx.run("""
    UNWIND $rows AS r
    MERGE (p:Project {id:r.id})
      ON CREATE SET p.name=r.name, p.description=r.description, p.location=r.location
      ON MATCH  SET p.name=r.name, p.description=r.description, p.location=r.location
    """, parameters={"rows":[{
        "id":pid, "name":name, "description":desc, "location":loc
    } for pid,name,desc,loc,_,_ in PROJECTS]})
    # relatedToKeyword
    tx.run("""
    UNWIND $rows AS r
    MATCH (p:Project {id:r.id})
    UNWIND r.keywords AS kw
    MERGE (k:Keyword {name: kw})
    MERGE (p)-[:relatedToKeyword]->(k)
    """, parameters={"rows":[{"id":pid, "keywords":kws} for pid,_,_,_,kws,_ in PROJECTS]})
    # contributesTo
    tx.run("""
    UNWIND $rows AS r
    MATCH (p:Project {id:r.id})
    UNWIND r.sdgs AS code
    MERGE (g:SDG {code: code})
    MERGE (p)-[:contributesTo]->(g)
    """, parameters={"rows":[{"id":pid, "sdgs":sdgs} for pid,_,_,_,_,sdgs in PROJECTS]})
    graph.commit(tx)

def load_participations(graph: Graph):
    tx = graph.begin()
    tx.run("""
    UNWIND $rows AS r
    MATCH (s:Stakeholder {id:r.sid})
    MATCH (p:Project {id:r.pid})
    MERGE (s)-[:participatesIn]->(p)
    """, parameters={"rows":[{"sid":sid,"pid":pid} for sid,pid in PARTICIPATIONS]})
    graph.commit(tx)

def load_keyword_lod_links(graph: Graph):
    tx = graph.begin()
    tx.run("""
    UNWIND $rows AS r
    MATCH (k:Keyword {name:r.keyword})
    MATCH (e:LODEntity {uri:r.uri})
    MERGE (k)-[:linkedTo]->(e)
    """, parameters={"rows":[{"keyword":kw,"uri":uri} for kw,uri in KEYWORD_LOD_LINKS]})
    graph.commit(tx)

# ------------------------- EXECUTE -------------------------
print("Loading SDGs...")
load_sdgs(graph)
print("Loading Sectors...")
load_sectors(graph)
print("Loading Keywords...")
load_keywords(graph)
print("Loading LOD Entities...")
load_lod_entities(graph)
print("Loading Stakeholders (with IN_SECTOR and hasKeyword)...")
load_stakeholders(graph)
print("Loading Projects (with relatedToKeyword and contributesTo)...")
load_projects(graph)
print("Creating participatesIn relationships...")
load_participations(graph)
print("Linking Keywords to LOD Entities...")
load_keyword_lod_links(graph)

# ------------------------- CHECKS -------------------------
print("Running quick checks...")
res = graph.run("""
RETURN
  size([(s:Stakeholder)-[:IN_SECTOR]->(:Sector) | 1]) AS in_sector,
  size([(s:Stakeholder)-[:hasKeyword]->(:Keyword) | 1]) AS stk_kw,
  size([(p:Project)-[:relatedToKeyword]->(:Keyword) | 1]) AS prj_kw,
  size([(p:Project)-[:contributesTo]->(:SDG) | 1]) AS prj_sdg,
  size([(k:Keyword)-[:linkedTo]->(:LODEntity) | 1]) AS kw_lod,
  size([(s:Stakeholder)-[:participatesIn]->(:Project) | 1]) AS stk_prj
""").data()[0]
print("Edges count:", res)