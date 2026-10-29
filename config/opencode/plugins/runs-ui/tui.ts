// runs-ui — commande /runs (TUI)
// Affiche les sessions actives (racines + subagents en arbre) avec kill vérifié.
// Suit le modèle orch-ui (ctx.keymap.layer, ctx.ui.dialog, ctx.ui.slot).
import { Plugin } from "@opencode/plugin/tui"
import { buildRuns, formatAge, summarize, type RunRow, type RunGroup } from "./runs-logic.ts"

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

function buildDescription(row: RunRow, parentTitle?: string): string {
  const idShort = shortID(row.id)
  if (parentTitle) {
    return `${idShort} — parent: ${shortID(row.parentID || "?")} "${parentTitle}"`
  }
  return `${idShort}`
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

// Interrupt via SDK d'abord, puis fallback REST POST /api/session/{id}/interrupt
async function interruptSession(ctx: Ctx, sessionID: string): Promise<{ ok: boolean; via: string; error?: string }> {
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
          if (!res.ok) return { ok: false, via: "rest", error: `HTTP ${res.status} (après échec SDK : ${reason})` }
          return { ok: true, via: "rest" }
        } catch (err2) {
          const reason2 = err2 instanceof Error ? err2.message : String(err2)
          return { ok: false, via: "rest", error: `${reason} / ${reason2}` }
        }
      }
    }
    // Pas de méthode SDK -> fallback REST
    const base = apiBase(ctx)
    const res = await fetch(`${base}/api/session/${encodeURIComponent(sessionID)}/interrupt`, { method: "POST" })
    if (!res.ok) return { ok: false, via: "rest", error: `HTTP ${res.status}` }
    return { ok: true, via: "rest" }
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err)
    return { ok: false, via: "unknown", error: reason }
  }
}

/* ───────── dialog helpers ───────── */

async function openRunsDialog(ctx: Ctx, all: boolean) {
  for (;;) {
    ctx.ui.dialog.set({ size: "large" })

    // Charger sessions + active
    let sessions: any[] = []
    let active: Record<string, any> = {}
    try {
      const client: any = ctx?.client
      if (client?.session?.list) {
        const listRes = await client.session.list({ limit: 500 })
        sessions = Array.isArray(listRes?.data) ? listRes.data : (Array.isArray(listRes) ? listRes : [])
      }
      if (client?.session?.active) {
        const activeRes = await client.session.active()
        active = activeRes && typeof activeRes === "object" ? activeRes : {}
      }
    } catch {
      // Ignorer erreurs de chargement, on affiche ce qu'on a
    }

    const groups = buildRuns({ sessions, active, now: Date.now(), all })
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
        const opt: any = {
          title: buildSessionLine(g.root, false),
          value: { kind: "session", sessionID: g.root.id, groupParentID: g.parentID },
          description: buildDescription(g.root),
          category: shortKey(g.parentID),
        }
        options.push(opt)
        lineCount++

        // Enfants
        for (const child of g.children) {
          if (lineCount >= MAX_LINES) break
          const childOpt: any = {
            title: buildSessionLine(child, true, g.root.title),
            value: { kind: "session", sessionID: child.id, groupParentID: g.parentID },
            description: buildDescription(child, g.root.title),
            category: shortKey(g.parentID),
          }
          options.push(childOpt)
          lineCount++
        }
      } else {
        // Groupe orphelin : ligne "(orphelin) parent — titre"
        if (lineCount >= MAX_LINES) break
        const orphanTitle = `(orphelin) ${g.parentTitle} — ${g.children[0]?.title || "?"}`
        const opt: any = {
          title: `${g.children[0]?.running ? "●" : "·"} ${orphanTitle.slice(0, 60)}`,
          value: { kind: "session", sessionID: g.children[0]?.id, groupParentID: g.parentID },
          description: buildDescription(g.children[0], g.parentTitle),
          category: shortKey(g.parentID),
        }
        options.push(opt)
        lineCount++

        // Enfants supplémentaires de l'orphelin
        for (let i = 1; i < g.children.length; i++) {
          if (lineCount >= MAX_LINES) break
          const child = g.children[i]
          const childOpt: any = {
            title: buildSessionLine(child, true, g.parentTitle),
            value: { kind: "session", sessionID: child.id, groupParentID: g.parentID },
            description: buildDescription(child, g.parentTitle),
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
      await handleKillAll(ctx, choice.sessionIDs)
      continue
    }

    if (choice.kind === "session") {
      await handleKillOne(ctx, choice.sessionID)
      continue
    }
  }
}

async function handleKillOne(ctx: Ctx, sessionID: string) {
  // Confirmation obligatoire
  const confirm = await ctx.ui.dialog.select<string>({
    title: `Confirmer l'arrêt de ${shortID(sessionID)} ?`,
    options: [
      { title: "⛔ Kill cette session", value: "confirm" },
      { title: "Annuler", value: "cancel" },
    ],
  })
  if (confirm !== "confirm") return

  // Interrupt
  const result = await interruptSession(ctx, sessionID)

  // Re-lire active() pour vérifier
  let stillActive = false
  try {
    const client: any = ctx?.client
    if (client?.session?.active) {
      const activeRes = await client.session.active()
      const active = activeRes && typeof activeRes === "object" ? activeRes : {}
      stillActive = Boolean(active[sessionID] && active[sessionID].type === "running")
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

async function handleKillAll(ctx: Ctx, sessionIDs: string[]) {
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
    const result = await interruptSession(ctx, sessionID)

    // Re-lire active() pour vérifier
    let stillActive = false
    try {
      const client: any = ctx?.client
      if (client?.session?.active) {
        const activeRes = await client.session.active()
        const active = activeRes && typeof activeRes === "object" ? activeRes : {}
        stillActive = Boolean(active[sessionID] && active[sessionID].type === "running")
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