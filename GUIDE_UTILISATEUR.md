# Guide utilisateur — tester TrajectoryOS V0.7

Ce guide est un **plan de test manuel** pour utiliser TrajectoryOS dès demain
et vérifier que les fonctions prévues se comportent correctement.

Il est organisé en sessions courtes. Chaque test a :

- un objectif ;
- des étapes ;
- un résultat attendu ;
- une case `OK` / `KO` ;
- un espace de notes.

> **But** : tester l’usage réel, pas le code.  
> **Règle** : ne jamais mettre en danger le portefeuille réel. Les tests
> destructifs se font sur une copie.

---

## 0. Avant de commencer

### 0.1 Vérifier l’environnement

```bash
cd "/home/chu/PROJETS IA/Projet IA_trajectoryOS/trajectory-os"
bash scripts/quality.sh
```

**Attendu** : pytest, Ruff et mypy passent.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T00 — quality gate | vert | [ ] | [ ] | |

### 0.2 Sauvegarder le portefeuille réel

```bash
scripts/mvp backups
scripts/mvp backup --root local_data/mvp --reason avant-tests
```

**Attendu** : une sauvegarde `portfolio-...-avant-tests.json` apparaît.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T01 — sauvegarde initiale | backup listé | [ ] | [ ] | |

### 0.3 Créer une copie de test

Pour les tests destructifs (corbeille, restauration, réparation) :

```bash
cp -a local_data/mvp local_data/mvp-test
scripts/mvp dashboard --root local_data/mvp-test --port 8788
```

Ouvre `http://127.0.0.1:8788/`.

Pour l’usage quotidien, garde le port normal :

```bash
scripts/mvp dashboard          # http://127.0.0.1:8787/
```

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T02 — dashboard réel | s’ouvre sur la vue sauvegardée | [ ] | [ ] | |
| T03 — dashboard de test | s’ouvre sur le port 8788 | [ ] | [ ] | |

### 0.4 Vocabulaire

- **Projet** : un résultat à atteindre.
- **Tâche** : une action atomique.
- **Signal** : un texte, un email ou un document entrant.
- **Proposition** : un changement suggéré, à accepter ou rejeter.
- **Santé projet** : `ON_TRACK`, `AT_RISK`, `STALE`, `NO_NEXT_ACTION`,
  `OVERLOADED`, `WAITING`, `BLOCKED`, `DONE`.
- **Corbeille** : suppression douce, restaurable.
- **Journal** : historique des changements, avec undo.

---

## Session 1 — Socle quotidien (30 min)

### T04 — Ouvrir Aujourd’hui

1. Clique sur **Aujourd’hui**.
2. Vérifie la carte **Santé du portefeuille**.
3. Vérifie la carte **Agenda**.
4. Vérifie la table **Base recommandée**.

**Attendu** :

- la vue s’ouvre sans erreur ;
- les compteurs de santé sont visibles ;
- les tâches ont un bouton `✓` et un bouton `⏱`.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T04 — Aujourd’hui | vue complète | [ ] | [ ] | |

### T05 — Terminer une tâche + annuler

1. Clique sur `✓` d’une tâche.
2. Lis le toast.
3. Clique sur **Annuler**.
4. Vérifie **Changements**.

**Attendu** :

- la tâche passe à `Terminé` ;
- le toast propose **Annuler** ;
- l’annulation restaure le statut précédent ;
- **Changements** affiche `Résultat` puis `Annulation`.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T05 — Done + undo | statut restauré | [ ] | [ ] | |

### T06 — Capture rapide ligne par ligne

1. Clique sur **Capture rapide**.
2. Saisis trois lignes :
   ```text
   appeler le notaire
   préparer les attestations
   vérifier le virement
   ```
3. Clique **Analyser**.
4. Applique **une seule ligne**.
5. Vérifie que les deux autres restent dans le preview.

**Attendu** :

- une seule ligne est appliquée ;
- les autres ne sont pas créées ;
- le toast confirme la ligne appliquée.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T06 — capture ligne par ligne | 1 seule ligne appliquée | [ ] | [ ] | |

### T07 — Réparer les statuts projet

