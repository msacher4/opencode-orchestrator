// factory-ui — commande /factory (TUI) : poste de pilotage
//
// Lit la source de vérité `cli.py --ui-json` et l'affiche en lignes mesurées.
// Le plugin ne calcule aucune décision : il rend, regroupe les incidents pour
// rester lisible, et appelle explicitement les sous-commandes existantes de
// cli.py pour agir (`factory pause|resume`). Aucun `exec` de chaîne arbitraire,
// aucune écriture de configuration.
import { Plugin } from "@opencode/plugin/tui"
import { execFileSync } from "node:child_process"
import { existsSync, readFileSync } from "node:fs"

const PROJECT = "/media/marcs/Linux_Apps/Projets_AI"
const FABRIQUE = `${PROJECT}/mcp/opencode-factory`
const CLI = `${FABRIQUE}/cli.py`
const BLOCS = `${PROJECT}/.opencode/factory/blocks.json`
const RECURRENTS = `${PROJECT}/.opencode/recurring-tasks.json`
const PAUSE = `${PROJECT}/.opencode/factory/paused.json`
const PYTHON = process.env.FACTORY_PYTHON || "python3"

// Mise en page : largeur mesurée sur le terminal, repli 100, plafond 200.
const LARGEUR_REPLI = 100
const LARGEUR_MAX = 200
const GROUPES_MAX = 8
const MESSAGE_MAX = 80
const INCIDENTS_DEPLIES_MAX = 6
const ID_TEST = "ses_test123"

// Appels au backend. Le test de contrat
// (mcp/opencode-factory/tests/test_tui_contract.py) lit ce bloc et vérifie que
// chaque option existe réellement dans cli.py.
//
// Ancien contrat (t30) : « Actions indisponibles ». t45 le remplace : pause et
// reprise passent par les sous-commandes réelles ci-dessous, avec code de
// sortie affiché. Aucune autre action n'est inventée.
const CLI_APPELS: Record<string, string[]> = {
  etat: ["--ui-json"],
  pause: ["factory", "pause", "--raison", "panneau"],
  reprise: ["factory", "resume"],
  // t59 : écriture d'un bloc depuis le panneau (touches e et m). Les valeurs
  // sont passées en arguments séparés à execFileSync, jamais dans une chaîne.
  bloc_set: ["bloc", "set"],
}

type Ctx = any
// Chaque section du relevé a exactement la forme {"valeur": …, "erreur": …}.
type Section = { valeur: any; erreur: string | null }
type Ligne = { texte: string; groupe?: string; action?: Action }
type Action = "pause" | "rafraichir" | "quitter" | "fenetre" | "sessions"

type Execution = { ok: boolean; code: number | null; sortie: string; erreur: string; commande: string }

type Etat = {
  data: any
  erreur: string | null
  blocs: any[]
  regles: any[]
  pause: boolean | null
  progression: any
  blocBrut: any | null
}

/* ───────── Appels explicites au backend ───────── */

function executer(args: string[], cwd = PROJECT): Execution {
  const commande = `python3 ${CLI} ${args.join(" ")}`
  try {
    const stdout = execFileSync(PYTHON, [CLI, ...args], {
      encoding: "utf8",
      timeout: 20000,
      maxBuffer: 8 * 1024 * 1024,
      cwd,
    })
    return { ok: true, code: 0, sortie: String(stdout).trim(), erreur: "", commande }
  } catch (error: any) {
    const code = typeof error?.status === "number" ? error.status : null
    const sortie = String(error?.stdout ?? "").trim()
    const brut = String(error?.stderr ?? "").trim() || String(error?.message ?? "échec")
    // CLI absent (binaire python3 ou cli.py introuvable) : message explicite,
    // le panneau reste ouvert au lieu de planter.
    const erreur = error?.code === "ENOENT" ? `CLI introuvable : ${PYTHON} ${CLI}` : brut
    return { ok: false, code, sortie, erreur, commande }
  }
}

function lireJson(chemin: string): any | null {
  try {
    return JSON.parse(readFileSync(chemin, "utf8"))
  } catch {
    return null
  }
}

