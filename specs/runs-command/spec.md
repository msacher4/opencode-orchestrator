# Spec — commande `/runs` : voir et tuer les sessions actives

Chantier : une commande TUI OpenCode qui affiche **tout ce qui tourne** (sessions racines ET
subagents), triée en arbre, avec un kill manuel vérifié.

Repo : `/media/marcs/Linux_Apps/Projets_AI/Opencodev2` (remote `msacher4/opencode-orchestrator`).

## Problème à résoudre

Marc a des agents qui tournent en arrière-plan sans pouvoir les voir ni les arrêter :
- la TUI ne montre que 237 sessions racines sur 1216 ; 979 sont des sessions enfants ;
- 271 sessions sont restées « en cours » (`time_idle` NULL), 4 tournent réellement ;
- le bouton Stop du proxy annule la requête HTTP, pas la session → l'agent reçoit
  « previous response was interrupted, continue » et repart.

La commande doit donner : **une liste lisible, triée, hiérarchique, et un vrai kill de session.**

## Fichiers à créer (4, rien d'autre)

```
Opencodev2/config/opencode/plugins/runs-ui/package.json
Opencodev2/config/opencode/plugins/runs-ui/index.ts
Opencodev2/config/opencode/plugins/runs-ui/runs-logic.ts
Opencodev2/config/opencode/plugins/runs-ui/tui.ts
```

Modèle à copier pour la structure : `plugins/orch-ui/` (même découpage index/tui, même style).
`index.ts` est un no-op côté serveur :

```ts
import { Plugin } from "@opencode/plugin"
export default Plugin.define({ id: "runs-ui.server", setup() {} })
```

`package.json` :

```json
{
  "name": "opencode-runs-ui",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "exports": { ".": "./index.ts", "./tui": "./tui.ts" }
}
```

## Source de données (API v2, vérifiée dans le SDK)

- `ctx.client.session.list({ limit: 500 })` → `{ data: SessionV2Info[], cursor }`
  champs utiles : `id`, `parentID?`, `agent?`, `model?.id`, `model?.providerID`, `title`,
  `time.updated`, `location.directory`.
- `ctx.client.session.active()` → `{ [sessionID]: { type: "running" } }`.
- Kill : `ctx.client.session.interrupt({ sessionID })`.
  Fallback REST si la méthode SDK échoue : `POST ${base}/api/session/{id}/interrupt`,
  base = `ctx.client.baseURL ?? ctx.client.options?.baseURL ?? process.env.OPENCODE_BASE_URL ?? "http://127.0.0.1:4096"`.

## `runs-logic.ts` — module pur, ZÉRO import (testable isolément)

API exacte exigée :

```ts
export type RunRow = {
  id: string
  parentID?: string
  agent: string      // "?" si absent
  model: string      // "provider/id" ou "?" si absent
  title: string
  updated: number    // ms
  running: boolean
}

export type RunGroup = {
  root: RunRow | null      // null => groupe orphelin
  parentID: string
  parentTitle: string      // "?" si le parent n'est pas dans la liste
  children: RunRow[]
  updated: number          // max(updated) du groupe
}

export function buildRuns(input: {
  sessions: any[]
  active: Record<string, any>
  now: number
  all: boolean
}): RunGroup[]

export function formatAge(ms: number): string
export function summarize(groups: RunGroup[]): { total: number; running: number }
```

Règles :

1. `running` = `Boolean(active[s.id] && active[s.id].type === "running")`.
2. `all === false` → ne garder que les sessions `running`. `all === true` → tout garder.
3. Regroupement : une session sans `parentID` est une racine (groupe avec `root` = elle-même,
   `children` = ses enfants retenus).
   **Parent de contexte** : si un enfant est retenu et que son parent existe dans `sessions`
   mais n'est pas retenu par le filtre, le parent est quand même affiché comme `root` du
   groupe, avec `running: false`, et `parentTitle` = son titre. Il sert de contexte, jamais
   de ligne active.
4. Une session enfant dont le parent est retenu — ou parent de contexte (règle 3) — va dans
   `children` du groupe de ce parent.
4bis. **Orphelin strict** : enfant retenu dont le parent est **absent** de `sessions` →
   `root: null`, `parentID` renseigné, `parentTitle: "(parent hors liste)"`.