Sur la copie de test `mvp-test` :

1. Ouvre **Santé du portefeuille**.
2. Clique **Recalculer les statuts projet** si le bouton apparaît.
3. Vérifie **Changements**.

**Attendu** :

- les projets cohérents passent en `ACTIVE` ;
- les projets parqués restent parqués ;
- l’action est journalisée.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T07 — réparation statuts | cohérence | [ ] | [ ] | |

### T08 — Aide clavier

1. Appuie sur `?`.
2. Vérifie la liste des raccourcis.
3. Ferme avec `Échap`.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T08 — aide clavier | modale visible | [ ] | [ ] | |

---

## Session 2 — Portefeuille planifiable (30 min)

### T09 — Estimer 20 tâches

1. Ouvre **Dette du portefeuille**.
2. Clique **Estimer les tâches sans estimation**.
3. Sélectionne **20 tâches**.
4. Choisis `30 min`.
5. Applique.
6. Vérifie **Changements** et la carte **Santé**.

**Attendu** :

- 20 tâches ont une estimation ;
- le compteur `X/Y tâches estimées` augmente ;
- 20 entrées `Mise à jour` dans le journal.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T09 — estimation en masse | 20 tâches estimées | [ ] | [ ] | |

### T10 — Trier un projet en veille

1. Dans **Dette**, repère un projet `Reporté`.
2. Clique **Activer**.
3. Vérifie qu’il quitte la liste des projets en veille.
4. Clique **Défer** depuis l’inspecteur ou **Revue projets**.
5. Vérifie le statut.

**Attendu** : le statut change et l’action est journalisée.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T10 — activer/défer | statut changé | [ ] | [ ] | |

### T11 — Revue projet guidée

1. Ouvre **Revue → Revue projets**.
2. Vérifie le compteur `1 / N`.
3. Clique **Suivant** puis **Précédent**.
4. Prends une décision **Continuer** sur un projet.
5. Vérifie **Changements**.

**Attendu** :

- un projet à la fois ;
- navigation fluide ;
- décision journalisée ;
- aucune tâche modifiée.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T11 — revue guidée | décision enregistrée | [ ] | [ ] | |

### T12 — Mise à jour de projet

1. Sélectionne un projet.
2. Dans l’inspecteur, clique **Mettre à jour**.
3. Renseigne :
   - santé ;
   - note ;
   - risque ;
   - décision ;
   - prochaine étape.
4. Enregistre.
5. Vérifie **Activité** dans l’inspecteur et **Changements**.

**Attendu** : la note apparaît dans l’activité et le journal.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T12 — mise à jour projet | note visible | [ ] | [ ] | |

### T13 — Expliquer un projet

1. Sélectionne un projet.
2. Clique **Expliquer**.
3. Vérifie :
   - santé ;
   - prochaines actions ;
   - blocages ;
   - risques ;
   - changements récents ;
   - signaux.

**Attendu** : sections remplies, aucune écriture.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T13 — explication | sections complètes | [ ] | [ ] | |

---

## Session 3 — Signaux entrants (45 min)

### T14 — Analyser un texte

1. Ouvre **Entrée → Signaux**.
2. Colle :
   ```text
   [NOM D’UN PROJET EXISTANT] : le notaire a envoyé les pièces.
   Échéance 15/11/2026. Appeler le notaire demain. Prévoir 30 min.
   ```
3. Choisis le projet cible.
4. Clique **Analyser**.

**Attendu** : propositions visibles, avec citation et confiance.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T14 — signal texte | propositions | [ ] | [ ] | |

### T15 — Split inbox

1. Vérifie les filtres **À traiter / Appliqués / Ignorés / Tous**.
2. Clique **À traiter**.
3. Clique **Tous**.

**Attendu** : les filtres changent la liste.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T15 — split inbox | filtres fonctionnels | [ ] | [ ] | |

### T16 — Accepter une proposition

1. Sur une proposition **Échéance** ou **Lever l’attente**, clique **Accepter**.
2. Vérifie la tâche ou le projet concerné.
3. Vérifie **Changements**.
4. Vérifie `inbox/evidence.jsonl` :

