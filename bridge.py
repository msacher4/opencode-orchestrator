#!/usr/bin/env python3
"""Pont Telegram → OpenCode Serve (polling direct via Bot API)

Toutes les sessions viennent de l'API opencode (GET /session).
Le stockage local ne contient que la session active par chat.
"""

import asyncio
import base64
import fcntl
import json
import logging
import os
import random
import subprocess
import sys
import time
from datetime import datetime

import httpx

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("tg-bridge")

OPENCODE_URL = os.getenv("OPENCODE_URL", "http://127.0.0.1:4096")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
STATE_FILE = os.getenv(
    "STATE_FILE",
    "/media/marcs/Linux_Apps/Projets_AI/.opencode/tg-sessions.json",
)

if not TELEGRAM_TOKEN and not any(x in sys.argv for x in ("--api-probe", "--selftest", "--notify-test")):
    log.error("TELEGRAM_TOKEN not set")
    sys.exit(1)

# AUTHORIZED_CHAT_IDS : variable d'environnement (identifiants séparés par des
# virgules), repli sur les clés déjà présentes dans l'état si la variable est
# absente. Un chat non autorisé reçoit un refus explicite, l'événement est
# journalisé, et AUCUN appel ne part vers l'API OpenCode.
def _authorized_chat_ids() -> set[int]:
    raw = os.getenv("AUTHORIZED_CHAT_IDS", "").strip()
    if raw:
        return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}
    # Repli : tous les chat_id présents dans l'état local
    return {int(cid) for cid in state.keys() if cid.isdigit()}


def _is_authorized(chat_id: int) -> bool:
    return chat_id in _authorized_chat_ids()


API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Backoff borné 5 s → 60 s sur erreur réseau (exponentiel + jitter), calculable
# sans dormir réellement pour être testable.
def _backoff_schedule(attempt: int, base: float = 5.0, max_delay: float = 60.0) -> float:
    """Retourne le délai en secondes pour la tentative `attempt` (0-indexé).
    Formule : min(base * 2^attempt, max_delay) + jitter(±25%).
    Ne fait PAS sleep — pour tests et usage réel on appellera asyncio.sleep soi-même.
    """
    delay = min(base * (2 ** attempt), max_delay)
    jitter = delay * 0.25 * (random.random() * 2 - 1)  # ±25%
    return delay + jitter


# Point d'injection de transport pour l'API OpenCode : tous les clients
# httpx vers OpenCode passent transport=TRANSPORT. Par défaut None = client
# standard. Permet aux tests de tourner hors réseau via httpx.MockTransport.
# L'API Telegram utilise TELEGRAM_TRANSPORT (défaut None) pour l'injection
# de tests ; en production, None = client standard.
TRANSPORT = None
TELEGRAM_TRANSPORT = None

# Employé autonome : les messages « #emp … » ne vont JAMAIS dans une session.
EMPLOYEE_SUPERVISOR = os.getenv(
    "EMPLOYEE_SUPERVISOR",
    "/media/marcs/Linux_Apps/Projets_AI/mcp/opencode-employee/supervisor.py",
)
EMPLOYEE_INBOX = os.getenv(
    "EMPLOYEE_INBOX",
    "/media/marcs/Linux_Apps/Projets_AI/Employe/inbox.jsonl",
)

# Ordres du jour : les messages « #push … » ne vont JAMAIS dans une session
# OpenCode ni dans l'inbox employé. Ils pilotent Employe/push/pusher.py.
PUSH_DIR = os.getenv(
    "PUSH_DIR",
    "/media/marcs/Linux_Apps/Projets_AI/Employe/push",
)
PUSHER = os.path.join(PUSH_DIR, "pusher.py")
PUSH_ACKS = os.path.join(PUSH_DIR, "acks.jsonl")


class TelegramError(Exception):
    """Lever une erreur explicite quand Telegram refuse l'envoi d'un bloc."""


def chunk_text(text: str, limit: int = 3500) -> list[str]:
    """Découpe text en blocs <= limit caractères en respectant les frontières :
    d'abord "\\n\\n", puis "\\n", puis un espace, puis coupe brute. Jamais de bloc
    vide ; un texte court (<= limit) revient en liste à un élément ; "" -> []."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        window = text[start : start + limit]
        idx = window.rfind("\n\n")
        if idx > 0:
            end = start + idx
        else:
            idx = window.rfind("\n")
            if idx > 0:
                end = start + idx
            else:
                idx = window.rfind(" ")
                if idx > 0:
                    end = start + idx
                else:
                    end = start + limit
        chunks.append(text[start:end])
        start = end
    return chunks


async def tg_send(chat_id: int, text: str, markdown: bool = True) -> int:
    """Envoi fiable : découpe en blocs <= 3500, tente parse_mode Markdown, replie
    en texte brut si l'API renvoie 400 (en journalisant description), lève
    TelegramError sinon. Toute exception réseau est loggée puis propagée.
    Retourne le message_id du dernier bloc envoyé (pour édition)."""
    last_msg_id = None
    try:
        async with httpx.AsyncClient(transport=TELEGRAM_TRANSPORT) as cl:
            for chunk in chunk_text(text):
                payload = {"chat_id": chat_id, "text": chunk}
                if markdown:
                    payload["parse_mode"] = "Markdown"
                resp = await cl.post(f"{API_BASE}/sendMessage", json=payload)
                log.debug("POST sendMessage -> %s", resp.status_code)
                data = resp.json()
                code = resp.status_code
                description = data.get("description", "")
                if not data.get("ok") and markdown and code == 400:
                    log.warning(
                        "Bloc refusé en Markdown (%s), réessai sans parse_mode",
                        description,
                    )
                    resp = await cl.post(
                        f"{API_BASE}/sendMessage",
                        json={"chat_id": chat_id, "text": chunk},
                    )
                    data = resp.json()
                    code = resp.status_code
                    description = data.get("description", "")
                if not data.get("ok"):
                    raise TelegramError(f"sendMessage {code}: {description}")
                if data.get("result"):
                    last_msg_id = data["result"].get("message_id")
    except httpx.HTTPError as e:
        log.exception("Erreur réseau Telegram: %s", e)
        raise
    return last_msg_id


async def tg_edit(chat_id: int, message_id: int, text: str, markdown: bool = True) -> None:
    """Édite un message Telegram existant. Découpe si > 3500, repli texte brut sur 400."""
    try:
        async with httpx.AsyncClient(transport=TELEGRAM_TRANSPORT) as cl:
            for chunk in chunk_text(text):
                payload = {"chat_id": chat_id, "message_id": message_id, "text": chunk}
                if markdown:
                    payload["parse_mode"] = "Markdown"
                resp = await cl.post(f"{API_BASE}/editMessageText", json=payload)
                log.debug("POST editMessageText -> %s", resp.status_code)
                data = resp.json()
                code = resp.status_code
                description = data.get("description", "")
                if not data.get("ok") and markdown and code == 400:
                    log.warning(
                        "Bloc refusé en Markdown (%s), réessai sans parse_mode",
                        description,
                    )
                    resp = await cl.post(
                        f"{API_BASE}/editMessageText",
                        json={"chat_id": chat_id, "message_id": message_id, "text": chunk},
                    )
                    data = resp.json()
                    code = resp.status_code
                    description = data.get("description", "")
                if not data.get("ok"):
                    raise TelegramError(f"editMessageText {code}: {description}")
    except httpx.HTTPError as e:
        log.exception("Erreur réseau Telegram (edit): %s", e)
        raise


async def send_message(chat_id: int, text: str) -> None:
    """Enveloppe de tg_send en markdown (1er essai Markdown, repli texte brut)."""
    await tg_send(chat_id, text, markdown=True)


async def _send_plain(chat_id: int, text: str) -> None:
    """Enveloppe de tg_send sans parse_mode (sortie pusher avec _ et *)."""
    await tg_send(chat_id, text, markdown=False)


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        log.error("État corrompu (%s), relance à vide", e)
        return {}


def _save_state(state: dict):
    """Écriture atomique : tmp puis os.replace, protégée par un verrou fcntl
    sur <STATE_FILE>.lock ; crée le dossier si absent."""
    directory = os.path.dirname(STATE_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    lock_file = os.path.join(directory, os.path.basename(STATE_FILE) + ".lock")
    with open(lock_file, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            tmp = f"{STATE_FILE}.tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, STATE_FILE)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


state = _load_state()

# MIRROR: intervalle de polling pour la boucle de miroir (défaut 3s, surchargeable par MIRROR_INTERVAL)
MIRROR_INTERVAL = float(os.getenv("MIRROR_INTERVAL", "3.0"))

# Tâches de miroir actives par chat_id -> asyncio.Task
_MIRROR_TASKS: dict[int, asyncio.Task] = {}

_INSTANCES = None


def instances():
    global _INSTANCES
    if _INSTANCES is None:
        _INSTANCES = parse_instances()
    return _INSTANCES


def default_instance():
    return next(iter(instances()))


def _cur(chat_id):
    """Session courante du chat, migrée depuis l'ancien format str."""
    cid = str(chat_id)
    c = state.get(cid, {}).get("current")
    if isinstance(c, str):
        c = {"instance": default_instance(), "sid": c}
        state.setdefault(cid, {})["current"] = c
        _save_state(state)
    return c


