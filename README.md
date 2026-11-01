# Pont Telegram → OpenCode (v1 + v2)

Ce pont permet de piloter des sessions OpenCode (v1 et v2) depuis Telegram via le bot `@opencode4578_bot`.

## Architecture

```
Telegram ⇄ bridge.py ──┬─ v1  http://127.0.0.1:4096   (opencode-serve.service, binaire v1)
                        └─ v2  http://127.0.0.1:4097   (opencode-serve-v2.service, headless v2)
```

- **v1** : API racine sans authentification (`/session`, `/session/{id}/message`)
- **v2** : API sous `/api/…` avec auth Basic `opencode:<OPENCODE_SERVER_PASSWORD>` (`/api/session`, `/api/session/{id}/prompt` + polling `/api/session/{id}/message`)

## Variables d'environnement

### `bridge.env` (mode 600, secrets)
Contient **tous les secrets** et la config partagée :
- `TELEGRAM_TOKEN` — token du bot `@opencode4578_bot`
- `OPENCODE_INSTANCES="v1=http://127.0.0.1:4096,v2=http://127.0.0.1:4097"` — instances déclarées
- `OPENCODE_V2_AUTH="opencode:<mot_de_passe_v2>"` — auth Basic pour l'instance v2 (même mot de passe que dans `v2.env`)
- `AUTHORIZED_CHAT_IDS=1155339708` — allowlist des chats autorisés (séparés par des virgules)
- `STATE_FILE` — chemin du fichier d'état local (défaut : `/media/marcs/Linux_Apps/Projets_AI/.opencode/tg-sessions.json`)
- `EMPLOYEE_SUPERVISOR`, `EMPLOYEE_INBOX`, `PUSH_DIR` — chemins de l'employé autonome et des ordres du jour

### `v2.env` (mode 600, secret v2 uniquement)
- `OPENCODE_SERVER_PASSWORD=<mot_de_passe_v2>` — **seul** secret de l'instance v2, lu par `opencode-serve-v2.service` via `EnvironmentFile`

> **⚠️ Jamais de secret dans une unit `644`**. Les deux fichiers `.env` sont en `600`, ignorés par git, et ne sont **jamais** affichés ni commités.

## Commandes

### Commandes du pont (jamais envoyées au modèle)

| Commande | Description |
|---|---|
| `/start` / `/help` | Affiche l'aide |
| `/status` | Instance, session active, agent, contexte (utilisé/limite) + cumul facturé et coût. |
| `/sessions [v1\|v2\|all\|suite]` | Liste paginée (10/page). Sans arg = sessions racines des deux instances. `all` = toutes, `suite` = page suivante. |
| `/select <n>` | Choisit la session n° n de la page affichée (même numérotation). |
| `/newsession [v1\|v2]` | Nouvelle session sur l'instance donnée (défaut = instance courante). |
| `/plan` | Passe l'agent en mode `plan` (lecture seule) pour la session active. |
| `/build` | Passe l'agent en mode `build` (actions) pour la session active. |
| `/compact` | Compacte le contexte de la session active. Refusé (`⏳ un compactage est déjà en cours`) si un compactage tourne déjà — pas de second envoi. |
| `/stop` (alias `/esc`) | Interrompt le tour en cours : `⏹ interrompu`, ou `❌ rien à interrompre (code N)`. |
| `/cmds` | Liste les commandes OpenCode disponibles sur l'instance. |
| `/say <texte>` | Envoie le texte **tel quel** au modèle (échappatoire pour un message qui commence par `/`). |

Les canaux spéciaux ne partent jamais dans une session :
`#emp <texte>` → employé autonome (`pause`, `resume`, `status`, `digest`, `liste`, `ok <id>`, `ko <id> "raison"`) ;
`#push <cmd>` → ordres du jour (`ok`, `skip`, `pause`, `status`).

### Commandes OpenCode (exécutées par le serveur)

`/orch`, `/task`, `/init`, `/review`, `/goal`, `/label`, `/night`… ne sont **pas** traitées par
le pont : elles sont exécutées par le serveur OpenCode via
`POST /api/session/{id}/command` (`{"name": …, "text": …}`) et **jamais envoyées au modèle**
comme du texte.

- La liste qui fait foi est `GET /api/command` (cache 5 min).
- Une commande **inconnue** est refusée : `❌ commande inconnue`, la liste des commandes
  disponibles, et **aucun POST**.
- Après l'exécution, la réponse assistant est éditée dans le message `⏳` et marquée vue
  (pas de doublon avec le miroir).

### Redémarrage

Un simple `kill` du process `bridge.py` suffit : `telegram-bridge.service` est en
`Restart=always` / `RestartSec=5`, systemd le relance automatiquement au bout de 5 s.
`systemctl restart telegram-bridge` reste possible mais n'est pas nécessaire pour recharger
le code. Aucune commande n'est nécessaire côté OpenCode.

