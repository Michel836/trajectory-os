# ADR-045 — Note attachée, schéma portefeuille v3

Status: Proposed (awaiting human dogfood)
Date: 2026-11-03

## Context

L'audit Phase 0 avait identifié le plus petit ensemble permettant la boucle
`coller → retrouver → reprendre → saisir → conserver`. Il était composé de six
éléments, dont le **premier** était un objet `Note` attaché à un dossier.

Les incréments 1.2, 1.3, 1.5 et 1.6 (alias, index unifié, resolver, ouverture
directe) ont été livrés en V0.8. Le `Note` — et donc la boucle complète — a été
reporté en « Phase 2 » et **c'était une erreur de séquencement** : sans `Note`,
un message collé ne peut pas vivre dans le dossier ; il ne peut produire que
des propositions sur des tâches existantes.

Constat mesuré sur un message réel (« AML / BioVerify » d'un cabinet comptable)
injecté dans le moteur déterministe :

- **1 proposition**, et elle était **fausse** (`RESOLVE_WAITING` sur une tâche,
  qui aurait levé une attente externe toujours valide) ;
- **0 proposition** pour l'erreur de nom, les deux questions ouvertes et la
  chaîne de dépendances — faute d'objet pour les porter ;
- le corps du message **n'était stocké nulle part** de lisible et attaché.

## Decision

### 1. `Note` est une entité de premier ordre, toujours attachée

```python
@dataclass(frozen=True)
class Note:
    note_id: str
    project_id: str            # obligatoire : jamais autonome
    text: str                  # ≤ 20 000 caractères, verbatim
    kind: str = "NOTE"         # NOTE/FACT/QUESTION/ANSWER/DECISION/REFERENCE/EVENT
    captured_at: str = ""
    source_ref: str = ""
    content_hash: str = ""     # déduplication
    sensitivity: str = "INTERNAL"
```

Une note **n'a pas de statut** (ce n'est pas du travail), **n'a pas d'enfants**
(ce n'est pas un second registre de projets), et son texte n'est jamais réécrit
implicitement.

### 2. Trois mutations validées, journalisées

`mutations.add_note` / `update_note` / `delete_note`. Le journal distingue
l'objet `note` (`OBJECT_NOTE`), avec `KIND_CAPTURE`, `KIND_UPDATE`,
`KIND_DELETE`. `add_note` est **idempotent par `content_hash`** dans un même
dossier : re-coller le même message renvoie la note existante.

### 3. Schéma v3, migration additive et réversible

`SCHEMA_VERSION = 3`, `LEGACY_SCHEMA_VERSIONS = (1, 2)`.
`Portfolio.notes` est additif ; un document v2 se lit et se met à niveau en
mémoire ; la première écriture prend une sauvegarde `pre-migration-v2` (même
mécanique que v1). Une version inconnue échoue toujours.

### 4. La note est retrouvable et consultable

`recall` l'indexe (`kind = "note"`, champ `note` à 35, `provenance` à 10) : le
message devient cherchable par son contenu et remonte dans le panneau
**Contexte du dossier**. C'est ce qui manquait pour que « bioverify » mène
quelque part.

## Consequences

Positives :

- un message collé **reste lisible dans le dossier**, avec sa provenance
  (`source_ref`, hash) et sa date de capture ;
- la distinction P03 est enfin matérialisée : une information n'est plus
  condamnée à devenir une tâche ;
- les questions ouvertes ont un objet (`kind = QUESTION`) au lieu d'être
  déguisées en tâches.

Coûts et limites :

- **schéma v3** : touche les 56 projets réels. Mitigé par la sauvegarde
  automatique, les tests de migration et le caractère additif ;
- une note n'est **pas** un fil de discussion : pas de réponse liée, pas de
  résolution. `kind = ANSWER` existe mais le lien question → réponse
  (`responds_to`) reste à faire (ADR-046 du programme, non pris) ;
- pas de `review_at` : une question ouverte ne relance pas automatiquement. Elle
  remonte par la vue En attente si elle porte une tâche, sinon par la recherche ;
- le texte est borné à 20 000 caractères : un très long fil doit être découpé.

## Alternatives écartées

- **réutiliser `Task.description`** : écrase du texte existant, un seul
  emplacement par objet, aucune provenance, aucune datation ;
- **un sidecar `notes.jsonl`** : un fichier de plus sans intégrité
  référentielle avec le projet, et une seconde vérité à réconcilier ;
- **un projet « Notes »** : viole §15/§61 (second registre) ;
- **attendre la Phase 2 complète (Living Case)** : c'est précisément ce report
  qui a produit l'écart constaté.
