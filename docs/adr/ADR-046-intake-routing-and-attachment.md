# ADR-046 — Intake : coller, proposer un dossier, valider, intégrer

Status: Proposed (awaiting human dogfood)
Date: 2026-11-03

## Context

Le besoin exprimé est celui qui justifie le produit : **coller un message entier
et que l'application le rattache au bon dossier, puis l'y intègre après
validation humaine**.

Trois briques existaient déjà séparément : l'analyse déterministe de signaux
(dates, statuts, attentes, 10 opérations de proposition), la validation humaine
des propositions, et — depuis ADR-042 — une mémoire d'identité par alias. Il
manquait le **rattachement** et l'**intégration**.

Mesure avant conception, sur un portefeuille réel de 56 dossiers, avec un
routage par recouvrement de vocabulaire (idf) + identité :

| Message | Top 1 | Score | Top 2 | Verdict |
|---|---|---|---|---|
| dossier nommé dans le texte | correct | 3,44 | 0,00 | ✅ |
| même message **sans** le nom | **faux** | 1,44 | 0,00 | ❌ |
| autre dossier, vocabulaire riche | correct | 7,96 | 2,72 | ✅ |
| message thématiquement proche, 1 mot commun | **faux** | 2,91 | 1,44 | ❌ |
| message hors sujet | **faux** | 1,44 | 0,00 | ❌ |

Deuxième itération, après avoir corrigé une **double comptabilisation** : le mot
« restauration » servait à la fois de vocabulaire et d'identité (nom du projet
« ORCINVEST — **restauration** et récupération des fonds »), ce qui proposait un
dossier de restauration de société pour un message sur la restauration de
sauvegardes. Règle retenue : **seul le premier jeton identitaire du nom** (la
marque : « orcinvest », « donation ») compte comme signal fort ; les autres
mots du nom sont du vocabulaire ordinaire.

Après correction :

| Message | Résultat |
|---|---|
| dossier nommé | ✅ proposé |
| dossier non nommé, sans alias | ⏸ abstention |
| autre dossier, vocabulaire riche | ✅ proposé |
| message proche, 1 mot commun | ⏸ abstention |
| message hors sujet | ⏸ abstention |

**Aucune assignation fausse.** C'est le contrat retenu.

## Decision

### 0. Une mention n'est pas une identité (correction après usage réel)

Deuxième mesure, sur une **lettre réelle** adressée à un service social : le
routeur a présélectionné le dossier `taxe-fonciere…` parce que la lettre
mentionnait, au **cinquième paragraphe**, le règlement d'une taxe foncière
(score 7,33 ; deuxième candidat 3,56). Le sujet réel de la lettre était
ailleurs, et aucun dossier n'existait pour lui.

Un routage lexical ne peut pas distinguer une mention incidente d'une identité :
la règle a donc changé de nature. **Présélectionner est une affirmation** ; elle
exige une preuve d'identité :

- un **alias déclaré** trouvé n'importe où (c'est l'humain qui l'affirme) ;
- ou le **nom du dossier en en-tête** (≤ 35 % du texte) ou **en signature**
  (≥ 75 %).

Position mesurée : « taxe » à **57 %** (corps → mention incidente) contre
« orcinvest » à **93 %** (signature → identité). Une mention au milieu du corps
ne présélectionne plus ; le candidat reste affiché avec sa raison.

Un second défaut a été trouvé par le test de bout en bout : un alias
**multi-mots** (« aduriz bonneau ») ne pouvait jamais correspondre, la
comparaison se faisant sur des jetons isolés. Les alias sont désormais
recherchés comme **phrases normalisées** dans le texte replié.

### 0 bis. La correction est durable

Quand l'humain choisit un dossier qui n'était pas proposé, l'écran permet de
**mémoriser un alias** (typiquement le nom de l'expéditeur). Il est ajouté aux
alias du dossier au moment de l'intégration, journalisé, et le courrier suivant
du même correspondant est routé correctement sans intervention. C'est le seul
« apprentissage » du système : une **déclaration humaine**, pas un modèle.

De même, en cas d'abstention, l'écran peut **créer le dossier manquant** en un
clic puis y intégrer le message.

### 1. `route()` propose, ne décide jamais

`recall.route(root, text)` renvoie jusqu'à trois candidats
(`project_id`, `title`, `score`, `reason`, `matched`) et un champ `abstain`.
Raisons explicites : « le texte nomme « orcinvest » », « le texte contient
l'alias « bioverify » », « vocabulaire commun : … ».

Seuils explicites et paramétrables :
`ROUTE_MIN_SCORE = 2.0`, `ROUTE_MIN_MARGIN = 1.2` (deux dossiers trop proches →
abstention). Aucune valeur apprise, aucun modèle.

### 2. `POST /api/intake` est un **essai à blanc**

Entrée : `{text, source_ref?, title?}`. Sortie : routage + propositions +
`duplicate` + natures de note disponibles. **Aucune écriture** : ni signal, ni
proposition, ni note, ni journal. Vérifié par test (compteurs de journal, de
notes et de signaux avant/après).

### 3. `POST /api/intake/confirm` est le seul chemin d'écriture

Entrée : `{text, source_ref, project_id, note_kind, accept_proposals?}`.
Il exécute, dans cet ordre :

1. `signals.create_signal` — provenance : hash, sensibilité, horodatage,
   déduplication ;
2. `mutations.add_note` — la note attachée au dossier, texte verbatim ;
3. pour chaque proposition **explicitement acceptée** :
   `proposals.apply_proposal` ; les autres restent `PENDING` dans la file de
   revue existante.

Un re-collage identique renvoie la note existante (`duplicate: true`) et ne
crée pas de doublon.

### 4. Une seule surface : le panneau Signaux

« Analyser » ne publie plus directement pour un texte collé : il affiche
**Où ranger ce message ?** avec le dossier proposé et sa raison, les alternatives
cliquables, la nature de note, la liste des propositions détectées (à valider
ensuite, une par une), et un bouton **Intégrer au dossier**. Le chemin fichier
(`.eml`, `.mbox`, document) conserve le comportement antérieur : son contenu
n'est connu qu'après l'envoi.

## Consequences

Positives :

- la boucle demandée existe : coller → proposition motivée → un clic → le
  message est dans le dossier, cherchable, journalisé ;
- le pire cas est visible : quand le routage hésite, l'écran le **dit** et
  laisse choisir, au lieu de ranger silencieusement au mauvais endroit ;
- aucune mutation métier non validée : les propositions restent explicites.

Coûts et limites :

- **le routage dépend du vocabulaire déclaré**. Un dossier sans alias et sans
  nom cité ne sera pas reconnu (mesuré : abstention). C'est assumé — la curation
  des alias est le levier, pas un modèle ;
- les seuils (2.0 / 1.2) sont **choisis**, pas calibrés. Ils devront être
  validés par un jeu étiqueté (Phase 3) ;
- pas de routage sémantique : une paraphrase sans mot commun n'est pas reconnue.
  Les embeddings ne changeront ce point qu'après une baseline mesurée ;
- le fichier `.eml` ne bénéficie pas encore de l'aperçu (upload immédiat) ;
- une note très longue (> 20 000 caractères) doit être découpée.

## Alternatives écartées

- **assigner automatiquement le meilleur candidat** : mesurablement faux sur
  3 cas sur 5 ;
- **demander un modèle pour choisir le dossier** : aucun gain démontré, et un
  choix non explicable rendre la validation humaine inutile ;
- **un écran dédié « Intake »** : nouvelle surface alors que le panneau Signaux
  porte déjà le collage, la provenance et la file de revue.
