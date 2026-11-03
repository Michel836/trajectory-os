# Règle du geste abouti

**Statut :** règle de développement, applicable à tout incrément d'interface.
**Origine :** cinq défauts trouvés par l'utilisateur en un après-midi d'usage
réel, qu'aucun des 5 392 tests automatisés n'avait vus (voir §4).

---

## 1. La règle

> **Un incrément d'interface n'est livré que si le geste de l'utilisateur,
> exécuté par lui sur ses données réelles, produit l'effet attendu — et qu'il
> le décrit en une phrase.**

Un test qui vérifie qu'une fonction existe, qu'une route répond 200 ou qu'un
gestionnaire est câblé **ne compte pas** comme preuve d'usage. Le gate reste
nécessaire ; il n'est jamais suffisant.

Corollaire : on ne dit **jamais** « c'est fonctionnel » sur la base du gate
seul. On dit « le gate est vert, le geste reste à valider ».

## 2. Les trois niveaux de preuve

| Niveau | Question | Instrument | Qui l'exécute |
|---|---|---|---|
| **L1 — le code s'exécute** | la fonction fait-elle ce qu'elle dit ? | `pytest`, Ruff, mypy | la machine |
| **L2 — la surface produit un rendu** | la vue, l'écran ou la route produit-elle quelque chose de visible ? | `scripts/mvp_ui_simulation.py` (117 surfaces), `tests/unit/test_mvp_ui_handlers.py` | la machine |
| **L3 — le geste aboutit** | l'utilisateur fait le geste et obtient l'effet attendu ? | ce document, §5 | **l'humain** |

Un incrément d'interface exige **L1 + L2 + L3**. Un incrément de domaine
(modèle, mutation, calcul) exige L1 ; L2/L3 seulement si une surface change.

## 3. La liste de contrôle avant de dire « utilisable »

Chaque ligne correspond à un défaut réellement livré. Elles doivent être
vérifiées **avant** toute annonce, pas après.

- [ ] **Le champ visible**, pas seulement l'API : l'élément que l'utilisateur voit
      (champ de recherche, filtre, bouton) trouve-t-il le résultat ?
- [ ] **L'effet visible d'un clic**, pas seulement l'état persisté : après le
      clic, *quelque chose change à l'écran* sans avoir à le chercher ?
- [ ] **Le cas réel**, pas mes fixtures : ai-je essayé avec *son* contenu
      (une lettre, un mail, un montant, un accent) ?
- [ ] **Les valeurs multi-mots et accentuées** : les alias, noms et expressions
      de plus d'un mot fonctionnent-ils ?
- [ ] **Le texte long** : un contenu de plusieurs paragraphes est-il traité
      comme tel, ou découpé en miettes ?
- [ ] **Le cas vide et le cas ambigu** : l'écran dit-il clairement « je ne sais
      pas » au lieu de deviner ?

## 4. Le registre des gestes

Chaque incrément d'interface ajoute une entrée **avant** d'être annoncé livré.
Un geste refusé par l'humain rouvre l'incrément, quel que soit l'état du gate.

| Date | Incrément | Geste (qui, quoi) | Effet attendu | Résultat observé | Verdict |
|---|---|---|---|---|---|
| 2026-11-03 | V0.8 recherche par alias | Michel : `alvis` dans le champ de recherche | le dossier ORCINVEST apparaît | **rien** — alias absent du filtre de vue | ❌ corrigé |
| 2026-11-03 | V0.8 ouvrir un dossier | Michel : clic sur « Ouvrir le dossier » | le dossier s'ouvre | **rien de visible** — vue inchangée, Inspector replié | ❌ corrigé |
| 2026-11-03 | V0.9 intake | Michel : coller une lettre, « Analyser » | dossier proposé correct | **mauvais dossier** — mention incidente prise pour une identité | ❌ corrigé |
| 2026-11-03 | V0.9 alias mémorisé | (test de bout en bout) | le courrier suivant est routé | **abstention** — alias multi-mots non reconnu | ❌ corrigé |
| 2026-11-03 | Capture rapide | Michel : coller un mail dans `c` | 2-3 propositions lisibles | **12 lignes opaques**, téléphone et e-mail proposés en tâches | ❌ corrigé |
| 2026-11-03 | Inspecteur d'un projet | Michel : ouvrir l'Inspecteur sur un dossier | la fiche du dossier | **15 champs de tâche vides**, « ProjetFait », « Notes — » alors qu'une note existe, 15 boutons, libellés anglais | ❌ corrigé |
| 2026-11-03 | Panneau Contexte | Michel : `g` puis `aduriz` | le dossier et son contenu lisible | dossier correct mais sections **« Bonjour Madame… »** (titre = 1re ligne) et **« Paristitre »** (type concaténé) | ❌ corrigé |

