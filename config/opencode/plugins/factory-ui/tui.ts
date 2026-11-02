// factory-ui — commande /factory (TUI)
//
// Lit l'unique source de vérité produite par `mcp/opencode-factory/cli.py
// --ui-json` et l'affiche en texte compact. Aucune logique métier ici : le
// backend calcule, le plugin rend. Lecture seule : le plugin n'écrit aucun
// fichier (surtout pas une configuration OpenCode).
import { Plugin } from "@opencode/plugin/tui"
import { execFileSync } from "node:child_process"
import { existsSync } from "node:fs"

const PROJECT = "/media/marcs/Linux_Apps/Projets_AI"
const CLI = `${PROJECT}/mcp/opencode-factory/cli.py`
const PYTHON = process.env.FACTORY_PYTHON || "python3"

// Appels au backend. Le test de contrat
// (mcp/opencode-factory/tests/test_tui_contract.py) lit ce bloc et vérifie que
// chaque option existe réellement dans cli.py.
//
// Actions s/p/m (démarrer/arrêter le bloc, pause, max_sessions) : ABSENTES.
// cli.py n'expose encore aucune sous-commande d'écriture pour ces gestes ; on
// n'en invente pas et on n'affiche donc aucune touche qui ne ferait rien.
const CLI_APPELS: Record<string, string[]> = {
  etat: ["--ui-json"],
}

type Ctx = any
type Section = { valeur: any; erreur: string | null }

/* ───────── Appel du backend ───────── */

function appel(nom: string): any {
  const args = CLI_APPELS[nom]
  if (!args) return { error: `appel inconnu : ${nom}` }
  if (!existsSync(CLI)) return { error: `backend absent : ${CLI}` }
  try {
    const stdout = execFileSync(PYTHON, [CLI, ...args], {
      encoding: "utf8",
      timeout: 20000,
      maxBuffer: 8 * 1024 * 1024,
      cwd: PROJECT,
    })
    return JSON.parse(stdout)
  } catch (error: any) {
    const stderr = String(error?.stderr ?? "").trim()
    const reason = error?.message ?? "échec de l'appel au backend"
    return { error: `${reason}${stderr ? ` — ${stderr}` : ""}` }
  }
}

/* ───────── Rendu : honnêteté avant tout ───────── */

// Une section absente ou illisible vaut {"valeur": null, "erreur": "..."}.
// On affiche « — » + la raison ; jamais 0, jamais une valeur inventée.
function section(data: any, cle: string): Section {
  const s = data && typeof data === "object" ? data[cle] : null
  if (!s || typeof s !== "object") {
    return { valeur: null, erreur: `section « ${cle} » absente du relevé` }
  }
  return {
    valeur: s.valeur ?? null,
    erreur: typeof s.erreur === "string" && s.erreur ? s.erreur : null,
  }
}

function absent(s: Section): string {
  return `—  (${s.erreur || "valeur absente"})`
}

function texte(value: any, repli = "—"): string {
  return typeof value === "string" && value.trim() ? value.trim() : repli
}

function nombre(value: any): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "—"
}

// Minutes -> « 42 min », « 2 h 05 », « 1 j 3 h ». Jamais appelé sur null.
function duree(min: any): string {
  if (typeof min !== "number" || !Number.isFinite(min) || min < 0) return "—"
  if (min < 60) return `${Math.round(min)} min`
  const totalH = Math.floor(min / 60)
  const minutes = Math.round(min % 60)
  if (totalH < 24) return minutes ? `${totalH} h ${String(minutes).padStart(2, "0")}` : `${totalH} h`
  const jours = Math.floor(totalH / 24)
  const heures = totalH % 24
  return heures ? `${jours} j ${heures} h` : `${jours} j`
}

