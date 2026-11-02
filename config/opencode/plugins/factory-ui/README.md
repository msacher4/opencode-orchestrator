# factory-ui — panneau `/factory`

Commande TUI qui montre ce que fait l'usine à blocs sans lire un journal :
bloc courant et temps restant, sessions par équipe, signaux anormaux, dernière
décision, incidents récents, prochain tick.

Le plugin ne calcule **rien** : il appelle
`python3 mcp/opencode-factory/cli.py --ui-json` et rend le JSON reçu en texte
compact. Toute la vérité est dans le backend.

## Lecture seule

| Ce que le plugin fait | Ce qu'il ne fait pas |
| --- | --- |
| lit la sortie de `cli.py --ui-json` | aucune écriture fichier |
| affiche `—` + la raison quand un champ vaut `null` | jamais `0` à la place d'une donnée absente |
| ouvre une boîte de dialogue TUI | aucun appel réseau, aucune écriture en base |

`cli.py` ouvre la base OpenCode en `mode=ro` et ne sonde pas le CDP. Le plugin
n'écrit ni `blocks.json`, ni `state.json`, ni aucune configuration OpenCode.

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

## Actions indisponibles

`/factory` **n'affiche aucune touche `s` / `p` / `m`**. `cli.py` n'expose
aujourd'hui que `--ui-json` : il n'existe aucune sous-commande pour démarrer ou
arrêter le bloc, mettre en pause, ou changer `max_sessions` (1 ou 2). Le plugin
n'invente pas d'appel et ne propose pas une touche qui ne ferait rien.

Quand le backend les exposera (par exemple `--bloc-start`, `--bloc-stop`,
`--bloc-pause`, `--max-sessions`), il suffira d'ajouter l'entrée correspondante
dans le bloc `CLI_APPELS` de `tui.ts` ; le test de contrat
`mcp/opencode-factory/tests/test_tui_contract.py` refusera toute option absente
de `cli.py`.

## Vérification

```sh
python3 mcp/opencode-factory/tests/test_tui_contract.py
```

Le test lit le bloc `CLI_APPELS` de `tui.ts`, vérifie que chaque option existe
dans `cli.py`, et vérifie qu'aucun fichier du plugin ne mentionne ni n'écrit
`.opencode/opencode.json`. Il ne démarre pas le serveur : le rendu visuel dans
la TUI reste à contrôler à la main par Marc.
