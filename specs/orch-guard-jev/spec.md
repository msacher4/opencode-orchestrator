# spec — orch-guard-jev (suite de nuit)

## Objectif

Finaliser la fiabilisation d'`Opencodev2/config/opencode/plugins/orch-guard.ts` :
mesurer le gain réel sur l'historique (banc de rejeu), faire revue indépendante,
puis livrer sur une branche GitHub **sans push**.

Contexte déjà acquis (session du jour, manifest : `.opencode/orch/ses_e918bc6d8ffeKgbycYQkXlk1S9/orchestration.md`) :
- t0 ✅ dump context-prompts reconstruit (déterminisme vérifié).
- t1 ✅ parsing conservateur déployé (1385 lignes, 131 tests PASS / 0 FAIL).
- t2 (second avis Typesafe jev-latest, zone grise uniquement, repli code) : exécuté
  par la session du jour — **précondition de la nuit : voir son statut dans le
  manifest du jour**. La nuit ne relance JAMAIS t2.

## Seuil de confiance (révision 2026-10-25)

Seuil de confiance abaissé de 0,90 à 0,75 sur décision de Marc ; probabilité du choix maintenue à 0,90. Justification : calibration mesurée de l'API Typesafe (confidence 0,80-0,84 sur un cas trivial de lecture), un seuil à 0,90 rendait le second avis structurellement inopérant ; fail-closed inchangé.

## Non-objectifs

- Aucune modification des caps du pool, de la politique d'offre, des rôles.
- Aucune exécution des commandes historiques de l'audit (rejeu = simulation dans le banc).
- Aucun push, aucune installation de dépendance, aucun changement du SDK.
- Aucune clé API affichée, loggée ou intégrée au code.

## Critères DONE QUAND (testables)

1. `specs/orch-guard-jev/banc-report.md` existe avec : nb commandes rejouées,
   faux refus éliminés avant/après, latence p50/p95, coût total ; toute passe
   non exécutée est marquée INCOMPLÈTE (jamais simulée).
2. Revue indépendante rendue avec verdict explicite PASS / FIX (1 max) / REJECT.
3. Branche `orch/guard-jev` créée dans `/media/marcs/Linux_Apps/Projets_AI/Opencodev2`
   avec un commit contenant `config/opencode/plugins/orch-guard.ts` et
   `specs/orch-guard-jev/` — **aucun push** (interdit la nuit).
4. `specs/orch-guard-jev/done.md` écrit (changé / appris / reste) ; manifest de
   nuit archivé dans `.opencode/orch-archive/`.

## Commande de vérification

```bash
ls specs/orch-guard-jev/banc-report.md specs/orch-guard-jev/done.md && \
git -C Opencodev2 log --oneline -1 orch/guard-jev && \
git -C Opencodev2 status --short | grep -v '^??' | wc -l
```

`done` si le rapport existe, le commit est sur la branche, et aucun push n'a eu lieu
(`git log origin/orch/guard-jev` doit échouer).
