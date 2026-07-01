# Stakeholder Knowledge Graph & Relationship Exploration

Applicazione interattiva per la **mappatura, l’arricchimento semantico e l’analisi delle relazioni** tra stakeholder, progetti, parole chiave e Obiettivi di Sviluppo Sostenibile (SDG).  
Il sistema aiuta a **comprendere, spiegare e prevedere** collaborazioni potenziali all’interno di ecosistemi territoriali e tematici.

L’app è composta da:

- **Backend REST** in Python + Neo4j (con Graph Data Science),
- **Pipeline NLP + Linked Open Data** (Wikidata / DBpedia / GeoNames),
- **Frontend interattivo** sviluppato in Streamlit per esplorazione, editing e analisi.

L’obiettivo è **mappare ecosistemi collaborativi** e suggerire nuove relazioni sulla base di:
- similarità semantiche tra descrizioni e parole chiave,
- struttura del grafo (co-partecipazioni, domini tematici),
- **Link Prediction** e embedding **FastRP**.

<img width="1356" height="697" alt="image" src="https://github.com/user-attachments/assets/2569757e-a467-40b4-9bda-f4ee333938e7" />

---


## Obiettivi

- Rappresentare Stakeholder e Progetti come grafo ricco e navigabile.
- Evidenziare **temi**, **territori** e **settori** condivisi.
- Arricchire nodi e keyword tramite **collegamento a fonti LOD**.
- Misurare prossimità e affinità tra attori.
- Suggerire collaborazioni tramite:
  - **FastRP + Cosine Similarity**
  - **Modello di Link Prediction (GDS Pipeline)**
  - **Similarità Jaccard basata su co-progetti**

---

## Architettura

```
(Frontend Streamlit) ←→ (REST API Flask) ←→ (Neo4j + GDS)
                            ↓
                     (Linked Open Data)
   Wikidata / DBpedia Spotlight / GeoNames / Sentence Embeddings
```

---

##  Struttura del progetto

```
backend/
├─ app.py                     → API REST Flask
├─ neo4j_client.py            → Connessioni DB + GDS
├─ helpers_lod.py             → Normalizzazione e mapping LOD
├─ lod_linking.py             → Estrazione termini + linking semantico
├─ lod.py                     → Utility Wikidata/DBpedia/GeoNames
└─ requirements.txt

streamlit_app/
├─ pages/
│  ├─ inserimento_stakeholder.py   → CRUD stakeholder + keyword + LOD
│  ├─ predizioni_stakeholder.py    → Similarità e suggerimenti Top-K
│  ├─ dashboard.py                 → KPI, matrici, overview rete
│  ├─ project_form.py              → Gestione progetti e collegamenti
│  └─ lod_linking.py               → Interfaccia linking semantico
│
├─ utils.py                    → Sessione HTTP verso backend
├─ cypher/load_demo_with_py2neo3.py           → Caricamento dataset di esempio
└─ .env
```

---

##  Backend Knowledge Graph

### Modello dati

**Nodi**
```
Stakeholder(id, name, type, location, description)
Project(id, name, description, location, code?)
Keyword(name)
SDG(code)
Sector(name)
LODEntity(uri, label, source)
```

**Relazioni**
```
(:Stakeholder)-[:participatesIn]->(:Project)
(:Project)-[:relatedToKeyword]->(:Keyword)
(:Stakeholder)-[:hasKeyword]->(:Keyword)
(:Project)-[:contributesTo]->(:SDG)
(:Stakeholder)-[:IN_SECTOR]->(:Sector)
(:Keyword)-[:linkedTo]->(:LODEntity)
(:Stakeholder)-[:COLLAB]->(:Stakeholder)   # usata per Link Prediction
```

### Pipeline NLP & LOD

- Estrazione di termini rilevanti dal testo (NER + noun chunks)
- Embedding semantici: **Sentence-Transformers (DistilUSE)**
- Fuzzy matching robusto: **RapidFuzz**
- Linking:
  - Wikidata → ricerca + disambiguazione SPARQL
  - DBpedia Spotlight (IT) → estrazione concetti
  - GeoNames → riconoscimento località reali

### Graph Data Science

- Proiezioni del grafo per analisi tematica
- Embedding **FastRP** scritti nel DB (`embedding_fastrp`)
- Pipeline LP (train → test → store → predict/write)
- Similarità (cosine, jaccard)

---

##  Installazione

### 1. Clona il repository
```bash
git clone https://github.com/Ylenia1211/sdg_graph_app.git
cd sdg_graph_app
```

### 2. Avvia Neo4j con GDS installato
```bash
python cypher/load_demo_with_py2neo3.py
```

### 3. Configura variabili ambiente
```
BACKEND_URL=http://localhost:8000
NEO4J_URI=neo4j://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASS=password
```

### 4. Avvia Backend
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

### 5. Avvia Frontend
```bash
cd ../streamlit_app
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run home.py
```

---

## 📄 Licenza

Da definire.

Rilascio: 30/09/2025
