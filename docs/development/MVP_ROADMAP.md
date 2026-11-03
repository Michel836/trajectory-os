# MVP Roadmap — V0.x

Statut au 2026-10-28. **Fait** signifie implémenté et passé au quality gate,
pas encore validé par un dogfood long.

| Niveau | Nom | Contenu | Statut |
|---|---|---|---|
| V0.0 | Miroir | modèle de données, import, portefeuille structuré | fait |
| V0.1 | Daily cockpit | 4 espaces, Done/undo, journal, santé, capture ligne par ligne | fait |
| V0.2 | Portefeuille planifiable | estimation en masse, suggestions calibrées, dette, domaines | fait |
| V0.3 | Trusted Local MVP | sauvegardes, restauration, verrou, corbeille, undo étendu, recherche globale, palette | fait |
| V0.4 | Inbound Loop | signaux, propositions, file de revue, omnibox, filtre à bruit | fait |
| V0.5 | Real Sources | `.eml`, `.mbox`, documents, versions, règles de source, IMAP opt-in | fait |
| V0.6 | Adaptive Triage | santé projet, métriques, revue projet guidée, split inbox, mises à jour projet, activité par objet | fait |
| V0.7 | Bounded Assistance | explication déterministe, assistant read-only, revue assistée, citations, raccourcis clavier | fait |
| V0.8 | Context Recall | alias déclarés, schéma v2, index unifié, resolver contextuel, panneau de reprise | fait (gate à dogfooder) |
| V0.9 | Intake & Notes | note attachée (schéma v3), routage proposé avec abstention, intégration après validation | fait (gate à dogfooder) |

---

## Portes de sortie

### V0.3

- sauvegarde/restauration testées ;
- verrou inter-processus ;
- corbeille et undo ;
- recherche indépendante des filtres.

### V0.4

- 100 signaux réels ;
- précision ≥ 90 % sur dates/statuts ;
- ≥ 70 % d’acceptation sans édition ;
- 0 écriture non autorisée ;
- bruit < 20 %.

### V0.5

- 100 emails/20 documents ;
- fausses échéances < 5 % ;
- aucune fuite de donnée sensible ;
- versions/diffs validés.

### V0.6

- ≥ 100 outcomes ;
- calibration utile ;
- revue projet utilisée ;
- métriques stables ;
- split inbox et mises à jour projet utilisés ;
- activité par objet consultée.

### V0.7

- assistant déterministe validé ;
- assistant local comparé sur 20 questions ;
- citations valides ;
- 0 écriture ;
- temps gagné ≥ 30 % sur les cas ambigus.

### V0.8

- un mot absent des données résout un dossier via un alias déclaré ;
- un alias déclaré est une identité exacte, jamais déduite ;
- un document v1 se lit et se migre avec sauvegarde préalable ;
- recherche couvrant projets, tâches, décisions, sources, signaux, journal ;
- 0 écriture pendant une recherche ou un resolve ;
- P95 de la recherche ≤ 150 ms et du panneau contextuel ≤ 300 ms.

---

## V1 — non atteint

V1 exige :

- 4 à 6 semaines d’usage quotidien réel ;
- boucle entrante fiable ;
- calibration d’effort mesurable ;
- zéro perte de données ;
- temps de revue inférieur au traitement manuel ;
- aucune régression de confiance.

---

## Prochaines étapes

1. Dogfood 5 jours.
2. 100 emails / 20 documents.
3. Test IMAP réel sur boîte de test.
4. Évaluation assistant déterministe vs local.
5. Merge + tag `v0.7.0-assistant`.
6. Décision V1.
