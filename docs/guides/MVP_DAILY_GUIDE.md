# TrajectoryOS — Guide quotidien du MVP

Ce guide décrit l’usage quotidien du MVP personnel (V0.7).  
Il ne remplace pas les ADR : il explique comment travailler avec le cockpit.

---

## 1. Démarrer

```bash
scripts/mvp dashboard
```

Le cockpit s’ouvre sur `http://127.0.0.1:8787/`.

Pour utiliser un autre portefeuille :

```bash
scripts/mvp dashboard --root chemin/vers/donnees
```

---

## 2. Les quatre espaces

### Aujourd’hui

- plan du jour ;
- agenda ;
- santé du portefeuille ;
- changements du jour ;
- base recommandée (readiness + priorité) ;
- tâches prêtes sans estimation.

### Entrée

- **Signaux** : emails, documents, notes, textes collés ;
- **Inbox** : projets parqués (`IDEA` / `SOMEDAY`) ;
- **En attente** : relances ;
- **Décisions** : registre des choix.

### Portefeuille

- liste, Kanban, WBS, graphe, Gantt ;
- vues avancées derrière **Plus**.

### Revue

- semaine, demain, progression, activité ;
- **Changements** : journal et annulations ;
- **Dette du portefeuille** : projets en veille, tâches sans estimation, doublons, attentes anciennes ;
- **Corbeille et sauvegardes** ;
- **Revue projets** : revue guidée, un projet à la fois.

---

## 3. Routine du matin

1. Ouvrir **Aujourd’hui**.
2. Lire la carte **Santé du portefeuille**.
3. Choisir **3 tâches maximum**.
4. Estimer celles qui n’ont pas d’estimation.
5. Vérifier l’agenda et la capacité restante.

---

## 4. Pendant la journée

### Retrouver un dossier (Context Recall)

`g` ou commande **Contexte d’un dossier**.

- saisir un mot, un nom, un montant ou une référence ;
- les dossiers apparaissent, triés par meilleure correspondance ;
- chaque dossier affiche son statut, sa santé, ses prochaines actions, ses
  blocages, ses attentes, ses sources liées et la raison du match ;
- **Ouvrir le dossier** pour reprendre le travail ;
- **Ajouter une information** ouvre la capture sans quitter le dossier.

Une recherche ne modifie rien. Si un mot connu n’est pas retrouvé, c’est que
l’alias n’a pas été déclaré : ajoutez-le dans l’édition du projet
(**Alias de recherche**) ou via
`scripts/mvp aliases --project ID --set "surnom, acronyme"`.

### Capture rapide

`c` ou bouton **Capture rapide**.

- une ou plusieurs lignes ;
- preview ;
- **Appliquer cette ligne** ou **Tout appliquer** ;
- rien n’est écrit avant confirmation.

### Coller un message (intake)

**Entrée → Signaux** → coller le message → **Analyser**.

1. L’écran propose un dossier **et la raison** (« le texte nomme … », « contient
   l’alias … ») ;
2. s’il hésite, il l’écrit et ne rattache rien : les candidats restent
   cliquables ;
3. choisir la nature (fait, question, réponse, décision, référence, événement) ;
4. **Intégrer au dossier** : le message devient une **note** du dossier,
   cherchable, avec sa provenance ;
5. les propositions détectées restent à valider une par une dans la liste.

Rien n’est écrit avant le clic sur **Intégrer au dossier**. Re-coller le même
message ne crée pas de doublon.

### Signaux entrants

**Entrée → Signaux**.

1. Coller un texte ou déposer un fichier :
   - `.eml`, `.mbox` ;
   - `.odt`, `.docx`, `.pdf`, `.md`, `.txt`.
2. Choisir la source et, si besoin, le projet cible.
3. Cliquer **Analyser**.
4. Examiner les propositions :
   - opération ;
   - cible ;
   - avant → après ;
   - confiance ;
   - citation ;
   - rationale.
5. **Accepter**, **Rejeter** ou **Plus tard**.

Les filtres **À traiter / Appliqués / Ignorés / Tous** séparent ce qui demande
une décision du reste.

Un signal accepté alimente :

- le portefeuille ;
- `inbox/evidence.jsonl` ;
- `changes.jsonl`.

### Règles de source

Dans **Signaux → Sources connues** :

- projet par défaut ;
- sourdine ;
- enregistrer.

Les emails sont rattachés par expéditeur. Les fichiers par source.

