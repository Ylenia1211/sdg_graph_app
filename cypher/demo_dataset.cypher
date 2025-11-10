////////////////////////////////////////////////////////////////////////
// RESET (opzionale)
////////////////////////////////////////////////////////////////////////
MATCH (n) DETACH DELETE n;

////////////////////////////////////////////////////////////////////////
// CONSTRAINT & INDEX
////////////////////////////////////////////////////////////////////////
CREATE CONSTRAINT stakeholder_id IF NOT EXISTS
FOR (s:Stakeholder) REQUIRE s.id IS UNIQUE;
CREATE CONSTRAINT project_id IF NOT EXISTS
FOR (p:Project) REQUIRE p.id IS UNIQUE;
CREATE CONSTRAINT sdg_code IF NOT EXISTS
FOR (g:SDG) REQUIRE g.code IS UNIQUE;
CREATE CONSTRAINT sector_name IF NOT EXISTS
FOR (sec:Sector) REQUIRE sec.name IS UNIQUE;
CREATE CONSTRAINT lod_uri IF NOT EXISTS
FOR (e:LODEntity) REQUIRE e.uri IS UNIQUE;

CREATE INDEX IF NOT EXISTS FOR (k:Keyword) ON (k.name);

////////////////////////////////////////////////////////////////////////
// SDGs
////////////////////////////////////////////////////////////////////////
UNWIND [
  ["SDG1","No Poverty"],["SDG2","Zero Hunger"],["SDG3","Good Health and Well-being"],
  ["SDG4","Quality Education"],["SDG5","Gender Equality"],["SDG6","Clean Water and Sanitation"],
  ["SDG7","Affordable and Clean Energy"],["SDG8","Decent Work and Economic Growth"],
  ["SDG9","Industry, Innovation and Infrastructure"],["SDG10","Reduced Inequalities"],
  ["SDG11","Sustainable Cities and Communities"],["SDG12","Responsible Consumption and Production"],
  ["SDG13","Climate Action"],["SDG14","Life Below Water"],["SDG15","Life on Land"],
  ["SDG16","Peace, Justice and Strong Institutions"],["SDG17","Partnerships for the Goals"]
] AS row
MERGE (:SDG {code: row[0], label: row[1]});

////////////////////////////////////////////////////////////////////////
// SECTORS
////////////////////////////////////////////////////////////////////////
UNWIND ["Energy","Mobility","Health","Education","Agriculture","ICT","Environment","Finance"] AS sec
MERGE (:Sector {name: sec});

////////////////////////////////////////////////////////////////////////
// KEYWORDS
////////////////////////////////////////////////////////////////////////
UNWIND [
 "solar","efficiency","smart-grid","green-hydrogen","emissions","iot","mobility","sensors",
 "ai","telemedicine","remote-learning","fintech","microcredit","circular-economy","recycling",
 "water","irrigation","smart-city","renewable-energy"
] AS kw
MERGE (:Keyword {name: kw});

////////////////////////////////////////////////////////////////////////
// LOD ENTITIES (demo)
////////////////////////////////////////////////////////////////////////
UNWIND [
  ["wikidata","http://www.wikidata.org/entity/Q132701","Solar energy"],
  ["dbpedia","http://dbpedia.org/resource/Renewable_energy","Renewable energy"],
  ["wikidata","http://www.wikidata.org/entity/Q3196","Hydrogen"],
  ["dbpedia","http://dbpedia.org/resource/Internet_of_things","Internet of things"],
  ["geonames","https://www.geonames.org/3169070","Torino"],
  ["geonames","https://www.geonames.org/3181928","Milano"]
] AS row
MERGE (:LODEntity {source: row[0], uri: row[1], label: row[2]});

////////////////////////////////////////////////////////////////////////
// STAKEHOLDERS (con IN_SECTOR e hasKeyword)
////////////////////////////////////////////////////////////////////////
WITH 1 AS _
CALL {
  WITH _
  UNWIND [
    ["s1","ACME Energy","azienda","Energy","Milano, IT","Utility focalizzata su rinnovabili",["solar","smart-grid","efficiency","renewable-energy"]],
    ["s2","GreenMovers","startup","Mobility","Torino, IT","Soluzioni di mobilità elettrica",["iot","mobility","sensors"]],
    ["s3","Health4All NGO","ong","Health","Roma, IT","Accesso a sanità territoriale",["telemedicine"]],
    ["s4","UniTech","università","ICT","Pisa, IT","Dip. Ingegneria con focus AI/IoT",["ai","iot"]],
    ["s5","City of Rivertown","ente pubblico","Environment","Rivertown, IT","Comune impegnato in smart city",["smart-city","recycling","circular-economy"]],
    ["s6","AgriNova","azienda","Agriculture","Parma, IT","Tecnologie irrigazione di precisione",["water","irrigation","sensors"]],
    ["s7","BlueFinance","azienda","Finance","Milano, IT","Fintech per microcredito",["fintech","microcredit"]],
    ["s8","HydroFuture","startup","Energy","Bologna, IT","Progetti green hydrogen",["green-hydrogen","emissions","mobility"]]
  ] AS row
  MERGE (s:Stakeholder {id: row[0]})
    SET s.name=row[1], s.type=row[2], s.location=row[4], s.description=row[5]
  MATCH (sec:Sector {name: row[3]})
  MERGE (s)-[:IN_SECTOR]->(sec)
  UNWIND row[6] AS kw
  MATCH (k:Keyword {name: kw})
  MERGE (s)-[:hasKeyword]->(k)
  RETURN count(*) AS _
}
RETURN _;