5. Tri : groupes par `updated` décroissant ; enfants par `updated` décroissant. Tri stable.
5bis. **Aucune session retenue ne disparaît** : après regroupement, toute session retenue qui
   n'est rattachée à aucun groupe (cas type : petit-enfant dont le parent est lui-même un
   enfant) est placée dans un groupe orphelin — `root: null`, `parentID` = son parent direct,
   `parentTitle` = titre du parent direct s'il existe dans `sessions`, sinon `"(parent hors liste)"`.
6. `formatAge(ms)` : reçoit une **DURÉE** en millisecondes (pas un timestamp). `< 1 min` →
   `"à l'instant"`, `< 60 min` → `"N min"`, `< 24 h` → `"N h"`, sinon `"N j"` (arrondi bas).
   L'UI appelle donc `formatAge(now - row.updated)` — jamais `formatAge(row.updated)`.
7. `summarize` : `total` = nombre exact de lignes affichées = `(1 si root existe) + children.length`,
   **parents de contexte inclus** ; `running` = nombre de lignes avec `running === true`.
8. Jamais d'exception sur une entrée malformée : champs manquants → `"?"`, tableaux vides → `[]`.

## Détection multi-instance (correctif du 2026-10-29)

`session.active()` ne renvoie que les sessions **possédées par le process OpenCode courant**
(comme `interrupt` : *owned by this OpenCode process*). Les sessions qui tournent dans le daemon
`opencode serve --service` (registre `Opencodev2/state/opencode/service.json`) ou dans un autre
serveur local sont donc invisibles → la commande affiche « aucune session active » à tort.

Source de vérité des instances locales :

- **instance courante** : `ctx.client` (base = `ctx.client.baseURL ?? ctx.client.options?.baseURL`,
  auth déjà portée par le client) ;
- **daemon** : lire `Opencodev2/state/opencode/service.json` → `{ "url": "http://127.0.0.1:PORT", "password": "..." }`,
  auth = `Basic base64("opencode:" + password)` (schéma confirmé le 2026-10-29 : 200 avec
  `opencode:<password>`, 401 sinon).
  **Le mot de passe ne doit jamais être affiché, loggé, ni écrit** — il ne sert qu'à construire l'en-tête.
  Fichier absent / JSON invalide / pas de `password` → instance ignorée, jamais d'exception.

Agrégation : pour chaque instance, `GET {base}/api/session/active` → `{data:{[sessionID]:{type:"running"}}}`.
On fusionne en `sessionID → instanceID` (l'instance courante gagne en cas de doublon).
Un kill cible **l'instance propriétaire** de la session (celle qui l'a déclarée `running`) ;
si la session n'est déclarée nulle part, on retombe sur l'instance courante.

### `instances.ts` — module pur, ZÉRO import

```ts
export type Instance = { id: string; base: string; auth: string | null }

export function buildAuth(user: string, password: string): string        // "Basic " + base64(user + ":" + password)
export function parseService(raw: string | null, fallbackBase?: string): Instance | null
export function mergeActive(input: {
  ownID: string
  own: Record<string, any>
  others: Array<{ id: string; active: Record<string, any> }>
}): { owner: Record<string, string>; running: string[] }
export function instanceFor(sessionID: string, owner: Record<string, string>, ownID: string): string
```

