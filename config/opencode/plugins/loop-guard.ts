// loop-guard — OpenCode v2 plugin
//
// AVERTISSEUR, PAS COUPE-CIRCUIT. Mode par défaut : LOOP_GUARD_MODE=nudge.
// Trois déclencheurs indépendants :
//   A) mêmes arguments exacts répétés (LOOP_GUARD_THRESHOLD, défaut 3)
//   C) même outil en échec avec la même empreinte d'erreur (3 fois de suite)
//   D) budget d'appels sans idle (LOOP_GUARD_BUDGET, défaut 200)
//
// Réaction : un message `synthetic` (delivery "steer") injecté dans la session,
// + une seule ligne d'audit par déclenchement. AUCUN throw, AUCUN appel refusé.
// LOOP_GUARD_MODE=block restaure l'ancien coupe-circuit (throw) ; =off rend le
// plugin totalement inerte.
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
// Exception cloak-agent / webnav : browse, snapshot et act réémettent légitimement des
// appels aux arguments proches pendant une navigation. Sans cette exception, le
// déclencheur A coupe webnav en pleine session (constaté le 2026-10-29).
const IGNORE_AGENTS = /^webnav$/i
const IGNORE_TOOL_PATTERNS = /cloak/i

const DEFAULT_THRESHOLD = 3
const DEFAULT_BUDGET = 200
const DEFAULT_COOLDOWN_MS = 60000
const ESCALATED_COOLDOWN_MS = 120000
const ESCALATE_AT = 3
const ARGS_LIMIT = 120
// idleMs 10 min : filet pour le cas où aucun événement de reset n'est émis.
const DEFAULT_IDLE_MS = 600000

// Erreurs d'argumentation : appel mal formé, pas une boucle. On ne compte pas.
const ARGUMENT_ERROR = /(path|include|not found|no such file|is not a directory|enoent|enotdir|invalid)/i

function envNumber(name, fallback) {
  const raw = process.env[name]
  if (raw === undefined || raw === null || String(raw).trim() === "") return fallback
  const n = Number(raw)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

// "nudge" (défaut) | "block" (ancien comportement) | "off" (inerte)
export function guardMode() {
  const m = String(process.env.LOOP_GUARD_MODE || "").trim().toLowerCase()
  if (m === "off") return "off"
  if (m === "block") return "block"
  return "nudge"
}

function resolveMode(explicit) {
  if (explicit === "off" || explicit === "block" || explicit === "nudge") return explicit
  return guardMode()
}

export function isIgnored(tool, agent) {
  return (
    IGNORE_TOOLS.test(String(tool || "")) ||
    IGNORE_AGENTS.test(String(agent || "")) ||
    IGNORE_TOOL_PATTERNS.test(String(tool || ""))
  )
}

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

function truncate(value, limit) {
  const s = String(value == null ? "" : value)
  return s.length > limit ? s.slice(0, limit - 3) + "..." : s
}

function safeJson(value) {
  try {
    return JSON.stringify(value == null ? {} : value)
  } catch (err) {
    return ""
  }
}

// Empreinte du message d'erreur : texte normalisé, 120 premiers caractères.
export function errorFingerprint(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120)
}

export function isArgumentError(text) {
  return ARGUMENT_ERROR.test(String(text || ""))
}

// Conservé pour les scripts qui l'importent (ancien détecteur A).
export function detectLoop(state, sig, opts) {
  if (state.sig === sig) {
    state.count++
  } else {
    state.sig = sig
    state.count = 1
  }
  const threshold = Number(opts && opts.threshold) || envNumber("LOOP_GUARD_THRESHOLD", DEFAULT_THRESHOLD)
  const blocked = state.count >= threshold
  return { blocked, count: state.count }
}

export function createState() {
  return {
    sig: null,
    count: 0,
    failTool: null,
    failPrint: null,
    failCount: 0,
    calls: 0,
    locked: false,
    reason: null,
    lastAt: 0,
    // état de nudge (par épisode)
    nudges: 0,
    lastNudgeAt: 0,
    successSig: null,
  }
}

function agentRole(agent) {
  return /worker|reviewer|merger/i.test(String(agent || "")) ? "subagent" : "orchestrator"
}

