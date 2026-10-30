// runs-ui — commande /runs (TUI)
// Affiche les sessions actives (racines + subagents en arbre) avec kill vérifié.
// Suit le modèle orch-ui (ctx.keymap.layer, ctx.ui.dialog, ctx.ui.slot).
import { Plugin } from "@opencode/plugin/tui"
import { buildRuns, formatAge, summarize, type RunRow, type RunGroup } from "./runs-logic.ts"
import { buildAuth, parseService, mergeActive, instanceFor, type Instance } from "./instances.ts"
import { readFileSync } from "node:fs"

const SERVICE_JSON = "/media/marcs/Linux_Apps/Projets_AI/Opencodev2/state/opencode/service.json"

type Ctx = any

function shortID(id: string): string {
  return `ses_${String(id || "").replace(/^ses_/, "").slice(0, 8)}…`
}

function shortKey(id: string): string {
  return `ses_${String(id || "").replace(/^ses_/, "").slice(0, 8)}`
}

function buildSessionLine(row: RunRow, isChild: boolean, parentTitle?: string): string {
  const dot = row.running ? "●" : "·"
  const agent = row.agent || "?"
  const model = row.model || "?"
  const age = formatAge(Date.now() - row.updated)
  const title = (row.title || "?").slice(0, 48)
  const prefix = isChild ? "  └ " : ""
  return `${prefix}${dot} ${agent} · ${model} · il y a ${age} · ${title}`
}

function buildDescription(row: RunRow, parentTitle?: string, instanceLabel?: string): string {
  const idShort = shortID(row.id)
  let desc = idShort
  if (parentTitle) {
    desc += ` — parent: ${shortID(row.parentID || "?")} "${parentTitle}"`
  }
  if (instanceLabel) {
    desc += ` (via ${instanceLabel})`
  }
  return desc
}

/* ───────── API helpers ───────── */

// Base API : client.baseURL, client.options?.baseURL, OPENCODE_BASE_URL, défaut 127.0.0.1:4096
function apiBase(ctx: Ctx): string {
  const c: any = ctx?.client
  const fromClient = c && (c.baseURL || c.baseUrl || (c.options && c.options.baseURL))
  if (typeof fromClient === "string" && fromClient) return fromClient.replace(/\/+$/, "")
  const env = process.env.OPENCODE_BASE_URL
  if (typeof env === "string" && env) return env.replace(/\/+$/, "")
  return "http://127.0.0.1:4096"
}

// Découverte de l'instance daemon (service.json)
async function discoverServiceInstance(): Promise<Instance | null> {
  let raw: string | null = null
  try {
    raw = readFileSync(SERVICE_JSON, "utf8")
  } catch {
    return null
  }
  return parseService(raw)
}

// Récupère /api/session/active depuis une instance distante
async function fetchActiveFromInstance(inst: Instance): Promise<Record<string, any>> {
  if (!inst.auth) return {}
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 1500)
  try {
    const res = await fetch(`${inst.base}/api/session/active`, {
      method: "GET",
      headers: { authorization: inst.auth },
      signal: controller.signal,
    })
    if (!res.ok) return {}
    const data = await res.json()
    // La réponse peut être {data:{...}} ou l'objet direct
    return data && typeof data === "object" && data.data ? data.data : data
  } catch {
    return {}
  } finally {
    clearTimeout(timeout)
  }
}