////////////////////////////////////////////////////////////////////////
// PROJECTS (relatedToKeyword, contributesTo)
////////////////////////////////////////////////////////////////////////
WITH 1 AS _
CALL {
  WITH _
  UNWIND [
    ["p1","Solar Schools","Installazione fotovoltaico su scuole","Torino, IT",["solar","smart-grid","efficiency","renewable-energy"],["SDG7","SDG11","SDG13"]],
    ["p2","City e-Mobility","Rete colonnine e gestione flotte e-bus","Rivertown, IT",["mobility","iot","sensors","smart-city"],["SDG11","SDG9","SDG13"]],
    ["p3","TeleHealth Rural","Telemedicina per aree rurali","Molise, IT",["telemedicine","ai"],["SDG3","SDG9"]],
    ["p4","Smart Irrigation","Irrigazione di precisione con IoT","Puglia, IT",["water","irrigation","iot","sensors"],["SDG6","SDG2","SDG9"]],
    ["p5","Circular City Lab","Piattaforma economia circolare","Rivertown, IT",["circular-economy","recycling","smart-city"],["SDG12","SDG11"]],
    ["p6","Hydrogen Pilot","Pilota idrogeno verde per trasporto","Bologna, IT",["green-hydrogen","emissions","mobility"],["SDG7","SDG13","SDG9"]]
  ] AS row
  MERGE (p:Project {id: row[0]})
    SET p.name=row[1], p.description=row[2], p.location=row[3]
  UNWIND row[4] AS kw
  MATCH (k:Keyword {name: kw})
  MERGE (p)-[:relatedToKeyword]->(k)
  UNWIND row[5] AS sdg
  MATCH (g:SDG {code: sdg})
  MERGE (p)-[:contributesTo]->(g)
  RETURN count(*) AS _
}
RETURN _;

////////////////////////////////////////////////////////////////////////
// PARTECIPAZIONI (participatesIn)
////////////////////////////////////////////////////////////////////////
UNWIND [
  ["s1","p1"],["s4","p1"],
  ["s2","p2"],["s5","p2"],["s1","p2"],
  ["s3","p3"],["s4","p3"],
  ["s6","p4"],["s4","p4"],
  ["s5","p5"],["s2","p5"],
  ["s8","p6"],["s2","p6"],["s1","p6"]
] AS row
MATCH (s:Stakeholder {id: row[0]}), (p:Project {id: row[1]})
MERGE (s)-[:participatesIn]->(p);

////////////////////////////////////////////////////////////////////////
// COLLEGAMENTI LOD (linkedTo)
////////////////////////////////////////////////////////////////////////
UNWIND [
  ["solar","http://www.wikidata.org/entity/Q40015"],
  ["renewable-energy","http://dbpedia.org/resource/Renewable_energy"],
  ["iot","http://dbpedia.org/resource/Internet_of_things"],
  ["green-hydrogen","http://www.wikidata.org/entity/Q99513382"],
  ["smart-city","http://dbpedia.org/resource/Smart_city"],
  ["mobility","http://dbpedia.org/resource/Urban_mobility"]
] AS link
MATCH (k:Keyword {name: link[0]})
MATCH (e:LODEntity {uri: link[1]})
MERGE (k)-[:linkedTo]->(e);

////////////////////////////////////////////////////////////////////////
// VERIFICHE
////////////////////////////////////////////////////////////////////////
MATCH (:Stakeholder)-[:IN_SECTOR]->(:Sector) RETURN count(*) AS in_sector;
MATCH (:Stakeholder)-[:hasKeyword]->(:Keyword) RETURN count(*) AS stk_kw;
MATCH (:Project)-[:relatedToKeyword]->(:Keyword) RETURN count(*) AS prj_kw;
MATCH (:Project)-[:contributesTo]->(:SDG) RETURN count(*) AS prj_sdg;
MATCH (:Keyword)-[:linkedTo]->(:LODEntity) RETURN count(*) AS kw_lod;
MATCH (:Stakeholder)-[:participatesIn]->(:Project) RETURN count(*) AS stk_prj;