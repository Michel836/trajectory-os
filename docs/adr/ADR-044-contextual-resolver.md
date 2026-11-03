# ADR-044 — Resolver contextuel et surface de reprise

Status: Proposed (awaiting human dogfood)
Date: 2026-11-03

## Context

ADR-043 donne une liste de résultats classés. Ce n'est pas encore ce qui permet
de **reprendre** un dossier : l'utilisateur doit ensuite ouvrir l'objet,
reconstituer mentalement l'état, retrouver les attentes, les sources et la
prochaine action. `insights.explain_project` répond déjà à « où en est ce
projet et pourquoi » pour **un** projet connu ; rien ne reliait un mot à ce
projet.

## Decision

### 1. `recall.resolve(root, query)` — du mot au dossier

Chaîne : requête → index (ADR-043) → agrégation par projet → contexte borné par
dossier. Un « dossier » est ici une **projection** du projet canonique, pas un
nouvel objet : mêmes identifiants, mêmes tâches, mêmes dépendances, mêmes
règles de validation (conforme §15).

### 2. Contenu du contexte, explicitement étiqueté

Par dossier : identité (nom, alias, statut, domaine, échéance), santé et
progression calculées, compteurs, prochaines actions classées, blocages,
attentes, risques, changements récents, signaux, sources liées, et les
correspondances (`matched`) avec leur raison.

Le payload porte `canonical: false` : c'est une **vue dérivée**. Elle ne
remplace ni l'état du portefeuille, ni une réponse RAG sourcée.

### 3. Ordre et bornes

Les dossiers sont ordonnés par meilleur score de correspondance (puis
`project_id`) ; 3 par défaut, 5 au maximum, 8 éléments par section. Un `ref_id`
de projet inconnu est ignoré, jamais fabriqué.

### 4. Surface

- `GET /api/recall?q=…&limit=…` ;
- palette : les 3 premiers dossiers apparaissent en tête, avec un item
  « contexte » ;
- panneau **Contexte du dossier** (raccourci `g`, commande « Contexte d'un
  dossier »), avec pour chaque dossier : prochaines actions, blocages,
  attentes, sources, signaux et « pourquoi ces résultats » ;
- action « Ajouter une information » qui ouvre la capture depuis le contexte ;
- `mvp recall "surnom"` en CLI.

### 5. Aucune mutation implicite

Ouvrir, résoudre ou capturer depuis le panneau ne modifie pas les attributs
métier. Seule une action explicite (capture confirmée, édition, acceptation de
proposition) écrit, via `mutations`.

### 6. Découpage du monolithe : différé, assumé

Le panneau est ajouté à `cockpit_ui.py` (5 400 lignes). Le découpage du
monolithe était prévu à l'incrément 2.3 ; l'ajouter maintenant aurait mélangé
un refactor structurel et un ajout fonctionnel dans le même lot. Le panneau
n'introduit qu'un overlay et des fonctions JS autonomes (`openRecall`,
`runRecall`, `recallHtml`), isolables lors du découpage.

## Consequences

Positives :

- la boucle `mot → dossier → contexte → reprise` existe et est mesurable ;
- le panneau est read-only, donc sans risque d'écriture non autorisée ;
- l'explicabilité du match reste visible à côté du contexte.

Coûts et limites :

- `resolve` recalcule `explain_project` par dossier (readiness + priorité) :
  mesuré à **31,7 ms** pour 1 dossier sur les données réelles, sous le budget
  P95 de 300 ms, mais à surveiller si l'on affiche 5 dossiers ;
- le contexte ne contient **aucune note libre** : l'objet `Note` relève de la
  Phase 2 (Living Case) ;
- la qualité du resolver dépend entièrement de la qualité des correspondances :
  sans alias déclaré, le mot reste introuvable. Ce n'est pas un défaut du
  resolver mais son contrat.

## Alternatives écartées

- un écran « dossier vivant » complet dès maintenant : duplication de
  l'Inspector avant d'avoir l'entité `Note` ;
- une synthèse générée par LLM : aucune base de mesure, et une synthèse non
  sourcée ne peut pas être présentée comme un fait (§21, §37) ;
- un endpoint qui écrit (ex. « ajouter une note depuis le contexte ») : la
  capture existe déjà et exige une confirmation explicite.