// Progression de l'objectif : lue par le module backend `milestones.py` (t42),
// appelé explicitement. Toute panne rend `etat="inconnu"` — jamais un 0.
const SCRIPT_PROGRESSION = [
  "import json,sys",
  "import milestones",
  "bloc=json.loads(sys.argv[1])",
  "print(json.dumps(milestones.progression(bloc)))",
].join(";")

function lireProgression(blocBrut: any): any {
  if (!blocBrut || typeof blocBrut !== "object" || !blocBrut.objectif) {
    return { etat: "inconnu", motif: "bloc sans objectif structuré" }
  }
  if (!existsSync(`${FABRIQUE}/milestones.py`)) {
    return { etat: "inconnu", motif: "milestones.py absent" }
  }
  try {
    const stdout = execFileSync(PYTHON, ["-c", SCRIPT_PROGRESSION, JSON.stringify(blocBrut)], {
      encoding: "utf8",
      timeout: 6000,
      maxBuffer: 1024 * 1024,
      cwd: FABRIQUE,
    })
    const valeur = JSON.parse(stdout)
    return valeur && typeof valeur === "object" ? valeur : { etat: "inconnu", motif: "progression illisible" }
  } catch (error: any) {
    const brut = String(error?.stderr ?? error?.message ?? "?").trim()
    const derniere = brut.split("\n").filter((l: string) => l.trim()).pop() || "progression indisponible"
    return { etat: "inconnu", motif: derniere }
  }
}

function chargerEtat(): Etat {
  const execution = executer(CLI_APPELS.etat)
  let data: any = null
  let erreur: string | null = null
  if (!execution.ok) {
    erreur = `backend code ${execution.code ?? "?"} — ${execution.erreur || execution.sortie || "aucun message"}`
  } else {
    try {
      data = JSON.parse(execution.sortie)
    } catch {
      erreur = "réponse --ui-json illisible (JSON invalide)"
    }
  }
  if (data && data.error) {
    erreur = String(data.error)
    data = null
  }

  const paquetBlocs = lireJson(BLOCS)
  const blocs = paquetBlocs && Array.isArray(paquetBlocs.blocks) ? paquetBlocs.blocks : []
  const paquetRegles = lireJson(RECURRENTS)
  const regles = paquetRegles && Array.isArray(paquetRegles.rules) ? paquetRegles.rules : []

  let pause: boolean | null = null
  if (existsSync(PAUSE)) {
    const etatPause = lireJson(PAUSE)
    pause = etatPause && typeof etatPause.paused === "boolean" ? etatPause.paused : true // fail-closed
  } else {
    pause = false
  }

  const blocValeur = data?.bloc?.valeur
  const id = blocValeur && typeof blocValeur.id === "string" ? blocValeur.id : null
  const blocBrut = id ? blocs.find((b: any) => b && b.id === id) || null : blocs[0] || null
  const progression = lireProgression(blocBrut)

  return { data, erreur, blocs, regles, pause, progression, blocBrut }
}

/* ───────── Format ───────── */

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

// Une section en erreur affiche « — » + la raison : jamais 0, jamais une
// valeur inventée (un zéro ferait croire que rien n'a été fait).
function absent(s: Section): string {
  return `— (${s.erreur || "valeur absente"})`
}

function texte(value: any, repli = "—"): string {
  return typeof value === "string" && value.trim() ? value.trim() : repli
}

function nombre(value: any): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "—"
}

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

function idCourt(value: any, taille = 16): string {
  const id = texte(value, "?")
  return id.length > taille ? `${id.slice(0, taille - 1)}…` : id
}

function couper(value: string, max: number): string {
  if (max <= 0) return ""
  return value.length <= max ? value : `${value.slice(0, max - 1)}…`
}

// Tronqué à 80 caractères comme demandé, jamais plus.
function resumeDetails(details: any): string {
  if (!details || typeof details !== "object") return couper(texte(details), MESSAGE_MAX)
  for (const cle of ["api_error", "message", "detail", "raison", "motif", "mode", "rule_id", "session_id"]) {
    const v = details[cle]
    if (typeof v === "string" && v.trim()) return couper(v.trim(), MESSAGE_MAX)
  }
  try {
    return couper(JSON.stringify(details), MESSAGE_MAX)
  } catch {
    return "—"
  }
}