### Fin de tâche

- `✓ Terminé` ;
- **Annuler** si erreur ;
- le temps réel peut être saisi via **Enregistrer un résultat**.

---

## 5. Revue projet guidée

**Revue → Revue projets**.

Un projet à la fois :

- santé ;
- statut ;
- progression ;
- prochaine action ;
- dernière activité ;
- échéance.

Décisions :

- **Continuer** ;
- **Défer** ;
- **Idée** ;
- **Un jour** ;
- **Clôturer** ;
- **Passer**.

La décision est journalisée. Aucune tâche n’est modifiée.

---

## 6. Mise à jour de projet

Depuis l’inspecteur d’un projet :

**Mettre à jour**.

Champs :

- santé ;
- note d’avancement ;
- risques ;
- décision ;
- prochaine étape.

La mise à jour apparaît dans **Activité** et dans **Changements**.

---

## 7. Explication et assistant

### Explication déterministe

Inspecteur projet → **Expliquer**.

Contenu :

- santé ;
- prochaines actions ;
- blocages ;
- risques ;
- changements récents ;
- signaux liés.

Aucun LLM, aucune écriture.

### Résumé assisté

Depuis **Expliquer** → **Résumé assisté**.

Par défaut, le moteur est **déterministe**.

Pour activer Ollama :

```bash
TRAJECTORY_MVP_ASSISTANT_ENGINE=local scripts/mvp dashboard
```

L’assistant :

- est read-only ;
- exige des citations ;
- ne peut pas écrire ;
- bascule en déterministe si le modèle local échoue ou cite mal.

### Revue assistée

**Revue → Semaine → Revue assistée**.

Résumé de la semaine, sans écriture.

---

## 8. Raccourcis clavier

| Touche | Action |
|---|---|
| `?` | aide |
| `n` | nouvelle tâche |
| `p` | nouveau projet |
| `c` | capture rapide |
| `i` | signaux |
| `t` | aujourd’hui |
| `r` | actualiser |
| `d` | terminer la tâche sélectionnée |
| `s` | reporter la tâche sélectionnée |
| `/` | recherche |
| `g` | contexte d’un dossier |
| `Ctrl/Cmd+K` | palette de commandes |

---

## 9. Routine du soir

1. **Bilan de fin de journée**.
2. Vérifier les tâches terminées.
3. Noter les blocages.
4. Transférer 1 à 3 tâches à demain.
5. Éventuellement **Expliquer** un projet à risque.

---

## 10. Revue hebdomadaire

1. **Revue → Semaine**.
2. **Revue assistée** si utile.
3. **Revue projets** : traiter 10 projets.
4. **Dette du portefeuille** :
   - estimer 10 tâches ;
   - vérifier les doublons ;
   - relancer les attentes anciennes.
5. **Corbeille et sauvegardes** : **Sauvegarder maintenant**.

---

## 11. Métriques de dogfood

À noter chaque semaine :

| Métrique | Départ | Cible |
|---|---:|---:|
| Tâches prêtes avec estimation | 11 / 92 | > 70 % |
| Tâches ouvertes avec estimation | 34 / 244 | > 60 % |
| Projets en veille revus | 0 / 42 | > 20 |
| Emails analysés | 0 | 20–30 |
| Propositions acceptées sans édition | — | > 70 % |
| Temps de capture | — | < 10 s |
| Temps de revue par email | — | < 2 min |

Noter aussi les frictions :

- mauvais rattachement ;
- proposition inutile ;
- agent qui invente ;
- statut incompréhensible ;
- action trop lente.

---

## 12. Ce qu’il ne faut pas faire

- Laisser un email non trié plus de 48 h.
- Accepter une proposition sans lire la citation.
- Utiliser **Tout accepter** : il n’existe pas, volontairement.
- Laisser le portefeuille sans sauvegarde.
- Ajouter une source sans règle de sourdine.
- Utiliser l’assistant comme source de vérité.

---

## 13. Références

- `README.md` — vue d’ensemble.
- `docs/guides/MVP_OPERATIONS.md` — sauvegardes, IMAP, sources, confidentialité.
- `docs/adr/ADR-038-phase0-daily-usability.md`
- `docs/adr/ADR-039-write-safety.md`
- `docs/adr/ADR-040-inbound-signals-proposals.md`
- `docs/adr/ADR-041-email-document-ingestion.md`
- `docs/development/MVP_ROADMAP.md`