async def _set_cur(chat_id, instance, sid):
    cid = str(chat_id)
    state.setdefault(cid, {})["current"] = {"instance": instance, "sid": sid}
    _save_state(state)
    # Redémarrer la tâche de miroir pour ce chat avec la nouvelle session
    await _restart_mirror_task(chat_id)


def _get_mirror_state(cid: str) -> dict:
    """Récupère l'état de miroir pour un chat (last_seen_id, seen_ids set en mémoire)."""
    chat_state = state.setdefault(cid, {})
    mirror = chat_state.setdefault("mirror", {})
    # seen_ids est stocké comme liste dans l'état (JSON), mais utilisé comme set en mémoire
    if "seen_ids" not in mirror:
        mirror["seen_ids"] = []
    return mirror


def _mirror_seen_ids_as_set(mirror: dict) -> set:
    """Convertit mirror['seen_ids'] (liste) en set pour usage en mémoire."""
    return set(mirror.get("seen_ids", []))


def _mirror_save_seen_ids(mirror: dict, seen_ids: set):
    """Sauvegarde le set seen_ids comme liste dans l'état."""
    mirror["seen_ids"] = list(seen_ids)


async def _fetch_last_message_id(inst: dict, sid: str) -> str | None:
    """Récupère l'ID du dernier message de la session (pour initialiser le miroir sans rejouer l'historique)."""
    kind = await detect_kind(inst)
    try:
        if kind == "api":
            data, _ = await _http_get(inst, f"/api/session/{sid}/message", timeout=10.0)
            items = (data or {}).get("data", []) if isinstance(data, dict) else []
        else:
            data, _ = await _http_get(inst, f"/session/{sid}/message", timeout=10.0)
            items = data if isinstance(data, list) else []
        if items:
            last = items[-1]
            if isinstance(last, dict):
                return last.get("id")
    except Exception as e:
        log.debug("Impossible de récupérer le dernier message pour miroir %s/%s: %s", inst.get("name"), sid, e)
    return None


async def _fetch_new_assistant_messages(inst: dict, sid: str, after_id: str | None) -> list[dict]:
    """Récupère les messages d'assistant nouveaux depuis after_id (exclusif).
    Retourne une liste de dicts {id, text} pour les messages assistant non vides."""
    kind = await detect_kind(inst)
    new_messages = []
    try:
        if kind == "api":
            data, _ = await _http_get(inst, f"/api/session/{sid}/message", timeout=10.0)
            items = (data or {}).get("data", []) if isinstance(data, dict) else []
        else:
            data, _ = await _http_get(inst, f"/session/{sid}/message", timeout=10.0)
            items = data if isinstance(data, list) else []

        found_after = after_id is None
        for item in items:
            if not isinstance(item, dict):
                continue
            msg_id = item.get("id")
            if not found_after:
                if msg_id == after_id:
                    found_after = True
                continue
            # Ne traiter que les messages assistant
            if item.get("type") != "assistant":
                continue
            # Extraire le texte depuis content[].text
            contents = item.get("content", [])
            if not isinstance(contents, list):
                continue
            text_parts = []
            for c in contents:
                if isinstance(c, dict) and c.get("type") == "text":
                    text = c.get("text", "").strip()
                    if text:
                        text_parts.append(text)
            if text_parts:
                new_messages.append({"id": msg_id, "text": "\n".join(text_parts)})
    except Exception as e:
        log.debug("Erreur récupération messages miroir %s/%s: %s", inst.get("name"), sid, e)
    return new_messages


async def _send_mirror_message(chat_id: int, inst: dict, sid: str, msg: dict):
    """Envoie un message miroir sur Telegram avec l'en-tête standard."""
    try:
        header = await _status_header(chat_id)
        text = f"{header}\n{msg['text']}"
        await tg_send(chat_id, text, markdown=True)
        log.info("Miroir: message %s envoyé au chat %s", msg.get("id"), chat_id)
    except Exception as e:
        log.error("Erreur envoi miroir chat %s: %s", chat_id, e)


async def _fetch_latest_assistant_message_id(inst: dict, sid: str) -> str | None:
    """Récupère l'ID du dernier message assistant de la session."""
    kind = await detect_kind(inst)
    try:
        if kind == "api":
            data, _ = await _http_get(inst, f"/api/session/{sid}/message", timeout=10.0)
            items = (data or {}).get("data", []) if isinstance(data, dict) else []
        else:
            data, _ = await _http_get(inst, f"/session/{sid}/message", timeout=10.0)
            items = data if isinstance(data, list) else []
        # Parcourir en sens inverse pour trouver le dernier assistant
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "assistant":
                return item.get("id")
    except Exception as e:
        log.debug("Impossible de récupérer le dernier message assistant %s/%s: %s", inst.get("name"), sid, e)
    return None