function largeur(): number {
  const cols = Number((process.stdout as any).columns)
  const base = Number.isFinite(cols) && cols > 20 ? Math.floor(cols) : LARGEUR_REPLI
  return Math.min(Math.max(base, 40), LARGEUR_MAX)
}

function aligner(value: string, taille: number): string {
  return value.length >= taille ? couper(value, taille) : value + " ".repeat(taille - value.length)
}

/* ───────── Incidents : un groupe par motif ───────── */

type Groupe = {
  motif: string
  nombre: number
  premier: string
  dernier: string
  message: string
  items: any[]
}

function contientIdTest(valeur: any): boolean {
  try {
    return JSON.stringify(valeur).includes(ID_TEST)
  } catch {
    return false
  }
}

function grouperIncidents(items: any[]): Groupe[] {
  const parMotif = new Map<string, Groupe>()
  const source = Array.isArray(items) ? items : []
  for (const inc of source) {
    if (contientIdTest(inc)) continue // jamais de données de test à l'écran
    const motif = texte(inc?.motif, "?")
    const h = heure(inc?.heure)
    let groupe = parMotif.get(motif)
    if (!groupe) {
      groupe = {
        motif,
        nombre: 0,
        premier: h,
        dernier: h,
        message: resumeDetails(inc?.details),
        items: [],
      }
      parMotif.set(motif, groupe)
    }
    groupe.nombre += 1
    if (h !== "—" && (groupe.premier === "—" || h < groupe.premier)) groupe.premier = h
    if (h !== "—" && (groupe.dernier === "—" || h > groupe.dernier)) groupe.dernier = h
    groupe.items.push(inc)
  }
  // Fréquence décroissante, puis ordre alphabétique pour un affichage stable.
  return [...parMotif.values()].sort((a, b) => b.nombre - a.nombre || a.motif.localeCompare(b.motif))
}

export function grouperIncidentsPourTest(items: any[]): Groupe[] {
  return grouperIncidents(items)
}

/* ───────── Sections ───────── */

function etatBloc(brut: any, pause: boolean | null): string {
  if (pause === true) return "EN PAUSE"
  if (brut?.dans_fenetre === true && brut?.actif_plan === true) return "ouverte"
  if (brut?.dans_fenetre === false || brut?.actif_plan === false) return "fermée"
  return "état inconnu"
}

function progressionTexte(p: any, objectifBloc: any): string {
  if (!p || p.etat !== "connu") {
    const motif = typeof p?.motif === "string" && p.motif ? ` : ${p.motif}` : ""
    return `inconnu${motif}`
  }
  const fait = typeof p.fait === "number" && Number.isFinite(p.fait) ? String(p.fait) : "?"
  if (typeof p.objectif === "number" && p.objectif > 0) {
    if (p.atteinte === true) return `cible atteinte (${fait}/${p.objectif})`
    return `${fait}/${p.objectif}`
  }
  if (objectifBloc?.mode === "max") return `${fait} faits (max)`
  return "inconnu : objectif non chiffré"
}

function reglesDuBloc(etat: Etat, blocBrut: any): any[] {
  const produits = Array.isArray(blocBrut?.produces) ? blocBrut.produces : []
  if (!produits.length) return []
  return etat.regles.filter((r: any) => r && produits.includes(r.id))
}

function etatRegle(regle: any, pause: boolean | null): string {
  if (pause === true) return "EN PAUSE"
  // Source réelle (recurring-tasks.json) : `paused` booléen.
  if (typeof regle?.paused === "boolean") return regle.paused ? "suspendue" : "active"
  if (regle?.actif === true) return "active"
  if (regle?.actif === false) return "suspendue"
  return "état inconnu"
}

function nomManifest(regle: any): string {
  const manifeste = regle?.goal?.manifest
  if (typeof manifeste !== "string" || !manifeste) return "—"
  const morceaux = manifeste.split("/")
  return morceaux[morceaux.length - 1] || manifeste
}

