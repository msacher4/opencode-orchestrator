// orch-ui — popup native /orchestrator (TUI)
//
// Graphe compact : une ligne par session orchestrateur, une ligne par subagent
// (rôle, étape, modèle, ancienneté, statut vivant) + changement de modèle et
// Pause/Play sur la session enfant (⏸ interrompt la session enfant réelle et
// persiste l'état ; ▶ envoie une consigne de continuation dans cette MÊME
// session, jamais de nouveau subagent).
// Suit le modèle de employee-ui (ctx.keymap.layer, ctx.ui.dialog, ctx.ui.toast).
import { Plugin } from "@opencode/plugin/tui"
import { readFileSync, writeFileSync, renameSync, appendFileSync } from "node:fs"

const ROOT = "/media/marcs/Linux_Apps/Projets_AI"
const STATE = `${ROOT}/.opencode/orch-state.json`
const ORCH_DIR = `${ROOT}/.opencode/orch`
const AUDIT = `${ROOT}/.opencode/orch-audit.jsonl`
const PAUSE_FILE = `${ROOT}/.opencode/orch-pause.json`

type Ctx = any
type Slot = { sessionID: string; parent: string; agent: string; model: string; provider: string; step: string; since: number; manifest?: string }
type PauseEntry = { at: number; step: string; agent: string; modelAtPause: string; parent: string }
type ApiResult = { ok: boolean; via: string; error?: string }

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

function appendAudit(entry: Record<string, unknown>): void {
  try { appendFileSync(AUDIT, JSON.stringify(entry) + "\n", "utf8") }
  catch { /* audit best-effort, jamais bloquant */ }
}

function escRE(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}

// Le manifeste est écrit par l'orchestrateur en gras (`- **id** : t5`) alors que ce module
// écrit la forme nue (`- Modèle: x`). On compare toujours sur une forme canonique :
// `- <champ>: <valeur>` (sans gras, sans espace avant le deux-points).
function canonLine(line: string): string {
  return String(line)
    .replace(/^\s*-\s*\*\*([^*]+?)\*\*\s*:/, "- $1:")
    .replace(/^\s*-\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ ]*?)\s*:/, "- $1:")
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

/* ───────── pause state (orch-pause.json) ───────── */

// État persistant des pauses, indexé par ID ENFANT réel :
//   {"paused":{"<childID>":{"at":<ms>,"step":"...","agent":"...",
//              "modelAtPause":"provider/id","parent":"ses_..."}}}
// Pause/Play ne touchent JAMAIS au manifest ni à la carte de tâche : seul ce
// fichier d'état est écrit, en atomique (tmp + rename) pour ne pas le
// corrompre si le processus est interrompu en pleine écriture.
function readPaused(): Record<string, PauseEntry> {
  try {
    const data = readJSON(PAUSE_FILE)
    if (data && data.paused && typeof data.paused === "object" && !Array.isArray(data.paused)) {
      return data.paused as Record<string, PauseEntry>
    }
  } catch { /* état indisponible → aucune pause connue */ }
  return {}
}

// Retourne true/false explicitement : l'UI ne doit jamais annoncer une pause
// (ou une reprise) enregistrée si l'écriture a échoué.
function writePaused(map: Record<string, PauseEntry>): boolean {
  try {
    const tmp = `${PAUSE_FILE}.tmp`
    writeFileSync(tmp, JSON.stringify({ paused: map }, null, 2) + "\n", "utf8")
    renameSync(tmp, PAUSE_FILE)
    return true
  } catch {
    return false
  }
}

// Association slot ↔ enregistrement de pause. Sur l'ID enfant connu c'est
// direct. Si l'enfant n'est plus résoluble, on ne retombe JAMAIS sur l'ID
// parent : un enregistrement n'est associé au slot que si la correspondance
// parent + étape + agent est non ambiguë (exactement un candidat).
function pauseForSlot(slot: Slot, child: string | null, map: Record<string, PauseEntry>): PauseEntry | null {
  try {
    if (child) return map[child] || null
    const matches = Object.keys(map).filter((id) => {
      const e = map[id]
      if (!e) return false
      return String(e.parent || "") === String(slot.parent || "")
        && String(e.step || "") === String(slot.step || "")
        && String(e.agent || "") === String(slot.agent || "")
    })
    return matches.length === 1 ? map[matches[0]] : null
  } catch { return null }
}