function heure(value: any): string {
  if (typeof value !== "string" || !value) return "—"
  if (/^\d{2}:\d{2}$/.test(value)) return value
  const iso = /T(\d{2}:\d{2})/.exec(value)
  if (iso) return iso[1]
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return "—"
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`
}

function idCourt(value: any): string {
  const id = texte(value, "?")
  return id.length > 16 ? `${id.slice(0, 16)}…` : id
}

/* ───────── Blocs du panneau ───────── */

function lignesBloc(s: Section): string[] {
  if (s.valeur === null) return [`BLOC    ${absent(s)}`]
  const b = s.valeur
  const nom = texte(b.nom ?? b.id)
  const fenetre =
    b.fenetre && b.fenetre.debut && b.fenetre.fin ? `${b.fenetre.debut}→${b.fenetre.fin}` : "fenêtre —"
  const etat = b.actif === true ? "actif" : b.actif === false ? "inactif" : "état —"
  const out = [`BLOC    ${nom}  ${fenetre}  ${etat}`]
  if (b.dans_fenetre === true) {
    out.push(
      `        en fenêtre · ${
        b.temps_restant_min === null ? "reste —" : `reste ${duree(b.temps_restant_min)}`
      }`
    )
  } else if (b.dans_fenetre === false) {
    out.push(
      `        hors fenêtre · ${
        b.prochain_debut_min === null
          ? "prochaine ouverture —"
          : `ouvre dans ${duree(b.prochain_debut_min)}`
      }`
    )
  } else {
    out.push("        fenêtre —")
  }
  return out
}

function lignesSessions(s: Section): string[] {
  if (s.valeur === null) return [`SESSIONS ${absent(s)}`]
  const v = s.valeur
  const items = Array.isArray(v.items) ? v.items : []
  const out = [`SESSIONS ${nombre(v.total)}`]

  const parEquipe = v.par_equipe && typeof v.par_equipe === "object" ? Object.entries(v.par_equipe) : []
  if (parEquipe.length) {
    out.push(`        ${parEquipe.map(([equipe, n]) => `${equipe} ${n}`).join(" · ")}`)
  } else {
    out.push("        par équipe —")
  }

  for (const it of items.slice(0, 6)) {
    const age =
      it && it.age_min === null
        ? `âge —  (${texte(it.erreur, "date de lancement illisible")})`
        : `il y a ${duree(it?.age_min)}`
    out.push(
      `        ${idCourt(it?.id)} · ${texte(it?.regle)} · ${texte(it?.equipe)} · ${age}`
    )
  }
  if (items.length > 6) out.push(`        … ${items.length - 6} de plus`)
  return out
}

function lignesSignaux(s: Section): string[] {
  if (s.valeur === null) return [`SIGNAUX ${absent(s)}`]
  const v = s.valeur
  const items = Array.isArray(v.items) ? v.items : []
  const fenetre = typeof v.fenetre_min === "number" ? ` (${v.fenetre_min} min)` : ""
  const out = [`SIGNAUX ${nombre(v.total)}${fenetre}`]
  if (!items.length) {
    out.push("        aucun")
  } else {
    for (const sig of items.slice(0, 6)) {
      out.push(
        `        ${texte(sig?.gravite, "?")} ${texte(sig?.code, "?")} ${idCourt(sig?.cible)}`
      )
    }
    if (items.length > 6) out.push(`        … ${items.length - 6} de plus`)
  }
  return out
}

function lignesDecision(s: Section): string[] {
  if (s.valeur === null) return [`DÉCISION ${absent(s)}`]
  const d = s.valeur
  const actions = Array.isArray(d.actions) ? `${d.actions.length} action(s)` : "actions —"
  const refus = Array.isArray(d.refus) ? `${d.refus.length} refus` : "refus —"
  const cout = typeof d.cout === "number" && Number.isFinite(d.cout) ? `${d.cout.toFixed(2)} €` : "—"
  const jev = typeof d.jev_consultations === "number" ? `${d.jev_consultations} consult.` : "—"
  return [
    `DÉCISION ${heure(d.at)} · ${texte(d.mode)} · ${actions} · ${refus}`,
    `        coût ${cout} · JEV ${jev}`,
  ]
}

function lignesIncidents(s: Section): string[] {
  if (s.valeur === null) return [`INCIDENTS ${absent(s)}`]
  const v = s.valeur
  const items = Array.isArray(v.items) ? v.items : []
  const fenetre = typeof v.fenetre_h === "number" ? `${v.fenetre_h} h` : "fenêtre —"
  const out = [`INCIDENTS ${nombre(v.total)} sur ${fenetre}`]
  if (!items.length) {
    out.push("        aucun")
  } else {
    for (const inc of items.slice(0, 5)) {
      out.push(
        `        ${heure(inc?.heure)} ${texte(inc?.action, "?")} ${texte(inc?.cible, "?")} · ${texte(inc?.motif, "?")}`
      )
    }
    if (items.length > 5) out.push(`        … ${items.length - 5} de plus`)
  }
  return out
}

function lignesTick(s: Section): string[] {
  if (s.valeur === null) return [`TICK    ${absent(s)}`]
  const t = s.valeur
  if (t.verrouille === true) {
    const battement =
      t.dans_s === null
        ? "battement —"
        : `battement dans ${nombre(t.dans_s)} s${t.battement_perime ? " · périmé" : ""}`
    return [`TICK    verrouillé · pid ${texte(t.pid, "—")} · ${battement}`]
  }
  if (t.verrouille === false) return ["TICK    libre · aucun battement en cours"]
  return ["TICK    verrou —"]
}

// Panneau complet. `data` = objet de cli.py --ui-json (ou {error} en échec).
function panneau(data: any): string {
  if (!data || typeof data !== "object" || data.error) {
    return [
      "Usine à blocs — panneau indisponible.",
      "",
      `Backend : ${texte(data?.error, "réponse vide")}`,
      "",
      "Vérifier :",
      `  python3 ${CLI} --ui-json`,
    ].join("\n")
  }

  const lignes: string[] = []
  lignes.push(`Usine — relevé ${heure(data.at)}${typeof data.schema === "number" ? ` · schéma ${data.schema}` : ""}`)
  lignes.push("")
  lignes.push(...lignesBloc(section(data, "bloc")))
  lignes.push(...lignesSessions(section(data, "sessions")))
  lignes.push(...lignesSignaux(section(data, "signaux")))
  lignes.push(...lignesDecision(section(data, "derniere_decision")))
  lignes.push(...lignesIncidents(section(data, "incidents")))
  lignes.push(...lignesTick(section(data, "prochain_tick")))
  lignes.push("")
  lignes.push("Lecture seule. Actions indisponibles : cli.py n'expose encore aucune")
  lignes.push("sous-commande d'écriture (démarrer/arrêter le bloc, pause, max_sessions);")
  lignes.push("aucune touche s/p/m n'est affichée pour ne pas proposer un geste inerte.")
  return lignes.join("\n")
}

/* ───────── Commande /factory ───────── */

async function ouvrirPanneau(ctx: Ctx) {
  const data = appel("etat")
  ctx.ui.dialog.set({ size: "large" })
  await ctx.ui.dialog.alert({ title: "Usine à blocs — /factory", message: panneau(data) })
}

function Commands(ctx: Ctx) {
  ctx.keymap.layer(() => ({
    mode: "global",
    priority: 10,
    commands: [
      {
        id: "factory.open",
        title: "Usine à blocs — état",
        description: "Bloc, sessions, signaux, dernière décision, incidents, prochain tick",
        group: "OpenCode",
        palette: true,
        slash: { name: "factory" },
        run: async () => {
          try {
            await ouvrirPanneau(ctx)
          } catch (error: any) {
            await ctx.ui.dialog.alert({
              title: "Usine à blocs",
              message: String(error?.message ?? error),
            })
          }
        },
      },
    ],
  }))
  return null
}

export default Plugin.define({
  id: "factory-ui",
  setup(context) {
    const dispose = context.ui.slot({ append: "app", render: () => Commands(context) })
    return () => {
      if (typeof dispose === "function") dispose()
    }
  },
})