function lignesBloc(s: Section, etat: Etat): Ligne[] {
  const out: Ligne[] = []
  if (s.valeur === null) {
    out.push({ texte: `BLOC     ${absent(s)}` })
    return out
  }
  const b = s.valeur
  const nom = texte(b.nom ?? b.id)
  const fenetre = b.fenetre && b.fenetre.debut && b.fenetre.fin ? `${b.fenetre.debut}→${b.fenetre.fin}` : "fenêtre —"
  const blocBrut = etat.blocs.find((x: any) => x && (x.id === b.id || x.id === b.nom)) || null
  const objectif = blocBrut?.objectif
  out.push({
    texte: `BLOC     ${nom}  ${fenetre}  ${etatBloc(b, etat.pause)}  objectif ${progressionTexte(etat.progression, objectif)}`,
  })

  if (b.dans_fenetre === true) {
    out.push({ texte: `         en fenêtre · ${b.temps_restant_min === null ? "reste —" : `reste ${duree(b.temps_restant_min)}`}` })
  } else if (b.dans_fenetre === false) {
    out.push({
      texte: `         hors fenêtre · ${b.prochain_debut_min === null ? "prochaine ouverture —" : `ouvre dans ${duree(b.prochain_debut_min)}`}`,
    })
  } else {
    out.push({ texte: "         fenêtre —" })
  }

  // Quota de sessions lu dans le bloc brut : une clé absente s'affiche « — »,
  // jamais 0 (0 ferait croire qu'aucune session ne peut tourner).
  const quota =
    blocBrut && typeof blocBrut.max_sessions === "number" ? String(blocBrut.max_sessions) : "—"
  out.push({ texte: `         sessions max : ${quota}` })

  const regles = reglesDuBloc(etat, blocBrut)
  out.push({ texte: `         règles rattachées : ${regles.length}` })
  if (!regles.length) {
    out.push({ texte: "           · aucune (produces vide ou bloc illisible)" })
  } else {
    for (const r of regles) {
      const id = aligner(idCourt(r.id, 12), 12)
      const st = aligner(etatRegle(r, etat.pause), 12)
      const h = aligner(heure(r?.schedule?.time), 5)
      out.push({ texte: `           · ${id}  ${st}  ${h}  ${nomManifest(r)}` })
    }
  }
  return out
}

function lignesSessions(s: Section): Ligne[] {
  const out: Ligne[] = []
  if (s.valeur === null) {
    out.push({ texte: `SESSIONS ${absent(s)}` })
    return out
  }
  const v = s.valeur
  const items = (Array.isArray(v.items) ? v.items : []).filter((it: any) => !contientIdTest(it))
  const ecartees = (Array.isArray(v.items) ? v.items : []).length - items.length
  const equipes = v.par_equipe && typeof v.par_equipe === "object" ? Object.entries(v.par_equipe) : []
  const resume = equipes.length ? equipes.map(([e, n]) => `${e} ${n}`).join(" · ") : "par équipe —"
  const note = ecartees ? ` · ${ecartees} entrée(s) de test écartée(s)` : ""
  out.push({ texte: `SESSIONS ${items.length} · ${resume}${note}` })
  if (!items.length) {
    out.push({ texte: "         aucune session suivie" })
  } else {
    for (const it of items.slice(0, 4)) {
      const age = it?.age_min === null ? `âge — (${texte(it?.erreur, "date illisible")})` : `il y a ${duree(it?.age_min)}`
      out.push({ texte: `         · ${idCourt(it?.id)} · ${texte(it?.regle)} · ${texte(it?.equipe)} · ${age}` })
    }
    if (items.length > 4) out.push({ texte: `         · … ${items.length - 4} de plus` })
  }
  return out
}

function lignesSignaux(s: Section): Ligne[] {
  const out: Ligne[] = []
  if (s.valeur === null) {
    out.push({ texte: `SIGNAUX  ${absent(s)}` })
    return out
  }
  const v = s.valeur
  const tous = Array.isArray(v.items) ? v.items : []
  const items = tous.filter((sig: any) => !contientIdTest(sig))
  const fenetre = typeof v.fenetre_min === "number" ? ` (${v.fenetre_min} min)` : ""
  out.push({ texte: `SIGNAUX  ${items.length}${fenetre}` })
  if (!items.length) {
    out.push({ texte: "         aucun" })
  } else {
    for (const sig of items.slice(0, 3)) {
      out.push({ texte: `         · ${texte(sig?.gravite, "?")} ${texte(sig?.code, "?")} ${idCourt(sig?.cible)}` })
    }
    if (items.length > 3) out.push({ texte: `         · … ${items.length - 3} de plus` })
  }
  return out
}