async def _mark_latest_assistant_seen(chat_id: int, inst: dict, sid: str):
    """Marque le dernier message assistant comme vu dans l'état miroir."""
    msg_id = await _fetch_latest_assistant_message_id(inst, sid)
    if not msg_id:
        return
    cid = str(chat_id)
    mirror_state = _get_mirror_state(cid)
    seen_ids = _mirror_seen_ids_as_set(mirror_state)
    if msg_id not in seen_ids:
        seen_ids.add(msg_id)
        mirror_state["last_seen_id"] = msg_id
        _mirror_save_seen_ids(mirror_state, seen_ids)
        _save_state(state)
        log.debug("Miroir: message %s marqué comme vu (livré en direct) pour chat %s", msg_id, cid)


async def _mirror_loop(chat_id: int):
    """Boucle de miroir pour un chat : poll périodique des nouveaux messages assistant."""
    cid = str(chat_id)
    log.info("Miroir démarré pour chat %s (intervalle %.1fs)", cid, MIRROR_INTERVAL)

    while True:
        try:
            await asyncio.sleep(MIRROR_INTERVAL)

            # Vérifier que le chat a encore une session courante
            cur = _cur(chat_id)
            if not cur or not cur.get("sid"):
                log.debug("Miroir: pas de session courante pour chat %s, attente...", cid)
                continue

            inst_name = cur["instance"]
            sid = cur["sid"]
            inst = instances().get(inst_name)
            if not inst:
                log.warning("Miroir: instance %s introuvable pour chat %s", inst_name, cid)
                continue

            mirror_state = _get_mirror_state(cid)
            last_seen = mirror_state.get("last_seen_id")
            seen_ids = _mirror_seen_ids_as_set(mirror_state)

            # Récupérer les nouveaux messages assistant
            new_messages = await _fetch_new_assistant_messages(inst, sid, last_seen)

            for msg in new_messages:
                msg_id = msg.get("id")
                if not msg_id or msg_id in seen_ids:
                    continue
                # Envoyer le message
                await _send_mirror_message(chat_id, inst, sid, msg)
                # Marquer comme vu
                seen_ids.add(msg_id)
                mirror_state["last_seen_id"] = msg_id
                _mirror_save_seen_ids(mirror_state, seen_ids)
                _save_state(state)

        except asyncio.CancelledError:
            log.info("Miroir arrêté pour chat %s", cid)
            break
        except Exception as e:
            log.exception("Erreur dans la boucle miroir chat %s: %s", cid, e)
            # On continue la boucle malgré l'erreur


async def _restart_mirror_task(chat_id: int):
    """Arrête l'ancienne tâche de miroir (si existante) et en démarre une nouvelle."""
    # Annuler l'ancienne tâche
    old_task = _MIRROR_TASKS.pop(chat_id, None)
    if old_task and not old_task.done():
        old_task.cancel()

    # Vérifier qu'il y a une session courante
    cur = _cur(chat_id)
    if not cur or not cur.get("sid"):
        return

    # Initialiser l'état de miroir : last_seen_id = dernier message existant
    cid = str(chat_id)
    inst_name = cur["instance"]
    sid = cur["sid"]
    inst = instances().get(inst_name)
    if not inst:
        return

    mirror_state = _get_mirror_state(cid)
    last_id = await _fetch_last_message_id(inst, sid)
    mirror_state["last_seen_id"] = last_id
    _mirror_save_seen_ids(mirror_state, set())
    _save_state(state)

    # Démarrer la nouvelle tâche
    task = asyncio.create_task(_mirror_loop(chat_id))
    _MIRROR_TASKS[chat_id] = task
    log.info("Miroir: tâche redémarrée pour chat %s (session %s, last_seen=%s)", cid, sid, last_id)


def _stop_mirror_task(chat_id: int):
    """Arrête la tâche de miroir pour un chat (ex: lors du changement de session)."""
    task = _MIRROR_TASKS.pop(chat_id, None)
    if task and not task.done():
        task.cancel()
        log.info("Miroir: tâche arrêtée pour chat %s", chat_id)


async def api_post(path, body=None):
    async with httpx.AsyncClient(base_url=OPENCODE_URL, timeout=httpx.Timeout(180.0)) as cl:
        r = await cl.post(path, json=body or {})
        log.debug("POST %s -> %s", path, r.status_code)
        r.raise_for_status()
        if not r.content:
            return None
        return r.json()


async def api_patch(path, body):
    async with httpx.AsyncClient(base_url=OPENCODE_URL, timeout=httpx.Timeout(10.0)) as cl:
        r = await cl.patch(path, json=body)
        log.debug("PATCH %s -> %s", path, r.status_code)
        r.raise_for_status()
        return r.json()


async def list_sessions():
    return await api_get("/session")


async def create_session() -> dict:
    return await api_post("/session", {})


async def create_session_on_instance(inst, body=None):
    """Créer une session sur une instance donnée (v1 ou v2)."""
    kind = await detect_kind(inst)
    if kind == "api":
        data, _ = await _http_post(inst, "/api/session", body or {}, timeout=180.0)
        return (data or {}).get("data") if isinstance(data, dict) else None
    data, _ = await _http_post(inst, "/session", body or {}, timeout=180.0)
    return data


async def send_to_session(sid: str, text: str) -> str:
    result = await api_post(f"/session/{sid}/message", {"parts": [{"type": "text", "text": text}]})
    parts = result.get("parts", [])
    return "\n".join(p["text"] for p in parts if p.get("type") == "text").strip() or "(pas de réponse textuelle)"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


async def get_session_context_tokens(inst: dict, sid: str) -> int:
    """Lit les tokens de contexte depuis l'objet session (pas depuis les messages).
    v1 (legacy) : session.tokens.input + session.tokens.output
    v2 (api)    : session.data.tokens.input + session.data.tokens.output
    Repli tolérant à 0 si absent ou si l'appel échoue, sans lever, en journalisant.
    """
    try:
        session = await cli_get_session(inst, sid)
        if not session:
            return 0
        kind = await detect_kind(inst)
        if kind == "api":
            # v2 : session est déjà l'objet data (cli_get_session dépaquette l'enveloppe)
            t = session.get("tokens") or {}
        else:
            # v1 : session est l'objet brut
            t = session.get("tokens") or {}
        return (t.get("input", 0) or 0) + (t.get("output", 0) or 0)
    except Exception:
        log.exception("Contexte tokens indisponible pour %s sur instance %s", sid, inst.get("name"))
    return 0


async def get_session_info(sid: str) -> dict:
    return await api_get(f"/session/{sid}")