function pauseMinutes(entry: PauseEntry): number {
  const at = Number(entry && entry.at) || 0
  if (!at) return 0
  return Math.max(0, Math.floor((Date.now() - at) / 60000))
}

/* ───────── model catalogue ───────── */

// Fallback list: used ONLY when the live catalogue is unavailable (API
// missing, throwing, or empty). Same entries as the former hard-coded list.
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

/* ───────── interrupt / resume (session enfant) ───────── */

// Base API : propriétés réellement disponibles du client, puis
// OPENCODE_BASE_URL, puis le port local par défaut d'OpenCode.
function apiBase(ctx: Ctx): string {
  const c: any = ctx?.client
  const fromClient = c && (c.baseURL || c.baseUrl || (c.options && c.options.baseURL))
  if (typeof fromClient === "string" && fromClient) return fromClient.replace(/\/+$/, "")
  const env = process.env.OPENCODE_BASE_URL
  if (typeof env === "string" && env) return env.replace(/\/+$/, "")
  return "http://127.0.0.1:4096"
}

// Interruption de la session enfant : SDK d'abord (méthode réellement
// exposée, confirmée par typeof), sinon fallback REST POST
// /api/session/{id}/interrupt. La réponse est vérifiée : une requête
// envoyée n'est pas une interruption réussie. Ne lève jamais d'exception.
async function interruptChild(ctx: Ctx, childID: string): Promise<ApiResult> {
  try {
    const ses: any = ctx?.client?.session
    const interruptFn = ses?.interrupt
    if (typeof interruptFn === "function") {
      try {
        const res: any = await interruptFn.call(ses, { sessionID: childID })
        if (res && typeof res === "object" && res.error) {
          return { ok: false, via: "sdk", error: String(res.error) }
        }
        return { ok: true, via: "sdk" }
      } catch (err) {
        // SDK présent mais échec d'appel → tentative REST avant d'abandonner.
        const reason = err instanceof Error ? err.message : String(err)
        try {
          const base = apiBase(ctx)
          const res = await fetch(`${base}/api/session/${encodeURIComponent(childID)}/interrupt`, { method: "POST" })
          if (!res.ok) return { ok: false, via: "rest", error: `HTTP ${res.status} (après échec SDK : ${reason})` }
          return { ok: true, via: "rest" }
        } catch (err2) {
          const reason2 = err2 instanceof Error ? err2.message : String(err2)
          return { ok: false, via: "rest", error: `${reason} / ${reason2}` }
        }
      }
    }
    // Pas de méthode SDK → fallback REST vérifié.
    const base = apiBase(ctx)
    const res = await fetch(`${base}/api/session/${encodeURIComponent(childID)}/interrupt`, { method: "POST" })
    if (!res.ok) return { ok: false, via: "rest", error: `HTTP ${res.status}` }
    return { ok: true, via: "rest" }
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err)
    return { ok: false, via: "unknown", error: reason }
  }
}