function lignesDecision(s: Section): Ligne[] {
  if (s.valeur === null) return [{ texte: `DÉCISION ${absent(s)}` }]
  const d = s.valeur
  const actions = Array.isArray(d.actions) ? d.actions.length : "—"
  const refus = Array.isArray(d.refus) ? d.refus.length : "—"
  // Le journal écrit `cout` (journal.ecrire_decision) ; `jev_cout` n'existe que
  // dans le dict interne de tick.py, réécrit avant l'écriture. On lit la clé
  // réelle, et une absence s'affiche « — » : jamais un 0 inventé.
  const coutBrut = typeof d.cout === "number" ? d.cout : d.jev_cout
  const cout = typeof coutBrut === "number" && Number.isFinite(coutBrut) ? `${coutBrut.toFixed(2)} €` : "—"
  return [
    {
      texte: `DÉCISION ${heure(d.at)} · ${texte(d.mode)} · ${actions} action(s) · ${refus} refus · coût ${cout}`,
    },
  ]
}

function lignesIncidents(s: Section, ouverts: Set<string>): Ligne[] {
  const out: Ligne[] = []
  if (s.valeur === null) {
    out.push({ texte: `INCIDENTS ${absent(s)}` })
    return out
  }
  const v = s.valeur
  const brut = Array.isArray(v.items) ? v.items : []
  const echappes = brut.filter((i: any) => contientIdTest(i)).length
  const groupes = grouperIncidents(brut)
  const fenetre = typeof v.fenetre_h === "number" ? `${v.fenetre_h} h` : "fenêtre —"
  const note = echappes ? ` · ${echappes} entrée(s) de test écartée(s)` : ""
  out.push({ texte: `INCIDENTS ${brut.length - echappes} sur ${fenetre} · ${groupes.length} motif(s)${note}` })
  if (!groupes.length) {
    out.push({ texte: "         aucun" })
    return out
  }
  for (const g of groupes.slice(0, GROUPES_MAX)) {
    const ouvert = ouverts.has(g.motif)
    const marque = ouvert ? "▾" : "▸"
    const ligne = couper(
      `         ${marque} ${g.motif} — ${g.nombre} fois — ${g.premier} → ${g.dernier} — 1re : ${g.message}`,
      largeur() - 2
    )
    out.push({ texte: ligne, groupe: g.motif })
    if (ouvert) {
      for (const inc of g.items.slice(0, INCIDENTS_DEPLIES_MAX)) {
        out.push({
          texte: couper(
            `             ${heure(inc?.heure)} ${texte(inc?.action, "?")} ${idCourt(inc?.cible, 12)} · ${resumeDetails(inc?.details)}`,
            largeur() - 2
          ),
        })
      }
      if (g.items.length > INCIDENTS_DEPLIES_MAX) {
        out.push({ texte: `             … ${g.items.length - INCIDENTS_DEPLIES_MAX} incident(s) de plus` })
      }
    }
  }
  if (groupes.length > GROUPES_MAX) out.push({ texte: `         + ${groupes.length - GROUPES_MAX} autres motifs` })
  return out
}

function lignesTick(s: Section): Ligne[] {
  if (s.valeur === null) return [{ texte: `TICK     ${absent(s)}` }]
  const t = s.valeur
  if (t.verrouille === true) {
    const battement =
      t.dans_s === null ? "battement —" : `battement dans ${nombre(t.dans_s)} s${t.battement_perime ? " · périmé" : ""}`
    return [{ texte: `TICK     verrouillé · pid ${texte(t.pid, "—")} · ${battement}` }]
  }
  if (t.verrouille === false) return [{ texte: "TICK     libre · aucun battement en cours" }]
  return [{ texte: "TICK     verrou —" }]
}

function lignesDigest(s: Section): Ligne[] {
  if (s.valeur === null) return [{ texte: `JOUR     ${absent(s)}` }]
  const d = s.valeur
  const cout = typeof d.cout === "number" && Number.isFinite(d.cout) ? `${d.cout.toFixed(2)} €` : "—"
  return [
    {
      texte: `JOUR     ${texte(d.date)} · ${nombre(d.runs)} run(s) · ${nombre(d.incidents)} incident(s) · coût ${cout}`,
    },
  ]
}