Règles :
- base64 maison (aucune dépendance, pas de `Buffer`, pas de `btoa`) : alphabet standard, padding `=`.
- `parseService` accepte `{url,password}` ; `url` trimé sans `/` final ; `id` = `"service"`.
  `password` vide → `auth: null` (mais l'instance reste utilisable).
- `mergeActive` : `ownID` prioritaire, puis les `others` dans l'ordre ; `running` trié par ordre
  d'apparition, sans doublon.
- `instanceFor` : `owner[sessionID]` sinon `ownID`.
- aucune exception sur entrée malformée (`raw` null, JSON invalide, `others` non tableau).

Vérification (déterministe, sans réseau) :

```bash
NODE=/media/marcs/Linux_Apps/.config/nvm/versions/node/v22.23.1/bin/node
$NODE --experimental-strip-types -e '
import("file:///media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/runs-ui/instances.ts").then(m => {
  if (m.buildAuth("opencode", "abc") !== "Basic b3BlbmNvZGU6YWJj") throw new Error("auth: " + m.buildAuth("opencode", "abc"));
  const svc = m.parseService(JSON.stringify({ id: "x", url: "http://127.0.0.1:49374/", password: "pw" }));
  if (!svc || svc.base !== "http://127.0.0.1:49374" || svc.auth !== "Basic b3BlbmNvZGU6cHc=" || svc.id !== "service") throw new Error("service: " + JSON.stringify(svc));
  if (m.parseService("pas du json") !== null) throw new Error("json invalide accepté");
  if (m.parseService(null) !== null) throw new Error("null accepté");
  const merged = m.mergeActive({
    ownID: "own",
    own: { ses_a: { type: "running" } },
    others: [{ id: "service", active: { ses_a: { type: "running" }, ses_b: { type: "running" } } }]
  });
  if (merged.owner.ses_a !== "own" || merged.owner.ses_b !== "service") throw new Error("merge: " + JSON.stringify(merged));
  if (merged.running.length !== 2) throw new Error("running: " + JSON.stringify(merged.running));
  if (m.instanceFor("ses_b", merged.owner, "own") !== "service") throw new Error("instanceFor");
  if (m.instanceFor("ses_zzz", merged.owner, "own") !== "own") throw new Error("instanceFor fallback");
  console.log("OK", JSON.stringify(merged.running));
});
'
```

Attendu : `OK ["ses_a","ses_b"]` (code 0).

## `tui.ts` — commande `/runs`

- **Détection** : `active` = fusion de `await client.session.active()` (instance courante, forme
  `{data:{...}}` ou objet direct — accepter les deux) et de `GET {service.base}/api/session/active`
  (daemon, avec `Authorization: service.auth`). Utiliser `mergeActive` de `./instances.ts`.
  Une session est `running` si elle figure dans la fusion.
- **Kill** : router vers l'instance propriétaire (`instanceFor`). Si c'est l'instance courante →
  `ctx.client.session.interrupt(...)` puis fallback REST local ; sinon → `POST {base}/api/session/{id}/interrupt`
  avec l'en-tête d'auth de cette instance. En dernier recours seulement, réessayer sur l'instance courante.
- **Description d'une ligne** : mentionner l'instance propriétaire (`service` ou `local`) quand ce
  n'est pas l'instance courante, pour qu'on sache d'où vient le run.
- **Une seule source de vérité** : `tui.ts` importe `buildRuns`, `formatAge`, `summarize` depuis
  `./runs-logic.ts` (`import { buildRuns, formatAge, summarize } from "./runs-logic.ts"`).
  **Aucune duplication** de cette logique dans `tui.ts` (pas de « copié depuis... »).
- Enregistrement identique à orch-ui :
  `ctx.keymap.layer(() => ({ mode: "global", priority: 10, commands: [ ... ] }))`
  + `ctx.ui.slot({ append: "app", render: () => Commands(ctx) })` dans `setup`.
- Commande : `id: "runs.open"`, `title: "Runs — sessions actives"`,
  `description: "Liste triée des sessions actives (subagents sous leur parent) + kill"`,
  `group: "OpenCode"`, `palette: true`, `slash: { name: "runs", arguments: true }`.
- Argument `all` (`/runs all`) → inclut les sessions non actives (plafond 50 lignes).
- Affichage : `ctx.ui.dialog.select` avec, dans l'ordre :
  1. une ligne d'en-tête désactivée : `"● N actives · M sessions"` ;
  2. pour chaque groupe : la racine (ou `"(orphelin) parent — titre"`), puis ses enfants
     précédés de `"  └ "` ;
  3. `"⛔ Tout arrêter (N)"` si plus d'une session active ;
  4. `"Annuler"`.
- Format d'une ligne :
  `(● ou ·) agent · model · il y a X · titre` (titre tronqué à 48 caractères),
  `description` = `"ses_…47 — parent: ses_…12 \"titre parent\""`.
- Sélection d'une session → **confirmation obligatoire** (`dialog.select` :
  `"⛔ Kill cette session"` / `"Annuler"`) avant tout appel.
- `⛔ Tout arrêter` → confirmation, puis interrupt séquentiel de chaque session active,
  avec un rapport final `X arrêtées, Y échecs` (jamais de succès affiché sans vérification).
