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

if not TELEGRAM_TOKEN and "--api-probe" not in sys.argv:
    log.error("TELEGRAM_TOKEN not set")
    sys.exit(1)

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Point d'injection de transport : TOUS les httpx.AsyncClient() passent
# transport=TRANSPORT. Par défaut None = client standard. Permet aux tests de
# tourner hors réseau via httpx.MockTransport.
TRANSPORT = None

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


async def tg_send(chat_id: int, text: str, markdown: bool = True) -> None:
    """Envoi fiable : découpe en blocs <= 3500, tente parse_mode Markdown, replie
    en texte brut si l'API renvoie 400 (en journalisant description), lève
    TelegramError sinon. Toute exception réseau est loggée puis propagée."""
    try:
        async with httpx.AsyncClient(transport=TRANSPORT) as cl:
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
    except httpx.HTTPError as e:
        log.exception("Erreur réseau Telegram: %s", e)
        raise


def send_message(chat_id: int, text: str) -> None:
    """Enveloppe de tg_send en markdown (1er essai Markdown, repli texte brut)."""
    return asyncio.to_thread(tg_send, chat_id, text, markdown=True)


def _send_plain(chat_id: int, text: str) -> None:
    """Enveloppe de tg_send sans parse_mode (sortie pusher avec _ et *)."""
    return asyncio.to_thread(tg_send, chat_id, text, markdown=False)


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
log.info("Chats suivis: %d", len(state))


async def api_get(path):
    async with httpx.AsyncClient(base_url=OPENCODE_URL, timeout=httpx.Timeout(10.0)) as cl:
        r = await cl.get(path)
        log.debug("GET %s -> %s", path, r.status_code)
        r.raise_for_status()
        return r.json()


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


async def send_to_session(sid: str, text: str) -> str:
    result = await api_post(f"/session/{sid}/message", {"parts": [{"type": "text", "text": text}]})
    parts = result.get("parts", [])
    return "\n".join(p["text"] for p in parts if p.get("type") == "text").strip() or "(pas de réponse textuelle)"


async def switch_agent(sid: str, agent_id: str):
    return await api_post(f"/api/session/{sid}/agent", {"agent": agent_id})


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


async def get_session_context_tokens(sid: str) -> int:
    try:
        msgs = await api_get(f"/session/{sid}/message?limit=2")
        if isinstance(msgs, list) and len(msgs) >= 2:
            return msgs[0].get("info", {}).get("tokens", {}).get("total", 0)
    except Exception:
        log.exception("Contexte tokens indisponible pour %s", sid)
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
            return None, r.status_code
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


def cli_normalize_session(raw, instance, kind):
    """normalize_session(raw, instance, kind) -> {instance, sid, title, agent,
    model, tokens} quel que soit le serveur."""
    t = raw.get("tokens") or {}
    cache = t.get("cache") if isinstance(t, dict) else {}
    tokens = t.get("input", 0) + t.get("output", 0)
    model = raw.get("model")
    if isinstance(model, dict):
        model = model.get("id")
    title = raw.get("title") or (raw.get("id") or "")[:12]
    return {
        "instance": instance,
        "sid": raw.get("id"),
        "title": title,
        "agent": raw.get("agent") or "build",
        "model": model,
        "tokens": tokens,
    }


async def cli_prompt(inst, sid, text, agent=None):
    """prompt(inst, sid, text, agent). legacy : réponse directe dans parts[].text ;
    api : POST /prompt puis polling GET /message jusqu'à un message assistant
    (timeout 180 s, intervalle 1 s)."""
    kind = await detect_kind(inst)
    if kind == "api":
        payload = {"text": text}
        if agent:
            payload["agents"] = [{"id": agent}]
        await _http_post(inst, f"/api/session/{sid}/prompt", payload, timeout=180.0)
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
        await api_patch(f"/session/{sid}", {"title": title})
        state.setdefault(cid, {}).setdefault("titled", []).append(sid)
        _save_state(state)
        log.info("Session %s titrée: %s", sid, title)
    except Exception as e:
        log.warning("Impossible de titrer session %s: %s", sid, e)


async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient() as cl:
        await cl.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown"},
        )


async def handle_sessions_list(chat_id: int):
    cid = str(chat_id)
    try:
        all_sessions = await list_sessions()
    except Exception as e:
        log.error("Erreur liste sessions: %s", e)
        await send_message(chat_id, f"❌ Erreur récupération sessions : {e}")
        return

    if not all_sessions:
        await send_message(chat_id, "Aucune session. Envoie `/newsession` pour en créer une.")
        return

    sorted_sessions = sorted(all_sessions, key=lambda s: s.get("time", {}).get("created", 0), reverse=True)
    current_sid = state.get(cid, {}).get("current")

    show_all = len(sorted_sessions) <= 30
    limit = None if show_all else 20
    displayed = sorted_sessions[:limit]

    lines = []
    for i, s in enumerate(displayed, 1):
        sid = s["id"]
        title = s.get("title", sid[:12])
        marker = " ◀️" if sid == current_sid else ""
        model = s.get("model", {}).get("id", "?")
        agent = s.get("agent", "?")
        agent_icon = "📋" if agent == "plan" else "🔧"
        lines.append(f"{i}. `{title}`{marker}  {agent_icon}`{agent}`  _{model}_")

    total = len(sorted_sessions)
    if limit and total > limit:
        lines.append(f"\n... et {total - limit} autres sessions")
    lines.append("")
    lines.append("Utilise `/select <n>` pour changer de session active.")

    await send_message(chat_id, "\n".join(lines))


