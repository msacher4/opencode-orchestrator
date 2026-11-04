import { Plugin } from "@opencode/plugin/tui"
import { execFileSync } from "node:child_process"
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs"

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
  manifest?: string
  agents?: string | Record<string, string>
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
    lines.push(`Manifest : ${rule.goal.manifest || "aucun (goal libre)"}`)
    lines.push("Lancement : session agent build à l'échéance, reprise auto si usageLimited")
    const parsed = manifestSteps(rule)
    if (parsed) {
      const byRole: Record<string, { count: number; models: Set<string> }> = {}
      for (const step of parsed.steps) {
        const r = step.role
        if (!byRole[r]) byRole[r] = { count: 0, models: new Set() }
        byRole[r].count++
        if (step.model) byRole[r].models.add(step.model)
      }
      const roleLines = Object.entries(byRole)
        .map(([role, info]) => `${role} ${info.count}× ${Array.from(info.models).join(", ") || "aucun modèle"}`)
        .join(" · ")
      lines.push(`Modèles réels (manifeste) : ${roleLines}`)
      lines.push(`Détail par étape : 🧩 Rôles & modèles`)
    } else if (rule.goal.agents) {
      const agentsStr = formatAgents(rule.goal)
      if (agentsStr) lines.push(`Défaut de rôle (aucun manifeste) : ${agentsStr}`)
    } else if (rule.goal.model) {
      lines.push(`Défaut de rôle (aucun manifeste) : Modèle : ${rule.goal.model}`)
    }
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

/* ───────── bloc « Avancement » (disque seul, aucun événement de session) ───────── */

const ORCH_ROOT = `${PROJECT}/.opencode/orch`
const PROGRESS_STEPS = 12

type StepAdvance = { id: string; title: string; status: string }

// Dernier manifeste `.opencode/orch/ses_*/orchestration.md` par mtime : un run
// peut mourir en silence, on se fie au fichier, jamais à l'état de session.
function latestManifest(): string | null {
  let best: string | null = null
  let bestAt = -1
  let dirs: string[] = []
  try {
    dirs = readdirSync(ORCH_ROOT).filter((name) => name.startsWith("ses_"))
  } catch {
    return null
  }
  for (const name of dirs) {
    const path = `${ORCH_ROOT}/${name}/orchestration.md`
    if (!existsSync(path)) continue
    try {
      const info = statSync(path)
      if (!info.isFile()) continue
      if (info.mtimeMs > bestAt) {
        bestAt = info.mtimeMs
        best = path
      }
    } catch {
      // manifeste illisible : on l'ignore, un autre run est peut-être vivant
    }
  }
  return best
}