```bash
tail -3 local_data/mvp/inbox/evidence.jsonl
# ou local_data/mvp-test/inbox/evidence.jsonl si tu utilises la copie de test
```

**Attendu** : modification appliquée, preuve enregistrée.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T16 — acceptation | changement + preuve | [ ] | [ ] | |

### T17 — Rejeter une proposition

1. Sur une proposition, clique **Rejeter**.
2. Vérifie que le portefeuille n’a pas changé.

**Attendu** : aucune écriture métier.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T17 — rejet | aucune écriture | [ ] | [ ] | |

### T18 — Mettre une source en sourdine

1. Clique **Mettre la source en sourdine**.
2. Essaie de recréer un signal avec la même source.
3. Vérifie le message d’erreur ou l’absence de signal.

**Attendu** : la source est bloquée.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T18 — sourdine | source bloquée | [ ] | [ ] | |

### T19 — File d’ingestion

1. Vérifie la section **File d’ingestion**.
2. Ouvre un signal et regarde la version, le statut, la date.

**Attendu** : les sources récentes sont visibles.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T19 — file d’ingestion | sources listées | [ ] | [ ] | |

---

## Session 4 — Emails et documents (45 min)

### T20 — Créer un email de test

Remplace `[NOM DU PROJET]` par un projet réel, puis :

```bash
cat > /tmp/test-mail.eml <<'EOF'
From: notaire@example.com
To: moi@example.com
Subject: [NOM DU PROJET] — pièces reçues
Date: Wed, 29 Oct 2026 10:00:00 +0000
Message-ID: <test-mail-001@example.com>
Content-Type: text/plain; charset=utf-8

Bonjour,

Le notaire a envoyé les pièces. Échéance 15/11/2026.
Prévoir 30 min pour vérifier.

Cordialement,
Le notaire
EOF
```

### T21 — Ingérer le `.eml`

1. **Signaux** → **Choisir un fichier** → `/tmp/test-mail.eml`.
2. Choisis la source `mail notaire`.
3. Choisis le projet cible.
4. Clique **Analyser**.

**Attendu** :

- signal analysé ;
- source `EMAIL` ;
- version 1 ;
- fil de discussion ;
- propositions.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T21 — import `.eml` | signal + propositions | [ ] | [ ] | |

### T22 — Créer et ingérer un `.mbox`

```bash
.venv/bin/python - <<'PY'
import mailbox
from email.message import EmailMessage

box = mailbox.mbox('/tmp/test-mail.mbox')
for index in range(2):
    message = EmailMessage()
    message['From'] = 'notaire@example.com'
    message['To'] = 'moi@example.com'
    message['Subject'] = '[NOM DU PROJET] — message ' + str(index + 1)
    message['Date'] = 'Wed, 29 Oct 2026 10:00:00 +0000'
    message['Message-ID'] = f'<test-mbox-{index}@example.com>'
    message.set_content('Le notaire a envoyé les pièces. Échéance 15/11/2026.')
    box.add(message)
box.flush()
box.close()
print('written /tmp/test-mail.mbox')
PY
```

Puis dépose `/tmp/test-mail.mbox` dans **Signaux**.

**Attendu** : 2 messages ingérés. Réimporte le même fichier : les doublons
doivent être détectés.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T22 — import `.mbox` | 2 messages, doublons | [ ] | [ ] | |

### T23 — Version de document v1

```bash
cat > /tmp/test-document.md <<'EOF'
# [NOM DU PROJET]
Objectif v1 : finaliser le dossier.
Action : appeler le notaire.
EOF
```

Dépose `/tmp/test-document.md` dans **Signaux**.

**Attendu** : version 1, pas de diff.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T23 — document v1 | version 1 | [ ] | [ ] | |

### T24 — Version de document v2 + diff

```bash
cat > /tmp/test-document.md <<'EOF'
# [NOM DU PROJET]
Objectif v2 : finaliser le dossier avant le 15/11/2026.
Action : appeler le notaire demain.
Nouvelle action : préparer les attestations.
EOF
```

Dépose à nouveau le fichier.

**Attendu** :