// Interrupt routé vers l'instance propriétaire
async function interruptSession(
  ctx: Ctx,
  sessionID: string,
  targetInstance: Instance,
  ownInstance: Instance
): Promise<{ ok: boolean; via: string; error?: string }> {
  // 1. Instance cible = instance courante → SDK puis fallback REST local
  if (targetInstance.id === ownInstance.id) {
    try {
      const ses: any = ctx?.client?.session
      const interruptFn = ses?.interrupt
      if (typeof interruptFn === "function") {
        try {
          const res: any = await interruptFn.call(ses, { sessionID })
          if (res && typeof res === "object" && res.error) {
            return { ok: false, via: "sdk", error: String(res.error) }
          }
          return { ok: true, via: "sdk" }
        } catch (err) {
          const reason = err instanceof Error ? err.message : String(err)
          try {
            const base = apiBase(ctx)
            const res = await fetch(`${base}/api/session/${encodeURIComponent(sessionID)}/interrupt`, { method: "POST" })
            if (!res.ok) return { ok: false, via: "rest-local", error: `HTTP ${res.status} (après échec SDK : ${reason})` }
            return { ok: true, via: "rest-local" }
          } catch (err2) {
            const reason2 = err2 instanceof Error ? err2.message : String(err2)
            return { ok: false, via: "rest-local", error: `${reason} / ${reason2}` }
          }
        }
      }
      // Pas de méthode SDK -> fallback REST local
      const base = apiBase(ctx)
      const res = await fetch(`${base}/api/session/${encodeURIComponent(sessionID)}/interrupt`, { method: "POST" })
      if (!res.ok) return { ok: false, via: "rest-local", error: `HTTP ${res.status}` }
      return { ok: true, via: "rest-local" }
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err)
      return { ok: false, via: "unknown", error: reason }
    }
  }

  // 2. Instance cible = daemon/service → POST vers cette instance
  try {
    const res = await fetch(`${targetInstance.base}/api/session/${encodeURIComponent(sessionID)}/interrupt`, {
      method: "POST",
      headers: targetInstance.auth ? { authorization: targetInstance.auth } : {},
    })
    if (!res.ok) return { ok: false, via: `rest-${targetInstance.id}`, error: `HTTP ${res.status}` }
    return { ok: true, via: `rest-${targetInstance.id}` }
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err)
    // Dernier recours : fallback SDK/REST local
    try {
      const ses: any = ctx?.client?.session
      const interruptFn = ses?.interrupt
      if (typeof interruptFn === "function") {
        try {
          const res: any = await interruptFn.call(ses, { sessionID })
          if (res && typeof res === "object" && res.error) {
            return { ok: false, via: "sdk-fallback", error: String(res.error) }
          }
          return { ok: true, via: "sdk-fallback" }
        } catch { /* ignore, essayer REST local */ }
      }
      const base = apiBase(ctx)
      const res2 = await fetch(`${base}/api/session/${encodeURIComponent(sessionID)}/interrupt`, { method: "POST" })
      if (!res2.ok) return { ok: false, via: "rest-local-fallback", error: `HTTP ${res2.status} (fallback après erreur daemon: ${reason})` }
      return { ok: true, via: "rest-local-fallback" }
    } catch (err2) {
      const reason2 = err2 instanceof Error ? err2.message : String(err2)
      return { ok: false, via: "fallback-failed", error: `${reason} / ${reason2}` }
    }
  }
}

// Vérifie si une session est encore active sur une instance donnée
async function checkStillActive(sessionID: string, inst: Instance): Promise<boolean> {
  try {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 1500)
    const res = await fetch(`${inst.base}/api/session/active`, {
      method: "GET",
      headers: inst.auth ? { authorization: inst.auth } : {},
      signal: controller.signal,
    })
    clearTimeout(timeout)
    if (!res.ok) return false
    const data = await res.json()
    const active = data && typeof data === "object" && data.data ? data.data : data
    return Boolean(active && active[sessionID] && active[sessionID].type === "running")
  } catch {
    return false
  }
}

/* ───────── dialog helpers ───────── */