- Après un interrupt : re-lire `active()` ; si la session y est toujours, l'annoncer
  explicitement comme échec (`"toujours active après interrupt"`).
- Aucun kill sans passage par une confirmation. Aucune écriture disque. Aucun appel `git`.
- Erreurs : `try/catch` autour de chaque appel API, message dans `dialog.alert`, jamais de throw.

## Interdits (worker)

- Ne pas modifier `orch-guard.ts`, `recurring-ui/`, `employee-ui/`, `orch-ui/`, ni aucun
  `opencode.json`, ni `.opencode/orchestration.md`.
- Aucune dépendance ajoutée, aucun `npm install`, aucun `bun add`.
- Aucun `git add/commit/push` (le merger s'en charge).
- Aucun kill réel pendant le développement (pas d'appel `interrupt` sur une vraie session).

## Vérification (déterministe, sans réseau ni SDK)

```bash
NODE=/media/marcs/Linux_Apps/.config/nvm/versions/node/v22.23.1/bin/node
$NODE --experimental-strip-types -e '
import("file:///media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/runs-ui/runs-logic.ts").then(m => {
  const sessions = [
    { id: "ses_p", title: "Parent", agent: "build", model: { id: "m1", providerID: "p" }, time: { updated: 1000 } },
    { id: "ses_c", parentID: "ses_p", title: "Enfant", agent: "worker", model: { id: "m2", providerID: "p" }, time: { updated: 3000 } },
    { id: "ses_o", parentID: "ses_absent", title: "Orphelin", agent: "reviewer", model: { id: "m3" }, time: { updated: 2000 } }
  ];
  const active = { ses_c: { type: "running" }, ses_o: { type: "running" } };
  const g = m.buildRuns({ sessions, active, now: 5000, all: false });
  if (g.length !== 2) throw new Error("groupes: " + g.length);
  const withRoot = g.find(x => x.root && x.root.id === "ses_p");
  if (!withRoot || withRoot.children.length !== 1 || withRoot.children[0].id !== "ses_c") throw new Error("arbre racine");
  const orphan = g.find(x => x.root === null);
  if (!orphan || orphan.parentID !== "ses_absent" || orphan.parentTitle !== "(parent hors liste)") throw new Error("orphelin");
  if (g[0].updated < g[1].updated) throw new Error("tri");
  if (m.formatAge(30 * 1000) !== "à l\'instant") throw new Error("age1: " + m.formatAge(30 * 1000));
  if (m.formatAge(50 * 60000) !== "50 min") throw new Error("age2");
  if (m.formatAge(2 * 3600000) !== "2 h") throw new Error("age3");
  if (m.formatAge(3 * 86400000) !== "3 j") throw new Error("age4");
  if (withRoot.root.running !== false) throw new Error("parent de contexte marqué running");
  const s = m.summarize(g);
  if (s.running !== 2) throw new Error("summarize.running: " + JSON.stringify(s));
  if (s.total !== 3) throw new Error("summarize.total: " + JSON.stringify(s));
  // petit-enfant retenu dont le parent est lui-même un enfant : ne doit PAS disparaître
  const g2 = m.buildRuns({
    sessions: [
      { id: "ses_r", title: "R", time: { updated: 1 } },
      { id: "ses_m", parentID: "ses_r", title: "M", time: { updated: 2 } },
      { id: "ses_g", parentID: "ses_m", title: "G", time: { updated: 3 } }
    ],
    active: { ses_g: { type: "running" } },
    now: 10000,
    all: false
  });
  const flat = g2.reduce((acc, x) => acc.concat(x.root ? [x.root] : [], x.children), []);
  if (!flat.some((x) => x.id === "ses_g")) throw new Error("petit-enfant invisible");
  console.log("OK", JSON.stringify(s));
});
'
```

Attendu : `OK {"total":3,"running":2}` (code de sortie 0).

## DONE QUAND

- les 4 fichiers existent, `runs-logic.ts` n'a **aucun** `import` ;
- la commande de vérification ci-dessus sort `OK` avec code 0 ;
- la review rend PASS sur le diff ;
- le commit est poussé sur `orch/runs-command` (jamais `main`).