- version 2 ;
- statut `CHANGED` ;
- diff visible ;
- propositions basées sur le contenu courant.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T24 — document v2 | version 2 + diff | [ ] | [ ] | |

### T25 — Règle de source

1. **Signaux → Sources connues**.
2. Choisis un projet par défaut pour `notaire@example.com`.
3. Enregistre.
4. Réimporte un email de cette source.
5. Vérifie le projet cible.
6. Coche **Sourdine** et réessaie.

**Attendu** : le projet est prérempli, puis la source est bloquée.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T25 — règles de source | projet + sourdine | [ ] | [ ] | |

### T26 — IMAP (optionnel)

À ne faire que sur une **boîte de test**.

```json
{
  "host": "imap.example.com",
  "username": "moi",
  "password_env": "IMAP_PASSWORD",
  "allow_senders": ["notaire@example.com"],
  "mailbox": "INBOX",
  "max_messages": 10,
  "sensitivity": "CONFIDENTIAL"
}
```

```bash
export IMAP_PASSWORD='...'
scripts/mvp imap-sync --root local_data/mvp-test --config imap.json
```

**Attendu** : messages ingérés, allowlist respectée, aucun envoi/suppression.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T26 — IMAP | lecture seule | [ ] | [ ] | |

---

## Session 5 — Confiance, corbeille, recherche (30 min)

### T27 — Sauvegarder et restaurer

Sur `mvp-test` :

1. **Corbeille et sauvegardes** → **Sauvegarder maintenant**.
2. Modifie une tâche.
3. Restaure la sauvegarde.
4. Vérifie que l’état courant a été sauvegardé avant (`pre-restore`).

**Attendu** : restauration valide, sauvegarde préalable.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T27 — backup/restore | état restauré | [ ] | [ ] | |

### T28 — Corbeille

1. Sélectionne une tâche.
2. Clique **Corbeille**.
3. Vérifie qu’elle disparaît.
4. Ouvre **Revue → Corbeille**.
5. Restaure-la.

**Attendu** : tâche supprimée puis restaurée, dépendances réparées.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T28 — corbeille | restauration | [ ] | [ ] | |

### T29 — Undo d’une modification

1. Modifie une estimation.
2. Ouvre **Changements**.
3. Clique **Annuler** sur la modification.
4. Vérifie l’ancienne valeur.

**Attendu** : valeur restaurée, journal `UNDO`.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T29 — undo update | valeur restaurée | [ ] | [ ] | |

### T30 — Recherche globale

1. `Ctrl+K`.
2. Tape le nom d’un projet.
3. Ouvre le résultat.
4. Tape le nom d’un signal.
5. Vérifie que le signal est trouvé.

**Attendu** : projets, tâches, signaux et changements recherchables.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T30 — recherche | résultats pertinents | [ ] | [ ] | |

### T31 — Raccourcis

Teste :

- `n` nouvelle tâche ;
- `c` capture ;
- `i` signaux ;
- `t` aujourd’hui ;
- `d` done sur une tâche sélectionnée ;
- `s` report ;
- `/` recherche ;
- `?` aide.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T31 — raccourcis | tous fonctionnels | [ ] | [ ] | |

---

## Session 6 — Assistant et revue (30 min)

### T32 — Assistant déterministe

1. Sélectionne un projet.
2. **Expliquer**.
3. **Résumé assisté**.
4. Vérifie :
   - moteur `deterministic` ;
   - citations ;
   - aucune écriture.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T32 — assistant déterministe | citations, read-only | [ ] | [ ] | |

### T33 — Assistant local (optionnel)

```bash
TRAJECTORY_MVP_ASSISTANT_ENGINE=local scripts/mvp dashboard
```

Refais T32.

**Attendu** :

- moteur `local` si Ollama est disponible ;
- citations valides ;
- repli déterministe si le modèle échoue.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T33 — assistant local | local ou fallback | [ ] | [ ] | |

### T34 — Revue assistée

1. **Revue → Semaine**.
2. Clique **Revue assistée**.
3. Vérifie le résumé et les citations.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T34 — revue assistée | résumé + citations | [ ] | [ ] | |

### T35 — Activité par objet