> Au démarrage, le pont publie son menu de commandes Telegram (`setMyCommands`). Un refus de
> Telegram est journalisé et **non bloquant** : le pont démarre quand même.

### En-tête des messages

Chaque réponse du modèle (et chaque message du miroir) est préfixée par :
```
[v2] Mon titre · build · ctx 115K/200K (58%)
<réponse du modèle>
```
`ctx utilisé/limite (pourcentage)` est la place réellement occupée dans la fenêtre du modèle
(dernier message assistant, pas le cumul de facturation) ; `Mon titre` est le titre de la
session. Limite inconnue → `ctx 115K` ; session vide → `ctx 0` ; échec réseau → `ctx ?`.
Le cumul facturé et le coût sont dans `/status`.

## Modes de preuve (exécutables sans Marc)

```bash
cd /home/marcs/.telegram-bridge-venv && set -a && . ./bridge.env && set +a
./bin/python3 bridge.py --api-probe   # sonde chaque instance : kind, nb sessions, 1re session, code HTTP
./bin/python3 bridge.py --selftest    # crée session v2, envoie "Réponds exactement : PONG", vérifie PONG
./bin/python3 bridge.py --notify-test # envoie un message au chat autorisé, exige ok:true
```
Les trois doivent sortir avec **code 0**.

## Services systemd (lecture seule)

- `telegram-bridge.service` — le pont (bridge.py), `EnvironmentFile=/home/marcs/.telegram-bridge-venv/bridge.env`, `After/Wants=network-online.target`, `Restart=always`
- `opencode-serve.service` — instance v1 sur port 4096 (existant)
- `opencode-serve-v2.service` — instance v2 sur port 4097, `EnvironmentFile=/home/marcs/.telegram-bridge-venv/v2.env`, `OPENCODE_DISABLE_PROJECT_CONFIG=1`, XDG isolés

Vérifier l'état :
```bash
systemctl is-active opencode-serve opencode-serve-v2 telegram-bridge
# doit retourner "active" pour les trois
```

## Dépannage

| Problème | Commande |
|---|---|
| Voir les logs du pont | `journalctl -u telegram-bridge -n 100 -f` |
| Voir les logs v1 | `journalctl -u opencode-serve -n 100 -f` |
| Voir les logs v2 | `journalctl -u opencode-serve-v2 -n 100 -f` |
| Vérifier les ports | `ss -ltn | grep -E '4096|4097'` |
| Tester l'API v1 | `curl -s http://127.0.0.1:4096/session | jq` |
| Tester l'API v2 | `curl -s -u opencode:<mdp> http://127.0.0.1:4097/api/session | jq` |
| Relancer le pont | `systemctl restart telegram-bridge` |
| Relancer v2 | `systemctl restart opencode-serve-v2` |

## Rollback (git local uniquement)

Le dépôt est dans `/home/marcs/.telegram-bridge-venv/` (aucun remote).

```bash
cd /home/marcs/.telegram-bridge-venv
git log --oneline -10          # voir l'historique
git checkout <commit> -- .     # restaurer le commit précédent (ex: HEAD~1)
# puis relancer les services :
systemctl restart telegram-bridge opencode-serve-v2
```

## Installation (fait par Marc une seule fois)

```bash
sudo bash /home/marcs/.telegram-bridge-venv/install.sh
```
Ce script est idempotent : copie les units dans `/etc/systemd/system/`, `daemon-reload`, `enable --now opencode-serve-v2`, `restart telegram-bridge`, affiche `is-active`.

## Fichiers du projet

```
~/.telegram-bridge-venv/
├── bridge.py              # code principal
├── bridge.env             # secrets + config partagée (600, gitignored)
├── v2.env                 # secret v2 uniquement (600, gitignored)
├── install.sh             # installation idempotente des units
├── units/
│   ├── telegram-bridge.service
│   └── opencode-serve-v2.service
├── tests/                 # unittest + MockTransport (hors réseau)
├── .gitignore
└── README.md              # ce fichier
```

## Sécurité

- `AUTHORIZED_CHAT_IDS` : seul le chat `1155339708` (Marc) est autorisé par défaut.
- Tout chat non autorisé reçoit `⛔ Chat non autorisé.` et **aucun appel** ne part vers OpenCode.
- Tokens et mots de passe **jamais** dans les logs, jamais dans git, jamais dans une unit `644`.
- Backoff borné 5 s → 60 s sur erreur réseau (B6).
- Accusé `⏳` puis édition du message (B8).
- Une seule re-création de session sur 404 (B13).
- Écriture d'état atomique (tmp + `os.replace` + verrou fcntl) (B12).

## Tests

```bash
cd /home/marcs/.telegram-bridge-venv
./bin/python3 -m py_compile bridge.py          # syntaxe OK
./bin/python3 -m unittest discover -s tests    # 113 tests hors réseau (MockTransport)
```