// Reprise : envoie une NOUVELLE consigne dans l'historique de la session
// enfant EXISTANTE (aucun nouveau subagent, aucune promesse de reprise
// atomique de la génération interrompue). SDK ctx.client.session.prompt
// d'abord (typeof), sinon REST POST /api/session/{id}/prompt {"text":...}.
// Réponse vérifiée ; ne lève jamais d'exception.
async function resumeChild(ctx: Ctx, childID: string, message: string): Promise<ApiResult> {
  const ses: any = ctx?.client?.session
  const promptFn = ses?.prompt
  if (typeof promptFn === "function") {
    try {
      const res: any = await promptFn.call(ses, { sessionID: childID, text: message })
      if (res && typeof res === "object" && res.error) {
        return { ok: false, via: "sdk", error: String(res.error) }
      }
      return { ok: true, via: "sdk" }
    } catch { /* SDK présent mais échec → fallback REST */ }
  }
  try {
    const base = apiBase(ctx)
    const res = await fetch(`${base}/api/session/${encodeURIComponent(childID)}/prompt`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text: message }),
    })
    if (!res.ok) return { ok: false, via: "rest", error: `HTTP ${res.status}` }
    return { ok: true, via: "rest" }
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err)
    return { ok: false, via: "rest", error: reason }
  }
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

  // 3) PRÉVALIDATION du manifest et du bloc de l'étape AVANT tout changement
  // de modèle : le chemin du manifest doit exister, l'étape doit être
  // localisée sans ambiguïté et un point d'insertion pour `- Modèle:` doit
  // être trouvé. Sinon on refuse AVANT le switch — jamais de modèle changé
  // pour un manifest qu'on ne saurait pas mettre à jour.
  const manifestPath = slot.manifest || `${ORCH_DIR}/${slot.sessionID}/orchestration.md`
  const content = readText(manifestPath)
  if (!content) {
    await ctx.ui.dialog.alert({ title: "Modèle", message: `Manifeste introuvable pour ${slot.sessionID} — modèle NON appliqué à ${slot.agent}.` })
    return
  }

  const lines = content.split("\n")
  const headerRE = /^#{1,6}\s/
  const idLineRE = new RegExp(`^-\\s*id:\\s*${escRE(slot.step)}\\s*$`)

  // Locate the step by its `- id: <step>` line (not by the header title),
  // keeping the LAST block when the same id appears several times.
  let idIdx = -1
  for (let i = lines.length - 1; i >= 0; i--) {
    if (idLineRE.test(canonLine(lines[i]))) { idIdx = i; break }
  }
  if (idIdx < 0) {
    await ctx.ui.dialog.alert({
      title: "Modèle",
      message: `Étape « ${slot.step} » introuvable dans le manifeste — modèle NON appliqué à ${slot.agent}, aucune écriture.`,
    })
    return
  }

  let blockStart = idIdx
  while (blockStart > 0 && !headerRE.test(lines[blockStart])) blockStart--
  let blockEnd = idIdx + 1
  while (blockEnd < lines.length && !headerRE.test(lines[blockEnd])) blockEnd++

  // Idempotent write, confined to the step's own block: replace an existing
  // `- Modèle:` in place, else insert one after `- Qui:`. Never a duplicate,
  // never an invented block — refuse BEFORE switching when unsure.
  const newLines = lines.slice()
  let wrote = false
  for (let i = blockStart + 1; i < blockEnd; i++) {
    if (/^- Modèle:/.test(canonLine(newLines[i]))) {
      const indent = newLines[i].match(/^\s*/)?.[0] ?? ""
      newLines[i] = `${indent}- Modèle: ${modelChoice}`
      wrote = true
      break
    }
  }
  if (!wrote) {
    for (let i = blockStart + 1; i < blockEnd; i++) {
      if (/^- Qui:/.test(canonLine(newLines[i]))) {
        const indent = newLines[i].match(/^\s*/)?.[0] ?? ""
        newLines.splice(i + 1, 0, `${indent}- Modèle: ${modelChoice}`)
        wrote = true
        break
      }
    }
  }
  if (!wrote && blockEnd > blockStart) {
    const indent = newLines[blockStart].match(/^\s*/)?.[0] ?? ""
    newLines.splice(blockStart + 1, 0, `${indent}- Modèle: ${modelChoice}`)
    wrote = true
  }
  if (!wrote) {
    await ctx.ui.dialog.alert({
      title: "Modèle",
      message: `Bloc de l'étape « ${slot.step} » inexploitable dans le manifeste — modèle NON appliqué à ${slot.agent}, aucune écriture.`,
    })
    return
  }

  // 4) Switch réel du modèle sur la session enfant.
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

  // 5) Écriture du manifest APRÈS un switch réussi. En cas d'échec : état
  // partiel annoncé clairement (modèle de session changé, manifest non mis
  // à jour, étape nommée) — JAMAIS de toast succès.
  try {
    writeText(manifestPath, newLines.join("\n"))
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err)
    await ctx.ui.dialog.alert({
      title: "Modèle",
      message: `Modèle de session changé (${modelChoice}) mais manifest NON mis à jour pour l'étape « ${slot.step} » (${reason}). Corrige le manifeste avant de relancer l'étape.`,
    })
    return
  }

  ctx.ui.toast.show({
    title: `Modèle changé`,
    message: `${slot.agent} → ${modelChoice}`,
    variant: "success",
    duration: 4000,
  })
}

/* ───────── pause / play flows ───────── */

