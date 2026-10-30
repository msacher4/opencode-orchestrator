// runs-logic.ts — module pur, ZÉRO import
// Exporte : RunRow, RunGroup, buildRuns, formatAge, summarize
// Signatures et règles exactes de la spec §runs-logic.ts

export type RunRow = {
  id: string
  parentID?: string
  agent: string      // "?" si absent
  model: string      // "provider/id" ou "?" si absent
  title: string
  updated: number    // ms epoch
  running: boolean
}

export type RunGroup = {
  root: RunRow | null      // null => groupe orphelin
  parentID: string
  parentTitle: string      // "?" si le parent n'est pas dans la liste
  children: RunRow[]
  updated: number          // max(updated) du groupe
}

/**
 * Construit les groupes de sessions à afficher.
 *
 * Règles (spec) :
 * 1. running = Boolean(active[s.id] && active[s.id].type === "running")
 * 2. all === false -> ne garder que les sessions running. all === true -> tout garder.
 * 3. Regroupement :
 *    - Vraies racines : sessions sans parentID -> groupe avec root = session
 *    - Parent de contexte : enfant retenu dont le parent existe dans sessions
 *      mais n'est PAS retenu -> parent affiché comme root avec running:false
 *    - Orphelin strict : enfant retenu dont le parent est ABSENT de sessions ->
 *      root: null, parentID renseigné, parentTitle: "(parent hors liste)"
 * 4. Tri : groupes par updated décroissant ; enfants par updated décroissant. Tri stable.
 * 8. Jamais d'exception sur entrée malformée : champs manquants -> "?", tableaux vides -> [].
 */