async function openRunsDialog(ctx: Ctx, all: boolean) {
  // Instance courante (client)
  const ownInstance: Instance = {
    id: "local",
    base: apiBase(ctx),
    auth: null, // Le client SDK porte déjà l'auth
  }

  for (;;) {
    ctx.ui.dialog.set({ size: "large" })

    // Charger sessions (global) + active (fusion multi-instance)
    let sessions: any[] = []
    let mergedActive: Record<string, any> = {}
    let owner: Record<string, string> = {}
    let serviceInstance: Instance | null = null

    try {
      // 1. Sessions globales (toujours depuis le client)
      const client: any = ctx?.client
      if (client?.session?.list) {
        const listRes = await client.session.list({ limit: 500 })
        sessions = Array.isArray(listRes?.data) ? listRes.data : (Array.isArray(listRes) ? listRes : [])
      }

      // 2. Active local (instance courante)
      let ownActive: Record<string, any> = {}
      if (client?.session?.active) {
        const activeRes = await client.session.active()
        ownActive = activeRes && typeof activeRes === "object" ? activeRes : {}
        // Accepter {data:{...}} ou objet direct
        if (ownActive.data && typeof ownActive.data === "object") {
          ownActive = ownActive.data
        }
      }

      // 3. Découvrir et interroger le daemon
      serviceInstance = await discoverServiceInstance()
      let others: Array<{ id: string; active: Record<string, any> }> = []
      if (serviceInstance) {
        const svcActive = await fetchActiveFromInstance(serviceInstance)
        if (Object.keys(svcActive).length > 0) {
          others.push({ id: serviceInstance.id, active: svcActive })
        }
      }

      // 4. Fusionner avec mergeActive
      const merged = mergeActive({
        ownID: ownInstance.id,
        own: ownActive,
        others,
      })
      owner = merged.owner
      // Convertir owner → active pour buildRuns
      mergedActive = {}
      for (const sid of merged.running) {
        mergedActive[sid] = { type: "running" }
      }
    } catch {
      // Ignorer erreurs de chargement, on affiche ce qu'on a
    }

    const groups = buildRuns({ sessions, active: mergedActive, now: Date.now(), all })
    const summary = summarize(groups)

    // Construire options pour dialog.select
    const options: any[] = []

    // 1. Ligne d'en-tête désactivée
    const activeCount = summary.running
    options.push({
      title: `● ${activeCount} actives · ${summary.total} sessions`,
      value: { kind: "header" },
      disabled: true,
    })

    // 2. Groupes : racine puis enfants préfixés "  └ "
    let lineCount = 0
    const MAX_LINES = all ? 50 : 200 // all a un plafond de 50 lignes

    for (const g of groups) {
      if (lineCount >= MAX_LINES) break

      if (g.root) {
        // Racine
        const rootInstanceLabel = owner[g.root.id] && owner[g.root.id] !== ownInstance.id ? owner[g.root.id] : undefined
        const opt: any = {
          title: buildSessionLine(g.root, false),
          value: { kind: "session", sessionID: g.root.id, groupParentID: g.parentID },
          description: buildDescription(g.root, undefined, rootInstanceLabel),
          category: shortKey(g.parentID),
        }
        options.push(opt)
        lineCount++

        // Enfants
        for (const child of g.children) {
          if (lineCount >= MAX_LINES) break
          const childInstanceLabel = owner[child.id] && owner[child.id] !== ownInstance.id ? owner[child.id] : undefined
          const childOpt: any = {
            title: buildSessionLine(child, true, g.root.title),
            value: { kind: "session", sessionID: child.id, groupParentID: g.parentID },
            description: buildDescription(child, g.root.title, childInstanceLabel),
            category: shortKey(g.parentID),
          }
          options.push(childOpt)
          lineCount++
        }
      } else {
        // Groupe orphelin : ligne "(orphelin) parent — titre"
        if (lineCount >= MAX_LINES) break
        const firstChild = g.children[0]
        const orphanInstanceLabel = owner[firstChild?.id] && owner[firstChild?.id] !== ownInstance.id ? owner[firstChild?.id] : undefined
        const orphanTitle = `(orphelin) ${g.parentTitle} — ${firstChild?.title || "?"}`
        const opt: any = {
          title: `${firstChild?.running ? "●" : "·"} ${orphanTitle.slice(0, 60)}`,
          value: { kind: "session", sessionID: firstChild?.id, groupParentID: g.parentID },
          description: buildDescription(firstChild, g.parentTitle, orphanInstanceLabel),
          category: shortKey(g.parentID),
        }
        options.push(opt)
        lineCount++

        // Enfants supplémentaires de l'orphelin
        for (let i = 1; i < g.children.length; i++) {
          if (lineCount >= MAX_LINES) break
          const child = g.children[i]
          const childInstanceLabel = owner[child.id] && owner[child.id] !== ownInstance.id ? owner[child.id] : undefined
          const childOpt: any = {
            title: buildSessionLine(child, true, g.parentTitle),
            value: { kind: "session", sessionID: child.id, groupParentID: g.parentID },
            description: buildDescription(child, g.parentTitle, childInstanceLabel),
            category: shortKey(g.parentID),
          }
          options.push(childOpt)
          lineCount++
        }
      }
    }

    // 3. "⛔ Tout arrêter (N)" si plus d'une session active
    const runningSessions: string[] = []
    for (const g of groups) {
      if (g.root && g.root.running) runningSessions.push(g.root.id)
      for (const c of g.children) if (c.running) runningSessions.push(c.id)
    }
    if (runningSessions.length > 1) {
      options.push({
        title: `⛔ Tout arrêter (${runningSessions.length})`,
        value: { kind: "kill-all", sessionIDs: runningSessions },
        description: `Interrompt ${runningSessions.length} sessions actives`,
      })
    }

    // 4. "Annuler"
    options.push({ title: "Annuler", value: { kind: "cancel" } })

    if (options.length <= 2) {
      // Seulement header + cancel
      options.splice(1, 0, { title: "aucune session", value: { kind: "empty" }, disabled: true })
    }

    const choice = await ctx.ui.dialog.select<any>({
      title: all ? "Runs — toutes les sessions (max 50)" : "Runs — sessions actives",
      options,
      search: (q: string, opts: any[]) => {
        const s = q.trim().toLowerCase()
        if (!s) return opts
        return opts.filter(o => `${o.title} ${o.description ?? ""}`.toLowerCase().includes(s))
      },
    })

    if (!choice || typeof choice !== "object") return

    if (choice.kind === "cancel" || choice.kind === "empty" || choice.kind === "header") {
      return
    }

    if (choice.kind === "kill-all") {
      await handleKillAll(ctx, choice.sessionIDs, owner, ownInstance, serviceInstance)
      continue
    }

    if (choice.kind === "session") {
      await handleKillOne(ctx, choice.sessionID, owner, ownInstance, serviceInstance)
      continue
    }
  }
}

