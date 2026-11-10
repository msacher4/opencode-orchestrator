# Orchestrateur — questions de modèles + role play final

## Objectif
Quand Marc valide un manifest, l'orchestrateur pose **un seul appel `question`** portant : la
validation (avec relecture), **un modèle par rôle** (worker / reviewer / merger) et, pour un
livrable consommé par un tiers, l'offre d'un **role play final**. Le modèle choisi est recopié en
`- Modèle:` sur chaque carte du rôle (seule forme que le guard fait respecter). Le role play devient
une carte `Qui: reviewer` **consultative**, après la réception.

## Non-objectifs
- Aucun nouvel agent, aucune modification de `Opencodev2/config/opencode/plugins/` (guard, UI).
- Aucun changement des front-matter `model:` des fiches worker / reviewer / merger.
- Aucun changement du modèle de l'orchestrateur lui-même.
- Pas de role play sur un refacto, un script, une lib, une config ou un doc interne.
- Pas de nouveau chemin de données : le retour du role play est un contrat, recopié dans `done.md`.

## Critères (DONE QUAND)
1. `agents/orchestrator.md` §3 décrit l'appel unique : validation (libellé `relire le plan à froid`
   conservé), trois questions de modèle, une question de role play limitée aux livrables tiers.
2. La même fiche décrit l'écriture des en-têtes (`- Relecture:`, `- Modèles:`, `- Role play:`), le
   pin `- Modèle:` par carte, le repli quota local et le cas du modèle payant.
3. La même fiche contient `6.6 Role play final` (carte, contrat, consultatif, verdict = test
   effectué) et la matrice de modèles réécrite autour de nemotron / ornith.
4. `agents/reviewer.md` contient `## Carte de role play` avec le contrat 6 lignes.
5. `commands/orch.md` étape 4 propose des options de validation alignées sur la fiche, dont
   `relire le plan à froid` (aujourd'hui absent → le guard refuserait la 1re carte worker).
6. `skills/orchestrate/SKILL.md` : Geste 2 = appel unique, template de manifest avec `- Modèles:` /
   `- Modèle:` et une carte role play d'exemple ; la phrase « le modèle n'apparaît pas dans le
   manifest » est corrigée.
7. `commands/night.md` : `/night queue` pose les mêmes questions, **local uniquement**.
8. Aucun fichier sous `plugins/` modifié.

## Vérification
Lecture seule, rejouable :
`git -C Opencodev2 diff --stat` puis les `grep -q` de chaque carte du manifest, et
`git -C Opencodev2 status --porcelain -- config/opencode/plugins` doit rester vide.
