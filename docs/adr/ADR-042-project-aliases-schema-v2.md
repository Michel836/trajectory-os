# ADR-042 — Alias de dossier et schéma portefeuille v2

Status: Proposed (awaiting human dogfood)
Date: 2026-11-03

## Context

Le scénario fondateur du programme V1 est « un nom de tiers → un dossier ».
Mesuré sur un portefeuille réel le 2026-11-03, ce nom n'apparaissait
**nulle part** : 0 occurrence dans `portfolio.json` (56 projets, 254 tâches),
0 dans le seul artefact d'import (370 candidats). La recherche globale
retournait `count = 0`.

Ce n'est pas un défaut de moteur : c'est une **absence de mémoire
d'identité**. Un nom de tiers connu de l'utilisateur et de son cahier
manuscrit n'a aucun endroit où exister. La recherche lexicale ne peut pas
inventer un lien qu'aucune donnée ne porte, et une similarité sémantique
(lot 3) ne le pourrait pas davantage de façon fiable.

Trois options ont été écartées :

1. écrire le nom dans la description du projet — écrase du texte libre
   existant, n'est pas structuré, et ne permet pas plusieurs synonymes ;
2. déduire l'alias d'un modèle (embeddings) — viole P07 et P13 (aucune preuve
   de qualité) et introduit une dépendance non nécessaire ;
3. créer un objet « topic » séparé — second registre, viole §15/§61.

## Decision

### 1. L'alias est un champ déclaré du projet

`Project.aliases: tuple[str, ...]` — 0 à 12 entrées, 64 caractères maximum
chacune, texte libre (pas d'identifiant slug : « nom du tiers » est valide).

L'alias est **déclaré par l'humain**, jamais inféré. C'est cette déclaration
qui autorise à le traiter comme une **identité exacte** en recherche
(score 110, au-dessus d'un titre exact à 100).

### 2. Un seul chemin d'écriture

`mutations.set_aliases(root, project_id, {"aliases": ...})`, exposé par
`POST /api/projects/<id>/aliases` et par
`mvp aliases --project ID --set "surnom, acronyme"`.
`aliases` est également un champ de `update_project` et de `create_project`.

L'opération est journalisée (`changes.jsonl`) et **idempotente** : une
réécriture équivalente après repli casse/accents ne crée pas d'entrée.

### 3. Schéma v2, migration additive et réversible

`SCHEMA_VERSION = 2`. `LEGACY_SCHEMA_VERSIONS = (1,)`.

- **Lecture** : un document v1 est accepté et surchargé en mémoire
  (`aliases = ()`). `READABLE_SCHEMA_VERSIONS = (1, 2)`.
- **Écriture** : `store.save_portfolio` détecte le v1 encore sur disque et
  prend **une** sauvegarde `pre-migration-v1` avant d'écrire. La sauvegarde est
  best-effort : elle ne peut jamais bloquer une écriture validée.
- **Rejet** : toute version inconnue échoue toujours avec
  `E_UNSUPPORTED_VERSION`. Aucune tolérance silencieuse.
- **Retour arrière** : `restore_portfolio` restaure le v1 sauvegardé ; le
  build courant relit le v1.

### 4. Repli identique au normaliseur partagé

`model.fold_text` délègue à `classification.normalize_text` (ADR-033) au lieu
de dupliquer la règle. Alias, requête de recherche et classifieur de capture ne
peuvent pas diverger sur ce qu'est « le même mot ».

## Consequences

Positives :

- le scénario « nom de tiers → dossier » devient réalisable **sans** LLM, **sans**
  embedding, **sans** nouvelle dépendance ;
- la migration est additive : aucun champ existant n'est renommé ni supprimé ;
- l'alias est auditable (journal) et affiché dans l'Inspector.

Coûts et limites :

- la déclaration est manuelle : l'utilisateur doit saisir ses synonymes. C'est
  assumé (P03/P07) ; une suggestion d'alias pourra être **proposée** plus tard
  via le modèle `Proposal`, jamais appliquée seule ;
- 12 alias par projet est une borne arbitraire mais fail-closed ; à réévaluer
  si l'usage réel la sature ;
- le v2 est un changement de contrat durable : toute régression ici touche les
  56 projets et 254 tâches réelles. D'où les tests de migration aller/retour et
  la sauvegarde automatique.

## Alternatives écartées

- champ `keywords` libre dans la description (non structuré, non requêtable) ;
- table alias séparée (`aliases.jsonl`) — un sidecar de plus à réconcilier,
  sans bénéfice pour 12 entrées par projet ;
- déduction automatique par modèle — aucune base de mesure, viole P13.