/* ───────── Composition du panneau ───────── */

function composer(etat: Etat, ouverts: Set<string>, retour: string): Ligne[] {
  if (etat.erreur) {
    return [
      { texte: "Usine à blocs — panneau indisponible." },
      { texte: `Backend : ${etat.erreur}` },
      { texte: `Vérifier : python3 ${CLI} --ui-json` },
      ...(retour ? [{ texte: "" }, { texte: couper(retour, largeur() - 2) }] : []),
      { texte: "" },
      { texte: "[r] ↻ Rafraîchir", action: "rafraichir" },
      { texte: "[q] ✕ Quitter", action: "quitter" },
    ]
  }

  const data = etat.data
  const pauseTexte = etat.pause === true ? "oui" : etat.pause === false ? "non" : "inconnue"
  const schema = typeof data?.schema === "number" ? ` · schéma ${data.schema}` : ""
  const lignes: Ligne[] = []
  lignes.push({
    texte: `Usine à blocs — relevé ${heure(data?.at)}${schema} · pause ${pauseTexte}`,
  })
  lignes.push({ texte: "" })
  lignes.push(...lignesBloc(section(data, "bloc"), etat))
  lignes.push(...lignesSessions(section(data, "sessions")))
  lignes.push(...lignesSignaux(section(data, "signaux")))
  lignes.push(...lignesDecision(section(data, "derniere_decision")))
  lignes.push(...lignesIncidents(section(data, "incidents"), ouverts))
  lignes.push(...lignesTick(section(data, "prochain_tick")))
  lignes.push(...lignesDigest(section(data, "digest_resume")))
  lignes.push({ texte: "" })
  if (retour) {
    lignes.push({ texte: couper(retour, largeur() - 2) })
    lignes.push({ texte: "" })
  }
  const pause = etat.pause === true
  const quotaCourant =
    etat.blocBrut && typeof etat.blocBrut.max_sessions === "number"
      ? etat.blocBrut.max_sessions
      : "—"
  lignes.push({ texte: pause ? "[p] ▶ Reprendre les récurrents" : "[p] ⏸ Mettre en pause", action: "pause" })
  lignes.push({
    texte: `[e] ✎ Fenêtre du bloc ${texte(etat.blocBrut?.id, "?")}`,
    action: "fenetre",
  })
  lignes.push({
    texte: `[m] ⚙ Sessions max du bloc (${quotaCourant})`,
    action: "sessions",
  })
  lignes.push({ texte: "[r] ↻ Rafraîchir", action: "rafraichir" })
  lignes.push({ texte: "[q] ✕ Quitter", action: "quitter" })

  return lignes.map((l) => ({ ...l, texte: couper(l.texte, largeur() - 2) }))
}

function panneau(etat: Etat, ouverts: Set<string>, retour: string): string {
  return composer(etat, ouverts, retour)
    .map((l) => l.texte)
    .join("\n")
}

/* ───────── Actions ───────── */

function basculerPause(etat: Etat): string {
  const enPause = etat.pause === true
  const args = enPause ? CLI_APPELS.reprise : CLI_APPELS.pause
  const execution = executer(args)
  if (execution.ok) {
    return `OK   ${execution.commande} → code ${execution.code} · ${execution.sortie || "fait"}`
  }
  return `ÉCHEC ${execution.commande} → code ${execution.code ?? "?"} · ${execution.erreur || execution.sortie || "aucun message"}`
}

/* ───────── Écriture d'un bloc : fenêtre (e) et quota (m) ───────── */

const HHMM_RE = /^([01][0-9]|2[0-3]):[0-5][0-9]$/
const FENETRE_RE = /^\s*([0-9]{1,2}:[0-9]{2})\s*-\s*([0-9]{1,2}:[0-9]{2})\s*$/