export function buildRuns(input: {
  sessions: any[]
  active: Record<string, any>
  now: number
  all: boolean
}): RunGroup[] {
  // --- Entrées robustes ---
  const sessions = Array.isArray(input.sessions) ? input.sessions : []
  const active = input.active && typeof input.active === "object" ? input.active : {}
  const now = typeof input.now === "number" && isFinite(input.now) ? input.now : Date.now()
  const all = Boolean(input.all)

  // --- Étape 1 : normaliser TOUTES les sessions en RunRow ---
  const allRows: RunRow[] = sessions.map((s: any) => {
    const id = typeof s?.id === "string" && s.id ? s.id : "?"
    const parentID = typeof s?.parentID === "string" && s.parentID ? s.parentID : undefined
    const agent = typeof s?.agent === "string" && s.agent ? s.agent : "?"
    const model = (() => {
      const m = s?.model
      if (m && typeof m === "object") {
        const provider = typeof m.providerID === "string" ? m.providerID : (typeof m.provider === "string" ? m.provider : "")
        const mid = typeof m.id === "string" ? m.id : ""
        if (provider && mid) return `${provider}/${mid}`
        if (mid) return mid
      }
      return "?"
    })()
    const title = typeof s?.title === "string" && s.title ? s.title : "?"
    const updated = typeof s?.time?.updated === "number" && isFinite(s.time.updated)
      ? s.time.updated
      : (typeof s?.time?.created === "number" && isFinite(s.time.created) ? s.time.created : 0)
    const running = Boolean(active[id] && active[id].type === "running")
    return { id, parentID, agent, model, title, updated, running }
  })

  // Index par id pour lookup O(1)
  const allById = new Map<string, RunRow>()
  for (const r of allRows) {
    allById.set(r.id, r)
  }

  // --- Étape 2 : sessions retenues pour l'affichage ---
  const retained = new Set<string>()
  for (const r of allRows) {
    if (all || r.running) {
      retained.add(r.id)
    }
  }

  // --- Étape 3 : map parentID -> enfants (toutes sessions) ---
  const childrenMap = new Map<string, RunRow[]>()
  for (const r of allRows) {
    if (r.parentID && allById.has(r.parentID)) {
      const arr = childrenMap.get(r.parentID) || []
      arr.push(r)
      childrenMap.set(r.parentID, arr)
    }
  }

  // --- Étape 4 : identifier vraies racines (sans parentID) et orphelins (parentID absent de sessions) ---
  const trueRoots = allRows.filter(r => !r.parentID)
  const orphans = allRows.filter(r => r.parentID && !allById.has(r.parentID))

  const groups: RunGroup[] = []

  // --- Étape 5 : traiter les vraies racines ---
  for (const root of trueRoots) {
    const allChildren = childrenMap.get(root.id) || []
    const retainedChildren = allChildren.filter(c => retained.has(c.id))
    const rootRetained = retained.has(root.id)

    // Groupe affiché si racine retenue OU enfants retenus
    if (rootRetained || retainedChildren.length > 0) {
      let groupUpdated = root.updated
      for (const c of retainedChildren) {
        if (c.updated > groupUpdated) groupUpdated = c.updated
      }

      if (rootRetained) {
        // Racine normale : affichée avec son running réel
        groups.push({
          root,
          parentID: root.id,
          parentTitle: root.title,
          children: retainedChildren,
          updated: groupUpdated,
        })
      } else {
        // Parent de contexte : racine non retenue mais a des enfants retenus
        // Affichée comme root avec running:false, ne compte pas comme ligne active
        const contextRoot: RunRow = {
          ...root,
          running: false,
        }
        groups.push({
          root: contextRoot,
          parentID: root.id,
          parentTitle: root.title,
          children: retainedChildren,
          updated: groupUpdated,
        })
      }
    }
  }

  // --- Étape 6 : traiter les orphelins (groupés par parentID) ---
  const orphanGroups = new Map<string, RunGroup>()

  for (const orphan of orphans) {
    if (!retained.has(orphan.id)) continue // orphelin non retenu -> pas affiché

    // Chercher le titre du parent dans TOUTES les sessions originales
    let parentTitle = "(parent hors liste)"
    for (const s of sessions) {
      if (typeof s?.id === "string" && s.id === orphan.parentID && typeof s?.title === "string" && s.title) {
        parentTitle = s.title
        break
      }
    }

    const key = orphan.parentID || ""
    const existing = orphanGroups.get(key)
    if (existing) {
      existing.children.push(orphan)
      if (orphan.updated > existing.updated) existing.updated = orphan.updated
    } else {
      orphanGroups.set(key, {
        root: null,
        parentID: orphan.parentID || "",
        parentTitle,
        children: [orphan],
        updated: orphan.updated,
      })
    }
  }

  for (const g of orphanGroups.values()) {
    groups.push(g)
  }

  // --- Étape 7 : rattacher les sessions retenues non groupées (règle 5bis) ---
  // Collecter tous les IDs déjà dans des groupes (roots + children)
  const groupedIds = new Set<string>()
  for (const g of groups) {
    if (g.root) groupedIds.add(g.root.id)
    for (const c of g.children) groupedIds.add(c.id)
  }

  // Sessions retenues mais absentes de tout groupe → groupes orphelins
  const ungroupedRetained = allRows.filter(r => retained.has(r.id) && !groupedIds.has(r.id))
  const ungroupedGroups = new Map<string, RunGroup>()

  for (const u of ungroupedRetained) {
    // parentID direct de cette session
    const parentID = u.parentID || ""
    // Chercher le titre du parent direct dans TOUTES les sessions
    let parentTitle = "(parent hors liste)"
    if (parentID) {
      for (const s of sessions) {
        if (typeof s?.id === "string" && s.id === parentID && typeof s?.title === "string" && s.title) {
          parentTitle = s.title
          break
        }
      }
    }

    const existing = ungroupedGroups.get(parentID)
    if (existing) {
      existing.children.push(u)
      if (u.updated > existing.updated) existing.updated = u.updated
    } else {
      ungroupedGroups.set(parentID, {
        root: null,
        parentID,
        parentTitle,
        children: [u],
        updated: u.updated,
      })
    }
  }

  for (const g of ungroupedGroups.values()) {
    groups.push(g)
  }

  // --- Étape 8 : tri stable ---
  // Enfants par updated décroissant
  for (const group of groups) {
    group.children.sort((a, b) => b.updated - a.updated)
  }
  // Groupes par updated décroissant
  groups.sort((a, b) => b.updated - a.updated)

  return groups
}

/**
 * Formate un âge en millisecondes (durée) vers string lisible.
 * Règles (spec, test) :
 * - < 1 min -> "à l'instant"
 * - < 60 min -> "N min"
 * - < 24 h -> "N h"
 * - sinon -> "N j" (arrondi bas)
 * Note : le paramètre est une DURÉE en ms (pas un timestamp epoch), conforme au test de la spec.
 */
export function formatAge(ms: number): string {
  if (typeof ms !== "number" || !isFinite(ms)) return "?"
  if (ms < 0) return "à l'instant"
  const mins = Math.floor(ms / 60000)
  if (mins < 1) return "à l'instant"
  if (mins < 60) return `${mins} min`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs} h`
  const days = Math.floor(hrs / 24)
  return `${days} j`
}

/**
 * Résume les groupes pour l'en-tête.
 * total = nombre exact de lignes affichées = (1 si root !== null) + children.length
 *   - parents de contexte INCLUS (root !== null donc comptent pour 1)
 *   - groupes orphelins (root === null) : seulement children.length lignes
 * running = nombre de lignes avec running === true (roots + children)
 */
export function summarize(groups: RunGroup[]): { total: number; running: number } {
  let total = 0
  let running = 0
  for (const g of groups) {
    if (g.root !== null) {
      total++
      if (g.root.running) running++
    }
    for (const c of g.children) {
      total++
      if (c.running) running++
    }
  }
  return { total, running }
}