1. Sélectionne une tâche.
2. Vérifie **Activité** dans l’inspecteur.
3. Sélectionne un projet.
4. Vérifie **Activité**.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T35 — activité | historique visible | [ ] | [ ] | |

---

## Session 7 — Bilan (20 min)

### T36 — Métriques de dogfood

Note :

| Métrique | Valeur |
|---|---:|
| Tâches prêtes avec estimation | |
| Tâches ouvertes avec estimation | |
| Projets en veille revus | |
| Emails analysés | |
| Propositions acceptées sans édition | |
| Temps de capture moyen | |
| Temps de revue par email | |
| Frictions principales | |

### T37 — Décision

Trois cas :

- **Tout est OK** → merge + tag `v0.7.0-assistant`.
- **Quelques frictions** → corriger les 3 plus coûteuses avant merge.
- **Problème bloquant** (perte de données, sécurité, restore cassé) → stop,
  restaurer la sauvegarde, documenter.

| Test | Attendu | OK | KO | Notes |
|---|---|---|---|---|
| T36 — métriques | tableau rempli | [ ] | [ ] | |
| T37 — décision | merge / corriger / stop | [ ] | [ ] | |

---

## Journal de test

Copie ce tableau dans un fichier et remplis-le au fur et à mesure.

| ID | Test | Attendu | Résultat | Note |
|---|---|---|---|---|
| T00 | quality gate | vert | | |
| T01 | backup initial | backup listé | | |
| T04 | Aujourd’hui | vue complète | | |
| T05 | Done + undo | statut restauré | | |
| T06 | capture ligne par ligne | 1 seule ligne | | |
| T09 | estimation en masse | 20 tâches | | |
| T11 | revue guidée | décision | | |
| T14 | signal texte | propositions | | |
| T16 | acceptation | changement + preuve | | |
| T21 | `.eml` | signal | | |
| T22 | `.mbox` | 2 messages | | |
| T24 | document v2 | diff | | |
| T27 | backup/restore | état restauré | | |
| T28 | corbeille | restauration | | |
| T30 | recherche | résultats | | |
| T32 | assistant | citations | | |

---

## Dépannage rapide

### Le dashboard ne s’ouvre pas

```bash
scripts/mvp plan
```

Si le portefeuille est absent, `scripts/mvp init`.

### Une écriture semble bloquée

Un autre processus tient `.portfolio.lock`. Ferme l’autre dashboard/CLI.

### L’assistant répond mal

Reviens en déterministe :

```bash
TRAJECTORY_MVP_ASSISTANT_ENGINE=deterministic scripts/mvp dashboard
```

### Un email n’est pas ingéré

- vérifie l’allowlist IMAP ;
- vérifie la règle de source (`mute`) ;
- vérifie `inbox/sources.jsonl` ;
- vérifie le format et la taille.

### Restauration

```bash
scripts/mvp backups
scripts/mvp restore --root local_data/mvp --file <backup>
```

---

## Limites connues

- pas d’OCR ;
- pas de cloud par défaut ;
- analyse déterministe conservatrice ;
- assistant local optionnel et non évalué en CI ;
- IMAP non testé en CI contre un vrai serveur ;
- pas encore de dogfood long ;
- V0.7 n’est pas V1.

---

## Ce qu’il ne faut pas faire

- Tester la corbeille ou la restauration sur le portefeuille réel sans backup.
- Accepter une proposition sans lire la citation.
- Utiliser une boîte IMAP personnelle pour le premier test.
- Mettre des secrets dans `inbox/rules.json`.
- Laisser une source bruyante sans sourdine.
- Considérer l’assistant comme une source de vérité.
- Merger sans quality gate vert.

---

## Modèle de retour

À la fin, envoie :

```text
Version : V0.7
Date :
Portefeuille : réel / test

Tests OK :
Tests KO :

Top 3 frictions :
1.
2.
3.

Top 3 propositions utiles :
1.
2.
3.

Top 3 propositions fausses :
1.
2.
3.

Temps de revue moyen par email :
Décision : merge / corriger / stop
```

C’est ce retour qui doit piloter la suite, pas une nouvelle fonctionnalité.
