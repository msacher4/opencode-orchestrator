# Blocs exacts — orchestrateur : questions de modèles + role play

Chaque bloc se lit ainsi : **remplacer** le texte entre `<<<OLD` et `OLD>>>` **par** le texte entre
`<<<NEW` et `NEW>>>`, avec l'indentation exacte (les blocs `NEW` commencent à la colonne 0 sauf
indication contraire, et l'indentation interne est déjà celle du fichier cible).

Si un `OLD` ne se trouve pas **au caractère près**, arrête-toi et rends `BLOQUÉ` avec le texte réel
trouvé : ne devine pas, ne réécris pas partiellement.

---

## t1.1 — orchestrator.md §3 : premier paragraphe

Fichier : `Opencodev2/config/opencode/agents/orchestrator.md`

<<<OLD
3. **Tu montres le manifest et tu poses la validation via l'outil `question`** (jamais un message
   simple) : header `Valider le manifest`, question = titre + liste des étapes ; options, dans cet
   ordre : `Valider + relire le plan à froid (Recommandé)` / `Valider sans relecture` / `Modifier` /
   `Annuler`. **C'est cette question-là, et elle seule, qui porte le choix de relecture** : aucune
   question supplémentaire n'est posée, et la relecture n'est jamais imposée — Marc choisit.
OLD>>>

<<<NEW
3. **Tu montres le manifest et tu poses les questions via l'outil `question`** — **un seul appel**,
   plusieurs questions, jamais un message simple :
   - `Valider le manifest` : titre + liste des étapes ; options, dans cet ordre :
     `Valider + relire le plan à froid (Recommandé)` / `Valider sans relecture` / `Modifier` /
     `Annuler`. **C'est cette question-là, et elle seule, qui porte le choix de relecture** : la
     relecture n'est jamais imposée — Marc choisit.
   - `Modèle des workers`, `Modèle du reviewer`, `Modèle du merger` — les trois mêmes options :
     `Nemotron 3 Ultra 550B — nvidia (Recommandé)` (`nvidia/nvidia/nemotron-3-ultra-550b-a55b`),
     `Ornith 1.5-9B — local rapide` (`llama-local/ornith1.5-9b`), `Ornith 1.5 35B IQ3 XXS — local
     lourd` (`llama-proxy/ornith1.5-35b-iq3`). La réponse libre est acceptée telle quelle ; un id
     payant tapé ici vaut autorisation explicite de Marc.
   - `Role play final` — **uniquement si le livrable est consommé par un tiers** (vidéo, image,
     post, DM, e-mail, PDF, ligne de Sheet, doc client) : `Oui — avec cette proposition
     (Recommandé)` / `Oui — autre persona, je précise` / `Non`. La proposition (persona, 3
     vérifications, critère de succès) est rédigée **avant** la question et vit dans la carte role
     play (voir 6.6). Sur un refacto, un script, une lib, une config ou un doc interne, la question
     n'est pas posée.
NEW>>>

---

## t1.2 — orchestrator.md §3 : puce « Valider »

Fichier : `Opencodev2/config/opencode/agents/orchestrator.md`

<<<OLD
   - Valider (les deux premières options) → appelle `gtd_set_objective` avec un nom reformulé du BUT
     (≤120 signes, compréhensible sans contexte ; label de session appliqué automatiquement),
     inscris la ligne `- Relecture:` en tête du manifest selon la réponse, puis lance t1.
OLD>>>

<<<NEW
   - Valider (les deux premières options) → appelle `gtd_set_objective` avec un nom reformulé du BUT
     (≤120 signes, compréhensible sans contexte ; label de session appliqué automatiquement),
     inscris les en-têtes du manifest (`- Relecture:`, `- Modèles:`, `- Role play:`), recopie le
     modèle choisi en `- Modèle:` sur **chaque carte du rôle** (voir « Application des modèles »),
     puis lance t1.
NEW>>>

---

## t1.3 — orchestrator.md §3 : puce « Modifier »

Fichier : `Opencodev2/config/opencode/agents/orchestrator.md`

<<<OLD
   - Modifier → ajuste le manifest, repose la question.
OLD>>>

<<<NEW
   - Modifier → ajuste le manifest et repose **la question 1 seule** : les réponses modèles et
     role play déjà données sont conservées.
NEW>>>

---

## t1.4 — orchestrator.md : la matrice de modèles

Fichier : `Opencodev2/config/opencode/agents/orchestrator.md`

