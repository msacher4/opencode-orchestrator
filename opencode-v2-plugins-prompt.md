/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/employee-guard.ts
---
// employee-guard — OpenCode v2 plugin
//
// Garde-fou des deux agents de l'employé (`employee` = worker, `planner`).
//   1. AUDIT   : chaque tool call est journalisé dans Employe/audit.jsonl
//   2. BLOCAGE : rien qui publie / envoie / pousse / paie ; rien qui écrit hors du
//                périmètre de l'employé.
//
// Les deux variantes de chemin sont testées (Employe/... et l'ancien
// .opencode/employee/... — un lien symbolique les relie, mais les sessions
// lancées avec un ancien brief écrivent encore avec l'ancien chemin).
//
// La LECTURE des réseaux sociaux est autorisée (les leads sont sur X et Reddit) :
// seules les écritures sont bloquées. Écritures vers la boucle locale autorisées
// (automatisations de Marc).
//
// CE FICHIER EST RECHARGÉ À CHAUD PAR LE SERVEUR : l'écrire toujours d'un seul
// coup. Une version incohérente lève une erreur sur TOUS les tool calls de
// TOUTES les sessions (y compris celles de Marc).
import { appendFileSync, mkdirSync } from "node:fs"
import { dirname } from "node:path"

const PROJECT = process.env.EMPLOYEE_PROJECT || "/media/marcs/Linux_Apps/Projets_AI"
const AUDIT = process.env.EMPLOYEE_AUDIT || PROJECT + "/Employe/audit.jsonl"
const GUARDED = new Set(["employee", "planner"])

// Clés qui portent une ACTION (commande, URL, chemin), jamais le texte descriptif.
const CMD_KEYS = ["command", "cmd", "script", "code"]
const URL_KEYS = ["url", "uri", "href"]
const PATH_KEYS = ["filePath", "path", "file", "target", "destination", "notebookPath", "dir"]

const LOCAL = /(127\.0\.0\.1|localhost|::1|\b0\.0\.0\.0\b)/
// ancien chemin (via symlink) OU nouveau chemin visible
const E = "(?:\\.opencode/employee|Employe)"

// --- Outils dont le nom seul suffit à interdire l'usage ---
const DENY_TOOLS = [
  { re: /gtd_(complete|move|reopen|set_objective|set_label)/i, why: "le board GTD est en lecture seule" },
  { re: /n8n/i, why: "les workflows n8n ne se pilotent pas depuis un shift" },
  { re: /comfyui/i, why: "la génération ComfyUI ne se déclenche pas depuis un shift" },
  { re: /session_(move|rename)|worktree_/i, why: "pilotage d'OpenCode hors périmètre" },
  { re: /recurring_(tick|add|delete|run_goal|watch_goals)/i, why: "le moteur de récurrences n'est pas une tâche de shift" },
]

