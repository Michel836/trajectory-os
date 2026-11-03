# ADR-043 — Context Recall : index unifié, explication du match, cache dérivé

Status: Proposed (awaiting human dogfood)
Date: 2026-11-03

## Context

Trois surfaces de recherche coexistaient avec trois périmètres différents :

- le filtre Liste (JS) : uniquement les objets qui passent **déjà** les filtres
  actifs — il répond « est-ce dans la vue courante ? » ;
- la palette (`/api/search`) : projets, tâches, vocabulaire d'import, signaux,
  changements ;
- l'Inspector : un seul objet.

Aucune ne répond à la question de reprise : « j'ai tapé un mot dont je me
souviens ; quel dossier est-ce, et où en est-il ? ». Deux défauts mesurés :

1. `inbox/sources.jsonl` (emails, documents) **n'était pas cherché du tout** :
   même après ingestion d'un email nommant un tiers, il restait invisible ;
2. les décisions, les montants et les attentes externes n'étaient pas indexés.

Par ailleurs, `search_memory.build_index` relisait **jusqu'à 500 artefacts
JSON d'import et re-parsait 20 000 caractères par entité à chaque recherche**,
ainsi qu'à chaque `build_views`. À 1 artefact c'est 11 ms ; le coût est linéaire
en nombre d'imports.

## Decision

### 1. Un module, `mvp/recall.py`

`recall` construit un **index dérivé** de toutes les sources canoniques :
projets (nom, objectif, description, domaine, alias), tâches (titre,
prochaine action, description, workstream, livrable, attente), décisions
(titre, question, choix, justification, options), sources entrantes (titre,
texte, expéditeur, fil), signaux (titre, texte), journal (titre, résumé), plus
le vocabulaire d'import existant.

`mvp/search.py` devient un **wrapper mince** qui conserve exactement le payload
historique `{"query", "results", "count"}` et y ajoute `dossiers`.

### 2. Échelle de score explicite et stable

| Niveau | Score | Sens |
|---|---|---|
| alias exact | 110 | identité déclarée |
| titre exact | 100 | identité textuelle |
| identifiant exact | 95 | identifiant technique (`dossier.payer`) |
| alias préfixe / contient | 90 / 70 | synonyme déclaré |
| titre préfixe / contient | 80 / 60 | |
| champ explicite | 35 | objectif, prochaine action, question, description |
| champ contextuel | 30 | domaine, projet vu depuis une tâche, expéditeur |
| texte de source | 30 | email/document |
| vocabulaire importé | 20 | mémoire d'import dérivée |
| signal | 20 | |
| journal | 10 | |

Départage : score, puis ordre de type (projet, tâche, décision, source,
signal, changement), puis `ref_id`. **Déterministe, sans horloge.**

### 3. Chaque résultat explique son match

Chaque hit porte `matched_field` et `reason` en français
(`alias`, `titre exact`, `objectif`, `prochaine action`, `texte de la source`,
`historique d'import`…). Aucun modèle n'est requis pour répondre à « pourquoi
ce résultat ? ».

### 4. Cache dérivé par signature de fichiers

L'index est mis en cache par `(racine, signature)` où la signature est composée
des `mtime_ns`/`taille` de `portfolio.json`, `changes.jsonl`,
`inbox/signals.jsonl`, `inbox/sources.jsonl` et des artefacts `imports/*.json`
(seuls des `stat`, aucun parsing). Budget : 8 entrées, éviction FIFO
déterministe, `clear_cache(root)` ciblé.

Conséquence mesurée sur le portefeuille réel (56 projets, 254 tâches) :
`build_index` 6 ms, recherche en cache **p50 6,4 ms / p95 7,1 ms**.

### 5. Fail-soft, jamais silencieux sur les bornes

Un sidecar absent, illisible ou malformé est **ignoré** : la récupération ne
peut pas être moins disponible que le portefeuille. En revanche une requête
trop longue (> 200 caractères) **échoue** (`E_MALFORMED`) au lieu d'être
tronquée.

### 6. Aucune écriture

Une recherche ne prend aucun verrou, n'écrit aucun fichier et n'ajoute aucune
entrée de journal. C'est vérifié par test comparant les octets de
`portfolio.json` avant/après et le nombre de changements.

## Consequences

Positives :

- une seule réponse à « est-ce que Trajectory connaît ce mot ? », indépendante
  des filtres d'affichage ;
- les sources, décisions et montants deviennent cherchables — le loop entrant
  (V0.4/V0.5) acquiert enfin une valeur de reprise ;
- la latence ne dépend plus du nombre d'imports.

Coûts et limites :

- l'index est reconstruit à chaque changement de fichier : à 500 artefacts le
  `stat` reste bon marché, mais un futur index persistant sera nécessaire
  (lot 3.2) ;
- l'échelle de score est **choisie**, pas apprise : elle doit être validée par
  le jeu de requêtes de référence avant toute fusion RRF ;
- pas de tolérance aux fautes (pas de trigrammes, pas de distance d'édition) :
  volontairement reporté, car non mesurable sans la baseline du lot 3.2.

## Alternatives écartées

- brancher immédiatement des embeddings : aucune baseline, aucune mesure
  (viole P13) ;
- SQLite FTS5 : dépendance et migration pour un corpus de 310 documents —
  à réévaluer au lot 3.2 ;
- indexer les objets dans le JS client : duplication de la logique métier et
  impossibilité de couvrir les sidecars.
