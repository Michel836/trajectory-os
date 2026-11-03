# TrajectoryOS — Guide d’exploitation du MVP

Ce guide couvre les données, les sauvegardes, la corbeille, les sources et la
confidentialité.

---

## 1. Arborescence des données

Sous le data root (par défaut `local_data/mvp/`) :

```text
portfolio.json                 portefeuille canonique (schéma v2)
outcomes.jsonl                 ledger append-only des résultats
changes.jsonl                  journal des changements
ui_state.json                  préférences de présentation
backups/                       sauvegardes versionnées
trash/                         corbeille (sidecar)
inbox/
  signals.jsonl                signaux entrants
  proposals/<signal_id>.json   propositions par signal
  sources.jsonl                manifeste des sources
  evidence.jsonl               preuves de décision
  rules.json                   règles de source
imports/                       artefacts d’import documentaire
plans/                         plans today/week
enrichment/                    suggestions IA par projet
```

Le portefeuille reste la seule source de vérité métier.

### Schéma du portefeuille

`portfolio.json` est en **schéma v2** depuis V0.8 (ajout additif
`projects[].aliases`). Un fichier v1 reste lisible : il est surchargé en
mémoire et la **première** écriture prend une sauvegarde
`portfolio-<horodatage>-pre-migration-v1.json` avant de persister le v2. Une
version inconnue est refusée (`UNSUPPORTED_MVP_SCHEMA`).

```bash
scripts/mvp aliases                      # lister les alias déclarés
scripts/mvp aliases --project mon-dossier  # alias d’un projet
scripts/mvp aliases --project mon-dossier --set "surnom, acronyme"
scripts/mvp recall "surnom" --json         # contexte résolu (lecture seule)
```

### Index de récupération

L’index de recherche est **dérivé et reconstructible** : il est bâti depuis
`portfolio.json`, `changes.jsonl`, `inbox/signals.jsonl`,
`inbox/sources.jsonl` et `imports/*.json`, puis mis en cache par signature de
fichiers (mtime + taille). Le supprimer ne coûte rien ; aucun index n’est une
source de vérité.

Un sidecar absent ou illisible est ignoré : la recherche reste disponible. Une
requête de plus de 200 caractères est refusée, jamais tronquée.

---

## 2. Sauvegardes

### Sauvegarde manuelle

```bash
scripts/mvp backup --root local_data/mvp
scripts/mvp backup --root local_data/mvp --reason avant-nettoyage
```

### Lister

```bash
scripts/mvp backups --root local_data/mvp
```

### Restaurer

```bash
scripts/mvp restore --root local_data/mvp --file <backup>
scripts/mvp restore --root local_data/mvp          # dernière sauvegarde
```

Une restauration :

- valide le JSON ;
- sauvegarde l’état courant (`pre-restore`) ;
- écrit atomiquement.

Rétention par défaut : 20 sauvegardes. Une sauvegarde quotidienne est créée
au démarrage du dashboard.

---

## 3. Verrou d’écriture

`store.portfolio_lock` combine :

- un `threading.RLock` par data root ;
- un `fcntl.flock` sur `.portfolio.lock`.

Conséquences :

- une écriture CLI et une écriture dashboard ne s’entrelacent pas ;
- les lectures ne sont jamais bloquées ;
- le verrou est réentrant dans un même processus.

Limite : le verrou est local à la machine.

---

## 4. Corbeille et undo

### Corbeille

- suppression douce des tâches ;
- suppression de projet avec `cascade=true` explicite ;
- conservation des dépendances entrantes ;
- restauration avec réparation des arêtes de dépendance.

### Undo

- `POST /api/changes/{change_id}/undo` ;
- couvre : outcome, update, create, capture, import, delete ;
- l’historique n’est jamais réécrit ;
- une annulation est un nouvel enregistrement `UNDO`.

### Purge

Une purge définitive est explicite et distincte de la suppression.

---

## 5. Ingestion de sources

### Formats

| Format | Support |
|---|---|
| `.eml` | oui, standard library |
| `.mbox` | oui, lecture seule, borné |
| `.odt`, `.docx` | oui, via `mvp.document` |
| `.pdf` | oui, via `pdftotext` |
| `.md`, `.txt` | oui |
| `.xlsx`, images | non (pas d’OCR) |

### Versions

Un fichier ré-ingéré avec un contenu différent produit :

- `version + 1` ;
- statut `CHANGED` ;
- diff borné ;
- propositions basées sur le contenu courant.

### Limites

- pas d’OCR ;
- pas d’exécution de pièce jointe ;
- les pièces jointes sont listées, pas extraites.

---

## 6. Règles de source

`inbox/rules.json` :

```json
{
  "schema_version": 1,
  "muted_sources": [],
  "source_rules": {
    "notaire@exemple.fr": {
      "project_id": "donation-notary",
      "mute": false,
      "sensitivity": "CONFIDENTIAL",
      "max_proposals": 3
    }
  },
  "max_proposals_per_signal": 5,
  "max_signals_per_day": 200
}
```

Les règles peuvent être éditées depuis **Signaux → Sources connues** ou via
`POST /api/sources/rule`.

---

## 7. IMAP lecture seule

### Configuration

```json
{
  "host": "imap.example.com",
  "username": "moi",
  "password_env": "IMAP_PASSWORD",
  "allow_senders": ["notaire@example.com"],
  "mailbox": "INBOX",
  "max_messages": 50,
  "sensitivity": "CONFIDENTIAL"
}
```

### Exécution

```bash
export IMAP_PASSWORD='...'
scripts/mvp imap-sync --root local_data/mvp --config imap.json
```

### Garanties

- allowlist d’expéditeurs obligatoire ;
- mot de passe jamais persisté ;
- `readonly=True` ;
- aucun envoi, suppression ou déplacement ;
- borne de messages ;
- tests CI avec faux fetcher, sans réseau.

### Limite

L’adaptateur réel n’est pas testé en CI contre un serveur IMAP. Il doit être
validé sur une boîte de test avant usage quotidien.

---

## 8. Confidentialité

- `PUBLIC`, `INTERNAL`, `CONFIDENTIAL`, `RESTRICTED` ;
- `RESTRICTED` n’est pas persisté ;
- les secrets de type `api_key = ...`, `password: ...`, `bearer ...` sont
  remplacés par `[REDACTED_SECRET]` ;
- aucun cloud par défaut ;
- l’assistant local est opt-in :

```bash
TRAJECTORY_MVP_ASSISTANT_ENGINE=local
```

- une citation invalide provoque un repli déterministe.

---

## 9. Dépannage

### Le dashboard ne s’ouvre pas

- vérifier `local_data/mvp/portfolio.json` ;
- lancer `scripts/mvp plan` ;
- vérifier les logs du terminal.

### Une écriture semble bloquée

- un autre processus tient `.portfolio.lock` ;
- fermer l’autre processus ;
- le verrou est libéré à la fin du processus.

### Un email n’est pas ingéré

- vérifier l’allowlist IMAP ;
- vérifier la règle de source (`mute`) ;
- vérifier la taille et le format ;
- consulter `inbox/sources.jsonl`.

### L’assistant répond mal

- revenir en mode déterministe ;
- vérifier les citations ;
- ne pas utiliser de cloud ;
- relancer avec `TRAJECTORY_MVP_ASSISTANT_ENGINE=deterministic`.

---

## 10. Quality gate

```bash
bash scripts/quality.sh
```

Cette commande exécute :

- `pytest` ;
- `ruff` ;
- `mypy`.

Ne pas contourner ce gate.
