# Phase 0 — Audit de dépôt et plan d'évolution (2026-11-03)

Audit **read-only** du dépôt au commit `7cfbde8` (branche `feat/inspired-patterns`,
depuis `main`), en réponse au document directeur v2.0. Toutes les mesures
ci-dessous sont exécutées, pas estimées.

## 1. Baseline

| Élément | Valeur mesurée |
|---|---|
| Branche / état | `feat/inspired-patterns`, aucun changement local |
| Commits | 155 (2026-08-22 → 2026-10-28) |
| `src/` | 376 fichiers .py, 135 592 lignes |
| dont `mvp/` (produit) | 40 fichiers, 24 963 lignes |
| `tests/` | 264 fichiers, 113 354 lignes |
| `docs/` | 79 fichiers .md (41 ADR), 9 853 lignes |
| Quality gate | `bash scripts/quality.sh` → **5310 passed**, Ruff clean, mypy strict clean, 349,72 s |

## 2. Constats majeurs

### C1 — Le chemin critique n'existait pas

Une recherche sur un nom de tiers connu de l'utilisateur mais absent des
données → **0 résultat**. Le mot n'apparaissait nulle part : 0 occurrence dans
`portfolio.json` (56 projets, 254 tâches), 0 dans le seul artefact d'import
(370 candidats). Aucune mémoire d'identité n'existait.

| Requête | Avant | Après V0.8 |
|---|---|---|
| nom de tiers | 0 | le dossier, via alias exact (110) |
| sigle métier (dans une source) | 0 | la source, rattachée au dossier |
| montant réel | 0 | la tâche qui le porte |
| identifiant de dossier | 11 | 11 (+ le dossier) |
| latence p50 | 11,1 ms | 6,4 ms (cache) |

### C2 — La capture violait P03

`capture.py` ne produisait que `project` ou `task` : une question, une
référence ou une donnée capturée devenait une tâche. Le contrat
`classification.NOTE_LIKE` existait mais n'avait **aucun objet durable** pour
l'accueillir (`importer` les `skip`). Traité en Phase 2 (entité `Note`), pas en
Phase 1.

### C3 — Trois modèles de domaine coexistaient

`mvp/model.py` (JSON, slugs, **en production avec les données réelles**) est
totalement isolé : aucun module hors `mvp/` ne l'importe, et `mvp/` n'importe
que `intelligence.model`. À côté : `domain/portfolio.py` (UUID + SQLite +
`RelationType` typé) et `graph/*` (graphes d'objectifs), testés mais non
branchés sur le produit. Aucune fusion n'est proposée ; la coexistence doit
être **documentée** (ADR-045 à écrire avant toute extension).

### C4 — Du code de valeur élevée déjà écrit mais non branché

- `intelligence/knowledge.py` : BM25 déterministe avec provenance, chunks,
  contradiction, fraîcheur, sensibilités, index persistable — moteur de la
  Phase 3, **non branché** ;
- `mvp/assistant.py` : réponses à citations obligatoires + repli déterministe
  — base saine pour le RAG de la Phase 5 ;
- `mvp/signals.py` : 10 opérations de proposition, filtrage de bruit, analyse
  déterministe de dates FR/EN ;
- `uistate.SEMANTIC_COLORS` + `HIGHLIGHT_COLORS` : la palette du cahier
  manuscrit existe déjà (jaune/rouge/violet/turquoise/gris).

### C5 — Le loop entrant n'a jamais tourné sur les données réelles

`inbox/` absent du data root : `signals = 0`, `sources = 0`, `changes.jsonl`
absent. Les jalons V0.4/V0.5 sont marqués « fait » (implémenté + testé) mais
leur valeur d'usage n'est pas prouvée. Cela confirme la distinction
« code écrit ≠ fonctionnalité validée » (§78).

### C6 — Dette d'atomicité (T1)

`record_and_save` écrit `portfolio.json`, puis `outcomes.jsonl`, puis
`changes.jsonl` ; le journal est explicitement *best-effort* (`journal_warning`).
Un crash entre les étapes 1 et 2 laisse une tâche `COMPLETED` sans trace de
ledger. Pas de transaction multi-fichiers. Non corrigé en Phase 1 (hors
périmètre) ; à traiter avec ADR-046 (signaux étendus) via write-ahead ou
réconciliation au démarrage.

## 3. Décisions prises (ADR)

- **ADR-042** — alias de dossier déclarés + schéma portefeuille v2, migration
  additive avec sauvegarde `pre-migration-v1`.
- **ADR-043** — Context Recall : index unifié, échelle de score explicite,
  explication du match, cache dérivé par signature de fichiers.
- **ADR-044** — resolver contextuel `mot → dossier → contexte`, panneau de
  reprise read-only.

## 4. Incréments livrés (Phase 1)

| # | Incrément | Fichiers |
|---|---|---|
| 1.1 | alias + schéma v2 + migration testée | `mvp/model.py`, `mvp/mutations.py`, `mvp/store.py` |
| 1.2 | index unifié (projets, tâches, décisions, sources, signaux, journal) | `mvp/recall.py`, `mvp/search.py` |
| 1.3 | cache par signature de fichiers | `mvp/recall.py` |
| 1.4 | resolver contextuel + `GET /api/recall` | `mvp/recall.py`, `mvp/dashboard.py` |
| 1.5 | palette groupée, panneau `g`, champ alias dans l'Inspector, CLI | `mvp/cockpit_ui.py`, `mvp/cli.py`, `mvp/visualization.py` |

## 5. Reste à faire (inchangé par rapport au plan)

- **Phase 2** — entité `Note`, capture non-tâche (P03), Living Case, grammaire
  manuscrite. ADR-045 (Living Case) et ADR-046 (grammaire) à rédiger avant.
- **Phase 3** — baseline de 100-150 requêtes, puis branchement de BM25
  existant, puis seulement embeddings derrière un flag désactivé.
- **Phase 4+** — signaux étendus, RAG sourcé, analytics.

## 6. Risques ouverts

| # | Risque | Mitigation en place |
|---|---|---|
| R1 | migration des données réelles | sauvegarde automatique + tests aller/retour |
| R3 | Living Case → second registre | ADR-042 pose `Note` comme entité attachée, pas un projet |
| R5 | embeddings sans baseline | Gate 3, mesure obligatoire |
| T1 | transaction multi-fichiers | non traitée, documentée |
| R6 | `cockpit_ui.py` monolithique | découpage reporté à 2.3, assumé dans ADR-044 |

## 7. Reproductibilité

```bash
bash scripts/quality.sh
scripts/mvp aliases --project mon-dossier --set "surnom, acronyme"
scripts/mvp recall "surnom"
scripts/mvp dashboard --root local_data/mvp   # touche « g », saisir le mot
```