function nudgeText(info, index) {
  const tool = String(info.tool || "")
  if (index >= ESCALATE_AT) {
    const tail =
      agentRole(info.agent) === "subagent"
        ? "ARRÊTE les outils et rends ton contrat de 5 lignes."
        : "ARRÊTE les outils et rends la main."
    return "⛔ loop-guard : tu boucles depuis " + ESCALATE_AT + " avertissements. " + tail
  }
  if (info.reason === "A") {
    return (
      "⚠️ loop-guard : " +
      (Number(info.count) || 0) +
      "e appel identique (" +
      tool +
      " " +
      truncate(info.argsText, ARGS_LIMIT) +
      "). Tu tournes en rond — change d'argument ou de stratégie."
    )
  }
  if (info.reason === "C") {
    return (
      "⚠️ loop-guard : " +
      tool +
      " a échoué " +
      (Number(info.failCount) || 0) +
      " fois de suite avec la même erreur (" +
      truncate(info.detail, ARGS_LIMIT) +
      "). Corrige l'appel, ne le rejoue pas."
    )
  }
  return (
    "⚠️ loop-guard : " +
    (Number(info.calls) || 0) +
    " appels d'outils sans rendre la main. Fais le point et rends ton résultat."
  )
}

// Programme un nudge si le cooldown le permet. Ne lève jamais.
function maybeNudge(state, info) {
  const now = Number(info.now) || Date.now()
  const nudges = Number(state.nudges) || 0
  const last = Number(state.lastNudgeAt) || 0
  const cooldown = nudges >= ESCALATE_AT ? ESCALATED_COOLDOWN_MS : envNumber("LOOP_GUARD_COOLDOWN_MS", DEFAULT_COOLDOWN_MS)
  if (last && now - last < cooldown) return null

  state.nudges = nudges + 1
  state.lastNudgeAt = now
  const index = state.nudges
  const text = nudgeText(info, index)

  try {
    const ctx = info.ctx
    if (ctx && ctx.session && typeof ctx.session.synthetic === "function") {
      const p = ctx.session.synthetic({
        sessionID: info.sessionID,
        text,
        description: "loop-guard",
        delivery: "steer",
      })
      if (p && typeof p.catch === "function") p.catch(() => {})
    }
  } catch (err) {
    // Le nudge ne doit jamais casser l'appel d'outil.
  }

  return { reason: info.reason, text, index }
}

// Observer pur : ne refuse jamais un appel (sauf LOOP_GUARD_MODE=block, où le
// hook appelant décide de lever). Ne lève jamais lui-même.
export function onToolCall(state, tool, sig, opts) {
  const o = opts || {}
  const mode = resolveMode(o.mode)
  const out = { blocked: false, reason: null, count: state.count, calls: state.calls, nudge: null }

  if (mode === "off") return out

  const now = o.now ?? Date.now()
  const idleMs = o.idleMs ?? DEFAULT_IDLE_MS
  const budget = o.budget ?? envNumber("LOOP_GUARD_BUDGET", DEFAULT_BUDGET)
  const threshold = o.threshold ?? envNumber("LOOP_GUARD_THRESHOLD", DEFAULT_THRESHOLD)

  if (now - state.lastAt > idleMs) onIdle(state)
  state.lastAt = now

  // mode block : ancien comportement, un verrou refuse les appels suivants.
  if (mode === "block" && state.locked) {
    out.blocked = true
    out.reason = state.reason
    out.count = state.count
    out.calls = state.calls
    return out
  }

  state.calls++

  if (state.sig === sig) {
    state.count++
  } else {
    state.sig = sig
    state.count = 1
  }

  let reason = null
  if (state.count >= threshold) reason = "A"
  else if (state.calls >= budget) reason = "D"

  if (reason) {
    state.reason = reason
    if (mode === "block") {
      state.locked = true
      out.blocked = true
      out.reason = reason
      out.count = state.count
      out.calls = state.calls
      return out
    }
    out.reason = reason
    out.nudge = maybeNudge(state, {
      reason,
      tool,
      count: state.count,
      calls: state.calls,
      argsText: o.argsText != null ? o.argsText : safeJson(o.args),
      agent: o.agent,
      sessionID: o.sessionID,
      ctx: o.ctx,
      now,
    })
  }

  out.count = state.count
  out.calls = state.calls
  return out
}

