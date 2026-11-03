# Changelog

## V0.9.0 — Intake & Notes (2026-11-03)

- objet `Note` attachée à un dossier (schéma portefeuille v3, migration additive
  avec sauvegarde `pre-migration-v2`) ;
- coller un message → **dossier proposé avec sa raison**, alternatives
  cliquables, abstention explicite quand le routage hésite ;
- un clic **Intégrer au dossier** : provenance (signal) + note lisible +
  application des seules propositions acceptées ;
- re-collage idempotent, note cherchable (`bioverify` retrouve le dossier) ;
- `POST /api/intake` (essai à blanc, zéro écriture) et
  `POST /api/intake/confirm` (seul chemin d'écriture) ;
- `mutations.add_note` / `update_note` / `delete_note`, journalisées ;
- présélection d’un dossier **uniquement sur preuve d’identité** : alias déclaré,
  ou nom en en-tête/signature — une mention au milieu du texte n’est plus une
  identité (correction après usage réel) ;
- alias multi-mots pris en charge par le routage ;
- **mémorisation d’un alias** depuis l’écran d’intake, et création du dossier
  manquant en un clic.

## V0.8.0 — Context Recall (2026-11-03)

- alias de dossier déclarés par l’humain (schéma portefeuille v2) ;
- migration v1 → v2 additive, avec sauvegarde `pre-migration-v1` avant la
  première écriture ;
- index de récupération unifié : projets, tâches, décisions, sources entrantes,
  signaux, journal, vocabulaire d’import ;
- explication du match (`matched_field` + `reason`) sur chaque résultat ;
- resolver contextuel `mot → dossier → contexte de reprise` ;
- panneau **Contexte du dossier** (touche `g`), palette groupée par dossier ;
- commandes `mvp aliases` et `mvp recall` ;
- cache d’index par signature de fichiers (recherche p50 ≈ 6 ms).

## V0.7.0 — Bounded Assistance (2026-10-28)

- explication déterministe d’un projet ;
- assistant read-only déterministe ;
- backend local Ollama opt-in, citations obligatoires, repli déterministe ;
- revue hebdomadaire assistée ;
- raccourcis clavier et aide.

## V0.6.0 — Adaptive Triage (2026-10-28)

- santé projet séparée du statut ;
- métriques de couverture des estimations ;
- revue projet guidée, un projet à la fois ;
- split inbox des signaux ;
- file d’ingestion visible ;
- mises à jour de projet et activité par objet.

## V0.5.0 — Real Sources (2026-10-28)

- `.eml` et `.mbox` en local ;
- manifeste `inbox/sources.jsonl` ;
- versions de documents et diff ;
- règles de source ;
- IMAP lecture seule opt-in ;
- redaction et `RESTRICTED` non persisté.

## V0.4.0 — Inbound Loop (2026-10-28)

- `InboundSignal` et `Proposal` ;
- analyse déterministe : dates, durées, statuts, attentes, actions ;
- file de revue, acceptation/rejet/report ;
- preuves `inbox/evidence.jsonl` ;
- vue Signaux et recherche globale des signaux.

## V0.3.0 — Trusted Local MVP (2026-10-28)

- sauvegardes versionnées et restauration ;
- verrou mono-écrivain ;
- corbeille et undo étendu ;
- recherche globale et palette `Ctrl+K` ;
- santé du portefeuille.

## V0.2.0 — Portefeuille planifiable (2026-10-28)

- estimation en masse ;
- suggestions calibrées ;
- dette du portefeuille ;
- renommage de domaines.

## V0.1.0 — Daily cockpit (2026-10-28)

- quatre espaces ;
- interface française ;
- Done en un clic + annulation ;
- journal des changements ;
- capture ligne par ligne.

## V0.0.0 — Miroir (2026-09)

- modèle de données ;
- import documentaire ;
- readiness, priorité, scheduler ;
- cockpit texte et HTML.