async function pauseFlow(ctx: Ctx, slot: Slot) {
  // La cible est la session ENFANT réelle (childOf), jamais slot.sessionID :
  // le registre est indexé sur l'ID du parent (l'orchestrateur lui-même).
  const childID = childOf(ctx, slot)
  if (!childID) {
    await ctx.ui.dialog.alert({
      title: "Pause",
      message: `Aucune session enfant résoluble pour ${slot.agent} — rien n'a été interrompu.`,
    })
    return
  }

  const paused = readPaused()
  if (paused[childID]) {
    await ctx.ui.dialog.alert({ title: "Pause", message: `${slot.agent} est déjà en pause.` })
    return
  }

  const it = await interruptChild(ctx, childID)
  if (!it.ok) {
    await ctx.ui.dialog.alert({
      title: "Pause",
      message: `Interruption impossible pour ${slot.agent} via ${it.via}${it.error ? ` : ${it.error}` : ""} — aucune pause enregistrée.`,
    })
    return
  }

  // Interruption confirmée → persister l'état. Si l'écriture échoue : état
  // partiel annoncé (session interrompue mais pause non persistée), PAS de
  // toast succès et PAS de pause fictive.
  const modelAtPause = currentModelOf(ctx, slot)
  paused[childID] = {
    at: Date.now(),
    step: slot.step || "",
    agent: slot.agent || "",
    modelAtPause,
    parent: slot.parent || slot.sessionID,
  }
  if (!writePaused(paused)) {
    await ctx.ui.dialog.alert({
      title: "Pause",
      message: `Session enfant interrompue (${childID}) mais état de pause NON persisté (${PAUSE_FILE}) — l'état est incohérent. Vérifie le fichier avant de relancer.`,
    })
    return
  }

  ctx.ui.toast.show({
    title: "⏸ Pause",
    message: `${slot.agent} interrompu et en pause`,
    variant: "success",
    duration: 4000,
  })
  appendAudit({
    kind: "pause",
    at: Date.now(),
    agent: slot.agent || "",
    childID,
    step: slot.step || "",
    model: modelAtPause,
  })
}