// Résultat d'outil. En mode nudge, un échec répété (même outil + même empreinte)
// programme un nudge au 3e ; un succès remet les compteurs A/C à zéro.
export function onToolResult(state, tool, ok, opts) {
  const o = opts || {}
  const mode = resolveMode(o.mode)
  const out = { blocked: false, reason: null, justLocked: false, nudge: null }

  if (mode === "off") return out

  const now = o.now ?? Date.now()

  if (!ok) {
    const text = o.errorText != null ? String(o.errorText) : ""
    const fp = errorFingerprint(text)

    // Erreur d'argumentation ou message inexploitable : ce n'est pas une boucle.
    if (!fp || isArgumentError(text)) {
      state.failTool = null
      state.failPrint = null
      state.failCount = 0
      return out
    }

    if (state.failTool === tool && state.failPrint === fp) {
      state.failCount++
    } else {
      state.failTool = tool
      state.failPrint = fp
      state.failCount = 1
    }

    const failThreshold = o.failThreshold ?? DEFAULT_THRESHOLD
    if (state.failCount >= failThreshold) {
      state.reason = "C"
      if (mode === "block") {
        state.locked = true
        out.blocked = true
        out.reason = "C"
        out.justLocked = true
        return out
      }
      out.reason = "C"
      out.nudge = maybeNudge(state, {
        reason: "C",
        tool,
        failCount: state.failCount,
        calls: state.calls,
        detail: state.failPrint,
        argsText: o.argsText != null ? o.argsText : safeJson(o.args),
        agent: o.agent,
        sessionID: o.sessionID,
        ctx: o.ctx,
        now,
      })
    }
    return out
  }

  // Succès : fin de l'épisode d'échecs.
  state.failTool = null
  state.failPrint = null
  state.failCount = 0

  const sig = o.sig != null ? o.sig : state.sig
  if (sig != null && sig !== state.successSig) {
    // Nouvel épisode : compteurs remis à zéro, cooldown conservé (lastNudgeAt).
    state.successSig = sig
    state.count = 0
    state.nudges = 0
  }
  return out
}