// --- Secrets : ni lus, ni recopiés ---
const DENY_SECRET = [
  { re: /telegram\.env|X_AUTH_TOKEN|X_CT0|LLAMA_SWITCH_TOKEN|\.aws\/credentials|id_rsa|Authorization:\s*Bearer/i, why: "secret hors périmètre" },
  { re: /(^|[\s/='"])[-.\w]*\.env(\s|$|['"])/, why: "lecture d'un fichier .env" },
  { re: /Opencodev2\/config\/opencode\//, why: "configuration OpenCode (lecture incluse)" },
]

// --- Écritures vers l'extérieur (commandes et URLs) ---
const DENY_NETWORK = [
  { re: /\bgit\s+push\b/, why: "git push" },
  { re: /\bnpm\s+publish\b|\bpip\s+upload\b|\btwine\s+(publish|upload)\b/, why: "publication de paquet" },
  { re: /\/2\/tweets\b|\/statuses\/update|\/friendships\/create|\/favorite\/create|\/retweets\/create|\/direct_messages\/events/, why: "X/Twitter en écriture" },
  { re: /reddit\.com\/api\/(submit|comment|vote|edit_user|comply)|\/api\/user\/(friend|subscribe)/i, why: "Reddit en écriture" },
  { re: /graph\.facebook\.com\/[^?\s]*(feed|photos|videos|messages)|linkedin\.com\/(ugcPosts|rest\/li\/comments)|tiktok\.com\/v2\/(post|upload)|instagram\.com\/api\/v1\/media/i, why: "API d'écriture réseau social" },
  { re: /api\.telegram\.org\/bot[^/\s]+\/(sendMessage|sendPhoto|sendMedia|sendDocument|sendVideo)/i, why: "Telegram direct vers l'extérieur (utiliser contact_telegram, qui écrit chez Marc)" },
  { re: /\b(sendmail|msmtp|mutt\s+-s|mail\s+-s)\b/, why: "envoi d'email" },
  { re: new RegExp(":8787/api/(add|move|complete|update|delete|archive)", "i"), why: "Kanban GTD en écriture" },
  { re: /\bsudo\b|systemctl\s+(restart|stop|start|kill|daemon-reload)|\bmkfs\b|chmod\s+-R\s+777/, why: "action système privilégiée" },
  { re: new RegExp("\\brm\\s+(-[a-zA-Z]*[rf][a-zA-Z]*\\s+)+(\\/(\\s|$)|\\/\\w+(\\s|$)|~|\\$HOME|\\/home\\/)", ""), why: "suppression récursive d'un chemin système" },
  { test: (t) => /\b(curl|wget)\b/.test(t) && (/\s(-X|--request)\s+(POST|PUT|PATCH|DELETE)\b/i.test(t) || /(^|\s)(-{1,2}data(-binary|-ascii)?|-F|-T|--upload-file|-d)(\s|$)/.test(t)) && !LOCAL.test(t), why: "requête HTTP en écriture vers l'extérieur" },
  { test: (t) => /\b(requests|httpx|aiohttp|urllib|fetch|axios)\b[^\n]*[.(](post|put|patch|delete)\s*\(/i.test(t) && !LOCAL.test(t), why: "appel HTTP en écriture depuis du code" },
  { test: (t) => /method\s*=\s*["'](POST|PUT|PATCH|DELETE)["']/i.test(t) && !LOCAL.test(t), why: "méthode HTTP en écriture vers l'extérieur" },
]

// Cibles d'écriture réelles d'une commande shell : on ne juge QUE ce qui est écrit.
// Avant, une simple MENTION de .opencode/skills/ dans une commande qui redirigeait sa
// sortie bloquait l'exécution d'un script en lecture seule (harvest.py, reddit_tracker.py).
function writePaths(text) {
  const out = []
  for (const m of String(text).matchAll(/>{1,2}\s*([^\s;|&]+)/g)) out.push(m[1])
  for (const m of String(text).matchAll(/\b(?:tee|touch|mkdir|truncate)\b([^\n;|&]*)/g)) out.push(...m[1].trim().split(/\s+/))
  for (const m of String(text).matchAll(/\b(?:rm|chmod|chown)\b([^\n;|&]*)/g)) out.push(...m[1].trim().split(/\s+/))
  for (const m of String(text).matchAll(/\bsed\s+-[a-zA-Z]*i\b([^\n;|&]*)/g)) out.push(...m[1].trim().split(/\s+/))
  for (const m of String(text).matchAll(/\b(?:cp|mv)\s+([^\n;|&]+)/g)) {
    const args = m[1].trim().split(/\s+/).filter((a) => !a.startsWith("-"))
    if (args.length) out.push(args[args.length - 1]) // destination = dernier argument
  }
  return out.filter((a) => a && !a.startsWith("-") && !a.includes("&"))
}

// --- Écritures de fichiers hors périmètre (chemins uniquement) ---
const DENY_WRITE = [
  { re: new RegExp(E + "/poste/docs/|Employe/docs/|employee/poste/docs/"), why: "docs/ est en lecture seule, c'est Marc qui y dépose ses documents" },
  { re: new RegExp(E + "/(poste/)?objectifs\\.md"), why: "objectifs.md est piloté par Marc via la popup /employee" },
  { re: new RegExp(E + "/(poste/)?fiche-de-poste\\.md"), why: "la fiche de poste appartient à Marc (propose une révision en note)" },
  { re: new RegExp(E + "/missions\\.json"), why: "les missions sont activées par Marc (proposition via plan.json → notes)" },
  { re: new RegExp(E + "/competences\\.json"), why: "le pool de compétences est assigné par Marc via la popup /employee" },
  { re: new RegExp(E + "/state\\.json"), why: "l'état est piloté par le superviseur (#emp pause / resume)" },
  { re: new RegExp(E + "/plan\\.json"), why: "plan.json appartient au planner", plannerOk: true },
  { re: new RegExp(E + "/(inbox|inbox-consumed)\\.jsonl"), why: "l'inbox est écrit par le pont Telegram" },
  { re: /Opencodev2\/config\/opencode\/|\.opencode\/(agents|commands|plugins|skills)/, why: "configuration OpenCode" },
  { re: /\/media\/marcs\/Linux_Apps\/LLM\//, why: "proxy et modèles locaux" },
]

// Outils qui écrivent (natifs, patch, écritures MCP type serena).
const WRITE_TOOL =
  /(apply_patch|multi.?edit|(^|__)edit($|__)|(^|__)write|write_file|create_file|replace_file_content|insert_file_content|delete_file|remove_file|move_file|rename_file)/i

const SHELL_TOOL = /\bbash\b|shell|\bpty\b/i

// Un shell qui écrit : redirection ou commande qui crée/modifie/supprime.
const BASH_WRITE =
  /(?<![=&<>-])>{1,2}\s|&>\s|\b(tee|touch|mkdir|rm|mv|cp|truncate|chmod|chown)\b|\bsed\s+(-[a-zA-Z]*i)/i

function audit(entry) {
  try {
    mkdirSync(dirname(AUDIT), { recursive: true })
    appendFileSync(AUDIT, JSON.stringify({ ...entry, at: new Date().toISOString() }) + "\n")
  } catch {
    // l'audit ne doit jamais casser l'agent
  }
}

function flatten(value, depth = 0) {
  if (value == null || depth > 3) return ""
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  if (Array.isArray(value)) return value.map((v) => flatten(v, depth + 1)).join("\n")
  if (typeof value === "object") return Object.values(value).map((v) => flatten(v, depth + 1)).join("\n")
  return ""
}

// N'aplatit que les clés qui portent une action (ou un chemin), pas le texte.
function pick(value, keys, depth = 0) {
  if (value == null || typeof value !== "object" || depth > 4) return ""
  const out = []
  for (const [k, v] of Object.entries(value)) {
    if (keys.includes(k)) out.push(flatten(v))
    else if (v && typeof v === "object") out.push(pick(v, keys, depth + 1))
  }
  return out.filter(Boolean).join("\n")
}

function match(rules, text) {
  if (!text) return []
  const hits = []
  for (const rule of rules) {
    const hit = rule.test ? rule.test(text) : rule.re.test(text)
    if (hit) hits.push(rule)
  }
  return hits
}

// Exporté pour être testé sans serveur (node /tmp/opencode/guard-cases.mjs).
export function check(input) {
  const agent = input && input.agent ? String(input.agent) : ""
  if (!GUARDED.has(agent)) return { blocked: [], tool: "", text: "" }

  const tool = String((input && input.tool) || "")
  const args = (input && input.input) || {}
  const cmdText = pick(args, CMD_KEYS)
  const urlText = pick(args, URL_KEYS)
  const pathText = pick(args, PATH_KEYS)
  const actionText = [cmdText, urlText].filter(Boolean).join("\n")

  const blocked = []
  for (const rule of DENY_TOOLS) if (rule.re.test(tool)) blocked.push(rule.why)
  for (const rule of DENY_SECRET) {
    if (match([rule], actionText).length || match([rule], pathText).length) blocked.push("secret : " + rule.why)
  }
  for (const rule of match(DENY_NETWORK, actionText)) blocked.push(rule.why)

  const bashWriting = SHELL_TOOL.test(tool) && BASH_WRITE.test(cmdText)
  if (WRITE_TOOL.test(tool) || bashWriting) {
    const where = bashWriting
      ? [pathText, ...writePaths(cmdText)].filter(Boolean).join("\n")
      : pathText
    for (const rule of DENY_WRITE) {
      if (!rule.re.test(where)) continue
      if (rule.plannerOk && agent === "planner") continue
      blocked.push("écriture : " + rule.why)
    }
  }
  return { blocked: [...new Set(blocked)], tool, text: flatten(args).slice(0, 1200) }
}

export default {
  id: "employee-guard",
  async setup(ctx) {
    await ctx.tool.hook("execute.before", async (input) => {
      const agent = input && input.agent ? String(input.agent) : ""
      if (!GUARDED.has(agent)) return

      const { blocked, tool, text } = check(input)
      audit({
        agent,
        tool,
        sessionID: input && input.sessionID,
        blocked: blocked.length ? blocked : null,
        input: text,
      })

      if (blocked.length) {
        throw new Error(
          "employee-guard : action refusée (" +
            blocked.join(" · ") +
            "). Hors du périmètre de l'employé. Produit un [BROUILLON — NON ENVOYÉ] " +
            "et note la proposition dans le journal ou dans une question à Marc.",
        )
      }
    })
  },
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/graphify.ts
---
// graphify — OpenCode v2 plugin
//
// Injects a knowledge-graph reminder before shell commands when a graph exists.
// Ported from the v1 plugin (.opencode/plugins/graphify.js) to the v2 plugin API.
//
// IMPORTANT: keep the reminder string free of backticks and $(...) constructs.
// The reminder is prepended as `echo '<reminder>' && <cmd>`; backticks inside
// the single-quoted echo would trigger command substitution in some shells.
import { existsSync } from "node:fs"
import { join } from "node:path"

export default {
  id: "graphify",
  async setup(ctx) {
    let reminded = false

    await ctx.tool.hook("execute.before", (call) => {
      if (reminded) return
      if (call.tool !== "shell" && call.tool !== "bash") return

      const directory = ctx.location.directory
      const graphs = []
      if (existsSync(join(directory, "graphify-out", "graph.json"))) {
        graphs.push("graphify-out/ (ZeroClaw)")
      }
      if (existsSync(join(directory, "graphify-out-llamacpp", "graph.json"))) {
        graphs.push("graphify-out-llamacpp/ (llama.cpp)")
      }
      if (graphs.length === 0) return

      const args = call.input
      if (!args || typeof args !== "object") return
      const command = args.command
      if (typeof command !== "string" || command.length === 0) return

      const reminder =
        "[graphify] knowledge graphs available: " +
        graphs.join(", ") +
        ". For focused questions, run graphify query --graph <path>/graph.json. Read GRAPH_REPORT.md for broad architecture context."

      args.command = "echo '" + reminder + "' && " + command
      reminded = true
    })
  },
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/gtd-sync.ts
---
// gtd-sync — OpenCode v2 plugin (mode 100 % MANUEL)
//
// Ported from the v1 plugin (.opencode/plugins/gtd-sync.js) to the v2 plugin API.
// Flux : /task (nom + steps validés) -> gtd_set_objective -> 1 carte.
// /label remplit l'unique case "label actuel", valable partout.
// Fin de chantier : l'agent appelle gtd_complete (MCP), sinon 1 clic 📦.
// Outils exposés : gtd_set_label, gtd_get_label, gtd_set_objective.
// Ne throw JAMAIS (erreurs renvoyées en texte).
import { existsSync, readFileSync, writeFileSync, unlinkSync } from "node:fs"
import { join } from "node:path"

const KANBAN_URL = process.env.GTD_KANBAN_URL || "http://127.0.0.1:8787"
const DEFAULT_LABEL = "+Opencode"
const PREFIX = "[opencode]"

const norm = (s) => String(s ?? "").toLowerCase().replace(/\s+/g, " ").trim()

// Coeur comparable : sans préfixe [opencode], sans labels, sans dates.
// (L'API Kanban renvoie `text` SANS labels.)
function core(s) {
  return norm(s)
    .replace(/^\[opencode\]\s*/, "")
    .replace(/^\d{4}-\d{2}-\d{2}\s*/, "")
    .replace(/(?<!\S)[+@]\S+/g, "")
    .replace(/\s+/g, " ")
    .trim()
}

function extractLabels(text) {
  const m = String(text ?? "").match(/(?<!\S)[+@]\S+/g)
  return m ? [...new Set(m)] : []
}

function tokens(s) {
  return new Set(core(s).split(" ").filter((w) => w.length > 2))
}
function containment(a, b) {
  const ta = tokens(a),
    tb = tokens(b)
  if (!ta.size || !tb.size) return 0
  let inter = 0
  for (const w of ta) if (tb.has(w)) inter++
  return inter / Math.min(ta.size, tb.size)
}
const DEDUP_MIN = 0.75

async function api(path, method = "GET", body = undefined) {
  try {
    const r = await fetch(KANBAN_URL + path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(5000),
    })
    return await r.json().catch(() => ({}))
  } catch {
    return null // Kanban éteint : on le dit dans le retour outil, sans throw
  }
}

function findCard(board, needle, onlyColumn = null) {
  const n = core(needle)
  if (!n) return []
  const hits = []
  for (const col of board?.columns ?? []) {
    if (onlyColumn && col.id !== onlyColumn) continue
    for (const t of col.tasks ?? []) {
      const txt = core(t.text ?? "")
      if (txt && (txt.includes(n) || n.includes(txt))) hits.push({ col: col.id, task: t })
    }
  }
  return hits
}

function labelTokenRe(lb) {
  const e = lb.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
  return new RegExp("(?<!\\S)" + e + "(?!\\S)")
}

function normalizeLabel(s) {
  let b = String(s ?? "").trim().replace(/\s+/g, "-")
  b = b.replace(/^[@+]+/, "")
  if (!b) return null
  const marker = String(s ?? "").trim().startsWith("@") ? "@" : "+"
  return marker + b
}

const textResult = (text) => ({ content: text })

export default {
  id: "gtd-sync",
  async setup(ctx) {
    const cwd = ctx.location.directory ?? ""

    // --- unique case "label actuel" (/label), valable partout ---
    const CURRENT_FILE = join(cwd, ".opencode", "gtd-current.json")
    function readCurrent() {
      try {
        if (existsSync(CURRENT_FILE)) {
          const data = JSON.parse(readFileSync(CURRENT_FILE, "utf-8"))
          if (data && typeof data.label === "string" && data.label) return data
        }
      } catch {
        /* ignore */
      }
      return {}
    }
    function writeCurrent(data) {
      try {
        writeFileSync(CURRENT_FILE, JSON.stringify(data, null, 1))
      } catch {
        /* best-effort */
      }
    }
    // Purge les anciens états du mode auto (sessions, chantiers, objectifs).
    for (const f of ["gtd-session-labels.json", "gtd-sync-units.json", "gtd-sync-objectives.json"]) {
      try {
        const p = join(cwd, ".opencode", f)
        if (existsSync(p)) unlinkSync(p)
      } catch {
        /* ignore */
      }
    }

    function cardTextFor(objective) {
      const base = objective.startsWith(PREFIX) ? objective : `${PREFIX} ${objective}`
      const explicit = extractLabels(objective)
      const labels = explicit.length ? explicit : [readCurrent().label || DEFAULT_LABEL]
      let text = base
      for (const lb of labels) {
        if (!labelTokenRe(lb).test(text)) text += " " + lb
      }
      return text
    }

    await ctx.tool.transform((editor) => {
      editor.add({
        name: "gtd_set_label",
        description:
          "Définit le label GTD actuel (commande /label). " +
          "Une seule case globale : la prochaine carte créée via /task " +
          "partira avec ce label. Écrase le choix précédent.",
        input: {
          type: "object",
          properties: {
            label: {
              type: "string",
              description: "Label GTD : +Projet ou @contexte (+ ajouté si absent, espaces -> -)",
            },
          },
          required: ["label"],
          additionalProperties: false,
        },
        async execute(args) {
          const label = normalizeLabel(args?.label)
          if (!label) return textResult("Label vide : rien n'a été changé.")
          writeCurrent({ label, at: Date.now() })
          return textResult(`Label actuel : ${label} — la prochaine carte /task partira avec.`)
        },
      })

      editor.add({
        name: "gtd_get_label",
        description: "Lit le label GTD actuel (défini via /label).",
        input: { type: "object", properties: {}, additionalProperties: false },
        async execute() {
          const cur = readCurrent().label
          return textResult(
            cur
              ? `Label actuel : ${cur}`
              : "Aucun label actuel : les cartes /task partiront avec +Opencode (utilise /label pour choisir).",
          )
        },
      })

      editor.add({
        name: "gtd_set_objective",
        description:
          "Crée L'UNIQUE carte GTD du chantier (commande /task, après validation " +
          "du nom + steps par l'utilisateur). Ne rien créer sans validation. " +
          "Idempotent : si la carte existe déjà (reformulée ou non), rien n'est créé.",
        input: {
          type: "object",
          properties: {
            objective: {
              type: "string",
              description:
                "Nom du chantier VALIDÉ par l'utilisateur, en langage clair compréhensible sans contexte (ex: 'Refonte du pipeline de leads Axel pour contacter 50 prospects/jour')",
            },
          },
          required: ["objective"],
          additionalProperties: false,
        },
        async execute(args) {
          const objective = String(args?.objective ?? "").trim()
          if (!objective) return textResult("Objectif vide : rien n'a été changé.")

          const board = await api("/api/board")
          if (!board || board.error) {
            return textResult(
              `Kanban injoignable (${KANBAN_URL}) : lance ./start-kanban.sh dans GoForIt-GTD/kanban puis réessaie /task. Rien n'a été créé.`,
            )
          }

          // Dédup : exact puis tolérant (reformulation), sur tout le board.
          if (findCard(board, objective).length) {
            return textResult(`Carte déjà présente (« ${objective.slice(0, 60)} ») : rien créé (dédup).`)
          }
          const n = core(objective)
          if (n) {
            let cands = 0
            for (const col of board.columns ?? []) {
              for (const t of col.tasks ?? []) {
                if (containment(objective, t.text ?? "") >= DEDUP_MIN) cands++
              }
            }
            if (cands === 1) {
              return textResult("Carte équivalente déjà présente : rien créé (dédup tolérante).")
            }
            if (cands > 1) {
              return textResult(
                "Plusieurs cartes proches existent : précise le nom via /task pour éviter un doublon. Rien créé.",
              )
            }
          }

          const created = await api("/api/add", "POST", { text: cardTextFor(objective) })
          if (!created || created.error) {
            return textResult(
              `Échec création carte : ${created?.error || "Kanban injoignable"}. Réessaie /task.`,
            )
          }
          return textResult(
            `Carte GTD créée en Inbox : ${cardTextFor(objective).slice(0, 100)} — fais tes todos normalement, puis gtd_complete en fin de chantier.`,
          )
        },
      })
    })
  },
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/loop-guard.ts
---
// loop-guard — OpenCode v2 plugin
//
// Bloque les boucles d'outils via 3 déclencheurs indépendants :
// A) mêmes arguments exacts répétés
// C) même outil qui échoue consécutivement
// D) budget d'appels sans idle
//
// CE FICHIER EST RECHARGÉ À CHAUD PAR LE SERVEUR : l'écrire toujours d'un seul
// coup. Une version incohérente lève une erreur sur TOUS les tool calls de
// TOUTES les sessions.

import { appendFileSync, mkdirSync } from "node:fs"
import { dirname } from "node:path"

const PROJECT = process.env.ORCH_PROJECT || "/media/marcs/Linux_Apps/Projets_AI"
const AUDIT_PATH = PROJECT + "/.opencode/loop-audit.jsonl"
const HISTORY = new Map()

const IGNORE_TOOLS = /^(todo|todowrite|question|skill)$/i

export function signature(tool, args) {
  const cleanArgs = {}
  if (args && typeof args === "object") {
    function sortKeys(obj) {
      if (obj === null || typeof obj !== "object") return obj
      if (Array.isArray(obj)) return obj.map(sortKeys)
      const sorted = {}
      Object.keys(obj).sort().forEach((k) => {
        if (k === "timeout" || k === "description") return
        sorted[k] = sortKeys(obj[k])
      })
      return sorted
    }
    Object.assign(cleanArgs, sortKeys(args))
  }
  return String(tool || "") + ":" + JSON.stringify(cleanArgs)
}

export function detectLoop(state, sig, opts) {
  if (state.sig === sig) {
    state.count++
  } else {
    state.sig = sig
    state.count = 1
  }
  const threshold = Number(opts && opts.threshold) || Number(process.env.LOOP_GUARD_THRESHOLD) || 3
  const blocked = state.count >= threshold
  return { blocked, count: state.count }
}

export function createState() {
  return {
    sig: null,
    count: 0,
    failTool: null,
    failCount: 0,
    calls: 0,
    locked: false,
    reason: null,
    lastAt: 0,
  }
}

export function onToolCall(state, tool, sig, opts) {
  const now = opts.now ?? Date.now()
  // idleMs 10 min (600000) : arbitrage assumé — filet pour le cas où aucun événement
  // n'est jamais émis ; à 5 min il rendait les boucles lentes invisibles aux trois déclencheurs.
  const idleMs = opts.idleMs ?? 600000
  const budget = opts.budget ?? (Number(process.env.LOOP_GUARD_BUDGET) || 80)

  if (now - state.lastAt > idleMs) {
    onIdle(state)
  }
  state.lastAt = now

  if (state.locked) {
    return { blocked: true, reason: state.reason, count: state.count, calls: state.calls }
  }

  state.calls++

  if (state.sig === sig) {
    state.count++
  } else {
    state.sig = sig
    state.count = 1
  }

  const threshold = opts.threshold ?? (Number(process.env.LOOP_GUARD_THRESHOLD) || 3)

  if (state.count >= threshold) {
    state.locked = true
    state.reason = "A"
    return { blocked: true, reason: "A", count: state.count, calls: state.calls }
  }

  if (state.calls >= budget) {
    state.locked = true
    state.reason = "D"
    return { blocked: true, reason: "D", count: state.count, calls: state.calls }
  }

  return { blocked: false, reason: null, count: state.count, calls: state.calls }
}

// Remise à zéro C sur TOUT succès : arbitrage assumé — la rendre « par outil »
// ferait verrouiller une session qui grepe légitimement dans trois fichiers absents.
export function onToolResult(state, tool, ok) {
  if (ok) {
    state.failTool = null
    state.failCount = 0
  } else {
    if (state.failTool === tool) {
      state.failCount++
    } else {
      state.failTool = tool
      state.failCount = 1
    }
    if (state.failCount >= 3) {
      state.locked = true
      state.reason = "C"
      return { blocked: true, reason: "C", justLocked: true }
    }
  }
  return { blocked: false, reason: null, justLocked: false }
}

export function onIdle(state) {
  state.sig = null
  state.count = 0
  state.failTool = null
  state.failCount = 0
  state.calls = 0
  state.locked = false
  state.reason = null
  state.lastAt = 0
}

export function toolFailed(input) {
  if (!input || typeof input !== "object") return false

  // Contrat « promise » (promise/tool.d.ts) : { status: "completed" | "error", result | error }
  const status = typeof input.status === "string" ? input.status : ""
  if (status === "error") return true
  if (status === "completed") return false

  // Contrat « legacy » (index.d.ts) : les infos sont dans `output`
  const out = input.output
  const title =
    typeof out === "string" ? out
    : out && typeof out === "object" && typeof out.title === "string" ? out.title
    : ""
  if (!title) return false // aucune information d'échec : on ne devine pas

  // Ancré au DÉBUT : un titre « Error: ... » est un échec ; un résultat qui CONTIENT
  // le mot « error » (ex. un grep sur des logs) n'en est pas un.
  return /^\s*(error|erreur|failed|failure|exception|refus|interrompu|bloqu)/i.test(title)
}

function audit(entry) {
  try {
    mkdirSync(dirname(AUDIT_PATH), { recursive: true })
    appendFileSync(AUDIT_PATH, JSON.stringify(entry) + "\n")
  } catch (err) {
    // Ignore audit errors so they never break execution
  }
}

// Budget D : arbitrage assumé — 80 verrouille 100 appels légitimes en rafale,
// mais la boucle du 23/10 a fait ~90 appels en 15 min 30 : relever aurait laissé
// passer l'incident réel. Un faux verrou est explicite et réversible, une boucle
// manquée est silencieuse et coûteuse.

export default {
  id: "loop-guard",
  async setup(ctx) {
    let controller = new AbortController()

    const runEvents = async () => {
      try {
        for await (const ev of ctx.event.subscribe({ signal: controller.signal })) {
          const sessionID = String((ev && ev.data && ev.data.sessionID) || "")
          if (!sessionID) continue
          const state = HISTORY.get(sessionID)
          if (!state) continue
          const type = String(ev.type || "")
          if (
            type === "session.idle" ||
            type === "session.execution.succeeded" ||
            type === "session.execution.failed"
          ) {
            onIdle(state)
          } else if (type === "session.deleted") {
            HISTORY.delete(sessionID)
          }
        }
      } catch (err) {
        // Event loop error must not crash the plugin
      }
    }
    runEvents()

    await ctx.tool.hook("execute.before", async (input) => {
      const tool = String((input && input.tool) || "")
      if (IGNORE_TOOLS.test(tool)) return

      const sessionID = String((input && input.sessionID) || "default")
      const agent = String((input && input.agent) || "")
      const args = (input && input.input) || (input && input.args) || {}

      const sig = signature(tool, args)

      let state = HISTORY.get(sessionID)
      if (!state) {
        state = createState()
        HISTORY.set(sessionID, state)
      }

      const threshold = Number(process.env.LOOP_GUARD_THRESHOLD) || 3
      const budget = Number(process.env.LOOP_GUARD_BUDGET) || 80
      const idleMs = 600000

      const result = onToolCall(state, tool, sig, { threshold, budget, idleMs, now: Date.now() })

      if (result.blocked) {
        const reason = result.reason
        let detail = ""
        if (reason === "A") detail = ` même signature ${result.count} fois`
        else if (reason === "C") detail = ` même outil ${state.failTool} en échec ${state.failCount} fois`
        else if (reason === "D") detail = ` budget d'appels ${result.calls} atteint`
        // Ne pas journaliser le verrou C ici : execute.after le fait déjà avec
        // les bons champs (failTool/failCount) au bon moment (justLocked).
        if (reason !== "C") {
          audit({
            at: new Date().toISOString(),
            sessionID,
            agent,
            tool,
            reason,
            count: result.count,
            calls: result.calls,
            input: JSON.stringify(args).slice(0, 800),
          })
        }
        throw new Error(
          "loop-guard : coupe-circuit déclenché (" + reason + ")." + detail + ". ARRÊTE d'appeler des outils : rends la main à Marc maintenant, en expliquant où tu en es et ce qui bloque."
        )
      }
    })

    await ctx.tool.hook("execute.after", async (input) => {
      const tool = String((input && input.tool) || "")
      if (IGNORE_TOOLS.test(tool)) return

      const sessionID = String((input && input.sessionID) || "default")
      const agent = String((input && input.agent) || "")
      const args = (input && input.input) || (input && input.args) || {}

      const state = HISTORY.get(sessionID)
      if (!state) return

      const ok = !toolFailed(input)
      const result = onToolResult(state, tool, ok)

      if (result.justLocked) {
        audit({
          at: new Date().toISOString(),
          sessionID,
          agent,
          tool,
          reason: "C",
          count: state.failCount,
          calls: state.calls,
          input: JSON.stringify(args).slice(0, 800),
        })
      }
    })

    return () => controller.abort()
  },
}

---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/notify-desktop.ts
---
// notify-desktop — OpenCode v2 plugin (Linux)
//
// Ported from the v1 plugin (.opencode/plugins/notify-desktop.js) to the v2 plugin API.
// Envoie une notification de bureau via notify-send quand opencode a fini de bosser.
// Couvre : fin de tâche (exécution réussie / idle), permission requise, formulaire
// en attente (question), erreur d'exécution.
// Anti-spam : sessions principales uniquement, cooldown 30s, collapse 3s.
import { execFile } from "node:child_process"

const COOLDOWN_MS = 30000
const COLLAPSE_MS = 3000

function run(file, args) {
  try {
    execFile(file, args, () => {})
  } catch {
    /* best-effort : jamais bloquant */
  }
}

export default {
  id: "notify-desktop",
  async setup(ctx) {
    const controller = new AbortController()
    const mainSessions = new Set()
    const lastNotify = new Map() // key -> timestamp ms
    const pendingCollapse = new Map() // key -> timeout id

    function notify({ title, body, urgency = "normal", icon = "dialog-information", sound = "complete.oga" }) {
      run("notify-send", ["-u", urgency, "-a", "OpenCode", "-i", icon, title, body])
      run("paplay", [`/usr/share/sounds/freedesktop/stereo/${sound}`])
    }

    function shouldSend(key) {
      const now = Date.now()
      const last = lastNotify.get(key) ?? 0
      if (now - last < COOLDOWN_MS) return false
      lastNotify.set(key, now)
      return true
    }

    function collapsed(key, fn) {
      // Collapse les rafales : seule la dernière notif de la fenêtre part.
      if (pendingCollapse.has(key)) clearTimeout(pendingCollapse.get(key))
      pendingCollapse.set(
        key,
        setTimeout(() => {
          pendingCollapse.delete(key)
          fn()
        }, COLLAPSE_MS),
      )
    }

    function done(sessionID) {
      const key = `idle:${sessionID}`
      collapsed(key, () => {
        if (!shouldSend(key)) return
        notify({
          title: "OpenCode — Tâche terminée",
          body: "L'agent a fini de bosser. À toi de relire.",
          urgency: "normal",
          icon: "dialog-information",
          sound: "complete.oga",
        })
      })
    }

    ;(async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          try {
            const data = event?.data ?? {}

            if (event.type === "session.created") {
              if (data.sessionID && !data.parentID) mainSessions.add(data.sessionID)
              continue
            }
            if (event.type === "session.deleted") {
              if (data.sessionID) mainSessions.delete(data.sessionID)
              continue
            }

            // --- fin de tâche ---
            if (event.type === "session.idle") {
              if (mainSessions.size && !mainSessions.has(data.sessionID)) continue
              done(data.sessionID)
              continue
            }
            if (event.type === "session.execution.succeeded") {
              if (mainSessions.size && !mainSessions.has(data.sessionID)) continue
              done(data.sessionID)
              continue
            }

            // --- permission requise (bloqué) ---
            if (event.type === "permission.asked") {
              const key = "permission"
              if (!shouldSend(key)) continue
              notify({
                title: "OpenCode — Permission requise",
                body: "L'agent est bloqué et attend ton autorisation.",
                urgency: "critical",
                icon: "dialog-warning",
                sound: "dialog-warning.oga",
              })
              continue
            }

            // --- formulaire / question en attente d'input ---
            if (event.type === "form.created") {
              const key = "form"
              if (!shouldSend(key)) continue
              notify({
                title: "OpenCode — Question en attente",
                body: data.form?.title || "L'agent te pose une question.",
                urgency: "normal",
                icon: "dialog-information",
                sound: "dialog-information.oga",
              })
              continue
            }

            // --- erreur d'exécution ---
            if (event.type === "session.execution.failed") {
              const key = "error"
              if (!shouldSend(key)) continue
              notify({
                title: "OpenCode — Erreur session",
                body: "La session a rencontré une erreur, vérifie le TUI.",
                urgency: "critical",
                icon: "dialog-error",
                sound: "dialog-error.oga",
              })
            }
          } catch {
            /* un événement mal formé ne doit pas tuer la boucle */
          }
        }
      } catch {
        /* aborted on cleanup */
      }
    })()

    return () => {
      controller.abort()
      for (const t of pendingCollapse.values()) clearTimeout(t)
      pendingCollapse.clear()
    }
  },
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/orch-guard.ts
---
// orch-guard — OpenCode plugin (v2 — multi-sessions)
//
// Le frontière planifie, le flash exécute. Ce plugin le fait respecter physiquement.
//
//   orchestrator (frontière) : écrit seulement SON manifest / specs / done.md ; shell en
//                              lecture seule ; ne lance qu'un subagent annoncé, avec un
//                              modèle free ou local, jamais hors manifest.
//   reviewer     (frontière) : ne modifie rien ; shell lecture seule + runners de test.
//   worker       (flash)     : edit + shell, mais dans le périmètre annoncé, sans push /
//                              publication / install / envoi.
//   merger       (flash)     : shell limité à git + push sur branche dédiée (orch/*), jamais main/master ni --force.
//
// Multi-sessions : chaque session orchestrator a SON manifest
//   .opencode/orch/<sessionID>/orchestration.md
// Le guard résout le manifest via input.sessionID. `.opencode/orchestration.md` (global)
// reste supporté en mode mono-session : le premier à l'utiliser en devient propriétaire
// (owners.json) et les autres sessions sont redirigées vers leur dossier.
//
// Toute action refusée est tracée dans .opencode/orch-audit.jsonl.
//
// CE FICHIER EST RECHARGÉ À CHAUD : l'écrire toujours d'un seul coup. Une version
// incohérente lève une erreur sur TOUS les tool calls de TOUTES les sessions.
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs"
import { dirname, resolve, relative, sep } from "node:path"

const PROJECT = process.env.ORCH_PROJECT || "/media/marcs/Linux_Apps/Projets_AI"
const ORCH_DIR = process.env.ORCH_DIR || PROJECT + "/.opencode/orch"
const LEGACY_MANIFEST = process.env.ORCH_MANIFEST || PROJECT + "/.opencode/orchestration.md"
const AUDIT = process.env.ORCH_AUDIT || PROJECT + "/.opencode/orch-audit.jsonl"
const KNOWN = ORCH_DIR + "/known.json"
const PENDING = ORCH_DIR + "/pending.json"
const OWNERS = ORCH_DIR + "/owners.json"
const PENDING_TTL = 2 * 60 * 60 * 1000
const STATE = process.env.ORCH_STATE || PROJECT + "/.opencode/orch-state.json"
const SLOT_TTL = 45 * 60 * 1000

const NIGHT_LOCK = PROJECT + "/.opencode/night/night-mode.json"
const NIGHT_MANIFESTS_DIR = PROJECT + "/.opencode/night/manifests/"
const NIGHT_QUEUE = PROJECT + "/.opencode/night/queue.jsonl"
const NIGHT_STOP = PROJECT + "/.opencode/night/STOP"

const AGENTS = new Set(["orchestrator", "worker", "reviewer", "merger"])
const SPAWNABLE = new Set(["worker", "reviewer", "merger"])

// Le frontière ne s'exécute jamais. Ces règles encodent la séparation :
//   - un worker/merger tourne sur un modèle d'exécution free ou local (rien d'autre),
//   - un reviewer tourne sur un modèle de revue (frontière ou abonnement), mais aussi sur
//     tout modèle free ou local : la séparation est portée par les permissions, pas par le nom.
function execAllowed(model, nightActive) {
  // Mode nuit : restrictif (uniquement llama-local / llama-proxy)
  if (nightActive) return execAllowedNight(model)
  const m = String(model || "")
  if (/free/i.test(m)) return true
  if (/^(llama-local|llama-proxy)\//i.test(m)) return true
  // nvidia : tout le catalogue NIM est gratuit, aucun id ne porte « -free ».
  if (/^nvidia\//i.test(m)) return true
  if (/^cline-pass\//i.test(m)) return true
  // openrouter : seuls les modèles explicitement gratuits (:free / -free) passent.
  if (/^openrouter\//i.test(m)) return /:free|-free/i.test(m)
  return false
}

// Mode nuit : execAllowed devient restrictif (uniquement llama-local / llama-proxy)
function execAllowedNight(model) {
  const m = String(model || "")
  return /^(llama-local|llama-proxy)\//i.test(m)
}

const REVIEW_MODELS = [
  "opencode/glm-5.3-flash-free",
  "opencode/claude-sonnet-5",
  "opencode/gpt-5.6-sol",
  "cline-pass/*",
]
function modelAllowed(model, pool) {
  return pool.some((m) => model === m || (m.endsWith("*") && model.startsWith(m.slice(0, -1))))
}

// Autorisation explicite de Marc, matérialisée dans le manifest (en-tête du chantier ou
// dans l'étape) : SEUL chemin vers un modèle payant pour un worker/merger. Le guard ne lit
// pas le chat — sans cette déclaration, tout modèle hors pool free/local est refusé.
function paidDeclared(txt, step, model) {
  if (!model) return false
  const norm = (s) => String(s).trim().replace(/^`|`$/g, "")
  const ids = []
  const header = String(txt || "").match(/^-\s*Modèle payant\s*:\s*(.+)$/im)
  if (header) ids.push(...header[1].split(/,|\s+/))
  if (step && step.paid) ids.push(...step.paid.split(/,|\s+/))
  return ids.map(norm).filter(Boolean).includes(model)
}

const SHELL_TOOL = /^(shell|bash|pty)$/i
const SPAWN_TOOL = /^(subagent|task)$/i
const WRITE_TOOL =
  /(apply_patch|multi.?edit|^edit$|^write$|write_file|create_file|replace_file_content|insert_file_content|delete_file|remove_file|move_file|rename_file)/i

const PATH_KEYS = ["filePath", "path", "file", "target", "destination", "notebookPath", "dir", "file_path"]

// Commandes de lecture (orchestrateur / reviewer).
const READONLY_CMD =
  /^(ls|cat|head|tail|wc|pwd|tree|stat|file|du|df|grep|rg|find|diff|jq|awk|cut|sort|uniq|sed|echo|which|realpath|basename|dirname|date|env|git)$/

// Runners de test (reviewer uniquement) : ils doivent relancer les tests eux-mêmes.
const TEST_RUNNER =
  /^(pytest|py\.test|tox|nox|node|bun|deno|npm|pnpm|yarn|cargo|go|make|php|dotnet|rspec|jest|vitest|mvn|gradle|ruff|eslint|tsc|mypy|shellcheck|bats|python3?|uv|poetry)$/

// Tout ce qui écrit ou sort : détecté sur la commande, segment par segment.
function mutating(cmd) {
  const c = String(cmd || "")
  if (!c) return ""
  if (/(?<![=&<>-])>{1,2}\s*(?!&)(?!\/dev\/null)/.test(c)) return "redirection d'écriture"
  if (/\b(tee|touch|mkdir|rmdir|rm|mv|cp|truncate|chmod|chown|ln|dd|install|rsync|patch)\b/.test(c)) return "écriture fichiers"
  if (/\bsed\s+-[a-zA-Z]*i/.test(c)) return "sed -i"
  if (/\bgit\s+(add|commit|merge|push|pull|reset|rebase|checkout|switch|cherry-pick|apply|stash|tag|worktree|clean|init)\b/.test(c)) return "git mutant"
  if (/\b(npm|pnpm|yarn|bun)\s+(i|install|add|remove|publish|link|exec)\b/.test(c)) return "gestionnaire de paquets"
  if (/\bpip3?\s+install\b|\buv\s+(add|sync)\b/.test(c)) return "installation"
  if (/\bsudo\b|\bsystemctl\b|\bkill\b|\bpkill\b/.test(c)) return "action système"
  return ""
}

// Réseau en écriture (jamais depuis un worker).
const NET_WRITE = [
  { re: /\bgit\s+push\b/, why: "git push" },
  { re: /\bnpm\s+publish\b|\bpip\s+upload\b|\btwine\s+(publish|upload)\b/, why: "publication de paquet" },
  { re: /\/2\/tweets\b|\/statuses\/update|\/friendships\/create|\/favorite\/create|\/direct_messages\/events/, why: "X en écriture" },
  { re: /reddit\.com\/api\/(submit|comment|vote|edit_user)|\/api\/user\/(friend|subscribe)/i, why: "Reddit en écriture" },
  { re: /api\.telegram\.org\/bot[^/\s]+\/send/i, why: "Telegram en écriture" },
  { re: /\b(sendmail|msmtp|mutt\s+-s|mail\s+-s)\b/, why: "envoi d'email" },
  {
    test: (t) =>
      /\b(curl|wget)\b/.test(t) &&
      (/\s(-X|--request)\s+(POST|PUT|PATCH|DELETE)\b/i.test(t) || /(^|\s)(-{1,2}data(-binary|-ascii)?|-F|-T|--upload-file)(\s|$)/.test(t)) &&
      !/(127\.0\.0\.1|localhost|::1)/.test(t),
    why: "requête HTTP en écriture",
  },
]

function firstPath(args) {
  const out = []
  const walk = (v, d) => {
    if (v == null || d > 4) return
    if (typeof v === "string") return
    if (Array.isArray(v)) return v.forEach((x) => walk(x, d + 1))
    if (typeof v === "object") {
      for (const [k, val] of Object.entries(v)) {
        if (PATH_KEYS.includes(k) && typeof val === "string") out.push(val)
        else walk(val, d + 1)
      }
    }
  }
  walk(args, 0)
  return out[0] || ""
}

function cmdOf(args) {
  const a = args || {}
  for (const k of ["command", "cmd", "script", "code"]) {
    if (typeof a[k] === "string") return a[k]
  }
  return ""
}

function segments(cmd) {
  return String(cmd || "")
    .split(/\n|&&|\|\||;|\|/)
    .map((s) => s.trim().replace(/^\(+|\)+$/g, "").trim())
    .filter((s) => s && !/^#/.test(s))
}

function firstWord(seg) {
  const clean = seg.replace(/^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+/, "")
  const m = clean.match(/^([^\s]+)/)
  return m ? m[1] : ""
}

// ---------- fichiers / sessions ----------

function readText(p) {
  try {
    return readFileSync(p, "utf8")
  } catch {
    return ""
  }
}
function readJson(p, fallback) {
  try {
    return JSON.parse(readFileSync(p, "utf8"))
  } catch {
    return fallback
  }
}
function writeJson(p, data) {
  try {
    mkdirSync(dirname(p), { recursive: true })
    writeFileSync(p, JSON.stringify(data, null, 2))
  } catch {
    // l'état ne doit jamais casser l'agent : au pire, on retombe en mode sans binding
  }
}
function fileExists(p) {
  try {
    return existsSync(p)
  } catch {
    return false
  }
}

function sessionId(input) {
  const s = String((input && input.sessionID) || "")
  return /^[A-Za-z0-9_-]{3,90}$/.test(s) ? s : ""
}

function sessionManifest(sid) {
  return ORCH_DIR + "/" + sid + "/orchestration.md"
}

function absPath(p) {
  const s = String(p || "").trim().replace(/^\.\//, "").replace(/^`|`$/g, "")
  if (!s) return ""
  return s.startsWith("/") ? s : PROJECT + "/" + s
}

// Le manifest de cette session : le sien s'il existe, sinon le global (legacy) — mais
// seulement si ce global est libre ou lui appartient. Sinon : aucun manifest (chaque
// orchestration doit écrire le sien, on ne spawne pas sur le plan d'une autre).
function manifestFor(sid) {
  if (sid && fileExists(sessionManifest(sid))) return sessionManifest(sid)
  const owner = readJson(OWNERS, {})[LEGACY_MANIFEST]
  if (owner && owner !== sid) return ""
  return LEGACY_MANIFEST
}

// Le premier qui écrit le global en devient propriétaire : les autres sessions sont
// redirigées vers leur propre dossier, mais une session déjà propriétaire garde l'accès
// (aucun chantier en cours ne peut être verrouillé par un second cycle).
function claim(path, sid) {
  const owners = readJson(OWNERS, {})
  const owner = owners[path]
  if (!owner) {
    if (sid) {
      owners[path] = sid
      writeJson(OWNERS, owners)
    }
    return true
  }
  return owner === sid
}

// Normalise un chemin absolu : résout .., ., doubles slashes, et vérifie qu'il reste sous PROJECT
function normalizeUnderProject(p) {
  try {
    const abs = resolve(p)
    const rel = relative(PROJECT, abs)
    if (rel.startsWith("..") || rel === "") return null // sort du projet ou racine
    return abs
  } catch {
    return null
  }
}

// Vérifie si un chemin normalisé est sous un préfixe donné (les deux déjà absolus et normalisés)
function isUnder(normalizedPath, prefix) {
  if (!normalizedPath || !prefix) return false
  const n = normalizedPath.replace(/\\/g, "/")
  const p = prefix.replace(/\\/g, "/")
  return n === p || n.startsWith(p + "/")
}

function orchestratorWrite(p, sid) {
  const a = absPath(p)
  if (!a) return { ok: false, why: "écriture sans chemin identifiable (le frontière ne produit que des plans)" }
  if (sid && (a === ORCH_DIR + "/" + sid || a.startsWith(ORCH_DIR + "/" + sid + "/"))) return { ok: true }

  // Normalisation du chemin pour résister à .., ./, doubles slashes, casse
  const norm = normalizeUnderProject(a)
  if (!norm) return { ok: false, why: "chemin invalide ou hors projet (" + p + ")" }

  // B. Permissions .opencode/night/ — liste blanche STRICTE
  // Autorisés UNIQUEMENT :
  //   - .opencode/night/manifests/** (récursif)
  //   - .opencode/night/queue.jsonl
  //   - .opencode/night/STOP
  // Tout le reste sous .opencode/night/ est REFUSÉ (night-mode.json, run.sh, run.log, etc.)
  const nightPrefix = PROJECT + "/.opencode/night"
  if (isUnder(norm, nightPrefix)) {
    const manifestsPrefix = PROJECT + "/.opencode/night/manifests"
    if (isUnder(norm, manifestsPrefix)) return { ok: true }
    if (norm === NIGHT_QUEUE) return { ok: true }
    if (norm === NIGHT_STOP) return { ok: true }
    return { ok: false, why: "écriture interdite sous .opencode/night/ (liste blanche : manifests/**, queue.jsonl, STOP uniquement) : " + p }
  }

  if (a === LEGACY_MANIFEST) {
    if (claim(LEGACY_MANIFEST, sid)) return { ok: true }
    return {
      ok: false,
      why: "manifest global déjà utilisé par une autre orchestration — écris le tien dans " + sessionManifest(sid),
    }
  }
  if (a.startsWith(ORCH_DIR + "/")) {
    return { ok: false, why: "dossier d'orchestration d'une autre session — le tien : " + sessionManifest(sid) }
  }
  if (a.startsWith(PROJECT + "/.opencode/orch-archive/")) return { ok: true }
  if (a === PROJECT + "/.opencode/orch-state.json") return { ok: true }
  if (/(^|\/)specs\//.test(a)) return { ok: true }
  if (/(^|\/)done\.md$/.test(a)) return { ok: true }
  return { ok: false, why: "le frontière n'écrit pas de code (" + p + ") — délègue à un worker flash" }
}

// ---------- manifest ----------

function parseSteps(txt) {
  const steps = []
  for (const block of String(txt).split(/^## /m).slice(1)) {
    const get = (re) => {
      const m = block.match(re)
      return m ? m[1].trim() : ""
    }
    const step = {
      title: block.split("\n")[0].trim(),
      id: get(/^-\s*id\s*:\s*(.+)$/im),
      qui: get(/^-\s*Qui\s*:\s*(.+)$/im).toLowerCase(),
      scope: get(/^-\s*P[ée]rim[èe]tre\s*:\s*(.+)$/im),
      paid: get(/^-\s*Modèle payant\s*:\s*(.+)$/im),
      statut: get(/^-\s*Statut\s*:\s*(.+)$/im).toLowerCase(),
    }
    if (step.id) steps.push(step)
  }
  return steps
}

function scopeList(step) {
  return String(step && step.scope ? step.scope : "")
    .split(/,|·|\n/)
    .map((s) => s.trim().replace(/^`|`$/g, "").replace(/^\.\./, "").replace(/^\.\//, ""))
    .filter(Boolean)
}

function inScope(path, scopes) {
  const p = String(path || "").replace(/^\.\./, "").replace(/^\.\//, "")
  return scopes.some((s) => {
    const sc = s.replace(/\/$/, "")
    return p === sc || p.startsWith(sc + "/") || p.endsWith("/" + sc) || p.endsWith(sc)
  })
}

const OPEN_STATUS = new Set(["annoncée", "annoncee", "lancée", "lancee", "rendue"])

// Une seule étape active pour cet agent => périmètre applicable. Sinon (parallélisme),
// on ne devine pas : le contrôle de périmètre est sauté et tracé.
function activeStep(txt, agent) {
  const active = parseSteps(txt).filter((s) => s.qui === agent && OPEN_STATUS.has(s.statut))
  return active.length === 1 ? active[0] : null
}

function openStepFor(txt, agent) {
  return parseSteps(txt).find((s) => s.qui === agent && OPEN_STATUS.has(s.statut)) || null
}

// ---------- binding subagent → manifest ----------

function bindWorker(sid, writingPath) {
  const known = readJson(KNOWN, {})
  if (known[sid]) return { ...known[sid], ambiguous: false }
  const pending = readJson(PENDING, []).filter((e) => Date.now() - (e.at || 0) < PENDING_TTL)
  const candidates = pending.filter((e) => e.agent === "worker")
  if (candidates.length === 0) return null

  // If writingPath is provided, deterministically match by scope
  if (writingPath) {
    const normPath = writingPath.replace(/^\.\./, "").replace(/^\.\//, "")
    const matching = []
    for (const c of candidates) {
      const txt = readText(c.manifest)
      if (!txt) continue
      const step = activeStep(txt, "worker")
      if (!step) continue
      const scope = scopeList(step)
      if (scope.length && inScope(normPath, scope)) {
        matching.push(c)
      }
    }
    if (matching.length === 1) {
      const entry = matching[0]
      writeJson(KNOWN, { ...known, [sid]: { agent: "worker", manifest: entry.manifest, at: Date.now() } })
      writeJson(PENDING, pending.filter((e) => e !== entry))
      return { agent: "worker", manifest: entry.manifest, ambiguous: false }
    }
    if (matching.length === 0 && candidates.length === 1) {
      // Single pending manifest but no scope match (e.g. multiple open steps, or path outside scope)
      // Bind to it but mark ambiguous so scope check is skipped with "liaison ambiguë" note
      const entry = candidates[0]
      writeJson(KNOWN, { ...known, [sid]: { agent: "worker", manifest: entry.manifest, at: Date.now() } })
      writeJson(PENDING, pending.filter((e) => e !== entry))
      return { agent: "worker", manifest: entry.manifest, ambiguous: true }
    }
    if (matching.length > 1) {
      // Multiple manifests match the path: do NOT guess, do NOT bind
      return null
    }
    // matching.length === 0 && candidates.length > 1: multiple manifests, none match → don't guess
    return null
  }

  // No writingPath provided: fall back to single pending entry
  if (candidates.length === 1) {
    const entry = candidates[0]
    writeJson(KNOWN, { ...known, [sid]: { agent: "worker", manifest: entry.manifest, at: Date.now() } })
    writeJson(PENDING, pending.filter((e) => e !== entry))
    return { agent: "worker", manifest: entry.manifest, ambiguous: false }
  }
  return null
}

function rememberSpawn(agent, manifest) {
  const pending = readJson(PENDING, []).filter((e) => Date.now() - (e.at || 0) < PENDING_TTL)
  pending.push({ agent, manifest, at: Date.now() })
  writeJson(PENDING, pending)
}

// ---------- pool de concurrence (slots modèle + fournisseur) ----------
//
// Un subagent occupe un slot de son modèle ET un slot de son fournisseur. L'état vit dans
// STATE (surchargeable par ORCH_STATE) : {"slots":[{sessionID,parent,agent,model,provider,step,manifest,since}]}.
// Libération minimale : tout slot de plus de 45 min est purgé, en lecture comme en écriture.
// Un état absent / vide / corrompu ne doit JAMAIS faire lever le guard : fallback « aucun slot ».
const POOL = {
  providerOf(model) {
    const m = String(model || "")
    const i = m.indexOf("/")
    return i > 0 ? m.slice(0, i).toLowerCase() : ""
  },
  // 0 = pas de cap par modèle (le fournisseur s'en charge).
  modelCap(model) {
    const m = String(model || "").toLowerCase()
    if (m === "llama-local/qwen-opus") return 2
    if (/^llama-local\//.test(m)) return 1
    if (/^llama-proxy\//.test(m)) return 1
    return 0
  },
  // 0 = pas de cap global pour ce provider (les modèles locaux sont plafonnés par modèle).
  providerCap(provider) {
    const p = String(provider || "").toLowerCase()
    if (p === "llama-local" || p === "llama-proxy") return 0
    if (["opencode", "opencode-cloud", "cline-pass", "nvidia", "openrouter"].includes(p)) return 2
    return 1
  },
  load() {
    let slots = []
    try {
      const data = JSON.parse(readFileSync(STATE, "utf8"))
      if (data && Array.isArray(data.slots)) slots = data.slots
    } catch {
      slots = []
    }
    const now = Date.now()
    const live = []
    for (const s of slots) {
      if (!s || typeof s !== "object") continue
      const since = Number(s.since)
      if (!Number.isFinite(since)) continue
      if (!(now - since < SLOT_TTL)) continue
      live.push({
        sessionID: String(s.sessionID || ""),
        parent: String(s.parent || ""),
        agent: String(s.agent || ""),
        model: String(s.model || ""),
        provider: String(s.provider || ""),
        step: String(s.step || ""),
        manifest: String(s.manifest || ""),
        since,
      })
    }
    if (live.length !== slots.length) POOL.save(live)
    return live
  },
  save(slots) {
    try {
      mkdirSync(dirname(STATE), { recursive: true })
      const tmp = STATE + ".tmp-" + Date.now() + "-" + Math.random().toString(36).slice(2)
      writeFileSync(tmp, JSON.stringify({ slots }, null, 2))
      renameSync(tmp, STATE) // écriture atomique : tmp puis rename
    } catch {
      // l'état ne doit jamais casser l'agent : au pire, le pool repart vide
    }
  },
  // Refus explicite, nommant la ressource saturée, le cap et le nombre courant.
  check(model) {
    const m = String(model || "")
    if (!m) return { ok: true, why: "" }
    const live = POOL.load()
    const provider = POOL.providerOf(m)
    const mcap = POOL.modelCap(m)
    if (mcap > 0) {
      const n = live.filter((s) => String(s.model || "").toLowerCase() === m.toLowerCase()).length
      if (n >= mcap) {
        const hint = m.toLowerCase() === "llama-local/qwen-opus" ? "" : " — attends la libération ou choisis qwen-opus (2 slots)"
        return { ok: false, why: "pool plein : " + m + " " + n + "/" + mcap + hint }
      }
    }
    const pcap = POOL.providerCap(provider)
    if (pcap > 0) {
      const n = live.filter((s) => String(s.provider || "").toLowerCase() === provider).length
      if (n >= pcap) {
        return {
          ok: false,
          why:
            "pool plein : fournisseur " + (provider || "(inconnu)") + " " + n + "/" + pcap +
            " — attends la libération ou choisis un autre fournisseur",
        }
      }
    }
    return { ok: true, why: "" }
  },
  add(model, ctx) {
    const m = String(model || "")
    if (!m) return
    const live = POOL.load()
    const step = String((ctx && ctx.step) || "")
    const manifest = String((ctx && ctx.manifest) || "")
    if (step) {
      const occupied = live.find((s) =>
        String(s.manifest || "") === manifest &&
        String(s.step || "") === step
      )
      if (occupied) {
        return "étape déjà occupée : " + step + " (worker " + m + " en cours) — attends la libération"
      }
    }
    live.push({
      sessionID: String((ctx && ctx.sessionID) || ""),
      parent: String((ctx && ctx.parent) || ""),
      agent: String((ctx && ctx.agent) || ""),
      model: m,
      provider: POOL.providerOf(m),
      step: step,
      manifest: manifest,
      since: Date.now(),
    })
    POOL.save(live)
  },
  // Retire le slot laissé par un spawn refusé par le runtime (modèle indisponible,
  // interruption). Appelé depuis tool.execute.after sur subagent quand l'output
  // traduit un échec. Le slot est identifié par (sessionID, step, manifest).
  removeSlot(sessionID, step, manifest) {
    const live = POOL.load()
    const filtered = live.filter(
      (s) =>
        !(
          String(s.sessionID || "") === String(sessionID || "") &&
          String(s.step || "") === String(step || "") &&
          String(s.manifest || "") === String(manifest || "")
        ),
    )
    if (filtered.length !== live.length) POOL.save(filtered)
  },
  // Purge les slots dont l'étape est terminée dans son manifest. Sûreté :
  // manifest absent/illisible → ne purge pas (le TTL reste le filet) ; étape
  // introuvable → ne purge pas ; slot sans step → purgé par le TTL seulement.
  purgeClosedSteps() {
    const live = POOL.load()
    const toKeep = []
    let changed = false
    for (const s of live) {
      let purge = false
      if (s.step && s.manifest) {
        const txt = readText(s.manifest)
        if (txt) {
          const step = parseSteps(txt).find((st) => st.id === s.step)
          if (step) {
            purge = !OPEN_STATUS.has(step.statut)
          }
          // étape introuvable → on ne purge pas
        }
        // manifest illisible → on ne purge pas (TTL reste le filet)
      }
      // slot sans step → pas de purge ici (TTL uniquement)
      if (purge) {
        changed = true
      } else {
        toKeep.push(s)
      }
    }
    if (changed) POOL.save(toKeep)
  },
}

// ---- Night lock ----
// Lu à CHAQUE check() : pas de cache.
// Retourne :
//   { active: false }                    → verrou absent (ENOENT), inactif, ou périmé (mode jour normal)
//   { active: true, until, ... }         → verrou actif et valide
//   { active: true, corrupted: true, error: "..." } → verrou présent mais illisible/invalide → fail closed
function readNightLock() {
  let raw
  try {
    raw = readFileSync(NIGHT_LOCK, "utf8")
  } catch (e) {
    // Fichier absent (ENOENT) → mode jour normal
    // Toute autre erreur de lecture (EACCES, EISDIR, EPERM, etc.) → fail closed
    const code = e && typeof e === "object" && "code" in e ? String(e.code) : ""
    if (code === "ENOENT") return { active: false }
    return { active: true, corrupted: true, error: "verrou de nuit illisible (erreur lecture: " + code + ") : répare ou supprime le fichier" }
  }

  // Fichier présent mais vide ou whitespace → corrompu (fail closed)
  if (!raw.trim()) {
    return { active: true, corrupted: true, error: "verrou de nuit illisible (fichier vide) : répare ou supprime le fichier" }
  }

  let data
  try {
    data = JSON.parse(raw)
  } catch (e) {
    // Fichier présent mais JSON invalide → fail closed
    return { active: true, corrupted: true, error: "verrou de nuit illisible (JSON invalide) : répare ou supprime le fichier" }
  }

  if (!data || typeof data !== "object") return { active: false }
  if (data.active !== true) return { active: false }

  const until = Date.parse(data.until)
  if (!Number.isFinite(until)) {
    // Fichier présent, active=true, mais until non datable → fail closed
    return { active: true, corrupted: true, error: "verrou de nuit illisible (champ 'until' invalide) : répare ou supprime le fichier" }
  }

  if (Date.now() >= until) return { active: false } // périmé = inactif

  return { active: true, until, execProviders: data.execProviders, blockTools: data.blockTools }
}

function audit(entry) {
  try {
    mkdirSync(dirname(AUDIT), { recursive: true })
    appendFileSync(AUDIT, JSON.stringify({ ...entry, at: new Date().toISOString() }) + "\n")
  } catch {
    // l'audit ne doit jamais casser l'agent
  }
}

export function check(input) {
  const agent = input && input.agent ? String(input.agent) : ""
  const sid = sessionId(input)
  const tool = String((input && input.tool) || "")
  const args = (input && input.input) || {}
  const blocked = []
  const notes = []

  // Night lock : lu à chaque check()
  const night = readNightLock()
  const nightActive = night.active
  const nightCorrupted = night.corrupted === true

  POOL.purgeClosedSteps()

  if (!AGENTS.has(agent)) return { blocked, notes, tool, text: "" }

  const isShell = SHELL_TOOL.test(tool)
  const isWrite = WRITE_TOOL.test(tool)
  const cmd = isShell ? cmdOf(args) : ""

  if (agent === "orchestrator") {
    if (isWrite) {
      const verdict = orchestratorWrite(firstPath(args), sid)
      if (!verdict.ok) blocked.push(verdict.why)
    }
    if (isShell) {
      if (!cmd) blocked.push("shell sans commande lisible par le guard")
      for (const seg of segments(cmd)) {
        const why = mutating(seg)
        if (why) {
          blocked.push("commande mutatrice interdite au frontière (" + why + ") — ordonne-la au merger")
          break
        }
        const w = firstWord(seg)
        if (w && !READONLY_CMD.test(w)) {
          blocked.push("seules les commandes de lecture sont autorisées au frontière (" + w + ")")
          break
        }
      }
    }
    if (SPAWN_TOOL.test(tool)) {
      const prompt = String(args.prompt || args.message || args.description || "")
      const explicit = (prompt.match(/(?:^|\s)manifest\s*:\s*([^\s`]+)/i) || [])[1] || ""
      let manifestPath = manifestFor(sid)

      if (explicit) {
        const a = absPath(explicit)
        const okOwn = !!sid && a === sessionManifest(sid)
        const legacyOwner = readJson(OWNERS, {})[LEGACY_MANIFEST]
        const okLegacy = a === LEGACY_MANIFEST && (!legacyOwner || legacyOwner === sid)
        // B. Permissions : manifest explicite sous .opencode/night/manifests/ accepté
        const okNightManifest = a.startsWith(NIGHT_MANIFESTS_DIR)
        if (!okOwn && !okLegacy && !okNightManifest) {
          blocked.push("manifest explicite non autorisé (" + explicit + ") — utilise le tien : " + sessionManifest(sid))
        } else manifestPath = a
      }
      if (!manifestPath) {
        blocked.push(
          "le manifest global appartient à une autre orchestration — écris le tien dans " + sessionManifest(sid),
        )
      } else if (!readText(manifestPath).trim()) {
        blocked.push("aucun manifest lisible (" + manifestPath + ") : écris ton manifest dans " + sessionManifest(sid))
      }
      // Parallélisme autorisé : c'est le pool de slots (caps modèle + fournisseur) qui borne.

      const sub = String(args.agent || args.subagent || args.name || "").toLowerCase()
      if (!SPAWNABLE.has(sub)) {
        blocked.push("subagent non autorisé : " + (sub || "(vide)") + " — attendu worker, reviewer ou merger")
      } else {
        const txt = manifestPath ? readText(manifestPath) : ""
        const step = manifestPath ? openStepFor(txt, sub) : null
        const model = String(args.model || "")

        // SOUS VERROU ACTIF : modèle explicite OBLIGATOIRE
        if (nightActive && !model) {
          blocked.push("mode nuit : modèle explicite obligatoire pour tout spawn de subagent")
        }

        if (model) {
          if (sub === "reviewer") {
            if (nightActive) {
              // Mode nuit : REVIEW_MODELS ne s'applique plus, uniquement execAllowed (llama-local / llama-proxy)
              if (!execAllowed(model, true)) {
                blocked.push(
                  "modèle de revue non autorisé pour le reviewer en mode nuit : " +
                    model +
                    " (autorisé : llama-local/*, llama-proxy/*)",
                )
              }
            } else {
              // Mode jour : REVIEW_MODELS ou modèles free/local
              if (!modelAllowed(model, REVIEW_MODELS) && !execAllowed(model, false)) {
                blocked.push(
                  "modèle de revue non autorisé pour le reviewer : " +
                    model +
                    " (autorisé : " +
                    REVIEW_MODELS.join(", ") +
                    ", tout modèle free ou local)",
                )
              }
            }
          } else {
            // worker / merger
            // Mode nuit : aucune exception payante — execAllowedNight est la SEULE voie
            if (nightActive) {
              if (!execAllowed(model, true)) {
                blocked.push(
                  "modèle d'exécution non autorisé en mode nuit : " +
                    model +
                    " (autorisé : llama-local/*, llama-proxy/*)",
                )
              }
            } else {
              // Mode jour : execAllowed (free/local) OU paidDeclared
              if (!execAllowed(model, false) && !paidDeclared(txt, step, model)) {
                blocked.push(
                  "modèle d'exécution non autorisé pour un worker : " +
                    model +
                    " (autorisé : tout modèle free ou local, ou un modèle payant déclaré au manifest : - Modèle payant: " +
                    model +
                    ")",
                )
              } else if (!execAllowed(model, false)) {
                notes.push("modèle payant déclaré au manifest : " + model)
              }
            }
          }
        }
        const pool = model ? POOL.check(model) : { ok: true, why: "" }
        if (!pool.ok) blocked.push(pool.why)
        if (!manifestPath) {
          // déjà bloqué plus haut (pas de manifest à cette session) : on n'ajoute pas de bruit
        } else if (!step) {
          blocked.push("aucune étape en attente pour « " + sub + " » dans " + manifestPath + " — ajoute une étape (Qui: " + sub + ", Statut: annoncée)")
        } else {
          const scope = scopeList(step)
          if (!scope.length) notes.push("étape " + step.id + " sans périmètre : le worker ne sera pas borné")
          else notes.push("spawn " + sub + (model ? " [" + model + "]" : "") + " → étape " + step.id)
        }
      }
      if (!blocked.length) {
        const m = String(args.model || "")
        if (m) {
          const st = manifestPath ? openStepFor(readText(manifestPath), sub) : null
            const refused = POOL.add(m, {
              sessionID: String(args.sessionID || args.session_id || sid),
              parent: sid,
              agent: sub,
              step: st ? st.id : "",
              manifest: manifestPath,
            })
            if (refused) blocked.push(refused)
        }
        rememberSpawn(sub, manifestPath)
      }
    }
    // A. Verrou de nuit : blocage question pour orchestrator
    if (nightActive && tool === "question") {
      blocked.push("mode nuit : aucune question posée à un humain")
    }

    // Fail closed : verrou corrompu → tous les spawns refusés
    if (nightCorrupted) {
      if (SPAWN_TOOL.test(tool)) {
        blocked.push(night.error || "verrou de nuit corrompu : répare ou supprime le fichier")
      }
      if (tool === "question") {
        blocked.push("mode nuit (verrou corrompu) : aucune question posée à un humain")
      }
    }
  }

  if (agent === "reviewer") {
    if (isWrite) blocked.push("le reviewer ne modifie rien : rends un verdict FIX/REJECT")
    if (isShell) {
      if (!cmd) blocked.push("shell sans commande lisible par le guard")
      for (const seg of segments(cmd)) {
        const why = mutating(seg)
        if (why) {
          blocked.push("commande mutatrice interdite au reviewer (" + why + ")")
          break
        }
        const w = firstWord(seg)
        if (w && !READONLY_CMD.test(w) && !TEST_RUNNER.test(w)) {
          blocked.push("seules la lecture et les runners de test sont autorisés au reviewer (" + w + ")")
          break
        }
        if (/^(python3?|uv|poetry)$/.test(w) && !/-m\s+(pytest|unittest)/.test(seg)) {
          blocked.push("script python ad hoc interdit au reviewer : lance les tests (pytest -q)")
          break
        }
      }
    }
  }

  if (agent === "merger") {
    if (isWrite) blocked.push("le merger n'édite pas de fichier")
    if (isShell) {
      if (!cmd) blocked.push("shell sans commande lisible par le guard")
      for (const seg of segments(cmd)) {
        const w = firstWord(seg)
        if (w !== "git") {
          blocked.push("le merger n'exécute que git (" + (w || "?") + ")")
          break
        }
        if (/\bgit\s+(pull|fetch|clone|remote\s+add)\b/.test(seg)) {
          blocked.push("le merger ne fait pas de réseau git pull/fetch/clone/remote (" + seg + ")")
          break
        }
        if (/\bgit\s+push\b/.test(seg)) {
          if (/(--force\b|--force-with-lease|\s-f(?!\w)|--delete\b|\s-d(?!\w)|--mirror\b|--all\b|--tags\b)/.test(seg)) {
            blocked.push("le merger ne force pas le push (" + seg + ")")
            break
          }
          // cible du push : refspec = 2e argument positionnel, destination après « : », refs/heads/ optionnel
          const pushArgs = seg.replace(/^\s*git\s+push\b/, "").trim().split(/\s+/).filter(Boolean)
          const positional: string[] = []
          for (const a of pushArgs) {
            const clean = a.replace(/^["']|["']$/g, "")
            if (clean.startsWith("-")) continue
            positional.push(clean)
          }
          const refspec = positional.length >= 2 ? positional[1] : ""
          const dest = (refspec.includes(":") ? refspec.split(":").pop() : refspec) || ""
          const branch = dest.replace(/^refs\/heads\//, "")
          if (branch === "main" || branch === "master") {
            blocked.push("le merger ne pousse pas main/master — branche dédiée obligatoire (orch/*) (" + seg + ")")
            break
          }
        }
      }
    }
  }

  function extractMutatingTarget(seg, why) {
  // For redirection: target is after > or >>
  if (why === "redirection d'écriture") {
    const m = seg.match(/>\s*([^\s|&;]+)/)
    if (m) return m[1].trim().replace(/^["']|["']$/g, "")
  }
  // For chmod/chown/touch/mkdir/rm/mv/cp/etc.: target is typically the last argument
  const parts = seg.trim().split(/\s+/)
  if (parts.length >= 2) {
    // Skip the command name, return the last argument as potential target
    const lastArg = parts[parts.length - 1].replace(/^["']|["']$/g, "")
    // Filter out flags (starting with -)
    if (!lastArg.startsWith("-")) return lastArg
    // If last arg is a flag, try the one before
    for (let i = parts.length - 2; i >= 1; i--) {
      const arg = parts[i].replace(/^["']|["']$/g, "")
      if (!arg.startsWith("-")) return arg
    }
  }
  return ""
}

if (agent === "worker") {
    if (SPAWN_TOOL.test(tool)) blocked.push("un worker ne lance pas de sous-agent")
    if (isShell) {
      if (!cmd) blocked.push("shell sans commande lisible par le guard")
      // Mutating check + write redirection: only block if target is under .opencode/night/
      for (const seg of segments(cmd)) {
        const why = mutating(seg)
        if (why) {
          const target = extractMutatingTarget(seg, why)
          const norm = normalizeUnderProject(absPath(firstPath({ filePath: target })) || "")
          const nightPrefix = PROJECT + "/.opencode/night"
          if (norm && isUnder(norm, nightPrefix)) {
            blocked.push("écriture interdite au worker sous .opencode/night/ (" + why + ")")
            break
          }
          // Mutating command OUTSIDE .opencode/night/ → allowed (falls back to perimeter check on write)
        }
      }
      // Network write / system actions (kept from before)
      for (const rule of NET_WRITE) {
        const hit = rule.test ? rule.test(cmd) : rule.re.test(cmd)
        if (hit) {
          blocked.push("action extérieure interdite au worker (" + rule.why + ") — livre un [BROUILLON — NON ENVOYÉ]")
          break
        }
      }
      if (/\bsudo\b|\bsystemctl\b/.test(cmd)) blocked.push("action système interdite au worker")
      if (/\bgit\s+push\b/.test(cmd)) blocked.push("git push interdit au worker")
    }
    if (isWrite) {
      const path = firstPath(args)
      // Refuse write/edit on night-mode.json for ALL agents, even if in declared scope
      const normWrite = normalizeUnderProject(absPath(path))
      if (normWrite && normWrite === NIGHT_LOCK) {
        blocked.push("écriture du verrou de nuit interdite (night-mode.json) — même dans le périmètre")
      } else {
        const bound = sid ? bindWorker(sid, path) : null
        if (!bound) {
          notes.push("session worker non associée à une étape : contrôle de périmètre sauté")
        } else if (bound.ambiguous) {
          notes.push("liaison ambiguë : contrôle de périmètre sauté")
        } else {
          const step = activeStep(readText(bound.manifest), "worker")
          if (step) {
            const scope = scopeList(step)
            if (scope.length) {
              if (!path) blocked.push("écriture sans chemin identifiable (périmètre " + step.id + ")")
              else if (!inScope(path, scope)) blocked.push("hors périmètre " + step.id + " : " + path + " (autorisé : " + scope.join(", ") + ")")
            } else notes.push("étape " + step.id + " sans périmètre : contrôle de périmètre sauté")
          } else {
            notes.push("plusieurs étapes worker actives : contrôle de périmètre sauté")
          }
        }
      }
    }
  }

  return { blocked: [...new Set(blocked)], notes, tool, text: String(cmd || firstPath(args)).slice(0, 800) }
}

export default {
  id: "orch-guard",
  async setup(ctx) {
    await ctx.tool.hook("execute.before", async (input) => {
      POOL.purgeClosedSteps()
      const agent = input && input.agent ? String(input.agent) : ""
      if (!AGENTS.has(agent)) return

      const { blocked, notes, tool, text } = check(input)
      if (blocked.length || notes.length) {
        audit({
          agent,
          tool,
          sessionID: input && input.sessionID,
          manifest: manifestFor(sessionId(input)) || null,
          blocked: blocked.length ? blocked : null,
          notes: notes.length ? notes : null,
          input: text,
        })
      }
      if (blocked.length) {
        throw new Error(
          "orch-guard : action refusée (" + blocked.join(" · ") + "). " +
            "Rappel : le frontière planifie et juge, le flash exécute. Annonce l'étape dans ton manifest (.opencode/orch/<session>/orchestration.md).",
        )
      }
    })
    // tool.execute.after sur subagent : si le runtime refuse/interrompt le spawn,
    // on libère le slot qu'il occupait (sinon bloqué 45 min par le TTL).
    await ctx.tool.hook("execute.after", async (input) => {
      const tool = String((input && input.tool) || "")
      if (!SPAWN_TOOL.test(tool)) return
      const output = (input && input.output) || {}
      const outText = String(output.title || output.output || output.metadata || "")
      const failed = /error|fail|interrupt|refus|bloqué|exception/i.test(outText)
      if (!failed) return
      const sid = sessionId(input)
      const args = (input && input.args) || {}
      const manifest = manifestFor(sid)
      const txt = manifest ? readText(manifest) : ""
      const sub = String(args.agent || args.subagent || args.name || "").toLowerCase()
      const step = txt ? openStepFor(txt, sub) : null
      if (sid && step && manifest) {
        POOL.removeSlot(sid, step.id, manifest)
      }
    })
  },
}

---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/recurring-ui/index.ts
---
import { Plugin } from "@opencode/plugin"

export default Plugin.define({
  id: "recurring-ui.server",
  setup() {},
})


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/recurring-ui/tui.ts
---
import { Plugin } from "@opencode/plugin/tui"
import { execFileSync } from "node:child_process"

const PROJECT = "/media/marcs/Linux_Apps/Projets_AI"
const PYTHON = `${PROJECT}/mcp/opencode-recurring/venv/bin/python`
const SERVER = `${PROJECT}/mcp/opencode-recurring/server.py`

type Schedule = {
  kind: "daily" | "every" | "weekly" | "monthly" | "yearly"
  time: string
  start: string
  weekdays?: string[]
  everyDays?: number
  dayOfMonth?: number
  month?: number
  day?: number
}

type GoalConfig = {
  enabled?: boolean
  objective?: string
  agent?: string
  model?: any
}

type GoalRun = {
  sessionID?: string
  sessionTitle?: string
  status?: string
  resumes?: number
  launchedAt?: string
  lastResumeAt?: string
  lastStatus?: string
  notifiedStatus?: string
  tokensUsed?: number
}

type Rule = {
  id: string
  title: string
  paused: boolean
  frequence: string
  prochaine: string | null
  enRetard?: boolean
  schedule: Schedule
  labels: string[]
  kanban: boolean
  telegram: boolean
  goal?: GoalConfig | null
  goalRun?: GoalRun | null
}

type Ctx = any
type Check = { value?: any; error?: string }

const DAY_ORDER = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
const DAY_ALIASES: Record<string, string> = {
  lun: "lun", lundi: "lun", mon: "lun", monday: "lun",
  mar: "mar", mardi: "mar", tue: "mar", tuesday: "mar",
  mer: "mer", mercredi: "mer", wed: "mer", wednesday: "mer",
  jeu: "jeu", jeudi: "jeu", thu: "jeu", thursday: "jeu",
  ven: "ven", vendredi: "ven", fri: "ven", friday: "ven",
  sam: "sam", samedi: "sam", sat: "sam", saturday: "sam",
  dim: "dim", dimanche: "dim", sun: "dim", sunday: "dim",
}
const TIME_RE = /^([01]?\d|2[0-3]):([0-5]\d)$/

const pad = (value: number) => String(value).padStart(2, "0")

function todayISO() {
  const now = new Date()
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

function frDate(value?: string | null) {
  if (!value) return "—"
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  return match ? `${match[3]}/${match[2]}/${match[1]}` : value
}

function shortDateTime(value?: string | null) {
  if (!value) return "—"
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(value)
  return match ? `${match[3]}/${match[2]} ${match[4]}:${match[5]}` : value
}

function longDateTime(value?: string | null) {
  if (!value) return "—"
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(value)
  return match ? `${match[3]}/${match[2]}/${match[1]} ${match[4]}:${match[5]}` : value
}

function call(args: string[]): any {
  try {
    const stdout = execFileSync(PYTHON, [SERVER, ...args], {
      encoding: "utf8",
      timeout: 45000,
      maxBuffer: 1024 * 1024,
    })
    return JSON.parse(stdout)
  } catch (error: any) {
    const stderr = String(error?.stderr ?? "").trim()
    return { error: `${error?.message ?? "échec de l'appel au moteur"}${stderr ? ` — ${stderr}` : ""}` }
  }
}

function describeRule(rule: Rule) {
  const bits = [rule.frequence]
  if (rule.prochaine) bits.push(`prochaine ${shortDateTime(rule.prochaine)}`)
  if (rule.paused) bits.push("suspendue")
  if (rule.enRetard) bits.push("en retard")
  if (!rule.kanban) bits.push("sans carte")
  if (!rule.telegram) bits.push("sans Telegram")
  if (rule.goal?.enabled) {
    const run = rule.goalRun
    bits.push(run?.status ? `🤖 ${run.status}${run.resumes ? ` · ${run.resumes} reprise(s)` : ""}` : "🤖 goal")
  }
  return bits.join(" · ")
}

function detailText(rule: Rule) {
  const lines = [
    `ID : ${rule.id}`,
    `Type : ${rule.frequence}`,
    `Départ : ${frDate(rule.schedule.start)}`,
    `Prochaine : ${rule.prochaine ? longDateTime(rule.prochaine) : "—"}`,
    `État : ${rule.paused ? "suspendue" : "active"}${rule.enRetard ? " (en retard)" : ""}`,
    `Carte Kanban : ${rule.kanban ? `oui (${rule.labels.join(" ")})` : "non"}`,
    `Telegram : ${rule.telegram ? "oui" : "non"}`,
  ]
  if (rule.goal?.enabled) {
    lines.push(`Goal : oui — ${rule.goal.objective || rule.title}`)
    lines.push("Lancement : session agent build à l'échéance, reprise auto si usageLimited")
    const run = rule.goalRun
    if (run) {
      const bits = [run.status || "?", `${run.resumes ?? 0} reprise(s)`]
      if (run.launchedAt) bits.push(`lancé ${shortDateTime(run.launchedAt)}`)
      lines.push(`Dernier goal : ${bits.join(" · ")}`)
      if (run.sessionTitle) lines.push(`Session : ${run.sessionTitle}`)
      if (run.lastStatus) lines.push(`Détail : ${run.lastStatus}`)
    }
  } else {
    lines.push("Goal : non")
  }
  return lines.join("\n")
}

function scheduleLabel(kind: string, values: any, time: string) {
  if (kind === "daily") return `tous les jours à ${time}`
  if (kind === "every") return `tous les ${values.every_days} jours à ${time}`
  if (kind === "weekly") return `hebdo (${values.weekdays.join(", ")}) à ${time}`
  if (kind === "monthly") return `le ${values.day_of_month} du mois à ${time}`
  if (kind === "yearly") return `le ${pad(values.day)}/${pad(values.month)} chaque année à ${time}`
  return time
}

async function ask(ctx: Ctx, options: any, check: (raw: string) => Check): Promise<any> {
  for (;;) {
    const raw = await ctx.ui.dialog.prompt(options)
    if (raw === undefined) return undefined
    const result = check(raw)
    if (result.error) {
      await ctx.ui.dialog.alert({ title: "Valeur invalide", message: result.error })
      continue
    }
    return result.value
  }
}

function checkTitle(raw: string): Check {
  const value = raw.trim()
  return value ? { value } : { error: "Le titre ne peut pas être vide." }
}

function checkTime(raw: string): Check {
  const value = raw.trim() || "09:00"
  if (!TIME_RE.test(value)) return { error: "Heure au format HH:MM (ex : 09:00 ou 18:30)." }
  const [hours, minutes] = value.split(":")
  return { value: `${pad(Number(hours))}:${minutes}` }
}

function checkDate(raw: string): Check {
  const value = raw.trim() || todayISO()
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return { error: "Date au format AAAA-MM-JJ (ex : 2026-10-20)." }
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const parsed = new Date(year, month - 1, day)
  if (parsed.getFullYear() !== year || parsed.getMonth() + 1 !== month || parsed.getDate() !== day) {
    return { error: "Cette date n'existe pas." }
  }
  return { value }
}

function checkEveryDays(raw: string): Check {
  const value = raw.trim() || "2"
  const number = Number(value)
  return Number.isInteger(number) && number >= 1 && number <= 365
    ? { value: number }
    : { error: "Entier entre 1 et 365." }
}

function checkDayOfMonth(raw: string): Check {
  const value = raw.trim() || "1"
  const number = Number(value)
  return Number.isInteger(number) && number >= 1 && number <= 31
    ? { value: number }
    : { error: "Entier entre 1 et 31." }
}

function checkWeekdays(raw: string): Check {
  const parts = raw.toLowerCase().split(/[,\s]+/).filter(Boolean)
  if (!parts.length) return { error: "Indique au moins un jour (ex : lun,jeu)." }
  const days: string[] = []
  for (const part of parts) {
    const day = DAY_ALIASES[part]
    if (!day) return { error: `Jour inconnu « ${part} » (lun, mar, mer, jeu, ven, sam, dim).` }
    if (!days.includes(day)) days.push(day)
  }
  days.sort((left, right) => DAY_ORDER.indexOf(left) - DAY_ORDER.indexOf(right))
  return { value: days }
}

function checkYearly(raw: string): Check {
  const value = raw.trim() || "01/01"
  const match = /^(\d{1,2})\/(\d{1,2})$/.exec(value)
  if (!match) return { error: "Format JJ/MM (ex : 01/01)." }
  const day = Number(match[1])
  const month = Number(match[2])
  if (month < 1 || month > 12 || day < 1 || day > 31) return { error: "Jour ou mois hors limites." }
  return { value: { day, month } }
}

async function addFlow(ctx: Ctx) {
  const title = await ask(
    ctx,
    { title: "Titre de la tâche récurrente", placeholder: "Relancer les leads X" },
    checkTitle,
  )
  if (title === undefined) return

  const kind = await ctx.ui.dialog.select<string>({
    title: "Fréquence",
    options: [
      { title: "Quotidien", value: "daily", description: "Tous les jours" },
      { title: "Tous les N jours", value: "every", description: "Ex : tous les 2 jours" },
      { title: "Hebdomadaire", value: "weekly", description: "Jours de la semaine au choix" },
      { title: "Mensuel", value: "monthly", description: "Un jour fixe du mois" },
      { title: "Annuel", value: "yearly", description: "Une date fixe chaque année" },
    ],
  })
  if (!kind) return

  const payload: any = { title, kind }

  if (kind === "every") {
    payload.every_days = await ask(ctx, { title: "Tous les combien de jours ?", value: "2" }, checkEveryDays)
    if (payload.every_days === undefined) return
  } else if (kind === "weekly") {
    payload.weekdays = await ask(
      ctx,
      { title: "Quels jours ?", description: "lun, mar, mer, jeu, ven, sam, dim", value: "lun" },
      checkWeekdays,
    )
    if (payload.weekdays === undefined) return
  } else if (kind === "monthly") {
    payload.day_of_month = await ask(
      ctx,
      { title: "Quel jour du mois ?", description: "1 à 31", value: "1" },
      checkDayOfMonth,
    )
    if (payload.day_of_month === undefined) return
  } else if (kind === "yearly") {
    const date = await ask(ctx, { title: "Quelle date ?", description: "JJ/MM", value: "01/01" }, checkYearly)
    if (date === undefined) return
    payload.day = date.day
    payload.month = date.month
  }

  payload.time = await ask(ctx, { title: "Heure d'exécution", description: "HH:MM", value: "09:00" }, checkTime)
  if (payload.time === undefined) return

  payload.start = await ask(
    ctx,
    { title: "À partir du", description: "AAAA-MM-JJ", value: todayISO() },
    checkDate,
  )
  if (payload.start === undefined) return

  const effects = await ctx.ui.dialog.select<string>({
    title: "Effets à échéance",
    options: [
      { title: "Carte Kanban + Telegram", value: "both", description: "Labels +Opencode @recurrent" },
      { title: "Carte Kanban seulement", value: "kanban", description: "Pas de notification" },
      { title: "Telegram seulement", value: "telegram", description: "Pas de carte" },
    ],
  })
  if (!effects) return

  payload.kanban = effects !== "telegram"
  payload.telegram = effects !== "kanban"

  const goalMode = await ctx.ui.dialog.select<string>({
    title: "Goal mode",
    options: [
      { title: "Non", value: "off", description: "Carte / Telegram uniquement" },
      {
        title: "Oui — lancer un agent à l'échéance",
        value: "on",
        description: "Session build + /goal, reprise automatique si plafond de tours",
      },
    ],
  })
  if (!goalMode) return

  let goalObjective: string | undefined
  if (goalMode === "on") {
    goalObjective = await ask(
      ctx,
      {
        title: "Objectif du goal",
        description: "Prérempli avec le titre ; éditable",
        value: title,
      },
      checkTitle,
    )
    if (goalObjective === undefined) return
    payload.goal = { objective: goalObjective }
  }

  const recap = [
    `« ${title} »`,
    `Fréquence : ${scheduleLabel(kind, payload, payload.time)}`,
    `Départ : ${frDate(payload.start)}`,
    `Effets : ${effects === "both" ? "carte Kanban + Telegram" : effects === "kanban" ? "carte Kanban" : "Telegram"}`,
    `Goal : ${goalObjective ? `oui — ${goalObjective}` : "non"}`,
  ].join("\n")

  const confirmed = await ctx.ui.dialog.confirm({
    title: "Créer la tâche récurrente ?",
    message: recap,
    label: { confirm: "Créer", cancel: "Annuler" },
  })
  if (!confirmed) return

  const result = call(["--add-json", JSON.stringify(payload)])
  if (result.error) {
    await ctx.ui.dialog.alert({ title: "Création impossible", message: result.error })
    return
  }
  ctx.ui.toast.show({
    title: "Tâche récurrente créée",
    message: `${result.frequence} · prochaine ${shortDateTime(result.prochaine)}`,
    variant: "success",
    duration: 4000,
  })
}

async function openList(ctx: Ctx) {
  for (;;) {
    const data = call(["--list", "--json"])
    if (data.error) {
      await ctx.ui.dialog.alert({ title: "Tâches récurrentes", message: data.error })
      return
    }

    const rules: Rule[] = data.rules ?? []
    ctx.ui.dialog.set({ size: "large" })

    const choice = await ctx.ui.dialog.select<string>({
      title: rules.length ? `Tâches récurrentes (${rules.length})` : "Tâches récurrentes",
      options: [
        { title: "➕ Ajouter une tâche récurrente", value: "__add__", description: "Formulaire guidé" },
        ...rules.map((rule) => ({
          title: `${rule.paused ? "⏸" : "▶"} ${rule.title}`,
          value: rule.id,
          description: describeRule(rule),
        })),
      ],
    })
    if (!choice) return

    if (choice === "__add__") {
      await addFlow(ctx)
      continue
    }

    const rule = rules.find((item) => item.id === choice)
    if (!rule) continue

    const action = await ctx.ui.dialog.select<string>({
      title: rule.title,
      options: [
        { title: "👁 Voir le détail", value: "detail", description: describeRule(rule) },
        ...(rule.goal?.enabled
          ? [
              {
                title: "▶ Lancer le goal maintenant",
                value: "run-goal",
                description: "Crée une session build et lance /goal immédiatement",
              },
            ]
          : []),
        ...(rule.goalRun?.sessionID
          ? [
              {
                title: "↗ Ouvrir la dernière session goal",
                value: "open-goal",
                description: rule.goalRun.status ? `Statut : ${rule.goalRun.status}` : undefined,
              },
            ]
          : []),
        {
          title: rule.paused ? "▶ Reprendre" : "⏸ Suspendre",
          value: "toggle",
          description: rule.paused ? "Réactive les échéances" : "Met les échéances en pause",
        },
        {
          title: "🗑 Supprimer",
          value: "delete",
          description: "La règle seulement, pas les cartes déjà créées",
        },
        { title: "↩ Retour à la liste", value: "back" },
      ],
    })

    if (action === "detail") {
      await ctx.ui.dialog.alert({ title: rule.title, message: detailText(rule) })
      continue
    }

    if (action === "run-goal") {
      const result = call(["--run-goal", rule.id])
      if (result.error) {
        await ctx.ui.dialog.alert({ title: "Lancement impossible", message: result.error })
      } else {
        ctx.ui.toast.show({
          title: "Goal lancé",
          message: result.sessionTitle ?? result.sessionID ?? "session créée",
          variant: "success",
          duration: 5000,
        })
      }
      continue
    }

    if (action === "open-goal") {
      const sessionID = rule.goalRun?.sessionID
      if (!sessionID) continue
      try {
        ctx.ui.router.navigate({ type: "session", sessionID })
      } catch (error: any) {
        await ctx.ui.dialog.alert({
          title: "Ouverture impossible",
          message: String(error?.message ?? error),
        })
      }
      return
    }

    if (action === "toggle") {
      const result = call(["--toggle", rule.id])
      if (result.error) {
        await ctx.ui.dialog.alert({ title: "Action impossible", message: result.error })
      } else {
        ctx.ui.toast.show({
          title: result.paused ? "Règle suspendue" : "Règle reprise",
          message: result.title,
          variant: "success",
        })
      }
      continue
    }

    if (action === "delete") {
      const confirmed = await ctx.ui.dialog.confirm({
        title: "Supprimer la règle ?",
        message: `« ${rule.title} »\n${describeRule(rule)}\n\nLes cartes déjà créées restent dans le Kanban.`,
        label: { confirm: "Supprimer", cancel: "Annuler" },
      })
      if (!confirmed) continue
      const result = call(["--delete", rule.id, "--yes"])
      if (result.error) {
        await ctx.ui.dialog.alert({ title: "Suppression impossible", message: result.error })
      } else {
        ctx.ui.toast.show({ title: "Règle supprimée", message: result.title, variant: "success" })
      }
      continue
    }

    // « back » ou Échap : on réaffiche la liste.
  }
}

function Commands(ctx: Ctx) {
  ctx.keymap.layer(() => ({
    mode: "global",
    priority: 10,
    commands: [
      {
        id: "recurring.open",
        title: "Tâches récurrentes OpenCode",
        description: "Voir, ajouter, suspendre ou supprimer les tâches récurrentes",
        group: "OpenCode",
        palette: true,
        slash: { name: "recurring", arguments: true },
        run: async (input?: string) => {
          const argument = (input ?? "").trim().toLowerCase()
          if (argument === "add" || argument === "ajouter") {
            await addFlow(ctx)
            await openList(ctx)
            return
          }
          await openList(ctx)
        },
      },
    ],
  }))
  return null
}

export default Plugin.define({
  id: "recurring-ui",
  setup(context) {
    // `keymap.layer` doit vivre dans un scope Solid : on le monte via le slot `app`,
    // comme le plugin goal. Le layer est libéré avec le slot.
    const dispose = context.ui.slot({ append: "app", render: () => Commands(context) })
    return () => {
      if (typeof dispose === "function") dispose()
    }
  },
})


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/employee-ui/index.ts
---
import { Plugin } from "@opencode/plugin"

// Côté serveur : rien à faire, l'interface vit dans ./tui (TUI uniquement).
export default Plugin.define({
  id: "employee-ui.server",
  setup() {},
})


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/employee-ui/tui.ts
---
// employee-ui — popup native /employee (TUI)
//
// Menu de l'employé autonome : statut, objectifs du jour, compétences (pool de
// skills assignés), plan courant, journal, pause/reprise. La popup appelle le
// CLI JSON du superviseur — même état, mêmes verrous que le cron.
import { Plugin } from "@opencode/plugin/tui"
import { execFileSync } from "node:child_process"

const PROJECT = "/media/marcs/Linux_Apps/Projets_AI"
const PYTHON = "python3"
const SUP = `${PROJECT}/mcp/opencode-employee/supervisor.py`
const LOG = `${PROJECT}/Employe/supervisor.log`

type Task = { id: string; kind: string; skill?: string; title?: string; status: string; priority?: number }
type Competence = { skill: string; description: string; lastDone: string; note: string }
type SkillEntry = { name: string; description: string; assigned: boolean; lastDone: string }
type Approval = { id: string; kind: string; what: string; who: string; due: string | null }

type UIState = {
  paused: boolean
  pauseWhy?: string
  model: string
  shiftCounter: number
  lastRole?: string
  lastGoalStatus?: string
  failuresStreak: number
  lastShiftEnd?: string
  running: { role: string; number: number; task: string; since: string } | null
  plan: { updatedAt?: string; reason?: string; tasks: Task[] } | null
  competences: Competence[]
  skills: SkillEntry[]
  objectifs: string
  questions: string[]
  queue: number
  approvals?: { pending: Approval[]; counts: { pending?: number; ok?: number; ko?: number } }
  journalHeads: string[]
  statusText: string
}

type Ctx = any

function call(args: string[]): any {
  try {
    const stdout = execFileSync(PYTHON, [SUP, ...args], {
      encoding: "utf8",
      timeout: 60000,
      maxBuffer: 4 * 1024 * 1024,
    })
    return JSON.parse(stdout)
  } catch (error: any) {
    const stderr = String(error?.stderr ?? "").trim()
    return { error: `superviseur injoignable${stderr ? ` — ${stderr}` : ""}` }
  }
}

function callText(args: string[], cwd?: string): string {
  try {
    return execFileSync(PYTHON, [SUP, ...args], {
      encoding: "utf8",
      timeout: 60000,
      cwd: cwd || PROJECT,
    }).trim()
  } catch (error: any) {
    return `Erreur : ${String(error?.message ?? error).slice(0, 300)}`
  }
}

function planText(plan: UIState["plan"]): string {
  if (!plan) return "Aucun plan exploitable (le planner tournera au prochain tick)."
  const lines = [`Mis à jour : ${plan.updatedAt ?? "?"}`, `Raison : ${plan.reason ?? "—"}`, "", "Tâches :"]
  const tasks = plan.tasks ?? []
  if (!tasks.length) lines.push("  (aucune — le planner a brainstormé ou n'a rien trouvé)")
  for (const t of tasks) {
    const name = t.skill || t.title || t.id
    lines.push(`  ${iconStatus(t.status)} ${name} · ${t.kind || "mission"} · prio ${t.priority ?? "—"}`)
  }
  return lines.join("\n")
}

function iconStatus(status: string): string {
  if (status === "done") return "✅"
  if (status === "failed") return "❌"
  if (status === "in_progress") return "🔨"
  if (status === "pending") return "⏳"
  return "•"
}

function summary(data: UIState): string {
  const bits = [
    data.paused ? `⏸ en pause (${data.pauseWhy ?? ""})` : "▶️ actif",
    `${data.shiftCounter} passages`,
    `rôle : ${data.lastRole ?? "?"} (${data.lastGoalStatus ?? "?"})`,
    `échecs : ${data.failuresStreak}`,
  ]
  if (data.running) bits.push(`en cours : ${data.running.role} #${data.running.number} — ${data.running.task}`)
  if (data.plan?.tasks?.length) bits.push(`${data.plan.tasks.filter((t) => ["pending", "in_progress"].includes(t.status))?.length ?? 0} tâche(s) au plan`)
  bits.push(`${data.competences.length} compétence(s)`, `${data.queue} action(s) en file`)
  if (data.questions?.length) bits.push(`${data.questions.length} question(s) ouverte(s)`)
  return bits.join(" · ")
}

function approvalIcon(kind: string): string {
  if (kind === "decision") return "🧭"
  if (kind === "dm") return "✉️"
  return "📄"
}

async function approvalsFlow(ctx: Ctx) {
  for (;;) {
    const data: UIState = call(["--ui-json"])
    if (data.error) {
      await ctx.ui.dialog.alert({ title: "À valider", message: data.error })
      return
    }
    const pending: Approval[] = data.approvals?.pending ?? []
    if (!pending.length) {
      await ctx.ui.dialog.alert({
        title: "📬 À valider (0)",
        message: "Aucune demande en attente. Les brouillons et décisions du superviseur apparaîtront ici.",
      })
      return
    }
    const okCount = data.approvals?.counts?.ok ?? 0
    const koCount = data.approvals?.counts?.ko ?? 0
    const choice = await ctx.ui.dialog.select<string>({
      title: `📬 À valider (${pending.length}) — déjà ok : ${okCount} · ko : ${koCount}`,
      options: [
        ...pending.map((a) => ({
          title: `${approvalIcon(String(a.kind ?? ""))} ${String(a.what ?? "").slice(0, 80) || a.id}`,
          value: a.id,
          description: `${a.id} · ${a.kind ?? "?"} · pour ${a.who || "?"} · échéance ${a.due ?? "—"}`,
        })),
        { title: "↩ Retour", value: "__back__" },
      ],
    })
    if (!choice || choice === "__back__") return
    const item = pending.find((a) => a.id === choice)
    if (!item) continue

    await ctx.ui.dialog.alert({
      title: `${item.id} — ${item.kind ?? "?"}`,
      message: [item.what || "(sans description)", "", `Pour : ${item.who || "—"}`, `Échéance : ${item.due ?? "—"}`].join("\n"),
    })

    const verdict = await ctx.ui.dialog.select<string>({
      title: `Statuer sur ${item.id}`,
      options: [
        { title: "✅ ok — approuver", value: "ok", description: "L'employé pourra exécuter cette action au prochain shift" },
        { title: "❌ ko — refuser", value: "ko", description: "Refus motivé : l'employé devra proposer autre chose" },
        { title: "↩ Retour", value: "__back__" },
      ],
    })
    if (!verdict || verdict === "__back__") continue

    let reason = ""
    if (verdict === "ko") {
      const raw = await ctx.ui.dialog.prompt({
        title: `Motif du refus (${item.id})`,
        placeholder: "ex. attendre les réponses avant de relancer",
      })
      reason = String(raw ?? "").trim()
      if (!reason) {
        await ctx.ui.dialog.alert({ title: "Refus annulé", message: "Motif vide : aucun statut n'a été envoyé, l'item reste en attente." })
        continue
      }
    }

    const result = callText(verdict === "ok" ? ["--approve", item.id, "ok"] : ["--approve", item.id, "ko", reason])
    const failed = /^(❌|Erreur|usage|statut manquant)/.test(result)
    if (failed) {
      await ctx.ui.dialog.alert({ title: "Refusé par le superviseur", message: result || "(aucun retour)" })
      continue
    }
    await ctx.ui.dialog.alert({
      title: verdict === "ok" ? "✅ Approuvé" : "⛔ Refusé",
      message: result || "(aucun retour du superviseur)",
    })
    ctx.ui.toast.show({
      title: verdict === "ok" ? "Item approuvé" : "Item refusé",
      message: item.id,
      variant: "success",
      duration: 3000,
    })
  }
}

async function objectifsFlow(ctx: Ctx) {
  for (;;) {
    const data: UIState = call(["--ui-json"])
    if (data.error) {
      await ctx.ui.dialog.alert({ title: "Objectifs", message: data.error })
      return
    }
    const body = data.objectifs || "(aucun objectif aujourd'hui)"
    const choice = await ctx.ui.dialog.select<string>({
      title: `Objectifs du jour — ${new Date().toISOString().slice(0, 10)}`,
      options: [
        { title: "📄 Voir / éditer à la main", value: "__show__", description: body.slice(0, 120) },
        { title: "➕ Ajouter un objectif", value: "__add__", description: "Une ligne, prioritaire pour le planner" },
        { title: "↩ Retour", value: "__back__" },
      ],
    })
    if (!choice || choice === "__back__") return
    if (choice === "__show__") {
      await ctx.ui.dialog.alert({
        title: "Employe/objectifs.md (section du jour)",
        message: body,
      })
      continue
    }
    const raw = await ctx.ui.dialog.prompt({
      title: "Nouvel objectif du jour",
      placeholder: "ex. finir la shortlist de leads X avec DM rédigés",
    })
    const text = String(raw ?? "").trim()
    if (!text) continue
    const result = call(["--add-objectif", text])
    if (result.error) {
      await ctx.ui.dialog.alert({ title: "Ajout impossible", message: result.error })
    } else {
      ctx.ui.toast.show({ title: "Objectif ajouté", message: text, variant: "success", duration: 3500 })
    }
  }
}

async function competencesFlow(ctx: Ctx) {
  for (;;) {
    const data: UIState = call(["--ui-json"])
    if (data.error) {
      await ctx.ui.dialog.alert({ title: "Compétences", message: data.error })
      return
    }
    const skills: SkillEntry[] = data.skills ?? []
    if (!skills.length) {
      await ctx.ui.dialog.alert({
        title: "Compétences",
        message: "Aucun skill trouvé dans .opencode/skills/. Crée un skill puis reviens.",
      })
      return
    }
    const choice = await ctx.ui.dialog.select<string>({
      title: `Compétences de l'employé (${data.competences.length}/${skills.length} assignées)`,
      options: [
        ...skills.map((s) => ({
          title: `${s.assigned ? "✓" : "·"} ${s.name}`,
          value: s.name,
          description:
            `${s.description.slice(0, 90)}` + (s.lastDone ? ` · dernier : ${s.lastDone}` : ""),
        })),
        { title: "↩ Retour", value: "__back__" },
      ],
    })
    if (!choice || choice === "__back__") return
    const current = skills.find((s) => s.name === choice)
    const target = current?.assigned ? "off" : "on"
    const result = call(["--set-competence", choice, target])
    if (result.error) {
      await ctx.ui.dialog.alert({ title: "Refusé", message: result.error })
      continue
    }
    ctx.ui.toast.show({
      title: target === "on" ? "Compétence assignée" : "Compétence retirée",
      message:
        target === "on"
          ? `${choice} rejoint le pool : le planner peut l'utiliser comme tâche de base`
          : `${choice} n'est plus proposée au planner`,
      variant: "success",
      duration: 4000,
    })
  }
}

async function openMenu(ctx: Ctx) {
  for (;;) {
    const data: UIState = call(["--ui-json"])
    if (data.error) {
      await ctx.ui.dialog.alert({ title: "Employé", message: data.error })
      return
    }
    const pendingApprovals = data.approvals?.pending?.length ?? 0
    ctx.ui.dialog.set({ size: "large" })
    const choice = await ctx.ui.dialog.select<string>({
      title: `👤 Employé — ${summary(data)}`,
      options: [
        {
          title: data.paused ? "▶️ Reprendre" : "⏸ Mettre en pause",
          value: "toggle",
          description: data.paused ? "Relance la boucle dès le prochain tick" : "Interrompt le shift en cours et rend le GPU",
        },
        { title: "🎯 Objectifs du jour", value: "objectifs", description: data.objectifs ? data.objectifs.split("\n").filter(Boolean).slice(0, 2).join(" / ").slice(0, 90) : "aucun" },
        { title: "🧰 Compétences (skills assignés)", value: "competences", description: data.competences.map((c) => c.skill).join(", ").slice(0, 90) || "aucune" },
        { title: `📬 À valider (${pendingApprovals})`, value: "approvals", description: pendingApprovals ? `${pendingApprovals} demande(s) en attente · ok ${data.approvals?.counts?.ok ?? 0} · ko ${data.approvals?.counts?.ko ?? 0}` : "aucune demande en attente" },
        { title: "🧭 Plan courant", value: "plan", description: data.plan ? `${(data.plan.tasks ?? []).filter((t) => ["pending", "in_progress"].includes(t.status)).length} tâche(s) actives` : "aucun" },
        { title: "📜 Journal (derniers blocs)", value: "journal", description: (data.journalHeads ?? []).slice(-1)[0] ?? "vide" },
        { title: "🩹 Logs du superviseur", value: "logs", description: "20 dernières lignes" },
      ],
    })
    if (!choice) return

    if (choice === "toggle") {
      if (data.paused) {
        callText(["--resume"])
        ctx.ui.toast.show({ title: "▶️ Employé relancé", message: "prochain tick ≤ 3 min", variant: "success" })
      } else {
        callText(["--pause", "pause depuis /employee"])
        ctx.ui.toast.show({ title: "⏸ Employé en pause", message: "shift en cours interrompu, GPU rendu", variant: "success" })
      }
      continue
    }
    if (choice === "objectifs") {
      await objectifsFlow(ctx)
      continue
    }
    if (choice === "competences") {
      await competencesFlow(ctx)
      continue
    }
    if (choice === "approvals") {
      await approvalsFlow(ctx)
      continue
    }
    if (choice === "plan") {
      await ctx.ui.dialog.alert({ title: "Plan courant", message: planText(data.plan) })
      continue
    }
    if (choice === "journal") {
      await ctx.ui.dialog.alert({
        title: "Derniers blocs du journal",
        message: (data.journalHeads ?? []).slice(-10).reverse().join("\n") || "(vide)",
      })
      continue
    }
    if (choice === "logs") {
      try {
        const tailText = execFileSync("tail", ["-n", "20", LOG], { encoding: "utf8", timeout: 5000 })
        await ctx.ui.dialog.alert({ title: "supervisor.log", message: tailText.trim().slice(0, 3000) || "(vide)" })
      } catch (e: any) {
        await ctx.ui.dialog.alert({ title: "Logs", message: String(e?.message ?? e) })
      }
      continue
    }
  }
}

function Commands(ctx: Ctx) {
  ctx.keymap.layer(() => ({
    mode: "global",
    priority: 10,
    commands: [
      {
        id: "employee.open",
        title: "Employé autonome — tableau de bord",
        description: "Objectifs du jour, compétences (skills), plan, journal, pause",
        group: "OpenCode",
        palette: true,
        slash: { name: "employee", arguments: true },
        run: async (input?: string) => {
          const argument = (input ?? "").trim().toLowerCase()
          if (argument === "pause") {
            callText(["--pause", "pause depuis /employee"])
            ctx.ui.toast.show({ title: "⏸ Employé en pause", variant: "success" })
            return
          }
          if (argument === "resume") {
            callText(["--resume"])
            ctx.ui.toast.show({ title: "▶️ Employé relancé", variant: "success" })
            return
          }
          if (argument === "status") {
            await ctx.ui.dialog.alert({ title: "Employé", message: callText(["--status"]) })
            return
          }
          await openMenu(ctx)
        },
      },
    ],
  }))
  return null
}

export default Plugin.define({
  id: "employee-ui",
  setup(context) {
    // `keymap.layer` doit vivre dans un scope Solid : on le monte via le slot `app`.
    const dispose = context.ui.slot({ append: "app", render: () => Commands(context) })
    return () => {
      if (typeof dispose === "function") dispose()
    }
  },
})


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/orch-ui/index.ts
---
import { Plugin } from "@opencode/plugin"

// Côté serveur : rien à faire, l'interface vit dans ./tui (TUI uniquement).
export default Plugin.define({
  id: "orch-ui.server",
  setup() {},
})


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/plugins/orch-ui/tui.ts
---
// orch-ui — popup native /orchestrator (TUI)
//
// Graphe compact : une ligne par session orchestrateur, une ligne par subagent
// (rôle, étape, modèle, ancienneté, statut vivant) + changement de modèle.
// Suit le modèle de employee-ui (ctx.keymap.layer, ctx.ui.dialog, ctx.ui.toast).
import { Plugin } from "@opencode/plugin/tui"
import { readFileSync, writeFileSync } from "node:fs"

const ROOT = "/media/marcs/Linux_Apps/Projets_AI"
const STATE = `${ROOT}/.opencode/orch-state.json`
const ORCH_DIR = `${ROOT}/.opencode/orch`
const AUDIT = `${ROOT}/.opencode/orch-audit.jsonl`

type Ctx = any
type Slot = { sessionID: string; parent: string; agent: string; model: string; provider: string; step: string; since: number; manifest?: string }

/* ───────── helpers ───────── */

function readJSON(path: string): any {
  try { return JSON.parse(readFileSync(path, "utf8")) }
  catch { return null }
}

function readText(path: string): string {
  try { return readFileSync(path, "utf8") }
  catch { return "" }
}

function writeText(path: string, content: string): void {
  writeFileSync(path, content, "utf8")
}

function escRE(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

function humanAge(ms: number): string {
  const diff = Date.now() - ms
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return "à l'instant"
  if (mins < 60) return `il y a ${mins} min`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `il y a ${hrs} h`
  const days = Math.floor(hrs / 24)
  return `il y a ${days} j`
}

function fmtAuditLine(line: any): string {
  const at = line.at ? new Date(line.at).toLocaleString("fr-FR") : "?"
  const agent = line.agent ?? "?"
  const reasons = Array.isArray(line.blocked) ? line.blocked.join(" ; ") : (line.blocked ?? "")
  return `${at} ${agent} — ${reasons}`.slice(0, 200)
}

/* ───────── parsing ───────── */

function parseSlots(): Slot[] {
  const data = readJSON(STATE)
  if (!data || !Array.isArray(data.slots)) return []
  return data.slots.filter((s: Slot) => s && s.sessionID)
}

function parseAudit(): string[] {
  const lines = readText(AUDIT).split("\n").filter(Boolean)
  const entries: any[] = []
  for (const raw of lines) {
    try { entries.push(JSON.parse(raw)) } catch { /* skip */ }
  }
  const blocked = entries.filter((e: any) => e && e.blocked && e.blocked.length > 0)
  return blocked.slice(-10).map(fmtAuditLine)
}

/* ───────── child resolution ───────── */

// The slot registry is indexed by ORCHESTRATOR id: on a fresh spawn the guard
// records sessionID === parent === the caller, and NEVER the child's id.
// The child must come from the session API — using slot.sessionID would
// retune (or display) the orchestrator itself.
//   - slot.sessionID !== slot.parent → sessionID IS the child (relance: the
//     guard recorded the id passed at the call);
//   - otherwise the most recent session from ctx.data.session.list() with
//     parentID === slot.parent AND agent === slot.agent (time.updated, else
//     time.created);
//   - nothing resolvable → null.
function childOf(ctx: Ctx, slot: Slot): string | null {
  if (slot.sessionID !== slot.parent) return slot.sessionID
  const listFn = ctx?.data?.session?.list
  if (typeof listFn !== "function") return null
  let childID = ""
  let bestT = -1
  try {
    for (const s of listFn.call(ctx.data.session) || []) {
      if (!s || typeof s.id !== "string" || !s.id) continue
      if (String(s.parentID ?? "") !== String(slot.parent)) continue
      if (String(s.agent ?? "") !== String(slot.agent)) continue
      const t = Number((s.time && (s.time.updated ?? s.time.created)) || 0)
      if (t > bestT) { bestT = t; childID = s.id }
    }
  } catch { childID = "" }
  if (!childID || childID === slot.parent) return null
  return childID
}

/* ───────── model catalogue ───────── */

// Fallback list: used ONLY when the live catalogue is unavailable (API
// missing, throwing, or empty). Same entries as the former hard-coded list.
const FALLBACK_MODELS = [
  "llama-local/qwen-opus",
  "llama-local/qwen3.6q4",
  "llama-local/qwen3.6iq3-150k",
  "nvidia/nvidia/nemotron-3.5-super-vl-120b-a12b",
  "nvidia/z-ai/glm-5.3-flash",
  "cline-pass/cline-pass/glm-5.3-flash",
  "opencode/deepseek-v4.1-flash-free",
]

type ModelOption = { id: string; description: string }

function providerLabel(providerID: string): string {
  const known: { [k: string]: string } = {
    "llama-local": "llama-local (local)",
    "llama-proxy": "llama-proxy (local)",
    nvidia: "NVIDIA NIM",
    "cline-pass": "Cline Pass",
    openrouter: "OpenRouter",
    opencode: "OpenCode Zen",
    anthropic: "Anthropic",
    openai: "OpenAI",
    google: "Google",
  }
  return known[providerID] || providerID
}

function isFreeModel(id: string): boolean {
  return /free/i.test(id)
}

function isLocalModel(id: string): boolean {
  return id.startsWith("llama-local/") || id.startsWith("llama-proxy/")
}

// Mirror of the guard's policy: the popup must never offer a model the guard
// would refuse at spawn time.
//   worker / merger → execution models: free, local, nvidia, cline-pass,
//                     openrouter only when :free / -free.
//   reviewer        → review pool + free + local.
function modelAllowedFor(role: string, id: string): boolean {
  const free = isFreeModel(id)
  const local = isLocalModel(id)
  if (role === "reviewer") {
    return free || local
      || id.startsWith("cline-pass/")
      || id === "opencode/glm-5.3-flash-free"
      || id === "opencode/claude-sonnet-5"
      || id === "opencode/gpt-5.6-sol"
  }
  if (free || local) return true
  if (id.startsWith("nvidia/") || id.startsWith("cline-pass/")) return true
  if (id.startsWith("openrouter/")) return /:free|-free/i.test(id)
  return false
}

// Live catalogue via the API (never opencode.json: the `opencode` provider is
// built in and absent from the config file).
//   ctx.client.model.list()    → { data: Model.Info[] }
//   ctx.client.provider.list() → { data: Provider.Info[] }
// Model.Info exposes `id`, `providerID`, `name`; Provider.Info exposes `id`,
// `name`.
async function readCatalog(ctx: Ctx): Promise<ModelOption[] | null> {
  try {
    const client: any = ctx?.client
    if (!client || !client.model) return null
    const listFn = client.model.list
    if (typeof listFn !== "function") return null
    const res: any = await listFn.call(client.model)
    const data: any[] = Array.isArray(res?.data) ? res.data : (Array.isArray(res) ? res : [])
    if (!data.length) return null

    const labels: { [k: string]: string } = {}
    try {
      const provFn = client.provider && client.provider.list
      if (typeof provFn === "function") {
        const pres: any = await provFn.call(client.provider)
        const providers: any[] = Array.isArray(pres?.data) ? pres.data : (Array.isArray(pres) ? pres : [])
        for (const p of providers) {
          if (p && p.id) labels[String(p.id)] = String(p.name || p.id)
        }
      }
    } catch { /* provider names are cosmetic */ }

    const seen: { [k: string]: boolean } = {}
    const out: ModelOption[] = []
    for (const m of data) {
      const providerID = String(m?.providerID ?? "")
      const id = String(m?.id ?? "")
      if (!providerID || !id) continue
      const full = `${providerID}/${id}`
      if (seen[full]) continue
      seen[full] = true
      out.push({ id: full, description: labels[providerID] || providerLabel(providerID) })
    }
    return out.length ? out : null
  } catch {
    return null
  }
}

// The ONE place where the offer policy lives. Called with slot.agent
// (worker / reviewer / merger); sorted by provider then id, never empty.
async function modelsFor(role: string, ctx?: Ctx): Promise<ModelOption[]> {
  const sortById = (list: ModelOption[]) =>
    list.slice().sort((a, b) => a.id.localeCompare(b.id))

  const catalog = await readCatalog(ctx)
  if (catalog) {
    const allowed = catalog.filter((m) => modelAllowedFor(role, m.id))
    if (allowed.length) return sortById(allowed)
  }
  const fallback = FALLBACK_MODELS.map((id) => ({
    id,
    description: providerLabel(id.split("/")[0]),
  }))
  const allowedFallback = fallback.filter((m) => modelAllowedFor(role, m.id))
  return sortById(allowedFallback.length ? allowedFallback : fallback)
}

// Model currently used by the subagent (live child first, then the slot).
function currentModelOf(ctx: Ctx, slot: Slot): string {
  try {
    const child = childOf(ctx, slot)
    if (child) {
      const ds: any = ctx?.data?.session
      if (ds && typeof ds.get === "function") {
        const m = fmtModel(ds.get(child)?.model)
        if (m) return m
      }
    }
  } catch { /* registre de slots */ }
  return slot.model || ""
}

/* ───────── model change flow ───────── */

async function changeModelFlow(ctx: Ctx, preset?: Slot) {
  let slot: Slot | undefined = preset
  if (!slot) {
    const slots = parseSlots()
    if (!slots.length) {
      await ctx.ui.dialog.alert({ title: "Modèle", message: "Aucun slot actif." })
      return
    }
    const choice = await ctx.ui.dialog.select<string>({
      title: "Changer le modèle d'un subagent",
      options: [
        ...slots.map((s) => ({
          title: `${s.agent} · ${s.model}`,
          value: s.sessionID,
          description: `${s.step} · depuis ${humanAge(s.since)}`,
        })),
      ],
    })
    if (!choice) return

    slot = slots.find((s) => s.sessionID === choice)
  }
  if (!slot) { await ctx.ui.dialog.alert({ title: "Modèle", message: "Slot introuvable." }); return }

  const current = currentModelOf(ctx, slot)
  const models = await modelsFor(slot.agent, ctx)

  const modelChoice = await ctx.ui.dialog.select<string>({
    title: `Modèle pour ${slot.agent} (${slot.step})`,
    options: models.map((m) => {
      const opt: any = { title: m.id, value: m.id, description: m.description }
      if (m.id === current) opt.current = true
      return opt
    }),
  })
  if (!modelChoice) return

  // 1) Resolve the LIVE child session of this subagent via the single
  // resolver (childOf): the slot registry is indexed by ORCHESTRATOR id
  // (slot.sessionID === slot.parent), so the child must come from the
  // session API — switching on slot.sessionID would retune the
  // orchestrator itself.
  const childID = childOf(ctx, slot)
  if (!childID) {
    await ctx.ui.dialog.alert({
      title: "Modèle",
      message: `Aucune session enfant trouvée — modèle NON appliqué à ${slot.agent}, manifeste inchangé.`,
    })
    return
  }
  // 2) Apply the model to the CHILD session: session.switchModel keeps its
  // full history and continues on the new model at the next turn. The
  // provider/id split is on the FIRST slash only — ids contain slashes.
  const firstSlash = modelChoice.indexOf("/")
  const providerID = firstSlash > 0 ? modelChoice.slice(0, firstSlash) : ""
  const modelID = firstSlash > 0 ? modelChoice.slice(firstSlash + 1) : ""
  const switchFn = ctx.client?.session?.switchModel
  if (typeof switchFn !== "function") {
    await ctx.ui.dialog.alert({
      title: "Modèle",
      message: `API switchModel indisponible — modèle NON appliqué à ${slot.agent}, manifeste inchangé.`,
    })
    return
  }
  try {
    await switchFn.call(ctx.client.session, {
      sessionID: childID,
      model: { providerID, id: modelID },
    })
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err)
    await ctx.ui.dialog.alert({
      title: "Modèle",
      message: `Échec du changement de modèle (${reason}) — manifeste inchangé.`,
    })
    return
  }

  // 3) Write the model change to the orchestration manifest. The slot carries
  // an absolute `manifest` path; fall back to the orchestrator-dir layout only
  // when that field is missing.
  const manifestPath = slot.manifest || `${ORCH_DIR}/${slot.sessionID}/orchestration.md`
  const content = readText(manifestPath)
  if (!content) {
    await ctx.ui.dialog.alert({ title: "Modèle", message: `Manifeste introuvable pour ${slot.sessionID}` })
    return
  }

  const lines = content.split("\n")
  const headerRE = /^#{1,6}\s/
  const idLineRE = new RegExp(`^\\s*-\\s*id:\\s*${escRE(slot.step)}\\s*$`)

  // Locate the step by its `- id: <step>` line (not by the header title),
  // keeping the LAST block when the same id appears several times.
  let idIdx = -1
  for (let i = lines.length - 1; i >= 0; i--) {
    if (idLineRE.test(lines[i])) { idIdx = i; break }
  }
  if (idIdx < 0) {
    await ctx.ui.dialog.alert({
      title: "Modèle",
      message: `Étape « ${slot.step} » introuvable dans le manifeste — aucune écriture.`,
    })
    return
  }

  let blockStart = idIdx
  while (blockStart > 0 && !headerRE.test(lines[blockStart])) blockStart--
  let blockEnd = idIdx + 1
  while (blockEnd < lines.length && !headerRE.test(lines[blockEnd])) blockEnd++

  // Idempotent write, confined to the step's own block: replace an existing
  // `- Modèle:` in place, else insert one after `- Qui:`. Never a duplicate,
  // never an invented block — the file is left untouched when unsure.
  let wrote = false
  for (let i = blockStart + 1; i < blockEnd; i++) {
    if (/^\s*-\s*Modèle:/.test(lines[i])) {
      const indent = lines[i].match(/^\s*/)?.[0] ?? ""
      lines[i] = `${indent}- Modèle: ${modelChoice}`
      wrote = true
      break
    }
  }
  if (!wrote) {
    for (let i = blockStart + 1; i < blockEnd; i++) {
      if (/^\s*-\s*Qui:/.test(lines[i])) {
        const indent = lines[i].match(/^\s*/)?.[0] ?? ""
        lines.splice(i + 1, 0, `${indent}- Modèle: ${modelChoice}`)
        wrote = true
        break
      }
    }
  }
  if (!wrote && blockEnd > blockStart) {
    const indent = lines[blockStart].match(/^\s*/)?.[0] ?? ""
    lines.splice(blockStart + 1, 0, `${indent}- Modèle: ${modelChoice}`)
    wrote = true
  }

  if (wrote) writeText(manifestPath, lines.join("\n"))

  ctx.ui.toast.show({
    title: `Modèle changé`,
    message: `${slot.agent} → ${modelChoice}`,
    variant: "success",
    duration: 4000,
  })
}

/* ───────── graph view ───────── */

function shortID(id: string): string {
  return `ses_${String(id || "").replace(/^ses_/, "").slice(0, 8)}…`
}

function shortKey(id: string): string {
  return `ses_${String(id || "").replace(/^ses_/, "").slice(0, 8)}`
}

function roleIcon(agent: string): string {
  if (agent === "reviewer") return "🔎"
  if (agent === "merger") return "🔀"
  return "🔨"
}

function fmtModel(m: any): string {
  try {
    if (!m) return ""
    if (typeof m === "string") return m
    if (typeof m === "object") {
      const id = m.id ?? ""
      const provider = m.providerID ?? m.provider ?? ""
      if (provider && id) return `${provider}/${id}`
      if (id) return String(id)
    }
  } catch { /* ignore */ }
  return ""
}

const SUB_CAP = 12

function buildGraph(ctx: Ctx): { title: string; options: any[] } {
  const slots = parseSlots()
  const enriched = slots.map((s: Slot) => {
    // Model and status come from the CHILD session (childOf), never from
    // slot.sessionID: on a fresh spawn that id is the ORCHESTRATOR's, so
    // ds.get/ds.status on it would display the orchestrator's model/status.
    // No resolvable child → fall back to slot.model and NO status footer.
    let model = s.model || ""
    let status = ""
    let child: string | null = null
    try {
      child = childOf(ctx, s)
      if (child) {
        const ds: any = ctx?.data?.session
        if (ds && typeof ds.get === "function") {
          const m = fmtModel(ds.get(child)?.model)
          if (m) model = m
        }
        if (ds && typeof ds.status === "function") {
          const st = ds.status(child)
          if (st === "running") status = "en cours"
          else if (st === "idle") status = "au repos"
        }
      }
    } catch { /* registre de slots */ }
    return { slot: s, model, status }
  })

  const groups: { parent: string; items: any[] }[] = []
  for (const e of enriched) {
    const parent = String(e.slot.parent || e.slot.sessionID)
    const g = groups.find((x) => x.parent === parent)
    if (g) g.items.push(e)
    else groups.push({ parent, items: [e] })
  }

  const totalSubs = enriched.length
  const hidden = Math.max(0, totalSubs - SUB_CAP)
  const title = `🎛 Orchestrator — ${groups.length} session${groups.length === 1 ? "" : "s"} · ${totalSubs} subagent${totalSubs === 1 ? "" : "s"}`

  const options: any[] = []
  let shown = 0
  for (const g of groups) {
    options.push({
      title: `${shortID(g.parent)} · ⚙ orchestrateur`,
      value: { kind: "header", sessionID: g.parent },
      disabled: true,
      category: shortKey(g.parent),
    })
    for (let i = 0; i < g.items.length; i++) {
      const e = g.items[i]
      if (shown >= SUB_CAP) continue
      const connector = i === g.items.length - 1 ? "└─" : "├─"
      const age = e.slot.since ? humanAge(e.slot.since) : "?"
      const opt: any = {
        title: `${connector} ${roleIcon(e.slot.agent)} ${e.slot.agent || "agent"} · ${e.slot.step || "?"} · ${e.model || "?"}`,
        value: { kind: "slot", sessionID: e.slot.sessionID, parent: g.parent, step: e.slot.step },
        description: `étape ${e.slot.step || "?"} · ${age}`,
        category: shortKey(g.parent),
      }
      if (e.status) opt.footer = e.status
      options.push(opt)
      shown++
    }
  }
  if (hidden > 0) {
    options.push({ title: `… et ${hidden} autres subagents`, value: { kind: "overflow", count: hidden }, disabled: true })
  }
  return { title, options }
}

async function openMenu(ctx: Ctx) {
  for (;;) {
    ctx.ui.dialog.set({ size: "xlarge" })

    let title = "🎛 Orchestrator"
    let options: any[] = []
    try {
      const graph = buildGraph(ctx)
      title = graph.title
      options = graph.options
    } catch {
      options = [{ title: "état indisponible", value: { kind: "error" }, disabled: true }]
    }
    if (!options.length) {
      options = [{ title: "aucun slot actif", value: { kind: "empty" }, disabled: true }]
    }

    const choice = await ctx.ui.dialog.select<any>({ title, options })
    if (!choice || typeof choice !== "object" || choice.kind !== "slot") return

    const slots = parseSlots()
    const slot = slots.find((s: Slot) => s.sessionID === choice.sessionID && (s.step || "") === (choice.step || ""))
    if (!slot) {
      await ctx.ui.dialog.alert({ title: "Orchestrator", message: "Slot introuvable — l'état a changé, rouvre /orchestrator." })
      continue
    }
    // One change per popup: hand control back instead of re-opening the
    // menu (a held Enter key must never trigger thousands of writes).
    // Reopen /orchestrator for a second change.
    await changeModelFlow(ctx, slot)
    return
  }
}

/* ───────── plugin definition ───────── */

function Commands(ctx: Ctx) {
  ctx.keymap.layer(() => ({
    mode: "global",
    priority: 10,
    commands: [
      {
        id: "orchestrator.open",
        title: "Orchestrator — vue d'ensemble",
        description: "Graphe des sessions et subagents, changement de modèle",
        group: "OpenCode",
        palette: true,
        slash: { name: "orchestrator", arguments: true },
        run: async (input?: string) => {
          const argument = (input ?? "").trim().toLowerCase()
          if (argument === "slots") {
            const slots = parseSlots()
            if (!slots.length) {
              await ctx.ui.dialog.alert({ title: "Slots", message: "Aucun slot actif." })
            } else {
              const lines = slots.map((s) => `${s.agent} · ${s.model} · étape ${s.step}`)
              await ctx.ui.dialog.alert({ title: "Slots actifs", message: lines.join("\n") })
            }
            return
          }
          if (argument === "audit") {
            const lines = parseAudit()
            if (!lines.length) {
              await ctx.ui.dialog.alert({ title: "Audit", message: "Aucun bloc récent." })
            } else {
              await ctx.ui.dialog.alert({ title: "Derniers blocs", message: lines.join("\n") })
            }
            return
          }
          await openMenu(ctx)
        },
      },
    ],
  }))
  return null
}

export default Plugin.define({
  id: "orch-ui",
  setup(context) {
    const dispose = context.ui.slot({ append: "app", render: () => Commands(context) })
    return () => {
      if (typeof dispose === "function") dispose()
    }
  },
})


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/README.md
---
# `@opencode/plugin`

Authoring interfaces and runtime loader support for OpenCode V2 plugins:

- `@opencode/plugin` — [Promise plugin API](./src/README.md)
- `@opencode/plugin/effect` — [Effect plugin API](./src/effect/README.md)
- `@opencode/plugin/rpc` — portable RPC contract definitions
- `@opencode/plugin/tui` — terminal UI plugin API

## Packaging And Runtime `effect`

When the OpenCode CLI loads server or TUI plugins, it resolves imports of `effect`, exported `effect/*` subpaths, and `@opencode/plugin` entrypoints (including imports from dependencies inside a plugin's `node_modules`) to the host's runtime module instances so fibers, loggers, and `Schema` parsers share one copy.

- Declare `"effect": "^4.0.0"` as a `peerDependency` (and `devDependency` for local type-checking and testing) rather than a bundled runtime dependency.
- Do not bundle `effect` into published plugin files; if you build with a bundler, keep `effect` and `effect/*` external. Two copies of `effect` do not share fiber, logger, or `Schema` internals.
- Plugins and their `node_modules` dependencies always receive OpenCode's host `effect` instance. Use `effect` APIs and module paths compatible with the OpenCode release you target; dependencies built on another `effect` major (such as Effect 3) are not supported.
- Only public `effect` subpaths are provided. A plugin that imports one of Effect's private `internal` modules fails to load with an error naming the path.
- The compiled OpenCode binary does not include the Scalar and Swagger UI assets used by Effect's HTTP API docs pages; serving those pages from a plugin shows a notice instead.


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/package.json
---
{
  "$schema": "https://json.schemastore.org/package.json",
  "name": "@opencode/plugin",
  "version": "2.0.32",
  "type": "module",
  "license": "MIT",
  "scripts": {
    "test": "bun test --only-failures",
    "typecheck": "tsgo --noEmit",
    "build": "tsc -p tsconfig.build.json"
  },
  "exports": {
    ".": {
      "import": "./dist/promise/index.js",
      "types": "./dist/promise/index.d.ts"
    },
    "./effect": {
      "import": "./dist/effect/index.js",
      "types": "./dist/effect/index.d.ts"
    },
    "./host": {
      "import": "./dist/host.js",
      "types": "./dist/host.d.ts"
    },
    "./runtime": {
      "import": "./dist/runtime.js",
      "types": "./dist/runtime.d.ts"
    },
    "./tui": {
      "import": "./dist/tui/index.js",
      "types": "./dist/tui/index.d.ts"
    },
    "./*": {
      "import": "./dist/*.js",
      "types": "./dist/*.d.ts"
    }
  },
  "imports": {
    "#plugin-source": {
      "bun": "./dist/source.bun.js",
      "node": "./dist/source.node.js",
      "default": "./dist/source.node.js"
    }
  },
  "files": [
    "dist"
  ],
  "dependencies": {
    "@ai-sdk/provider": "3.0.8",
    "@opencode/ai": "2.0.32",
    "@opencode/client": "2.0.32",
    "@opencode/protocol": "2.0.32",
    "@opencode/schema": "2.0.32",
    "@opencode/util": "2.0.32",
    "effect": "4.0.1"
  },
  "peerDependencies": {
    "@opencode/theme": "2.0.32",
    "@opentui/core": ">=0.5.17",
    "@opentui/solid": ">=0.5.17",
    "solid-js": ">=1.9.0"
  },
  "peerDependenciesMeta": {
    "@opencode/theme": {
      "optional": true
    },
    "@opentui/core": {
      "optional": true
    },
    "@opentui/solid": {
      "optional": true
    },
    "solid-js": {
      "optional": true
    }
  },
  "devDependencies": {
    "@opencode/theme": "2.0.32",
    "@opentui/core": "0.5.17",
    "@opentui/solid": "0.5.17",
    "@tsconfig/bun": "1.0.9",
    "@tsconfig/node22": "22.0.2",
    "@types/bun": "1.4.0",
    "@types/node": "24.12.2",
    "solid-js": "1.9.15",
    "typescript": "5.8.2",
    "@typescript/native-preview": "7.0.0-dev.20251207.1"
  }
}

---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/app.d.ts
---
export interface App {
    readonly name: string;
    readonly version: string;
    readonly channel: string;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/host.d.ts
---
export * as Host from "./host.js";
export interface Target {
    readonly directory: string;
    readonly name?: string;
}
export interface Entrypoints {
    readonly server?: string;
    readonly tui?: string;
    readonly rpc?: string;
}
export declare function resolve(target: Target): Entrypoints;
export declare function load(entrypoint: string): Promise<unknown>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/options.d.ts
---
export type PluginOptions = Readonly<Record<string, any>>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/permission.d.ts
---
import { Schema } from "effect";
export declare const AssertInput: Schema.Struct<{
    readonly sessionID: Schema.brand<Schema.String, "SessionID"> & {
        create: () => string & import("effect/Brand").Brand<"SessionID">;
        descending: (id?: string) => string & import("effect/Brand").Brand<"SessionID">;
    };
    readonly agent: Schema.optional<Schema.brand<Schema.String, "Agent.ID">>;
    readonly action: Schema.String;
    readonly resources: Schema.$Array<Schema.String>;
    readonly save: Schema.decodeTo<Schema.optional<Schema.toType<Schema.$Array<Schema.String>>>, Schema.optionalKey<Schema.$Array<Schema.String>>, never, never>;
    readonly metadata: Schema.decodeTo<Schema.optional<Schema.toType<Schema.$Record<Schema.String, Schema.Unknown>>>, Schema.optionalKey<Schema.$Record<Schema.String, Schema.Unknown>>, never, never>;
    readonly source: Schema.decodeTo<Schema.optional<Schema.toType<Schema.Union<readonly [Schema.Struct<{
        readonly type: Schema.Literal<"tool">;
        readonly messageID: Schema.String;
        readonly id: Schema.String;
    }>]>>>, Schema.optionalKey<Schema.Union<readonly [Schema.Struct<{
        readonly type: Schema.Literal<"tool">;
        readonly messageID: Schema.String;
        readonly id: Schema.String;
    }>]>>, never, never>;
}>;
export interface AssertInput extends Schema.Schema.Type<typeof AssertInput> {
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/rpc.d.ts
---
export { Rpc } from "@opencode/schema/rpc";


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/runtime-modules.d.ts
---
export type RuntimeModuleLoader = () => Record<string, unknown> | Promise<Record<string, unknown>>;
export declare function resolveHostPackageRoots(from?: string, packages?: readonly string[]): string[];
export declare function loadRuntimeModules(): Readonly<Record<string, RuntimeModuleLoader>>;
export declare function discoverPluginRuntimeSpecifiers(from?: string, packages?: readonly string[]): ReadonlyMap<string, string>;
export declare function pluginRuntimeLoaderCode(specifier: string, entries: ReadonlyMap<string, string>): string;
export declare function createLoader(resolved: string): RuntimeModuleLoader;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/runtime.d.ts
---
import { type RuntimeModuleLoader } from "./runtime-modules.js";
export declare function provides(specifier: string): boolean;
export declare function ensurePluginRuntime(): Readonly<Record<string, RuntimeModuleLoader>>;
export declare function createForeignPackageFilter(rootsInput?: Iterable<string>): RegExp;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/source.bun.d.ts
---
export declare function prepareSource(entrypoint: string, track: (file: string, directory?: boolean) => void): Promise<{
    version: string;
    load: () => Promise<unknown>;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/source.d.ts
---
export declare function createPluginSources(watch: (file: string) => Promise<void>): {
    read: (entrypoint: string) => Promise<{
        version: string;
        module: unknown;
    }>;
    dispose: () => void;
};
export declare function localSource(spec: string, directory: string): import("url").URL | undefined;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/source.node.d.ts
---
export declare function prepareSource(entrypoint: string, track: (file: string, directory?: boolean) => void): Promise<{
    version: string;
    load: () => Promise<unknown>;
    dispose: () => void;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/source.package.d.ts
---
export declare function missingPackageTarget(specifier: string, importer: string): string | undefined;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/storage.d.ts
---
import type { Schema } from "effect";
export interface StorageEntry {
    readonly key: string;
    readonly value: Schema.Json;
}
export interface StorageScanOptions {
    readonly prefix: string;
    readonly after?: string;
    readonly limit?: number;
}
export interface StorageScanResult {
    readonly entries: readonly StorageEntry[];
    readonly next?: string;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/worktree.d.ts
---
export interface WorktreeCreateInput {
    readonly sourceDirectory: string;
    /** Suggested destination after naming and collision handling. Strategies may return a different directory. */
    readonly directory: string;
    /** Starting ref, not the name of a new branch. Reject unsupported refs rather than ignoring them. */
    readonly branch?: string;
}
export interface WorktreeRemoveInput {
    readonly directory: string;
    readonly force: boolean;
}
export interface WorktreeResult {
    /** Actual directory created by the strategy, used for inventory and startup commands. */
    readonly directory: string;
}
export interface WorktreeEntry extends WorktreeResult {
    readonly type: "root" | "worktree";
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/agent.d.ts
---
import type { AgentApi } from "@opencode/client/effect/api";
import type { Agent } from "@opencode/schema/agent";
import type { Effect, Types } from "effect";
import type { Transform } from "./registration.js";
export interface AgentEditor {
    list(): readonly Types.DeepMutable<Agent.Info>[];
    get(id: string): Types.DeepMutable<Agent.Info> | undefined;
    default(id: string | undefined): void;
    update(id: string, update: (agent: Types.DeepMutable<Agent.Info>) => void): void;
    remove(id: string): void;
}
export interface AgentDomain extends AgentApi<unknown> {
    readonly transform: Transform<AgentEditor>;
    readonly reload: () => Effect.Effect<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/aisdk.d.ts
---
import type { LanguageModelV3 } from "@ai-sdk/provider";
import type { Model } from "@opencode/schema/model";
import type { ModelHooks } from "./registration.js";
export interface AISDKHooks {
    sdk: {
        readonly model: Model.Info;
        readonly package: string;
        readonly options: Record<string, any>;
        sdk?: any;
    };
    language: {
        readonly model: Model.Info;
        readonly sdk: any;
        readonly options: Record<string, any>;
        language?: LanguageModelV3;
    };
}
export interface AISDKDomain {
    readonly hook: ModelHooks<AISDKHooks>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/command.d.ts
---
import type { CommandApi } from "@opencode/client/effect/api";
import type { PromptInput } from "@opencode/schema/prompt-input";
import type { Session } from "@opencode/schema/session";
import type { SessionInbox } from "@opencode/schema/session-inbox";
import type { Effect } from "effect";
import type { Transform } from "./registration.js";
export interface CommandInvocation {
    readonly sessionID: Session.ID;
    readonly prompt: PromptInput.Prompt;
    readonly delivery: SessionInbox.Delivery;
}
export interface CommandDefinition {
    readonly name: string;
    readonly description?: string;
    readonly execute: (input: CommandInvocation) => Effect.Effect<void, unknown>;
}
export interface CommandEditor {
    add(definition: CommandDefinition): void;
}
export interface CommandDomain extends Pick<CommandApi<unknown>, "list"> {
    readonly transform: Transform<CommandEditor>;
    readonly reload: () => Effect.Effect<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/event.d.ts
---
import type { EventApi } from "@opencode/client/effect/api";
export interface EventDomain extends Pick<EventApi<unknown>, "subscribe"> {
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/index.d.ts
---
export * as Plugin from "./plugin.js";
export type { StorageEntry, StorageScanOptions, StorageScanResult } from "../storage.js";
export { Agent } from "@opencode/schema/agent";
export { Command } from "@opencode/schema/command";
export { Connection } from "@opencode/schema/connection";
export { Credential } from "@opencode/schema/credential";
export { Integration } from "@opencode/schema/integration";
export { Location } from "@opencode/schema/location";
export { Mcp } from "@opencode/schema/mcp";
export { Model } from "@opencode/schema/model";
export { PersistentPty } from "@opencode/schema/persistent-pty";
export { Provider } from "@opencode/schema/provider";
export { Reference } from "@opencode/schema/reference";
export { Rpc } from "@opencode/schema/rpc";
export { Skill } from "@opencode/schema/skill";
export { Vcs } from "@opencode/schema/vcs";
export { WebSearch } from "@opencode/schema/websearch";
export { Worktree } from "@opencode/schema/worktree";


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/integration.d.ts
---
import type { ConnectionInfo } from "@opencode/client";
import type { IntegrationApi } from "@opencode/client/effect/api";
import { Connection } from "@opencode/schema/connection";
import { Credential } from "@opencode/schema/credential";
import { Form } from "@opencode/schema/form";
import type { Effect, Scope } from "effect";
import type { Transform } from "./registration.js";
type IntegrationRef = {
    id: string;
    name: string;
};
export interface IntegrationOAuthMethod {
    readonly id: string;
    readonly type: "oauth";
    readonly label: string;
    readonly form?: Form.Fields;
}
export interface IntegrationCommandMethod {
    readonly id: string;
    readonly type: "command";
    readonly label: string;
    readonly command: ReadonlyArray<string>;
}
export interface IntegrationKeyMethod {
    readonly type: "key";
    readonly label?: string;
    readonly form?: Form.Fields;
}
export interface IntegrationExternalMethod {
    readonly id: string;
    readonly type: "external";
    readonly label: string;
    readonly form?: Form.Fields;
}
export interface IntegrationEnvMethod {
    readonly type: "env";
    readonly names: ReadonlyArray<string>;
}
export type IntegrationMethod = IntegrationOAuthMethod | IntegrationCommandMethod | IntegrationKeyMethod | IntegrationExternalMethod | IntegrationEnvMethod;
export type IntegrationOAuthAuthorization = {
    readonly url: string;
    readonly instructions: string;
    readonly expiresAt?: number;
} & ({
    readonly mode: "auto";
    readonly callback: Effect.Effect<Credential.OAuth, unknown>;
} | {
    readonly mode: "code";
    readonly callback: (code: string) => Effect.Effect<Credential.OAuth, unknown>;
});
export type IntegrationOAuthMethodRegistration = {
    readonly integrationID: string;
    readonly method: IntegrationOAuthMethod;
    readonly authorize: (answer: Form.Answer) => Effect.Effect<IntegrationOAuthAuthorization, unknown, Scope.Scope>;
    readonly refresh?: (credential: Credential.OAuth) => Effect.Effect<Credential.OAuth, unknown>;
    readonly label?: (credential: Credential.OAuth) => string | undefined;
};
export type IntegrationMethodRegistration = IntegrationOAuthMethodRegistration | {
    readonly integrationID: string;
    readonly method: IntegrationCommandMethod;
} | {
    readonly integrationID: string;
    readonly method: IntegrationKeyMethod;
} | {
    readonly integrationID: string;
    readonly method: IntegrationExternalMethod;
} | {
    readonly integrationID: string;
    readonly method: IntegrationEnvMethod;
};
export interface IntegrationEditor {
    list(): readonly IntegrationRef[];
    get(id: string): IntegrationRef | undefined;
    update(id: string, update: (integration: IntegrationRef) => void): void;
    remove(id: string): void;
    readonly method: {
        list(integrationID: string): readonly IntegrationMethod[];
        update(input: IntegrationMethodRegistration): void;
        remove(integrationID: string, method: IntegrationMethod): void;
    };
}
export interface IntegrationDomain extends Omit<IntegrationApi<unknown>, "wellknown"> {
    readonly transform: Transform<IntegrationEditor>;
    readonly reload: () => Effect.Effect<void>;
    readonly connection: {
        readonly active: (integrationID: string) => Effect.Effect<ConnectionInfo | undefined>;
        readonly resolve: (connection: ConnectionInfo) => Effect.Effect<Credential.Value | undefined, unknown>;
        /** Reports a problem with a connection, such as a required sign-in; `undefined` clears it. */
        readonly status: (input: {
            readonly integrationID: string;
            readonly connection: ConnectionInfo;
            readonly status: Connection.Status | undefined;
        }) => Effect.Effect<void>;
    };
}
export {};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/mcp.d.ts
---
import type { McpApi } from "@opencode/client/effect/api";
import type { Mcp } from "@opencode/schema/mcp";
import type { Effect, Types } from "effect";
import type { Transform } from "./registration.js";
export interface MCPEditor {
    list(): readonly [string, Types.DeepMutable<Mcp.ServerConfig>][];
    get(name: string): Types.DeepMutable<Mcp.ServerConfig> | undefined;
    set(name: string, config: Mcp.ServerConfig): void;
    update(name: string, update: (config: Types.DeepMutable<Mcp.ServerConfig>) => void): void;
    remove(name: string): void;
}
export interface MCPDomain extends Pick<McpApi<unknown>, "list"> {
    readonly transform: Transform<MCPEditor>;
    readonly reload: () => Effect.Effect<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/model.d.ts
---
import type { ModelApi } from "@opencode/client/effect/api";
import type { Model } from "@opencode/schema/model";
import type { Effect, Types } from "effect";
import type { ProviderRecord } from "./provider.js";
import type { Transform } from "./registration.js";
export interface ModelEditor {
    /** Candidates from available providers, including models disabled by earlier transforms. */
    list(providerID?: string): readonly Types.DeepMutable<Model.Info>[];
    get(providerID: string, modelID: string): Types.DeepMutable<Model.Info> | undefined;
    /** Edits raw model overrides; cannot create an unavailable provider. */
    update(providerID: string, modelID: string, update: (model: Types.DeepMutable<Model.Info>) => void): void;
    remove(providerID: string, modelID: string): void;
    readonly default: {
        get(): {
            providerID: string;
            modelID: string;
        } | undefined;
        set(providerID: string, modelID: string): void;
    };
    /** Immutable provider inputs, including inactive templates, before model transforms. */
    readonly provider: {
        list(): readonly ProviderRecord[];
        get(providerID: string): ProviderRecord | undefined;
    };
}
export interface ModelDomain extends ModelApi<unknown> {
    readonly transform: Transform<ModelEditor>;
    readonly reload: () => Effect.Effect<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/permission.d.ts
---
import type { PermissionApi } from "@opencode/client/effect/api";
import type { Agent } from "@opencode/schema/agent";
import type { Permission } from "@opencode/schema/permission";
import type { Session } from "@opencode/schema/session";
import type { Tool } from "@opencode/schema/tool";
import type { Effect } from "effect";
import type { AssertInput } from "../permission.js";
import type { Hooks } from "./registration.js";
export interface PermissionEvaluation {
    readonly sessionID: Session.ID;
    readonly agent?: Agent.ID;
    readonly action: string;
    readonly resources: ReadonlyArray<string>;
    readonly metadata?: Record<string, unknown>;
    readonly source?: Permission.Source;
    effect: Permission.Effect;
    message?: string;
}
export interface PermissionHooks {
    readonly evaluate: PermissionEvaluation;
}
export type PermissionDomain = Pick<PermissionApi<unknown>, "list" | "get" | "reply"> & {
    readonly hook: Hooks<PermissionHooks>;
    /**
     * Evaluates the session's permission rules and, when approval is required, prompts the user and
     * waits for the reply. Succeeds when allowed. A deny rule or a rejection with feedback fails with a
     * `Tool.Error` carrying the reason; a plain rejection ends the current step.
     */
    readonly assert: (input: AssertInput) => Effect.Effect<void, Tool.Error>;
};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/plugin.d.ts
---
import type { CredentialApi, ExperimentalApi, GenerateApi, PluginApi } from "@opencode/client/effect/api";
import type { Location } from "@opencode/schema/location";
import type { Effect, Scope } from "effect";
import type { PluginOptions } from "../options.js";
import type { App } from "../app.js";
import type { AgentDomain } from "./agent.js";
import type { AISDKDomain } from "./aisdk.js";
import type { CommandDomain } from "./command.js";
import type { EventDomain } from "./event.js";
import type { IntegrationDomain } from "./integration.js";
import type { MCPDomain } from "./mcp.js";
import type { ModelDomain } from "./model.js";
import type { PermissionDomain } from "./permission.js";
import type { ProviderDomain } from "./provider.js";
import type { ReferenceDomain } from "./reference.js";
import type { RpcDomain } from "./rpc.js";
import type { SessionDomain } from "./session.js";
import type { ShellDomain } from "./shell.js";
import type { SkillDomain } from "./skill.js";
import type { StorageDomain } from "./storage.js";
import type { ToolDomain } from "./tool.js";
import type { VcsDomain } from "./vcs.js";
import type { WebSearchDomain } from "./websearch.js";
import type { WorktreeDomain } from "./worktree.js";
export interface Context {
    readonly app: App;
    readonly location: Location.Info;
    readonly options: PluginOptions;
    readonly agent: AgentDomain;
    readonly aisdk: AISDKDomain;
    readonly command: CommandDomain;
    readonly credential: Pick<CredentialApi<unknown>, "activate">;
    readonly event: EventDomain;
    readonly experimental: {
        readonly terminal: Pick<ExperimentalApi<unknown>["persistentPty"], "read">;
    };
    readonly integration: IntegrationDomain;
    readonly mcp: MCPDomain;
    readonly model: ModelDomain;
    readonly generate: GenerateApi<unknown>;
    readonly permission: PermissionDomain;
    readonly plugin: Pick<PluginApi<unknown>, "list">;
    readonly provider: ProviderDomain;
    readonly reference: ReferenceDomain;
    readonly rpc: RpcDomain;
    readonly session: SessionDomain;
    readonly shell: ShellDomain;
    readonly skill: SkillDomain;
    readonly storage: StorageDomain;
    readonly tool: ToolDomain;
    readonly vcs: VcsDomain;
    readonly websearch: WebSearchDomain;
    readonly worktree: WorktreeDomain;
}
export interface Plugin<R = Scope.Scope> {
    readonly id: string;
    readonly effect: (context: Context) => Effect.Effect<void, never, R>;
}
export declare function define<R = Scope.Scope>(plugin: Plugin<R>): Plugin<R>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/provider.d.ts
---
import type { ProviderApi } from "@opencode/client/effect/api";
import type { Model } from "@opencode/schema/model";
import type { Provider } from "@opencode/schema/provider";
import type { ConnectionInfo } from "@opencode/client";
import type { Effect, Types } from "effect";
import type { Transform } from "./registration.js";
/** Provider metadata and immutable source definitions, including inactive providers. */
export interface ProviderRecord {
    readonly provider: Provider.Info;
    readonly models: ReadonlyMap<string, Model.Info>;
    readonly sourceConnection?: ConnectionInfo;
}
export interface ProviderEditor {
    list(): readonly ProviderRecord[];
    get(providerID: string): ProviderRecord | undefined;
    /** A discovered account-specific inventory can be bound to the connection that produced it. */
    add(input: {
        info: Provider.Info;
        models: readonly Model.Info[];
        sourceConnection?: ConnectionInfo;
    }): void;
    update(providerID: string, update: (provider: Types.DeepMutable<Provider.Info>) => void): void;
    remove(providerID: string): void;
    readonly models: {
        set(providerID: string, models: readonly Model.Info[]): void;
        /** Updates an owned copy of a source definition. */
        update(providerID: string, modelID: string, update: (model: Types.DeepMutable<Model.Info>) => void): void;
        remove(providerID: string, modelID: string): void;
    };
}
export interface ProviderDomain extends ProviderApi<unknown> {
    readonly transform: Transform<ProviderEditor>;
    readonly reload: () => Effect.Effect<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/reference.d.ts
---
import type { ReferenceGitSource, ReferenceLocalSource } from "@opencode/client";
import type { ReferenceApi } from "@opencode/client/effect/api";
import type { Effect } from "effect";
import type { Transform } from "./registration.js";
export interface ReferenceEditor {
    add(name: string, source: ReferenceLocalSource | ReferenceGitSource): void;
    remove(name: string): void;
    list(): readonly (readonly [string, ReferenceLocalSource | ReferenceGitSource])[];
    get(name: string): ReferenceLocalSource | ReferenceGitSource | undefined;
}
export interface ReferenceDomain extends ReferenceApi<unknown> {
    readonly transform: Transform<ReferenceEditor>;
    readonly reload: () => Effect.Effect<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/registration.d.ts
---
import type { Effect, Scope } from "effect";
export interface Registration {
    readonly dispose: Effect.Effect<void>;
}
export interface ModelHookOptions {
    /** Limits the hook to one provider. Unscoped hooks apply to every provider. */
    readonly providerID?: string;
}
export type Hooks<Spec, Failures extends Record<keyof Spec, unknown> = Record<keyof Spec, never>> = <Name extends keyof Spec>(name: Name, callback: (input: Spec[Name]) => Effect.Effect<void, Failures[Name]>) => Effect.Effect<Registration, never, Scope.Scope>;
export type ModelHooks<Spec, Failures extends Record<keyof Spec, unknown> = Record<keyof Spec, never>> = <Name extends keyof Spec>(name: Name, callback: (input: Spec[Name]) => Effect.Effect<void, Failures[Name]>, options?: Spec[Name] extends {
    readonly model: unknown;
} ? ModelHookOptions : never) => Effect.Effect<Registration, never, Scope.Scope>;
export type Transform<Input> = (callback: (input: Input) => void) => Effect.Effect<Registration, never, Scope.Scope>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/rpc.d.ts
---
import type { RpcApi } from "@opencode/client/effect/api";
export type { RpcClient } from "@opencode/client/effect/api";
import type { Rpc } from "@opencode/schema/rpc";
import type { Effect, Scope } from "effect";
import type { Registration } from "./registration.js";
export interface RpcCallContext<M extends Rpc.Method> {
    readonly error: Rpc.ErrorFactory<M>;
}
export type RpcHandlers<D extends Rpc.Definition> = {
    readonly [Name in keyof D["methods"]]: (input: Rpc.Output<D["methods"][Name]["input"]>, context: RpcCallContext<D["methods"][Name]>) => Effect.Effect<Rpc.HandlerOutput<D["methods"][Name]["output"]>, Rpc.HandlerError<D["methods"][Name]>>;
};
export interface RpcRegistration<D extends Rpc.Definition> extends Registration {
    readonly events: {
        readonly emit: (...args: Rpc.EventInput<D>) => Effect.Effect<void, unknown>;
    };
}
export interface RpcDomain extends RpcApi<Rpc.SystemError, never, unknown> {
    readonly register: <const D extends Rpc.Definition>(definition: D, handlers: RpcHandlers<NoInfer<D>>) => Effect.Effect<RpcRegistration<D>, unknown, Scope.Scope>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/session.d.ts
---
import type { SessionApi } from "@opencode/client/effect/api";
import type { GenerationOptionsFields, Message, SystemPart } from "@opencode/ai";
import type { Agent } from "@opencode/schema/agent";
import type { Model } from "@opencode/schema/model";
import type { PromptInput } from "@opencode/schema/prompt-input";
import type { Session } from "@opencode/schema/session";
import type { SessionInbox } from "@opencode/schema/session-inbox";
import type { SessionError } from "@opencode/schema/session-error";
import type { SessionMessage } from "@opencode/schema/session-message";
import type { TokenUsage } from "@opencode/schema/token-usage";
import type { JsonSchema, Types } from "effect";
import type { ModelHooks } from "./registration.js";
export interface SessionPrompt {
    readonly sessionID: Session.ID;
    readonly messageID: SessionMessage.ID;
    prompt: Types.DeepMutable<PromptInput.Prompt>;
    metadata?: Record<string, unknown>;
    delivery: SessionInbox.Delivery;
}
/** Request overrides. Typed keys are generation settings; any other key is a provider option. */
export type SessionRequestOptions = Types.DeepMutable<GenerationOptionsFields> & Record<string, unknown>;
export interface SessionRequest {
    readonly sessionID: Session.ID;
    readonly model: Model.Ref;
    system: Array<SystemPart>;
    messages: Array<Message>;
    options: SessionRequestOptions;
}
export interface SessionContext extends SessionRequest {
    readonly agent: Agent.ID;
    tools: Record<string, {
        description: string;
        input: JsonSchema.JsonSchema;
    }>;
}
export interface SessionCompactionResult {
    summary: string;
    providerState?: SessionMessage.ProviderState;
    metadata?: Record<string, unknown>;
    tokens?: TokenUsage.Info;
}
export interface SessionCompaction extends SessionContext {
    /** Set to use this compaction and skip the model request. */
    result?: SessionCompactionResult;
}
export interface SessionGenerate extends SessionContext {
}
export interface SessionTitle extends SessionRequest {
    /** Set to use this title and skip the model request. */
    result?: string;
}
/**
 * Why a Session request is being made. Auxiliary requests share the Session's
 * hook identity but need to be told apart from the agent loop.
 */
export type SessionRequestKind = "primary" | "compaction" | "title" | "generate";
export interface SessionModelRequest {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    baseURL?: string;
    headers: Record<string, string>;
}
export interface SessionHttpRequest {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    request: Request;
}
export interface SessionHttpResponse {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    readonly request: Request;
    response: Response;
}
/**
 * Connection a WebSocket-backed request opens or reuses. Runs once per model call before the
 * Session's socket is selected; changing `url` or `headers` reopens the socket. Experimental.
 */
export interface SessionWebSocketHandshake {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    url: string;
    headers: Record<string, string>;
}
/**
 * Outbound frame about to be written to the Session's socket, after the provider driver has built
 * it. Replacing `frame` sends the replacement verbatim; the driver still tracks state from the
 * provider's replies, so a rewrite that changes protocol meaning is on the plugin. Experimental.
 */
export interface SessionWebSocketSend {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    frame: string;
}
/**
 * Inbound frame read from the Session's socket, before the provider driver observes it. Replacing
 * `frame` hands the replacement to the driver verbatim. Experimental.
 */
export interface SessionWebSocketReceive {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    frame: string;
}
export type SessionRetryDecision = {
    retry: false;
} | {
    retry: true;
    delay: number;
};
export interface SessionRetry {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly error: SessionError.Error;
    readonly attempt: number;
    decision: SessionRetryDecision;
}
export interface SessionHooks {
    readonly prompt: SessionPrompt;
    readonly context: SessionContext;
    readonly compaction: SessionCompaction;
    readonly generate: SessionGenerate;
    readonly title: SessionTitle;
    readonly "model.request": SessionModelRequest;
    readonly "http.request": SessionHttpRequest;
    readonly "http.response": SessionHttpResponse;
    readonly "experimental.ws.handshake": SessionWebSocketHandshake;
    readonly "experimental.ws.send": SessionWebSocketSend;
    readonly "experimental.ws.receive": SessionWebSocketReceive;
    readonly retry: SessionRetry;
}
export type SessionDomain = Pick<SessionApi<unknown>, "create" | "get" | "remove" | "switchAgent" | "switchModel" | "prompt" | "generate" | "command" | "compact" | "synthetic" | "interrupt" | "update" | "move" | "wait" | "context"> & {
    readonly hook: ModelHooks<SessionHooks>;
};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/shell.d.ts
---
import type { Hooks } from "./registration.js";
export interface ShellCreateBefore {
    command: string;
    cwd: string;
    timeout: number;
    shell: string;
    env: Record<string, string | undefined>;
}
export interface ShellHooks {
    readonly "create.before": ShellCreateBefore;
}
export interface ShellDomain {
    readonly hook: Hooks<ShellHooks>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/skill.d.ts
---
import type { SkillApi } from "@opencode/client/effect/api";
import { Skill } from "@opencode/schema/skill";
import type { Effect, Types } from "effect";
import type { Transform } from "./registration.js";
export interface SkillEditor {
    list(): readonly Types.DeepMutable<Skill.Info>[];
    get(id: string): Types.DeepMutable<Skill.Info> | undefined;
    add(skill: Skill.Info): void;
    update(id: string, update: (skill: Types.DeepMutable<Skill.Info>) => void): void;
    remove(id: string): void;
}
export interface SkillDomain extends SkillApi<unknown> {
    readonly transform: Transform<SkillEditor>;
    readonly reload: () => Effect.Effect<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/storage.d.ts
---
import type { Effect, Schema } from "effect";
import type { StorageScanOptions, StorageScanResult } from "../storage.js";
export interface StorageDomain {
    readonly get: (key: string) => Effect.Effect<Schema.Json | undefined>;
    readonly set: (key: string, value: Schema.Json) => Effect.Effect<void>;
    readonly remove: (key: string) => Effect.Effect<void>;
    readonly scan: (options: StorageScanOptions) => Effect.Effect<StorageScanResult>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/tool.d.ts
---
import { Tool } from "@opencode/schema/tool";
import type { Agent } from "@opencode/schema/agent";
import type { Session } from "@opencode/schema/session";
import type { SessionMessage } from "@opencode/schema/session-message";
import type { Effect, Types } from "effect";
import type { Hooks, Transform } from "./registration.js";
export interface ToolEditor {
    list(): readonly (Tool.Info & {
        readonly id: string;
    })[];
    get(id: string): (Tool.Info & {
        readonly id: string;
    }) | undefined;
    namespace(namespace: Tool.Namespace): void;
    add<Input extends Tool.ValueSchema<any>, Output extends Tool.ValueSchema<any> | undefined>(tool: Tool.Info<Input, Output>): void;
    /** Updates an existing tool; missing IDs are ignored. */
    update(id: string, update: (tool: Types.Mutable<Tool.Info>) => void): void;
    remove(id: string): void;
}
export interface ToolHooks {
    readonly "execute.before": {
        tool: string;
        readonly sessionID: Session.ID;
        readonly agent: Agent.ID;
        readonly messageID: SessionMessage.ID;
        readonly id: Tool.CallID;
        input: unknown;
    };
    readonly "execute.after": {
        readonly tool: string;
        readonly sessionID: Session.ID;
        readonly agent: Agent.ID;
        readonly messageID: SessionMessage.ID;
        readonly id: Tool.CallID;
        readonly input: unknown;
    } & ({
        readonly status: "completed";
        result: Tool.Result;
    } | {
        readonly status: "error";
        error: Tool.Error;
    });
}
export interface ToolFailures extends Record<keyof ToolHooks, unknown> {
    readonly "execute.before": Tool.Error;
    readonly "execute.after": never;
}
export interface ToolDomain {
    readonly transform: Transform<ToolEditor>;
    readonly reload: () => Effect.Effect<void>;
    /** Currently registered tools, after every transform, keyed by effective name. */
    readonly list: () => Effect.Effect<readonly (Tool.Info & {
        readonly id: string;
    })[]>;
    readonly hook: Hooks<ToolHooks, ToolFailures>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/vcs.d.ts
---
import type { VcsApi } from "@opencode/client/effect/api";
import type { FileDiff } from "@opencode/schema/file-diff";
import type { Vcs } from "@opencode/schema/vcs";
import type { Effect } from "effect";
import type { Transform } from "./registration.js";
export interface VcsScope {
    readonly directory: string;
    readonly worktree: string;
    readonly canonical: string;
    readonly store?: string;
}
export interface VcsBranchesInput extends VcsScope {
    readonly search?: string;
    readonly limit?: number;
}
export interface VcsDiffInput extends VcsScope {
    readonly mode: Vcs.Mode;
    readonly base?: string;
    readonly context: number;
    readonly maxOutputBytes: number;
}
export interface VcsDefinition {
    readonly id: string;
    readonly name: string;
    readonly init?: (input: VcsScope) => Effect.Effect<void, unknown>;
    readonly info: (input: VcsScope) => Effect.Effect<Vcs.Info, unknown>;
    readonly base?: (input: VcsScope) => Effect.Effect<Vcs.Base | null, unknown>;
    readonly branches: (input: VcsBranchesInput) => Effect.Effect<Vcs.BranchList, unknown>;
    readonly status: (input: VcsScope) => Effect.Effect<readonly Vcs.FileStatus[], unknown>;
    readonly diff: (input: VcsDiffInput) => Effect.Effect<readonly FileDiff.Info[], unknown>;
}
export interface VcsDomain extends Omit<VcsApi<unknown>, "init"> {
    readonly transform: Transform<VcsEditor>;
    readonly reload: () => Effect.Effect<void>;
}
export interface VcsEditor {
    add(definition: VcsDefinition): void;
    readonly default: {
        get(): string | undefined;
        set(selection: string): void;
    };
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/websearch.d.ts
---
import type { WebSearch } from "@opencode/schema/websearch";
import type { WebSearchApi } from "@opencode/client/effect/api";
import type { Effect } from "effect";
import type { Transform } from "./registration.js";
export interface WebSearchDefinition {
    readonly id: string;
    readonly name: string;
    readonly execute: (input: WebSearch.ProviderInput) => Effect.Effect<readonly WebSearch.Result[], unknown>;
}
export interface WebSearchDomain extends WebSearchApi<unknown> {
    readonly transform: Transform<WebSearchEditor>;
    readonly reload: () => Effect.Effect<void>;
}
export interface WebSearchEditor {
    add(definition: WebSearchDefinition): void;
    readonly default: {
        get(): string | false | undefined;
        set(selection: string | false): void;
    };
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/effect/worktree.d.ts
---
import type { WorktreeApi } from "@opencode/client/effect/api";
import type { Effect } from "effect";
import type { WorktreeCreateInput, WorktreeEntry, WorktreeRemoveInput, WorktreeResult } from "../worktree.js";
import type { Transform } from "./registration.js";
export interface WorktreeDefinition {
    readonly id: string;
    readonly create: (input: WorktreeCreateInput) => Effect.Effect<WorktreeResult, unknown>;
    readonly remove: (input: WorktreeRemoveInput) => Effect.Effect<void, unknown>;
    readonly list: (sourceDirectory: string) => Effect.Effect<readonly WorktreeEntry[], unknown>;
}
export interface WorktreeEditor {
    /** Registers an implementation and selects it as the default. Later active registrations win. */
    add(definition: WorktreeDefinition): void;
}
export interface WorktreeDomain extends WorktreeApi<unknown> {
    readonly transform: Transform<WorktreeEditor>;
    readonly reload: () => Effect.Effect<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/tui/context.d.ts
---
import type { AgentInfo, CommandInfo, SessionFormCancelInput, FormInfo, SessionFormReplyInput, IntegrationInfo, LocationRef, McpResource, McpServer, ModelInfo, OpenCodeClient, OpenCodeEvent, PermissionSavedInfo, PermissionRequest, Project, ProviderInfo, ReferenceInfo, SessionInfo, SessionMessageInfo, SessionInboxInfo, ShellInfo, SkillInfo, VcsInfo } from "@opencode/client";
import type { ResolvedTheme } from "@opencode/theme/tui";
import type { CliRenderer, KeyEvent, MarkdownCodeBlockRenderer, Renderable } from "@opentui/core";
import type { JSX } from "@opentui/solid";
import type { Store } from "solid-js/store";
export interface Storage {
    /**
     * Durable JSON state: persisted to disk, survives hot reloads and TUI
     * restarts, and stays live-synced across running TUI instances.
     */
    store<Value extends object>(key: string, options: {
        readonly initial: Value;
    }): readonly [Store<Value>, (mutation: (draft: Value) => void) => Promise<void>];
    /**
     * Ephemeral in-memory state: survives plugin hot reloads (old and new
     * generations share the same live store) and is gone when the TUI exits.
     * Updates are synchronous and values need not be JSON-serializable.
     */
    memory<Value extends object>(key: string, options: {
        readonly initial: Value;
    }): readonly [Store<Value>, (mutation: (draft: Value) => void) => void];
}
interface LocationCollection<Value> {
    list(location?: LocationRef): Value[] | undefined;
    sync(location?: LocationRef): Promise<void>;
    invalidate(location?: LocationRef): void;
}
type OpenCodeEventMap = {
    [Type in OpenCodeEvent["type"]]: Extract<OpenCodeEvent, {
        type: Type;
    }>;
};
export interface Data {
    readonly on: <Type extends OpenCodeEvent["type"]>(type: Type, handler: (event: OpenCodeEventMap[Type]) => void) => () => void;
    readonly listen: (handler: (event: {
        details: OpenCodeEvent;
    }) => void) => () => void;
    readonly session: {
        list(): SessionInfo[];
        get(sessionID: string): SessionInfo | undefined;
        root(sessionID: string): string;
        family(sessionID: string): string[];
        cost(sessionID: string): number;
        status(sessionID: string): "idle" | "running";
        readonly pending: {
            list(sessionID: string): SessionInboxInfo[];
            sync(sessionID: string): Promise<void>;
            invalidate(sessionID: string): void;
        };
        sync(sessionID: string): Promise<void>;
        invalidate(sessionID: string): void;
        readonly message: {
            list(sessionID: string): SessionMessageInfo[];
            get(sessionID: string, messageID: string): SessionMessageInfo | undefined;
            sync(sessionID: string): Promise<void>;
            invalidate(sessionID: string): void;
        };
        readonly permission: {
            list(sessionID: string): PermissionRequest[] | undefined;
            sync(sessionID: string): Promise<void>;
            invalidate(sessionID: string): void;
        };
        readonly form: {
            list(sessionID: string, location?: LocationRef): Array<FormInfo & {
                readonly location?: LocationRef;
            }> | undefined;
            sync(sessionID: string, location?: LocationRef): Promise<void>;
            invalidate(sessionID: string, location?: LocationRef): void;
            reply(input: SessionFormReplyInput, location?: LocationRef): Promise<void>;
            cancel(input: SessionFormCancelInput, location?: LocationRef): Promise<void>;
        };
    };
    readonly project: {
        list(): Project[];
        get(projectID: string): Project | undefined;
        sync(): Promise<void>;
        invalidate(): void;
        readonly permission: {
            list(projectID: string): PermissionSavedInfo[] | undefined;
            sync(projectID: string): Promise<void>;
            invalidate(projectID: string): void;
        };
    };
    readonly shell: {
        list(location?: LocationRef): ShellInfo[];
        get(id: string): ShellInfo | undefined;
        sync(location?: LocationRef): Promise<void>;
        invalidate(location?: LocationRef): void;
    };
    readonly location: {
        default(): LocationRef;
        sync(location?: LocationRef): Promise<void>;
        invalidate(location?: LocationRef): void;
        readonly vcs: {
            info(location?: LocationRef): VcsInfo | undefined;
            sync(location?: LocationRef): Promise<void>;
            invalidate(location?: LocationRef): void;
        };
        readonly agent: LocationCollection<AgentInfo>;
        readonly command: LocationCollection<CommandInfo>;
        readonly integration: LocationCollection<IntegrationInfo>;
        readonly mcp: {
            readonly server: LocationCollection<McpServer>;
            readonly resource: LocationCollection<McpResource>;
        };
        readonly model: LocationCollection<ModelInfo>;
        readonly provider: LocationCollection<ProviderInfo>;
        readonly reference: LocationCollection<ReferenceInfo>;
        readonly skill: LocationCollection<SkillInfo>;
    };
}
export type Route = {
    readonly type: "home";
} | {
    readonly type: "session";
    readonly sessionID: string;
} | {
    readonly type: "plugin";
    readonly id: string;
    readonly name: string;
    readonly data?: Record<string, any>;
};
export type Destination = Route | Omit<Extract<Route, {
    readonly type: "plugin";
}>, "id">;
export interface Page {
    readonly name: string;
    readonly render: (input: {
        readonly data?: Record<string, any>;
    }) => JSX.Element;
}
type PromptFooterInput = {
    readonly sessionID?: string;
    readonly mode: "normal" | "shell";
    readonly showDetails: boolean;
};
export type PanelPresentation = "panel" | "fullscreen";
/** Client-local state of the selected session panel. The host owns its layout and input scope. */
export interface PanelInput {
    /** Selected content name, set by ui.panel.open. Contributions decide whether to render it. */
    readonly name: string;
    readonly sessionID: string;
    readonly width: number;
    readonly presentation: PanelPresentation;
    readonly focused: boolean;
    readonly focus: () => void;
    readonly close: () => void;
    readonly toggleFullscreen: () => void;
}
/**
 * The host UI's slot tree. Every path is one slot: a named boundary a plugin
 * may render around, inside, or take over. Paths are absolute and
 * dot-separated, and a path contains every path it prefixes — replacing
 * `prompt.footer` owns everything under `prompt.footer.*`.
 *
 * Each slot publishes an input: reactive props passed to every claim render
 * targeting it. Inputs carry only what the SDK cannot answer — instance
 * identity and client-local state. Paths and their inputs are documented
 * API: coarse, few, and kept stable across host refactors.
 */
export interface SlotMap {
    readonly app: Readonly<Record<string, never>>;
    readonly "home.footer": Readonly<Record<string, never>>;
    readonly "home.footer.status": Readonly<Record<string, never>>;
    readonly "prompt.footer": PromptFooterInput;
    readonly "prompt.footer.status": PromptFooterInput;
    readonly "prompt.footer.file": PromptFooterInput;
    readonly "session.composer.top": {
        readonly sessionID: string;
    };
    readonly "session.panel": PanelInput;
    readonly "sidebar.content": {
        readonly sessionID: string;
    };
    readonly "sidebar.footer": {
        readonly sessionID: string;
    };
}
export type SlotPath = keyof SlotMap;
/**
 * One contribution to the slot tree. Exactly one placement key names an
 * absolute target path:
 * - `prepend` / `append`: first/last inside the target's boundary
 * - `before` / `after`: siblings adjacent to the target, outside its boundary
 * - `replace`: take over the target. The boundary itself survives — siblings
 *   anchored `before`/`after` it still compose — but the original content and
 *   every claim inside the boundary are suppressed and recorded, never
 *   silently dropped. At the same target the last-enabled claim wins; an
 *   ancestor replacement beats a descendant one regardless of enable order.
 *
 * A claim aimed at a path the host no longer publishes degrades: additive
 * claims append to the nearest surviving ancestor, replacements are
 * suppressed. Several claims at one anchor coexist in plugin enable order.
 *
 * `render` receives the target slot's input, reactively. The `?: never`
 * fields make the variants mutually exclusive: a claim with two placement
 * keys is a type error, not a silent priority pick.
 */
export type SlotClaim<Path extends SlotPath = SlotPath> = Path extends SlotPath ? {
    readonly render: (input: SlotMap[Path]) => JSX.Element;
} & ({
    readonly prepend: Path;
    readonly append?: never;
    readonly before?: never;
    readonly after?: never;
    readonly replace?: never;
} | {
    readonly append: Path;
    readonly prepend?: never;
    readonly before?: never;
    readonly after?: never;
    readonly replace?: never;
} | {
    readonly before: Path;
    readonly prepend?: never;
    readonly append?: never;
    readonly after?: never;
    readonly replace?: never;
} | {
    readonly after: Path;
    readonly prepend?: never;
    readonly append?: never;
    readonly before?: never;
    readonly replace?: never;
} | {
    readonly replace: Path;
    readonly prepend?: never;
    readonly append?: never;
    readonly before?: never;
    readonly after?: never;
}) : never;
export interface App {
    readonly version: string;
    readonly channel: string;
}
export type ToastVariant = "info" | "success" | "warning" | "error";
export interface ToastOptions {
    readonly title?: string;
    readonly message: string;
    readonly variant?: ToastVariant;
    readonly duration?: number;
    /** When this session's family is not open, the title defaults to the session title and the toast offers to open it. */
    readonly sessionID?: string;
}
export interface Toast {
    show(options: ToastOptions): void;
}
export type AttentionWhen = "always" | "focused" | "blurred";
export type AttentionSoundName = "default" | "question" | "permission" | "error" | "done" | "subagent_done";
export type AttentionNotification = boolean | {
    readonly when?: AttentionWhen;
};
export type AttentionSound = boolean | {
    readonly name?: AttentionSoundName;
    readonly volume?: number;
    readonly when?: AttentionWhen;
};
export interface AttentionNotifyOptions {
    readonly title?: string;
    readonly message: string;
    readonly notification?: AttentionNotification;
    readonly sound?: AttentionSound;
}
export type AttentionNotifySkipReason = "attention_disabled" | "empty_message" | "blurred" | "focused" | "focus_unknown" | "renderer_destroyed";
export interface AttentionNotifyResult {
    readonly ok: boolean;
    readonly notification: boolean;
    readonly sound: boolean;
    readonly skipped?: AttentionNotifySkipReason;
}
export interface Attention {
    notify(options: AttentionNotifyOptions): Promise<AttentionNotifyResult>;
}
export type DialogSize = "medium" | "large" | "xlarge";
export interface DialogOptions {
    readonly size?: DialogSize;
    readonly centered?: boolean;
}
export interface DialogAlertOptions {
    readonly title: string;
    readonly message: string;
}
export interface DialogConfirmOptions {
    readonly title: string;
    readonly message: string;
    readonly label?: {
        readonly confirm?: string;
        readonly cancel?: string;
    };
}
export interface DialogPromptOptions {
    readonly title: string;
    readonly description?: string;
    readonly placeholder?: string;
    readonly value?: string;
}
export interface DialogSelectOption<Value> {
    readonly title: string;
    readonly value: Value;
    readonly description?: string;
    readonly footer?: string;
    readonly category?: string;
    readonly disabled?: boolean;
}
export type DialogSelectAction<Value> = {
    readonly bind: string;
    readonly title: string;
    readonly side?: "left" | "right";
} & ({
    /** Unavailable while no option is selected. */
    readonly selection?: "required";
    readonly onTrigger: (value: Value) => void;
} | {
    readonly selection: "none";
    readonly onTrigger: () => void;
});
export interface DialogSelectOptions<Value> {
    readonly title: string;
    readonly placeholder?: string;
    readonly options: readonly DialogSelectOption<Value>[];
    readonly current?: Value;
    /** Returns matching options in display order, including for an empty query. Receives only enabled options. Results are listed flat. */
    readonly search?: (query: string, options: readonly DialogSelectOption<Value>[]) => readonly DialogSelectOption<Value>[];
    /** Footer actions, also triggered by their bindings. Triggering one does not close the dialog. */
    readonly actions?: readonly DialogSelectAction<Value>[];
}
export interface Dialog {
    /** Shows a dialog. */
    show(render: () => JSX.Element, onClose?: () => void): void;
    /** Sets the active dialog's presentation options. */
    set(options: DialogOptions): void;
    /** Closes the active dialog. */
    clear(): void;
    alert(options: DialogAlertOptions): Promise<void>;
    confirm(options: DialogConfirmOptions): Promise<boolean | undefined>;
    prompt(options: DialogPromptOptions): Promise<string | undefined>;
    select<Value>(options: DialogSelectOptions<Value>): Promise<Value | undefined>;
}
export interface KeymapCommand {
    /** Stable command and config keybind identifier. Omit for an inline command. */
    readonly id?: string;
    /** Optional label used by command discovery and keyboard-help UI. */
    readonly title?: string;
    /** Optional longer description. */
    readonly description?: string;
    /** Groups the command in discovery and keyboard-help UI. */
    readonly group?: string;
    /** Enables or disables the command. */
    readonly enabled?: boolean | (() => boolean);
    /** Configures automatic binding, or disables it for a named command. */
    readonly bind?: false | string;
    /** Adds a named command to the command palette. */
    readonly palette?: true;
    /** Adds a named command to prompt slash completion. */
    readonly slash?: {
        readonly name: string;
        readonly aliases?: string[];
        /** Keeps the slash command in the prompt and passes its raw input to run. */
        readonly arguments?: true;
    };
    /** Promotes the command in discovery UI. */
    readonly suggested?: boolean | (() => boolean);
    /** Executes the command. Keyboard dispatch includes its event; programmatic dispatch does not. Return false to continue. */
    readonly run: (input?: string, event?: KeyEvent) => void | false | Promise<void>;
}
export interface KeymapLayer {
    /** Limits the layer to one OpenCode input mode. Use global to opt out; defaults to base. */
    readonly mode?: string;
    /** Enables or disables the complete layer. */
    readonly enabled?: boolean | (() => boolean);
    /** Limits the layer to a focused renderable. */
    readonly target?: () => Renderable | null | undefined;
    /** Resolves conflicts with other active layers. */
    readonly priority?: number;
    /** Commands owned by this layer. */
    readonly commands?: readonly KeymapCommand[];
    /** IDs of commands whose configured bindings should be active in this layer. */
    readonly bindings?: readonly string[];
}
export interface KeymapPending {
    readonly key: string;
    readonly token?: string;
}
export interface KeymapActive {
    readonly key: string;
    readonly title?: string;
    readonly description?: string;
    readonly group?: string;
    readonly continues: boolean;
}
export interface Keymap {
    /**
     * Creates a reactive keymap layer. It is removed when the plugin deactivates, or earlier when the component or reactive
     * computation that created it is disposed. The factory must be pure. It runs once untracked for initial command-shape
     * validation, then reactively. Initial command-shape errors throw synchronously. OpenTUI handles binding-parse errors.
     * Errors from later reactive updates follow Solid's error handling.
     */
    layer(factory: () => KeymapLayer): void;
    /** Dispatches a reachable command by ID. */
    dispatch(id: string, input?: string): void;
    /** Returns every formatted shortcut for a registered command. */
    shortcuts(id: string): readonly string[];
    /** Returns the currently reachable commands. Reactive when read in a Solid computation. */
    commands(): readonly KeymapCommand[];
    /** Returns the pending key sequence. Reactive when read in a Solid computation. */
    pending(): readonly KeymapPending[];
    /** Returns bindings reachable from the pending key sequence. Reactive when read in a Solid computation. */
    active(): readonly KeymapActive[];
    /** Controls mutually exclusive OpenCode input modes. */
    readonly mode: {
        /** Returns the active mode. */
        current(): string;
        /** Pushes a mode until the returned cleanup is called. */
        push(mode: string): () => void;
    };
}
export interface UI {
    readonly dialog: Dialog;
    readonly toast: Toast;
    readonly format: {
        path(value: string): string;
    };
    readonly router: {
        register(page: Page): () => void;
        navigate(destination: Destination): void;
        current(): Route;
    };
    readonly panel: {
        /** Opens the session.panel slot in the current session. */
        open(name: string, options?: {
            readonly presentation?: PanelPresentation;
        }): boolean;
        /** Closes this plugin's active panel. Other plugins' panels are unaffected. */
        close(): void;
        /** This plugin's active panel, if any. Reactive when read in a Solid computation. */
        current(): {
            readonly name: string;
            readonly sessionID: string;
        } | undefined;
    };
    readonly tabs: {
        /** Returns whether session tabs are enabled for this TUI. */
        enabled(): boolean;
        /** Returns the currently open root-session tabs. Reactive when read in a Solid computation. */
        list(): readonly {
            readonly sessionID: string;
            readonly title?: string;
            readonly active: boolean;
            readonly busy: boolean;
            readonly attention: boolean;
            readonly unread?: "activity" | "error";
        }[];
        /** Opens a tab for a session without focusing it. Returns false when tabs are disabled. */
        open(sessionID: string): boolean;
        /** Opens a tab when needed, then focuses it. Returns false when tabs are disabled. */
        focus(sessionID: string): boolean;
        /** Moves an open tab to an index and returns false when it is not open. */
        move(sessionID: string, index: number): boolean;
        /** Closes an open tab, or the active tab when omitted, and returns false when no tab matched. */
        close(sessionID?: string): boolean;
    };
    readonly model: {
        /** The prompt's selected model; variant is undefined for the model default. Reactive when read in a Solid computation. */
        current(): {
            readonly providerID: string;
            readonly modelID: string;
            readonly variant?: string;
        } | undefined;
        readonly variant: {
            /** Variant IDs of the selected model. Reactive when read in a Solid computation. */
            list(): readonly string[];
            /** Selects a variant of the selected model, or the model default when undefined. Returns false when no model is selected or the variant is unavailable. */
            set(variant: string | undefined): boolean;
        };
    };
    /** Claims a place in the slot tree; see SlotClaim. */
    readonly slot: (claim: SlotClaim) => () => void;
}
export interface Context {
    readonly options: Readonly<Record<string, any>>;
    readonly location: LocationRef | undefined;
    readonly app: App;
    readonly renderer: CliRenderer;
    readonly client: OpenCodeClient;
    readonly data: Data;
    readonly attention: Attention;
    readonly theme: ResolvedTheme;
    readonly themeMode: "dark" | "light";
    readonly markdown: {
        registerCodeBlockRenderer(language: string, render: MarkdownCodeBlockRenderer): () => void;
    };
    readonly keymap: Keymap;
    readonly storage: Storage;
    readonly ui: UI;
}
export {};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/tui/index.d.ts
---
export * as Plugin from "./plugin.js";
export { PluginContextProvider, usePlugin } from "./solid.js";


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/tui/plugin.d.ts
---
import type { Context } from "./context.js";
export type { Context };
export type Cleanup = () => Promise<void> | void;
export interface Definition {
    readonly id: string;
    readonly setup: (context: Context) => Promise<Cleanup | void> | Cleanup | void;
}
export declare function define(plugin: Definition): Definition;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/tui/solid.d.ts
---
import { type JSX } from "solid-js";
import type { Context } from "./context.js";
export declare function PluginContextProvider(props: {
    readonly value: Context;
    readonly children: JSX.Element;
}): JSX.Element;
export declare function usePlugin(): Context;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/adapter.d.ts
---
import type { Scope } from "effect";
import type { Plugin } from "./plugin.js";
/**
 * Adapts a Promise plugin into an Effect plugin so the existing Effect-only
 * Core plugin runtime can run it unchanged.
 *
 * Hook registrations created during the async `setup` attach to the plugin's
 * scope, so unloading the plugin disposes them. The captured fiber context
 * preserves boot-time batching, so Promise-plugin transforms still coalesce
 * into one reload per domain.
 */
export declare function fromPromise(plugin: Plugin): import("../effect/plugin.js").Plugin<Scope.Scope>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/agent.d.ts
---
import type { AgentApi } from "@opencode/client/promise/api";
import type { Agent } from "@opencode/schema/agent";
import type { Transform } from "./registration.js";
import type { DeepMutable } from "./types.js";
export interface AgentEditor {
    list(): readonly DeepMutable<Agent.Info>[];
    get(id: string): DeepMutable<Agent.Info> | undefined;
    default(id: string | undefined): void;
    update(id: string, update: (agent: DeepMutable<Agent.Info>) => void): void;
    remove(id: string): void;
}
export interface AgentDomain extends AgentApi {
    readonly transform: Transform<AgentEditor>;
    readonly reload: () => Promise<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/aisdk.d.ts
---
import type { LanguageModelV3 } from "@ai-sdk/provider";
import type { Model } from "@opencode/schema/model";
import type { ModelHooks } from "./registration.js";
export interface AISDKHooks {
    sdk: {
        readonly model: Model.Info;
        readonly package: string;
        readonly options: Record<string, any>;
        sdk?: any;
    };
    language: {
        readonly model: Model.Info;
        readonly sdk: any;
        readonly options: Record<string, any>;
        language?: LanguageModelV3;
    };
}
export interface AISDKDomain {
    readonly hook: ModelHooks<AISDKHooks>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/command.d.ts
---
import type { CommandApi } from "@opencode/client/promise/api";
import type { PromptInput } from "@opencode/schema/prompt-input";
import type { Session } from "@opencode/schema/session";
import type { SessionInbox } from "@opencode/schema/session-inbox";
import type { Transform } from "./registration.js";
export interface CommandInvocation {
    readonly sessionID: Session.ID;
    readonly prompt: PromptInput.Prompt;
    readonly delivery: SessionInbox.Delivery;
}
export interface CommandDefinition {
    readonly name: string;
    readonly description?: string;
    readonly execute: (input: CommandInvocation) => Promise<void>;
}
export interface CommandEditor {
    add(definition: CommandDefinition): void;
}
export interface CommandDomain extends Pick<CommandApi, "list"> {
    readonly transform: Transform<CommandEditor>;
    readonly reload: () => Promise<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/event.d.ts
---
import type { EventApi } from "@opencode/client/promise/api";
export interface EventDomain extends Pick<EventApi, "subscribe"> {
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/index.d.ts
---
export type { PluginOptions } from "../options.js";
export type { StorageEntry, StorageScanOptions, StorageScanResult } from "../storage.js";
export * as Plugin from "./plugin.js";
export { Agent } from "@opencode/schema/agent";
export { Command } from "@opencode/schema/command";
export { Connection } from "@opencode/schema/connection";
export { Credential } from "@opencode/schema/credential";
export { Integration } from "@opencode/schema/integration";
export { Location } from "@opencode/schema/location";
export { Mcp } from "@opencode/schema/mcp";
export { Model } from "@opencode/schema/model";
export { PersistentPty } from "@opencode/schema/persistent-pty";
export { Provider } from "@opencode/schema/provider";
export { Reference } from "@opencode/schema/reference";
export { Rpc } from "@opencode/schema/rpc";
export { Skill } from "@opencode/schema/skill";
export { Vcs } from "@opencode/schema/vcs";
export { WebSearch } from "@opencode/schema/websearch";
export { Worktree } from "@opencode/schema/worktree";


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/integration.d.ts
---
import type { ConnectionInfo } from "@opencode/client";
import type { IntegrationApi } from "@opencode/client/promise/api";
import { Connection } from "@opencode/schema/connection";
import { Credential } from "@opencode/schema/credential";
import { Form } from "@opencode/schema/form";
import type { Transform } from "./registration.js";
type IntegrationRef = {
    id: string;
    name: string;
};
export interface IntegrationOAuthMethod {
    readonly id: string;
    readonly type: "oauth";
    readonly label: string;
    readonly form?: Form.Fields;
}
export interface IntegrationCommandMethod {
    readonly id: string;
    readonly type: "command";
    readonly label: string;
    readonly command: ReadonlyArray<string>;
}
export interface IntegrationKeyMethod {
    readonly type: "key";
    readonly label?: string;
    readonly form?: Form.Fields;
}
export interface IntegrationExternalMethod {
    readonly id: string;
    readonly type: "external";
    readonly label: string;
    readonly form?: Form.Fields;
}
export interface IntegrationEnvMethod {
    readonly type: "env";
    readonly names: ReadonlyArray<string>;
}
export type IntegrationMethod = IntegrationOAuthMethod | IntegrationCommandMethod | IntegrationKeyMethod | IntegrationExternalMethod | IntegrationEnvMethod;
export type IntegrationOAuthAuthorization = {
    readonly url: string;
    readonly instructions: string;
    readonly expiresAt?: number;
} & ({
    readonly mode: "auto";
    readonly callback: Promise<Credential.OAuth>;
} | {
    readonly mode: "code";
    readonly callback: (code: string) => Promise<Credential.OAuth>;
});
export type IntegrationOAuthMethodRegistration = {
    readonly integrationID: string;
    readonly method: IntegrationOAuthMethod;
    readonly authorize: (answer: Form.Answer) => Promise<IntegrationOAuthAuthorization>;
    readonly refresh?: (credential: Credential.OAuth) => Promise<Credential.OAuth>;
    readonly label?: (credential: Credential.OAuth) => string | undefined;
};
export type IntegrationMethodRegistration = IntegrationOAuthMethodRegistration | {
    readonly integrationID: string;
    readonly method: IntegrationCommandMethod;
} | {
    readonly integrationID: string;
    readonly method: IntegrationKeyMethod;
} | {
    readonly integrationID: string;
    readonly method: IntegrationExternalMethod;
} | {
    readonly integrationID: string;
    readonly method: IntegrationEnvMethod;
};
export interface IntegrationEditor {
    list(): readonly IntegrationRef[];
    get(id: string): IntegrationRef | undefined;
    update(id: string, update: (integration: IntegrationRef) => void): void;
    remove(id: string): void;
    readonly method: {
        list(integrationID: string): readonly IntegrationMethod[];
        update(input: IntegrationMethodRegistration): void;
        remove(integrationID: string, method: IntegrationMethod): void;
    };
}
export interface IntegrationDomain extends Omit<IntegrationApi, "wellknown"> {
    readonly transform: Transform<IntegrationEditor>;
    readonly reload: () => Promise<void>;
    readonly connection: {
        readonly active: (integrationID: string) => Promise<ConnectionInfo | undefined>;
        readonly resolve: (connection: ConnectionInfo) => Promise<Credential.Value | undefined>;
        /** Reports a problem with a connection, such as a required sign-in; `undefined` clears it. */
        readonly status: (input: {
            readonly integrationID: string;
            readonly connection: ConnectionInfo;
            readonly status: Connection.Status | undefined;
        }) => Promise<void>;
    };
}
export {};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/mcp.d.ts
---
import type { McpApi } from "@opencode/client/promise/api";
import type { Mcp } from "@opencode/schema/mcp";
import type { Transform } from "./registration.js";
import type { DeepMutable } from "./types.js";
export interface MCPEditor {
    list(): readonly [string, DeepMutable<Mcp.ServerConfig>][];
    get(name: string): DeepMutable<Mcp.ServerConfig> | undefined;
    set(name: string, config: Mcp.ServerConfig): void;
    update(name: string, update: (config: DeepMutable<Mcp.ServerConfig>) => void): void;
    remove(name: string): void;
}
export interface MCPDomain extends Pick<McpApi, "list"> {
    readonly transform: Transform<MCPEditor>;
    readonly reload: () => Promise<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/model.d.ts
---
import type { ModelApi } from "@opencode/client/promise/api";
import type { Model } from "@opencode/schema/model";
import type { ProviderRecord } from "./provider.js";
import type { Transform } from "./registration.js";
import type { DeepMutable } from "./types.js";
export interface ModelEditor {
    /** Candidates from available providers, including models disabled by earlier transforms. */
    list(providerID?: string): readonly DeepMutable<Model.Info>[];
    get(providerID: string, modelID: string): DeepMutable<Model.Info> | undefined;
    /** Edits raw model overrides; cannot create an unavailable provider. */
    update(providerID: string, modelID: string, update: (model: DeepMutable<Model.Info>) => void): void;
    remove(providerID: string, modelID: string): void;
    readonly default: {
        get(): {
            providerID: string;
            modelID: string;
        } | undefined;
        set(providerID: string, modelID: string): void;
    };
    /** Immutable provider inputs, including inactive templates, before model transforms. */
    readonly provider: {
        list(): readonly ProviderRecord[];
        get(providerID: string): ProviderRecord | undefined;
    };
}
export interface ModelDomain extends ModelApi {
    readonly transform: Transform<ModelEditor>;
    readonly reload: () => Promise<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/permission.d.ts
---
import type { PermissionApi } from "@opencode/client/promise/api";
import type { Agent } from "@opencode/schema/agent";
import type { Permission } from "@opencode/schema/permission";
import type { Session } from "@opencode/schema/session";
import type { AssertInput } from "../permission.js";
import type { Hooks } from "./registration.js";
export interface PermissionEvaluation {
    readonly sessionID: Session.ID;
    readonly agent?: Agent.ID;
    readonly action: string;
    readonly resources: ReadonlyArray<string>;
    readonly metadata?: Record<string, unknown>;
    readonly source?: Permission.Source;
    effect: Permission.Effect;
    message?: string;
}
export interface PermissionHooks {
    readonly evaluate: PermissionEvaluation;
}
export type PermissionDomain = Pick<PermissionApi, "list" | "get" | "reply"> & {
    readonly hook: Hooks<PermissionHooks>;
    /**
     * Evaluates the session's permission rules and, when approval is required, prompts the user and
     * waits for the reply. Resolves when allowed. A deny rule or a rejection with feedback rejects with
     * a `Tool.Error` carrying the reason. A plain rejection rejects with an error that ends the current
     * step; rethrow it rather than returning it as tool output.
     */
    readonly assert: (input: typeof AssertInput.Encoded) => Promise<void>;
};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/plugin.d.ts
---
import type { OpenCodeClient } from "@opencode/client";
import type { CredentialApi, GenerateApi, PluginApi } from "@opencode/client/promise/api";
import type { Location } from "@opencode/schema/location";
import type { PluginOptions } from "../options.js";
import type { App } from "../app.js";
import type { AgentDomain } from "./agent.js";
import type { AISDKDomain } from "./aisdk.js";
import type { CommandDomain } from "./command.js";
import type { EventDomain } from "./event.js";
import type { IntegrationDomain } from "./integration.js";
import type { MCPDomain } from "./mcp.js";
import type { ModelDomain } from "./model.js";
import type { PermissionDomain } from "./permission.js";
import type { ProviderDomain } from "./provider.js";
import type { ReferenceDomain } from "./reference.js";
import type { RpcDomain } from "./rpc.js";
import type { SessionDomain } from "./session.js";
import type { ShellDomain } from "./shell.js";
import type { SkillDomain } from "./skill.js";
import type { StorageDomain } from "./storage.js";
import type { ToolDomain } from "./tool.js";
import type { VcsDomain } from "./vcs.js";
import type { WebSearchDomain } from "./websearch.js";
import type { WorktreeDomain } from "./worktree.js";
export interface Context {
    readonly app: App;
    readonly location: Location.Info;
    readonly options: PluginOptions;
    readonly agent: AgentDomain;
    readonly aisdk: AISDKDomain;
    readonly command: CommandDomain;
    readonly credential: Pick<CredentialApi, "activate">;
    readonly event: EventDomain;
    readonly experimental: {
        readonly terminal: Pick<OpenCodeClient["experimental"]["persistentPty"], "read">;
    };
    readonly integration: IntegrationDomain;
    readonly mcp: MCPDomain;
    readonly model: ModelDomain;
    readonly generate: GenerateApi;
    readonly permission: PermissionDomain;
    readonly plugin: Pick<PluginApi, "list">;
    readonly provider: ProviderDomain;
    readonly reference: ReferenceDomain;
    readonly rpc: RpcDomain;
    readonly session: SessionDomain;
    readonly shell: ShellDomain;
    readonly skill: SkillDomain;
    readonly storage: StorageDomain;
    readonly tool: ToolDomain;
    readonly vcs: VcsDomain;
    readonly websearch: WebSearchDomain;
    readonly worktree: WorktreeDomain;
}
export type Cleanup = () => Promise<void> | void;
export interface Plugin {
    readonly id: string;
    readonly setup: (context: Context) => Promise<Cleanup | void> | Cleanup | void;
}
export declare function define(plugin: Plugin): Plugin;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/provider.d.ts
---
import type { ProviderApi } from "@opencode/client/promise/api";
import type { Model } from "@opencode/schema/model";
import type { Provider } from "@opencode/schema/provider";
import type { ConnectionInfo } from "@opencode/client";
import type { Transform } from "./registration.js";
import type { DeepMutable } from "./types.js";
/** Provider metadata and immutable source definitions, including inactive providers. */
export interface ProviderRecord {
    readonly provider: Provider.Info;
    readonly models: ReadonlyMap<string, Model.Info>;
    readonly sourceConnection?: ConnectionInfo;
}
export interface ProviderEditor {
    list(): readonly ProviderRecord[];
    get(providerID: string): ProviderRecord | undefined;
    /** A discovered account-specific inventory can be bound to the connection that produced it. */
    add(input: {
        info: Provider.Info;
        models: readonly Model.Info[];
        sourceConnection?: ConnectionInfo;
    }): void;
    update(providerID: string, update: (provider: DeepMutable<Provider.Info>) => void): void;
    remove(providerID: string): void;
    readonly models: {
        set(providerID: string, models: readonly Model.Info[]): void;
        /** Updates an owned copy of a source definition. */
        update(providerID: string, modelID: string, update: (model: DeepMutable<Model.Info>) => void): void;
        remove(providerID: string, modelID: string): void;
    };
}
export interface ProviderDomain extends ProviderApi {
    readonly transform: Transform<ProviderEditor>;
    readonly reload: () => Promise<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/reference.d.ts
---
import type { ReferenceApi } from "@opencode/client/promise/api";
import type { ReferenceGitSource, ReferenceLocalSource } from "@opencode/client";
import type { Transform } from "./registration.js";
export interface ReferenceEditor {
    add(name: string, source: ReferenceLocalSource | ReferenceGitSource): void;
    remove(name: string): void;
    list(): readonly (readonly [string, ReferenceLocalSource | ReferenceGitSource])[];
    get(name: string): ReferenceLocalSource | ReferenceGitSource | undefined;
}
export interface ReferenceDomain extends ReferenceApi {
    readonly transform: Transform<ReferenceEditor>;
    readonly reload: () => Promise<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/registration.d.ts
---
export interface Registration {
    readonly dispose: () => Promise<void>;
}
export interface ModelHookOptions {
    /** Limits the hook to one provider. Unscoped hooks apply to every provider. */
    readonly providerID?: string;
}
export type Hooks<Spec> = <Name extends keyof Spec>(name: Name, callback: (input: Spec[Name]) => Promise<void> | void) => Promise<Registration>;
export type ModelHooks<Spec> = <Name extends keyof Spec>(name: Name, callback: (input: Spec[Name]) => Promise<void> | void, options?: Spec[Name] extends {
    readonly model: unknown;
} ? ModelHookOptions : never) => Promise<Registration>;
export type Transform<Input> = (callback: (input: Input) => void) => Promise<Registration>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/rpc.d.ts
---
import type { RpcApi, RpcCallOptions } from "@opencode/client/promise/api";
import type { Rpc } from "@opencode/schema/rpc";
import type { Registration } from "./registration.js";
export type { RpcEventPayload } from "@opencode/client/promise/api";
export interface RpcCallContext<M extends Rpc.Method> {
    readonly signal: AbortSignal;
    readonly error: Rpc.ErrorFactory<M>;
}
export type RpcHandlers<D extends Rpc.PortableDefinition> = {
    readonly [Name in keyof D["methods"]]: (input: Rpc.Output<D["methods"][Name]["input"]>, context: RpcCallContext<D["methods"][Name]>) => Promise<Rpc.HandlerOutput<D["methods"][Name]["output"]> | Rpc.HandlerError<D["methods"][Name]>>;
};
export interface RpcRegistration<D extends Rpc.PortableDefinition> extends Registration {
    readonly events: {
        readonly emit: (...args: Rpc.EventInput<D>) => Promise<void>;
    };
}
export interface RpcDomain extends RpcApi<Pick<RpcCallOptions, "signal"> & {
    readonly location?: never;
    readonly headers?: never;
}> {
    readonly register: <const D extends Rpc.PortableDefinition>(definition: D, handlers: RpcHandlers<NoInfer<D>>) => Promise<RpcRegistration<D>>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/session.d.ts
---
import type { SessionApi } from "@opencode/client/promise/api";
import type { GenerationOptionsFields, Message, SystemPart } from "@opencode/ai";
import type { Agent } from "@opencode/schema/agent";
import type { Model } from "@opencode/schema/model";
import type { PromptInput } from "@opencode/schema/prompt-input";
import type { Session } from "@opencode/schema/session";
import type { SessionInbox } from "@opencode/schema/session-inbox";
import type { SessionError } from "@opencode/schema/session-error";
import type { SessionMessage } from "@opencode/schema/session-message";
import type { TokenUsage } from "@opencode/schema/token-usage";
import type { JsonSchema, Types } from "effect";
import type { ModelHooks } from "./registration.js";
export interface SessionPrompt {
    readonly sessionID: Session.ID;
    readonly messageID: SessionMessage.ID;
    prompt: Types.DeepMutable<PromptInput.Prompt>;
    metadata?: Record<string, unknown>;
    delivery: SessionInbox.Delivery;
}
/** Request overrides. Typed keys are generation settings; any other key is a provider option. */
export type SessionRequestOptions = Types.DeepMutable<GenerationOptionsFields> & Record<string, unknown>;
export interface SessionRequest {
    readonly sessionID: Session.ID;
    readonly model: Model.Ref;
    system: Array<SystemPart>;
    messages: Array<Message>;
    options: SessionRequestOptions;
}
export interface SessionContext extends SessionRequest {
    readonly agent: Agent.ID;
    tools: Record<string, {
        description: string;
        input: JsonSchema.JsonSchema;
    }>;
}
export interface SessionCompactionResult {
    summary: string;
    providerState?: SessionMessage.ProviderState;
    metadata?: Record<string, unknown>;
    tokens?: TokenUsage.Info;
}
export interface SessionCompaction extends SessionContext {
    /** Set to use this compaction and skip the model request. */
    result?: SessionCompactionResult;
}
export interface SessionGenerate extends SessionContext {
}
export interface SessionTitle extends SessionRequest {
    /** Set to use this title and skip the model request. */
    result?: string;
}
/**
 * Why a Session request is being made. Auxiliary requests share the Session's
 * hook identity but need to be told apart from the agent loop.
 */
export type SessionRequestKind = "primary" | "compaction" | "title" | "generate";
export interface SessionModelRequest {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    baseURL?: string;
    headers: Record<string, string>;
}
export interface SessionHttpRequest {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    request: Request;
}
export interface SessionHttpResponse {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    readonly request: Request;
    response: Response;
}
/**
 * Connection a WebSocket-backed request opens or reuses. Runs once per model call before the
 * Session's socket is selected; changing `url` or `headers` reopens the socket. Experimental.
 */
export interface SessionWebSocketHandshake {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    url: string;
    headers: Record<string, string>;
}
/**
 * Outbound frame about to be written to the Session's socket, after the provider driver has built
 * it. Replacing `frame` sends the replacement verbatim; the driver still tracks state from the
 * provider's replies, so a rewrite that changes protocol meaning is on the plugin. Experimental.
 */
export interface SessionWebSocketSend {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    frame: string;
}
/**
 * Inbound frame read from the Session's socket, before the provider driver observes it. Replacing
 * `frame` hands the replacement to the driver verbatim. Experimental.
 */
export interface SessionWebSocketReceive {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly kind: SessionRequestKind;
    frame: string;
}
export type SessionRetryDecision = {
    retry: false;
} | {
    retry: true;
    delay: number;
};
export interface SessionRetry {
    readonly sessionID: Session.ID;
    readonly agent: Agent.ID;
    readonly model: Model.Ref;
    readonly error: SessionError.Error;
    readonly attempt: number;
    decision: SessionRetryDecision;
}
export interface SessionHooks {
    readonly prompt: SessionPrompt;
    readonly context: SessionContext;
    readonly compaction: SessionCompaction;
    readonly generate: SessionGenerate;
    readonly title: SessionTitle;
    readonly "model.request": SessionModelRequest;
    readonly "http.request": SessionHttpRequest;
    readonly "http.response": SessionHttpResponse;
    readonly "experimental.ws.handshake": SessionWebSocketHandshake;
    readonly "experimental.ws.send": SessionWebSocketSend;
    readonly "experimental.ws.receive": SessionWebSocketReceive;
    readonly retry: SessionRetry;
}
export type SessionDomain = Pick<SessionApi, "create" | "get" | "remove" | "switchAgent" | "switchModel" | "prompt" | "generate" | "command" | "compact" | "synthetic" | "interrupt" | "update" | "move" | "wait" | "context"> & {
    readonly hook: ModelHooks<SessionHooks>;
};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/shell.d.ts
---
import type { Hooks } from "./registration.js";
export interface ShellCreateBefore {
    command: string;
    cwd: string;
    timeout: number;
    shell: string;
    env: Record<string, string | undefined>;
}
export interface ShellHooks {
    readonly "create.before": ShellCreateBefore;
}
export interface ShellDomain {
    readonly hook: Hooks<ShellHooks>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/skill.d.ts
---
import type { SkillApi } from "@opencode/client/promise/api";
import type { Skill } from "@opencode/schema/skill";
import type { Transform } from "./registration.js";
import type { DeepMutable } from "./types.js";
export interface SkillEditor {
    list(): readonly DeepMutable<Skill.Info>[];
    get(id: string): DeepMutable<Skill.Info> | undefined;
    add(skill: Skill.Info): void;
    update(id: string, update: (skill: DeepMutable<Skill.Info>) => void): void;
    remove(id: string): void;
}
export interface SkillDomain extends SkillApi {
    readonly transform: Transform<SkillEditor>;
    readonly reload: () => Promise<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/storage.d.ts
---
import type { Schema } from "effect";
import type { StorageScanOptions, StorageScanResult } from "../storage.js";
export interface StorageDomain {
    readonly get: (key: string) => Promise<Schema.Json | undefined>;
    readonly set: (key: string, value: Schema.Json) => Promise<void>;
    readonly remove: (key: string) => Promise<void>;
    readonly scan: (options: StorageScanOptions) => Promise<StorageScanResult>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/tool.d.ts
---
export { CallID, Error } from "@opencode/schema/tool";
export type { Metadata, Options, Result } from "@opencode/schema/tool";
import { Tool } from "@opencode/schema/tool";
import type { Agent } from "@opencode/schema/agent";
import type { Session } from "@opencode/schema/session";
import type { SessionMessage } from "@opencode/schema/session-message";
import type { Types } from "effect";
import type { Hooks, Transform } from "./registration.js";
export interface ToolContext extends Omit<Tool.Context, "progress"> {
    readonly signal: AbortSignal;
    readonly progress: (update: Tool.Metadata) => Promise<void>;
}
export type Info<Input extends Tool.ValueSchema<any> = Tool.ValueSchema<any>, Output extends Tool.ValueSchema<any> | undefined = Tool.ValueSchema<any> | undefined> = Omit<Tool.Info<Input, Output>, "execute"> & {
    readonly execute: (input: Parameters<Tool.Info<Input, Output>["execute"]>[0], context: ToolContext) => Promise<Tool.Result<Output>>;
};
export interface ToolEditor {
    list(): readonly (Info & {
        readonly id: string;
    })[];
    get(id: string): (Info & {
        readonly id: string;
    }) | undefined;
    namespace(namespace: Tool.Namespace): void;
    add<Input extends Tool.ValueSchema<any>, Output extends Tool.ValueSchema<any> | undefined>(tool: Info<Input, Output>): void;
    /** Updates an existing tool; missing IDs are ignored. */
    update(id: string, update: (tool: Types.Mutable<Info>) => void): void;
    remove(id: string): void;
}
interface ToolHooks {
    readonly "execute.before": {
        tool: string;
        readonly sessionID: Session.ID;
        readonly agent: Agent.ID;
        readonly messageID: SessionMessage.ID;
        readonly id: Tool.CallID;
        input: unknown;
    };
    readonly "execute.after": {
        readonly tool: string;
        readonly sessionID: Session.ID;
        readonly agent: Agent.ID;
        readonly messageID: SessionMessage.ID;
        readonly id: Tool.CallID;
        readonly input: unknown;
    } & ({
        readonly status: "completed";
        result: Tool.Result;
    } | {
        readonly status: "error";
        error: Tool.Error;
    });
}
export interface ToolDomain {
    readonly transform: Transform<ToolEditor>;
    readonly reload: () => Promise<void>;
    /** Currently registered tools, after every transform, keyed by effective name. */
    readonly list: () => Promise<readonly (Info & {
        readonly id: string;
    })[]>;
    readonly hook: Hooks<ToolHooks>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/types.d.ts
---
export type DeepMutable<A> = A extends (...args: never[]) => unknown ? A : A extends ReadonlyMap<infer K, infer V> ? Map<DeepMutable<K>, DeepMutable<V>> : A extends ReadonlyArray<infer I> ? DeepMutable<I>[] : A extends object ? {
    -readonly [K in keyof A]: DeepMutable<A[K]>;
} : A;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/vcs.d.ts
---
import type { VcsApi } from "@opencode/client/promise/api";
import type { FileDiff } from "@opencode/schema/file-diff";
import type { Vcs } from "@opencode/schema/vcs";
import type { Transform } from "./registration.js";
export interface VcsScope {
    readonly directory: string;
    readonly worktree: string;
    readonly canonical: string;
    readonly store?: string;
}
export interface VcsBranchesInput extends VcsScope {
    readonly search?: string;
    readonly limit?: number;
}
export interface VcsDiffInput extends VcsScope {
    readonly mode: Vcs.Mode;
    readonly base?: string;
    readonly context: number;
    readonly maxOutputBytes: number;
}
export interface VcsDefinition {
    readonly id: string;
    readonly name: string;
    readonly init?: (input: VcsScope, context: {
        readonly signal: AbortSignal;
    }) => Promise<void>;
    readonly info: (input: VcsScope, context: {
        readonly signal: AbortSignal;
    }) => Promise<Vcs.Info>;
    readonly base?: (input: VcsScope, context: {
        readonly signal: AbortSignal;
    }) => Promise<Vcs.Base | null>;
    readonly branches: (input: VcsBranchesInput, context: {
        readonly signal: AbortSignal;
    }) => Promise<Vcs.BranchList>;
    readonly status: (input: VcsScope, context: {
        readonly signal: AbortSignal;
    }) => Promise<readonly Vcs.FileStatus[]>;
    readonly diff: (input: VcsDiffInput, context: {
        readonly signal: AbortSignal;
    }) => Promise<readonly FileDiff.Info[]>;
}
export interface VcsDomain extends Omit<VcsApi, "init"> {
    readonly transform: Transform<VcsEditor>;
    readonly reload: () => Promise<void>;
}
export interface VcsEditor {
    add(definition: VcsDefinition): void;
    readonly default: {
        get(): string | undefined;
        set(selection: string): void;
    };
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/websearch.d.ts
---
import type { WebSearch } from "@opencode/schema/websearch";
import type { WebSearchApi } from "@opencode/client/promise/api";
import type { Transform } from "./registration.js";
export interface WebSearchDefinition {
    readonly id: string;
    readonly name: string;
    readonly execute: (input: WebSearch.ProviderInput, context: {
        readonly signal: AbortSignal;
    }) => Promise<readonly WebSearch.Result[]>;
}
export interface WebSearchDomain extends WebSearchApi {
    readonly transform: Transform<WebSearchEditor>;
    readonly reload: () => Promise<void>;
}
export interface WebSearchEditor {
    add(definition: WebSearchDefinition): void;
    readonly default: {
        get(): string | false | undefined;
        set(selection: string | false): void;
    };
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/node_modules/@opencode/plugin/dist/promise/worktree.d.ts
---
import type { WorktreeApi } from "@opencode/client/promise/api";
import type { WorktreeCreateInput, WorktreeEntry, WorktreeRemoveInput, WorktreeResult } from "../worktree.js";
import type { Transform } from "./registration.js";
export interface WorktreeDefinition {
    readonly id: string;
    readonly create: (input: WorktreeCreateInput, context: {
        readonly signal: AbortSignal;
    }) => Promise<WorktreeResult>;
    readonly remove: (input: WorktreeRemoveInput, context: {
        readonly signal: AbortSignal;
    }) => Promise<void>;
    readonly list: (sourceDirectory: string, context: {
        readonly signal: AbortSignal;
    }) => Promise<readonly WorktreeEntry[]>;
}
export interface WorktreeEditor {
    /** Registers an implementation and selects it as the default. Later active registrations win. */
    add(definition: WorktreeDefinition): void;
}
export interface WorktreeDomain extends WorktreeApi {
    readonly transform: Transform<WorktreeEditor>;
    readonly reload: () => Promise<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/package.json
---
{
  "$schema": "https://json.schemastore.org/package.json",
  "name": "@opencode-ai/plugin",
  "version": "1.18.35",
  "type": "module",
  "license": "MIT",
  "scripts": {
    "typecheck": "tsgo --noEmit",
    "build": "tsc"
  },
  "exports": {
    ".": {
      "import": "./dist/index.js",
      "types": "./dist/index.d.ts"
    },
    "./tool": {
      "import": "./dist/tool.js",
      "types": "./dist/tool.d.ts"
    },
    "./tui": {
      "import": "./dist/tui.js",
      "types": "./dist/tui.d.ts"
    },
    "./v2/effect": {
      "import": "./dist/v2/effect/index.js",
      "types": "./dist/v2/effect/index.d.ts"
    },
    "./v2/effect/integration": {
      "import": "./dist/v2/effect/integration.js",
      "types": "./dist/v2/effect/integration.d.ts"
    },
    "./v2/effect/plugin": {
      "import": "./dist/v2/effect/plugin.js",
      "types": "./dist/v2/effect/plugin.d.ts"
    },
    "./v2/promise": {
      "import": "./dist/v2/promise/index.js",
      "types": "./dist/v2/promise/index.d.ts"
    }
  },
  "files": [
    "dist"
  ],
  "dependencies": {
    "@ai-sdk/provider": "3.0.8",
    "@opencode-ai/sdk": "1.18.35",
    "effect": "4.0.0-beta.83",
    "zod": "4.1.8"
  },
  "peerDependencies": {
    "@opentui/core": ">=0.4.5",
    "@opentui/keymap": ">=0.4.5",
    "@opentui/solid": ">=0.4.5"
  },
  "peerDependenciesMeta": {
    "@opentui/core": {
      "optional": true
    },
    "@opentui/keymap": {
      "optional": true
    },
    "@opentui/solid": {
      "optional": true
    }
  },
  "devDependencies": {
    "@opentui/core": "0.4.5",
    "@opentui/keymap": "0.4.5",
    "@opentui/solid": "0.4.5",
    "@tsconfig/node22": "22.0.2",
    "@types/node": "24.12.2",
    "typescript": "5.8.2",
    "@typescript/native-preview": "7.0.0-dev.20251207.1"
  }
}

---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/example-workspace.d.ts
---
import type { Plugin } from "@opencode-ai/plugin";
export declare const FolderWorkspacePlugin: Plugin;
export default FolderWorkspacePlugin;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/example.d.ts
---
import { Plugin } from "./index.js";
export declare const ExamplePlugin: Plugin;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/index.d.ts
---
import type { Event, createOpencodeClient, Project, Model, Provider, Permission, UserMessage, Message, Part, Config as SDKConfig } from "@opencode-ai/sdk";
import type { Provider as ProviderV2, Model as ModelV2, Auth } from "@opencode-ai/sdk/v2";
import type { BunShell } from "./shell.js";
import { type ToolDefinition } from "./tool.js";
export * from "./tool.js";
export type ProviderContext = {
    source: "env" | "config" | "custom" | "api";
    info: Provider;
    options: Record<string, any>;
};
export type WorkspaceInfo = {
    id: string;
    type: string;
    name: string;
    branch: string | null;
    directory: string | null;
    extra: unknown | null;
    projectID: string;
};
export type WorkspaceTarget = {
    type: "local";
    directory: string;
} | {
    type: "remote";
    url: string | URL;
    headers?: HeadersInit;
};
export type WorkspaceAdapter = {
    name: string;
    description: string;
    configure(config: WorkspaceInfo): WorkspaceInfo | Promise<WorkspaceInfo>;
    create(config: WorkspaceInfo, env: Record<string, string | undefined>, from?: WorkspaceInfo): Promise<void>;
    remove(config: WorkspaceInfo): Promise<void>;
    target(config: WorkspaceInfo): WorkspaceTarget | Promise<WorkspaceTarget>;
};
export type PluginInput = {
    client: ReturnType<typeof createOpencodeClient>;
    project: Project;
    directory: string;
    worktree: string;
    experimental_workspace: {
        register(type: string, adapter: WorkspaceAdapter): void;
    };
    serverUrl: URL;
    $: BunShell;
};
export type PluginOptions = Record<string, unknown>;
export type Config = Omit<SDKConfig, "plugin"> & {
    plugin?: Array<string | [string, PluginOptions]>;
};
export type Plugin = (input: PluginInput, options?: PluginOptions) => Promise<Hooks>;
export type PluginModule = {
    id?: string;
    server: Plugin;
    tui?: never;
};
type Rule = {
    key: string;
    op: "eq" | "neq";
    value: string;
};
export type AuthHook = {
    provider: string;
    loader?: (auth: () => Promise<Auth>, provider: Provider) => Promise<Record<string, any>>;
    methods: ({
        type: "oauth";
        label: string;
        prompts?: Array<{
            type: "text";
            key: string;
            message: string;
            placeholder?: string;
            validate?: (value: string) => string | undefined;
            /** @deprecated Use `when` instead */
            condition?: (inputs: Record<string, string>) => boolean;
            when?: Rule;
        } | {
            type: "select";
            key: string;
            message: string;
            options: Array<{
                label: string;
                value: string;
                hint?: string;
            }>;
            /** @deprecated Use `when` instead */
            condition?: (inputs: Record<string, string>) => boolean;
            when?: Rule;
        }>;
        authorize(inputs?: Record<string, string>): Promise<AuthOAuthResult>;
    } | {
        type: "api";
        label: string;
        prompts?: Array<{
            type: "text";
            key: string;
            message: string;
            placeholder?: string;
            validate?: (value: string) => string | undefined;
            /** @deprecated Use `when` instead */
            condition?: (inputs: Record<string, string>) => boolean;
            when?: Rule;
        } | {
            type: "select";
            key: string;
            message: string;
            options: Array<{
                label: string;
                value: string;
                hint?: string;
            }>;
            /** @deprecated Use `when` instead */
            condition?: (inputs: Record<string, string>) => boolean;
            when?: Rule;
        }>;
        authorize?(inputs?: Record<string, string>): Promise<{
            type: "success";
            key: string;
            provider?: string;
            metadata?: Record<string, string>;
        } | {
            type: "failed";
        }>;
    })[];
};
export type AuthOAuthResult = {
    url: string;
    instructions: string;
} & ({
    method: "auto";
    callback(): Promise<({
        type: "success";
        provider?: string;
    } & ({
        refresh: string;
        access: string;
        expires: number;
        accountId?: string;
        enterpriseUrl?: string;
    } | {
        key: string;
        metadata?: Record<string, string>;
    })) | {
        type: "failed";
    }>;
} | {
    method: "code";
    callback(code: string): Promise<({
        type: "success";
        provider?: string;
    } & ({
        refresh: string;
        access: string;
        expires: number;
        accountId?: string;
        enterpriseUrl?: string;
    } | {
        key: string;
        metadata?: Record<string, string>;
    })) | {
        type: "failed";
    }>;
});
export type ProviderHookContext = {
    auth?: Auth;
};
export type ProviderHook = {
    id: string;
    models?: (provider: ProviderV2, ctx: ProviderHookContext) => Promise<Record<string, ModelV2>>;
};
/** @deprecated Use AuthOAuthResult instead. */
export type AuthOuathResult = AuthOAuthResult;
export interface Hooks {
    dispose?: () => Promise<void>;
    event?: (input: {
        event: Event;
    }) => Promise<void>;
    config?: (input: Config) => Promise<void>;
    tool?: {
        [key: string]: ToolDefinition;
    };
    auth?: AuthHook;
    provider?: ProviderHook;
    /**
     * Called when a new message is received
     */
    "chat.message"?: (input: {
        sessionID: string;
        agent?: string;
        model?: {
            providerID: string;
            modelID: string;
        };
        messageID?: string;
        variant?: string;
    }, output: {
        message: UserMessage;
        parts: Part[];
    }) => Promise<void>;
    /**
     * Modify parameters sent to LLM
     */
    "chat.params"?: (input: {
        sessionID: string;
        agent: string;
        model: Model;
        provider: ProviderContext;
        message: UserMessage;
    }, output: {
        temperature: number;
        topP: number;
        topK: number;
        maxOutputTokens: number | undefined;
        options: Record<string, any>;
    }) => Promise<void>;
    "chat.headers"?: (input: {
        sessionID: string;
        agent: string;
        model: Model;
        provider: ProviderContext;
        message: UserMessage;
    }, output: {
        headers: Record<string, string>;
    }) => Promise<void>;
    "permission.ask"?: (input: Permission, output: {
        status: "ask" | "deny" | "allow";
    }) => Promise<void>;
    "command.execute.before"?: (input: {
        command: string;
        sessionID: string;
        arguments: string;
    }, output: {
        parts: Part[];
    }) => Promise<void>;
    "tool.execute.before"?: (input: {
        tool: string;
        sessionID: string;
        callID: string;
    }, output: {
        args: any;
    }) => Promise<void>;
    "shell.env"?: (input: {
        cwd: string;
        sessionID?: string;
        callID?: string;
    }, output: {
        env: Record<string, string>;
    }) => Promise<void>;
    "tool.execute.after"?: (input: {
        tool: string;
        sessionID: string;
        callID: string;
        args: any;
    }, output: {
        title: string;
        output: string;
        metadata: any;
    }) => Promise<void>;
    "experimental.chat.messages.transform"?: (input: {}, output: {
        messages: {
            info: Message;
            parts: Part[];
        }[];
    }) => Promise<void>;
    "experimental.chat.system.transform"?: (input: {
        sessionID?: string;
        model: Model;
    }, output: {
        system: string[];
    }) => Promise<void>;
    "experimental.provider.small_model"?: (input: {
        provider: ProviderV2;
    }, output: {
        model?: ModelV2;
    }) => Promise<void>;
    /**
     * Called before session compaction starts. Allows plugins to customize
     * the compaction prompt.
     *
     * - `context`: Additional context strings appended to the default prompt
     * - `prompt`: If set, replaces the default compaction prompt entirely
     */
    "experimental.session.compacting"?: (input: {
        sessionID: string;
    }, output: {
        context: string[];
        prompt?: string;
    }) => Promise<void>;
    /**
     * Called after compaction succeeds and before a synthetic user
     * auto-continue message is added.
     *
     * - `enabled`: Defaults to `true`. Set to `false` to skip the synthetic
     *   user "continue" turn.
     */
    "experimental.compaction.autocontinue"?: (input: {
        sessionID: string;
        agent: string;
        model: Model;
        provider: ProviderContext;
        message: UserMessage;
        overflow: boolean;
    }, output: {
        enabled: boolean;
    }) => Promise<void>;
    "experimental.text.complete"?: (input: {
        sessionID: string;
        messageID: string;
        partID: string;
    }, output: {
        text: string;
    }) => Promise<void>;
    /**
     * Modify tool definitions (description and parameters) sent to LLM
     */
    "tool.definition"?: (input: {
        toolID: string;
    }, output: {
        description: string;
        parameters: any;
    }) => Promise<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/shell.d.ts
---
export type ShellFunction = (input: Uint8Array) => Uint8Array;
export type ShellExpression = {
    toString(): string;
} | Array<ShellExpression> | string | {
    raw: string;
} | ReadableStream;
export interface BunShell {
    (strings: TemplateStringsArray, ...expressions: ShellExpression[]): BunShellPromise;
    /**
     * Perform bash-like brace expansion on the given pattern.
     * @param pattern - Brace pattern to expand
     */
    braces(pattern: string): string[];
    /**
     * Escape strings for input into shell commands.
     */
    escape(input: string): string;
    /**
     * Change the default environment variables for shells created by this instance.
     */
    env(newEnv?: Record<string, string | undefined>): BunShell;
    /**
     * Default working directory to use for shells created by this instance.
     */
    cwd(newCwd?: string): BunShell;
    /**
     * Configure the shell to not throw an exception on non-zero exit codes.
     */
    nothrow(): BunShell;
    /**
     * Configure whether or not the shell should throw an exception on non-zero exit codes.
     */
    throws(shouldThrow: boolean): BunShell;
}
export interface BunShellPromise extends Promise<BunShellOutput> {
    readonly stdin: WritableStream;
    /**
     * Change the current working directory of the shell.
     */
    cwd(newCwd: string): this;
    /**
     * Set environment variables for the shell.
     */
    env(newEnv: Record<string, string> | undefined): this;
    /**
     * By default, the shell will write to the current process's stdout and stderr, as well as buffering that output.
     * This configures the shell to only buffer the output.
     */
    quiet(): this;
    /**
     * Read from stdout as a string, line by line
     * Automatically calls quiet() to disable echoing to stdout.
     */
    lines(): AsyncIterable<string>;
    /**
     * Read from stdout as a string.
     * Automatically calls quiet() to disable echoing to stdout.
     */
    text(encoding?: BufferEncoding): Promise<string>;
    /**
     * Read from stdout as a JSON object
     * Automatically calls quiet()
     */
    json(): Promise<any>;
    /**
     * Read from stdout as an ArrayBuffer
     * Automatically calls quiet()
     */
    arrayBuffer(): Promise<ArrayBuffer>;
    /**
     * Read from stdout as a Blob
     * Automatically calls quiet()
     */
    blob(): Promise<Blob>;
    /**
     * Configure the shell to not throw an exception on non-zero exit codes.
     */
    nothrow(): this;
    /**
     * Configure whether or not the shell should throw an exception on non-zero exit codes.
     */
    throws(shouldThrow: boolean): this;
}
export interface BunShellOutput {
    readonly stdout: Buffer;
    readonly stderr: Buffer;
    readonly exitCode: number;
    /**
     * Read from stdout as a string
     */
    text(encoding?: BufferEncoding): string;
    /**
     * Read from stdout as a JSON object
     */
    json(): any;
    /**
     * Read from stdout as an ArrayBuffer
     */
    arrayBuffer(): ArrayBuffer;
    /**
     * Read from stdout as an Uint8Array
     */
    bytes(): Uint8Array;
    /**
     * Read from stdout as a Blob
     */
    blob(): Blob;
}
export type BunShellError = Error & BunShellOutput;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/tool.d.ts
---
import { z } from "zod";
export type ToolContext = {
    sessionID: string;
    messageID: string;
    agent: string;
    /**
     * Current project directory for this session.
     * Prefer this over process.cwd() when resolving relative paths.
     */
    directory: string;
    /**
     * Project worktree root for this session.
     * Useful for generating stable relative paths (e.g. path.relative(worktree, absPath)).
     */
    worktree: string;
    abort: AbortSignal;
    metadata(input: {
        title?: string;
        metadata?: {
            [key: string]: any;
        };
    }): void;
    ask(input: AskInput): Promise<void>;
};
type AskInput = {
    permission: string;
    patterns: string[];
    always: string[];
    metadata: {
        [key: string]: any;
    };
};
export type ToolAttachment = {
    type: "file";
    mime: string;
    url: string;
    filename?: string;
};
export type ToolResult = string | {
    title?: string;
    output: string;
    metadata?: {
        [key: string]: any;
    };
    attachments?: ToolAttachment[];
};
export declare function tool<Args extends z.ZodRawShape>(input: {
    description: string;
    args: Args;
    execute(args: z.infer<z.ZodObject<Args>>, context: ToolContext): Promise<ToolResult>;
}): {
    description: string;
    args: Args;
    execute(args: z.infer<z.ZodObject<Args>>, context: ToolContext): Promise<ToolResult>;
};
export declare namespace tool {
    var schema: typeof z;
}
export type ToolDefinition = ReturnType<typeof tool>;
export {};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/tui.d.ts
---
import type { AgentPart, OpencodeClient, Event, FilePart, LspStatus, McpStatus, Todo, Message, Part, Provider, PermissionRequest, QuestionRequest, Session, SessionStatus, TextPart, Config as SdkConfig } from "@opencode-ai/sdk/v2";
import type { CliRenderer, KeyEvent, RGBA, Renderable, SlotMode } from "@opentui/core";
import type { Binding, Keymap } from "@opentui/keymap";
import { type BindingConfig, type CreateBindingLookupOptions, type KeySequenceFormatPart, type SequenceBindingLike } from "@opentui/keymap/extras";
import type { JSX, SolidPlugin } from "@opentui/solid";
import type { Config as PluginConfig, PluginOptions } from "./index.js";
export type { CliRenderer, KeyEvent, Renderable, SlotMode } from "@opentui/core";
export { stringifyKeySequence, stringifyKeyStroke } from "@opentui/keymap";
export type { Binding, KeyLike, KeySequencePart, KeyStringifyInput, StringifyOptions } from "@opentui/keymap";
export { formatCommandBindings, formatKeySequence } from "@opentui/keymap/extras";
export type { BindingConfig, BindingLookup, BindingValue, CreateBindingLookupOptions, FormatCommandBindingsOptions, FormatKeySequenceOptions, KeySequenceFormatPart, SequenceBindingLike, } from "@opentui/keymap/extras";
export declare function createBindingLookup(config: BindingConfig<Renderable, KeyEvent> | undefined, options?: CreateBindingLookupOptions<Renderable, KeyEvent>): import("@opentui/keymap/extras").BindingLookup<Renderable, KeyEvent>;
export type TuiRouteCurrent = {
    name: "home";
} | {
    name: "session";
    params: {
        sessionID: string;
        prompt?: unknown;
    };
} | {
    name: string;
    params?: Record<string, unknown>;
};
export type TuiRouteDefinition = {
    name: string;
    render: (input: {
        params?: Record<string, unknown>;
    }) => JSX.Element;
};
export type TuiKeys = {
    formatSequence: (parts: readonly KeySequenceFormatPart[] | undefined) => string;
    formatBindings: (bindings: readonly SequenceBindingLike[] | undefined) => string | undefined;
};
export type TuiKeymap = Keymap<Renderable, KeyEvent>;
export type TuiModeApi = {
    current: () => string;
    push: (mode: string) => () => void;
};
/**
 * Legacy `api.command` shape kept so v1 plugins can initialize. Remove in v2.
 *
 * @deprecated Use `api.keymap.registerLayer({ commands, bindings })` instead.
 */
export type TuiCommand = {
    title: string;
    value: string;
    description?: string;
    category?: string;
    keybind?: string;
    suggested?: boolean;
    hidden?: boolean;
    enabled?: boolean;
    slash?: {
        name: string;
        aliases?: string[];
    };
    onSelect?: (dialog?: TuiDialogStack) => void | Promise<void>;
};
/**
 * Legacy `api.command` API kept so v1 plugins can initialize. Remove in v2.
 *
 * @deprecated Use `api.keymap.registerLayer`, `api.keymap.dispatchCommand`, and
 * `api.keymap.dispatchCommand("command.palette.show")` instead.
 */
export type TuiCommandApi = {
    /** @deprecated Use `api.keymap.registerLayer({ commands, bindings })` instead. */
    register: (cb: () => TuiCommand[]) => () => void;
    /** @deprecated Use `api.keymap.dispatchCommand(name)` instead. */
    trigger: (value: string) => void;
    /** @deprecated Use `api.keymap.dispatchCommand("command.palette.show")` instead. */
    show: () => void;
};
export type TuiDialogProps = {
    size?: "medium" | "large" | "xlarge";
    onClose: () => void;
    children?: JSX.Element;
};
export type TuiDialogStack = {
    replace: (render: () => JSX.Element, onClose?: () => void) => void;
    clear: () => void;
    setSize: (size: "medium" | "large" | "xlarge") => void;
    readonly size: "medium" | "large" | "xlarge";
    readonly depth: number;
    readonly open: boolean;
};
export type TuiDialogAlertProps = {
    title: string;
    message: string;
    onConfirm?: () => void;
};
export type TuiDialogConfirmProps = {
    title: string;
    message: string;
    onConfirm?: () => void;
    onCancel?: () => void;
};
export type TuiDialogPromptProps = {
    title: string;
    description?: () => JSX.Element;
    placeholder?: string;
    value?: string;
    busy?: boolean;
    busyText?: string;
    onConfirm?: (value: string) => void;
    onCancel?: () => void;
};
export type TuiDialogSelectOption<Value = unknown> = {
    title: string;
    value: Value;
    description?: string;
    footer?: JSX.Element | string;
    category?: string;
    disabled?: boolean;
    onSelect?: () => void;
};
export type TuiDialogSelectProps<Value = unknown> = {
    title: string;
    placeholder?: string;
    options: TuiDialogSelectOption<Value>[];
    flat?: boolean;
    onMove?: (option: TuiDialogSelectOption<Value>) => void;
    onFilter?: (query: string) => void;
    onSelect?: (option: TuiDialogSelectOption<Value>) => void;
    skipFilter?: boolean;
    current?: Value;
};
export type TuiPromptInfo = {
    input: string;
    mode?: "normal" | "shell";
    parts: (Omit<FilePart, "id" | "messageID" | "sessionID"> | Omit<AgentPart, "id" | "messageID" | "sessionID"> | (Omit<TextPart, "id" | "messageID" | "sessionID"> & {
        source?: {
            text: {
                start: number;
                end: number;
                value: string;
            };
        };
    }))[];
};
export type TuiPromptRef = {
    focused: boolean;
    current: TuiPromptInfo;
    set(prompt: TuiPromptInfo): void;
    reset(): void;
    blur(): void;
    focus(): void;
    submit(): void;
};
export type TuiPromptProps = {
    sessionID?: string;
    visible?: boolean;
    disabled?: boolean;
    onSubmit?: () => void;
    ref?: (ref: TuiPromptRef | undefined) => void;
    hint?: JSX.Element;
    right?: JSX.Element;
    showPlaceholder?: boolean;
    placeholders?: {
        normal?: string[];
        shell?: string[];
    };
};
export type TuiToast = {
    variant?: "info" | "success" | "warning" | "error";
    title?: string;
    message: string;
    duration?: number;
};
export type TuiAttentionWhen = "always" | "focused" | "blurred";
export declare const TuiAttentionSoundNames: readonly ["default", "question", "permission", "error", "done", "subagent_done"];
export type TuiAttentionSoundName = (typeof TuiAttentionSoundNames)[number];
export type TuiAttentionSound = boolean | {
    name?: TuiAttentionSoundName;
    volume?: number;
    when?: TuiAttentionWhen;
};
export type TuiAttentionNotification = boolean | {
    when?: TuiAttentionWhen;
};
export type TuiAttentionSoundPack = {
    id: string;
    name?: string;
    sounds: Partial<Record<TuiAttentionSoundName, string>>;
};
export type TuiAttentionSoundPackInfo = {
    id: string;
    name?: string;
    active: boolean;
    builtin: boolean;
};
export type TuiAttentionSoundboardActivateOptions = {
    persist?: boolean;
};
export type TuiAttentionSoundboard = {
    registerPack(pack: TuiAttentionSoundPack): () => void;
    activate(id: string, options?: TuiAttentionSoundboardActivateOptions): boolean;
    current(): string;
    list(): ReadonlyArray<TuiAttentionSoundPackInfo>;
};
export type TuiAttentionNotifyInput = {
    title?: string;
    message: string;
    notification?: TuiAttentionNotification;
    sound?: TuiAttentionSound;
};
export type TuiAttentionNotifySkipReason = "attention_disabled" | "empty_message" | "blurred" | "focused" | "focus_unknown" | "renderer_destroyed";
export type TuiAttentionNotifyResult = {
    ok: boolean;
    notification: boolean;
    sound: boolean;
    skipped?: TuiAttentionNotifySkipReason;
};
export type TuiAttention = {
    notify(input: TuiAttentionNotifyInput): Promise<TuiAttentionNotifyResult>;
    soundboard: TuiAttentionSoundboard;
};
export type TuiThemeCurrent = {
    readonly primary: RGBA;
    readonly secondary: RGBA;
    readonly accent: RGBA;
    readonly error: RGBA;
    readonly warning: RGBA;
    readonly success: RGBA;
    readonly info: RGBA;
    readonly text: RGBA;
    readonly textMuted: RGBA;
    readonly selectedListItemText: RGBA;
    readonly background: RGBA;
    readonly backgroundPanel: RGBA;
    readonly backgroundElement: RGBA;
    readonly backgroundMenu: RGBA;
    readonly border: RGBA;
    readonly borderActive: RGBA;
    readonly borderSubtle: RGBA;
    readonly diffAdded: RGBA;
    readonly diffRemoved: RGBA;
    readonly diffContext: RGBA;
    readonly diffHunkHeader: RGBA;
    readonly diffHighlightAdded: RGBA;
    readonly diffHighlightRemoved: RGBA;
    readonly diffAddedBg: RGBA;
    readonly diffRemovedBg: RGBA;
    readonly diffContextBg: RGBA;
    readonly diffLineNumber: RGBA;
    readonly diffAddedLineNumberBg: RGBA;
    readonly diffRemovedLineNumberBg: RGBA;
    readonly markdownText: RGBA;
    readonly markdownHeading: RGBA;
    readonly markdownLink: RGBA;
    readonly markdownLinkText: RGBA;
    readonly markdownCode: RGBA;
    readonly markdownBlockQuote: RGBA;
    readonly markdownEmph: RGBA;
    readonly markdownStrong: RGBA;
    readonly markdownHorizontalRule: RGBA;
    readonly markdownListItem: RGBA;
    readonly markdownListEnumeration: RGBA;
    readonly markdownImage: RGBA;
    readonly markdownImageText: RGBA;
    readonly markdownCodeBlock: RGBA;
    readonly syntaxComment: RGBA;
    readonly syntaxKeyword: RGBA;
    readonly syntaxFunction: RGBA;
    readonly syntaxVariable: RGBA;
    readonly syntaxString: RGBA;
    readonly syntaxNumber: RGBA;
    readonly syntaxType: RGBA;
    readonly syntaxOperator: RGBA;
    readonly syntaxPunctuation: RGBA;
    readonly thinkingOpacity: number;
};
export type TuiTheme = {
    readonly current: TuiThemeCurrent;
    readonly selected: string;
    has: (name: string) => boolean;
    set: (name: string) => boolean;
    install: (jsonPath: string) => Promise<void>;
    mode: () => "dark" | "light";
    readonly ready: boolean;
};
export type TuiKV = {
    get: <Value = unknown>(key: string, fallback?: Value) => Value;
    set: (key: string, value: unknown) => void;
    readonly ready: boolean;
};
export type TuiState = {
    readonly ready: boolean;
    readonly config: SdkConfig;
    readonly provider: ReadonlyArray<Provider>;
    readonly path: {
        state: string;
        config: string;
        worktree: string;
        directory: string;
    };
    readonly vcs: {
        branch?: string;
        default_branch?: string;
    } | undefined;
    session: {
        count: () => number;
        get: (sessionID: string) => Session | undefined;
        diff: (sessionID: string) => ReadonlyArray<TuiSidebarFileItem>;
        todo: (sessionID: string) => ReadonlyArray<TuiSidebarTodoItem>;
        messages: (sessionID: string) => ReadonlyArray<Message>;
        status: (sessionID: string) => SessionStatus | undefined;
        permission: (sessionID: string) => ReadonlyArray<PermissionRequest>;
        question: (sessionID: string) => ReadonlyArray<QuestionRequest>;
    };
    part: (messageID: string) => ReadonlyArray<Part>;
    lsp: () => ReadonlyArray<TuiSidebarLspItem>;
    mcp: () => ReadonlyArray<TuiSidebarMcpItem>;
};
type TuiBindingLookupView = {
    readonly bindings: ReadonlyArray<Binding<Renderable, KeyEvent>>;
    get: (command: string) => ReadonlyArray<Binding<Renderable, KeyEvent>>;
    has: (command: string) => boolean;
    gather: (name: string, commands: readonly string[]) => ReadonlyArray<Binding<Renderable, KeyEvent>>;
    pick: (name: string, commands: readonly string[]) => Binding<Renderable, KeyEvent>[];
    omit: (name: string, commands: readonly string[]) => Binding<Renderable, KeyEvent>[];
};
type TuiAttentionConfigView = {
    enabled: boolean;
    notifications: boolean;
    sound: boolean;
    volume: number;
    sound_pack: string;
    sounds: Partial<Record<TuiAttentionSoundName, string>>;
};
type TuiConfigView = Pick<PluginConfig, "$schema" | "theme" | "plugin"> & NonNullable<PluginConfig["tui"]> & {
    leader_timeout: number;
    attention: TuiAttentionConfigView;
    plugin_enabled?: Record<string, boolean>;
    keybinds: TuiBindingLookupView;
};
export type TuiApp = {
    readonly version: string;
};
type Frozen<Value> = Value extends (...args: never[]) => unknown ? Value : Value extends ReadonlyArray<infer Item> ? ReadonlyArray<Frozen<Item>> : Value extends object ? {
    readonly [Key in keyof Value]: Frozen<Value[Key]>;
} : Value;
export type TuiSidebarMcpItem = {
    name: string;
    status: McpStatus["status"];
    error?: string;
};
export type TuiSidebarLspItem = Pick<LspStatus, "id" | "root" | "status">;
export type TuiSidebarTodoItem = Pick<Todo, "content" | "status">;
export type TuiSidebarFileItem = {
    file: string;
    additions: number;
    deletions: number;
};
export type TuiHostSlotMap = {
    app: {};
    app_bottom: {};
    home_logo: {};
    home_prompt: {
        ref?: (ref: TuiPromptRef | undefined) => void;
    };
    home_prompt_right: {};
    session_prompt: {
        session_id: string;
        visible?: boolean;
        disabled?: boolean;
        on_submit?: () => void;
        ref?: (ref: TuiPromptRef | undefined) => void;
    };
    session_prompt_right: {
        session_id: string;
    };
    home_bottom: {};
    home_footer: {};
    sidebar_title: {
        session_id: string;
        title: string;
        share_url?: string;
    };
    sidebar_content: {
        session_id: string;
    };
    sidebar_footer: {
        session_id: string;
    };
};
export type TuiSlotMap<Slots extends Record<string, object> = {}> = TuiHostSlotMap & Slots;
type TuiSlotShape<Name extends string, Slots extends Record<string, object>> = Name extends keyof TuiHostSlotMap ? TuiHostSlotMap[Name] : Name extends keyof Slots ? Slots[Name] : Record<string, unknown>;
export type TuiSlotProps<Name extends string = string, Slots extends Record<string, object> = {}> = {
    name: Name;
    mode?: SlotMode;
    children?: JSX.Element;
} & TuiSlotShape<Name, Slots>;
export type TuiSlotContext = {
    theme: TuiTheme;
};
type SlotCore<Slots extends Record<string, object> = {}> = SolidPlugin<TuiSlotMap<Slots>, TuiSlotContext>;
export type TuiSlotPlugin<Slots extends Record<string, object> = {}> = Omit<SlotCore<Slots>, "id"> & {
    id?: never;
};
export type TuiSlots = {
    register: {
        (plugin: TuiSlotPlugin): string;
        <Slots extends Record<string, object>>(plugin: TuiSlotPlugin<Slots>): string;
    };
};
export type TuiEventBus = {
    on: <Type extends Event["type"]>(type: Type, handler: (event: Extract<Event, {
        type: Type;
    }>) => void) => () => void;
};
export type TuiDispose = () => void | Promise<void>;
export type TuiLifecycle = {
    readonly signal: AbortSignal;
    onDispose: (fn: TuiDispose) => () => void;
};
export type TuiPluginState = "first" | "updated" | "same";
export type TuiPluginEntry = {
    id: string;
    source: "file" | "npm" | "internal";
    spec: string;
    target: string;
    requested?: string;
    version?: string;
    modified?: number;
    first_time: number;
    last_time: number;
    time_changed: number;
    load_count: number;
    fingerprint: string;
};
export type TuiPluginMeta = TuiPluginEntry & {
    state: TuiPluginState;
};
export type TuiPluginStatus = {
    id: string;
    source: TuiPluginEntry["source"];
    spec: string;
    target: string;
    enabled: boolean;
    active: boolean;
};
export type TuiPluginInstallOptions = {
    global?: boolean;
};
export type TuiPluginInstallResult = {
    ok: true;
    dir: string;
    tui: boolean;
} | {
    ok: false;
    message: string;
    missing?: boolean;
};
export type TuiWorkspace = {
    current: () => string | undefined;
    set: (workspaceID?: string) => void;
};
export type TuiPluginApi = {
    app: TuiApp;
    attention: TuiAttention;
    /**
     * Legacy `api.command` API kept so v1 plugins can initialize. Remove in v2.
     *
     * @deprecated Use `api.keymap.registerLayer`, `api.keymap.dispatchCommand`, and
     * `api.keymap.dispatchCommand("command.palette.show")` instead.
     */
    command?: TuiCommandApi;
    keys: TuiKeys;
    keymap: TuiKeymap;
    mode: TuiModeApi;
    route: {
        register: (routes: TuiRouteDefinition[]) => () => void;
        navigate: (name: string, params?: Record<string, unknown>) => void;
        readonly current: TuiRouteCurrent;
    };
    ui: {
        Dialog: (props: TuiDialogProps) => JSX.Element;
        DialogAlert: (props: TuiDialogAlertProps) => JSX.Element;
        DialogConfirm: (props: TuiDialogConfirmProps) => JSX.Element;
        DialogPrompt: (props: TuiDialogPromptProps) => JSX.Element;
        DialogSelect: <Value = unknown>(props: TuiDialogSelectProps<Value>) => JSX.Element;
        Slot: <Name extends string>(props: TuiSlotProps<Name>) => JSX.Element | null;
        Prompt: (props: TuiPromptProps) => JSX.Element;
        toast: (input: TuiToast) => void;
        dialog: TuiDialogStack;
    };
    readonly tuiConfig: Frozen<TuiConfigView>;
    kv: TuiKV;
    state: TuiState;
    theme: TuiTheme;
    client: OpencodeClient;
    event: TuiEventBus;
    renderer: CliRenderer;
    slots: TuiSlots;
    plugins: {
        list: () => ReadonlyArray<TuiPluginStatus>;
        activate: (id: string) => Promise<boolean>;
        deactivate: (id: string) => Promise<boolean>;
        add: (spec: string) => Promise<boolean>;
        install: (spec: string, options?: TuiPluginInstallOptions) => Promise<TuiPluginInstallResult>;
    };
    lifecycle: TuiLifecycle;
};
export type TuiPlugin = (api: TuiPluginApi, options: PluginOptions | undefined, meta: TuiPluginMeta) => Promise<void>;
export type TuiPluginModule = {
    id?: string;
    tui: TuiPlugin;
    server?: never;
};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/options.d.ts
---
export type PluginOptions = Readonly<Record<string, any>>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/agent.d.ts
---
import type { AgentV2Info } from "@opencode-ai/sdk/v2/types";
import type { Hooks } from "./registration.js";
export interface AgentDraft {
    list(): readonly AgentV2Info[];
    get(id: string): AgentV2Info | undefined;
    default(id: string | undefined): void;
    update(id: string, update: (agent: AgentV2Info) => void): void;
    remove(id: string): void;
}
export type AgentHooks = Hooks<{
    transform: AgentDraft;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/aisdk.d.ts
---
import type { LanguageModelV3 } from "@ai-sdk/provider";
import type { ModelV2Info } from "@opencode-ai/sdk/v2/types";
import type { Hooks } from "./registration.js";
export type AISDKHooks = Hooks<{
    sdk: {
        readonly model: ModelV2Info;
        readonly package: string;
        readonly options: Record<string, any>;
        sdk?: any;
    };
    language: {
        readonly model: ModelV2Info;
        readonly sdk: any;
        readonly options: Record<string, any>;
        language?: LanguageModelV3;
    };
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/catalog.d.ts
---
import type { ModelV2Info, ProviderV2Info } from "@opencode-ai/sdk/v2/types";
import type { Hooks } from "./registration.js";
export interface CatalogProviderRecord {
    readonly provider: ProviderV2Info;
    readonly models: ReadonlyMap<string, ModelV2Info>;
}
export interface CatalogDraft {
    readonly provider: {
        list(): readonly CatalogProviderRecord[];
        get(providerID: string): CatalogProviderRecord | undefined;
        update(providerID: string, update: (provider: ProviderV2Info) => void): void;
        remove(providerID: string): void;
    };
    readonly model: {
        get(providerID: string, modelID: string): ModelV2Info | undefined;
        update(providerID: string, modelID: string, update: (model: ModelV2Info) => void): void;
        remove(providerID: string, modelID: string): void;
        readonly default: {
            get(): {
                providerID: string;
                modelID: string;
            } | undefined;
            set(providerID: string, modelID: string): void;
        };
    };
}
export type CatalogHooks = Hooks<{
    transform: CatalogDraft;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/command.d.ts
---
import type { CommandV2Info } from "@opencode-ai/sdk/v2/types";
import type { Hooks } from "./registration.js";
export interface CommandDraft {
    list(): readonly CommandV2Info[];
    get(name: string): CommandV2Info | undefined;
    update(name: string, update: (command: CommandV2Info) => void): void;
    remove(name: string): void;
}
export type CommandHooks = Hooks<{
    transform: CommandDraft;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/context.d.ts
---
import type { PluginOptions } from "../options.js";
import type { AgentHooks } from "./agent.js";
import type { AISDKHooks } from "./aisdk.js";
import type { CatalogHooks } from "./catalog.js";
import type { CommandHooks } from "./command.js";
import type { IntegrationHooks } from "./integration.js";
import type { PluginDomain } from "./plugin.js";
import type { ReferenceHooks } from "./reference.js";
import type { SkillHooks } from "./skill.js";
import type { Reload } from "./registration.js";
export interface PluginContext {
    readonly options: PluginOptions;
    readonly agent: AgentHooks & Reload;
    readonly aisdk: AISDKHooks;
    readonly catalog: CatalogHooks & Reload;
    readonly command: CommandHooks & Reload;
    readonly integration: IntegrationHooks & Reload;
    readonly plugin: PluginDomain;
    readonly reference: ReferenceHooks & Reload;
    readonly skill: SkillHooks & Reload;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/event.d.ts
---
import type { Event as SDKEvent } from "@opencode-ai/sdk/v2/types";
import type { Stream } from "effect";
export type EventMap = {
    [Item in SDKEvent as Item["type"]]: Item;
};
export interface Event {
    subscribe<Type extends keyof EventMap>(type: Type): Stream.Stream<EventMap[Type]>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/filesystem.d.ts
---
import type { FileSystemEntry } from "@opencode-ai/sdk/v2/types";
import type { Effect } from "effect";
export interface FileSystem {
    read(input: {
        readonly path: string;
    }): Effect.Effect<{
        readonly content: Uint8Array;
        readonly mime: string;
    }>;
    list(input?: {
        readonly path?: string;
    }): Effect.Effect<FileSystemEntry[]>;
    find(input: {
        readonly query: string;
        readonly type?: "file" | "directory";
        readonly limit?: number;
    }): Effect.Effect<FileSystemEntry[]>;
    glob(input: {
        readonly pattern: string;
        readonly path?: string;
        readonly limit?: number;
    }): Effect.Effect<readonly FileSystemEntry[]>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/index.d.ts
---
export type { PluginContext } from "./context.js";
export { define } from "./plugin.js";
export type { Plugin } from "./plugin.js";


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/integration.d.ts
---
import type { ConnectionInfo, CredentialOAuth, CredentialValue, IntegrationEnvMethod, IntegrationInputs, IntegrationKeyMethod, IntegrationMethod, IntegrationOAuthMethod, IntegrationRef } from "@opencode-ai/sdk/v2/types";
import type { Effect, Scope } from "effect";
import type { Hooks } from "./registration.js";
export type IntegrationOAuthAuthorization = {
    readonly url: string;
    readonly instructions: string;
} & ({
    readonly mode: "auto";
    readonly callback: Effect.Effect<CredentialOAuth, unknown>;
} | {
    readonly mode: "code";
    readonly callback: (code: string) => Effect.Effect<CredentialOAuth, unknown>;
});
export type IntegrationOAuthMethodRegistration = {
    readonly integrationID: string;
    readonly method: IntegrationOAuthMethod;
    readonly authorize: (inputs: IntegrationInputs) => Effect.Effect<IntegrationOAuthAuthorization, unknown, Scope.Scope>;
    readonly refresh?: (credential: CredentialOAuth) => Effect.Effect<CredentialOAuth, unknown>;
    readonly label?: (credential: CredentialOAuth) => string | undefined;
};
export type IntegrationMethodRegistration = IntegrationOAuthMethodRegistration | {
    readonly integrationID: string;
    readonly method: IntegrationKeyMethod;
} | {
    readonly integrationID: string;
    readonly method: IntegrationEnvMethod;
};
export interface IntegrationDraft {
    list(): readonly IntegrationRef[];
    get(id: string): IntegrationRef | undefined;
    update(id: string, update: (integration: IntegrationRef) => void): void;
    remove(id: string): void;
    readonly method: {
        list(integrationID: string): readonly IntegrationMethod[];
        update(input: IntegrationMethodRegistration): void;
        remove(integrationID: string, method: IntegrationMethod): void;
    };
}
export interface IntegrationHooks extends Hooks<{
    transform: IntegrationDraft;
}> {
    readonly connection: {
        readonly active: (integrationID: string) => Effect.Effect<ConnectionInfo | undefined>;
        readonly resolve: (connection: ConnectionInfo) => Effect.Effect<CredentialValue | undefined, unknown>;
    };
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/location.d.ts
---
export interface Location {
    readonly directory: string;
    readonly project: {
        readonly directory: string;
    };
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/npm.d.ts
---
import type { Effect } from "effect";
export interface Npm {
    add(pkg: string): Effect.Effect<{
        readonly directory: string;
        readonly entrypoint?: string;
    }, unknown>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/path.d.ts
---
export interface Path {
    readonly home: string;
    readonly data: string;
    readonly cache: string;
    readonly config: string;
    readonly state: string;
    readonly temp: string;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/plugin.d.ts
---
import type { Effect, Scope } from "effect";
import type { PluginContext } from "./context.js";
export interface Plugin<R = Scope.Scope> {
    readonly id: string;
    readonly effect: (context: PluginContext) => Effect.Effect<void, never, R>;
}
export declare function define<R = Scope.Scope>(plugin: Plugin<R>): Plugin<R>;
export interface PluginDomain {
    readonly add: (plugin: Plugin) => Effect.Effect<void>;
    readonly remove: (id: string) => Effect.Effect<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/reference.d.ts
---
import type { ReferenceGitSource, ReferenceLocalSource } from "@opencode-ai/sdk/v2/types";
import type { Hooks } from "./registration.js";
export interface ReferenceDraft {
    add(name: string, source: ReferenceLocalSource | ReferenceGitSource): void;
    remove(name: string): void;
    list(): readonly (readonly [string, ReferenceLocalSource | ReferenceGitSource])[];
}
export type ReferenceHooks = Hooks<{
    transform: ReferenceDraft;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/registration.d.ts
---
import type { Effect, Scope } from "effect";
export interface Registration {
    readonly dispose: Effect.Effect<void>;
}
export interface Reload {
    readonly reload: () => Effect.Effect<void>;
}
export type Hooks<Spec> = {
    readonly [Name in keyof Spec]: (callback: (input: Spec[Name]) => Effect.Effect<void> | void) => Effect.Effect<Registration, never, Scope.Scope>;
};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/effect/skill.d.ts
---
import type { SkillV2Source } from "@opencode-ai/sdk/v2/types";
import type { Hooks } from "./registration.js";
export interface SkillDraft {
    source(source: SkillV2Source): void;
    list(): readonly SkillV2Source[];
}
export type SkillHooks = Hooks<{
    transform: SkillDraft;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/agent.d.ts
---
import type { AgentDraft } from "../effect/agent.js";
import type { Hooks } from "./registration.js";
export type { AgentDraft };
export type AgentHooks = Hooks<{
    transform: AgentDraft;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/aisdk.d.ts
---
import type { LanguageModelV3 } from "@ai-sdk/provider";
import type { ModelV2Info } from "@opencode-ai/sdk/v2/types";
import type { Hooks } from "./registration.js";
export type AISDKHooks = Hooks<{
    sdk: {
        readonly model: ModelV2Info;
        readonly package: string;
        readonly options: Record<string, any>;
        sdk?: any;
    };
    language: {
        readonly model: ModelV2Info;
        readonly sdk: any;
        readonly options: Record<string, any>;
        language?: LanguageModelV3;
    };
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/catalog.d.ts
---
import type { CatalogDraft, CatalogProviderRecord } from "../effect/catalog.js";
import type { Hooks } from "./registration.js";
export type { CatalogDraft, CatalogProviderRecord };
export type CatalogHooks = Hooks<{
    transform: CatalogDraft;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/command.d.ts
---
import type { CommandDraft } from "../effect/command.js";
import type { Hooks } from "./registration.js";
export type { CommandDraft };
export type CommandHooks = Hooks<{
    transform: CommandDraft;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/context.d.ts
---
import type { PluginOptions } from "../options.js";
import type { AgentHooks } from "./agent.js";
import type { AISDKHooks } from "./aisdk.js";
import type { CatalogHooks } from "./catalog.js";
import type { CommandHooks } from "./command.js";
import type { IntegrationHooks } from "./integration.js";
import type { PluginDomain } from "./plugin.js";
import type { ReferenceHooks } from "./reference.js";
import type { SkillHooks } from "./skill.js";
import type { Reload } from "./registration.js";
export interface PluginContext {
    readonly options: PluginOptions;
    readonly agent: AgentHooks & Reload;
    readonly aisdk: AISDKHooks;
    readonly catalog: CatalogHooks & Reload;
    readonly command: CommandHooks & Reload;
    readonly integration: IntegrationHooks & Reload;
    readonly plugin: PluginDomain;
    readonly reference: ReferenceHooks & Reload;
    readonly skill: SkillHooks & Reload;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/index.d.ts
---
export type { PluginContext } from "./context.js";
export type { PluginOptions } from "../options.js";
export { define } from "./plugin.js";
export type { Plugin, PluginDomain } from "./plugin.js";
export type { Registration, Reload } from "./registration.js";
export type { AgentDraft, AgentHooks } from "./agent.js";
export type { AISDKHooks } from "./aisdk.js";
export type { CatalogDraft, CatalogHooks, CatalogProviderRecord } from "./catalog.js";
export type { CommandDraft, CommandHooks } from "./command.js";
export type { IntegrationDraft, IntegrationHooks, IntegrationMethodRegistration } from "./integration.js";
export type { ReferenceDraft, ReferenceHooks } from "./reference.js";
export type { SkillDraft, SkillHooks } from "./skill.js";


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/integration.d.ts
---
import type { IntegrationDraft, IntegrationMethodRegistration } from "../effect/integration.js";
import type { CredentialValue } from "@opencode-ai/sdk/v2/types";
import type { Hooks } from "./registration.js";
export type { IntegrationDraft, IntegrationMethodRegistration };
export interface IntegrationHooks extends Hooks<{
    transform: IntegrationDraft;
}> {
    readonly connection: {
        readonly active: (integrationID: string) => Promise<import("@opencode-ai/sdk/v2/types").ConnectionInfo | undefined>;
        readonly resolve: (connection: import("@opencode-ai/sdk/v2/types").ConnectionInfo) => Promise<CredentialValue | undefined>;
    };
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/plugin.d.ts
---
import type { PluginContext } from "./context.js";
export interface Plugin {
    readonly id: string;
    readonly setup: (context: PluginContext) => Promise<void> | void;
}
export declare function define(plugin: Plugin): Plugin;
export interface PluginDomain {
    readonly add: (plugin: Plugin) => Promise<void>;
    readonly remove: (id: string) => Promise<void>;
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/reference.d.ts
---
import type { ReferenceDraft } from "../effect/reference.js";
import type { Hooks } from "./registration.js";
export type { ReferenceDraft };
export type ReferenceHooks = Hooks<{
    transform: ReferenceDraft;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/registration.d.ts
---
export interface Registration {
    readonly dispose: () => Promise<void>;
}
export interface Reload {
    readonly reload: () => Promise<void>;
}
export type Hooks<Spec> = {
    readonly [Name in keyof Spec]: (callback: (input: Spec[Name]) => Promise<void> | void) => Promise<Registration>;
};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/node_modules/@opencode-ai/plugin/dist/v2/promise/skill.d.ts
---
import type { SkillDraft } from "../effect/skill.js";
import type { Hooks } from "./registration.js";
export type { SkillDraft };
export type SkillHooks = Hooks<{
    transform: SkillDraft;
}>;


---
/media/marcs/Linux_Apps/Projets_AI/.opencode/plugins/graphify.js
---
// graphify OpenCode plugin
// Injects a knowledge graph reminder before bash tool calls when the graph exists.
//
// IMPORTANT: keep the reminder string free of backticks and $(...) constructs.
// The hook prepends `echo "<reminder>" && <cmd>` to the user's bash command;
// backticks inside the double-quoted echo trigger bash command substitution,
// which both corrupts tool output and silently executes the very graphify
// command we are only suggesting. Plain words render fine in opencode's TUI.
import { existsSync } from "fs";
import { join } from "path";

export const GraphifyPlugin = async ({ directory }) => {
  let reminded = false;

  return {
    "tool.execute.before": async (input, output) => {
      if (reminded) return;

      const graphs = [];
      if (existsSync(join(directory, "graphify-out", "graph.json"))) {
        graphs.push("graphify-out/ (ZeroClaw)");
      }
      if (existsSync(join(directory, "graphify-out-llamacpp", "graph.json"))) {
        graphs.push("graphify-out-llamacpp/ (llama.cpp)");
      }
      if (graphs.length === 0) return;

      if (input.tool === "bash") {
        const reminder =
          "[graphify] knowledge graphs available: " +
          graphs.join(", ") +
          '. For focused questions, run graphify query --graph <path>/graph.json. Read GRAPH_REPORT.md for broad architecture context.';
        output.args.command =
          "echo '" + reminder + "' && " + output.args.command;
        reminded = true;
      }
    },
  };
};


---
/media/marcs/Linux_Apps/Projets_AI/.opencode/plugins/gtd-sync.js
---
// gtd-sync OpenCode plugin — mode 100 % MANUEL.
// Plus aucun miroir automatique : aucune carte GTD n'est créée sans ordre.
// Flux : /task (nom + steps validés) -> gtd_set_objective -> 1 carte.
// /label remplit l'unique case "label actuel", valable partout.
// Fin de chantier : l'agent appelle gtd_complete (MCP), sinon 1 clic 📦.
// Outils exposés : gtd_set_label, gtd_get_label, gtd_set_objective.
// Ne throw JAMAIS (erreurs renvoyées en texte).
import { existsSync, readFileSync, writeFileSync, unlinkSync } from "fs";
import { join } from "path";
import { tool } from "@opencode-ai/plugin";

const KANBAN_URL = process.env.GTD_KANBAN_URL || "http://127.0.0.1:8787";
const DEFAULT_LABEL = "+Opencode";
const PREFIX = "[opencode]";

const norm = (s) =>
  String(s ?? "").toLowerCase().replace(/\s+/g, " ").trim();

// Coeur comparable : sans préfixe [opencode], sans labels, sans dates.
// (L'API Kanban renvoie `text` SANS labels.)
function core(s) {
  return norm(s)
    .replace(/^\[opencode\]\s*/, "")
    .replace(/^\d{4}-\d{2}-\d{2}\s*/, "")
    .replace(/(?<!\S)[+@]\S+/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function extractLabels(text) {
  const m = String(text ?? "").match(/(?<!\S)[+@]\S+/g);
  return m ? [...new Set(m)] : [];
}

function tokens(s) {
  return new Set(core(s).split(" ").filter((w) => w.length > 2));
}
function containment(a, b) {
  const ta = tokens(a), tb = tokens(b);
  if (!ta.size || !tb.size) return 0;
  let inter = 0;
  for (const w of ta) if (tb.has(w)) inter++;
  return inter / Math.min(ta.size, tb.size);
}
const DEDUP_MIN = 0.75;

async function api(path, method = "GET", body = undefined) {
  try {
    const r = await fetch(KANBAN_URL + path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(5000),
    });
    return await r.json().catch(() => ({}));
  } catch {
    return null; // Kanban éteint : on le dit dans le retour outil, sans throw
  }
}

function findCard(board, needle, onlyColumn = null) {
  const n = core(needle);
  if (!n) return [];
  const hits = [];
  for (const col of board?.columns ?? []) {
    if (onlyColumn && col.id !== onlyColumn) continue;
    for (const t of col.tasks ?? []) {
      const txt = core(t.text ?? "");
      if (txt && (txt.includes(n) || n.includes(txt))) hits.push({ col: col.id, task: t });
    }
  }
  return hits;
}

function labelTokenRe(lb) {
  const e = lb.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp("(?<!\\S)" + e + "(?!\\S)");
}

export const GtdSyncPlugin = async ({ directory }) => {
  const cwd = directory ?? "";

  // --- unique case "label actuel" (/label), valable partout ---
  const CURRENT_FILE = join(cwd, ".opencode", "gtd-current.json");
  function readCurrent() {
    try {
      if (existsSync(CURRENT_FILE)) {
        const data = JSON.parse(readFileSync(CURRENT_FILE, "utf-8"));
        if (data && typeof data.label === "string" && data.label) return data;
      }
    } catch { /* ignore */ }
    return {};
  }
  function writeCurrent(data) {
    try {
      writeFileSync(CURRENT_FILE, JSON.stringify(data, null, 1));
    } catch { /* best-effort */ }
  }
  // Purge les anciens états du mode auto (sessions, chantiers, objectifs).
  for (const f of ["gtd-session-labels.json", "gtd-sync-units.json", "gtd-sync-objectives.json"]) {
    try {
      const p = join(cwd, ".opencode", f);
      if (existsSync(p)) unlinkSync(p);
    } catch { /* ignore */ }
  }

  function normalizeLabel(s) {
    let b = String(s ?? "").trim().replace(/\s+/g, "-");
    b = b.replace(/^[@+]+/, "");
    if (!b) return null;
    const marker = String(s ?? "").trim().startsWith("@") ? "@" : "+";
    return marker + b;
  }

  function cardTextFor(objective) {
    const base = objective.startsWith(PREFIX) ? objective : `${PREFIX} ${objective}`;
    const explicit = extractLabels(objective);
    const labels = explicit.length
      ? explicit
      : [readCurrent().label || DEFAULT_LABEL];
    let text = base;
    for (const lb of labels) {
      if (!labelTokenRe(lb).test(text)) text += " " + lb;
    }
    return text;
  }

  return {
    tool: {
      gtd_set_label: tool({
        description:
          "Définit le label GTD actuel (commande /label). " +
          "Une seule case globale : la prochaine carte créée via /task " +
          "partira avec ce label. Écrase le choix précédent.",
        args: {
          label: tool.schema.string().describe("Label GTD : +Projet ou @contexte (+ ajouté si absent, espaces -> -)"),
        },
        async execute(args) {
          const label = normalizeLabel(args?.label);
          if (!label) return "Label vide : rien n'a été changé.";
          writeCurrent({ label, at: Date.now() });
          return `Label actuel : ${label} — la prochaine carte /task partira avec.`;
        },
      }),
      gtd_get_label: tool({
        description:
          "Lit le label GTD actuel (défini via /label).",
        args: {},
        async execute() {
          const cur = readCurrent().label;
          return cur
            ? `Label actuel : ${cur}`
            : "Aucun label actuel : les cartes /task partiront avec +Opencode (utilise /label pour choisir).";
        },
      }),
      gtd_set_objective: tool({
        description:
          "Crée L'UNIQUE carte GTD du chantier (commande /task, après validation " +
          "du nom + steps par l'utilisateur). Ne rien créer sans validation. " +
          "Idempotent : si la carte existe déjà (reformulée ou non), rien n'est créé.",
        args: {
          objective: tool.schema.string().describe("Nom du chantier VALIDÉ par l'utilisateur, en langage clair compréhensible sans contexte (ex: 'Refonte du pipeline de leads Axel pour contacter 50 prospects/jour')"),
        },
        async execute(args) {
          const objective = String(args?.objective ?? "").trim();
          if (!objective) return "Objectif vide : rien n'a été changé.";
          const board = await api("/api/board");
          if (!board || board.error) {
            return `Kanban injoignable (${KANBAN_URL}) : lance ./start-kanban.sh dans GoForIt-GTD/kanban puis réessaie /task. Rien n'a été créé.`;
          }
          // Dédup : exact puis tolérant (reformulation), sur tout le board.
          if (findCard(board, objective).length) {
            return `Carte déjà présente (« ${objective.slice(0, 60)} ») : rien créé (dédup).`;
          }
          const n = core(objective);
          if (n) {
            let cands = 0;
            for (const col of board.columns ?? []) {
              for (const t of col.tasks ?? []) {
                if (containment(objective, t.text ?? "") >= DEDUP_MIN) cands++;
              }
            }
            if (cands === 1) {
              return `Carte équivalente déjà présente : rien créé (dédup tolérante).`;
            }
            if (cands > 1) {
              return `Plusieurs cartes proches existent : précise le nom via /task pour éviter un doublon. Rien créé.`;
            }
          }
          const created = await api("/api/add", "POST", { text: cardTextFor(objective) });
          if (!created || created.error) {
            return `Échec création carte : ${created?.error || "Kanban injoignable"}. Réessaie /task.`;
          }
          return `Carte GTD créée en Inbox : ${cardTextFor(objective).slice(0, 100)} — fais tes todos normalement, puis gtd_complete en fin de chantier.`;
        },
      }),
    },
  };
};


---
/media/marcs/Linux_Apps/Projets_AI/.opencode/plugins/notify-desktop.js
---
// notify-desktop OpenCode plugin (Linux)
// Envoie une notification de bureau via notify-send quand opencode a fini de bosser.
// Couvre : fin de tâche (main session idle), permission requise, question en attente, erreur session.
// Anti-spam : sessions principales uniquement, cooldown 30s, collapse 3s, dedup session.idle vs session.status.

export const NotifyDesktopPlugin = async ({ $ }) => {
  const mainSessions = new Set();
  const lastNotify = new Map(); // key -> timestamp ms
  const seenIdle = new Set(); // sessionID ayant déjà notifié via session.status
  const pendingCollapse = new Map(); // key -> timeout id

  const COOLDOWN_MS = 30000;
  const COLLAPSE_MS = 3000;

  const esc = (s) => String(s ?? "").replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\$/g, "\\$").slice(0, 300);

  async function notify({ title, body, urgency = "normal", icon = "dialog-information", sound = "complete.oga" }) {
    const t = esc(title);
    const b = esc(body);
    try {
      await $`notify-send -u ${urgency} -a OpenCode -i ${icon} "${t}" "${b}"`;
    } catch (e) {
      console.error("[notify-desktop] notify-send failed:", String(e?.message ?? e));
    }
    // Son best-effort, jamais bloquant. paplay dispo vérifié sur cette machine.
    try {
      await $`paplay /usr/share/sounds/freedesktop/stereo/${sound} 2>/dev/null || true`;
    } catch {
      // silencieux : le visuel suffit
    }
  }

  function shouldSend(key) {
    const now = Date.now();
    const last = lastNotify.get(key) ?? 0;
    if (now - last < COOLDOWN_MS) return false;
    lastNotify.set(key, now);
    return true;
  }

  function collapsed(key, fn) {
    // Collapse les rafales : seule la dernière notif de la fenêtre part.
    if (pendingCollapse.has(key)) clearTimeout(pendingCollapse.get(key));
    pendingCollapse.set(
      key,
      setTimeout(() => {
        pendingCollapse.delete(key);
        fn();
      }, COLLAPSE_MS)
    );
  }

  return {
    event: async ({ event }) => {
      try {
        // --- tracking sessions principales (sans parentID) ---
        if (event.type === "session.created") {
          const info = event.properties?.info;
          if (info && !info.parentID) mainSessions.add(info.id);
          return;
        }
        if (event.type === "session.deleted") {
          const id = event.properties?.info?.id;
          if (id) {
            mainSessions.delete(id);
            seenIdle.delete(id);
          }
          return;
        }

        // --- fin de tâche : session principale -> idle ---
        if (event.type === "session.status") {
          const { sessionID, status } = event.properties ?? {};
          const state = status?.type ?? status;
          if (state === "idle" && mainSessions.has(sessionID)) {
            seenIdle.add(sessionID);
            const key = `idle:${sessionID}`;
            collapsed(key, () => {
              if (!shouldSend(key)) return;
              notify({
                title: "OpenCode — Tâche terminée",
                body: "L'agent a fini de bosser. À toi de relire.",
                urgency: "normal",
                icon: "dialog-information",
                sound: "complete.oga",
              });
            });
          }
          return;
        }

        // --- legacy session.idle : ignoré si session.status déjà vu (anti-doublon) ---
        if (event.type === "session.idle") {
          const id =
            event.properties?.sessionID ?? event.properties?.session?.id ?? event.properties?.info?.id ?? "unknown";
          if (seenIdle.has(id)) return; // déjà notifié via session.status
          if (id !== "unknown" && !mainSessions.has(id)) return; // sub-agent : silence
          const key = `idle:${id}`;
          collapsed(key, () => {
            if (!shouldSend(key)) return;
            notify({
              title: "OpenCode — Tâche terminée",
              body: "L'agent a fini de bosser. À toi de relire.",
              urgency: "normal",
              icon: "dialog-information",
              sound: "complete.oga",
            });
          });
          return;
        }

        // --- permission requise (bloqué) ---
        if (event.type === "permission.asked") {
          const key = "permission";
          if (!shouldSend(key)) return;
          notify({
            title: "OpenCode — Permission requise",
            body: "L'agent est bloqué et attend ton autorisation.",
            urgency: "critical",
            icon: "dialog-warning",
            sound: "dialog-warning.oga",
          });
          return;
        }

        // --- question en attente d'input ---
        if (event.type === "question.asked") {
          const key = "question";
          if (!shouldSend(key)) return;
          notify({
            title: "OpenCode — Question en attente",
            body: "L'agent te pose une question.",
            urgency: "normal",
            icon: "dialog-information",
            sound: "dialog-information.oga",
          });
          return;
        }

        // --- erreur session ---
        if (event.type === "session.error") {
          const key = "error";
          if (!shouldSend(key)) return;
          notify({
            title: "OpenCode — Erreur session",
            body: "La session a rencontré une erreur, vérifie le TUI.",
            urgency: "critical",
            icon: "dialog-error",
            sound: "dialog-error.oga",
          });
        }
      } catch (e) {
        console.error("[notify-desktop] handler failed:", String(e?.message ?? e));
      }
    },
  };
};


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/README-V2.md
---
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


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/plugin-sdk/package.json
---
{
  "name": "plugin-sdk",
  "version": "1.0.0",
  "main": "index.js",
  "scripts": {
    "test": "echo \"Error: no test specified\" && exit 1"
  },
  "keywords": [],
  "author": "",
  "license": "ISC",
  "description": "",
  "dependencies": {
    "@opencode/plugin": "^2.0.32"
  }
}


---
/media/marcs/Linux_Apps/Projets_AI/Opencodev2/config/opencode/package.json
---
{
  "dependencies": {
    "@opencode-ai/plugin": "1.18.35"
  }
}


---