# ---------------------------------------------------------------------------
# Couche client multi-instances (t3a) — adaptateurs legacy (v1) et api (v2)
# derrière une interface unique. Ne touche pas aux commandes Telegram. Tous les
# appels HTTP passent par TRANSPORT (point d'injection des tests MockTransport).
# ---------------------------------------------------------------------------


def parse_instances(raw=None):
    """lit OPENCODE_INSTANCES="nom=url,nom=url" (défaut v1/v2) et
    OPENCODE_V2_AUTH="user:pass" (Basic pour les instances à API sous /api)."""
    if raw is None:
        raw = os.getenv("OPENCODE_INSTANCES")
    if not raw:
        raw = "v1=http://127.0.0.1:4096,v2=http://127.0.0.1:4097"
    auth = os.getenv("OPENCODE_V2_AUTH", "")
    instances = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, _, url = pair.partition("=")
        name = name.strip()
        url = url.strip()
        if not name or not url:
            continue
        instances[name] = {"name": name, "url": url, "auth": auth, "kind": None}
    return instances


def _auth_headers(inst):
    auth = inst.get("auth") or ""
    if not auth:
        return {}
    user, _, passw = auth.partition(":")
    token = base64.b64encode(f"{user}:{passw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def _http_get(inst, path, timeout=10.0):
    headers = _auth_headers(inst)
    async with httpx.AsyncClient(base_url=inst["url"], transport=TRANSPORT, timeout=httpx.Timeout(timeout)) as cl:
        r = await cl.get(path, headers=headers)
        log.debug("GET %s -> %s", path, r.status_code)
        if r.status_code != 200:
            return None, r.status_code
        return r.json(), r.status_code


async def _http_post(inst, path, body=None, timeout=10.0):
    headers = _auth_headers(inst)
    async with httpx.AsyncClient(base_url=inst["url"], transport=TRANSPORT, timeout=httpx.Timeout(timeout)) as cl:
        r = await cl.post(path, json=body or {}, headers=headers)
        log.debug("POST %s -> %s", path, r.status_code)
        if r.status_code != 200:
            # Capturer le corps pour les erreurs (utile pour messages lisibles)
            try:
                error_data = r.json()
            except Exception:
                error_data = {"raw": r.text}
            return error_data, r.status_code
        if not r.content:
            return None, r.status_code
        return r.json(), r.status_code


async def _http_patch(inst, path, body, timeout=10.0):
    headers = _auth_headers(inst)
    async with httpx.AsyncClient(base_url=inst["url"], transport=TRANSPORT, timeout=httpx.Timeout(timeout)) as cl:
        r = await cl.patch(path, json=body, headers=headers)
        log.debug("PATCH %s -> %s", path, r.status_code)
        if r.status_code != 200:
            return None, r.status_code
        if not r.content:
            return None, r.status_code
        return r.json(), r.status_code


_KIND_CACHE = {}


async def detect_kind(inst):
    """Détection du kind si absent : GET /api/session 200 => api, sinon
    GET /session JSON => legacy. Résultat mémorisé (seulement api/legacy)."""
    name = inst["name"]
    if name in _KIND_CACHE:
        return _KIND_CACHE[name]
    kind = "unreachable"
    data, code = await _http_get(inst, "/api/session", timeout=10.0)
    if code == 200 and isinstance(data, dict):
        kind = "api"
    else:
        data, code = await _http_get(inst, "/session", timeout=10.0)
        if code == 200 and isinstance(data, (list, dict)) and data:
            kind = "legacy"
    if kind in ("api", "legacy"):
        _KIND_CACHE[name] = kind
    return kind


async def cli_list_sessions(instances):
    """Liste toutes les instances. Une instance injoignable ne bloque pas :
    marquée indisponible, le reste continue. Retourne {name: {kind, unreachable,
    sessions, code}}."""
    results = {}
    for name, inst in instances.items():
        try:
            kind = await detect_kind(inst)
            if kind == "unreachable":
                results[name] = {"kind": kind, "unreachable": True, "sessions": [], "code": -1}
                continue
            if kind == "api":
                data, code = await _http_get(inst, "/api/session", timeout=10.0)
                raw = (data or {}).get("data", []) if isinstance(data, dict) else []
            else:
                data, code = await _http_get(inst, "/session", timeout=10.0)
                raw = data if isinstance(data, list) else []
            sessions = [cli_normalize_session(s, name, kind) for s in raw if isinstance(s, dict)]
            results[name] = {"kind": kind, "unreachable": False, "sessions": sessions, "code": code}
        except Exception as e:
            log.exception("Listing instance %s échoué: %s", name, e)
            results[name] = {"kind": None, "unreachable": True, "sessions": [], "code": -1, "error": str(e)}
    return results


async def cli_create_session(inst):
    kind = await detect_kind(inst)
    if kind == "api":
        data, _ = await _http_post(inst, "/api/session", {}, timeout=180.0)
        return (data or {}).get("data") if isinstance(data, dict) else None
    data, _ = await _http_post(inst, "/session", {}, timeout=180.0)
    return data


async def cli_get_session(inst, sid):
    kind = await detect_kind(inst)
    if kind == "api":
        data, _ = await _http_get(inst, f"/api/session/{sid}", timeout=10.0)
        return (data or {}).get("data") if isinstance(data, dict) else None
    data, _ = await _http_get(inst, f"/session/{sid}", timeout=10.0)
    return data


async def cli_set_title(inst, sid, title):
    kind = await detect_kind(inst)
    if kind == "api":
        return await _http_patch(inst, f"/api/session/{sid}", {"title": title}, timeout=10.0)
    return await _http_patch(inst, f"/session/{sid}", {"title": title}, timeout=10.0)


def _extract_time_fields(raw):
    """Extrait created/updated depuis raw.time (v1) ou raw.data.time (v2).
    Retourne (created_ms, updated_ms) en int ou None si absent.
    Les timestamps sont en millisecondes epoch (OpenCode)."""
    time_obj = raw.get("time")
    if isinstance(time_obj, dict):
        created = time_obj.get("created")
        updated = time_obj.get("updated")
        return (created if isinstance(created, (int, float)) else None,
                updated if isinstance(updated, (int, float)) else None)
    return (None, None)


def cli_normalize_session(raw, instance, kind):
    """normalize_session(raw, instance, kind) -> {instance, sid, title, agent,
    model, tokens, created_ms, updated_ms} quel que soit le serveur."""
    t = raw.get("tokens") or {}
    cache = t.get("cache") if isinstance(t, dict) else {}
    tokens = t.get("input", 0) + t.get("output", 0)
    model = raw.get("model")
    if isinstance(model, dict):
        model = model.get("id")
    title = raw.get("title") or (raw.get("id") or "")[:12]
    created_ms, updated_ms = _extract_time_fields(raw)
    return {
        "instance": instance,
        "sid": raw.get("id"),
        "title": title,
        "agent": raw.get("agent") or "build",
        "model": model,
        "tokens": tokens,
        "created_ms": created_ms,
        "updated_ms": updated_ms,
    }


async def cli_prompt(inst, sid, text, agent=None):
    """prompt(inst, sid, text, agent). legacy : réponse directe dans parts[].text ;
    api : POST /prompt puis polling GET /message jusqu'à un message assistant
    (timeout 180 s, intervalle 1 s)."""
    kind = await detect_kind(inst)
    if kind == "api":
        payload = {"text": text}
        if agent:
            # L'API v2 attend "name" (AgentAttachment), pas "id" (cf. SDK @opencode/protocol)
            payload["agents"] = [{"name": agent}]
        data, code = await _http_post(inst, f"/api/session/{sid}/prompt", payload, timeout=180.0)
        if code != 200:
            # Erreur lisible avec le corps de la réponse (sans exposer de secret)
            body = json.dumps(data, ensure_ascii=False) if data else "(corps vide)"
            raise httpx.HTTPStatusError(
                f"POST /api/session/{sid}/prompt → {code}: {body}",
                request=None,
                response=httpx.Response(code, json=data),
            )
        return await _poll_api(inst, sid)
    result, _ = await _http_post(inst, f"/session/{sid}/message", {"parts": [{"type": "text", "text": text}]}, timeout=180.0)
    parts = (result or {}).get("parts", []) if isinstance(result, dict) else []
    out = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    return out or "(pas de réponse textuelle)"


async def _poll_api(inst, sid, timeout=180.0, interval=1.0):
    deadline = time.monotonic() + timeout
    while True:
        data, _ = await _http_get(inst, f"/api/session/{sid}/message", timeout=10.0)
        items = (data or {}).get("data", []) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("type") == "assistant":
                contents = item.get("content", [])
                if isinstance(contents, list):
                    for c in contents:
                        if isinstance(c, dict) and c.get("type") == "text":
                            return c.get("text", "").strip()
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timeout polling v2 après {timeout}s : aucune réponse assistant pour {sid}")
        await asyncio.sleep(interval)


async def api_probe(instances):
    """--api-probe : pour chaque instance, kind, nb sessions, 1re session (sid +
    titre), code observé. Retourne True si toutes répondent."""
    results = await cli_list_sessions(instances)
    all_ok = True
    lines = []
    for name, res in results.items():
        if res.get("unreachable"):
            all_ok = False
            lines.append(f"[{name}] kind=? indisponible (code {res.get('code')})")
            continue
        sessions = res.get("sessions", [])
        lines.append(f"[{name}] kind={res.get('kind')} sessions={len(sessions)} code={res.get('code')}")
        if sessions:
            first = sessions[0]
            lines.append(f"    1re: sid={first['sid']} titre={first['title']!r}")
        else:
            lines.append("    1re: (aucune)")
    print("\n".join(lines))
    return all_ok


# ...


async def ensure_session_titled(cid: str, sid: str, first_text: str):
    titled = state.get(cid, {}).get("titled", [])
    if sid in titled:
        return
    cur = _cur(cid)
    inst_name = cur.get("instance", default_instance())
    inst = instances()[inst_name]
    try:
        info = await get_session_info(sid)
    except Exception:
        return
    current_title = info.get("title", "")
    if current_title and not current_title.startswith("New session"):
        state.setdefault(cid, {}).setdefault("titled", []).append(sid)
        _save_state(state)
        return
    title = first_text.strip()[:60]
    if not title:
        return
    try:
        await cli_set_title(inst, sid, title)
        state.setdefault(cid, {}).setdefault("titled", []).append(sid)
        _save_state(state)
        log.info("Session %s titrée: %s", sid, title)
    except Exception as e:
        log.warning("Impossible de titrer session %s: %s", sid, e)


def _resolve_instances(arg: str):
    """Retourne le nom d'instance (str) si arg est connu, None si vide/inconnu."""
    if not arg:
        return None
    if arg in instances():
        return arg
    return None


async def handle_status(chat_id: int):
    """/status : affiche instance, session, agent et le contexte en tokens."""
    cid = str(chat_id)
    cur = _cur(chat_id)
    inst_name = cur.get("instance", default_instance())
    inst = instances()[inst_name]
    sid = cur.get("sid", "")
    agent = cur.get("agent", "build")

    # Tokens
    tokens = "?"
    try:
        if sid:
            n = await get_session_context_tokens(inst, sid)
            tokens = _fmt_tokens(n) if n > 0 else "0"
    except Exception:
        pass

    title = ""
    try:
        info = await get_session_info(sid) if sid else {}
        title = info.get("title", "") or sid[:12] if sid else ""
    except Exception:
        pass

    lines = [
        f"[{inst_name}] {title} · {agent} · ctx {tokens}",
        f"Session : {sid or '(aucune)'}",
    ]
    await send_message(chat_id, "\n".join(lines))


def _fmt_ts(ms):
    """Formate un timestamp ms epoch en 'JJ/MM HH:MM' (ex: 28/10 23:58).
    Retourne '' si ms est None."""
    if ms is None:
        return ""
    try:
        dt = datetime.fromtimestamp(ms / 1000.0)
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return ""


def _sort_key_session(s):
    """Clé de tri pour sessions : updated desc, puis created desc, None à la fin.
    Retourne un tuple comparable : (has_date, updated_ms, created_ms) où has_date
    force les sessions sans date à la fin (False < True)."""
    updated = s.get("updated_ms")
    created = s.get("created_ms")
    has_date = updated is not None or created is not None
    # Pour tri décroissant : on inverse le signe des timestamps
    # None -> -inf pour les mettre à la fin (mais has_date gère déjà)
    return (has_date, updated if updated is not None else -1, created if created is not None else -1)


async def handle_sessions_list(chat_id: int, arg: str):
    """/sessions [v1|v2|suite] : liste multi-instances paginée (10 par page).

    - arg = nom d'une instance connue -> liste seulement celle-ci ; sinon toutes.
    - instance injoignable -> ligne « ⚠️ <nom> injoignable » affichée en-tête,
      jamais dans page_rows (évite sélection par /select).
    - la page affichée est mémorisée dans state[cid]["page_rows"] : /select
      pioche dedans, sans recalcul.
    - ◀️ marque la session courante.
    - Sessions triées par récence décroissante (updated, sinon created).
    - Horodatage court affiché (ex: 28/10 23:58).
    """
    cid = str(chat_id)
    results = await cli_list_sessions(instances())

    wanted = _resolve_instances(arg)

    warning_lines = []
    rows = []
    for name, res in results.items():
        if wanted is not None and name != wanted:
            continue
        if res.get("unreachable"):
            warning_lines.append(f"⚠️ {name} injoignable")
            continue
        for s in res.get("sessions", []):
            rows.append(
                {
                    "instance": name,
                    "sid": s["sid"],
                    "title": s["title"],
                    "agent": s["agent"],
                    "created_ms": s.get("created_ms"),
                    "updated_ms": s.get("updated_ms"),
                }
            )

    # Trier par récence décroissante : updated desc, puis created desc, None à la fin
    rows.sort(key=_sort_key_session, reverse=True)

    current = _cur(chat_id)

    if arg == "suite":
        page = state.get(cid, {}).get("page", 0) + 1
    else:
        page = 0

    page_rows = rows[page * 10 : (page + 1) * 10]
    state.setdefault(cid, {})
    state[cid]["page"] = page
    state[cid]["page_rows"] = page_rows
    _save_state(state)

    lines = []
    # Afficher les avertissements en-tête (hors page_rows)
    for wl in warning_lines:
        lines.append(wl)
    # Lignes de sessions paginées
    for row in page_rows:
        marker = ""
        if (
            current
            and current.get("instance") == row["instance"]
            and current.get("sid") == row["sid"]
        ):
            marker = " ◀️"
        agent_icon = "📋" if row["agent"] == "plan" else "🔧"
        ts = _fmt_ts(row.get("updated_ms") or row.get("created_ms"))
        ts_part = f" [{ts}]" if ts else ""
        lines.append(f"{row['instance']}: {row['title']}{ts_part}{marker}  {agent_icon}`{row['agent']}`")

    total = len(rows)
    pages = (total + 9) // 10 if total else 1
    if arg == "suite":
        lines.append("")
        lines.append("Utilise `/sessions` pour revenir à la première page.")
    else:
        lines.append(f"\nPage {page + 1} sur {pages}")
        lines.append("Utilise `/sessions suite` pour la page suivante.")
    lines.append("")
    lines.append("Utilise `/select <n>` pour choisir une session active.")

    await send_message(chat_id, "\n".join(lines))


async def handle_select(chat_id: int, arg: str):
    """/select <n> : borné à la page affichée (state[cid]["page_rows"]).

    - n hors page -> message clair, sans devinette.
    - sélection via _set_cur (instance + sid), puis confirmation.
    """
    cid = str(chat_id)

    if not arg:
        await send_message(chat_id, "Utilise `/select <n>` avec le numéro de la session.")
        return

    try:
        n = int(arg)
    except ValueError:
        await send_message(chat_id, "Le numéro doit être un entier.")
        return

    page_rows = state.get(cid, {}).get("page_rows")
    if not page_rows:
        await send_message(chat_id, "Aucune liste de sessions en mémoire. Envoie `/sessions` d'abord.")
        return

    idx = n - 1
    if idx < 0 or idx >= len(page_rows):
        await send_message(chat_id, f"Numéro hors page. Choisis entre 1 et {len(page_rows)}.")
        return

    row = page_rows[idx]
    if "warning" in row:
        await send_message(chat_id, row["warning"])
        return

    await _set_cur(chat_id, row["instance"], row["sid"])

    title = row.get("title", row["sid"][:12])
    agent = row.get("agent", "build")
    agent_icon = "📋" if agent == "plan" else "🔧"
    await send_message(chat_id, f"✅ Session active : `{title}`  {agent_icon}`{agent}`  [{row['instance']}]")


async def handle_newsession(chat_id: int, arg: str = ""):
    """/**newsession* [v1|v2] : nouvelle session sur une instance donnée.
    Sans argument -> instance de la session courante, sinon la première configurée.
    Instance inconnue -> message clair avec la liste des disponibles."""
    cid = str(chat_id)
    inst_name = _resolve_instances(arg)
    if arg and inst_name is None:
        available = ", ".join(instances().keys())
        await send_message(chat_id, f"❌ Instance inconnue : {arg} — disponibles : {available}")
        return
    if inst_name is None:
        inst_name = default_instance()
    inst = instances()[inst_name]
    try:
        s = await create_session_on_instance(inst, {})
        sid = s["id"]
        title = s.get("title", sid[:12])
        await _set_cur(chat_id, inst_name, sid)
        log.info("Nouvelle session %s pour chat %s sur instance %s", sid, cid, inst_name)
        await send_message(chat_id, f"✅ Nouvelle session créée : `{title}`  (contexte 0 tokens)")
    except Exception as e:
        log.error("Erreur création session: %s", e)
        await send_message(chat_id, f"❌ Erreur : {e}")


def _run_supervisor(*args: str) -> str:
    """Lance le superviseur de l'employé (pause/resume/status/digest)."""
    try:
        proc = subprocess.run(
            ["python3", EMPLOYEE_SUPERVISOR, *args],
            capture_output=True,
            text=True,
            timeout=25,
        )
        return (proc.stdout or proc.stderr or f"code {proc.returncode}").strip() or "fait."
    except Exception as e:  # noqa: BLE001
        return f"❌ Erreur superviseur : {e}"


def _run_pusher(*args: str) -> str:
    """Lance pusher.py (ordres du jour) et renvoie sa sortie."""
    try:
        proc = subprocess.run(
            ["python3", PUSHER, *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            out = (out + ("\n" if out and err else "") + err).strip() or f"code {proc.returncode}"
        return out or "fait."
    except Exception as e:  # noqa: BLE001
        return f"❌ Erreur pusher : {e}"


async def handle_push(chat_id: int, body: str):
    """Canal #push : les ordres du jour. Jamais transmis à une session OpenCode."""
    body = body.strip()
    low = body.lower()
    action = low.split()[0] if low else "status"

    if action in ("ok", "done", "fait", "oui"):
        cmd = "ok"
    elif action in ("skip", "next", "passe", "passer"):
        cmd = "skip"
    elif action in ("pause", "stop", "arret", "arrêt"):
        cmd = "pause"
    elif action in ("status", "etat", "état", "state") or not body:
        cmd = "status"
    else:
        await _send_plain(
            chat_id,
            "📤 Commandes : #push ok · #push skip · #push pause · #push status",
        )
        return

    entry = {
        "from": chat_id,
        "text": body,
        "cmd": cmd,
        "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        with open(PUSH_ACKS, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("acks.jsonl indisponible : %s", e)

    out = await asyncio.to_thread(_run_pusher, "--ack", cmd)
    await _send_plain(chat_id, f"📤 {out}")
    log.info("Push ack %s (%d chars de sortie)", cmd, len(out))


async def handle_emp(chat_id: int, body: str):
    """Canal #emp : contrôle instantané de l'employé + dépôt dans son inbox."""
    body = body.strip()
    low = body.lower()

    if low in ("pause", "stop"):
        out = await asyncio.to_thread(_run_supervisor, "--pause", "demandé via Telegram")
        await send_message(chat_id, f"👤⏸ {out}")
    elif low in ("resume", "go", "start", "reprends"):
        out = await asyncio.to_thread(_run_supervisor, "--resume")
        await send_message(chat_id, f"👤▶️ {out}")
    elif low in ("status", "etat", "état", "state"):
        out = await asyncio.to_thread(_run_supervisor, "--status")
        await send_message(chat_id, f"👤\n{out}")
    elif low == "digest":
        out = await asyncio.to_thread(_run_supervisor, "--digest")
        await send_message(chat_id, "👤 Digest envoyé.")
    elif low in ("liste", "list", "approvals", "a valider", "à valider"):
        out = await asyncio.to_thread(_run_supervisor, "--approvals")
        await send_message(chat_id, out)
    elif low.startswith("ok "):
        parts = body.split(None, 1)
        approval_id = parts[1].strip().split()[0] if len(parts) > 1 and parts[1].strip() else ""
        if not approval_id:
            await send_message(chat_id, "👤 Usage : `#emp ok <id>`")
        else:
            out = await asyncio.to_thread(_run_supervisor, "--approve", approval_id, "ok")
            await send_message(chat_id, f"👤 {out}")
    elif low.startswith("ko "):
        parts = body.split(None, 2)
        approval_id = parts[1].strip() if len(parts) > 1 else ""
        reason = parts[2].strip() if len(parts) > 2 else ""
        if not approval_id:
            await send_message(chat_id, '👤 Usage : `#emp ko <id> "<raison>"`')
        else:
            out = await asyncio.to_thread(
                _run_supervisor, "--approve", approval_id, "ko", reason or "refusé via Telegram"
            )
            await send_message(chat_id, f"👤 {out}")
    elif not body:
        await send_message(
            chat_id,
            "👤 Commandes : `#emp pause` · `#emp resume` · `#emp status` · `#emp digest`\n"
            "Tout autre texte est transmis au planner dès que le shift en cours se termine (≤ ~20 min).",
        )
    else:
        entry = {"from": chat_id, "text": body, "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            with open(EMPLOYEE_INBOX, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            now_status = await asyncio.to_thread(_run_supervisor, "--now")
            if "entre deux shifts" in now_status:
                # Aucun shift en cours : on traite la consigne tout de suite
                # au lieu d'attendre le prochain cron (≤ 3 min).
                await asyncio.to_thread(_run_supervisor, "--tick")
                await send_message(
                    chat_id,
                    f"👤 {now_status}\nConsigne prise en compte : un passage démarre maintenant.",
                )
            else:
                await send_message(chat_id, f"👤 {now_status}\nTa consigne est notée — le planner la traite dès que le shift en cours se termine.")
        except OSError as e:
            await send_message(chat_id, f"❌ Inbox employé indisponible : {e}")


async def _status_header(chat_id: int) -> str:
    """Génère la chaîne d'en-tête [instance] titre · agent · ctx N tokens."""
    cid = str(chat_id)
    cur = _cur(chat_id)
    inst_name = cur.get("instance", default_instance())
    inst = instances()[inst_name]
    sid = cur.get("sid", "")
    agent = cur.get("agent", "build")

    tokens = "?"
    try:
        if sid:
            n = await get_session_context_tokens(inst, sid)
            tokens = _fmt_tokens(n) if n > 0 else "0"
    except Exception:
        pass

    title = ""
    try:
        if sid:
            info = await get_session_info(sid)
            title = info.get("title", "") or sid[:12]
    except Exception:
        pass

    return f"[{inst_name}] {title} · {agent} · ctx {tokens}"


async def handle_message(chat_id: int, text: str):
    cid = str(chat_id)

    # B2 — contrôle d'accès : AUTHORIZED_CHAT_IDS lu dans l'environnement,
    # repli sur les clés de l'état. Chat non autorisé = refus explicite, loggé,
    # AUCUN appel vers l'API OpenCode.
    if not _is_authorized(chat_id):
        log.warning("Chat non autorisé: %s", chat_id)
        await send_message(chat_id, "⛔ Chat non autorisé.")
        return

    cur = _cur(cid)  # {"instance": inst_name, "sid": sid}
    inst_name = cur["instance"]
    sid = cur["sid"]
    agent = state[cid].get("agent", "build")
    inst = instances()[inst_name]

    if not sid:
        try:
            s = await create_session()
            sid = s["id"]
            await _set_cur(chat_id, inst_name, sid)
            log.info("Auto-session %s pour chat %s", sid, cid)
        except Exception as e:
            log.error("Erreur création auto-session: %s", e)
            await send_message(chat_id, f"❌ Erreur création session : {e}")
            return

    # B8 — accusé de réception : envoyer « ⏳ » avant un prompt long,
    # puis ÉDITER ce message avec la réponse.
    ack_msg_id = await tg_send(chat_id, "⏳", markdown=False)

    # B13 — borner la récursion sur 404 : une seule re-création, jamais de
    # rappel récursif sans limite. On utilise un drapeau local.
    recreated = False

    while True:
        try:
            response = await cli_prompt(inst, sid, text, agent)
            # Titrer la session après le 1er message si pas déjà fait
            await ensure_session_titled(cid, sid, text)
            header = await _status_header(chat_id)
            await tg_edit(chat_id, ack_msg_id, f"{header}\n{response}")
            log.info("Réponse envoyée (%d chars)", len(response))
            # Marquer le message assistant livré comme vu pour éviter le doublon miroir
            await _mark_latest_assistant_seen(chat_id, inst, sid)
            break
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # Erreurs 4xx/5xx : message lisible pour l'utilisateur, journalisé
            # côté serveur, sans exposer de secret. On inclut le corps tronqué.
            def _err_body(resp):
                try:
                    body = resp.json()
                    text = json.dumps(body, ensure_ascii=False)
                except Exception:
                    text = resp.text or ""
                return text[:300]

            body_snippet = _err_body(e.response)
            if status == 404 and not recreated:
                log.warning("Session %s introuvable (404), création d'une nouvelle (tentative unique)", sid)
                recreated = True
                # Nouvelle session, on réessaie une seule fois
                try:
                    s = await create_session()
                    sid = s["id"]
                    await _set_cur(chat_id, inst_name, sid)
                    log.info("Nouvelle session %s pour chat %s après 404", sid, cid)
                    continue  # réessaie la boucle while
                except Exception as e2:
                    log.error("Erreur création session après 404: %s", e2)
                    await tg_edit(chat_id, ack_msg_id, f"❌ Erreur création session : {e2}")
                    break
            elif 400 <= status < 500:
                log.error("Erreur client HTTP %s: %s | body=%s", status, e, body_snippet)
                await tg_edit(chat_id, ack_msg_id, f"❌ Erreur client ({status}) : {body_snippet}")
                break
            elif 500 <= status < 600:
                log.error("Erreur serveur HTTP %s: %s | body=%s", status, e, body_snippet)
                await tg_edit(chat_id, ack_msg_id, f"❌ Erreur serveur ({status}) : {body_snippet}")
                break
            else:
                log.error("Erreur HTTP inattendue %s: %s | body=%s", status, e, body_snippet)
                await tg_edit(chat_id, ack_msg_id, f"❌ Erreur HTTP ({status}) : {body_snippet}")
                break
        except httpx.TimeoutException:
            log.error("Timeout lors de l'appel OpenCode")
            await tg_edit(chat_id, ack_msg_id, "❌ Timeout : OpenCode ne répond pas")
            break
        except httpx.NetworkError as e:
            log.error("Erreur réseau OpenCode: %s", e)
            await tg_edit(chat_id, ack_msg_id, "❌ Erreur réseau : impossible de joindre OpenCode")
            break
        except Exception as e:
            log.exception("Erreur inattendue dans handle_message: %s", e)
            await tg_edit(chat_id, ack_msg_id, f"❌ Erreur interne")
            break


async def main():
    offset = 0
    log.info("Pont Telegram → OpenCode démarré (polling direct)")

    # B6 — backoff borné 5 s → 60 s sur erreur réseau (pas de boucle serrée,
    # pas d'attente infinie), calculable sans dormir réellement pour être testable.
    poll_attempt = 0

    async with httpx.AsyncClient() as cl:
        while True:
            try:
                resp = await cl.post(
                    f"{API_BASE}/getUpdates",
                    json={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                )
                data = resp.json()

                if not data.get("ok"):
                    log.warning("API error: %s", data)
                    delay = _backoff_schedule(poll_attempt)
                    log.info("Backoff polling: %.1fs (tentative %d)", delay, poll_attempt)
                    await asyncio.sleep(delay)
                    poll_attempt += 1
                    continue

                # Succès : on remet le compteur de backoff à zéro
                poll_attempt = 0

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message")
                    if not msg or "text" not in msg:
                        continue

                    chat_id = msg["chat"]["id"]
                    text = msg["text"].strip()
                    log.info("Message de %s: %.80s", chat_id, text)

                    try:
                        # Canal ordres du jour : intercepté AVANT #emp, jamais transmis.
                        if text.startswith("#push"):
                            await handle_push(chat_id, text[5:].strip())
                            continue

                        # Canal employé : intercepté AVANT toute session, jamais transmis.
                        if text.startswith("#emp"):
                            await handle_emp(chat_id, text[4:].strip())
                            continue

                        cmd = text.lower().split()[0] if text else ""

                        if cmd in ("/start", "/help"):
                            await send_message(
                                chat_id,
                                "🤖 *Pont Telegram → OpenCode*\n\n"
                                "Envoie un message → OpenCode répond.\n\n"
                                "*/newsession* — nouvelle session\n"
                                "*/sessions* — liste TOUTES les sessions\n"
                                "*/select <n>* — choisir une session active\n"
                                "*/plan* — mode planification (lecture seule)\n"
                                "*/build* — mode construction (actions)\n"
                                "*#emp <texte>* — l'employé autonome (pause/resume/status)\n"
                                "*/help* — cette aide",
                            )

                        elif cmd == "/newsession":
                            await handle_newsession(
                                chat_id,
                                text.lower().split()[1] if len(text.split()) > 1 else "",
                            )

                        elif cmd == "/status":
                            await handle_status(chat_id)

                        elif cmd == "/sessions":
                            await handle_sessions_list(
                                chat_id,
                                text.lower().split()[1] if len(text.split()) > 1 else "",
                            )

                        elif cmd == "/select":
                            await handle_select(chat_id, text.lower().split()[1] if len(text.split()) > 1 else "")

                        elif cmd == "/plan":
                            cid = str(chat_id)
                            state.setdefault(cid, {})["agent"] = "plan"
                            _save_state(state)
                            emoji = "📋"
                            await send_message(chat_id, f"{emoji} Mode passé à `plan` pour la session active.")

                        elif cmd == "/build":
                            cid = str(chat_id)
                            state.setdefault(cid, {})["agent"] = "build"
                            _save_state(state)
                            emoji = "🔧"
                            await send_message(chat_id, f"{emoji} Mode passé à `build` pour la session active.")

                        else:
                            await handle_message(chat_id, text)

                    except Exception as e:
                        log.error("Erreur handler: %s", e)
                        await send_message(chat_id, f"❌ Erreur : {e}")

            except httpx.TimeoutException:
                # Timeout sur getUpdates : backoff
                delay = _backoff_schedule(poll_attempt)
                log.info("Timeout getUpdates, backoff: %.1fs (tentative %d)", delay, poll_attempt)
                await asyncio.sleep(delay)
                poll_attempt += 1
            except httpx.NetworkError as e:
                # Erreur réseau : backoff
                delay = _backoff_schedule(poll_attempt)
                log.error("Erreur réseau polling: %s, backoff: %.1fs (tentative %d)", e, delay, poll_attempt)
                await asyncio.sleep(delay)
                poll_attempt += 1
            except Exception as e:
                log.error("Erreur polling: %s", e)
                delay = _backoff_schedule(poll_attempt)
                log.info("Backoff polling: %.1fs (tentative %d)", delay, poll_attempt)
                await asyncio.sleep(delay)
                poll_attempt += 1


async def selftest(instances):
    """--selftest : crée une session v2, envoie "Réponds exactement : PONG",
    vérifie PONG reçu, sortie 0/1."""
    # Trouver l'instance v2
    inst = instances.get("v2")
    if not inst:
        print("ERREUR: instance v2 non trouvée dans OPENCODE_INSTANCES")
        return False
    try:
        # Créer une session
        session = await cli_create_session(inst)
        if not session or "id" not in session:
            print("ERREUR: création session v2 échouée")
            return False
        sid = session["id"]
        print(f"Session créée: {sid}")
        # Envoyer le prompt
        response = await cli_prompt(inst, sid, "Réponds exactement : PONG")
        print(f"Réponse: {response!r}")
        # Vérifier PONG
        if "PONG" in response.upper():
            print("OK: PONG reçu")
            return True
        else:
            print("ERREUR: PONG non trouvé dans la réponse")
            return False
    except Exception as e:
        print(f"ERREUR selftest: {e}")
        return False


async def notify_test(instances):
    """--notify-test : envoie un message au chat autorisé et exige ok:true."""
    chat_id = 1155339708
    test_text = "🧪 Test de notification depuis bridge.py --notify-test"
    try:
        # Utiliser tg_send directement (bypass handle_message)
        msg_id = await tg_send(chat_id, test_text, markdown=False)
        if msg_id:
            print(f"OK: message envoyé (message_id={msg_id})")
            return True
        else:
            print("ERREUR: aucun message_id retourné")
            return False
    except Exception as e:
        print(f"ERREUR notify_test: {e}")
        return False


if __name__ == "__main__":
    if "--api-probe" in sys.argv:
        asyncio.run(api_probe(parse_instances()))
    elif "--selftest" in sys.argv:
        ok = asyncio.run(selftest(parse_instances()))
        sys.exit(0 if ok else 1)
    elif "--notify-test" in sys.argv:
        ok = asyncio.run(notify_test(parse_instances()))
        sys.exit(0 if ok else 1)
    else:
        asyncio.run(main())