// Analyse locale, avant tout appel : une saisie invalide ne doit rien écrire.
function analyserFenetre(valeur: string): { debut: string; fin: string } | string {
  const brut = String(valeur ?? "").trim()
  const m = FENETRE_RE.exec(brut)
  if (!m) return `format attendu HH:MM-HH:MM (reçu ${JSON.stringify(brut)})`
  const debut = m[1] ?? ""
  const fin = m[2] ?? ""
  if (!HHMM_RE.test(debut) || !HHMM_RE.test(fin)) {
    return `heure invalide : ${debut}-${fin} (00:00 à 23:59 attendu)`
  }
  if (!(debut < fin)) return `fenêtre ${debut}-${fin} refusée : debut doit être strictement avant fin`
  return { debut, fin }
}

function analyserQuota(valeur: string): number | string {
  const brut = String(valeur ?? "").trim()
  if (!/^[0-9]+$/.test(brut)) return `entier >= 1 attendu (reçu ${JSON.stringify(brut)})`
  const n = Number(brut)
  if (!Number.isSafeInteger(n) || n < 1) return `entier >= 1 attendu (reçu ${JSON.stringify(brut)})`
  return n
}

// Bilan d'un appel : le code de sortie réel, jamais une réussite supposée.
function bilan(execution: Execution): string {
  if (execution.ok) {
    const detail = execution.sortie.split("\n").map((l) => l.trim()).filter(Boolean).slice(0, 2).join(" · ")
    return `OK ${execution.commande} → code 0${detail ? ` · ${detail}` : ""}`
  }
  const message = execution.erreur || execution.sortie || "aucun message"
  const premiere = message.split("\n").map((l) => l.trim()).filter(Boolean).slice(0, 2).join(" · ")
  return `ÉCHEC code ${execution.code ?? "?"} — ${premiere}`
}

function idBlocSelectionne(etat: Etat): string | null {
  const id = etat.blocBrut?.id ?? etat.data?.bloc?.valeur?.id
  return typeof id === "string" && id ? id : null
}

async function editerFenetre(ctx: Ctx, etat: Etat): Promise<string> {
  const id = idBlocSelectionne(etat)
  if (!id) return "ÉCHEC code ? — aucun bloc sélectionné, rien n'a été écrit"
  const brut = etat.blocBrut
  const debut = brut && typeof brut.debut === "string" ? brut.debut : "09:00"
  const fin = brut && typeof brut.fin === "string" ? brut.fin : "14:00"
  const saisie = await ctx.ui.dialog.prompt({
    title: `Fenêtre de ${id} — HH:MM-HH:MM`,
    placeholder: "09:00-14:00",
    value: `${debut}-${fin}`,
  })
  if (saisie === undefined) return "fenêtre : annulé (Échap), rien n'a été écrit"
  const verif = analyserFenetre(saisie)
  if (typeof verif === "string") return `ÉCHEC — ${verif} · rien n'a été écrit`
  // Arguments séparés : aucune chaîne utilisateur n'entre dans une commande.
  const execution = executer([
    ...CLI_APPELS.bloc_set,
    "--bloc", id,
    "--debut", verif.debut,
    "--fin", verif.fin,
  ])
  return bilan(execution)
}

async function editerQuota(ctx: Ctx, etat: Etat): Promise<string> {
  const id = idBlocSelectionne(etat)
  if (!id) return "ÉCHEC code ? — aucun bloc sélectionné, rien n'a été écrit"
  const actuel =
    etat.blocBrut && typeof etat.blocBrut.max_sessions === "number"
      ? String(etat.blocBrut.max_sessions)
      : ""
  const saisie = await ctx.ui.dialog.prompt({
    title: `Sessions max de ${id} — entier ≥ 1`,
    placeholder: "1",
    value: actuel,
  })
  if (saisie === undefined) return "sessions max : annulé (Échap), rien n'a été écrit"
  const verif = analyserQuota(saisie)
  if (typeof verif === "string") return `ÉCHEC — ${verif} · rien n'a été écrit`
  const execution = executer([
    ...CLI_APPELS.bloc_set,
    "--bloc", id,
    "--max-sessions", String(verif),
  ])
  return bilan(execution)
}

/* ───────── Boucle de pilotage ───────── */

async function executerActionPanneau(ctx: Ctx, etat: Etat, action: Action): Promise<string | null> {
  if (action === "quitter") return null
  if (action === "rafraichir") return `↻ relevé rafraîchi à ${heure(new Date().toISOString())}`
  if (action === "pause") return basculerPause(etat)
  if (action === "fenetre") return editerFenetre(ctx, etat)
  return editerQuota(ctx, etat)
}