async def handle_select(chat_id: int, arg: str):
    cid = str(chat_id)

    if not arg:
        await send_message(chat_id, "Utilise `/select <n>` avec le numéro de la session.")
        return

    try:
        n = int(arg)
    except ValueError:
        await send_message(chat_id, "Le numéro doit être un entier.")
        return

    try:
        all_sessions = await list_sessions()
    except Exception as e:
        await send_message(chat_id, f"❌ Erreur : {e}")
        return

    if not all_sessions:
        await send_message(chat_id, "Aucune session disponible.")
        return

    sorted_sessions = sorted(all_sessions, key=lambda s: s.get("time", {}).get("created", 0), reverse=True)

    if n < 1 or n > len(sorted_sessions):
        await send_message(chat_id, f"Numéro invalide. Choisis entre 1 et {len(sorted_sessions)}.")
        return

    selected = sorted_sessions[n - 1]
    sid = selected["id"]
    title = selected.get("title", sid[:12])

    if cid not in state:
        state[cid] = {}
    state[cid]["current"] = sid
    _save_state(state)

    try:
        ctx = await get_session_context_tokens(sid)
        suffix = f"  (contexte {_fmt_tokens(ctx)} tokens)" if ctx else ""
    except Exception:
        suffix = ""

    agent = selected.get("agent", state.get(cid, {}).get("agent", "build"))
    agent_icon = "📋" if agent == "plan" else "🔧"
    state.setdefault(cid, {})["agent"] = agent
    _save_state(state)
    await send_message(chat_id, f"✅ Session active : `{title}`  {agent_icon}`{agent}`{suffix}")


async def handle_newsession(chat_id: int):
    cid = str(chat_id)
    try:
        s = await create_session()
        sid = s["id"]
        title = s.get("title", sid[:12])
        if cid not in state:
            state[cid] = {}
        state[cid]["current"] = sid
        _save_state(state)
        log.info("Nouvelle session %s pour chat %s", sid, cid)
        await send_message(chat_id, f"✅ Nouvelle session créée : `{title}`  (contexte 0 tokens)")
    except Exception as e:
        log.error("Erreur création session: %s", e)
        await send_message(chat_id, f"❌ Erreur : {e}")


async def handle_switch_agent(chat_id: int, agent_id: str):
    cid = str(chat_id)
    current_sid = state.get(cid, {}).get("current")

    if not current_sid:
        await send_message(chat_id, "Aucune session active. Crée‑en une avec `/newsession` d'abord.")
        return

    try:
        await switch_agent(current_sid, agent_id)
        state.setdefault(cid, {})["agent"] = agent_id
        _save_state(state)
        emoji = "📋" if agent_id == "plan" else "🔧"
        await send_message(chat_id, f"{emoji} Mode passé à `{agent_id}` pour la session active.")
        log.info("Session %s → agent %s", current_sid, agent_id)
    except Exception as e:
        log.error("Erreur switch agent: %s", e)
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


async def _send_plain(chat_id: int, text: str):
    """Envoi sans parse_mode : la sortie du pusher contient des _ et des *."""
    async with httpx.AsyncClient() as cl:
        await cl.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
        )


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


async def handle_message(chat_id: int, text: str):
    cid = str(chat_id)
    current_sid = state.get(cid, {}).get("current")

    if not current_sid:
        try:
            s = await create_session()
            current_sid = s["id"]
            title = s.get("title", current_sid[:12])
            if cid not in state:
                state[cid] = {}
            state[cid]["current"] = current_sid
            _save_state(state)
            log.info("Auto-session %s pour chat %s (%s)", current_sid, cid, title)
        except Exception as e:
            log.error("Erreur création auto-session: %s", e)
            await send_message(chat_id, f"❌ Erreur création session : {e}")
            return

    try:
        response = await send_to_session(current_sid, text)
        # Titrer la session après le 1er message si pas déjà fait
        await ensure_session_titled(cid, current_sid, text)
        await send_message(chat_id, response)
        log.info("Réponse envoyée (%d chars)", len(response))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            log.warning("Session %s introuvable, création d'une nouvelle", current_sid)
            del state[cid]["current"]
            _save_state(state)
            await handle_message(chat_id, text)
        else:
            log.error("Erreur HTTP: %s", e)
            await send_message(chat_id, f"❌ Erreur : {e}")
    except Exception as e:
        log.error("Erreur: %s", e)
        await send_message(chat_id, f"❌ Erreur : {e}")


async def main():
    offset = 0
    log.info("Pont Telegram → OpenCode démarré (polling direct)")

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
                    await asyncio.sleep(5)
                    continue

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
                            await handle_newsession(chat_id)

                        elif cmd == "/sessions":
                            await handle_sessions_list(chat_id)

                        elif cmd == "/select":
                            await handle_select(chat_id, text.lower().split()[1] if len(text.split()) > 1 else "")

                        elif cmd == "/plan":
                            await handle_switch_agent(chat_id, "plan")

                        elif cmd == "/build":
                            await handle_switch_agent(chat_id, "build")

                        else:
                            await handle_message(chat_id, text)

                    except Exception as e:
                        log.error("Erreur handler: %s", e)
                        await send_message(chat_id, f"❌ Erreur : {e}")

            except httpx.TimeoutException:
                pass
            except Exception as e:
                log.error("Erreur polling: %s", e)
                await asyncio.sleep(5)


if __name__ == "__main__":
    if "--api-probe" in sys.argv:
        asyncio.run(api_probe(parse_instances()))
    else:
        asyncio.run(main())