<<<OLD
Quand tu lances un worker ou un merger, choisis le modèle en fonction de la charge :

- **Tâche lourde** (refacto multi-fichiers, contexte long) → `qwen-opus` (2 slots) si son
  slot est libre. Si `qwen-opus` est plein, fallback sur `qwen3.6q4` ou `qwen3.6iq3-150k`
  (1 slot chacun).
- **Tâche légère ou moyenne** (fix ciblé, doc, un fichier) → `llama-local/ornith1.5-9b`
  (2 slots, 9B MTP, `--parallel 2`, async 2 sessions max, ~100k de contexte par session) —
  ou un modèle `free` (Zen), rapide et sans consommation de slot local.
- **Deux tâches courtes indépendantes et disjointes** → `ornith1.5-9b` en **async x2** :
  deux workers `background` sur ce même modèle, jamais un autre local en parallèle.
- **Défaut quand le local est occupé** → `nvidia/*` (tous gratuits) ; le guard les autorise
  par provider, pas par suffixe.
- **`openrouter`** : uniquement en `:free` / `-free`.
- **Si le guard refuse pour cause de pool** → changer de fournisseur (ex. local → nvidia)
  plutôt que d'attendre.
OLD>>>

<<<NEW
### Application des modèles

Les réponses aux trois questions de modèle deviennent une ligne d'en-tête, écrite avant la première
carte : `- Modèles: worker=<id>, reviewer=<id>, merger=<id>`. Elle **n'applique rien** à elle seule :
le modèle choisi est recopié en `- Modèle: <id>` sur **chaque carte du rôle** — c'est la seule forme
que le guard fait respecter (il refuse un spawn dont le modèle diffère de la ligne de l'étape).

- **Repli quota** : une carte épinglée qui meurt sur `Rate limit exceeded` ou `429` voit sa **seule**
  ligne `- Modèle:` réécrite vers `llama-local/ornith1.5-9b` (léger) ou
  `llama-proxy/ornith1.5-35b-iq3` (lourd), puis relancée ; la mention `(repli quota)` va dans
  `done.md`. Jamais pour un autre motif : l'épinglage est un ordre, pas une suggestion.
- **Modèle payant** : si Marc a tapé un id hors pool gratuit/local, tu écris aussi
  `- Modèle payant: <id>` (en-tête ou carte) — sa réponse vaut autorisation explicite.
- **Mode nuit** : seuls `llama-local/*` et `llama-proxy/*` existent ; tout modèle cloud est refusé
  physiquement.

### Repères de charge (quand Marc laisse la réponse libre)

- **Défaut** → `nvidia/nvidia/nemotron-3-ultra-550b-a55b` (tier NIM gratuit, un seul id pour les
  trois rôles).
- **Tâche légère ou moyenne** → `llama-local/ornith1.5-9b` (2 slots, MTP, `--parallel 2`, async 2
  sessions max, ~100k de contexte par session).
- **Local lourd** → `llama-proxy/ornith1.5-35b-iq3` (1 slot, 100k de contexte).
- **Deux tâches courtes indépendantes et disjointes** → `ornith1.5-9b` en **async x2** : deux
  workers `background` sur ce même modèle, jamais un autre local en parallèle.
- **`qwen-opus`** et les autres modèles locaux restent disponibles en réponse libre, caps inchangés.
- **`openrouter`** : uniquement en `:free` / `-free`.
- **Si le guard refuse pour cause de pool** → changer de fournisseur (ex. local → nvidia)
  plutôt que d'attendre.
NEW>>>

---

## t1.5 — orchestrator.md : insérer 6.6 « Role play final »

Fichier : `Opencodev2/config/opencode/agents/orchestrator.md`

Insertion : le bloc `NEW` se place **juste avant** la ligne `7. **Clôture** : quand toutes les étapes sont closes, présente un récapitulatif (étapes réalisées,` — il n'y a rien à remplacer, seulement à insérer.

<<<OLD
7. **Clôture** : quand toutes les étapes sont closes, présente un récapitulatif (étapes réalisées,
OLD>>>

<<<NEW
6.6 **Role play final** — quand Marc a répondu `Oui` : une carte de plus, `Qui: reviewer`,
   `- Résumé:` commençant par `roleplay`, `- Après:` la réception (ou la dernière carte de
   production s'il n'y en a pas), `- Entrée:` les chemins du livrable **seulement** — jamais le
   diff, jamais le manifest — et `- Retour:` le contrat role play de 6 lignes.
   La carte porte la persona, ses 3 vérifications et le critère de succès, rédigés par toi **avant**
   la question (« je comprends l'offre en 10 s », « je trouve le prix sans scroller »).
   Le reviewer joue l'utilisateur final et rend `ROLE PLAY` / `PARCOURS` / `FRICTIONS` (un endroit
   par défaut : `frame 1041`, `0:22`, `ligne 12`) / `CE QUI M'A CONVAINCU` / `JE RECOMMENCERAIS`.
   **Consultatif** : aucune correction n'est ordonnée automatiquement ; les frictions rejoignent le
   récapitulatif de clôture et `done.md`, Marc tranche. La ligne `- Verdict: PASS` de cette carte
   signifie « le test a eu lieu et son retour est enregistré », jamais « le livrable est bon ».
   Le reviewer est en lecture seule : le role play juge les artefacts déjà produits (captures,
   transcript, copie, PDF, `ffprobe`), pas un navigateur live.
7. **Clôture** : quand toutes les étapes sont closes, présente un récapitulatif (étapes réalisées,
NEW>>>

---

## t2.1 — reviewer.md : insérer « Carte de role play »

Fichier : `Opencodev2/config/opencode/agents/reviewer.md`

Insertion : le bloc `NEW` se place **juste avant** la ligne `## Interdits`.

<<<OLD
## Interdits
OLD>>>

<<<NEW
## Carte de role play

Quand ta carte porte `roleplay` dans son `- Résumé:`, tu n'es plus le reviewer du diff : tu es
**l'utilisateur final**. Tu ne lis ni le manifest, ni le diff, ni le code source — seulement le
livrable, tel que Marc le recevra, et la persona écrite dans la carte.

1. Tu adoptes la persona à la lettre : son contexte, son appareil, ce qu'elle veut obtenir.
2. Tu fais les 3 vérifications de la carte, dans l'ordre, sans en ajouter.
3. Tu ne proposes **aucune correction** : tu racontes l'expérience, avec un endroit par défaut.
4. Tu rends ce contrat, rien d'autre :

```
ROLE PLAY: <persona en une ligne>
PARCOURS: <3 à 6 étapes, dans l'ordre>
FRICTIONS:
- <endroit> | <ce que je vis> | bloquant|agaçant|cosmétique
CE QUI M'A CONVAINCU: <une ligne, ou rien>
JE RECOMMENCERAIS: oui|non — <pourquoi en une ligne>
```

Ton `- Verdict:` (PASS) certifie que **le test a eu lieu et que le retour est enregistré**, pas que
le livrable est bon : la décision appartient à Marc. Tu es en lecture seule, donc tu juges les
artefacts déjà produits (captures, transcript, copie, PDF), jamais un navigateur live.

## Interdits
NEW>>>

---

## t2.2 — orch.md : étape 4

Fichier : `Opencodev2/config/opencode/commands/orch.md`

<<<OLD
4. Montre le manifest ET pose la validation via l'outil `question` (jamais un message simple) :
   header `Valider le manifest`, question = titre + liste des étapes, options
   `Valider et créer la tuile GTD (Recommandé)` / `Modifier` / `Annuler`.
   Valider → appelle `gtd_set_objective` (nom reformulé du BUT, ≤120 signes, compréhensible sans
   contexte), puis lance t1. Modifier → ajuste et repose. Annuler → rien n'est créé, on s'arrête.
OLD>>>

<<<NEW
4. Montre le manifest ET pose **un seul appel** `question` (jamais un message simple) :
   - `Valider le manifest` : titre + liste des étapes, options `Valider + relire le plan à froid
     (Recommandé)` / `Valider sans relecture` / `Modifier` / `Annuler`. Ce libellé est exigé par le
     guard : ne le raccourcis pas, sinon la première carte worker est refusée ;
   - `Modèle des workers`, `Modèle du reviewer`, `Modèle du merger` : `Nemotron 3 Ultra 550B —
     nvidia (Recommandé)` / `Ornith 1.5-9B — local rapide` / `Ornith 1.5 35B IQ3 XXS — local lourd` ;
   - `Role play final` **seulement** si le livrable est consommé par un tiers (vidéo, page, post,
     DM, e-mail, PDF, doc client) : `Oui — avec cette proposition (Recommandé)` / `Oui — autre
     persona` / `Non`.
   Valider → `gtd_set_objective` (nom reformulé du BUT, ≤120 signes, compréhensible sans contexte),
   puis inscris en tête du manifest `- Relecture:`, `- Modèles:` et `- Role play:`, recopie le
   modèle choisi en `- Modèle:` sur chaque carte du rôle, puis lance t1. Modifier → ajuste et
   repose la question 1 seule. Annuler → rien n'est créé, on s'arrête.
NEW>>>

---

## t3.1 — SKILL.md : Geste 2, la validation

Fichier : `.opencode/skills/orchestrate/SKILL.md`

<<<OLD
Avant de lancer : pose la validation via l'outil `question` (header `Valider le manifest`,
options `Valider + relire le plan à froid (Recommandé)` / `Valider sans relecture` / `Modifier` / `Annuler`). Sur validation,
crée la tuile du chantier via `gtd_set_objective` (nom reformulé, ≤120 signes) puis lance la
première carte. Aucune tuile sans validation explicite.
OLD>>>

<<<NEW
Avant de lancer : pose **un seul appel** `question` avec la validation et les choix de modèles —
header `Valider le manifest`, options `Valider + relire le plan à froid (Recommandé)` /
`Valider sans relecture` / `Modifier` / `Annuler` (ce libellé est exigé par le guard, ne le
raccourcis pas), puis `Modèle des workers` / `Modèle du reviewer` / `Modèle du merger` :
`Nemotron 3 Ultra 550B — nvidia (Recommandé)` / `Ornith 1.5-9B — local rapide` / `Ornith 1.5 35B
IQ3 XXS — local lourd`. Ajoute `Role play final` (`Oui — avec cette proposition` / `Oui — autre
persona` / `Non`) **seulement** si le livrable est consommé par un tiers : vidéo, page, post, DM,
e-mail, PDF, doc client. Sur validation, crée la tuile du chantier via `gtd_set_objective` (nom
reformulé, ≤120 signes), inscris `- Relecture:`, `- Modèles:` et `- Role play:` en tête du
manifest, recopie le modèle choisi en `- Modèle:` sur chaque carte du rôle, puis lance la première
carte. Aucune tuile sans validation explicite.
NEW>>>

---

## t3.2 — SKILL.md : le template de manifest

Fichier : `.opencode/skills/orchestrate/SKILL.md`

<<<OLD
```markdown
# Orchestration — <titre> (<date>)
Brief: <les 5 lignes, collées telles quelles>
- Modèle payant: <id>[, <id>…]   # OPTIONNEL — autorisation explicite de Marc, vaut pour tout le chantier

## Étape 1 — <action>
- id: t1
- Qui: worker
- Périmètre: <chemins autorisés, rien d'autre>
- Entrée: <chemins/liens — jamais le transcript>
- Retour: contrat 5 lignes (FICHIERS / TESTS / CRITÈRES / BLOQUÉ)
- Vérif: <commande ou critère vérifiable à l'œil>
- Statut: annoncée

## Étape 2 — <action>
- id: t2
- Qui: worker
...

## Étape 3 — merge
- id: m1
- Qui: merger
- Modèle payant: <id>            # OPTIONNEL — autorise un modèle payant pour CETTE étape seulement
- Commandes: git -C <repo> add -A ; git -C <repo> commit -m "..." (une par ligne, dans l'ordre)
- Statut: annoncée
```
OLD>>>

<<<NEW
```markdown
# Orchestration — <titre> (<date>)
Brief: <les 5 lignes, collées telles quelles>
- Relecture: panel|aucune                              # réponse à la question 1
- Modèles: worker=<id>, reviewer=<id>, merger=<id>      # réponses aux questions 2-4 (trace)
- Role play: <persona en 6 mots>|aucune                 # réponse à la question 5
- Modèle payant: <id>[, <id>…]   # OPTIONNEL — autorisation explicite de Marc, vaut pour tout le chantier

## Étape 1 — <action>
- id: t1
- Qui: worker
- Modèle: <id choisi pour les workers>
- Périmètre: <chemins autorisés, rien d'autre>
- Entrée: <chemins/liens — jamais le transcript>
- Retour: contrat 5 lignes (FICHIERS / TESTS / CRITÈRES / BLOQUÉ)
- Vérif: <commande ou critère vérifiable à l'œil>
- Statut: annoncée

## Étape 2 — <action>
- id: t2
- Qui: worker
...

## Étape 3 — role play final (seulement si Marc a répondu Oui)
- id: rp1
- Qui: reviewer
- Modèle: <id choisi pour le reviewer>
- Après: <id de la réception ou de la dernière carte de production>
- Résumé: roleplay — <persona : qui, son contexte, ce qu'elle veut faire>
- Entrée: <chemins du livrable uniquement — jamais le diff, jamais le manifest>
- Retour: contrat role play (ROLE PLAY / PARCOURS / FRICTIONS / CE QUI M'A CONVAINCU / JE RECOMMENCERAIS)
- Vérif: <lecture seule>
- Statut: annoncée

## Étape 4 — merge
- id: m1
- Qui: merger
- Modèle: <id choisi pour le merger>
- Modèle payant: <id>            # OPTIONNEL — autorise un modèle payant pour CETTE étape seulement
- Commandes: git -C <repo> add -A ; git -C <repo> commit -m "..." (une par ligne, dans l'ordre)
- Statut: annoncée
```
NEW>>>

---

## t3.3 — SKILL.md : la phrase « le modèle n'apparaît pas dans le manifest »

Fichier : `.opencode/skills/orchestrate/SKILL.md`

<<<OLD
Le modèle n'apparaît **pas** dans le manifest : il est épinglé dans la fiche de l'agent
(`worker.md` → flash, `reviewer.md` → frontière). Un manifest ne peut donc pas le contredire.
OLD>>>

<<<NEW
Le modèle apparaît **désormais** dans le manifest : `- Modèles:` en en-tête pour la trace, et
`- Modèle:` sur chaque carte — c'est la réponse à la question de validation, et le guard refuse un
spawn dont le modèle diffère de la ligne de l'étape. La fiche d'agent garde son modèle par défaut
pour un spawn hors chantier.
NEW>>>

---

## t3.4 — SKILL.md : insérer « Geste 4ter — Le role play »

Fichier : `.opencode/skills/orchestrate/SKILL.md`

Insertion : le bloc `NEW` se place **juste avant** la ligne `## 2. Quand orchestrer, quand pas`.

<<<OLD
## 2. Quand orchestrer, quand pas
OLD>>>

<<<NEW
### Geste 4ter — Le role play final (option, livrables tiers)

Quand Marc a répondu `Oui` à la question `Role play final`, une dernière carte `Qui: reviewer`
ferme le chantier, **après la réception** : `- Résumé:` commençant par `roleplay`, `- Après:` la
réception, `- Entrée:` le livrable seul — jamais le diff, jamais le manifest. Le reviewer joue
l'utilisateur final (la persona et ses 3 vérifications sont écrites dans la carte) et raconte son
parcours avec un endroit par défaut (`frame 1041`, `0:22`, ligne 12).

**Consultatif** : aucune correction n'est ordonnée ; les frictions rejoignent le récapitulatif de
clôture et `done.md`, et Marc tranche. Son `- Verdict: PASS` certifie que le test a eu lieu, pas que
le livrable est bon. Jamais de role play sur un refacto, un script, une lib, une config ou un doc
interne : la suite de tests est le testeur final.

## 2. Quand orchestrer, quand pas
NEW>>>

---

## t4.1 — night.md : étape 3 de `/night queue`

Fichier : `Opencodev2/config/opencode/commands/night.md`

<<<OLD
3. **Une seule question** : « go ? » — rien n'est écrit avant la réponse.
OLD>>>

<<<NEW
3. **Un seul appel `question`**, posé en préparation de jour — rien n'est écrit avant la réponse :
   - `Valider le plan de nuit` : « go ? » + liste des étapes ;
   - `Modèle des workers` / `Modèle du reviewer` / `Modèle du merger` : **local uniquement** —
     `Ornith 1.5-9B — local rapide (Recommandé)` (`llama-local/ornith1.5-9b`) / `Ornith 1.5 35B IQ3
     XXS — local lourd` (`llama-proxy/ornith1.5-35b-iq3`). Nemotron et tout modèle cloud sont
     refusés physiquement par le verrou de nuit : ne les propose pas ;
   - `Role play final` **seulement** si le livrable est consommé par un tiers.
   Chaque carte du manifest de nuit porte ensuite `- Modèle: <id>` — obligatoire la nuit, le guard
   refuse un spawn sans modèle explicite — et l'en-tête `- Modèles:` pour la trace.
NEW>>>

<<<END
Fin des blocs.
END>>>