async function playFlow(ctx: Ctx, slot: Slot) {
  // Jamais de prompt sur la session parente : seule la session enfant
  // résoluble peut être relancée.
  const childID = childOf(ctx, slot)
  if (!childID) {
    await ctx.ui.dialog.alert({
      title: "Play",
      message: `Aucune session enfant résoluble pour ${slot.agent} — aucun prompt envoyé.`,
    })
    return
  }

  const paused = readPaused()
  const entry = paused[childID]
  if (!entry) {
    await ctx.ui.dialog.alert({ title: "Play", message: `${slot.agent} n'est pas en pause.` })
    return
  }

  // Un changement de modèle pendant la pause est autorisé et ne bloque pas
  // la reprise ; on le constate et on le signale dans la consigne.
  const modelNow = currentModelOf(ctx, slot)
  const modelChanged = Boolean(modelNow && entry.modelAtPause && modelNow !== entry.modelAtPause)

  const isReviewer = slot.agent === "reviewer"
  let message = isReviewer
    ? "▶ Reprise demandée par Marc après pause. Reprends la revue à partir du contexte et des éléments déjà présents dans cette session. Ne recommence pas inutilement la revue depuis zéro. Termine la revue et rends ton verdict PASS/FIX/REJECT."
    : "▶ Reprise demandée par Marc après pause. Reprends à partir du contexte et de l'avancement déjà présents dans cette session. Ne recommence pas toute la carte depuis zéro. Vérifie ce qui a réellement été accompli avant de continuer. Termine la carte et rends ton contrat de retour."
  if (modelChanged) {
    message += `\n\nNote : le modèle de session a changé pendant la pause (${entry.modelAtPause || "?"} → ${modelNow}). Continue avec ce modèle constaté.`
  }

  const rs = await resumeChild(ctx, childID, message)
  if (!rs.ok) {
    // Reprise échouée : l'entrée de pause est CONSERVÉE (retente possible).
    await ctx.ui.dialog.alert({
      title: "Play",
      message: `Reprise impossible pour ${slot.agent} via ${rs.via}${rs.error ? ` : ${rs.error}` : ""} — l'entrée de pause est conservée, retente ▶ Play.`,
    })
    return
  }

  // Reprise confirmée → retirer l'entrée et persister. Si l'écriture échoue
  // après le prompt : état partiel annoncé, jamais une reprise « bien
  // enregistrée ».
  delete paused[childID]
  if (!writePaused(paused)) {
    await ctx.ui.dialog.alert({
      title: "Play",
      message: `Reprise envoyée à ${slot.agent} mais l'entrée de pause n'a pas pu être retirée (${PAUSE_FILE}) — état partiel, l'entrée reste enregistrée.`,
    })
    return
  }

  ctx.ui.toast.show({
    title: "▶ Play",
    message: modelChanged ? `${slot.agent} relancé · nouveau modèle : ${modelNow}` : `${slot.agent} relancé`,
    variant: "success",
    duration: 4000,
  })
  appendAudit({
    kind: "play",
    at: Date.now(),
    agent: slot.agent || "",
    childID,
    modelChanged,
    model: modelNow,
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
  const pausedMap = readPaused()
  const enriched = slots.map((s: Slot) => {
    // Model and status come from the CHILD session (childOf), never from
    // slot.sessionID: on a fresh spawn that id is the ORCHESTRATOR's, so
    // ds.get/ds.status on it would display the orchestrator's model/status.
    // No resolvable child → fall back to slot.model and NO status footer.
    // L'état de pause est consulté en sus, sans remplacer cette résolution :
    // si l'enfant n'est pas résoluble, on ne retombe jamais sur l'ID parent
    // et une pause n'est associée que si la correspondance est non ambiguë.
    let model = s.model || ""
    let status = ""
    let child: string | null = null
    let paused: PauseEntry | null = null
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
      paused = pauseForSlot(s, child, pausedMap)
    } catch { /* registre de slots */ }
    return { slot: s, model, status, paused }
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
      const pausedTitle = e.paused ? "⏸ " : ""
      const opt: any = {
        title: `${connector} ${pausedTitle}${roleIcon(e.slot.agent)} ${e.slot.agent || "agent"} · ${e.slot.step || "?"} · ${e.model || "?"}`,
        value: { kind: "slot", sessionID: e.slot.sessionID, parent: g.parent, step: e.slot.step },
        description: `étape ${e.slot.step || "?"} · ${age}`,
        category: shortKey(g.parent),
      }
      if (e.paused) opt.footer = `⏸ en pause (depuis ${pauseMinutes(e.paused)} min)`
      else if (e.status) opt.footer = e.status
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

    // Sous-menu d'action : une SEULE action par ouverture de popup, puis
    // sortie (une touche Entrée maintenue ne doit jamais déclencher des
    // opérations répétées). Rouvre /orchestrator pour une seconde action.
    const pausedMap = readPaused()
    const pausedEntry = pauseForSlot(slot, childOf(ctx, slot), pausedMap)
    const action = await ctx.ui.dialog.select<string>({
      title: `Action — ${slot.agent} (${slot.step})`,
      options: [
        {
          title: "🔁 Changer le modèle",
          value: "model",
          description: `actuel : ${currentModelOf(ctx, slot) || "?"} — possible aussi pendant la pause`,
        },
        pausedEntry
          ? { title: "▶ Play — reprendre", value: "play", description: `en pause depuis ${pauseMinutes(pausedEntry)} min` }
          : { title: "⏸ Pause", value: "pause", description: "interrompt la session enfant et persiste l'état" },
        { title: "Annuler", value: "cancel" },
      ],
    })
    if (!action || action === "cancel") return
    if (action === "pause") await pauseFlow(ctx, slot)
    else if (action === "play") await playFlow(ctx, slot)
    else await changeModelFlow(ctx, slot)
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
        description: "Graphe des sessions et subagents, changement de modèle, pause/play",
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
              // ⏸ sur les ENFANTS en pause uniquement (childOf + état
              // persistant) : l'orchestrateur n'est jamais affiché en pause.
              const pausedMap = readPaused()
              const lines = slots.map((s: Slot) => {
                let pe: PauseEntry | null = null
                try { pe = pauseForSlot(s, childOf(ctx, s), pausedMap) } catch { pe = null }
                return `${pe ? "⏸ " : ""}${s.agent} · ${s.model} · étape ${s.step}`
              })
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
