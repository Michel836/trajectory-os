# Architecture Decision Records

Les ADR enregistrent les décisions d’architecture durables.  
Une décision non documentée ne doit pas être considérée comme acquise.

## MVP personnel

| ADR | Sujet |
|---|---|
| ADR-029 | couche visuelle d’exécution et de décision |
| ADR-030 | import factuel d’abord, routage d’engine |
| ADR-031 | Quick Capture, vues sauvegardées, surface Today/Ready |
| ADR-032 | fenêtre de travail explicite et capacité restante |
| ADR-033 | contrat de classification partagé et Inbox |
| ADR-034 | agenda réel et semaine réaliste |
| ADR-035 | routines et tâches récurrentes |
| ADR-036 | registre des décisions |
| ADR-037 | estimations honnêtes et calibration |
| ADR-038 | Phase 0 : quatre espaces, undo, santé, journal |
| ADR-039 | write safety : sauvegardes, verrou, corbeille, undo |
| ADR-040 | signaux entrants et propositions humaines |
| ADR-041 | ingestion email/document, versions, confidentialité |
| ADR-042 | alias de dossier et schéma portefeuille v2 |
| ADR-043 | Context Recall : index unifié et explication du match |
| ADR-044 | resolver contextuel et surface de reprise |
| ADR-045 | note attachée, schéma portefeuille v3 |
| ADR-046 | intake : coller, proposer un dossier, valider, intégrer |

La numérotation suit l’ordre de rédaction. Les décisions d’architecture
restantes du programme V1 (Living Case, grammaire sémantique, signaux étendus,
IA supervisée, analytics) seront enregistrées à partir d’ADR-047 lorsqu’elles
seront effectivement prises, avec les identifiants attribués à l’audit Phase 0.

## Plateforme et opérateur

| ADR | Sujet |
|---|---|
| ADR-001 | Python et uv |
| ADR-002 | SQLite comme store opérationnel |
| ADR-003 | intelligence hybride |
| ADR-004 | fondation du contrôle runtime |
| ADR-005 | attestation exacte d’exécution |
| ADR-006 | promotion sémantique read-only fail-closed |
| ADR-007 | contrat de promotion sémantique writable |
| ADR-008 | identités de patch |
| ADR-009 | exécution à intervention réduite |
| ADR-010 | graphe de décomposition d’objectif |
| ADR-011 | scheduler de portefeuille et arbitrage de ressources |
| ADR-012 | réutilisation de preuve inter-missions |
| ADR-013 | générations de replanification adaptative |
| ADR-014 | backend agent borné et canary DeepSeek |
| ADR-015 | preuve dérivée au niveau objectif |
| ADR-016 | couche produit opérateur visible |
| ADR-017 | portefeuille multi-objectifs, daemon, backends |
| ADR-018 | harness isolé, ressources locales, provenance d’artefacts |
| ADR-019 | événements autoritatifs, web, LifeOS |
| ADR-020 | benchmark Pi vs Harness |
| ADR-021 | observabilité live et télémétrie |
| ADR-022 | assemblage de mission de bout en bout |
| ADR-023 | contrôle opérateur, recovery, acceptation |
| ADR-024 | bundle de release human-gated |
| ADR-025 | plateforme opérateur self-hosting |
| ADR-026 | plateforme opérateur autonome persistante |
| ADR-027 | intelligence adaptative et workflows pratiques |
| ADR-028 | real-world OS et preuve de portefeuille |