function shortText(value: string, max: number) {
  const text = value.replace(/\s+/g, " ").trim()
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

// Segment du bas de l'id, sans provider ni vendor et sans les suffixes de
// quantification (a3b, a55b, iq3, q4, q8, gguf) : ce que Marc voit dans la liste
// du select (qui n'affiche QUE le titre). Rend "" si le modèle est vide ou
// commence par "—".
function shortModel(model: string): string {
  if (!model || model.startsWith("—")) return ""
  const segments = model.split("/")
  let base = segments[segments.length - 1]
  const suffixes = ["a3b", "a55b", "iq3", "q4", "q8", "gguf"]
  for (;;) {
    let stripped = false
    for (const suffix of suffixes) {
      const tail = `-${suffix}`
      if (base.length > tail.length && base.toLowerCase().endsWith(tail)) {
        base = base.slice(0, base.length - tail.length)
        stripped = true
      }
    }
    if (!stripped) break
  }
  return base
}

function clockLabel(at: number) {
  const date = new Date(at)
  return `${pad(date.getDate())}/${pad(date.getMonth() + 1)} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

// Couples `## <id> — <titre>` / `- Statut:`. Une étape réapparue (relance) garde
// sa DERNIÈRE occurrence ; les titres sans statut (sections) ne sont pas des étapes.
function parseAdvance(content: string): StepAdvance[] {
  const steps: StepAdvance[] = []
  let current: StepAdvance | null = null
  for (const raw of content.split("\n")) {
    const line = raw.trim()
    const heading = /^##\s+(.+)$/.exec(line)
    if (heading) {
      if (current) steps.push(current)
      const text = heading[1].trim()
      const parts = /^([A-Za-z0-9_-]+)\s+[—-]\s+(.+)$/.exec(text)
      current = parts
        ? { id: parts[1], title: parts[2].trim(), status: "" }
        : { id: text, title: text, status: "" }
      continue
    }
    if (!current) continue
    const status = /^-\s*Statut\s*:\s*(.+)$/.exec(line)
    if (status) current.status = status[1].trim()
  }
  if (current) steps.push(current)

  const kept: StepAdvance[] = []
  const seen: { [id: string]: number } = {}
  for (const step of steps) {
    if (!step.status) continue
    const position = seen[step.id]
    if (position === undefined) {
      seen[step.id] = kept.length
      kept.push(step)
    } else {
      kept[position] = step
    }
  }
  return kept
}

// Relu sur le disque à CHAQUE appel (donc à chaque tick du timer) : aucun état
// de session, aucun cache entre deux ticks.
function progressText(): string {
  const path = latestManifest()
  if (!path) return "Avancement — aucun run"
  let content = ""
  let at = Date.now()
  try {
    content = readFileSync(path, "utf8")
    at = statSync(path).mtimeMs
  } catch {
    return "Avancement — aucun run"
  }
  const dirs = path.split("/")
  const session = dirs[dirs.length - 2] || "?"
  const head = `Avancement — ${session} · maj ${clockLabel(at)}`
  const steps = parseAdvance(content).slice(-PROGRESS_STEPS)
  if (!steps.length) return `${head}\n(aucune étape dans le manifeste)`
  return [
    head,
    ...steps.map((step) => `${step.id} · ${shortText(step.title, 56)} — Statut : ${shortText(step.status, 56)}`),
  ].join("\n")
}

// Ligne unique affichée dans le panneau (/recurring) ; réécrite par le timer.
let progressLine = "Avancement — aucun run"

function refreshProgress() {
  const lines = progressText().split("\n")
  const head = lines[0] ?? "Avancement — aucun run"
  const last = lines.length > 1 ? lines[lines.length - 1] : ""
  progressLine = shortText(last ? `${head} · ${last}` : head, 140)
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

function checkModel(raw: string): Check {
  const value = raw.trim()
  if (!value) return { value: undefined }
  const re = /^[a-z0-9][a-z0-9._-]*(\/[a-z0-9][a-z0-9._-]*)+(#[a-z0-9._-]+)?$/i
  if (!re.test(value)) {
    return { error: "Format attendu : provider/model, provider/model/sous-modele ou provider/model#variant (ex : opencode/deepseek-v4.1-flash-free ou nvidia/nvidia/nemotron-3-ultra-550b-a55b)" }
  }
  return { value }
}

/* ───────── model catalogue (copied from orch-ui/tui.ts) ───────── */

const FALLBACK_MODELS = [
  "llama-local/qwen-opus",
  "llama-local/qwen3.6q4",
  "llama-local/qwen3.6iq3-150k",
  "llama-local/ornith1.5-9b",
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
//                   openrouter only when :free / -free.
//   reviewer        → review pool + free + local.
function modelAllowedFor(role: string, id: string): boolean {
  // orchestrator role (session model) is not subject to the execution pool
  if (role === "orchestrator") return true

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

async function listModelOptions(ctx: Ctx, role: string): Promise<Array<{ title: string; value: string; description: string }>> {
  const sortByValue = (list: Array<{ title: string; value: string; description: string }>) =>
    list.slice().sort((a, b) => a.value.localeCompare(b.value))

  // a) ctx.client.model.list
  try {
    const client: any = ctx?.client
    if (client && client.model && typeof client.model.list === "function") {
      const res: any = await client.model.list.call(client.model)
      const data: any[] = Array.isArray(res?.data) ? res.data : (Array.isArray(res) ? res : [])
      if (data.length) {
        const seen: { [k: string]: boolean } = {}
        const out: Array<{ title: string; value: string; description: string }> = []
        for (const m of data) {
          const providerID = String(m?.providerID ?? "")
          const id = String(m?.id ?? "")
          if (!providerID || !id) continue
          const full = `${providerID}/${id}`
          if (seen[full]) continue
          seen[full] = true
          if (!modelAllowedFor(role, full)) continue
          out.push({
            title: full,
            value: full,
            description: providerLabel(providerID) + (m.name ? ` · ${m.name}` : ""),
          })
        }
        if (out.length) return sortByValue(out)
      }
    }
  } catch { /* ignore, fall through */ }

  // b) ctx.location.model.list
  try {
    const models = ctx.location?.model?.list?.() ?? []
    if (models.length) {
      const seen: { [k: string]: boolean } = {}
      const out: Array<{ title: string; value: string; description: string }> = []
      for (const m of models) {
        const providerID = String(m?.providerID ?? "")
        const modelID = String(m?.modelID ?? "")
        if (!providerID || !modelID) continue
        const full = `${providerID}/${modelID}`
        if (seen[full]) continue
        seen[full] = true
        if (!modelAllowedFor(role, full)) continue
        const ctxLimit = Math.round((m.limit?.context ?? 0) / 1000)
        const desc = m.name ? `${m.name} · ${ctxLimit}k ctx` : full
        out.push({ title: full, value: full, description: providerLabel(providerID) + (m.name ? ` · ${m.name}` : "") })
      }
      if (out.length) return sortByValue(out)
    }
  } catch { /* ignore, fall through */ }

  // c) FALLBACK_MODELS
  const fallback = FALLBACK_MODELS
    .filter((id) => modelAllowedFor(role, id))
    .map((id) => ({
      title: id,
      value: id,
      description: providerLabel(id.split("/")[0]),
    }))
  return sortByValue(fallback.length ? fallback : FALLBACK_MODELS.map((id) => ({
    title: id,
    value: id,
    description: providerLabel(id.split("/")[0]),
  })))
}

async function askModel(ctx: Ctx, title: string, current: string, role: string): Promise<string | undefined> {
  const modelOptions = await listModelOptions(ctx, role)
  const options = [
    { title: "✎ Saisir à la main…", value: "__manual__", description: "provider/model (ou provider/model#variant)" },
    ...modelOptions,
  ]

  if (modelOptions.length === 0) {
    return await ask(ctx, { title, placeholder: "provider/model", value: current || "" }, checkModel)
  }

  for (;;) {
    const choice = await ctx.ui.dialog.select<string>({
      title,
      placeholder: "recherche : tape un morceau de provider ou de modèle",
      options,
      current: current || undefined,
      search: (q: string, opts: any[]) => {
        const s = q.trim().toLowerCase()
        if (!s) return opts
        return opts.filter(o => `${o.title} ${o.description ?? ""}`.toLowerCase().includes(s))
      },
    })
    if (choice === undefined) return undefined
    if (choice === "__manual__") {
      return await ask(ctx, { title, placeholder: "provider/model", value: current || "" }, checkModel)
    }
    const checked = checkModel(choice)
    if (checked.error) {
      await ctx.ui.dialog.alert({ title: "Valeur invalide", message: checked.error })
      continue
    }
    return checked.value
  }
}

function checkManifest(raw: string): Check {
  const value = raw.trim()
  if (!value) return { value: undefined }
  const resolved = value.startsWith("/") ? value : `${PROJECT}/${value}`
  if (!existsSync(resolved)) {
    return { error: `Fichier introuvable : ${resolved}` }
  }
  return { value }
}

function formatAgents(goal: GoalConfig | undefined): string | null {
  if (!goal) return null
  if (typeof goal.agents === 'string') {
    // Legacy format: "worker=..., reviewer=..., merger=..."
    return goal.agents
  }
  if (goal.agents && typeof goal.agents === 'object') {
    const parts: string[] = []
    const roles = ['orchestrator', 'worker', 'reviewer', 'merger'] as const
    for (const role of roles) {
      const model = goal.agents[role]
      if (model !== undefined) {
        parts.push(`${role}=${model || 'défaut'}`)
      }
    }
    return parts.length ? parts.join(' · ') : null
  }
  // Fallback to legacy goal.model for orchestrator role
  if (goal.model) {
    return `orchestrator=${goal.model}`
  }
  return null
}

function getAgentOptions(isOrchestratorMode: boolean): Array<{ title: string; value: string; description: string }> {
  const agentsDir = `${PROJECT}/.opencode/agents`
  const files = readdirSync(agentsDir).filter((f) => f.endsWith(".md"))
  const options: Array<{ title: string; value: string; description: string }> = []

  if (isOrchestratorMode) {
    options.push({
      title: "orchestrator-auto (défaut)",
      value: "orchestrator-auto",
      description: "Orchestrateur 100% autonome pour manifest préenregistré",
    })
  }

  for (const file of files) {
    const name = file.replace(/\.md$/, "")
    const path = `${agentsDir}/${file}`
    let description = ""
    try {
      const content = readFileSync(path, "utf8")
      const match = /^description:\s*(.+)$/m.exec(content)
      if (match) description = match[1].trim()
    } catch {
      // ignore
    }
    options.push({ title: name, value: name, description: description || "Agent personnalisé" })
  }

  options.push(
    { title: "build (défaut)", value: "build", description: "Agent build par défaut" },
    { title: "orchestrator-auto", value: "orchestrator-auto", description: "Orchestrateur 100% autonome pour manifest préenregistré" }
  )

  return options
}

/* ───────── Manifest parsing (adapté depuis orch-ui/tui.ts) ───────── */

type ManifestStep = {
  id: string
  title: string
  role: string
  model: string
  summary: string
  modelLine: number
  blockStart: number
  blockEnd: number
}

function canonLine(line: string): string {
  return String(line)
    .replace(/^\s*-\s*\*\*([^*]+?)\*\*\s*:/, "- $1:")
    .replace(/^\s*-\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ ]*?)\s*:/, "- $1:")
}

function manifestSteps(rule: Rule): { path: string; steps: ManifestStep[] } | null {
  const manifestPath = rule.goal?.manifest
  if (!manifestPath) return null
  const resolved = manifestPath.startsWith("/") ? manifestPath : `${PROJECT}/${manifestPath}`
  if (!existsSync(resolved)) return null
  let content: string
  try {
    content = readFileSync(resolved, "utf8")
  } catch {
    return null
  }
  const lines = content.split("\n")
  const headerRE = /^#{2,4}\s/
  const steps: ManifestStep[] = []
  const seenId: Record<string, ManifestStep> = {}

  for (let i = 0; i < lines.length; i++) {
    if (!headerRE.test(lines[i])) continue
    const headerText = lines[i].replace(/^#{2,4}\s*/, "").trim()
    const headerIdMatch = /^([A-Za-z0-9_-]+)\s+[—-]\s+(.+)$/.exec(headerText)
    let stepId = ""
    let stepTitle = headerText
    if (headerIdMatch) {
      stepId = headerIdMatch[1]
      stepTitle = headerIdMatch[2].trim()
    }
    // Find block end (next header or EOF)
    let blockEnd = i + 1
    while (blockEnd < lines.length && !headerRE.test(lines[blockEnd])) blockEnd++
    // Scan block for fields
    let role = ""
    let model = ""
    let summary = ""
    let modelLine = -1
    let hasIdLine = false
    for (let j = i + 1; j < blockEnd; j++) {
      const canon = canonLine(lines[j])
      if (/^-\s*id\s*:/.test(canon)) hasIdLine = true
      if (/^-\s*Qui\s*:/.test(canon)) role = canon.replace(/^-\s*Qui\s*:\s*/, "").trim()
      if (/^-\s*Modèle\s*:/.test(canon)) {
        model = canon.replace(/^-\s*Modèle\s*:\s*/, "").trim()
        modelLine = j
      }
      if (/^-\s*Résumé\s*:/.test(canon)) summary = canon.replace(/^-\s*Résumé\s*:\s*/, "").trim()
    }
    if (!hasIdLine) continue
    // Clean title: remove (worker) / (reviewer) / (webnav) suffix
    const cleanTitle = stepTitle.replace(/\s*\((worker|reviewer|webnav)\)\s*$/, "")
    const step: ManifestStep = {
      id: stepId,
      title: cleanTitle,
      role: role || "inconnu",
      model,
      summary,
      modelLine,
      blockStart: i,
      blockEnd,
    }
    seenId[stepId] = step // keep last occurrence
  }

  // Preserve manifest order for steps with same role, but group by role order
  const roleOrder = ["worker", "reviewer", "merger", "webnav", "orchestrator"]
  const ordered: ManifestStep[] = []
  for (const role of roleOrder) {
    for (const step of Object.values(seenId)) {
      if (step.role === role) ordered.push(step)
    }
  }
  // Unknown roles alphabetically
  const unknownRoles = Array.from(new Set(Object.values(seenId).map(s => s.role)))
    .filter(r => !roleOrder.includes(r))
    .sort()
  for (const role of unknownRoles) {
    for (const step of Object.values(seenId)) {
      if (step.role === role) ordered.push(step)
    }
  }

  return ordered.length ? { path: resolved, steps: ordered } : null
}

function stepSummary(step: ManifestStep): string {
  const text = step.summary || step.title
  return text.length > 90 ? `${text.slice(0, 89)}…` : text
}

function roleIcon(role: string): string {
  if (role === "worker") return "🔨"
  if (role === "reviewer") return "🔎"
  if (role === "merger") return "🔀"
  if (role === "webnav") return "🌐"
  if (role === "orchestrator") return "⚙"
  return "❓"
}

async function manifestFlow(ctx: Ctx, rule: Rule) {
  const parsed = manifestSteps(rule)
  if (!parsed) {
    await ctx.ui.dialog.alert({
      title: "Manifeste",
      message: "Aucun manifeste lisible pour cette règle (goal libre ou fichier absent).",
    })
    return
  }
  const { steps } = parsed

  for (;;) {
    const options: Array<{ title: string; value: string; description: string }> = []
    // Un groupe par couple (rôle, modèle) : les rôles dans leur ordre
    // d'apparition, et dans chaque rôle les modèles dans leur ordre
    // d'apparition. L'en-tête porte le modèle court (le select n'affiche QUE le
    // titre), la phrase de l'étape reste entière sous lui.
    const groups: Array<{ role: string; model: string; steps: ManifestStep[] }> = []
    for (const step of steps) {
      let group = groups.find(g => g.role === step.role && g.model === step.model)
      if (!group) {
        group = { role: step.role, model: step.model, steps: [] }
        groups.push(group)
      }
      group.steps.push(step)
    }
    for (const group of groups) {
      const label = shortModel(group.model) || "(modèle de la session)"
      const count = group.steps.length
      const headerTitle = shortText(`──── ${group.role} · ${count} ${count > 1 ? "étapes" : "étape"} · ${label}`, 60)
      options.push({
        title: headerTitle,
        value: `__hdr__:${group.role}:${group.model}`,
        description: "",
      })
      for (const step of group.steps) {
        options.push({
          title: `${roleIcon(step.role)} ${step.id} · ${shortText(step.summary, 46)}`,
          value: `step:${step.id}`,
          description: `${step.role} · ${step.model || "aucun modèle"}`,
        })
      }
    }
    // Role bulk edit options
    const rolesPresent = Array.from(new Set(steps.map(s => s.role))).filter(r => r !== "orchestrator")
    for (const role of rolesPresent) {
      const roleSteps = steps.filter(s => s.role === role)
      options.push({
        title: `⚙ Modifier le modèle des ${roleSteps.length} étapes ${role}`,
        value: `role:${role}`,
        description: `${roleSteps.length} étape(s) du rôle ${role}`,
      })
    }
    // Session model (orchestrator) option
    options.push({
      title: "⚙ Modifier le modèle de session (orchestrator)",
      value: "role:orchestrator",
      description: "Modèle de la session orchestrateur (goal.agents.orchestrator ou goal.model)",
    })
    options.push({ title: "↩ Retour", value: "back" })

    const choice = await ctx.ui.dialog.select<string>({
      title: `${rule.title} — ${steps.length} étapes`,
      options,
      search: (q: string, opts: any[]) => {
        const s = q.trim().toLowerCase()
        if (!s) return opts
        return opts.filter(o => `${o.title} ${o.description ?? ""}`.toLowerCase().includes(s))
      },
    })
    if (!choice || choice === "back") return

    if (choice.startsWith("__hdr__")) {
      continue
    }

    if (choice.startsWith("step:")) {
      const stepId = choice.slice(5)
      const step = steps.find(s => s.id === stepId)
      if (step) await setStepModels(ctx, rule, [step], step.role, `${step.role} · ${step.id}`)
      continue
    }
    if (choice.startsWith("role:")) {
      const role = choice.slice(5)
      if (role === "orchestrator") {
        await setSessionModel(ctx, rule)
      } else {
        const roleSteps = steps.filter(s => s.role === role)
        if (roleSteps.length) await setStepModels(ctx, rule, roleSteps, role, role)
      }
      continue
    }
  }
}

async function setStepModels(ctx: Ctx, rule: Rule, target: ManifestStep[], role: string, label: string) {
  const parsed = manifestSteps(rule)
  if (!parsed) return
  const { path, steps: allSteps } = parsed

  // Current model(s)
  const models = Array.from(new Set(target.map(s => s.model).filter(Boolean)))
  const current = models.join(" · ")

  const model = await askModel(ctx, `Modèle ${label}`, current, role)
  if (model === undefined) return

  // Confirm
  const lines = [
    `${target.length} étape(s)`,
    ...target.slice(0, 15).map(s => `${s.id} · ${s.model || "aucun"} → ${model}`),
    ...(target.length > 15 ? ["…"] : []),
  ]
  if (rule.goalRun?.status === "active") {
    lines.push("Un run est en cours : il garde l'ancien modèle (snapshot), le changement s'applique au prochain run.")
  }
  const confirmed = await ctx.ui.dialog.confirm({
    title: `Confirmer le changement de modèle`,
    message: lines.join("\n"),
    label: { confirm: "Appliquer", cancel: "Annuler" },
  })
  if (!confirmed) return

  // Read fresh, edit, write with backup
  let content = readFileSync(path, "utf8")
  let lines2 = content.split("\n")

  for (const step of target) {
    const blockStart = step.blockStart
    const blockEnd = step.blockEnd
    let wrote = false
    // Replace existing - Modèle: in block
    for (let i = blockStart + 1; i < blockEnd; i++) {
      if (/^- Modèle:/.test(canonLine(lines2[i]))) {
        const indent = lines2[i].match(/^\s*/)?.[0] ?? ""
        lines2[i] = `${indent}- Modèle: ${model}`
        wrote = true
        break
      }
    }
    // Insert after - Qui: if not found
    if (!wrote) {
      for (let i = blockStart + 1; i < blockEnd; i++) {
        if (/^- Qui:/.test(canonLine(lines2[i]))) {
          const indent = lines2[i].match(/^\s*/)?.[0] ?? ""
          lines2.splice(i + 1, 0, `${indent}- Modèle: ${model}`)
          wrote = true
          break
        }
      }
    }
    // Fallback: insert after header
    if (!wrote && blockEnd > blockStart) {
      const indent = lines2[blockStart].match(/^\s*/)?.[0] ?? ""
      lines2.splice(blockStart + 1, 0, `${indent}- Modèle: ${model}`)
      wrote = true
    }
    if (!wrote) {
      await ctx.ui.dialog.alert({
        title: "Échec",
        message: `Bloc de l'étape « ${step.id} » inexploitable — aucune écriture.`,
      })
      return
    }
  }

  // Backup
  const now = new Date()
  const bak = `${path}.bak-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  writeFileSync(bak, content, "utf8")

  // Write
  writeFileSync(path, lines2.join("\n"), "utf8")

  // Verify
  const verifyContent = readFileSync(path, "utf8")
  const verifyLines = verifyContent.split("\n")
  let ok = true
  const errors: string[] = []
  for (const step of target) {
    let found = false
    for (let i = step.blockStart + 1; i < step.blockEnd; i++) {
      const canon = canonLine(verifyLines[i])
      if (/^-\s*Modèle\s*:/.test(canon)) {
        const val = canon.replace(/^-\s*Modèle\s*:\s*/, "").trim()
        if (val === model) found = true
        else errors.push(`${step.id} : attendu ${model}, trouvé ${val}`)
        break
      }
    }
    if (!found) errors.push(`${step.id} : ligne Modèle absente après écriture`)
  }
  if (errors.length) {
    // Restore
    writeFileSync(path, content, "utf8")
    await ctx.ui.dialog.alert({
      title: "Échec — backup restauré",
      message: errors.join("\n"),
    })
    ok = false
  }

  // Trace
  try {
    writeFileSync(
      "/tmp/recurring-ui-last-manifest-edit.json",
      JSON.stringify({
        at: new Date().toISOString(),
        manifest: path,
        ids: target.map(s => s.id),
        role,
        from: current,
        to: model,
        ok,
      }, null, 2),
      "utf8",
    )
  } catch {}

  if (ok) {
    ctx.ui.toast.show({
      title: "Modèle mis à jour",
      message: `${target.length} étape(s) · ${label} → ${model}`,
      variant: "success",
      duration: 4000,
    })
  }
}

async function setSessionModel(ctx: Ctx, rule: Rule) {
  const current = rule.goal?.agents?.orchestrator || rule.goal?.model || ""
  const model = await askModel(ctx, "Modèle orchestrator (session)", current, "orchestrator")
  if (model === undefined) return

  const result = call(["--update", rule.id, "--patch-json", JSON.stringify({ goal: { agents: { orchestrator: model } } })])
  if (result.error) {
    await ctx.ui.dialog.alert({ title: "Échec", message: result.error })
    return
  }
  ctx.ui.toast.show({
    title: "Modèle de session mis à jour",
    message: `orchestrator → ${model}`,
    variant: "success",
    duration: 4000,
  })
}

async function addFlow(ctx: Ctx, opts?: { prefill?: Rule; id?: string }) {
  const prefill = opts?.prefill
  const isEdit = !!opts?.id

  // ===== BLOC 1 — Planification (INCHANGÉ) =====
  const title = await ask(
    ctx,
    { title: "Titre de la tâche récurrente", placeholder: "Relancer les leads X", value: prefill?.title || "" },
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
    current: prefill?.schedule?.kind || "daily",
  })
  if (!kind) return

  const payload: any = { title, kind }

  if (kind === "every") {
    payload.every_days = await ask(ctx, { title: "Tous les combien de jours ?", value: String(prefill?.schedule?.everyDays || 2) }, checkEveryDays)
    if (payload.every_days === undefined) return
  } else if (kind === "weekly") {
    payload.weekdays = await ask(
      ctx,
      { title: "Quels jours ?", description: "lun, mar, mer, jeu, ven, sam, dim", value: prefill?.schedule?.weekdays?.join(",") || "lun" },
      checkWeekdays,
    )
    if (payload.weekdays === undefined) return
  } else if (kind === "monthly") {
    payload.day_of_month = await ask(
      ctx,
      { title: "Quel jour du mois ?", description: "1 à 31", value: String(prefill?.schedule?.dayOfMonth || 1) },
      checkDayOfMonth,
    )
    if (payload.day_of_month === undefined) return
  } else if (kind === "yearly") {
    const date = await ask(ctx, { title: "Quelle date ?", description: "JJ/MM", value: prefill?.schedule?.day && prefill?.schedule?.month ? `${String(prefill.schedule.day).padStart(2,'0')}/${String(prefill.schedule.month).padStart(2,'0')}` : "01/01" }, checkYearly)
    if (date === undefined) return
    payload.day = date.day
    payload.month = date.month
  }

  payload.time = await ask(ctx, { title: "Heure d'exécution", description: "HH:MM", value: prefill?.schedule?.time || "09:00" }, checkTime)
  if (payload.time === undefined) return

  payload.start = await ask(
    ctx,
    { title: "À partir du", description: "AAAA-MM-JJ", value: prefill?.schedule?.start || todayISO() },
    checkDate,
  )
  if (payload.start === undefined) return

  // Effets à échéance (s'applique à TOUS les modes)
  const effects = await ctx.ui.dialog.select<string>({
    title: "Effets à échéance",
    options: [
      { title: "Carte Kanban + Telegram", value: "both", description: "Labels +Opencode @recurrent" },
      { title: "Carte Kanban seulement", value: "kanban", description: "Pas de notification" },
      { title: "Telegram seulement", value: "telegram", description: "Pas de carte" },
    ],
    current: prefill?.kanban && prefill?.telegram ? "both" : prefill?.kanban ? "kanban" : prefill?.telegram ? "telegram" : "both",
  })
  if (!effects) return

  payload.kanban = effects !== "telegram"
  payload.telegram = effects !== "kanban"

  // ===== BLOC 2 — Exécution (NOUVEAU) =====
  const execMode = await ctx.ui.dialog.select<string>({
    title: "Mode d'exécution",
    options: [
      { title: "Notification simple", value: "notification", description: "Carte Kanban / Telegram, pas de session" },
      { title: "Goal (agent à l'échéance)", value: "goal", description: "Session agent + /goal avec reprise auto" },
      { title: "Orchestrator (manifest préenregistré)", value: "orchestrator", description: "Exécute un manifest de bout en bout, 100% autonome" },
    ],
    current: prefill?.goal?.enabled ? (prefill.goal.agents && typeof prefill.goal.agents === 'object' ? "orchestrator" : "goal") : "notification",
  })
  if (!execMode) return

  let goalObjective: string | undefined
  let goalAgent: string | undefined
  let goalModel: string | undefined
  let goalManifest: string | undefined
  let goalAgents: Record<string, string> = {}

  if (execMode === "notification") {
    // Rien de plus : notification simple
  } else if (execMode === "goal" || execMode === "orchestrator") {
    // Select Agent de la session
    const agentOptions = getAgentOptions(execMode === "orchestrator")
    const prefillAgent = prefill?.goal?.agent || (execMode === "orchestrator" ? "orchestrator-auto" : "build")
    const agentChoice = await ctx.ui.dialog.select<string>({
      title: "Agent de la session",
      options: agentOptions,
      current: prefillAgent,
    })
    if (!agentChoice) return
    if (agentChoice !== "build") {
      goalAgent = agentChoice
    }

    if (execMode === "goal") {
      // Mode goal : un seul modèle pour la session
      const goalModelLegacy = prefill?.goal?.model || ""
      const modelChoice = await askModel(ctx, "Modèle de la session", goalModelLegacy, "orchestrator")
      if (modelChoice === undefined) return
      if (modelChoice) goalModel = modelChoice
    } else {
      // Mode orchestrator : 4 modèles distincts
      const orchestratorModel = await askModel(
        ctx,
        "Modèle orchestrator (session)",
        prefill?.goal?.agents?.orchestrator || prefill?.goal?.model || "",
        "orchestrator"
      )
      if (orchestratorModel === undefined) return
      if (orchestratorModel) goalAgents.orchestrator = orchestratorModel

      const workerModel = await askModel(
        ctx,
        "Modèle worker",
        prefill?.goal?.agents?.worker || "",
        "worker"
      )
      if (workerModel === undefined) return
      if (workerModel) goalAgents.worker = workerModel

      const reviewerModel = await askModel(
        ctx,
        "Modèle reviewer",
        prefill?.goal?.agents?.reviewer || "",
        "reviewer"
      )
      if (reviewerModel === undefined) return
      if (reviewerModel) goalAgents.reviewer = reviewerModel

      const mergerModel = await askModel(
        ctx,
        "Modèle merger",
        prefill?.goal?.agents?.merger || "",
        "merger"
      )
      if (mergerModel === undefined) return
      if (mergerModel) goalAgents.merger = mergerModel
    }

    // Ask Objectif du goal
    goalObjective = await ask(
      ctx,
      {
        title: "Objectif du goal",
        description: "Prérempli avec le titre ; l'orchestrator-auto lira le manifest si tu en choisis un",
        value: prefill?.goal?.objective || title,
      },
      checkTitle,
    )
    if (goalObjective === undefined) return

    if (execMode === "orchestrator") {
      // Ask Chemin du manifest
      const manifestChoice = await ask(
        ctx,
        {
          title: "Chemin du manifest",
          placeholder: "relatif à /media/marcs/Linux_Apps/Projets_AI ou absolu (vide = goal libre)",
          value: prefill?.goal?.manifest || "",
        },
        checkManifest,
      )
      if (manifestChoice === undefined) return
      if (manifestChoice) goalManifest = manifestChoice
    }
  }

  // Build goal payload
  if (execMode !== "notification") {
    payload.goal = { objective: goalObjective! }
    if (goalAgent) payload.goal.agent = goalAgent
    if (execMode === "goal") {
      if (goalModel) payload.goal.model = goalModel
    } else {
      // orchestrator mode: agents object
      if (Object.keys(goalAgents).length > 0) payload.goal.agents = goalAgents
    }
    if (goalManifest) payload.goal.manifest = goalManifest
  }

  // ===== BLOC 3 — Recap étendu =====
  let execLine: string
  if (execMode === "notification") {
    execLine = "Exécution : notification simple"
  } else if (execMode === "goal") {
    const agentStr = goalAgent || "build"
    const modelStr = goalModel || "défaut"
    const objStr = goalObjective!.length > 60 ? goalObjective!.slice(0, 60) + "…" : goalObjective!
    execLine = `Goal : agent ${agentStr} · modèle ${modelStr} · ${objStr}`
  } else {
    const agentStr = goalAgent || "orchestrator-auto"
    const manifestStr = goalManifest || "aucun"
    const agentsStr = formatAgents({ agents: goalAgents } as GoalConfig) || "défaut"
    execLine = `Orchestrator : agent ${agentStr} · manifest ${manifestStr} · modèles ${agentsStr}`
  }

  const effectsLabel = effects === "both" ? "carte Kanban + Telegram" : effects === "kanban" ? "carte Kanban" : "Telegram"

  const recap = [
    isEdit ? `ID : ${opts?.id}` : "ID : (nouvelle règle)",
    `« ${title} »`,
    `Fréquence : ${scheduleLabel(kind, payload, payload.time)}`,
    `Départ : ${frDate(payload.start)}`,
    `Effets : ${effectsLabel}`,
    execLine,
  ].join("\n")

  try {
    writeFileSync(
      "/tmp/recurring-ui-last-payload.json",
      JSON.stringify({ at: new Date().toISOString(), isEdit, id: opts?.id ?? null, payload }, null, 2),
      "utf8",
    )
  } catch {}

  const confirmed = await ctx.ui.dialog.confirm({
    title: isEdit ? "Modifier la tâche récurrente ?" : "Créer la tâche récurrente ?",
    message: recap,
    label: { confirm: isEdit ? "Enregistrer" : "Créer", cancel: "Annuler" },
  })
  if (!confirmed) return

  if (isEdit && opts.id) {
    const result = call(["--update", opts.id, "--patch-json", JSON.stringify(payload)])
    try {
      writeFileSync("/tmp/recurring-ui-last-result.json", JSON.stringify({ at: new Date().toISOString(), id: opts.id, result }, null, 2), "utf8")
    } catch {}
    if (result.error) {
      await ctx.ui.dialog.alert({ title: "Mise à jour impossible", message: result.error })
      return
    }
    ctx.ui.toast.show({
      title: "Tâche récurrente modifiée",
      message: `${result.frequence} · prochaine ${shortDateTime(result.prochaine)}`,
      variant: "success",
      duration: 4000,
    })
    if (result.warning) {
      await ctx.ui.dialog.alert({ title: "Attention — manifest incohérent", message: result.warning })
    }
  } else {
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
    if (result.warning) {
      await ctx.ui.dialog.alert({ title: "Attention — manifest incohérent", message: result.warning })
    }
  }
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
        { title: "📈 Avancement", value: "__advance__", description: progressLine },
        { title: "➕ Ajouter une tâche récurrente", value: "__add__", description: "Formulaire guidé" },
        ...rules.map((rule) => ({
          title: `${rule.paused ? "⏸" : "▶"} ${rule.title}`,
          value: rule.id,
          description: describeRule(rule),
        })),
      ],
    })
    if (!choice) return

    if (choice === "__advance__") {
      await ctx.ui.dialog.alert({ title: "Avancement", message: progressText() })
      continue
    }

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
        ...(rule.goal?.manifest
          ? [
              {
                title: "🧩 Rôles & modèles du manifeste",
                value: "manifest",
                description: "Chaque worker/reviewer/merger, son job en une phrase et son modèle — modifiable",
              },
            ]
          : []),
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
          title: "✏️ Modifier",
          value: "edit",
          description: "Rouvre le formulaire prérempli",
        },
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

    if (action === "manifest") {
      await manifestFlow(ctx, rule)
      continue
    }

    if (action === "run-goal") {
      const result = call(["--run-goal", rule.id])
      if (result.error) {
        await ctx.ui.dialog.alert({ title: "Lancement impossible", message: result.error })
      } else {
        const msg = result.warning
          ? result.warning
          : result.sessionTitle ?? result.sessionID ?? "session créée"
        const variant = result.warning ? "success" : "success"
        ctx.ui.toast.show({
          title: result.warning ? "Goal lancé (timeout commande)" : "Goal lancé",
          message: msg,
          variant,
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

    if (action === "edit") {
      await addFlow(ctx, { prefill: rule, id: rule.id })
      continue
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
    // Bloc « Avancement » : timer de 2 s qui RELIT le manifeste sur le disque,
    // indépendant de tout événement de session. Nettoyé à la destruction.
    refreshProgress()
    const progressTimer = setInterval(refreshProgress, 2000)
    // `keymap.layer` doit vivre dans un scope Solid : on le monte via le slot `app`,
    // comme le plugin goal. Le layer est libéré avec le slot.
    const dispose = context.ui.slot({ append: "app", render: () => Commands(context) })
    return () => {
      clearInterval(progressTimer)
      if (typeof dispose === "function") dispose()
    }
  },
})