async function handleKillOne(
  ctx: Ctx,
  sessionID: string,
  owner: Record<string, string>,
  ownInstance: Instance,
  serviceInstance: Instance | null
) {
  // Confirmation obligatoire
  const confirm = await ctx.ui.dialog.select<string>({
    title: `Confirmer l'arrêt de ${shortID(sessionID)} ?`,
    options: [
      { title: "⛔ Kill cette session", value: "confirm" },
      { title: "Annuler", value: "cancel" },
    ],
  })
  if (confirm !== "confirm") return

  // Déterminer l'instance cible
  const targetInstanceID = instanceFor(sessionID, owner, ownInstance.id)
  const targetInstance = targetInstanceID === ownInstance.id ? ownInstance : (serviceInstance?.id === targetInstanceID ? serviceInstance : ownInstance)

  // Interrupt
  const result = await interruptSession(ctx, sessionID, targetInstance, ownInstance)

  // Re-lire active() pour vérifier (sur l'instance cible + locale)
  let stillActive = false
  try {
    // Vérifier sur l'instance cible
    if (targetInstance.id !== ownInstance.id && targetInstance.auth) {
      stillActive = await checkStillActive(sessionID, targetInstance)
    }
    // Vérifier aussi en local (double vérification)
    if (!stillActive) {
      const client: any = ctx?.client
      if (client?.session?.active) {
        const activeRes = await client.session.active()
        const active = activeRes && typeof activeRes === "object" ? activeRes : {}
        const localActive = active.data && typeof active.data === "object" ? active.data : active
        stillActive = Boolean(localActive[sessionID] && localActive[sessionID].type === "running")
      }
    }
  } catch { /* ignore */ }

  if (result.ok && !stillActive) {
    await ctx.ui.dialog.alert({
      title: "Session arrêtée",
      message: `${shortID(sessionID)} interrompue via ${result.via}.`,
    })
  } else if (result.ok && stillActive) {
    await ctx.ui.dialog.alert({
      title: "Échec",
      message: `${shortID(sessionID)} : toujours active après interrupt (${result.via}).`,
    })
  } else {
    await ctx.ui.dialog.alert({
      title: "Échec",
      message: `${shortID(sessionID)} : ${result.error || "erreur inconnue"}`,
    })
  }
}

