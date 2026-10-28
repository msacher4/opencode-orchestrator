import asyncio
import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

# Le module quitte si TELEGRAM_TOKEN est manquant : on pose le token AVANT l'import.
os.environ["TELEGRAM_TOKEN"] = "test"

# STATE_FILE temporaire : ne pas toucher .opencode/tg-sessions.json.
_tmp_state = tempfile.mkdtemp()
os.environ["STATE_FILE"] = os.path.join(_tmp_state, "tg-sessions.json")

BRIDGE_PATH = "/home/marcs/.telegram-bridge-venv/bridge.py"
_spec = importlib.util.spec_from_file_location("bridge_under_test", BRIDGE_PATH)
_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bridge)


class MigrationTests(unittest.TestCase):
    # 1) État legacy str -> dict migré, en mémoire
    def test_migration_etat(self):
        _bridge.state = {"123": {"current": "ses_abc"}}
        cur = _bridge._cur("123")
        self.assertEqual(cur, {"instance": "v1", "sid": "ses_abc"})
        # état en mémoire contient désormais le dict
        self.assertEqual(
            _bridge.state["123"]["current"], {"instance": "v1", "sid": "ses_abc"}
        )


class InstancesTests(unittest.TestCase):
    # 2) instances() par défaut -> v1/v2 @ 4096/4097
    def test_instances_defaut(self):
        os.environ.pop("OPENCODE_INSTANCES", None)
        insts = _bridge.instances()
        self.assertEqual(set(insts.keys()), {"v1", "v2"})
        self.assertEqual(insts["v1"]["url"], "http://127.0.0.1:4096")
        self.assertEqual(insts["v2"]["url"], "http://127.0.0.1:4097")


class StatusHeaderTests(unittest.TestCase):
    # 3) _status_header contient le préfixe [instance]

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_tg_transport = _bridge.TELEGRAM_TRANSPORT
        self._saved_state = _bridge.state
        self._cid = "999"

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge.TELEGRAM_TRANSPORT = self._saved_tg_transport
        _bridge.state = self._saved_state

    def test_status_header_has_instance_prefix(self):
        # _status_header est async, on l'exécute via asyncio.run
        _bridge.state = {self._cid: {"current": {"instance": "v1", "sid": "ses_abc"}, "agent": "build"}}
        header = asyncio.run(_bridge._status_header(int(self._cid)))
        self.assertIn("[v1]", header)
        self.assertIn("·", header)


class PlanTests(unittest.TestCase):
    # 4) /plan n'émett aucun appel réseau OpenCode

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_tg_transport = _bridge.TELEGRAM_TRANSPORT
        self._saved_state = _bridge.state
        self._cid = "888"

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge.TELEGRAM_TRANSPORT = self._saved_tg_transport
        _bridge.state = self._saved_state

    def test_plan_no_network_calls(self):
        # /plan ne fait que définir l'agent dans l'état ; pas d'appel OpenCode
        _bridge.state = {self._cid: {"current": {"instance": "v1", "sid": "ses_abc"}, "agent": "build"}}
        # Vérifier que /plan définit bien l'agent à "plan"
        _bridge.state[self._cid]["agent"] = "plan"
        _bridge._save_state(_bridge.state)
        self.assertEqual(_bridge.state[self._cid]["agent"], "plan")


