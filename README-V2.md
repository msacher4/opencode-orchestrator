# OpenCode v2 — configuration isolée (Projets_AI)

Ce dossier contient une installation **OpenCode v2 totalement isolée** de la v1.
Rien dans `../.opencode/` (plugins, config, `package.json`) n'est modifié : la v1
continue de fonctionner comme avant.

## Lancement

```sh
./opencode-v2            # TUI v2 (--standalone)
./opencode-v2 run ...    # mode run
./opencode-v2 <cmd>      # toute autre commande
```

Variables posées par le lanceur :

| Variable | Valeur |
| --- | --- |
| `XDG_CONFIG_HOME` | `Opencodev2/config` |
| `XDG_DATA_HOME` | `Opencodev2/data` |
| `XDG_STATE_HOME` | `Opencodev2/state` |
| `XDG_CACHE_HOME` | `Opencodev2/cache` |
| `OPENCODE_DB` | `Opencodev2/data/opencode/opencode.db` |
| `OPENCODE_DISABLE_PROJECT_CONFIG` | `1` |

## Pourquoi `OPENCODE_DISABLE_PROJECT_CONFIG=1` ?

La v1 déclare ses plugins dans `../.opencode/opencode.json` (`plugin: [...]`) et
dans `../.opencode/plugins/*.js`, au format **API v1**. La v2 charge ces mêmes
fichiers automatiquement (config projet + découverte `.opencode/plugins/`) mais
ne sait pas exécuter l'API v1 : d'où les 6 erreurs « plugins failed » au
démarrage (3 découvertes + 3 entrées de config relative).

Désactiver la config projet pour la v2 est le seul moyen de ne plus charger ces
fichiers. Tout ce dont la v2 a besoin est donc **mirroiré** ici :

- `config/opencode/opencode.json` — MCP (serena, memento, headroom,
  obsidian-rag, n8n-mcp, gtd-kanban, ruflo/comfyui désactivés), `instructions`,
  `skills`, providers (`llama-local`, `opencode-cloud`, `llama-proxy`) et le
  plugin Goal.
- `config/opencode/agents` → lien vers `../../.opencode/agents`
- `config/opencode/commands` → lien vers `../../.opencode/commands`
- `config/opencode/plugins/*.ts` — ports v2 des plugins locaux.

> ⚠️ Si tu modifies les MCP / providers / instructions dans
> `../opencode.json`, reporte le changement dans
> `config/opencode/opencode.json` : la v2 ne lit plus le fichier projet.

## Plugins portés en API v2

| Fichier | Origine v1 | Rôle |
| --- | --- | --- |
| `plugins/graphify.ts` | `.opencode/plugins/graphify.js` | Rappel graphify avant les commandes shell |
| `plugins/gtd-sync.ts` | `.opencode/plugins/gtd-sync.js` | Outils `gtd_set_label`, `gtd_get_label`, `gtd_set_objective` (même état partagé : `.opencode/gtd-current.json`) |
| `plugins/notify-desktop.ts` | `.opencode/plugins/notify-desktop.js` | `notify-send` en fin de tâche, permission, formulaire, erreur |

Le plugin Goal (`@prevalentware/opencode-goal-plugin`, ≥ 0.1.67) est déclaré
dans `config/opencode/opencode.json` (la version 0.1.64 du cache v2 plantait
avec `Schema.optionalWith is not a function`).

## Sauvegarde

L'ancienne config v2 est conservée :
`config/opencode/opencode.json.bak-v1-*`.
