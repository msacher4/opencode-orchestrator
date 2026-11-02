#!/usr/bin/env python3
"""Moteur des tâches récurrentes OpenCode.

Deux effets à échéance, configurables par règle :
  1. créer une carte dans le Kanban GTD (one-way, label dédié, marqueur anti-doublon) ;
  2. envoyer un message Telegram par tâche.

Le moteur ne lit JAMAIS les règles récurrentes du Kanban (recurring.json) :
le Kanban n'est utilisé que comme destination (POST /api/add) et pour la
déduplication de nos propres marqueurs.
"""

import calendar
import hashlib
import json
import os
import re
import sys
import subprocess
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import store

# --------------------------------------------------------------------------- #
# Pause globale de l'usine (t40)
# --------------------------------------------------------------------------- #
# Le bouton d'arrêt est porté par mcp/opencode-factory/pause.py. On le charge
# par chemin absolu (pas de sys.path modifié, pas de dépendance au cwd).
_PAUSE_PY = Path(__file__).resolve().parents[2] / "mcp" / "opencode-factory" / "pause.py"
_PAUSE_MODULE = None


def _module_pause():
    """Charge (une seule fois) le module pause de l'usine."""
    global _PAUSE_MODULE
    if _PAUSE_MODULE is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_factory_pause", _PAUSE_PY)
        if spec is None or spec.loader is None:
            raise ImportError(f"pause.py introuvable : {_PAUSE_PY}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PAUSE_MODULE = module
    return _PAUSE_MODULE


def est_en_pause() -> bool:
    """Vrai si la pause globale est posée (ou indéchiffrable -> pause)."""
    try:
        return bool(_module_pause().est_en_pause())
    except Exception:  # noqa: BLE001 - fail-closed, jamais de traceback
        return True


def motif_pause() -> str | None:
    """Raison lisible de la pause, ou None."""
    try:
        return _module_pause().raison()
    except Exception as exc:  # noqa: BLE001
        return f"pause illisible : {exc}"


PROJECT_ROOT = Path(__file__).resolve().parents[2]
KANBAN_URL = os.environ.get("GTD_KANBAN_URL", "http://127.0.0.1:8787").rstrip("/")
KANBAN_TIMEOUT = float(os.environ.get("GTD_KANBAN_TIMEOUT", "5"))
TELEGRAM_SCRIPT = os.environ.get(
    "OPENCODE_RECURRING_TELEGRAM",
    str(PROJECT_ROOT / ".opencode" / "skills" / "contact_telegram" / "scripts" / "send_telegram.sh"),
)
DEFAULT_LABELS = ["+Opencode", "@recurrent"]
CATCHUP_MAX = 7
HISTORY_MAX = 30

BLOCKS_PATH = PROJECT_ROOT / ".opencode" / "factory" / "blocks.json"

# --------------------------------------------------------------------------- #
# Battement de l'usine — arbitrage des règles de bloc (t53)
# --------------------------------------------------------------------------- #
# L'usine à blocs (mcp/opencode-factory/tick.py) pilote elle aussi les règles
# listées dans `produces` d'un bloc. Sans arbitrage, les deux chemins lancent la
# même règle : deux sessions, deux Telegram, deux écritures Sheet. `engine.tick`
# s'efface donc tant que l'usine est vivante.
#
# Le critère est le battement écrit par state.py (TickLock.heartbeat) :
#   {"pid", "started_at", "last_beat", "interval_s", "beat"}
# `last_beat` est un ISO8601 UTC. Le seuil est 3 x `interval_s`, exactement la
# règle `HeartbeatData.is_stale` / `ABANDONED_MULTIPLIER` de state.py (10 s par
# défaut, donc 30 s). Au-delà, ou si le fichier est absent/illisible, l'usine
# est morte ou pas installée : `engine.tick` lance normalement (pas de trou).
SEUIL_BATTEMENT_S = 30.0  # 3 x DEFAULT_HEARTBEAT_INTERVAL_S (10 s) de state.py
INTERVALLE_BATTEMENT_DEFAUT_S = 10.0
BATTEMENT_RELPATH = Path(".opencode") / "factory" / "tick.heartbeat"


def chemin_battement() -> Path:
    """Chemin du battement de l'usine.

    Surchargeable par FACTORY_HEARTBEAT_PATH (tests hermétiques) ; sinon sous
    PROJECT_ROOT, lu à l'appel pour suivre un PROJECT_ROOT patché.
    """
    env = os.environ.get("FACTORY_HEARTBEAT_PATH")
    if env:
        return Path(env).expanduser()
    return PROJECT_ROOT / BATTEMENT_RELPATH


def seuil_battement(donnees: dict) -> float:
    """Seuil de fraîcheur : 3 x l'intervalle déclaré, 30 s par défaut."""
    try:
        intervalle = float(donnees.get("interval_s") or INTERVALLE_BATTEMENT_DEFAUT_S)
    except (TypeError, ValueError):
        intervalle = INTERVALLE_BATTEMENT_DEFAUT_S
    if intervalle <= 0:
        intervalle = INTERVALLE_BATTEMENT_DEFAUT_S
    return max(SEUIL_BATTEMENT_S, intervalle * 3)


def usine_vivante(now: datetime | None = None) -> bool:
    """Vrai si le battement de l'usine est présent, lisible et frais.

    Fail-open volontaire : toute anomalie (absent, JSON invalide, encodage,
    horodatage illisible, périmé) rend False, donc `engine.tick` continue de
    lancer. Le coût d'un doublon est préférable à une prospection arrêtée.
    """
    try:
        donnees = json.loads(chemin_battement().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    if not isinstance(donnees, dict):
        return False
    dernier = donnees.get("last_beat")
    if not isinstance(dernier, str) or not dernier:
        return False
    try:
        battu = datetime.fromisoformat(dernier.replace("Z", "+00:00"))
    except ValueError:
        return False
    if battu.tzinfo is None:
        battu = battu.replace(tzinfo=timezone.utc)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    age = (instant - battu).total_seconds()
    return age <= seuil_battement(donnees)


# --------------------------------------------------------------------------- #
# Registre partagé des lancements du jour — course résiduelle (t60)
# --------------------------------------------------------------------------- #
# Le battement ferme la course « usine vivante / usine morte », pas le cas où
# l'usine meurt LE JOUR MÊME après avoir lancé une règle : sa déduplication
# « règle + jour » vit dans `state.py` (`PlafondAsync`), que le moteur ne lit
# pas. `act.py` inscrit donc chaque lancement réel dans
# `.opencode/factory/lancees.json` AVANT de créer la session, et `engine.tick`
# lit ce registre sur son chemin de repli. Fail-open : registre absent,
# illisible ou corrompu => on lance (jamais de trou de production).
LANCEES_RELPATH = Path(".opencode") / "factory" / "lancees.json"
TEXTE_DEJA_LANCEE = "skipped: déjà lancée par l'usine aujourd'hui"


def chemin_lancees() -> Path:
    """Chemin du registre des lancements (FACTORY_LANCEES_PATH, tests hermétiques)."""
    env = os.environ.get("FACTORY_LANCEES_PATH")
    if env:
        return Path(env).expanduser()
    return PROJECT_ROOT / LANCEES_RELPATH


def deja_lancee_ce_jour(regle: str, jour: str) -> bool:
    """Vrai si `regle` est inscrite au registre partagé pour `jour` (ISO).

    Toute anomalie (fichier absent, JSON invalide, encodage, structure
    inattendue) rend False : le moteur lance normalement.
    """
    try:
        donnees = json.loads(chemin_lancees().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    if not isinstance(donnees, dict):
        return False
    entrees = donnees.get("lancees")
    if not isinstance(entrees, list):
        return False
    for entree in entrees:
        if (
            isinstance(entree, dict)
            and entree.get("regle") == regle
            and entree.get("date") == jour
        ):
            return True
    return False

OPENCODE_BIN = os.environ.get(
    "OPENCODE_BIN", str(PROJECT_ROOT / "Opencodev2" / "opencode-v2")
)
OPENCODE_API_TIMEOUT = float(os.environ.get("OPENCODE_API_TIMEOUT", "30"))
OPENCODE_SERVER_URL = os.environ.get("OPENCODE_SERVER_URL", "http://127.0.0.1:4097")
OPENCODE_V2_ENV_FILE = Path(os.environ.get("OPENCODE_V2_ENV_FILE", "/home/marcs/.telegram-bridge-venv/v2.env"))
GOAL_RESUME_DEBOUNCE = float(os.environ.get("OPENCODE_RECURRING_GOAL_RESUME_DEBOUNCE", "90"))
GOAL_RESUME_MAX = int(os.environ.get("OPENCODE_RECURRING_GOAL_MAX_RESUMES", "0"))
GOAL_RUNS_MAX = 200
GOAL_RUNS_KEEP_DAYS = 30

WEEKDAY_ALIASES = {
    "mon": 0, "monday": 0, "lun": 0, "lundi": 0,
    "tue": 1, "tuesday": 1, "mar": 1, "mardi": 1,
    "wed": 2, "wednesday": 2, "mer": 2, "mercredi": 2,
    "thu": 3, "thursday": 3, "jeu": 3, "jeudi": 3,
    "fri": 4, "friday": 4, "ven": 4, "vendredi": 4,
    "sat": 5, "saturday": 5, "sam": 5, "samedi": 5,
    "sun": 6, "sunday": 6, "dim": 6, "dimanche": 6,
}
WEEKDAY_SHORT = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
FREQ_LABEL = {
    "daily": "quotidien",
    "every": "tous les N jours",
    "weekly": "hebdomadaire",
    "monthly": "mensuel",
    "yearly": "annuel",
}
VALID_KINDS = set(FREQ_LABEL)
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# --------------------------------------------------------------------------- #
# Blocs — chargement et logique de couverture
# --------------------------------------------------------------------------- #
class BlocksLoadError(Exception):
    """Erreur de chargement des blocs avec type explicite."""
    def __init__(self, kind: str, detail: str, path: Path):
        self.kind = kind  # "missing" | "unreadable"
        self.detail = detail
        self.path = path
        super().__init__(f"{kind}: {detail} (path: {path})")


def _load_blocks() -> dict:
    """Charge blocks.json depuis le chemin projet.
    
    Lève BlocksLoadError avec kind='missing' si fichier absent,
    ou kind='unreadable' si présent mais illisible (JSON invalide, encodage,
    permission, structure inattendue comme blocks pas une liste).
    """
    if not BLOCKS_PATH.exists():
        raise BlocksLoadError("missing", "fichier blocks.json introuvable", BLOCKS_PATH)
    
    try:
        raw = BLOCKS_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BlocksLoadError("unreadable", f"lecture impossible : {exc}", BLOCKS_PATH) from exc
    
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BlocksLoadError("unreadable", f"JSON invalide : {exc}", BLOCKS_PATH) from exc
    
    # Validation structurelle minimale
    if not isinstance(data, dict):
        raise BlocksLoadError("unreadable", "racine JSON n'est pas un objet", BLOCKS_PATH)
    if "blocks" not in data:
        raise BlocksLoadError("unreadable", "champ 'blocks' manquant", BLOCKS_PATH)
    if not isinstance(data.get("blocks"), list):
        raise BlocksLoadError("unreadable", "champ 'blocks' n'est pas une liste", BLOCKS_PATH)
    if "equipes" not in data:
        raise BlocksLoadError("unreadable", "champ 'equipes' manquant", BLOCKS_PATH)
    if not isinstance(data.get("equipes"), dict):
        raise BlocksLoadError("unreadable", "champ 'equipes' n'est pas un objet", BLOCKS_PATH)
    
    return data


def _parse_block_time(t: str) -> time:
    """Parse HH:MM en time."""
    return parse_time(t)


def _block_is_active(block: dict, now: datetime) -> bool:
    """Vérifie si un bloc est actif à l'instant `now`."""
    if not block.get("actif", False):
        return False
    jours = block.get("jours", [])
    if now.weekday() not in jours:
        return False
    debut = _parse_block_time(block.get("debut", "00:00"))
    fin = _parse_block_time(block.get("fin", "23:59"))
    now_time = now.time()
    if debut <= fin:
        return debut <= now_time <= fin
    # Fenêtre traversant minuit (ex: 22:00-02:00)
    return now_time >= debut or now_time <= fin


def _find_block_for_rule(rule_id: str, blocks_data: dict) -> dict | None:
    """Trouve le bloc qui produit cette règle (via `produces`)."""
    for block in blocks_data.get("blocks", []):
        if rule_id in block.get("produces", []):
            return block
    return None


def _get_team_for_block(block: dict, blocks_data: dict) -> dict | None:
    """Retourne l'équipe à utiliser pour ce bloc.
    
    - Si max_sessions == 2, prend la première équipe qui n'est pas 'locale'.
    - Sinon, prend la première équipe déclarée.
    Retourne None si l'équipe est introuvable dans blocks.json.
    """
    equipes_names = block.get("equipes", [])
    if not equipes_names:
        return None
    
    max_sessions = block.get("max_sessions", 1)
    equipes_def = blocks_data.get("equipes", {})
    
    if max_sessions == 2:
        # Prendre la première qui n'est pas 'locale'
        for name in equipes_names:
            if name != "locale" and name in equipes_def:
                return equipes_def[name]
    
    # Sinon, première disponible
    for name in equipes_names:
        if name in equipes_def:
            return equipes_def[name]
    
    return None


def _substitute_agents(rule: dict, team: dict) -> dict:
    """Retourne une copie de la règle avec les agents substitués par ceux de l'équipe.
    
    Ne modifie pas la règle originale. Les agents de l'équipe priment sur goal.agents.
    L'orchestrator reste toujours opencode-go/deepseek-v4.1-flash.
    """
    import copy
    rule_copy = copy.deepcopy(rule)
    goal = rule_copy.get("goal")
    if isinstance(goal, dict) and goal.get("enabled"):
        # L'équipe fournit worker, reviewer, merger
        agents = {
            "worker": team.get("worker"),
            "reviewer": team.get("reviewer"),
            "merger": team.get("merger"),
            "orchestrator": "opencode-go/deepseek-v4.1-flash",
        }
        # Filtrer les None
        agents = {k: v for k, v in agents.items() if v}
        goal["agents"] = agents
    return rule_copy


# --------------------------------------------------------------------------- #
# Schéma / validation
# --------------------------------------------------------------------------- #
def parse_time(value) -> time:
    if value in (None, ""):
        return time(9, 0)
    match = TIME_RE.match(str(value).strip())
    if not match:
        raise ValueError(f"heure invalide '{value}' (attendu HH:MM, ex: 09:00)")
    return time(int(match.group(1)), int(match.group(2)))


def parse_weekdays(value) -> list[int]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        items = [v for v in re.split(r"[,\s]+", value) if v]
    else:
        items = list(value)
    out: list[int] = []
    for item in items:
        key = str(item).strip().lower()
        if key.isdigit():
            num = int(key)
            if num == 7:
                num = 6  # tolérance dimanche=7
            if not 0 <= num <= 6:
                raise ValueError(f"jour de semaine invalide '{item}' (0=lundi .. 6=dimanche)")
        elif key in WEEKDAY_ALIASES:
            num = WEEKDAY_ALIASES[key]
        else:
            raise ValueError(f"jour de semaine inconnu '{item}' (lun/mar/mer/jeu/ven/sam/dim)")
        if num not in out:
            out.append(num)
    return sorted(out)


def build_schedule(
    kind: str,
    *,
    time_value="09:00",
    start=None,
    weekdays=None,
    every_days=None,
    day_of_month=None,
    month=None,
    day=None,
) -> dict:
    kind = (kind or "").strip().lower()
    if kind not in VALID_KINDS:
        raise ValueError(f"fréquence inconnue '{kind}' (daily|every|weekly|monthly|yearly)")

    start_s = (start or date.today().isoformat()).strip()
    try:
        start_date = date.fromisoformat(start_s)
    except ValueError as exc:
        raise ValueError(f"date de départ invalide '{start_s}' (attendu YYYY-MM-DD)") from exc

    schedule = {"kind": kind, "time": parse_time(time_value).strftime("%H:%M"), "start": start_date.isoformat()}

    if kind == "every":
        try:
            n = int(every_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("kind=every nécessite every_days (entier >= 1)") from exc
        if n < 1:
            raise ValueError("every_days doit être >= 1")
        schedule["everyDays"] = n
    elif kind == "weekly":
        days = parse_weekdays(weekdays)
        if not days:
            raise ValueError("kind=weekly nécessite weekdays (ex: ['mon','thu'])")
        schedule["weekdays"] = [WEEKDAY_SHORT[d] for d in days]
    elif kind == "monthly":
        dom = int(day_of_month or start_date.day)
        if not 1 <= dom <= 31:
            raise ValueError("day_of_month doit être entre 1 et 31")
        schedule["dayOfMonth"] = dom
    elif kind == "yearly":
        mo = int(month or start_date.month)
        dy = int(day or start_date.day)
        if not 1 <= mo <= 12:
            raise ValueError("month doit être entre 1 et 12")
        if not 1 <= dy <= 31:
            raise ValueError("day doit être entre 1 et 31")
        schedule["month"] = mo
        schedule["day"] = dy
    return schedule


def normalize_labels(labels) -> list[str]:
    if labels in (None, "", []):
        return list(DEFAULT_LABELS)
    if isinstance(labels, str):
        items = [v for v in labels.split() if v]
    else:
        items = [str(v) for v in labels]
    out = []
    for item in items:
        item = item.strip().replace(" ", "-")
        if not item:
            continue
        if item[0] not in "+@":
            item = "+" + item
        if item not in out:
            out.append(item)
    return out or list(DEFAULT_LABELS)


VALID_AGENT_ROLES = ("orchestrator", "worker", "reviewer", "merger")


def _model_ref(value) -> dict | None:
    """Normalise une référence de modèle ('provider/id', 'provider/id#variant' ou objet)."""
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        if not value.get("providerID") or not value.get("id"):
            raise ValueError("goal.model doit contenir providerID et id")
        out = {"providerID": str(value["providerID"]), "id": str(value["id"])}
        if value.get("variant"):
            out["variant"] = str(value["variant"])
        return out
    text = str(value).strip()
    variant = None
    if "#" in text:
        text, variant = text.split("#", 1)
    if "/" not in text:
        raise ValueError("goal.model attendu 'provider/model' ou {providerID,id}")
    provider, model_id = text.split("/", 1)
    if not provider or not model_id:
        raise ValueError("goal.model attendu 'provider/model'")
    out = {"providerID": provider, "id": model_id}
    if variant:
        out["variant"] = variant
    return out


def _format_model(ref: dict) -> str:
    """Formate une référence de modèle interne en chaîne 'provider/id#variant'."""
    if not ref:
        return ""
    out = f"{ref['providerID']}/{ref['id']}"
    if ref.get("variant"):
        out += f"#{ref['variant']}"
    return out


def _parse_agents_string(agents_str: str) -> dict:
    """Parse la forme chaîne 'worker=provider/model, reviewer=provider/model' en objet.
    
    Accepte séparateurs virgule et/ou espaces. Clé inconnue -> ValueError.
    Valeur vide ignorée silencieusement.
    """
    result = {}
    # Split par virgules d'abord, puis par espaces pour chaque partie
    parts = [p.strip() for p in agents_str.split(",") if p.strip()]
    for part in parts:
        # Chaque partie peut contenir plusieurs key=value séparés par espaces
        for token in part.split():
            if "=" not in token:
                continue
            key, val = token.split("=", 1)
            key = key.strip().lower()
            val = val.strip()
            if not key or not val:
                continue
            if key not in VALID_AGENT_ROLES:
                raise ValueError(f"rôle d'agent inconnu '{key}' (attendu: {', '.join(VALID_AGENT_ROLES)})")
            # Validation via _model_ref (lève ValueError si format invalide)
            _model_ref(val)
            result[key] = _format_model(_model_ref(val))
    return result


# --------------------------------------------------------------------------- #
# Parsing des étapes du manifest
# --------------------------------------------------------------------------- #
def parseSteps(text: str) -> list[dict]:
    """Parse un manifest markdown et extrait les étapes (e1, e2, ...).
    
    Retourne une liste de dicts avec clés : id, qui, modele, perimetre.
    Les étapes sont identifiées par des sections commençant par '## eX —'.
    Chaque étape contient des lignes '- id:', '- Qui:', '- Modèle:', '- Périmètre:'.
    """
    import re
    steps = []
    # Pattern pour trouver les sections d'étape : ## e1 — titre
    step_pattern = re.compile(r'^##\s+(e\d+)\s*—\s*.+$', re.MULTILINE)
    step_matches = list(step_pattern.finditer(text))
    
    for i, match in enumerate(step_matches):
        step_id = match.group(1)
        # Début de l'étape = position du match
        start = match.end()
        # Fin = début de l'étape suivante ou fin du texte
        end = step_matches[i + 1].start() if i + 1 < len(step_matches) else len(text)
        step_text = text[start:end]
        
        # Extraire les champs
        fields = {}
        for field_name in ['id', 'Qui', 'Modèle', 'Périmètre']:
            # Pattern : - id: value (peut être multi-lignes pour Périmètre)
            if field_name == 'Périmètre':
                # Périmètre peut s'étendre sur plusieurs lignes jusqu'au prochain champ ou fin
                pattern = re.compile(rf'^\s*-\s*{re.escape(field_name)}:\s*(.+?)(?=\n\s*-\s*(?:id|Qui|Modèle|Périmètre|Entrée|Retour|Vérif|Statut):|\n\s*##|\Z)', re.MULTILINE | re.DOTALL)
            else:
                pattern = re.compile(rf'^\s*-\s*{re.escape(field_name)}:\s*(.+)$', re.MULTILINE)
            m = pattern.search(step_text)
            if m:
                fields[field_name.lower()] = m.group(1).strip()
        
        if fields.get('id') == step_id:
            steps.append({
                'id': fields.get('id', ''),
                'qui': fields.get('qui', ''),
                'modele': fields.get('modèle', ''),
                'perimetre': fields.get('périmètre', ''),
            })
    
    return steps


def normalize_goal(value, title: str) -> dict | None:
    """Normalise le champ `goal` d'une règle.

    absent / False / None / {} -> None (goal désactivé)
    True / chaîne -> objectif = titre / chaîne
    objet -> {enabled, objective, agent?, model?, manifest?, agents?}
    
    agents: objet {role: "provider/model"} ou chaîne "worker=..., reviewer=..."
    """
    if value in (None, False, "", {}):
        return None
    if isinstance(value, bool):
        value = {}
    if isinstance(value, str):
        value = {"objective": value}
    if not isinstance(value, dict):
        raise ValueError("goal invalide (booléen, chaîne ou objet attendu)")
    if value.get("enabled") is False:
        return None
    objective = str(value.get("objective") or title or "").strip()
    if not objective:
        raise ValueError("goal.objective vide")
    out = {"enabled": True, "objective": objective}
    agent = str(value.get("agent") or "").strip()
    if agent:
        out["agent"] = agent
    if value.get("model") not in (None, ""):
        _model_ref(value["model"])  # validation immédiate
        out["model"] = value["model"]
    manifest = str(value.get("manifest") or "").strip()
    if manifest:
        if ".." in manifest:
            raise ValueError("goal.manifest invalide")
        resolved = Path(manifest)
        if not resolved.is_absolute():
            resolved = PROJECT_ROOT / resolved
        if not resolved.is_file():
            raise ValueError(f"goal.manifest introuvable : {resolved}")
        out["manifest"] = manifest
    agents = value.get("agents")
    if agents is not None:
        if isinstance(agents, str):
            agents_str = agents.strip()
            if agents_str:
                # Forme chaîne : parse et normalise en objet
                parsed = _parse_agents_string(agents_str)
                if parsed:
                    out["agents"] = parsed
        elif isinstance(agents, dict):
            # Forme objet : valider chaque rôle et valeur
            normalized = {}
            for role, model_ref in agents.items():
                if role not in VALID_AGENT_ROLES:
                    raise ValueError(f"rôle d'agent inconnu '{role}' (attendu: {', '.join(VALID_AGENT_ROLES)})")
                if model_ref in (None, ""):
                    # Valeur vide -> ignorée silencieusement
                    continue
                # Validation via _model_ref (lève ValueError si format invalide)
                _model_ref(model_ref)
                # Stocker sous forme string 'provider/id#variant' (PAS le dict interne)
                normalized[role] = _format_model(_model_ref(model_ref))
            if normalized:
                out["agents"] = normalized
        else:
            raise ValueError("goal.agents doit être un objet ou une chaîne")
    return out


def goal_enabled(rule: dict) -> bool:
    goal = rule.get("goal")
    return bool(isinstance(goal, dict) and goal.get("enabled"))


def manifest_warning(goal: dict | None, title: str) -> str | None:
    """Avertissement non bloquant : le manifest ne colle pas au titre de la règle.

    Une règle nommée « Run find_x_leads » qui pointe sur find_x_visibility.md
    exécute silencieusement le mauvais workflow : on le signale à la création
    et à la modification, sans refuser (le titre reste libre).
    """
    if not isinstance(goal, dict):
        return None
    manifest = str(goal.get("manifest") or "").strip()
    if not manifest:
        return None
    stem = Path(manifest).stem.lower()
    normalized_title = re.sub(r"[^a-z0-9]+", "_", str(title or "").lower()).strip("_")
    if not stem or stem in normalized_title:
        return None
    return (
        f"le manifest « {Path(manifest).name} » ne correspond pas au titre "
        f"« {title} » : vérifie que la règle lance bien le workflow attendu"
    )


# --------------------------------------------------------------------------- #
# Calcul des échéances
# --------------------------------------------------------------------------- #
def _at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock)


def next_occurrence(schedule: dict, after: datetime) -> datetime:
    """Première occurrence >= `after` (datetime local naïf)."""
    clock = parse_time(schedule.get("time", "09:00"))
    start = date.fromisoformat(schedule["start"])
    kind = schedule["kind"]

    if kind == "daily":
        day = max(start, after.date())
        candidate = _at(day, clock)
        if candidate < after:
            candidate = _at(day + timedelta(days=1), clock)
        return candidate

    if kind == "every":
        step = int(schedule.get("everyDays", 1))
        base = _at(start, clock)
        if after <= base:
            return base
        k = (after.date() - start).days // step
        candidate = base + timedelta(days=k * step)
        while candidate < after:
            candidate = base + timedelta(days=((candidate - base).days // step + 1) * step)
        return candidate

    if kind == "weekly":
        wanted = {WEEKDAY_ALIASES[w] for w in schedule.get("weekdays", []) if w in WEEKDAY_ALIASES}
        if not wanted:
            raise ValueError("règle weekly sans jours valides")
        day = max(start, after.date())
        for offset in range(0, 21):
            current = day + timedelta(days=offset)
            if current.weekday() in wanted:
                candidate = _at(current, clock)
                if candidate >= after:
                    return candidate
        raise ValueError("aucune occurrence hebdomadaire trouvée")

    if kind == "monthly":
        dom = int(schedule.get("dayOfMonth", 1))
        year, month_num = after.year, after.month
        if (year, month_num) < (start.year, start.month):
            year, month_num = start.year, start.month
        for _ in range(60):
            last = calendar.monthrange(year, month_num)[1]
            day = date(year, month_num, min(dom, last))
            candidate = _at(day, clock)
            if day >= start and candidate >= after:
                return candidate
            month_num += 1
            if month_num == 13:
                month_num, year = 1, year + 1
        raise ValueError("aucune occurrence mensuelle trouvée")

    if kind == "yearly":
        month_num = int(schedule.get("month", start.month))
        dom = int(schedule.get("day", start.day))
        year = max(start.year, after.year)
        for _ in range(12):
            last = calendar.monthrange(year, month_num)[1]
            day = date(year, month_num, min(dom, last))
            candidate = _at(day, clock)
            if day >= start and candidate >= after:
                return candidate
            year += 1
        raise ValueError("aucune occurrence annuelle trouvée")

    raise ValueError(f"fréquence inconnue '{kind}'")


def rule_next_run(rule: dict, after: datetime, keep_past: bool = False) -> datetime:
    """Prochaine échéance d'une règle.

    keep_past=True (moteur/tick) : on repart du nextRun stocké même s'il est
    dépassé, pour rattraper les occurrences manquées.
    keep_past=False (affichage, reprise) : on repart de `after` si le stock est
    périmé, pour ne pas déclencher une avalanche d'occurrences.
    """
    schedule = rule["schedule"]
    start = date.fromisoformat(schedule["start"])
    floor = _at(start, parse_time(schedule.get("time", "09:00")))
    if after < floor:
        after = floor
    stored = rule.get("nextRun")
    if stored:
        try:
            parsed = datetime.fromisoformat(stored)
            if keep_past or parsed >= after:
                return parsed
        except ValueError:
            pass
    return next_occurrence(schedule, after)


def describe_schedule(schedule: dict) -> str:
    kind = schedule.get("kind", "?")
    clock = schedule.get("time", "09:00")
    if kind == "every":
        return f"tous les {schedule.get('everyDays')} jours à {clock}"
    if kind == "weekly":
        days = ", ".join(WEEKDAY_SHORT[WEEKDAY_ALIASES[d]] for d in schedule.get("weekdays", []))
        return f"hebdo ({days}) à {clock}"
    if kind == "monthly":
        return f"le {schedule.get('dayOfMonth')} du mois à {clock}"
    if kind == "yearly":
        return f"le {schedule.get('day')}/{schedule.get('month'):02d} chaque année à {clock}"
    if kind == "daily":
        return f"tous les jours à {clock}"
    return f"{kind} à {clock}"


# --------------------------------------------------------------------------- #
# Kanban (destination + dédup)
# --------------------------------------------------------------------------- #
def _http(method: str, path: str, payload=None) -> tuple[bool, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        KANBAN_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=KANBAN_TIMEOUT) as response:
            body = response.read().decode() or "{}"
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode()[:200]
        except Exception:  # noqa: BLE001
            detail = ""
        return False, {"error": f"Kanban HTTP {exc.code} : {detail}"}
    except OSError as exc:
        return False, {"error": f"Kanban injoignable ({KANBAN_URL}) : {exc}"}
    try:
        return True, json.loads(body)
    except ValueError:
        return False, {"error": "réponse non-JSON du Kanban"}


def marker_for(rule: dict, occurrence: datetime) -> str:
    return f"[{rule['id']}-{occurrence:%Y%m%d}]"


def card_exists(marker: str) -> tuple[bool, str | None]:
    ok, board = _http("GET", "/api/board")
    if not ok:
        return False, board.get("error")
    for column in board.get("columns", []):
        for task in column.get("tasks", []):
            haystack = f"{task.get('text', '')} {task.get('raw', '')}"
            if marker in haystack:
                return True, column.get("id")
    return False, None


def card_text(rule: dict, occurrence: datetime) -> str:
    labels = rule.get("labels") or list(DEFAULT_LABELS)
    return " ".join([rule["title"], *labels, marker_for(rule, occurrence)])


def int_kanban(rule: dict, occurrence: datetime, dry_run: bool) -> dict:
    """Crée (ou simule) la carte. Idempotent via le marqueur de la règle."""
    marker = marker_for(rule, occurrence)
    exists, where = card_exists(marker)
    if exists:
        return {"ok": True, "dedup": True, "column": where, "marker": marker}
    if where is not None:
        # card_exists a renvoyé une erreur (Kanban injoignable, HTTP, ...).
        return {"ok": False, "error": where, "marker": marker}
    if dry_run:
        return {"ok": True, "dryRun": True, "marker": marker, "text": card_text(rule, occurrence)}
    ok, payload = _http("POST", "/api/add", {"text": card_text(rule, occurrence)})
    if not ok:
        return {"ok": False, "error": payload.get("error", "échec Kanban"), "marker": marker}
    return {"ok": True, "dedup": False, "marker": marker}


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def telegram_message(rule: dict, occurrence: datetime, card: dict, goal: dict | None = None) -> str:
    lines = [
        "🔁 Tâche récurrente OpenCode",
        rule["title"],
        f"Échéance : {occurrence:%d/%m/%Y %H:%M}",
    ]
    if card.get("dedup"):
        lines.append("Carte : déjà présente dans le Kanban")
    elif card.get("dryRun"):
        lines.append("Carte : serait créée en Inbox (dry-run)")
    elif card.get("ok"):
        lines.append("Carte : créée en Inbox (+Opencode @recurrent)")
    else:
        lines.append("Carte : échec de création")
    if goal and not goal.get("disabled"):
        if goal.get("dryRun"):
            lines.append("Goal : serait lancé (dry-run)")
        elif goal.get("skipped"):
            lines.append("Goal : non lancé (occurrence antérieure rattrapée)")
        elif goal.get("ok"):
            short = str(goal.get("sessionID") or "")[-8:]
            title = goal.get("title") or "session goal"
            lines.append(f"Goal : lancé — {title}" + (f" (…{short})" if short else ""))
        else:
            detail = str(goal.get("error", "inconnu"))
            short = str(goal.get("sessionID") or "")[-8:]
            if short:
                detail += f" (session créée …{short}, à vérifier)"
            lines.append(f"Goal : échec — {detail}")
    lines.append("Kanban : http://127.0.0.1:8787")
    return "\n".join(lines)


def send_telegram(message: str) -> tuple[bool, str | None]:
    script = Path(TELEGRAM_SCRIPT)
    if not script.is_file():
        return False, f"script Telegram introuvable : {script}"
    try:
        proc = subprocess.run(
            [str(script)],
            input=message,
            text=True,
            capture_output=True,
            timeout=25,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or f"code {proc.returncode}").strip()[:300]
    return True, None


# --------------------------------------------------------------------------- #
# Goal mode (sessions agent + watcher)
# --------------------------------------------------------------------------- #
def _opencode_env() -> dict:
    """Env enrichi pour le CLI `opencode-v2 api`.

    Copie de os.environ enrichie de la variable d'auth gagnante (OPENCODE_PASSWORD),
    lue dans OPENCODE_V2_ENV_FILE. Lecture silencieuse : fichier absent ou
    illisible => dict(os.environ) inchangé, jamais d'exception, jamais de log de
    la valeur.
    """
    env = dict(os.environ)
    try:
        raw = OPENCODE_V2_ENV_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return env
    for line in raw.splitlines():
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or not stripped.startswith("OPENCODE_SERVER_PASSWORD=")
        ):
            continue
        env["OPENCODE_PASSWORD"] = stripped.split("=", 1)[1].strip()
    return env


def _opencode_api(method: str, path: str, payload=None) -> tuple[bool, dict]:
    """Appelle le CLI `opencode-v2 api` (JSON sur stdout, bannière sur stderr)."""
    args = [OPENCODE_BIN, "api", "--server", OPENCODE_SERVER_URL, method, path]
    if payload is not None:
        args += ["-d", json.dumps(payload, ensure_ascii=False)]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=OPENCODE_API_TIMEOUT,
            env=_opencode_env(),
        )
    except subprocess.TimeoutExpired:
        return False, {
            "error": f"OpenCode API {method} {path} : timeout après {OPENCODE_API_TIMEOUT:.0f}s",
            "timeout": True,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return False, {"error": f"OpenCode CLI ({OPENCODE_BIN}) : {exc}"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"code {proc.returncode}").strip()
        return False, {"error": f"OpenCode API {method} {path} : {detail[:400]}"}
    body = (proc.stdout or "").strip()
    if not body:
        return True, {}
    try:
        data = json.loads(body)
    except ValueError:
        return False, {"error": f"réponse non-JSON de l'API OpenCode : {body[:200]}"}
    return True, data if isinstance(data, dict) else {"data": data}


def goal_state_path() -> Path:
    """Localise goals.json (le cron n'a pas forcément XDG_DATA_HOME)."""
    env = os.environ.get("OPENCODE_GOAL_STATE_PATH")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        candidate = Path(xdg) / "opencode-goal-plugin" / "goals.json"
        if candidate.exists():
            return candidate
    project_candidate = PROJECT_ROOT / "Opencodev2" / "data" / "opencode-goal-plugin" / "goals.json"
    if project_candidate.exists():
        return project_candidate
    return Path.home() / ".local" / "share" / "opencode-goal-plugin" / "goals.json"


def load_goal_state() -> dict:
    path = goal_state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"error": f"état des goals introuvable : {path}"}
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"état des goals illisible ({path}) : {exc}"}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return {"error": f"état des goals invalide ({path}) : {exc}"}
    if not isinstance(data, dict):
        return {"error": f"état des goals invalide ({path}) : objet attendu"}
    return data


def launch_goal(rule: dict, occurrence: datetime, dry_run: bool = False) -> dict:
    """Crée une session agent et lance /goal avec l'objectif de la règle."""
    goal = rule.get("goal") if isinstance(rule.get("goal"), dict) else {}
    if not goal.get("enabled"):
        return {"ok": True, "disabled": True}
    objective = str(goal.get("objective") or rule.get("title") or "").strip()
    manifest_path = str(goal.get("manifest") or "").strip()
    
    # Construire l'objective avec sections séparées par \n\n
    # Ordre : Manifest (si présent), Modèles (si agents non vide), puis l'objective
    sections = []
    if manifest_path:
        sections.append(f"Manifest: {manifest_path}")
    
    agents = goal.get("agents")
    if agents and isinstance(agents, dict) and agents:
        # Ordre fixe : orchestrator -> worker -> reviewer -> merger
        model_parts = []
        for role in VALID_AGENT_ROLES:
            ref = agents.get(role)
            if ref:
                # ref est déjà stocké sous forme string 'provider/id#variant'
                model_parts.append(f"{role}={ref}")
        if model_parts:
            sections.append("Modèles: " + ", ".join(model_parts))
    
    if sections:
        objective = "\n\n".join(sections + [objective])
    
    if not objective:
        return {"ok": False, "error": "objectif de goal vide"}
    title = f"[récurrent] {rule.get('title', '')} — {occurrence:%d/%m %H:%M}"
    if len(title) > 120:
        title = title[:117] + "…"
    if dry_run:
        return {"ok": True, "dryRun": True, "title": title, "objective": objective}

    # Calcul de l'agent selon la règle :
    # - goal.agent explicite → cet agent
    # - sinon, si manifest ou agents non vide → "orchestrator-auto"
    # - sinon → "build"
    explicit_agent = str(goal.get("agent") or "").strip()
    if explicit_agent:
        agent_name = explicit_agent
    elif manifest_path or (isinstance(agents, dict) and agents):
        agent_name = "orchestrator-auto"
    else:
        agent_name = "build"

    payload = {
        "title": title,
        "agent": agent_name,
        "location": {"directory": str(PROJECT_ROOT)},
    }
    
    # Résolution du modèle de session : agents.orchestrator prime sur goal.model
    session_model = None
    if agents and isinstance(agents, dict):
        orchestrator_ref = agents.get("orchestrator")
        if orchestrator_ref:
            try:
                session_model = _model_ref(orchestrator_ref)
            except ValueError as exc:
                return {"ok": False, "error": str(exc), "title": title}
    
    if not session_model and goal.get("model") not in (None, ""):
        try:
            session_model = _model_ref(goal.get("model"))
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "title": title}
    
    if session_model:
        payload["model"] = session_model

    ok, data = _opencode_api("POST", "/api/session", payload)
    if not ok:
        return {"ok": False, "error": data.get("error", "création de session impossible"), "title": title}
    session = data.get("data") or {}
    session_id = session.get("id")
    if not session_id:
        return {"ok": False, "error": "session créée sans id", "title": title}

    # Vérifier l'agent UNIQUEMENT via GET /api/session/{id}
    # Ne jamais conclure depuis le champ agent de la réponse POST (peut être absent)
    ok2, data2 = _opencode_api("GET", f"/api/session/{session_id}")
    if ok2:
        session2 = data2.get("data") or {}
        actual_agent = session2.get("agent")
        if actual_agent != agent_name:
            _opencode_api("DELETE", f"/api/session/{session_id}")  # best effort
            return {
                "ok": False,
                "error": f"agent {agent_name} refusé par l'API : session créée avec agent={actual_agent!r}",
                "title": title,
                "sessionID": session_id,
            }
    else:
        # GET en échec technique : ne PAS supprimer la session, ne PAS bloquer
        # Poursuivre le lancement avec un warning
        warning = f"impossible de vérifier l'agent via GET /api/session/{session_id} : {data2.get('error', 'inconnu')}"

    ok, data = _opencode_api(
        "POST",
        f"/api/session/{session_id}/command",
        {"name": "goal", "text": objective},
    )
    accepted_warning = None
    if not ok:
        # Un 499 (« client closed »), un 408 ou un timeout signifient presque
        # toujours que la commande a bien été acceptée mais que la réponse n'est
        # jamais revenue. La session est alors probablement VIVANTE : la
        # supprimer laisse des écritures orphelines (FOREIGN KEY constraint
        # failed) et un onglet TUI figé sur une session morte.
        error_text = str(data.get("error") or "")
        status_text = str(
            data.get("status") or data.get("code") or data.get("statusCode") or ""
        )
        haystack = f"{error_text} {status_text}".lower()
        accepted_reason = None
        if data.get("timeout"):
            accepted_reason = "timeout"
        elif "client closed" in haystack:
            accepted_reason = "client closed"
        elif "499" in haystack:
            accepted_reason = "499"
        elif "408" in haystack:
            accepted_reason = "408"
        elif "timeout" in haystack:
            accepted_reason = "timeout"
        if accepted_reason:
            label = "timeout" if accepted_reason == "timeout" else f"{accepted_reason}/timeout"
            accepted_warning = (
                f"commande envoyée, réponse non attendue ({label}) ; vérifie la session"
            )
        else:
            # Un échec de commande ne justifie JAMAIS de détruire la session :
            # on la laisse exister et on remonte la raison exacte.
            print(
                f"[recurring-launch] commande goal refusée (session {session_id} conservée) : "
                f"{error_text[:300] or 'raison inconnue'}",
                file=sys.stderr,
            )
            return {
                "ok": False,
                "error": data.get("error", "lancement du goal impossible"),
                "title": title,
                "sessionID": session_id,
            }

    # Un seul GET de contrôle après l'envoi (chemin nominal ET chemin accepté) :
    # une session disparue juste après le lancement doit être signalée.
    probe_ok, probe = _opencode_api("GET", f"/api/session/{session_id}")
    if not probe_ok:
        probe_text = json.dumps(probe, ensure_ascii=False, default=str).lower()
        if (
            "404" in probe_text
            or "sessionnotfound" in probe_text
            or "not found" in probe_text
        ):
            return {
                "ok": False,
                "error": "session morte après lancement",
                "title": title,
                "sessionID": session_id,
            }

    # Le chemin accepté (499/timeout) ne retourne PAS ici : le marqueur de
    # session récurrente doit être écrit sur les deux chemins. Le warning est
    # simplement mémorisé et ajouté au résultat final.
    # Écrit le marqueur de session récurrente (best-effort, ne casse jamais le lancement)
    if manifest_path:
        try:
            orch_dir = PROJECT_ROOT / ".opencode" / "orch" / session_id
            orch_dir.mkdir(parents=True, exist_ok=True)
            source = (PROJECT_ROOT / manifest_path).read_text(encoding="utf-8")
            (orch_dir / "recurring-source.md").write_text(source, encoding="utf-8")
            marker = orch_dir / "recurring.json"
            with marker.open("w", encoding="utf-8") as f:
                json.dump({
                    "manifest": manifest_path,
                    "rule": rule.get("title", ""),
                    "occurrence": occurrence.isoformat(timespec="minutes"),
                    "session": session_id,
                    "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                }, f)
        except Exception as e:
            print(f"[recurring-marker] échec écriture {session_id}: {e}", file=sys.stderr)

    result = {
        "ok": True,
        "sessionID": session_id,
        "title": title,
        "objective": objective,
    }
    if 'warning' in locals():
        result["warning"] = warning
    if accepted_warning is not None:
        if "warning" in result:
            result["warning"] = result["warning"] + "; " + accepted_warning
        else:
            result["warning"] = accepted_warning
    return result


RESUME_GOAL_PROMPT = (
    'Resume the current session goal by calling update_goal_status with status "active", '
    "then continue working toward it."
)


def resume_goal(session_id: str) -> tuple[bool, dict]:
    # On passe par /prompt (asynchrone) : /command attend la fin du tour et peut
    # dépasser le timeout. Ce texte est celui que le plugin goal reconnaît comme
    # une demande de reprise explicite (messages.tui.resumePrompt).
    return _opencode_api(
        "POST",
        f"/api/session/{session_id}/prompt",
        {"text": RESUME_GOAL_PROMPT},
    )


def goal_run_entry(rule: dict, occurrence: datetime, launch: dict, now: datetime) -> dict:
    goal = rule.get("goal") or {}
    entry = {
        "sessionID": launch.get("sessionID"),
        "ruleID": rule.get("id"),
        "title": rule.get("title"),
        "occurrence": occurrence.isoformat(timespec="minutes"),
        "objective": launch.get("objective"),
        "sessionTitle": launch.get("title"),
        "launchedAt": now.isoformat(timespec="seconds"),
        "resumes": 0,
        "lastResumeAt": None,
        "status": "active",
        "notifiedStatus": None,
    }
    agents = goal.get("agents")
    if agents:
        entry["agents"] = agents
    return entry


def prune_goal_runs(runs: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    cutoff = now - timedelta(days=GOAL_RUNS_KEEP_DAYS)
    kept: list[dict] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("notifiedStatus") and run.get("launchedAt"):
            try:
                if datetime.fromisoformat(run["launchedAt"]) < cutoff:
                    continue
            except ValueError:
                pass
        kept.append(run)
    return kept[-GOAL_RUNS_MAX:]


# --------------------------------------------------------------------------- #
# Tick
# --------------------------------------------------------------------------- #
def _log(path: Path, message: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass


def tick(now: datetime | None = None, dry_run: bool = False, log_path: Path | None = None) -> dict:
    now = now or datetime.now()
    result = {
        "now": now.isoformat(timespec="seconds"),
        "dryRun": dry_run,
        "created": [],
        "goals": [],
        "notified": 0,
        "errors": [],
        "skipped": [],
        "paused": False,
    }

    # Pause globale (t40) : elle arrête TOUS les récurrents. Ni carte, ni goal,
    # ni Telegram, ni écriture du store. Les nextRun ne bougent pas : la reprise
    # rattrapera dans la limite de CATCHUP_MAX.
    if est_en_pause():
        motif = motif_pause()
        result["paused"] = True
        try:
            regles = store.load().get("rules", [])
        except ValueError as exc:
            result["errors"].append(str(exc))
            regles = []
        for regle in regles:
            if regle.get("paused"):
                continue
            identifiant = regle.get("id")
            result["skipped"].append(f"{identifiant}: skipped: pause")
            if log_path is not None:
                _log(
                    log_path,
                    f"{identifiant} {now:%Y-%m-%d %H:%M:%S} : skipped: pause"
                    + (f" ({motif})" if motif else ""),
                )
        if log_path is not None:
            _log(log_path, f"tick pause : aucun lancement ({motif or 'sans motif'})")
        return result

    try:
        data = store.load()
    except ValueError as exc:
        result["errors"].append(str(exc))
        return result

    # Charger les blocs une seule fois
    try:
        blocks_data = _load_blocks()
    except BlocksLoadError as exc:
        # Erreur de chargement des blocs : ne pas traiter aucune règle,
        # ne pas avancer nextRun, retourner l'erreur explicitement
        result["errors"].append(f"blocks_{exc.kind}: {exc.detail}")
        # Ne pas faire de mise à jour du store
        if log_path is not None:
            _log(log_path, f"tick dry={dry_run} error: blocks_{exc.kind} ({exc.detail})")
        return result

    # L'usine est vivante ? Lu une seule fois pour tout le tick (le battement
    # ne doit pas changer d'avis entre deux règles).
    usine_active = usine_vivante()

    updates: dict[str, dict] = {}
    new_goal_runs: list[dict] = []

    for rule in data.get("rules", []):
        if rule.get("paused"):
            continue
        try:
            nxt = rule_next_run(rule, now, keep_past=True)
        except (KeyError, ValueError, TypeError) as exc:
            result["errors"].append(f"{rule.get('id', '?')} : règle invalide ({exc})")
            continue

        # Vérifier si la règle appartient à un bloc (avant de calculer les occurrences)
        rule_id = rule.get("id")
        block = _find_block_for_rule(rule_id, blocks_data)
        
        if block and usine_active:
            # L'usine pilote les règles de ses blocs : engine s'efface pour ne
            # pas doubler session/Telegram/Sheet. nextRun n'est PAS touché :
            # c'est le planning de l'usine qui fait foi, celui du moteur ne
            # doit pas dériver. Au prochain tick, si l'usine meurt, la règle
            # redevient due et sera rattrapée par CATCHUP_MAX.
            result["skipped"].append(f"{rule_id}: skipped: bloc piloté par l'usine")
            if log_path is not None:
                _log(
                    log_path,
                    f"{rule_id} {now:%Y-%m-%d %H:%M:%S} : skipped: bloc piloté par l'usine",
                )
            continue

        if block:
            # t60 : l'usine est morte (battement absent ou périmé) mais elle a
            # déjà lancé cette règle AUJOURD'HUI avant de mourir. Le registre
            # partagé, écrit par act.py, est la seule mémoire qui survit : sans
            # lui, engine relancerait la même règle (2 sessions, 2 Telegram,
            # 2 écritures Sheet). Dédup journalière, pas éternelle.
            if not usine_active and deja_lancee_ce_jour(
                rule_id, now.date().isoformat()
            ):
                result["skipped"].append(f"{rule_id}: {TEXTE_DEJA_LANCEE}")
                if log_path is not None:
                    _log(
                        log_path,
                        f"{rule_id} {now:%Y-%m-%d %H:%M:%S} : {TEXTE_DEJA_LANCEE}",
                    )
                continue
            # La règle appartient à un bloc : vérifier si le bloc est actif maintenant
            if not _block_is_active(block, now):
                # Bloc inactif : sauter la règle, avancer nextRun, logger skipped: block
                result["skipped"].append(f"{rule_id}: skipped: block")
                _log(log_path, f"{rule_id} {now:%Y-%m-%d %H:%M:%S} : skipped: block") if log_path else None
                # Calculer le nextRun suivant pour l'avancer (même logique que ci-dessous)
                truncated = False
                if nxt <= now:
                    truncated = True
                    nxt = next_occurrence(rule["schedule"], now + timedelta(seconds=1))
                updates[rule["id"]] = {"nextRun": nxt.isoformat(timespec="minutes")}
                continue
            # Bloc actif : on continue pour traiter les occurrences avec l'équipe du bloc
            team = _get_team_for_block(block, blocks_data)
            if not team:
                result["errors"].append(f"{rule_id} {now:%Y-%m-%d} : error: team_not_found")
                _log(log_path, f"{rule_id} {now:%Y-%m-%d %H:%M:%S} : error: team_not_found") if log_path else None
                # Avancer nextRun pour ne pas boucler
                truncated = False
                if nxt <= now:
                    truncated = True
                    nxt = next_occurrence(rule["schedule"], now + timedelta(seconds=1))
                updates[rule["id"]] = {"nextRun": nxt.isoformat(timespec="minutes")}
                continue
            # Substituer les agents par ceux de l'équipe du bloc
            rule = _substitute_agents(rule, team)

        # Calculer les occurrences (pour règles hors bloc, ou règles dans bloc actif)
        occurrences = []
        while nxt <= now and len(occurrences) < CATCHUP_MAX:
            occurrences.append(nxt)
            nxt = next_occurrence(rule["schedule"], nxt + timedelta(seconds=1))

        truncated = False
        if nxt <= now:
            truncated = True
            nxt = next_occurrence(rule["schedule"], now + timedelta(seconds=1))
            result["skipped"].append(rule.get("id"))

        if not occurrences:
            if truncated or rule.get("nextRun") != nxt.isoformat(timespec="minutes"):
                updates[rule["id"]] = {"nextRun": nxt.isoformat(timespec="minutes")}
            continue

        history = []
        failed_at = None
        for occurrence in occurrences:
            entry = {"occurrence": occurrence.isoformat(timespec="minutes")}
            card = {"ok": True, "skipped": True}
            if rule.get("kanban", True):
                card = int_kanban(rule, occurrence, dry_run)
                if not card.get("ok"):
                    result["errors"].append(f"{rule['id']} {occurrence:%Y-%m-%d} : carte non créée ({card.get('error')})")
                    # On n'avance pas au-delà de cette occurrence : retry au prochain tick.
                    failed_at = occurrence
                    break
            entry["card"] = "dedup" if card.get("dedup") else ("created" if not dry_run else "dry-run")
            entry["marker"] = card.get("marker")

            goal_result = None
            if goal_enabled(rule):
                if occurrence != occurrences[-1]:
                    goal_result = {
                        "ok": True,
                        "skipped": True,
                        "reason": "occurrence antérieure (un seul goal par règle et par tick)",
                    }
                    entry["goal"] = "skipped"
                else:
                    try:
                        goal_result = launch_goal(rule, occurrence, dry_run)
                    except Exception as exc:  # noqa: BLE001
                        goal_result = {"ok": False, "error": f"exception : {exc}"}
                    if goal_result.get("dryRun"):
                        entry["goal"] = "dry-run"
                    elif goal_result.get("ok"):
                        entry["goal"] = "launched"
                        entry["goalSession"] = goal_result.get("sessionID")
                        new_goal_runs.append(goal_run_entry(rule, occurrence, goal_result, now))
                    else:
                        entry["goal"] = "failed"
                        result["errors"].append(
                            f"{rule['id']} {occurrence:%Y-%m-%d} : goal non lancé "
                            f"({goal_result.get('error', 'inconnu')})"
                        )
                    result["goals"].append(
                        {
                            "ruleID": rule.get("id"),
                            "title": rule.get("title"),
                            "occurrence": occurrence.isoformat(timespec="minutes"),
                            "ok": bool(goal_result.get("ok")),
                            "dryRun": bool(goal_result.get("dryRun")),
                            "sessionID": goal_result.get("sessionID"),
                            "sessionTitle": goal_result.get("title"),
                            "error": goal_result.get("error"),
                        }
                    )
            else:
                entry["goal"] = "disabled"

            if rule.get("telegram"):
                if dry_run:
                    entry["telegram"] = "dry-run"
                else:
                    ok, err = send_telegram(telegram_message(rule, occurrence, card, goal_result))
                    entry["telegram"] = "sent" if ok else "failed"
                    if ok:
                        result["notified"] += 1
                    else:
                        result["errors"].append(f"{rule['id']} {occurrence:%Y-%m-%d} : Telegram ({err})")
            else:
                entry["telegram"] = "disabled"

            entry["at"] = now.isoformat(timespec="seconds")
            history.append(entry)
            result["created"].append(
                {
                    "id": rule.get("id"),
                    "title": rule.get("title"),
                    "occurrence": occurrence.isoformat(timespec="minutes"),
                    "card": entry["card"],
                    "telegram": entry["telegram"],
                    "goal": entry.get("goal"),
                    "goalSession": entry.get("goalSession"),
                }
            )

        patch = {"nextRun": (failed_at or nxt).isoformat(timespec="minutes")}
        if history:
            patch["lastRun"] = history[-1]["occurrence"]
            patch["history"] = ((rule.get("history") or []) + history)[-HISTORY_MAX:]
        updates[rule["id"]] = patch

    if not dry_run and (updates or new_goal_runs):
        try:
            with store.locked():
                fresh = store.load()
                for current in fresh.get("rules", []):
                    patch = updates.get(current.get("id"))
                    if patch:
                        current.update(patch)
                if new_goal_runs:
                    fresh["goalRuns"] = prune_goal_runs(
                        (fresh.get("goalRuns") or []) + new_goal_runs, now
                    )
                store.save(fresh)
        except (OSError, ValueError) as exc:
            result["errors"].append(f"écriture du store impossible : {exc}")

    if log_path is not None:
        summary = (
            f"tick dry={dry_run} créés={len(result['created'])} "
            f"telegram={result['notified']} erreurs={len(result['errors'])}"
        )
        _log(log_path, summary)
        for error in result["errors"]:
            _log(log_path, f"  erreur: {error}")

    return result


# --------------------------------------------------------------------------- #
# Watcher goal (cron --watch-goals)
# --------------------------------------------------------------------------- #
GOAL_TRACK_FIELDS = (
    "status",
    "autoTurns",
    "maxAutoTurns",
    "tokensUsed",
    "timeUsedSeconds",
    "lastStatus",
    "stopReason",
    "updatedAt",
    "completionEvidence",
    "blocker",
    "notifiedStatus",
    "resumes",
    "lastResumeAt",
    "lastResumeUpdatedAt",
    "resumeFailures",
    "resumeBlockedReason",
)


def _goal_resumable(goal: dict) -> tuple[bool, str | None]:
    stop = str(goal.get("stopReason") or "").lower()
    if "max duration" in stop or ("duration" in stop and "max" in stop):
        return False, "limite de durée atteinte"
    if "token budget" in stop or "budget" in stop:
        return False, "budget de tokens atteint"
    max_duration = goal.get("maxDurationSeconds")
    if max_duration and float(goal.get("timeUsedSeconds") or 0) >= float(max_duration):
        return False, "limite de durée atteinte"
    return True, None


def _goal_notification(run: dict, status: str, goal: dict) -> str:
    title = run.get("title") or "?"
    if status == "complete":
        lines = ["🏁 Goal récurrent terminé", title, "Statut : complete"]
        evidence = goal.get("completionEvidence")
        if evidence:
            lines.append("Preuve : " + str(evidence)[:900])
    elif status == "unmet":
        lines = ["🚫 Goal récurrent non atteint", title, "Statut : unmet"]
        blocker = goal.get("blocker")
        if blocker:
            lines.append("Blocage : " + str(blocker)[:900])
    elif status == "cancelled":
        lines = ["⏹ Goal récurrent annulé", title, "Statut : cancelled"]
    elif status == "paused":
        lines = [
            "⏸ Goal récurrent en pause",
            title,
            "Statut : paused",
            "Le moteur ne relance pas un goal paused (pause manuelle ou absence de progrès).",
        ]
    elif status == "budgetLimited":
        lines = ["⛔ Goal récurrent : budget atteint", title, "Statut : budgetLimited"]
    elif status == "usageLimited-max":
        lines = [
            "⏸ Goal récurrent : plafond de reprises atteint",
            title,
            "Statut : usageLimited",
        ]
    elif status == "usageLimited-blocked":
        lines = [
            "⏸ Goal récurrent : reprise impossible",
            title,
            "Statut : usageLimited",
        ]
        if run.get("resumeBlockedReason"):
            lines.append("Cause : " + str(run["resumeBlockedReason"])[:300])
    elif status == "usageLimited-stuck":
        lines = [
            "⚠️ Goal récurrent : reprise sans effet",
            title,
            "Statut : usageLimited",
            "Trois demandes de reprise n'ont pas relancé le goal. Vérifie la session.",
        ]
    elif status == "missing":
        lines = [
            "⚠️ Goal récurrent introuvable",
            title,
            "La session a peut-être été supprimée ou le goal détaché.",
        ]
    else:
        lines = ["🔔 Goal récurrent", title, f"Statut : {status}"]

    if run.get("lastStatus") and status not in ("complete", "unmet"):
        lines.append("Détail : " + str(run["lastStatus"])[:500])
    if run.get("stopReason") and status not in ("complete", "unmet", "paused"):
        lines.append("Raison : " + str(run["stopReason"])[:400])
    lines.append(f"Reprises : {int(run.get('resumes') or 0)}")
    if run.get("sessionTitle"):
        lines.append("Session : " + str(run["sessionTitle"]))
    elif run.get("sessionID"):
        lines.append("Session : " + str(run["sessionID"]))
    return "\n".join(lines)


def watch_goals(
    now: datetime | None = None,
    dry_run: bool = False,
    log_path: Path | None = None,
) -> dict:
    """Surveille les goals lancés par les récurrences.

    - usageLimited -> resume_goal (debounce, jamais un goal paused) ;
    - complete / unmet / cancelled / paused / budgetLimited -> Telegram une fois ;
    - laisse les goals active tranquilles.
    """
    now = now or datetime.now()
    result = {
        "now": now.isoformat(timespec="seconds"),
        "dryRun": dry_run,
        "checked": 0,
        "resumed": [],
        "notified": [],
        "errors": [],
    }

    try:
        data = store.load()
    except ValueError as exc:
        result["errors"].append(str(exc))
        return result

    runs = [run for run in data.get("goalRuns", []) if isinstance(run, dict)]
    result["checked"] = len(runs)
    if not runs:
        return result

    state = load_goal_state()
    if state.get("error"):
        result["errors"].append(state["error"])
        if log_path is not None:
            _log(log_path, f"watch-goals : {state['error']}")
        return result

    goals = state.get("goals") or {}
    archives = state.get("archives") or {}
    changed = False

    for run in runs:
        session_id = run.get("sessionID")
        if not session_id:
            continue
        before = json.dumps({key: run.get(key) for key in GOAL_TRACK_FIELDS}, sort_keys=True, default=str)
        goal = goals.get(session_id)

        if goal is None:
            if session_id in archives:
                if run.get("status") != "cleared":
                    run["status"] = "cleared"
                    run["notifiedStatus"] = "cleared"
                    changed = True
                continue
            if run.get("notifiedStatus") != "missing":
                run["status"] = "missing"
                run["notifiedStatus"] = "missing"
                changed = True
                if dry_run:
                    result["notified"].append({"sessionID": session_id, "status": "missing", "dryRun": True})
                else:
                    ok, err = send_telegram(_goal_notification(run, "missing", {}))
                    if ok:
                        result["notified"].append({"sessionID": session_id, "status": "missing"})
                    else:
                        result["errors"].append(f"{session_id} : Telegram ({err})")
            continue

        status = str(goal.get("status") or "unknown")
        run["status"] = status
        for key in ("autoTurns", "maxAutoTurns", "tokensUsed", "timeUsedSeconds", "lastStatus",
                    "stopReason", "updatedAt", "completionEvidence", "blocker"):
            run[key] = goal.get(key)

        if status == "active":
            if run.get("notifiedStatus") not in (None, "active"):
                run["notifiedStatus"] = None
        elif status == "usageLimited":
            resumable, reason = _goal_resumable(goal)
            max_resumes = GOAL_RESUME_MAX
            if max_resumes and int(run.get("resumes") or 0) >= max_resumes:
                if run.get("notifiedStatus") != "usageLimited-max":
                    run["notifiedStatus"] = "usageLimited-max"
                    if dry_run:
                        result["notified"].append({"sessionID": session_id, "status": "usageLimited-max", "dryRun": True})
                    else:
                        ok, err = send_telegram(_goal_notification(run, "usageLimited-max", goal))
                        if ok:
                            result["notified"].append({"sessionID": session_id, "status": "usageLimited-max"})
                        else:
                            result["errors"].append(f"{session_id} : Telegram ({err})")
            elif not resumable:
                run["resumeBlockedReason"] = reason
                if run.get("notifiedStatus") != "usageLimited-blocked":
                    run["notifiedStatus"] = "usageLimited-blocked"
                    if dry_run:
                        result["notified"].append({"sessionID": session_id, "status": "usageLimited-blocked", "dryRun": True})
                    else:
                        ok, err = send_telegram(_goal_notification(run, "usageLimited-blocked", goal))
                        if ok:
                            result["notified"].append({"sessionID": session_id, "status": "usageLimited-blocked"})
                        else:
                            result["errors"].append(f"{session_id} : Telegram ({err})")
            else:
                last_updated = run.get("lastResumeUpdatedAt")
                same_state = last_updated is not None and str(last_updated) == str(goal.get("updatedAt"))
                failures = int(run.get("resumeFailures") or 0) + 1 if same_state else 0
                run["resumeFailures"] = failures
                if failures >= 3:
                    if run.get("notifiedStatus") != "usageLimited-stuck":
                        run["notifiedStatus"] = "usageLimited-stuck"
                        if dry_run:
                            result["notified"].append({"sessionID": session_id, "status": "usageLimited-stuck", "dryRun": True})
                        else:
                            ok, err = send_telegram(_goal_notification(run, "usageLimited-stuck", goal))
                            if ok:
                                result["notified"].append({"sessionID": session_id, "status": "usageLimited-stuck"})
                            else:
                                result["errors"].append(f"{session_id} : Telegram ({err})")
                    continue
                can_resume = True
                if run.get("lastResumeAt"):
                    try:
                        elapsed = (now - datetime.fromisoformat(run["lastResumeAt"])).total_seconds()
                        can_resume = elapsed >= GOAL_RESUME_DEBOUNCE
                    except ValueError:
                        can_resume = True
                if can_resume:
                    if dry_run:
                        result["resumed"].append(
                            {"sessionID": session_id, "resumes": int(run.get("resumes") or 0), "dryRun": True}
                        )
                    else:
                        ok, payload = resume_goal(session_id)
                        if ok:
                            run["resumes"] = int(run.get("resumes") or 0) + 1
                            run["lastResumeAt"] = now.isoformat(timespec="seconds")
                            run["lastResumeUpdatedAt"] = goal.get("updatedAt")
                            run["status"] = "resuming"
                            result["resumed"].append(
                                {"sessionID": session_id, "resumes": run["resumes"]}
                            )
                        else:
                            result["errors"].append(
                                f"{session_id} : resume impossible ({payload.get('error', 'inconnu')})"
                            )
        elif status in ("complete", "unmet", "cancelled", "paused", "budgetLimited"):
            if run.get("notifiedStatus") != status:
                run["notifiedStatus"] = status
                if dry_run:
                    result["notified"].append({"sessionID": session_id, "status": status, "dryRun": True})
                else:
                    ok, err = send_telegram(_goal_notification(run, status, goal))
                    if ok:
                        result["notified"].append({"sessionID": session_id, "status": status})
                    else:
                        result["errors"].append(f"{session_id} : Telegram ({err})")
        else:
            if run.get("notifiedStatus") != status:
                run["notifiedStatus"] = status
                if not dry_run:
                    ok, err = send_telegram(_goal_notification(run, status, goal))
                    if ok:
                        result["notified"].append({"sessionID": session_id, "status": status})
                    else:
                        result["errors"].append(f"{session_id} : Telegram ({err})")

        after = json.dumps({key: run.get(key) for key in GOAL_TRACK_FIELDS}, sort_keys=True, default=str)
        if before != after:
            changed = True

    if changed and not dry_run:
        try:
            with store.locked():
                fresh = store.load()
                by_session = {
                    run.get("sessionID"): run
                    for run in fresh.get("goalRuns", [])
                    if isinstance(run, dict) and run.get("sessionID")
                }
                for run in runs:
                    session_id = run.get("sessionID")
                    if not session_id:
                        continue
                    if session_id in by_session:
                        by_session[session_id].update(run)
                    else:
                        by_session[session_id] = run
                fresh["goalRuns"] = prune_goal_runs(list(by_session.values()), now)
                store.save(fresh)
        except (OSError, ValueError) as exc:
            result["errors"].append(f"écriture du store impossible : {exc}")

    if log_path is not None:
        summary = (
            f"watch-goals dry={dry_run} suivis={result['checked']} "
            f"repris={len(result['resumed'])} notifiés={len(result['notified'])} "
            f"erreurs={len(result['errors'])}"
        )
        _log(log_path, summary)
        for item in result["resumed"]:
            _log(log_path, f"  repris: {item.get('sessionID')} (reprises={item.get('resumes')})")
        for item in result["notified"]:
            _log(log_path, f"  notifié: {item.get('sessionID')} statut={item.get('status')}")
        for error in result["errors"]:
            _log(log_path, f"  erreur: {error}")

    return result