export function onIdle(state) {
  state.sig = null
  state.count = 0
  state.failTool = null
  state.failPrint = null
  state.failCount = 0
  state.calls = 0
  state.locked = false
  state.reason = null
  state.lastAt = 0
  state.nudges = 0
  // lastNudgeAt est conservé : le cooldown est par session, pas par épisode.
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

// Texte d'erreur exploitable pour l'empreinte C.
export function errorText(input) {
  if (!input || typeof input !== "object") return ""
  const candidates = []

  const err = input.error
  if (typeof err === "string") candidates.push(err)
  else if (err && typeof err === "object") {
    ;["message", "title", "text", "name", "value"].forEach((k) => {
      if (typeof err[k] === "string") candidates.push(err[k])
    })
    if (err.data && typeof err.data === "object") {
      ;["message", "title", "text"].forEach((k) => {
        if (typeof err.data[k] === "string") candidates.push(err.data[k])
      })
    }
  }

  const out = input.output
  if (typeof out === "string") candidates.push(out)
  else if (out && typeof out === "object") {
    ;["title", "message", "text", "error"].forEach((k) => {
      if (typeof out[k] === "string") candidates.push(out[k])
    })
  }

  ;["message", "title", "result"].forEach((k) => {
    if (typeof input[k] === "string") candidates.push(input[k])
  })

  const first = candidates.find((c) => typeof c === "string" && c.trim())
  return first ? first.slice(0, 400) : ""
}

function audit(entry) {
  try {
    mkdirSync(dirname(AUDIT_PATH), { recursive: true })
    appendFileSync(AUDIT_PATH, JSON.stringify(entry) + "\n")
  } catch (err) {
    // Ignore audit errors so they never break execution
  }
}

export default {
  id: "loop-guard",
  async setup(ctx) {
    const mode = guardMode()
    if (mode === "off") return () => {}

    let controller = new AbortController()

    const runEvents = async () => {
      try {
        for await (const ev of ctx.event.subscribe({ signal: controller.signal })) {
          const sessionID = String((ev && ev.data && ev.data.sessionID) || "")
          if (!sessionID) continue
          const state = HISTORY.get(sessionID)
          if (!state) continue
          const type = String((ev && ev.type) || "")
          if (
            type === "session.idle" ||
            type === "session.execution.started" ||
            type === "session.execution.succeeded" ||
            type === "session.execution.failed" ||
            type === "session.execution.interrupted"
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
      let refusal = null
      try {
        if (!input || typeof input !== "object") return
        const tool = String(input.tool || "")
        const sessionID = String(input.sessionID || "")
        if (!sessionID) return // plus de repli "default" : sans session, on ne suit rien
        const agent = String(input.agent || "")
        if (isIgnored(tool, agent)) return

        const args = input.input || input.args || {}
        const sig = signature(tool, args)
        const argsText = safeJson(args)

        let state = HISTORY.get(sessionID)
        if (!state) {
          state = createState()
          HISTORY.set(sessionID, state)
        }

        const result = onToolCall(state, tool, sig, {
          ctx,
          sessionID,
          agent,
          now: Date.now(),
          argsText,
        })

        if (result.nudge) {
          audit({
            at: new Date().toISOString(),
            sessionID,
            agent,
            tool,
            reason: result.nudge.reason,
            count: result.count,
            calls: result.calls,
            nudges: state.nudges,
            nudgeIndex: result.nudge.index,
            mode: "nudge",
            input: argsText.slice(0, 400),
          })
        }

        if (result.blocked) {
          // LOOP_GUARD_MODE=block uniquement : ancien coupe-circuit, hors défaut.
          const reason = result.reason
          let detail = ""
          if (reason === "A") detail = " même signature " + result.count + " fois"
          else if (reason === "C") detail = " même outil " + state.failTool + " en échec " + state.failCount + " fois"
          else if (reason === "D") detail = " budget d'appels " + result.calls + " atteint"
          audit({
            at: new Date().toISOString(),
            sessionID,
            agent,
            tool,
            reason,
            count: result.count,
            calls: result.calls,
            nudges: state.nudges,
            nudgeIndex: null,
            mode: "block",
            input: argsText.slice(0, 400),
          })
          refusal =
            "loop-guard : coupe-circuit déclenché (" +
            reason +
            ")." +
            detail +
            ". ARRÊTE d'appeler des outils : rends la main maintenant, en expliquant où tu en es et ce qui bloque."
        }
      } catch (err) {
        // Le hook ne doit jamais casser un appel d'outil.
      }
      if (refusal) throw new Error(refusal)
    })

    await ctx.tool.hook("execute.after", async (input) => {
      try {
        if (!input || typeof input !== "object") return
        const tool = String(input.tool || "")
        const sessionID = String(input.sessionID || "")
        if (!sessionID) return
        const agent = String(input.agent || "")
        if (isIgnored(tool, agent)) return

        const state = HISTORY.get(sessionID)
        if (!state) return

        const args = input.input || input.args || {}
        const ok = !toolFailed(input)
        const result = onToolResult(state, tool, ok, {
          ctx,
          sessionID,
          agent,
          now: Date.now(),
          sig: state.sig,
          errorText: ok ? "" : errorText(input),
          args: args,
        })

        if (result.nudge) {
          audit({
            at: new Date().toISOString(),
            sessionID,
            agent,
            tool,
            reason: result.nudge.reason,
            count: state.failCount,
            calls: state.calls,
            nudges: state.nudges,
            nudgeIndex: result.nudge.index,
            mode: "nudge",
            input: safeJson(args).slice(0, 400),
          })
        } else if (result.blocked) {
          audit({
            at: new Date().toISOString(),
            sessionID,
            agent,
            tool,
            reason: "C",
            count: state.failCount,
            calls: state.calls,
            nudges: state.nudges,
            nudgeIndex: null,
            mode: "block",
            input: safeJson(args).slice(0, 400),
          })
        }
      } catch (err) {
        // Le hook ne doit jamais casser un appel d'outil.
      }
    })

    return () => controller.abort()
  },
}