Ces cinq lignes sont la justification de cette règle. Elles ne doivent pas
disparaître : elles sont la preuve que L1+L2 ne suffisent pas.

## 4 bis. Gestes d'acceptation V1 — en attente de L3

Définis par l'utilisateur le 2026-11-03 comme la définition d'« utilisable ».
Les trois doivent aboutir. **Aucun n'est coché : la colonne « Observé » est
remplie par l'humain.**

| # | Geste | Attendu | L1 | L2 | L3 (observé / verdict) |
|---|---|---|---|---|---|
| G1 | Je colle un message et je valide | il atterrit dans le bon dossier, et ses **questions ouvertes** sont listées | ✅ | ✅ | ☐ |
| G2 | J'ouvre l'application | je vois **3 choses à faire**, avec le contexte à un clic | ✅ | ✅ | ☐ |
| G3 | Je tape n'importe quel mot | je retrouve le dossier en **moins de 5 s** et je reprends | ✅ | ✅ | ☐ |

Mesures de référence au 2026-11-03 (portefeuille réel, copie) :

- **G1** — dossier proposé `orcinvest` avec la raison « le texte nomme
  « orcinvest » (97 %) ; le texte contient l'alias « orc » » ; note attachée ;
  **2 questions** extraites et listées dans le contexte du dossier.
- **G2** — plan du jour vide (fenêtre de travail terminée, capacité restante
  0 min) : l'écran affiche malgré tout **3 priorités cliquables** avec leur
  effort et leur raison, plus « Estimer les tâches sans estimation ».
- **G3** — `alvis`, `bioverify`, `mollie`, `companies house` → 1 à 5 résultats
  en **6,5 à 6,8 ms**.

Limites connues, à vérifier pendant la validation : un mot qui n'existe dans
aucune donnée ni alias restera introuvable ; une question n'est pas encore
reliée à sa réponse.

## 5. Format d'une entrée

```text
Date        : AAAA-MM-JJ
Incrément   : <nom + version>
Geste       : <qui> · <action exacte> · <données utilisées>
Attendu     : <ce que l'utilisateur doit voir ou obtenir, en une phrase>
Observé     : <ce qu'il a réellement vu>
Verdict     : OK / KO (<défaut> → <correctif, commit>)
```

Un geste n'est jamais déclaré OK par un agent. La colonne « Observé » est
remplie par l'humain.

## 6. Automatisation associée

Ces trois contrôles tournent à chaque `bash scripts/quality.sh` :

| Contrôle | Fichier | Ce qu'il garantit |
|---|---|---|
| tout gestionnaire câblé est défini | `tests/unit/test_mvp_ui_handlers.py` | aucun bouton ne mène à une fonction inexistante |
| toute vue ciblée est déclarée et atteignable | idem | aucune vue fantôme, aucune vue orpheline |
| les 117 surfaces produisent un rendu visible | `scripts/mvp_ui_simulation.py` + son test | aucune vue ni overlay qui ne s'ouvre |

Ce qui **ne peut pas** être automatisé — et qui reste donc humain : le contenu,
la pertinence, le sentiment d'usage. C'est le niveau L3.

## 7. Ce que cette règle change dans le processus

À partir d'aujourd'hui, la séquence canonique devient :

```text
Issue → branche → implémentation → L1 (gate) → L2 (simulation)
      → L3 (geste humain) → revue → PR → CI → merge → évidence
```

Un incrément ne peut pas passer de L2 à la revue sans une ligne renseignée
dans le registre §4. La revue humaine vérifie cette ligne autant que le diff.
