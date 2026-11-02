# factory-ui — panneau `/factory`

Poste de pilotage de l'usine à blocs : on voit le bloc, ses règles rattachées,
la progression de son objectif, les sessions, les signaux et les incidents
groupés — **et on agit** (pause/reprise) sans quitter la TUI.

Le plugin ne décide rien : il lit `python3 mcp/opencode-factory/cli.py --ui-json`
et rend le JSON reçu en lignes mesurées. Les gestes passent par les
sous-commandes réelles de `cli.py`, jamais par une chaîne exécutée librement.

## Ce que le panneau affiche

| Ligne | Contenu |
| --- | --- |
| `BLOC` | nom, fenêtre `09:00→14:00`, état (`ouverte`, `fermée`, `EN PAUSE`), progression de l'objectif |
| règles | une ligne par règle rattachée (`produces` du bloc), indentée, avec son état |
| `SESSIONS` | nombre réel, équipes, âge — les entrées de test (`ses_test123`) ne s'affichent jamais |
| `SIGNAUX` | total et trois signaux les plus récents |
| `DÉCISION` | dernière décision : mode, actions, refus, coût |
| `INCIDENTS` | **groupés par motif**, triés par fréquence décroissante, huit groupes visibles maximum |
| `TICK` | verrou et prochain battement |
| `JOUR` | runs, incidents et coût du jour |

La progression vient de `milestones.progression()` (module backend t42). Si la
source est absente, injoignable ou si le bloc n'a pas d'objectif structuré, la
ligne affiche `inconnu : <motif>` — **jamais `0`** : un zéro inventé ferait
croire que rien n'a été produit.

Les incidents sont regroupés par motif :

```
▸ dry_run — 142 fois — 09:12 → 13:58 — 1re : mode=dry_run
```

`entrée` sur un groupe le déplie (fenêtre d'incidents), `entrée` à nouveau le
replie.

## Touches réelles

| Touche | Effet | Backend |
| --- | --- | --- |
| `p` | pause ou reprise des récurrents | `cli.py factory pause --raison "panneau"` / `cli.py factory resume` |
| `r` | rafraîchit le relevé | `cli.py --ui-json` |
| `entrée` | déplie / replie un groupe d'incidents | — (état local du panneau) |
| `q` | quitte le panneau | — |

Chaque action affiche le code de sortie et la sortie du backend :

```
OK   python3 …/cli.py factory pause --raison panneau → code 0 · pause posée depuis …
ÉCHEC python3 …/cli.py factory resume → code 2 · ERREUR : reprise non enregistrée (…)
```

Une action qui échoue ne prétend jamais avoir réussi : le code et le message
d'erreur restent affichés en tête du panneau.

## Ancien contrat : actions indisponibles

Jusqu'à t30, `cli.py` n'exposait que `--ui-json` et le panneau était en lecture
seule : aucune touche `s`/`p`/`m` n'était proposée. Depuis t45, `factory
pause|resume` existent et sont branchées ; les autres gestes (démarrer/arrêter
le bloc, changer `max_sessions`) restent absents et **aucune touche ne les
simule**. Pour en ajouter un, il faudra une sous-commande réelle dans `cli.py`
puis une entrée explicite dans `CLI_APPELS` (`tui.ts`) — le test de contrat
refuse toute option inconnue du backend.

## Lecture seule côté fichiers

| Ce que le plugin fait | Ce qu'il ne fait pas |
| --- | --- |
| lit `cli.py --ui-json`, `blocks.json`, `recurring-tasks.json`, `paused.json` | aucune écriture fichier depuis TypeScript |
| appelle `factory pause` / `factory resume` (le backend écrit) | aucun `exec` de chaîne arbitraire |
| affiche `—` + la raison quand un champ vaut `null` | jamais `0` à la place d'une donnée absente |

Le plugin n'écrit ni `blocks.json`, ni `state.json`, ni aucune configuration
OpenCode. Un incident ou une session mentionnant `ses_test123` est écarté de
l'affichage, et l'entrée a été purgée de `.opencode/factory/state.json` avec
sauvegarde `.bak-<horodatage>` (écriture atomique tmp + `os.replace`).

## Activation — v2, depuis le dossier, sans ligne de config

**Rien n'est enregistré automatiquement.** Ce plugin n'ajoute aucune entrée de
configuration : ni `.opencode/opencode.json`, ni
`Opencodev2/config/opencode/opencode.json` ne sont modifiés. Aucun des deux ne
doit l'être pour que le plugin marche.

Le dossier est complet, et c'est tout ce qui est nécessaire :

```
Opencodev2/config/opencode/plugins/factory-ui/
├── package.json   # name, private, type module, exports "." et "./tui"
├── index.ts       # Plugin.define({ id: "factory-ui.server", setup() {} })
└── tui.ts         # la commande /factory
```

C'est exactement la forme de `orch-ui`, `runs-ui`, `recurring-ui` et
`employee-ui`, qui vivent dans le même dossier `plugins/` et qui n'ont **aucune**
entrée dans `opencode.json` : le dossier est découvert tel quel.

### La ligne v1 ne serait pas lue

La TUI tourne en v2 et son lanceur pose `OPENCODE_DISABLE_PROJECT_CONFIG=1` :
**`.opencode/opencode.json` n'est pas lu**. Y ajouter
`"…/plugins/factory-ui"` dans son tableau `plugin` n'aurait aucun effet — c'est
une déclaration v1. Ne pas le faire.

Si (et seulement si) la découverte du dossier s'avérait insuffisante, la forme v2
d'une entrée — tableau `"plugins"` de
`Opencodev2/config/opencode/opencode.json` — est un objet `package`, comme pour
le plugin Goal :

```json
{ "package": "/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/factory-ui" }
```

Ce cas n'est pas nécessaire aujourd'hui : les quatre panneaux voisins se chargent
sans entrée.

Activer = relancer `./opencode-v2` ; `/factory` apparaît alors dans la palette.

## Retrait en trois commandes (si le TUI ne démarre plus)

```sh
cp Opencodev2/config/opencode/opencode.json Opencodev2/config/opencode/opencode.json.bak-factory
sed -i '/factory-ui/d' Opencodev2/config/opencode/opencode.json
mv Opencodev2/config/opencode/plugins/factory-ui /tmp/opencode/factory-ui.off
```

Puis relancer `./opencode-v2`. Le `mv` suffit à lui seul à empêcher le
chargement ; la copie de sauvegarde et le `sed` ne servent qu'à nettoyer une
éventuelle entrée `package` ajoutée à la main. Pour revenir en arrière :
`mv /tmp/opencode/factory-ui.off Opencodev2/config/opencode/plugins/factory-ui`.

## Vérification

```sh
python3 mcp/opencode-factory/tests/test_tui_contract.py
python3 mcp/opencode-factory/tests/test_ui_json.py
```

`test_ui_json.py` vérifie que `--ui-json` reste exploitable (sept sections,
erreur par section sans plantage global) et que le groupement d'incidents donne
les bons comptes et le bon ordre sur un jeu de 175 incidents à deux motifs.
`test_tui_contract.py` vérifie que chaque option appelée par `tui.ts` existe
dans `cli.py` et qu'aucun fichier du plugin n'écrit de configuration. Ni l'un ni
l'autre ne démarre le serveur : le rendu visuel réel reste à contrôler à la main
par Marc.