async function piloter(ctx: Ctx): Promise<void> {
  let etat = chargerEtat()
  const ouverts = new Set<string>()
  let retour = ""
  ctx.ui.dialog.set({ size: "xlarge" })

  for (;;) {
    const lignes = composer(etat, ouverts, retour)
    const options = lignes.map((l, index) => {
      if (l.action) {
        return {
          title: l.texte,
          value: { type: "action" as const, action: l.action },
        }
      }
      if (l.groupe) {
        return {
          title: l.texte,
          value: { type: "groupe" as const, groupe: l.groupe },
          description: ouverts.has(l.groupe) ? "entrée : replier" : "entrée : déplier",
        }
      }
      return { title: l.texte || " ", value: { type: "info" as const, index }, disabled: true }
    })

    const declencheur: { action: Action | null } = { action: null }
    let choix: any = null
    try {
      choix = await ctx.ui.dialog.select({
        title: "Usine à blocs — poste de pilotage",
        placeholder:
          "p pause/reprise · e fenêtre · m sessions max · r rafraîchir · entrée déplier/replier · q quitter",
        options,
        actions: [
          {
            bind: "p",
            title: "pause / reprise",
            selection: "none" as const,
            onTrigger: () => {
              declencheur.action = "pause"
              try { ctx.ui.dialog.clear() } catch { /* déjà fermé */ }
            },
          },
          {
            bind: "e",
            title: "fenêtre du bloc",
            selection: "none" as const,
            onTrigger: () => {
              declencheur.action = "fenetre"
              try { ctx.ui.dialog.clear() } catch { /* déjà fermé */ }
            },
          },
          {
            bind: "m",
            title: "sessions max du bloc",
            selection: "none" as const,
            onTrigger: () => {
              declencheur.action = "sessions"
              try { ctx.ui.dialog.clear() } catch { /* déjà fermé */ }
            },
          },
          {
            bind: "r",
            title: "rafraîchir",
            selection: "none" as const,
            onTrigger: () => {
              declencheur.action = "rafraichir"
              try { ctx.ui.dialog.clear() } catch { /* déjà fermé */ }
            },
          },
          {
            bind: "q",
            title: "quitter",
            selection: "none" as const,
            onTrigger: () => {
              declencheur.action = "quitter"
              try { ctx.ui.dialog.clear() } catch { /* déjà fermé */ }
            },
          },
        ],
      })
    } catch (error: any) {
      // Dernier recours : le panneau texte, sans prétendre que l'action a eu lieu.
      await ctx.ui.dialog.alert({
        title: "Usine à blocs",
        message: `${panneau(etat, ouverts, retour)}\n\naction impossible : ${String(error?.message ?? error)}`,
      })
      return
    }

    if (declencheur.action !== null) {
      const action = declencheur.action
      if (action === "quitter") return
      const resultat = await executerActionPanneau(ctx, etat, action)
      etat = chargerEtat()
      retour = resultat ?? ""
      continue
    }

    if (!choix || choix.type === "info") {
      if (!choix) return
      continue
    }
    if (choix.type === "action") {
      if (choix.action === "quitter") return
      const resultat = await executerActionPanneau(ctx, etat, choix.action)
      etat = chargerEtat()
      retour = resultat ?? ""
      continue
    }
    if (choix.type === "groupe") {
      if (ouverts.has(choix.groupe)) ouverts.delete(choix.groupe)
      else ouverts.add(choix.groupe)
      continue
    }
  }
}

/* ───────── Commande /factory ───────── */

function Commands(ctx: Ctx) {
  ctx.keymap.layer(() => ({
    mode: "global",
    priority: 10,
    commands: [
      {
        id: "factory.open",
        title: "Usine à blocs — pilotage",
        description: "Bloc, règles, sessions, signaux, incidents groupés, tick · p pause/reprise · e fenêtre · m sessions max · r rafraîchir",
        group: "OpenCode",
        palette: true,
        slash: { name: "factory" },
        run: async () => {
          try {
            await piloter(ctx)
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