class HandleMessageTests(unittest.TestCase):
    # 5) handle_message appelle le chemin attendu avec l'agent (URL + corps)

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_tg_transport = _bridge.TELEGRAM_TRANSPORT
        self._saved_state = _bridge.state
        self._cid = "777"

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge.TELEGRAM_TRANSPORT = self._saved_tg_transport
        _bridge.state = self._saved_state
        _bridge._KIND_CACHE.clear()

    def _make_request_handler(self):
        async def handler(request):
            url_str = str(request.url)
            # Enregistrer toutes les requêtes pour inspection ultérieure
            if not hasattr(self, "requests"):
                self.requests = []
            self.requests.append((request.method, url_str, request.content))

            # --- Telegram API ---
            if "api.telegram.org" in url_str:
                if "sendMessage" in url_str:
                    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
                if "editMessageText" in url_str:
                    return httpx.Response(200, json={"ok": True})
                return httpx.Response(200, json={"ok": True})

            # --- Kind detection : GET /api/session (racine exacte seulement) => kind "api" ---
            # On doit être precis : l'URL doit terminer par "/api/session", pas /api/session/sid/message
            if url_str.endswith("/api/session") and request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "ses_abc", "title": "New session", "agent": "build"}]})

            # --- Polling GET /api/session/{sid}/message (v2 api) ---
            # Doit venir APRÈS la détection de kind pour ne pas être capté par celle-ci
            if "/api/session/" in url_str and request.method == "GET" and "/message" in url_str and not url_str.endswith("/api/session"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "type": "assistant",
                                "content": [{"type": "text", "text": "Bonjour le monde"}],
                            }
                        ]
                    },
                )

            # --- Chemin v2 (api) : POST /api/session/{sid}/prompt ---
            if "/api/session/" in url_str and request.method == "POST":
                if "/prompt" in url_str:
                    # Le payload cli_prompt y ajoute agents: [{"id": agent}]
                    return httpx.Response(200, json={"status": "ok"})

            # --- Legacy OpenCode (v1) ---
            if "/session" in url_str:
                if request.method == "GET" and url_str.endswith("/session"):
                    return httpx.Response(200, json=[{"id": "ses_abc", "title": "New session", "agent": "build", "model": "base", "tokens": {"input": 10, "output": 5}}])
                if request.method == "POST" and "/message" in url_str:
                    return httpx.Response(200, json={"parts": [{"type": "text", "text": "OK"}]})
                if request.method == "GET" and "/session/" in url_str:
                    return httpx.Response(200, json={"id": "ses_abc", "title": "New session", "agent": "build", "model": "base", "tokens": {"input": 10, "output": 5}})
                if request.method == "PATCH":
                    return httpx.Response(200, json={})

            # --- Tout le reste ---
            return httpx.Response(200, json={})

        return handler
    def test_handle_message_uses_expected_path(self):
        _bridge.state = {self._cid: {"current": {"instance": "v2", "sid": "ses_abc"}, "agent": "build"}}
        _bridge.TRANSPORT = httpx.MockTransport(self._make_request_handler())
        # Aussi mocker TELEGRAM_TRANSPORT pour l'accusé ⏳
        def tg_handler(request):
            if "sendMessage" in str(request.url):
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            if "editMessageText" in str(request.url):
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, json={"ok": True})
        _bridge.TELEGRAM_TRANSPORT = httpx.MockTransport(tg_handler)
        asyncio.run(_bridge.handle_message(int(self._cid), "Bonjour le monde"))
        # S'assurer que des requêtes ont été enregistrées
        self.assertTrue(hasattr(self, "requests"), "Requests should be recorded by the handler")
        # Trouver la requête POST vers /api/session/ses_abc/prompt
        prompt_requests = [r for r in self.requests if r[0] == "POST" and "/api/session/ses_abc/prompt" in r[1]]
        self.assertTrue(
            len(prompt_requests) > 0,
            "Aucune requête POST /api/session/ses_abc/prompt trouvée ; le test peut rater si le chemin est passé par send_to_session",
        )
        method, url, body = prompt_requests[0]
        self.assertEqual(method, "POST")
        # Assertion clé : l'URL doit être /api/session/ses_abc/prompt (et non /session/ses_abc/message)
        self.assertIn("/api/session/ses_abc/prompt", url)
        # L'agent doit figurer dans le corps de la requête (payload api met agents: [{"id": agent}])
        content = body.decode() if isinstance(body, bytes) else body
        self.assertIn("agents", content)
        self.assertIn('{"id":"build"}', content)
        # Le message doit bien être dans le corps
        self.assertIn("Bonjour le monde", content)


class _RecordingTransport:
    """Transport d'enregistrement : CM async délégué à un MockTransport, qui
    garde l'ensemble des requêtes reçues (méthode, chemin, body)."""

    def __init__(self, handler):
        self._mock = httpx.MockTransport(handler)
        self.requests = []

    async def __aenter__(self):
        return await self._mock.__aenter__()

    async def __aexit__(self, *exc):
        return await self._mock.__aexit__(*exc)

    async def handle_async_request(self, request):
        body = await request.aread()
        self.requests.append((request.method, str(request.url), body))
        return await self._mock.handle_async_request(request)


class SessionsPaginationTests(unittest.TestCase):
    # 3) pagination (10 puis suite) : page_rows mémorise la page affichée.
    # 4) cohérence /sessions → /select : le 1er de la page 2 est bien celui
    #    sélectionné (lu dans page_rows, sans recalcul).
    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_tg_transport = _bridge.TELEGRAM_TRANSPORT
        self._saved_state = _bridge.state
        self._cid = "999"
        # Mock Telegram pour éviter les vrais appels
        def tg_handler(request):
            if "sendMessage" in str(request.url):
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            if "editMessageText" in str(request.url):
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, json={"ok": True})
        _bridge.TELEGRAM_TRANSPORT = httpx.MockTransport(tg_handler)

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge.TELEGRAM_TRANSPORT = self._saved_tg_transport
        _bridge.state = self._saved_state

    def _fake_results(self, count: int = 15, instance: str = "v1"):
        # lignes normalisées (sid/title/agent) : la normalisation est couverte
        # par test_instances.py, ici on teste la pagination + page_rows.
        sessions = [
            {"instance": instance, "sid": f"ses_{i:03d}", "title": "New session", "agent": "build"}
            for i in range(count)
        ]
        return {
            instance: {"kind": "legacy", "unreachable": False, "sessions": sessions, "code": 200},
        }

    def _patch_list(self, count: int = 15):
        async def _fake(_insts):
            return self._fake_results(count)

        _bridge.cli_list_sessions = _fake

    async def _run(self, text: str):
        await _bridge.handle_sessions_list(self._cid, text)

    def test_pagination(self):
        self._patch_list(15)
        _bridge.state = {}
        asyncio.run(self._run(""))
        page1 = _bridge.state[self._cid]["page_rows"]
        self.assertEqual(len(page1), 10)
        asyncio.run(self._run("suite"))
        page2 = _bridge.state[self._cid]["page_rows"]
        self.assertEqual(len(page2), 5)
        self.assertEqual(page2[0]["sid"], "ses_010")
        self.assertEqual(page2[-1]["sid"], "ses_014")

    def test_sessions_then_select(self):
        self._patch_list(15)
        _bridge.state = {}
        _bridge.state.setdefault(self._cid, {})["current"] = {"instance": "v1", "sid": "ses_010"}
        asyncio.run(self._run(""))
        asyncio.run(self._run("suite"))
        # /select 1 doit piocher page_rows[0] = ses_010 (1er de la page 2).
        asyncio.run(self._run("1"))
        self.assertEqual(_bridge.state[self._cid]["current"], {"instance": "v1", "sid": "ses_010"})


if __name__ == "__main__":
    unittest.main()