async function handleKillAll(
  ctx: Ctx,
  sessionIDs: string[],
  owner: Record<string, string>,
  ownInstance: Instance,
  serviceInstance: Instance | null
) {
  // Confirmation obligatoire
  const confirm = await ctx.ui.dialog.select<string>({
    title: `Confirmer l'arrêt de ${sessionIDs.length} sessions ?`,
    options: [
      { title: `⛔ Tout arrêter (${sessionIDs.length})`, value: "confirm" },
      { title: "Annuler", value: "cancel" },
    ],
  })
  if (confirm !== "confirm") return

  let stopped = 0
  let failed = 0
  const failures: string[] = []

  for (const sessionID of sessionIDs) {
    const targetInstanceID = instanceFor(sessionID, owner, ownInstance.id)
    const targetInstance = targetInstanceID === ownInstance.id ? ownInstance : (serviceInstance?.id === targetInstanceID ? serviceInstance : ownInstance)

    const result = await interruptSession(ctx, sessionID, targetInstance, ownInstance)

    // Re-lire active() pour vérifier (sur l'instance cible + locale)
    let stillActive = false
    try {
      if (targetInstance.id !== ownInstance.id && targetInstance.auth) {
        stillActive = await checkStillActive(sessionID, targetInstance)
      }
      if (!stillActive) {
        const client: any = ctx?.client
        if (client?.session?.active) {
          const activeRes = await client.session.active()
          const active = activeRes && typeof activeRes === "object" ? activeRes : {}
          const localActive = active.data && typeof active.data === "object" ? active.data : active
          stillActive = Boolean(localActive[sessionID] && localActive[sessionID].type === "running")
        }
      }
    } catch { /* ignore */ }

    if (result.ok && !stillActive) {
      stopped++
    } else {
      failed++
      const msg = stillActive ? "toujours active après interrupt" : (result.error || "erreur inconnue")
      failures.push(`${shortID(sessionID)}: ${msg}`)
    }
  }

  let message = `${stopped} arrêtées, ${failed} échecs`
  if (failures.length) message += "\n\n" + failures.join("\n")

  await ctx.ui.dialog.alert({
    title: failed ? "Résultat partiel" : "Toutes arrêtées",
    message,
  })
}

/* ───────── plugin definition ───────── */

function Commands(ctx: Ctx) {
  ctx.keymap.layer(() => ({
    mode: "global",
    priority: 10,
    commands: [
      {
        id: "runs.open",
        title: "Runs — sessions actives",
        description: "Liste triée des sessions actives (subagents sous leur parent) + kill",
        group: "OpenCode",
        palette: true,
        slash: { name: "runs", arguments: true },
        run: async (input?: string) => {
          const argument = (input ?? "").trim().toLowerCase()
          const all = argument === "all"
          await openRunsDialog(ctx, all)
        },
      },
    ],
  }))
  return null
}

export default Plugin.define({
  id: "runs-ui",
  setup(context) {
    const dispose = context.ui.slot({ append: "app", render: () => Commands(context) })
    return () => {
      if (typeof dispose === "function") dispose()
    }
  },
})