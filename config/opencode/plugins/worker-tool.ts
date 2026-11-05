// worker-tool — OpenCode v2 plugin
//
// Lie les jobs pocketflow-runner à leur session et livre le contrat quand
// l'orchestrateur peut le lire. N'exécute rien, ne décide rien.
// VOIE_ENVOI prouvée: ctx.client.session.promptAsync (SDK v2, prompt_async)
// avec fallback HTTP POST /session/{id}/prompt_async (voir probe-prompt.ts).

import {
  appendFileSync,
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { dirname, join } from "node:path"

const JOBS_DIR = "/tmp/opencode/jobs"
const ORPHANS_DIR = "/tmp/opencode/jobs/orphans"
const RUNS_DIR = "/tmp/opencode/runs"
const AUDIT_PATH = "/tmp/opencode/worker-tool-audit.jsonl"
function normTool(n: string) { return n.toLowerCase().replace(/^mcp__/, "").replace(/[._]+/g, "-").replace(/-+/g, "-") }
function isOurTool(n: string) { const v = normTool(n); return v === "pocketflow-runner-run-flow" || v.endsWith("-pocketflow-runner-run-flow") }
const TOOL_NAME = "pocketflow-runner_run_flow"
const MAX_LINE = 400
const ORPHAN_MS = 10 * 60 * 1000
const MAX_ATTEMPTS = 2

type Job = Record<string, any>

function ensureDirs() {
  for (const d of ["/tmp/opencode", JOBS_DIR, ORPHANS_DIR, RUNS_DIR]) {
    try { mkdirSync(d, { recursive: true }) } catch {}
  }
}

export function audit(entry: any) {
  try {
    ensureDirs()
    appendFileSync(AUDIT_PATH, JSON.stringify({ at: new Date().toISOString(), ...entry }) + "\n")
  } catch {}
}

export function atomicWrite(path: string, data: string) {
  const tmp = path + ".tmp." + process.pid + "." + Math.random().toString(16).slice(2)
  writeFileSync(tmp, data)
  renameSync(tmp, path)
}

export function extractLaunchedJob(text: string): string | null {
  const m = String(text || "").match(/LANC[ÉE]?:\s*([A-Za-z0-9._-]+)/i)
  return m ? m[1] : null
}

export function parseLaunchedCard(md: string): { card: string | null; count: number } {
  const text = String(md || "")
  // Split into step blocks starting at headings or id markers; keep it simple.
  const blocks = text.split(/\n(?=##\s|\n- id:)/g)
  const found: string[] = []
  for (const b of blocks) {
    const idm = b.match(/(?:^|\n)(?:##\s*|- id:\s*)([A-Za-z0-9_-]+)/)
    // Status can be "- Statut:" or "Statut:" line containing lancée/lancee
    const sm = b.match(/(?:^|\n)\s*-?\s*Statut\s*:\s*([^\n\r]+)/i)
    if (idm && sm && /\blanc[eé]e\b/i.test(sm[1])) found.push(idm[1].trim())
  }
  // Fallback: line-based scan if block split missed
  if (found.length !== 1) {
    const ids: string[] = []
    const re = /- id:\s*([A-Za-z0-9_-]+)[\s\S]{0,500}?-?\s*Statut\s*:\s*([^\n\r]+)/gi
    let m: RegExpExecArray | null
    while ((m = re.exec(text))) if (/\blanc[eé]e\b/i.test(m[2])) ids.push(m[1])
    const uniq = Array.from(new Set(ids))
    if (uniq.length) return { card: uniq.length === 1 ? uniq[0] : null, count: uniq.length }
  }
  const uniq = Array.from(new Set(found))
  return { card: uniq.length === 1 ? uniq[0] : null, count: uniq.length }
}

export function readLaunchedCard(projectDir: string, sessionID: string): string | null {
  try {
    const p = join(projectDir, ".opencode", "orch", sessionID, "orchestration.md")
    if (!existsSync(p)) return null
    const { card } = parseLaunchedCard(readFileSync(p, "utf8"))
    return card
  } catch { return null }
}

export function formatDeliveryLine(card: string, status: string, contract: string, jobPath: string): string {
  const head = `[worker-tool ${card || "?"}] ${status || "unknown"} — `
  const tail = ` — job ${jobPath}`
  const budget = MAX_LINE - head.length - tail.length
  if (budget <= 0) return (head.trimEnd() + tail).slice(0, MAX_LINE) // header never truncated in spirit; extreme fallback
  let c = String(contract || "").replace(/[\r\n]+/g, " ").trim()
  if (c.length > budget) c = c.slice(0, Math.max(0, budget - 1)) + "…"
  return head + c + tail
}

export function isTerminal(job: Job): boolean {
  const s = String(job?.status || "")
  return s !== "" && s !== "running"
}

export function readJob(path: string): Job | null {
  try { return JSON.parse(readFileSync(path, "utf8")) } catch { return null }
}

export function writeJob(path: string, job: Job) {
  atomicWrite(path, JSON.stringify(job))
}

export function acquireLock(jobPath: string): boolean {
  const lock = jobPath.replace(/\.json$/i, "") + ".lock"
  try {
    const fd = openSync(lock, "wx")
    closeSync(fd)
    return true
  } catch { return false }
}

export function releaseLock(jobPath: string) {
  try { unlinkSync(jobPath.replace(/\.json$/i, "") + ".lock") } catch {}
}

export async function sendPrompt(ctx: any, sessionID: string, text: string): Promise<{ ok: boolean; detail?: string }> {
  const body = { parts: [{ type: "text", text, synthetic: true }] }
  try {
    const pa = ctx?.client?.session?.promptAsync
    if (typeof pa === "function") {
      await pa.call(ctx.client.session, { sessionID }, { body })
      return { ok: true }
    }
  } catch (e: any) { var err1 = String(e?.message || e) }
  try {
    const base = String(ctx?.serverUrl || process.env.OPENCODE_SERVER_URL || process.env.OPENCODE_URL || "").replace(/\/$/, "")
    if (base) {
      const res = await fetch(base + "/session/" + encodeURIComponent(sessionID) + "/prompt_async", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      })
      if (res.ok || res.status === 204) return { ok: true }
      return { ok: false, detail: `HTTP ${res.status}` }
    }
  } catch (e: any) { return { ok: false, detail: err1 ? err1 + " | " + String(e?.message || e) : String(e?.message || e) } }
  return { ok: false, detail: err1 || "no ctx.client and no serverUrl" }
}

type Tracker = {
  firstSeen: number
  attempts: number
  lastAttempt: number
  orphaned: boolean
}

export function createRuntime(opts?: { now?: () => number; send?: (sessionID: string, text: string) => Promise<{ ok: boolean; detail?: string }> }) {
  const jobToSession = new Map<string, string>()
  const sessionIdle = new Map<string, boolean>()
  const trackers = new Map<string, Tracker>()
  const now = opts?.now || (() => Date.now())
  const send = opts?.send || (async () => ({ ok: false, detail: "no sender" }))

  function noteBinding(job: string, sessionID: string, jobOwner?: string) {
    if (jobOwner && jobOwner !== sessionID) {
      audit({ event: "owner_mismatch", job, sessionID, owner: jobOwner, action: "no_delivery" })
      return false
    }
    if (job && sessionID) jobToSession.set(job, sessionID)
    return true
  }

  function ownerFor(job: string, jobObj: Job): string {
    return String(jobObj?.owner || jobToSession.get(job) || "")
  }

  function markIdle(sessionID: string, idle: boolean) {
    if (sessionID) sessionIdle.set(sessionID, idle)
  }

  function isAtRest(sessionID: string): boolean | null {
    if (!sessionID) return null
    if (!sessionIdle.has(sessionID)) return null // unknown → queue
    return !!sessionIdle.get(sessionID)
  }

  async function tryDeliver(jobPath: string, job: Job, forceOwner?: string): Promise<"delivered" | "queued" | "orphaned" | "skip"> {
    const jobName = String(job?.job || jobPath.split("/").pop()?.replace(/\.json$/i, "") || "")
    if (!jobName || !isTerminal(job)) return "skip"
    if (job?.delivered) return "skip"

    let tr = trackers.get(jobName)
    if (!tr) { tr = { firstSeen: now(), attempts: Number(job?.attempts || 0), lastAttempt: 0, orphaned: false }; trackers.set(jobName, tr) }
    if (tr.orphaned) return "skip"

    const owner = forceOwner || ownerFor(jobName, job)
    const age = now() - tr.firstSeen
    if (!owner || age > ORPHAN_MS || tr.attempts >= MAX_ATTEMPTS) {
      await orphanJob(jobPath, job, !owner ? "no_owner" : age > ORPHAN_MS ? "timeout" : "attempts")
      tr.orphaned = true
      return "orphaned"
    }

    const rest = isAtRest(owner)
    if (rest !== true) return "queued" // unknown or busy

    if (!acquireLock(jobPath)) return "queued"
    try {
      // re-read under lock
      const fresh = readJob(jobPath) || job
      if (fresh.delivered) return "skip"
      const line = formatDeliveryLine(String(fresh.card || "?"), String(fresh.status || "unknown"), String(fresh.contract || ""), jobPath)
      tr.attempts++
      tr.lastAttempt = now()
      const res = await send(owner, line)
      if (res.ok) {
        fresh.delivered = true
        fresh.attempts = tr.attempts
        writeJob(jobPath, fresh)
        return "delivered"
      }
      // failed send: persist attempts, may orphan next tick
      fresh.attempts = tr.attempts
      try { writeJob(jobPath, fresh) } catch {}
      if (tr.attempts >= MAX_ATTEMPTS) {
        await orphanJob(jobPath, fresh, "attempts")
        tr.orphaned = true
        return "orphaned"
      }
      return "queued"
    } finally {
      releaseLock(jobPath)
    }
  }

  async function orphanJob(jobPath: string, job: Job, reason: string) {
    try {
      ensureDirs()
      const name = jobPath.split("/").pop() || (String(job?.job || "job") + ".json")
      const dest = join(ORPHANS_DIR, name)
      try { renameSync(jobPath, dest) } catch { try { atomicWrite(dest, JSON.stringify(job)); unlinkSync(jobPath) } catch {} }
      audit({ event: "orphan", job: job?.job || name, reason, owner: job?.owner || "", card: job?.card || "", path: dest })
    } catch (e: any) {
      audit({ event: "orphan_error", job: job?.job, reason, error: String(e?.message || e) })
    }
  }

  async function scanOnce() {
    try {
      ensureDirs()
      const files = readdirSync(JOBS_DIR).filter((f) => f.endsWith(".json"))
      for (const f of files) {
        const p = join(JOBS_DIR, f)
        const job = readJob(p)
        if (!job) continue // unreadable → ignore this tour
        await tryDeliver(p, job)
      }
    } catch {}
  }

  return { jobToSession, sessionIdle, trackers, noteBinding, ownerFor, markIdle, isAtRest, tryDeliver, scanOnce, orphanJob }
}

function resultText(input: any): string {
  if (!input) return ""
  const parts = [input.result, input.output, input.response, input.content]
  for (const p of parts) {
    if (typeof p === "string" && p) return p
    if (p && typeof p === "object") {
      try { const s = JSON.stringify(p); if (s && s !== "{}") return s } catch {}
      if (typeof p.text === "string") return p.text
      if (typeof p.content === "string") return p.content
    }
  }
  return ""
}

export default {
  id: "worker-tool",
  async setup(ctx: any) {
    ensureDirs()
    const runtime = createRuntime({
      send: (sessionID, text) => sendPrompt(ctx, sessionID, text),
    })
    let stopped = false
    let controller = new AbortController()

    const watchLoop = async () => {
      while (!stopped) {
        await runtime.scanOnce().catch(() => {})
        await new Promise((r) => setTimeout(r, 1000))
      }
    }
    watchLoop().catch(() => {})

    const runEvents = async () => {
      try {
        for await (const ev of ctx.event.subscribe({ signal: controller.signal })) {
          const type = String(ev?.type || "")
          const sid = String(ev?.data?.sessionID || ev?.data?.id || ev?.properties?.sessionID || "")
          if (!sid) continue
          if (type === "session.idle" || type === "session.status" && String(ev?.data?.status?.type || ev?.data?.status || ev?.properties?.status?.type || "").toLowerCase() === "idle") {
            runtime.markIdle(sid, true)
            runtime.scanOnce().catch(() => {})
          } else if (type === "session.status") {
            const st = String(ev?.data?.status?.type || ev?.data?.status || ev?.properties?.status?.type || "").toLowerCase()
            if (st && st !== "idle") runtime.markIdle(sid, false)
          } else if (type === "session.execution.started" || type === "message.updated" && ev?.data?.role === "user") {
            runtime.markIdle(sid, false)
          } else if (type === "session.deleted") {
            runtime.sessionIdle.delete(sid)
          }
        }
      } catch {}
    }
    runEvents().catch(() => {})

    await ctx.tool.hook("execute.before", async (input: any) => {
      try {
        try {
          const _tn = String(input?.tool || "")
          if (/flow/i.test(_tn)) appendFileSync("/tmp/opencode/toolname.log", JSON.stringify({tool: _tn, norm: normTool(_tn), match: isOurTool(_tn)}) + "\n")
        } catch {}
        if (!isOurTool(String(input?.tool || ""))) return
        const sessionID = String(input?.sessionID || "")
        if (!sessionID) return
        const args = input.input && typeof input.input === "object" ? input.input : null
        if (args) args.session_id = sessionID
        if (input.args && typeof input.args === "object") input.args.session_id = sessionID
        const projectDir = String(ctx?.location?.directory || process.env.ORCH_PROJECT || process.cwd())
        const card = readLaunchedCard(projectDir, sessionID)
        if (card) {
          if (args) args.card = card
          if (input.args && typeof input.args === "object") input.args.card = card
        }
        // zero or multiple → inject nothing, let server refuse
      } catch {}
    })

    await ctx.tool.hook("execute.after", async (input: any) => {
      try {
        if (!isOurTool(String(input?.tool || ""))) return
        const sessionID = String(input?.sessionID || "")
        if (!sessionID) return
        const job = extractLaunchedJob(resultText(input))
        if (!job) return
        // Check owner from job file if it exists
        let jobOwner = ""
        try {
          const jp = join(JOBS_DIR, job + ".json")
          if (existsSync(jp)) jobOwner = String(readJob(jp)?.owner || "")
        } catch {}
        runtime.noteBinding(job, sessionID, jobOwner)
        runtime.scanOnce().catch(() => {})
      } catch {}
    })

    return () => { stopped = true; try { controller.abort() } catch {} }
  },
}
