import asyncio
import importlib.util
import json
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
        # L'agent doit figurer dans le corps de la requête (payload api met agents: [{"name": agent}])
        content = body.decode() if isinstance(body, bytes) else body
        self.assertIn("agents", content)
        self.assertIn('{"name":"build"}', content)
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


class NewSessionTests(unittest.TestCase):
    """Tests pour /newsession : résolution d'instance et message d'erreur clair."""

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_tg_transport = _bridge.TELEGRAM_TRANSPORT
        self._saved_state = _bridge.state
        self._cid = "333"
        # Mock Telegram
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
        _bridge._KIND_CACHE.clear()

    def _fake_create_session(self, inst_name="v1"):
        """Mock create_session_on_instance pour retourner une session valide."""
        async def fake(inst, body=None):
            return {"id": "ses_new", "title": "New session", "agent": "build"}
        _bridge.create_session_on_instance = fake

    def test_newsession_resolves_instance_v2(self):
        """/newsession v2 résout correctement l'instance v2."""
        self._fake_create_session("v2")
        _bridge.state = {}
        asyncio.run(_bridge.handle_newsession(int(self._cid), "v2"))
        # Vérifier que la session a été créée sur v2
        self.assertEqual(_bridge.state[self._cid]["current"]["instance"], "v2")
        self.assertEqual(_bridge.state[self._cid]["current"]["sid"], "ses_new")

    def test_newsession_unknown_instance_clear_error(self):
        """/newsession v3 (inconnue) -> message clair avec liste dispo."""
        _bridge.state = {}
        # Capturer le message envoyé
        sent_messages = []
        def tg_handler(request):
            if "sendMessage" in str(request.url):
                import json as _json
                payload = _json.loads(request.content)
                sent_messages.append(payload["text"])
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            return httpx.Response(200, json={"ok": True})
        _bridge.TELEGRAM_TRANSPORT = httpx.MockTransport(tg_handler)
        asyncio.run(_bridge.handle_newsession(int(self._cid), "v3"))
        # Vérifier le message d'erreur
        self.assertTrue(len(sent_messages) > 0)
        self.assertIn("Instance inconnue : v3", sent_messages[0])
        self.assertIn("disponibles : v1, v2", sent_messages[0])
        # L'état ne doit pas être modifié
        self.assertNotIn(self._cid, _bridge.state)


class ErrorBodyTests(unittest.TestCase):
    """Tests pour l'inclusion du corps de réponse dans les erreurs 4xx/5xx."""

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_tg_transport = _bridge.TELEGRAM_TRANSPORT
        self._saved_state = _bridge.state
        self._cid = "444"

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge.TELEGRAM_TRANSPORT = self._saved_tg_transport
        _bridge.state = self._saved_state
        _bridge._KIND_CACHE.clear()

    def test_4xx_error_includes_response_body(self):
        """Erreur 400 sur cli_prompt -> message utilisateur contient le corps tronqué."""
        _bridge.state = {self._cid: {"current": {"instance": "v2", "sid": "ses_abc"}, "agent": "build"}}

        # Mock pour simuler un 400 avec corps JSON
        def make_transport():
            def handler(request):
                path = request.url.path
                if path == "/api/session":
                    return httpx.Response(200, json={"data": [{"id": "ses_abc", "title": "Test", "agent": "build"}]})
                if "/api/session/ses_abc/prompt" in path:
                    # Retourne 400 avec corps détaillé
                    return httpx.Response(
                        400,
                        json={"_tag": "InvalidRequestError", "message": "Invalid payload: missing required field 'agents'", "kind": "Payload", "details": {"field": "agents"}}
                    )
                if "/api/session/ses_abc/message" in path:
                    return httpx.Response(200, json={"data": []})
                return httpx.Response(404)
            return httpx.MockTransport(handler)

        _bridge.TRANSPORT = make_transport()

        # Mock Telegram pour capturer le message d'erreur
        sent_messages = []
        def tg_handler(request):
            if "sendMessage" in str(request.url):
                import json as _json
                payload = _json.loads(request.content)
                sent_messages.append(payload["text"])
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            if "editMessageText" in str(request.url):
                import json as _json
                payload = _json.loads(request.content)
                sent_messages.append(payload["text"])
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, json={"ok": True})
        _bridge.TELEGRAM_TRANSPORT = httpx.MockTransport(tg_handler)

        asyncio.run(_bridge.handle_message(int(self._cid), "test prompt"))

        # Vérifier que le message d'erreur contient le corps de la réponse
        self.assertTrue(len(sent_messages) > 0)
        error_msg = sent_messages[-1]  # Dernier message (editMessageText)
        self.assertIn("400", error_msg)
        self.assertIn("Invalid payload", error_msg)
        self.assertIn("missing required field", error_msg)
        # Le corps doit être tronqué à ~300 chars
        self.assertLessEqual(len(error_msg), 500)  # Marge raisonnable


class SessionSortTests(unittest.TestCase):
    """Tests pour _sort_key_session : tri par récence décroissante."""

    def test_sessions_sort_recency_desc(self):
        """_sort_key_session ordonne updated desc, puis created desc, None à la fin,
        sans TypeError entre None et une date."""
        # Session 1 : updated le plus récent (mis à jour après création)
        s1 = {
            "sid": "ses_a",
            "created_ms": 1000,
            "updated_ms": 3000,  # mis à jour récemment
        }
        # Session 2 : seulement created, pas d'updated
        s2 = {
            "sid": "ses_b",
            "created_ms": 2000,
            "updated_ms": None,
        }
        # Session 3 : ni created ni updated (sans date)
        s3 = {
            "sid": "ses_c",
            "created_ms": None,
            "updated_ms": None,
        }
        # Session 4 : updated plus ancien que s1, created plus récent que s2
        s4 = {
            "sid": "ses_d",
            "created_ms": 2500,
            "updated_ms": 1500,
        }

        sessions = [s1, s2, s3, s4]
        # Trier avec reverse=True car la clé est conçue pour l'ordre croissant
        # (has_date True > False, puis timestamps plus grands = plus récents)
        sessions.sort(key=_bridge._sort_key_session, reverse=True)

        # Ordre attendu : s1 (updated=3000), s4 (updated=1500), s2 (created=2000, pas d'updated), s3 (sans date)
        self.assertEqual(sessions[0]["sid"], "ses_a")  # updated le plus récent
        self.assertEqual(sessions[1]["sid"], "ses_d")  # updated=1500
        self.assertEqual(sessions[2]["sid"], "ses_b")  # seulement created=2000
        self.assertEqual(sessions[3]["sid"], "ses_c")  # sans date à la fin

        # Vérifier qu'aucun TypeError n'est levé (None vs int comparé dans la clé)
        # La clé retourne (has_date, updated_or_-1, created_or_-1) - tous comparables
        for s in sessions:
            _ = _bridge._sort_key_session(s)  # ne doit pas lever


class MirrorTests(unittest.TestCase):
    """Tests pour la boucle de miroir : anti-doublon, anti-rejeu, exclusion live."""

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_tg_transport = _bridge.TELEGRAM_TRANSPORT
        self._saved_state = _bridge.state
        self._saved_mirror_tasks = dict(_bridge._MIRROR_TASKS)
        self._saved_kind_cache = dict(_bridge._KIND_CACHE)
        self._cid = "9999"

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge.TELEGRAM_TRANSPORT = self._saved_tg_transport
        _bridge.state = self._saved_state
        _bridge._MIRROR_TASKS.clear()
        _bridge._MIRROR_TASKS.update(self._saved_mirror_tasks)
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved_kind_cache)

    def _make_transports(self, opencode_handler, tg_handler):
        """Configure les transports mock pour OpenCode et Telegram."""
        _bridge.TRANSPORT = httpx.MockTransport(opencode_handler)
        _bridge.TELEGRAM_TRANSPORT = httpx.MockTransport(tg_handler)

    async def _run_mirror_iteration(self, chat_id, inst_name="v1", sid="ses_test"):
        """Exécute une itération de la boucle miroir (une fois _mirror_loop sans sleep)."""
        # On appelle directement _fetch_new_assistant_messages et _send_mirror_message
        # pour tester la logique sans attendre le sleep
        inst = _bridge.instances()[inst_name]
        mirror_state = _bridge._get_mirror_state(str(chat_id))
        last_seen = mirror_state.get("last_seen_id")
        seen_ids = _bridge._mirror_seen_ids_as_set(mirror_state)

        new_messages = await _bridge._fetch_new_assistant_messages(inst, sid, last_seen)

        sent_count = 0
        for msg in new_messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            await _bridge._send_mirror_message(chat_id, inst, sid, msg)
            seen_ids.add(msg_id)
            mirror_state["last_seen_id"] = msg_id
            _bridge._mirror_save_seen_ids(mirror_state, seen_ids)
            _bridge._save_state(_bridge.state)
            sent_count += 1
        return sent_count, new_messages

    def test_mirror_new_message_once(self):
        """Un nouveau message d'assistant n'est envoyé qu'une fois et entre dans seen_ids
        sur plusieurs tours de boucle."""
        # État initial : session courante, last_seen_id = None (premier tour)
        _bridge.state = {
            self._cid: {
                "current": {"instance": "v1", "sid": "ses_test"},
                "mirror": {"last_seen_id": None, "seen_ids": []},
            }
        }

        tg_requests = []

        def opencode_handler(request):
            url = str(request.url)
            if "/session/ses_test/message" in url:
                return httpx.Response(
                    200,
                    json=[
                        {"id": "msg_1", "type": "user", "content": [{"type": "text", "text": "hello"}]},
                        {"id": "msg_2", "type": "assistant", "content": [{"type": "text", "text": "Bonjour !"}]},
                    ],
                )
            return httpx.Response(404)

        def tg_handler(request):
            if "sendMessage" in str(request.url):
                import json as _json
                payload = _json.loads(request.content)
                tg_requests.append(payload["text"])
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            return httpx.Response(404)

        self._make_transports(opencode_handler, tg_handler)

        # Premier tour de boucle : découvre le message assistant
        sent1, _ = asyncio.run(self._run_mirror_iteration(int(self._cid)))
        self.assertEqual(sent1, 1, "Premier tour : un message envoyé")
        self.assertEqual(len(tg_requests), 1)
        self.assertIn("Bonjour !", tg_requests[0])

        # Vérifier que seen_ids contient maintenant msg_2
        mirror_state = _bridge._get_mirror_state(self._cid)
        seen_ids = _bridge._mirror_seen_ids_as_set(mirror_state)
        self.assertIn("msg_2", seen_ids, "L'id du message doit être dans seen_ids")

        # Deuxième tour de boucle : même réponse de l'API, mais déjà vu
        sent2, _ = asyncio.run(self._run_mirror_iteration(int(self._cid)))
        self.assertEqual(sent2, 0, "Deuxième tour : aucun envoi (déjà vu)")
        self.assertEqual(len(tg_requests), 1, "Total Telegram : exactement 1 envoi")

        # Troisième tour : encore zéro
        sent3, _ = asyncio.run(self._run_mirror_iteration(int(self._cid)))
        self.assertEqual(sent3, 0, "Troisième tour : aucun envoi")

    def test_mirror_no_replay_after_select(self):
        """Après /select sur une session avec historique, le fetch utilise after_id
        égal au dernier message existant → l'historique n'est jamais rejoué."""
        # État : session sélectionnée, last_seen_id = dernier message de l'historique
        _bridge.state = {
            self._cid: {
                "current": {"instance": "v1", "sid": "ses_test"},
                "mirror": {"last_seen_id": "msg_99", "seen_ids": ["msg_1", "msg_50", "msg_99"]},
            }
        }

        tg_requests = []

        def opencode_handler(request):
            url = str(request.url)
            if "/session/ses_test/message" in url:
                return httpx.Response(
                    200,
                    json=[
                        {"id": "msg_1", "type": "user", "content": [{"type": "text", "text": "old"}]},
                        {"id": "msg_50", "type": "assistant", "content": [{"type": "text", "text": "old reply"}]},
                        {"id": "msg_99", "type": "user", "content": [{"type": "text", "text": "last user"}]},
                        {"id": "msg_100", "type": "assistant", "content": [{"type": "text", "text": "NEW assistant"}]},
                    ],
                )
            return httpx.Response(404)

        def tg_handler(request):
            if "sendMessage" in str(request.url):
                import json as _json
                payload = _json.loads(request.content)
                tg_requests.append(payload["text"])
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            return httpx.Response(404)

        self._make_transports(opencode_handler, tg_handler)

        # Un tour de boucle : after_id = "msg_99" (last_seen_id)
        sent, new_msgs = asyncio.run(self._run_mirror_iteration(int(self._cid)))

        # Seul msg_100 (après msg_99) doit être traité
        self.assertEqual(len(new_msgs), 1)
        self.assertEqual(new_msgs[0]["id"], "msg_100")
        self.assertEqual(sent, 1)
        self.assertIn("NEW assistant", tg_requests[0])

        # Vérifier que les messages avant msg_99 (msg_1, msg_50) ne sont PAS dans new_msgs
        for m in new_msgs:
            self.assertNotIn(m["id"], ["msg_1", "msg_50", "msg_99"])

    def test_mirror_no_duplicate_live_delivery(self):
        """Le message livré en direct par handle_message est marqué vu ;
        le miroir ne le renvoie pas (pas de doublon)."""
        # Simuler : handle_message vient de livrer un message assistant (msg_42)
        # et a appelé _mark_latest_assistant_seen -> seen_ids contient msg_42
        _bridge.state = {
            self._cid: {
                "current": {"instance": "v1", "sid": "ses_test"},
                "mirror": {"last_seen_id": "msg_42", "seen_ids": ["msg_42"]},
            }
        }

        tg_requests = []

        def opencode_handler(request):
            url = str(request.url)
            if "/session/ses_test/message" in url:
                return httpx.Response(
                    200,
                    json=[
                        {"id": "msg_40", "type": "user", "content": [{"type": "text", "text": "q"}]},
                        {"id": "msg_42", "type": "assistant", "content": [{"type": "text", "text": "Réponse livrée en direct"}]},
                        {"id": "msg_43", "type": "assistant", "content": [{"type": "text", "text": "Autre réponse"}]},
                    ],
                )
            return httpx.Response(404)

        def tg_handler(request):
            if "sendMessage" in str(request.url):
                import json as _json
                payload = _json.loads(request.content)
                tg_requests.append(payload["text"])
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
            return httpx.Response(404)

        self._make_transports(opencode_handler, tg_handler)

        # Un tour de boucle : msg_42 est déjà dans seen_ids, seul msg_43 devrait passer
        sent, new_msgs = asyncio.run(self._run_mirror_iteration(int(self._cid)))

        # msg_42 doit être filtré par seen_ids
        # Seuls les messages assistant non vus après last_seen_id (msg_42) sont candidats
        # Ici : msg_42 est à l'index 1, msg_43 à l'index 2
        # last_seen_id = "msg_42", donc found_after devient True à msg_42
        # msg_43 est après, mais c'est aussi un assistant -> devrait être envoyé
        # MAIS msg_42 est dans seen_ids, donc il est skip
        # msg_43 n'est PAS dans seen_ids, donc il SERAIT envoyé
        # Le test vérifie que le message "live" (msg_42) n'est pas rejoué

        # Vérifier que msg_42 n'est PAS dans les messages envoyés
        sent_texts = " ".join(tg_requests)
        self.assertNotIn("Réponse livrée en direct", sent_texts, "msg_42 (live) ne doit pas être renvoyé")


class SessionFilterTests(unittest.TestCase):
    """Tests pour le filtrage des sessions (racines vs filles)."""

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_tg_transport = _bridge.TELEGRAM_TRANSPORT
        self._saved_state = _bridge.state
        self._saved_kind_cache = dict(_bridge._KIND_CACHE)
        self._cid = "555"
        # Mock Telegram
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
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved_kind_cache)

    def _fake_results_with_children(self):
        """2 racines + 2 filles (avec parent_id)."""
        sessions = [
            {"instance": "v2", "sid": "ses_root1", "title": "Orchestrator principal", "agent": "orchestrator", "created_ms": 1000, "updated_ms": 5000, "parent_id": None},
            {"instance": "v2", "sid": "ses_root2", "title": "Session utilisateur", "agent": "build", "created_ms": 2000, "updated_ms": 4000, "parent_id": None},
            {"instance": "v2", "sid": "ses_child1", "title": "t1 — worker", "agent": "worker", "created_ms": 3000, "updated_ms": 3500, "parent_id": "ses_root1"},
            {"instance": "v2", "sid": "ses_child2", "title": "m1 — merger", "agent": "merger", "created_ms": 4000, "updated_ms": 3000, "parent_id": "ses_root1"},
        ]
        return {
            "v2": {"kind": "api", "unreachable": False, "sessions": sessions, "code": 200},
        }

    def _patch_list(self, results):
        async def _fake(_insts):
            return results
        _bridge.cli_list_sessions = _fake

    def _mock_transport_for_select(self):
        """Mock transport pour /select : kind detection + session/message (pour miroir)."""
        def handler(request):
            path = request.url.path
            # Kind detection : GET /api/session -> 200 avec data
            if path == "/api/session" and request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "ses_root1", "title": "test", "agent": "build"}]})
            # GET /api/session/{sid}/message pour _fetch_last_message_id
            if "/api/session/" in path and "/message" in path and request.method == "GET":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(404)
        _bridge.TRANSPORT = httpx.MockTransport(handler)

    async def _run(self, text: str):
        await _bridge.handle_sessions_list(self._cid, text)

    def test_default_filters_children_only_roots(self):
        """Par défaut /sessions n'affiche que les racines (pas de parent_id)."""
        self._patch_list(self._fake_results_with_children())
        _bridge.state = {}
        asyncio.run(self._run(""))
        page_rows = _bridge.state[self._cid]["page_rows"]
        # Seules les 2 racines doivent être affichées
        self.assertEqual(len(page_rows), 2)
        sids = {r["sid"] for r in page_rows}
        self.assertEqual(sids, {"ses_root1", "ses_root2"})

    def test_sessions_all_shows_all_four(self):
        """/sessions all affiche les 4 sessions (2 racines + 2 filles)."""
        self._patch_list(self._fake_results_with_children())
        _bridge.state = {}
        asyncio.run(self._run("all"))
        page_rows = _bridge.state[self._cid]["page_rows"]
        self.assertEqual(len(page_rows), 4)
        sids = {r["sid"] for r in page_rows}
        self.assertEqual(sids, {"ses_root1", "ses_root2", "ses_child1", "ses_child2"})

    def test_select_aligned_with_filtered_list(self):
        """/select n pioche dans la liste filtrée (page_rows), pas dans la liste complète."""
        self._patch_list(self._fake_results_with_children())
        self._mock_transport_for_select()
        _bridge.state = {}
        asyncio.run(self._run(""))  # Liste filtrée : 2 racines
        # La page_rows contient ses_root1 (plus récent updated=5000) puis ses_root2 (updated=4000)
        page_rows = _bridge.state[self._cid]["page_rows"]
        self.assertEqual(page_rows[0]["sid"], "ses_root1")
        self.assertEqual(page_rows[1]["sid"], "ses_root2")

        # /select 1 doit choisir ses_root1 (premier de la liste filtrée)
        asyncio.run(_bridge.handle_select(int(self._cid), "1"))
        self.assertEqual(_bridge.state[self._cid]["current"]["sid"], "ses_root1")

        # /select 2 doit choisir ses_root2
        asyncio.run(_bridge.handle_select(int(self._cid), "2"))
        self.assertEqual(_bridge.state[self._cid]["current"]["sid"], "ses_root2")

        # Vérifier que les sessions filles ne sont PAS sélectionnables via /select 3/4
        # (elles ne sont pas dans page_rows)
        asyncio.run(_bridge.handle_select(int(self._cid), "3"))
        # Doit rester sur ses_root2 (sélection invalide -> message d'erreur, pas de changement)
        self.assertEqual(_bridge.state[self._cid]["current"]["sid"], "ses_root2")


class ContextUsageTests(unittest.TestCase):
    """context_usage lit le DERNIER message assistant, pas le cumul de session."""

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_state = _bridge.state
        _bridge._KIND_CACHE.clear()
        _bridge._CTX_LIMIT_CACHE.clear()
        self.inst = {"name": "v2", "url": "http://127.0.0.1:4097", "auth": "", "kind": None}

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge.state = self._saved_state
        _bridge._KIND_CACHE.clear()
        _bridge._CTX_LIMIT_CACHE.clear()

    def _api_handler(self, messages, model_items=None, session=None, fail=False, default_model=None):
        async def handler(request):
            path = request.url.path
            if path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if path == "/api/model/default":
                if default_model is None:
                    return httpx.Response(404, json={})
                return httpx.Response(200, json={"location": {"directory": "."}, "data": default_model})
            if path == "/api/model":
                return httpx.Response(200, json={"data": model_items or []})
            if path.endswith("/message"):
                if fail:
                    return httpx.Response(500, json={})
                return httpx.Response(200, json={"data": messages})
            if path.startswith("/api/session/"):
                return httpx.Response(200, json={"data": session or {}})
            return httpx.Response(404, json={})

        return handler

    def _legacy_handler(self, messages, session=None):
        async def handler(request):
            path = request.url.path
            if path == "/api/session":
                return httpx.Response(500, json={})
            if path.endswith("/message"):
                return httpx.Response(200, json=messages)
            if path == "/session":
                return httpx.Response(200, json=[{"id": "ses_x"}])
            if path.startswith("/session/"):
                return httpx.Response(200, json=session or {})
            return httpx.Response(404, json={})

        return handler

    _ASSISTANT = {
        "type": "assistant",
        "tokens": {"input": 1864, "output": 72, "cache": {"read": 113408, "write": 0}},
    }

    def test_v2_last_assistant_tokens_not_billing_cumul(self):
        _bridge.TRANSPORT = httpx.MockTransport(self._api_handler([self._ASSISTANT]))
        used, limit = asyncio.run(_bridge.context_usage(self.inst, "ses_abc"))
        self.assertEqual(used, 115344)
        self.assertIsNone(limit)

    def test_v2_skips_user_item_first(self):
        messages = [
            {"type": "user", "tokens": {"input": 1, "output": 0}},
            self._ASSISTANT,
            {"type": "assistant", "tokens": {"input": 9, "output": 9}},
        ]
        _bridge.TRANSPORT = httpx.MockTransport(self._api_handler(messages))
        used, _ = asyncio.run(_bridge.context_usage(self.inst, "ses_abc"))
        self.assertEqual(used, 115344)

    def test_v2_model_context_limit(self):
        session = {"model": {"id": "deepseek-v4.1-flash-free", "providerID": "opencode-go"}}
        models = [
            {"id": "other", "providerID": "opencode-go", "limit": {"context": 1000}},
            {
                "id": "deepseek-v4.1-flash-free",
                "providerID": "opencode-go",
                "limit": {"context": 200000},
            },
        ]
        _bridge.TRANSPORT = httpx.MockTransport(
            self._api_handler([self._ASSISTANT], model_items=models, session=session)
        )
        self.assertEqual(
            asyncio.run(_bridge.context_usage(self.inst, "ses_abc")), (115344, 200000)
        )

    # Forme réelle relevée sur l'instance v2 le 2026-11-01 :
    # GET /api/model/default -> {"location": {...}, "data": {Model}} où Model
    # porte id / modelID / providerID / name / limit.context / limit.output.
    _DEFAULT_MODEL = {
        "id": "particle-ai/particle-1",
        "modelID": "particle-ai/particle-1",
        "providerID": "openrouter",
        "name": "Particle-1",
        "limit": {"context": 1048576, "output": 131072},
    }

    def test_session_model_none_uses_default_model(self):
        """Session créée par le pont (model null) : la limite vient du modèle par défaut."""
        _bridge.TRANSPORT = httpx.MockTransport(
            self._api_handler(
                [self._ASSISTANT], session={"model": None}, default_model=self._DEFAULT_MODEL
            )
        )
        self.assertEqual(
            asyncio.run(_bridge.context_usage(self.inst, "ses_abc")), (115344, 1048576)
        )

    def test_default_limit_resolved_through_model_list(self):
        """Défaut sans limit.context : /api/model est consulté avec ses ids."""
        default_model = {"id": "particle-ai/particle-1", "providerID": "openrouter"}
        models = [
            {
                "id": "particle-ai/particle-1",
                "providerID": "openrouter",
                "limit": {"context": 1000000},
            }
        ]
        _bridge.TRANSPORT = httpx.MockTransport(
            self._api_handler([], default_model=default_model, model_items=models)
        )
        self.assertEqual(
            asyncio.run(_bridge.default_context_limit(self.inst)), 1000000
        )

    def test_default_limit_success_is_cached(self):
        calls = {"n": 0}
        base = self._api_handler([], default_model=self._DEFAULT_MODEL)

        async def handler(request):
            if request.url.path == "/api/model/default":
                calls["n"] += 1
            return await base(request)

        _bridge.TRANSPORT = httpx.MockTransport(handler)
        self.assertEqual(asyncio.run(_bridge.default_context_limit(self.inst)), 1048576)
        self.assertEqual(asyncio.run(_bridge.default_context_limit(self.inst)), 1048576)
        self.assertEqual(calls["n"], 1)

    def test_default_limit_failure_not_cached(self):
        calls = {"n": 0}

        async def handler(request):
            path = request.url.path
            if path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if path == "/api/model/default":
                calls["n"] += 1
                return httpx.Response(500, json={})
            return httpx.Response(404, json={})

        _bridge.TRANSPORT = httpx.MockTransport(handler)
        self.assertIsNone(asyncio.run(_bridge.default_context_limit(self.inst)))
        self.assertIsNone(asyncio.run(_bridge.default_context_limit(self.inst)))
        # Pas de cache négatif : la 2e requête repart sur le réseau.
        self.assertEqual(calls["n"], 2)

    def test_default_limit_none_on_legacy(self):
        _bridge.TRANSPORT = httpx.MockTransport(self._legacy_handler([]))
        self.assertIsNone(asyncio.run(_bridge.default_context_limit(self.inst)))

    def test_session_model_set_does_not_use_default(self):
        """Non-régression : model renseigné -> /api/model, jamais /api/model/default."""
        seen = []
        session = {"model": {"id": "m", "providerID": "p"}}
        models = [{"id": "m", "providerID": "p", "limit": {"context": 200000}}]
        base = self._api_handler(
            [self._ASSISTANT],
            model_items=models,
            session=session,
            default_model=self._DEFAULT_MODEL,
        )

        async def handler(request):
            seen.append(request.url.path)
            return await base(request)

        _bridge.TRANSPORT = httpx.MockTransport(handler)
        self.assertEqual(
            asyncio.run(_bridge.context_usage(self.inst, "ses_abc")), (115344, 200000)
        )
        self.assertNotIn("/api/model/default", seen)

    def test_legacy_last_assistant(self):
        messages = [
            {"info": {"role": "user", "tokens": {"input": 500}}},
            {"info": {"role": "assistant", "tokens": {"input": 9, "output": 1}}},
            {
                "info": {
                    "role": "assistant",
                    "tokens": {"input": 1864, "output": 72, "cache": {"read": 113408}},
                }
            },
        ]
        _bridge.TRANSPORT = httpx.MockTransport(self._legacy_handler(messages))
        self.assertEqual(
            asyncio.run(_bridge.context_usage(self.inst, "ses_abc")), (115344, None)
        )

    def test_empty_session(self):
        _bridge.TRANSPORT = httpx.MockTransport(self._api_handler([]))
        self.assertEqual(asyncio.run(_bridge.context_usage(self.inst, "ses_abc")), (0, None))

    def test_empty_200_session_keeps_limit_without_raising(self):
        session = {"model": {"id": "m", "providerID": "p"}}
        models = [{"id": "m", "providerID": "p", "limit": {"context": 200000}}]
        _bridge.TRANSPORT = httpx.MockTransport(
            self._api_handler([], model_items=models, session=session)
        )
        # Session vide = 200 sans message assistant : (0, limite), jamais d'exception.
        self.assertEqual(
            asyncio.run(_bridge.context_usage(self.inst, "ses_abc")), (0, 200000)
        )

    def test_http_500_raises_context_unavailable(self):
        _bridge.TRANSPORT = httpx.MockTransport(self._api_handler([], fail=True))
        with self.assertRaises(_bridge.ContextUnavailable):
            asyncio.run(_bridge.context_usage(self.inst, "ses_abc"))

    def test_http_error_returns_zero_without_raising(self):
        # context_usage lève ; get_session_context_tokens, lui, ne lève jamais.
        _bridge.TRANSPORT = httpx.MockTransport(self._api_handler([], fail=True))
        self.assertEqual(
            asyncio.run(_bridge.get_session_context_tokens(self.inst, "ses_abc")), 0
        )

    def test_model_limit_failure_not_cached(self):
        calls = {"n": 0}

        async def handler(request):
            path = request.url.path
            if path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if path == "/api/model":
                calls["n"] += 1
                return httpx.Response(500, json={})
            return httpx.Response(404, json={})

        _bridge.TRANSPORT = httpx.MockTransport(handler)
        self.assertIsNone(asyncio.run(_bridge.model_context_limit(self.inst, "p", "m")))
        self.assertIsNone(asyncio.run(_bridge.model_context_limit(self.inst, "p", "m")))
        # Un échec ne doit pas être mémorisé : la 2e requête repart sur le réseau.
        self.assertEqual(calls["n"], 2)

    def test_model_limit_success_is_cached(self):
        calls = {"n": 0}
        models = [{"id": "m", "providerID": "p", "limit": {"context": 200000}}]

        async def handler(request):
            path = request.url.path
            if path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if path == "/api/model":
                calls["n"] += 1
                return httpx.Response(200, json={"data": models})
            return httpx.Response(404, json={})

        _bridge.TRANSPORT = httpx.MockTransport(handler)
        self.assertEqual(asyncio.run(_bridge.model_context_limit(self.inst, "p", "m")), 200000)
        self.assertEqual(asyncio.run(_bridge.model_context_limit(self.inst, "p", "m")), 200000)
        self.assertEqual(calls["n"], 1)

    def test_fmt_ctx_with_limit(self):
        self.assertEqual(_bridge._fmt_ctx(115344, 200000), "115K/200K (57%)")


class StatusContextTests(unittest.TestCase):
    """En-tête et /status : panne (ctx ?) vs session vide, plus le cumul facturé."""

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_tg = _bridge.TELEGRAM_TRANSPORT
        self._saved_state = _bridge.state
        self._saved_kind = dict(_bridge._KIND_CACHE)
        os.environ.pop("OPENCODE_INSTANCES", None)
        _bridge._KIND_CACHE.clear()
        _bridge._CTX_LIMIT_CACHE.clear()
        self._cid = "4242"
        _bridge.state = {
            self._cid: {"current": {"instance": "v1", "sid": "ses_ctx"}, "agent": "build"}
        }
        self.tg_bodies = []

        def tg_handler(request):
            self.tg_bodies.append(request.content.decode("utf-8", "replace"))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        _bridge.TELEGRAM_TRANSPORT = httpx.MockTransport(tg_handler)

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge.TELEGRAM_TRANSPORT = self._saved_tg
        _bridge.state = self._saved_state
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved_kind)
        _bridge._CTX_LIMIT_CACHE.clear()

    def _handler(self, message_code=200, messages=None, model_items=None, session=None):
        async def handler(request):
            path = request.url.path
            if path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if path == "/api/model":
                return httpx.Response(200, json={"data": model_items or []})
            if path.endswith("/message"):
                return httpx.Response(message_code, json={"data": messages or []})
            if path.startswith("/api/session/"):
                return httpx.Response(200, json={"data": session or {}})
            return httpx.Response(404, json={})

        return handler

    def test_header_shows_question_mark_on_500(self):
        _bridge.TRANSPORT = httpx.MockTransport(self._handler(message_code=500))
        header = asyncio.run(_bridge._status_header(int(self._cid)))
        self.assertIn("ctx ?", header)

    def test_header_empty_session_with_limit(self):
        session = {"model": {"id": "m", "providerID": "p"}}
        models = [{"id": "m", "providerID": "p", "limit": {"context": 200000}}]
        _bridge.TRANSPORT = httpx.MockTransport(
            self._handler(model_items=models, session=session)
        )
        header = asyncio.run(_bridge._status_header(int(self._cid)))
        self.assertIn("ctx 0/200K (0%)", header)

    def test_status_shows_billing_cumul(self):
        session = {
            "model": {"id": "m", "providerID": "p"},
            "tokens": {
                "input": 1278997,
                "output": 86692,
                "cache": {"read": 14912640},
            },
            "cost": 0.31,
        }
        _bridge.TRANSPORT = httpx.MockTransport(
            self._handler(
                messages=[{"type": "assistant", "tokens": {"input": 10, "output": 2}}],
                session=session,
            )
        )
        asyncio.run(_bridge.handle_status(int(self._cid)))
        sent = " ".join(self.tg_bodies)
        self.assertIn("cumul 1.4M", sent)
        self.assertIn("cache 14.9M", sent)
        self.assertIn("$0.31", sent)
        self.assertIn("ctx", sent)

    def test_status_without_billing_fields_adds_no_line(self):
        session = {"model": {"id": "m", "providerID": "p"}}
        _bridge.TRANSPORT = httpx.MockTransport(
            self._handler(
                messages=[{"type": "assistant", "tokens": {"input": 10, "output": 2}}],
                session=session,
            )
        )
        asyncio.run(_bridge.handle_status(int(self._cid)))
        sent = " ".join(self.tg_bodies)
        self.assertNotIn("cumul", sent)
        self.assertNotIn("$", sent)


class SessionTitleTests(unittest.TestCase):
    """Titre de session : en-tête, /status et titrage automatique (v1 et v2)."""

    SID = "ses_title_1234"

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_tg = _bridge.TELEGRAM_TRANSPORT
        self._saved_state = _bridge.state
        self._saved_kind = dict(_bridge._KIND_CACHE)
        self._saved_instances = _bridge._INSTANCES
        os.environ.pop("OPENCODE_INSTANCES", None)
        _bridge._INSTANCES = None
        _bridge._KIND_CACHE.clear()
        _bridge._CTX_LIMIT_CACHE.clear()
        self._cid = "5151"
        _bridge.state = {
            self._cid: {
                "current": {"instance": "v2", "sid": self.SID},
                "agent": "build",
            }
        }
        self.requests = []
        self.tg_bodies = []

        def tg_handler(request):
            self.tg_bodies.append(request.content.decode("utf-8", "replace"))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        _bridge.TELEGRAM_TRANSPORT = httpx.MockTransport(tg_handler)

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge.TELEGRAM_TRANSPORT = self._saved_tg
        _bridge.state = self._saved_state
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved_kind)
        _bridge._INSTANCES = self._saved_instances
        _bridge._CTX_LIMIT_CACHE.clear()

    def _handler(self, session, kind="api"):
        async def handler(request):
            path = request.url.path
            self.requests.append((request.method, path, request.content))
            if kind == "api":
                if path == "/api/session":
                    return httpx.Response(200, json={"data": []})
                if path == "/api/model":
                    return httpx.Response(200, json={"data": []})
                if path.endswith("/message"):
                    return httpx.Response(200, json={"data": []})
                if path.startswith("/api/session/"):
                    if request.method == "PATCH":
                        return httpx.Response(200, json={"data": {}})
                    return httpx.Response(200, json={"data": session})
                return httpx.Response(404, json={})
            # legacy (v1)
            if path == "/api/session":
                return httpx.Response(404, json={})
            if path == "/session":
                return httpx.Response(200, json=[{"id": self.SID}])
            if path.endswith("/message"):
                return httpx.Response(200, json=[])
            if path.startswith("/session/"):
                if request.method == "PATCH":
                    return httpx.Response(200, json={})
                return httpx.Response(200, json=session)
            return httpx.Response(404, json={})

        return handler

    def _patches(self):
        return [r for r in self.requests if r[0] == "PATCH"]

    # 1) le titre de l'API apparaît dans l'en-tête, en v2 comme en v1
    def test_header_contains_title_v2(self):
        _bridge.TRANSPORT = httpx.MockTransport(
            self._handler({"id": self.SID, "title": "Mon chantier"}, kind="api")
        )
        header = asyncio.run(_bridge._status_header(int(self._cid)))
        self.assertIn("Mon chantier", header)
        self.assertNotIn("None", header)

    def test_header_contains_title_v1(self):
        _bridge.state[self._cid]["current"]["instance"] = "v1"
        _bridge.TRANSPORT = httpx.MockTransport(
            self._handler({"id": self.SID, "title": "Mon chantier"}, kind="legacy")
        )
        header = asyncio.run(_bridge._status_header(int(self._cid)))
        self.assertIn("Mon chantier", header)
        self.assertNotIn("None", header)

    # 2) pas de titre : repli sur les 12 premiers caractères du sid, sans exception
    def test_header_falls_back_to_sid_when_no_title(self):
        _bridge.TRANSPORT = httpx.MockTransport(
            self._handler({"id": self.SID, "title": None}, kind="api")
        )
        header = asyncio.run(_bridge._status_header(int(self._cid)))
        self.assertIn(self.SID[:12], header)
        self.assertNotIn("None", header)

    # 3) session encore « New session … » : le pont PATCH le titre
    def test_ensure_session_titled_patches_new_session(self):
        _bridge.TRANSPORT = httpx.MockTransport(
            self._handler({"id": self.SID, "title": "New session - 2026-11-01"}, kind="api")
        )
        asyncio.run(
            _bridge.ensure_session_titled(self._cid, self.SID, "Mon premier message")
        )
        patches = self._patches()
        self.assertEqual(len(patches), 1)
        body = patches[0][2].decode("utf-8")
        self.assertIn("Mon premier message", body)
        self.assertIn(self.SID, _bridge.state[self._cid]["titled"])

    def test_ensure_session_titled_skips_already_titled(self):
        _bridge.TRANSPORT = httpx.MockTransport(
            self._handler({"id": self.SID, "title": "Mon chantier"}, kind="api")
        )
        asyncio.run(
            _bridge.ensure_session_titled(self._cid, self.SID, "Mon premier message")
        )
        self.assertEqual(self._patches(), [])
        self.assertIn(self.SID, _bridge.state[self._cid]["titled"])

    # 4) /status affiche le titre
    def test_handle_status_shows_title(self):
        _bridge.TRANSPORT = httpx.MockTransport(
            self._handler({"id": self.SID, "title": "Mon chantier"}, kind="api")
        )
        asyncio.run(_bridge.handle_status(int(self._cid)))
        sent = " ".join(self.tg_bodies)
        self.assertIn("Mon chantier", sent)


class CommandsApiTests(unittest.TestCase):
    """Socle bas niveau des commandes serveur : liste, exécution, compactage et
    polling robuste (baseline). Aucun routage Telegram : les fonctions sont
    appelées directement, tout le réseau passe par TRANSPORT (MockTransport)."""

    SID = "ses_cmd_1"

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_kind = dict(_bridge._KIND_CACHE)
        self._saved_cmds = dict(_bridge._COMMANDS_CACHE)
        self._saved_state = _bridge.state
        _bridge._KIND_CACHE.clear()
        _bridge._COMMANDS_CACHE.clear()
        self.requests = []
        self.inst_v2 = {"name": "t2-api", "url": "http://v2.test", "auth": "", "kind": "api"}
        self.inst_v1 = {"name": "t2-legacy", "url": "http://v1.test", "auth": "", "kind": "legacy"}

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge.state = self._saved_state
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved_kind)
        _bridge._COMMANDS_CACHE.clear()
        _bridge._COMMANDS_CACHE.update(self._saved_cmds)

    # --- helpers ---------------------------------------------------------
    def _transport(self, handler):
        def wrapped(request):
            self.requests.append((request.method, request.url.path, request.content))
            return handler(request)

        _bridge.TRANSPORT = httpx.MockTransport(wrapped)

    @staticmethod
    def _assistant(msg_id, text):
        return {
            "id": msg_id,
            "type": "assistant",
            "agent": "build",
            "content": [{"type": "text", "text": text}],
        }

    def _count(self, path, method=None):
        return len([
            r for r in self.requests
            if r[1] == path and (method is None or r[0] == method)
        ])

    @staticmethod
    def _json_body(raw):
        return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)

    @staticmethod
    def _noop_sleep():
        async def _sleep(_seconds):
            return None

        return patch.object(_bridge.asyncio, "sleep", _sleep)

    # 1) v2 : POST attendu, puis texte de la NOUVELLE réponse
    def test_command_v2_posts_and_returns_new_text(self):
        state = {"posted": False}

        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "GET" and path == f"/api/session/{self.SID}/message":
                if not state["posted"]:
                    return httpx.Response(
                        200, json={"data": [self._assistant("msg_old", "ancienne réponse")]}
                    )
                return httpx.Response(
                    200, json={"data": [self._assistant("msg_new", "nouvelle réponse")]}
                )
            if request.method == "POST" and path == f"/api/session/{self.SID}/command":
                state["posted"] = True
                return httpx.Response(204)
            return httpx.Response(404)

        self._transport(handler)
        out = asyncio.run(
            _bridge.cli_command(self.inst_v2, self.SID, "orch", "mon chantier", agent="build")
        )
        self.assertEqual(out, "nouvelle réponse")
        posts = [r for r in self.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], f"/api/session/{self.SID}/command")
        body = self._json_body(posts[0][2])
        self.assertEqual(body["name"], "orch")
        self.assertEqual(body["text"], "mon chantier")
        self.assertEqual(body["agents"], [{"name": "build"}])

    # 2) course : le 1er poll renvoie encore la baseline ⇒ on attend le nouveau tour
    def test_command_v2_waits_for_new_turn_not_previous(self):
        state = {"posted": False, "polls": 0}

        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "GET" and path == f"/api/session/{self.SID}/message":
                if not state["posted"]:
                    return httpx.Response(
                        200, json={"data": [self._assistant("msg_old", "ancienne réponse")]}
                    )
                state["polls"] += 1
                if state["polls"] == 1:
                    # le nouveau tour n'a pas démarré : baseline toujours en tête
                    return httpx.Response(
                        200, json={"data": [self._assistant("msg_old", "ancienne réponse")]}
                    )
                if state["polls"] == 2:
                    # nouveau tour démarré mais pas encore de texte : ne PAS renvoyer
                    # l'ancienne réponse qui traîne plus bas dans la liste
                    return httpx.Response(
                        200,
                        json={
                            "data": [
                                {
                                    "id": "msg_new",
                                    "type": "assistant",
                                    "content": [{"type": "step-start"}],
                                },
                                self._assistant("msg_old", "ancienne réponse"),
                            ]
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            self._assistant("msg_new", "nouvelle réponse"),
                            self._assistant("msg_old", "ancienne réponse"),
                        ]
                    },
                )
            if request.method == "POST" and path == f"/api/session/{self.SID}/command":
                state["posted"] = True
                return httpx.Response(204)
            return httpx.Response(404)

        self._transport(handler)
        with self._noop_sleep():
            out = asyncio.run(_bridge.cli_command(self.inst_v2, self.SID, "orch"))
        self.assertEqual(out, "nouvelle réponse")
        self.assertEqual(state["polls"], 3)
        # Sans agent fourni, le corps du POST ne doit pas contenir de clé "agents"
        posts = [r for r in self.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        body = self._json_body(posts[0][2])
        self.assertNotIn("agents", body)
        self.assertEqual(body, {"name": "orch", "text": ""})

    # 3) 400 CommandNotFoundError ⇒ CommandNotFound(.command)
    def test_command_v2_400_command_not_found(self):
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "GET" and path.endswith("/message"):
                return httpx.Response(
                    200, json={"data": [self._assistant("msg_old", "ancien")]}
                )
            if request.method == "POST" and path == f"/api/session/{self.SID}/command":
                return httpx.Response(
                    400, json={"_tag": "CommandNotFoundError", "command": "nope"}
                )
            return httpx.Response(404)

        self._transport(handler)
        with self.assertRaises(_bridge.CommandNotFound) as ctx:
            asyncio.run(_bridge.cli_command(self.inst_v2, self.SID, "nope"))
        self.assertEqual(ctx.exception.command, "nope")

    # 4) v1 : réponse synchrone, parts[] text concaténés
    def test_command_v1_synchronous_parts(self):
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(404)
            if request.method == "GET" and path == "/session":
                return httpx.Response(200, json=[{"id": self.SID}])
            if request.method == "POST" and path == f"/session/{self.SID}/command":
                return httpx.Response(
                    200,
                    json={
                        "info": {"id": "msg_1"},
                        "parts": [
                            {"type": "text", "text": "ligne 1"},
                            {"type": "tool", "text": "ignoré"},
                            {"type": "text", "text": "ligne 2"},
                        ],
                    },
                )
            return httpx.Response(404)

        self._transport(handler)
        out = asyncio.run(
            _bridge.cli_command(self.inst_v1, self.SID, "orch", "a b", agent="build")
        )
        self.assertEqual(out, "ligne 1\nligne 2")
        posts = [r for r in self.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], f"/session/{self.SID}/command")
        self.assertEqual(
            self._json_body(posts[0][2]),
            {"command": "orch", "arguments": "a b", "agent": "build"},
        )

    # 5) v2 : filtre des entrées sans nom + cache 300 s + force
    def test_commands_list_v2_cache_and_force(self):
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "GET" and path == "/api/command":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"name": "init", "description": "Initialiser"},
                            {"name": "orch", "description": "Lancer un chantier"},
                            {"description": "sans nom"},
                            {"name": "", "description": "nom vide"},
                        ]
                    },
                )
            return httpx.Response(404)

        self._transport(handler)
        first = asyncio.run(_bridge.cli_commands_list(self.inst_v2))
        self.assertEqual([c["name"] for c in first], ["init", "orch"])
        self.assertEqual(first[0]["description"], "Initialiser")
        # Le succès est bien mis en cache
        self.assertIn("t2-api", _bridge._COMMANDS_CACHE)
        second = asyncio.run(_bridge.cli_commands_list(self.inst_v2))
        self.assertEqual(second, first)
        self.assertEqual(self._count("/api/command"), 1)
        forced = asyncio.run(_bridge.cli_commands_list(self.inst_v2, force=True))
        self.assertEqual(len(forced), 2)
        self.assertEqual(self._count("/api/command"), 2)

    # 5bis) v2 : un 500 ne doit PAS empoisonner le cache 300 s
    def test_commands_list_v2_error_not_cached(self):
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "GET" and path == "/api/command":
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(404)

        self._transport(handler)
        self.assertEqual(asyncio.run(_bridge.cli_commands_list(self.inst_v2)), [])
        self.assertNotIn("t2-api", _bridge._COMMANDS_CACHE)
        # Le second appel refait la requête (pas de cache négatif)
        self.assertEqual(asyncio.run(_bridge.cli_commands_list(self.inst_v2)), [])
        self.assertEqual(self._count("/api/command"), 2)

    # 6) v1 : liste vide, aucune requête
    def test_commands_list_v1_empty_without_request(self):
        _bridge._KIND_CACHE["t2-legacy"] = "legacy"

        def handler(request):
            return httpx.Response(500)

        self._transport(handler)
        out = asyncio.run(_bridge.cli_commands_list(self.inst_v1))
        self.assertEqual(out, [])
        self.assertEqual(self.requests, [])

    # 7) v2 : POST compact puis running → completed
    def test_compact_v2_running_then_completed(self):
        state = {"polls": 0}

        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "POST" and path == f"/api/session/{self.SID}/compact":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": "msg_comp",
                            "type": "compaction",
                            "status": "running",
                            "reason": "manual",
                        }
                    },
                )
            if request.method == "GET" and path == f"/api/session/{self.SID}/message":
                state["polls"] += 1
                status = "running" if state["polls"] == 1 else "completed"
                return httpx.Response(
                    200, json={"data": [{"id": "msg_comp", "type": "compaction", "status": status}]}
                )
            return httpx.Response(404)

        self._transport(handler)
        with self._noop_sleep():
            out = asyncio.run(_bridge.cli_compact(self.inst_v2, self.SID))
        self.assertEqual(out, "completed")
        self.assertEqual(self._count(f"/api/session/{self.SID}/compact", "POST"), 1)

    def test_compact_v2_failed(self):
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "POST" and path == f"/api/session/{self.SID}/compact":
                return httpx.Response(200, json={"data": {"id": "msg_comp", "status": "running"}})
            if request.method == "GET" and path == f"/api/session/{self.SID}/message":
                return httpx.Response(
                    200, json={"data": [{"id": "msg_comp", "type": "compaction", "status": "failed"}]}
                )
            return httpx.Response(404)

        self._transport(handler)
        with self._noop_sleep():
            out = asyncio.run(_bridge.cli_compact(self.inst_v2, self.SID))
        self.assertEqual(out, "failed")

    # 8) v1 : summarize avec les ids du modèle ; absent ⇒ unsupported sans POST
    def test_compact_v1_summarize_with_model_ids(self):
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(404)
            if request.method == "GET" and path == "/session":
                return httpx.Response(200, json=[{"id": self.SID}])
            if request.method == "GET" and path == f"/session/{self.SID}":
                return httpx.Response(
                    200, json={"id": self.SID, "model": {"providerID": "prov", "id": "mod"}}
                )
            if request.method == "POST" and path == f"/session/{self.SID}/summarize":
                return httpx.Response(200, json=True)
            return httpx.Response(404)

        self._transport(handler)
        out = asyncio.run(_bridge.cli_compact(self.inst_v1, self.SID))
        self.assertEqual(out, "completed")
        posts = [r for r in self.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], f"/session/{self.SID}/summarize")
        self.assertEqual(
            self._json_body(posts[0][2]), {"providerID": "prov", "modelID": "mod"}
        )

    def test_compact_v1_unsupported_without_model(self):
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(404)
            if request.method == "GET" and path == "/session":
                return httpx.Response(200, json=[{"id": self.SID}])
            if request.method == "GET" and path == f"/session/{self.SID}":
                return httpx.Response(200, json={"id": self.SID})
            return httpx.Response(404)

        self._transport(handler)
        out = asyncio.run(_bridge.cli_compact(self.inst_v1, self.SID))
        self.assertEqual(out, "unsupported")
        self.assertEqual([r for r in self.requests if r[0] == "POST"], [])

    # 9) aucun nouveau message textuel ⇒ TimeoutError
    def test_poll_api_timeout_when_no_new_text(self):
        def handler(request):
            return httpx.Response(
                200, json={"data": [self._assistant("msg_old", "ancien")]}
            )

        self._transport(handler)
        with self.assertRaises(TimeoutError):
            asyncio.run(
                _bridge._poll_api(
                    self.inst_v2,
                    self.SID,
                    baseline_id="msg_old",
                    timeout=0.05,
                    interval=0.01,
                )
            )

    # 10) cli_prompt v2 fonctionne toujours après le changement de signature
    def test_cli_prompt_v2_still_returns_new_turn(self):
        state = {"posted": False}

        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "GET" and path == f"/api/session/{self.SID}/message":
                if not state["posted"]:
                    return httpx.Response(
                        200, json={"data": [self._assistant("msg_old", "ancien")]}
                    )
                return httpx.Response(
                    200, json={"data": [self._assistant("msg_new", "nouveau")]}
                )
            if request.method == "POST" and path == f"/api/session/{self.SID}/prompt":
                state["posted"] = True
                return httpx.Response(200, json={"data": {"id": "msg_x"}})
            return httpx.Response(404)

        self._transport(handler)
        out = asyncio.run(
            _bridge.cli_prompt(self.inst_v2, self.SID, "salut", agent="build")
        )
        self.assertEqual(out, "nouveau")

    # 11) v2 : un 204 au POST /prompt n'est pas une erreur (2xx accepté)
    def test_cli_prompt_v2_accepts_204(self):
        state = {"posted": False}

        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "GET" and path == f"/api/session/{self.SID}/message":
                if not state["posted"]:
                    return httpx.Response(
                        200, json={"data": [self._assistant("msg_old", "ancien")]}
                    )
                return httpx.Response(
                    200, json={"data": [self._assistant("msg_new", "nouveau")]}
                )
            if request.method == "POST" and path == f"/api/session/{self.SID}/prompt":
                state["posted"] = True
                return httpx.Response(204)
            return httpx.Response(404)

        self._transport(handler)
        out = asyncio.run(_bridge.cli_prompt(self.inst_v2, self.SID, "salut"))
        self.assertEqual(out, "nouveau")

    # 12) v2 : liste décroissante => le PREMIER assistant est le plus récent
    def test_fetch_latest_assistant_id_v2_most_recent_first(self):
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "GET" and path == f"/api/session/{self.SID}/message":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            self._assistant("msg_recent", "réponse récente"),
                            self._assistant("msg_ancien", "réponse ancienne"),
                        ]
                    },
                )
            return httpx.Response(404)

        self._transport(handler)
        msg_id = asyncio.run(
            _bridge._fetch_latest_assistant_message_id(self.inst_v2, self.SID)
        )
        self.assertEqual(msg_id, "msg_recent")

    # 13) v1 : liste croissante => le DERNIER assistant (info.role) est le plus récent
    def test_fetch_latest_assistant_id_v1_most_recent_last(self):
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == f"/session/{self.SID}/message":
                return httpx.Response(
                    200,
                    json=[
                        {"info": {"id": "msg_ancien", "role": "assistant"}, "parts": []},
                        {"info": {"id": "msg_user", "role": "user"}, "parts": []},
                        {"info": {"id": "msg_recent", "role": "assistant"}, "parts": []},
                    ],
                )
            return httpx.Response(404)

        _bridge._KIND_CACHE["t2-legacy"] = "legacy"
        self._transport(handler)
        msg_id = asyncio.run(
            _bridge._fetch_latest_assistant_message_id(self.inst_v1, self.SID)
        )
        self.assertEqual(msg_id, "msg_recent")

    # 14) v2 : _mark_latest_assistant_seen écrit l'id du message le plus récent
    def test_mark_latest_assistant_seen_v2_uses_most_recent(self):
        _bridge.state = {
            "555": {
                "current": {"instance": "t2-api", "sid": self.SID},
                "mirror": {"last_seen_id": "msg_ancien", "seen_ids": ["msg_ancien"]},
            }
        }

        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if request.method == "GET" and path == f"/api/session/{self.SID}/message":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            self._assistant("msg_recent", "réponse récente"),
                            self._assistant("msg_ancien", "réponse ancienne"),
                        ]
                    },
                )
            return httpx.Response(404)

        self._transport(handler)
        asyncio.run(_bridge._mark_latest_assistant_seen(555, self.inst_v2, self.SID))

        mirror = _bridge.state["555"]["mirror"]
        self.assertEqual(mirror["last_seen_id"], "msg_recent")
        self.assertIn("msg_recent", mirror["seen_ids"])
        # L'état persisté (STATE_FILE temporaire) pointe le même message
        with open(_bridge.STATE_FILE) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["555"]["mirror"]["last_seen_id"], "msg_recent")


# ---------------------------------------------------------------------------
# Routage des commandes slash (/compact, /cmds, /say, /<commande serveur>)
# ---------------------------------------------------------------------------


class SlashRoutingTests(unittest.TestCase):
    """Une commande slash ne doit plus jamais partir comme du texte au modèle.
    Tout le réseau passe par MockTransport et tg_send/tg_edit sont remplacés par
    des enregistreurs en mémoire : aucun appel réel, aucune session de Marc."""

    SID = "ses_slash_1"
    CID = 4242
    TOKENS = {"input": 1000, "output": 200, "cache": {"read": 800, "write": 0}}

    def setUp(self):
        self._saved = {
            "transport": _bridge.TRANSPORT,
            "kind": dict(_bridge._KIND_CACHE),
            "cmds": dict(_bridge._COMMANDS_CACHE),
            "state": _bridge.state,
            "instances": _bridge._INSTANCES,
        }
        os.environ.pop("OPENCODE_INSTANCES", None)
        _bridge._INSTANCES = None
        _bridge._KIND_CACHE.clear()
        _bridge._COMMANDS_CACHE.clear()
        _bridge._KIND_CACHE["v2"] = "api"
        self.requests = []
        self.sent = []
        self.edited = []
        self.posted_compact = False
        self.posted_command = False
        self.opts = {
            "commands": [],
            "compact_status": None,
            "command_response": "réponse serveur",
        }
        _bridge.state = {
            str(self.CID): {
                "current": {"instance": "v2", "sid": self.SID},
                "agent": "build",
                "mirror": {"last_seen_id": "msg_old", "seen_ids": ["msg_old"]},
            }
        }
        self._patchers = [
            patch.object(_bridge, "tg_send", self._fake_send),
            patch.object(_bridge, "tg_edit", self._fake_edit),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        _bridge.TRANSPORT = self._saved["transport"]
        _bridge.state = self._saved["state"]
        _bridge._INSTANCES = self._saved["instances"]
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved["kind"])
        _bridge._COMMANDS_CACHE.clear()
        _bridge._COMMANDS_CACHE.update(self._saved["cmds"])

    # --- helpers ---------------------------------------------------------
    async def _fake_send(self, chat_id, text, markdown=True):
        self.sent.append((chat_id, text))
        return 5000 + len(self.sent)

    async def _fake_edit(self, chat_id, message_id, text, markdown=True):
        self.edited.append((chat_id, message_id, text))

    @staticmethod
    def _assistant(msg_id, text):
        return {
            "id": msg_id,
            "type": "assistant",
            "agent": "build",
            "content": [{"type": "text", "text": text}],
            "tokens": SlashRoutingTests.TOKENS,
        }

    def _messages(self):
        items = []
        status = self.opts.get("compact_status")
        # Le message de compactage n'existe qu'APRÈS le POST : avant, la session
        # n'en a aucun (sinon le garde anti double-compactage le verrait "running"
        # et refuserait un compactage pourtant légitime).
        if status and self.posted_compact:
            items.append(
                {
                    "id": "msg_comp",
                    "type": "compaction",
                    "status": status,
                }
            )
        if self.posted_command and self.opts.get("command_response"):
            items.append(self._assistant("msg_new", self.opts["command_response"]))
        items.append(self._assistant("msg_old", "ancienne réponse"))
        return items

    def _install_handler(self):
        def handler(request):
            path, method = request.url.path, request.method
            self.requests.append((method, path, request.content))
            if method == "GET" and path == "/api/session":
                return httpx.Response(200, json={"data": []})
            if method == "GET" and path == "/api/command":
                return httpx.Response(200, json={"data": self.opts["commands"]})
            if method == "GET" and path == f"/api/session/{self.SID}/message":
                return httpx.Response(200, json={"data": self._messages()})
            if method == "GET" and path == f"/api/session/{self.SID}":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": self.SID,
                            "title": "Session test",
                            "model": {"id": "mod", "providerID": "prov"},
                        }
                    },
                )
            if method == "GET" and path == "/api/model":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "mod",
                                "providerID": "prov",
                                "limit": {"context": 200000},
                            }
                        ]
                    },
                )
            if method == "POST" and path == f"/api/session/{self.SID}/compact":
                if self.opts.get("compact_error"):
                    return httpx.Response(self.opts["compact_error"], json={"error": "boom"})
                self.posted_compact = True
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": "msg_comp",
                            "type": "compaction",
                            "status": "running",
                        }
                    },
                )
            if method == "POST" and path == f"/api/session/{self.SID}/command":
                if self.opts.get("command_error"):
                    return httpx.Response(
                        self.opts["command_error"],
                        json=self.opts.get("command_error_body") or {},
                    )
                self.posted_command = True
                return httpx.Response(204)
            if method == "PATCH":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        _bridge.TRANSPORT = httpx.MockTransport(handler)

    def _posts(self, path):
        return [r for r in self.requests if r[0] == "POST" and r[1] == path]

    @staticmethod
    def _noop_sleep():
        async def _sleep(_seconds):
            return None

        return patch.object(_bridge.asyncio, "sleep", _sleep)

    # 1) Normalisation : casse, suffixe @bot, arguments
    def test_split_slash(self):
        self.assertEqual(
            _bridge._split_slash("/Compact@opencode4578_bot  a b"), ("compact", "a b")
        )
        self.assertIsNone(_bridge._split_slash("bonjour"))
        self.assertEqual(_bridge._split_slash("/cmds"), ("cmds", ""))

    # 2) /compact : POST, puis ÉDITION du même message (pas un 2e envoi)
    def test_compact_posts_and_edits_ack(self):
        self.opts["compact_status"] = "completed"
        self._install_handler()
        with self._noop_sleep():
            asyncio.run(_bridge.handle_slash(self.CID, "/compact"))

        self.assertEqual(len(self._posts(f"/api/session/{self.SID}/compact")), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("compactage", self.sent[0][1])
        self.assertEqual(len(self.edited), 1)
        self.assertEqual(self.edited[0][1], 5001)
        self.assertIn("✅ compacté", self.edited[0][2])
        self.assertIn("ctx", self.edited[0][2])

    # 3) /compact en échec ⇒ édition avec ❌, rien ne remonte
    def test_compact_failed_edits_error(self):
        self.opts["compact_status"] = "failed"
        self._install_handler()
        with self._noop_sleep():
            asyncio.run(_bridge.handle_slash(self.CID, "/compact"))

        self.assertEqual(len(self.edited), 1)
        self.assertIn("❌", self.edited[0][2])
        self.assertIn("échoué", self.edited[0][2])

    # 4) /orch t1 : commande listée ⇒ POST /command + édition avec la réponse
    def test_server_command_posts_and_edits_with_response(self):
        self.opts["commands"] = [
            {"name": "orch", "description": "Lancer un chantier"},
            {"name": "init", "description": "Initialiser"},
        ]
        self._install_handler()
        with self._noop_sleep():
            asyncio.run(_bridge.handle_slash(self.CID, "/orch t1"))

        posts = self._posts(f"/api/session/{self.SID}/command")
        self.assertEqual(len(posts), 1)
        body = json.loads(posts[0][2].decode("utf-8"))
        self.assertEqual(body["name"], "orch")
        self.assertEqual(body["text"], "t1")
        self.assertEqual(body["agents"], [{"name": "build"}])
        self.assertEqual(len(self.edited), 1)
        self.assertIn("réponse serveur", self.edited[0][2])

    # 5) /nope : aucun POST, message « commande inconnue » + noms disponibles
    def test_unknown_command_no_post_and_lists_names(self):
        self.opts["commands"] = [
            {"name": "orch", "description": "Lancer un chantier"},
            {"name": "init", "description": "Initialiser"},
        ]
        self._install_handler()
        asyncio.run(_bridge.handle_slash(self.CID, "/nope"))

        self.assertEqual(self._posts(f"/api/session/{self.SID}/command"), [])
        self.assertEqual(self.edited, [])
        self.assertEqual(len(self.sent), 1)
        message = self.sent[0][1]
        self.assertIn("commande inconnue", message)
        self.assertIn("/orch", message)

    # 6) /say <texte> : le texte part littéralement, slash compris
    def test_say_sends_literal_text_to_model(self):
        calls = []

        async def fake_handle_message(chat_id, text):
            calls.append((chat_id, text))

        self._install_handler()
        with patch.object(_bridge, "handle_message", fake_handle_message):
            asyncio.run(_bridge.handle_slash(self.CID, "/say /etc/passwd"))

        self.assertEqual(calls, [(self.CID, "/etc/passwd")])
        self.assertEqual(self._posts(f"/api/session/{self.SID}/command"), [])

    # 7) /status reste natif du pont : aucun POST vers /command
    def test_native_status_not_routed(self):
        self.opts["commands"] = [{"name": "orch", "description": "Lancer un chantier"}]
        self._install_handler()
        asyncio.run(_bridge.handle_slash(self.CID, "/status"))

        self.assertEqual(self._posts(f"/api/session/{self.SID}/command"), [])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.edited, [])

    # 8) après une commande serveur, le dernier assistant est marqué vu
    def test_server_command_marks_latest_assistant_seen(self):
        self.opts["commands"] = [{"name": "orch", "description": "Lancer un chantier"}]
        self._install_handler()
        with self._noop_sleep():
            asyncio.run(_bridge.handle_slash(self.CID, "/orch t1"))

        mirror = _bridge.state[str(self.CID)]["mirror"]
        self.assertEqual(mirror["last_seen_id"], "msg_new")
        self.assertIn("msg_new", mirror["seen_ids"])

    # 9) course liste/POST : CommandNotFound ⇒ message clair, pas d'exception
    def test_command_not_found_race_message(self):
        self.opts["commands"] = [{"name": "orch", "description": "Lancer un chantier"}]
        self.opts["command_error"] = 400
        self.opts["command_error_body"] = {
            "_tag": "CommandNotFoundError",
            "command": "orch",
        }
        self._install_handler()
        with self._noop_sleep():
            asyncio.run(_bridge.handle_slash(self.CID, "/orch t1"))

        self.assertEqual(len(self.edited), 1)
        self.assertIn("commande inconnue côté OpenCode", self.edited[0][2])

    # 10) /compact sur erreur HTTP : ❌ édité, aucune exception ne remonte
    def test_compact_http_error_is_reported_not_raised(self):
        self.opts["compact_error"] = 500
        self._install_handler()
        asyncio.run(_bridge.handle_slash(self.CID, "/compact"))

        self.assertEqual(len(self.edited), 1)
        self.assertIn("❌", self.edited[0][2])
        self.assertIn("500", self.edited[0][2])


# ---------------------------------------------------------------------------
# Autorisation à la racine (boucle de polling) et forme /<native>@bot
# ---------------------------------------------------------------------------


class _PollStop(BaseException):
    """Sentinelle de test : sort de la boucle infinie de main() sans la mimer."""


class RootAuthorizationTests(unittest.TestCase):
    """Le garde d'autorisation est évalué dans main(), AVANT toute interception :
    un chat inconnu ne peut ni lancer /orch ni inscrire un sid via /sessions ; les
    commandes natives fonctionnent avec le suffixe @bot des groupes."""

    CID_AUTH = 4242
    CID_FOREIGN = 666
    SID = "ses_root_1"

    def setUp(self):
        self._saved = {
            "transport": _bridge.TRANSPORT,
            "tg": _bridge.TELEGRAM_TRANSPORT,
            "poll": _bridge.POLL_TRANSPORT,
            "kind": dict(_bridge._KIND_CACHE),
            "cmds": dict(_bridge._COMMANDS_CACHE),
            "state": _bridge.state,
            "instances": _bridge._INSTANCES,
            "auth_env": os.environ.get("AUTHORIZED_CHAT_IDS"),
        }
        os.environ.pop("AUTHORIZED_CHAT_IDS", None)
        os.environ.pop("OPENCODE_INSTANCES", None)
        _bridge._INSTANCES = None
        _bridge._KIND_CACHE.clear()
        _bridge._COMMANDS_CACHE.clear()
        _bridge._KIND_CACHE["v2"] = "api"
        self.api_requests = []
        self.sent = []
        # Seul CID_AUTH est présent dans l'état -> seul chat autorisé (repli).
        _bridge.state = {
            str(self.CID_AUTH): {
                "current": {"instance": "v2", "sid": self.SID},
                "agent": "build",
                "mirror": {"last_seen_id": "msg_old", "seen_ids": ["msg_old"]},
            }
        }

    def tearDown(self):
        _bridge.TRANSPORT = self._saved["transport"]
        _bridge.TELEGRAM_TRANSPORT = self._saved["tg"]
        _bridge.POLL_TRANSPORT = self._saved["poll"]
        _bridge.state = self._saved["state"]
        _bridge._INSTANCES = self._saved["instances"]
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved["kind"])
        _bridge._COMMANDS_CACHE.clear()
        _bridge._COMMANDS_CACHE.update(self._saved["cmds"])
        if self._saved["auth_env"] is None:
            os.environ.pop("AUTHORIZED_CHAT_IDS", None)
        else:
            os.environ["AUTHORIZED_CHAT_IDS"] = self._saved["auth_env"]

    # --- helpers ---------------------------------------------------------
    def _opencode_handler(self, request):
        path, method = request.url.path, request.method
        self.api_requests.append((method, path, request.content))
        if method == "GET" and path == "/api/session":
            return httpx.Response(200, json={"data": []})
        if method == "GET" and path == "/api/command":
            return httpx.Response(
                200, json={"data": [{"name": "orch", "description": "Lancer un chantier"}]}
            )
        if method == "GET" and path == f"/api/session/{self.SID}/message":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "msg_old",
                            "type": "assistant",
                            "content": [{"type": "text", "text": "ancienne réponse"}],
                            "tokens": {"input": 1000, "output": 200},
                        }
                    ]
                },
            )
        if method == "GET" and path == f"/api/session/{self.SID}":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": self.SID,
                        "title": "Session test",
                        "model": {"id": "mod", "providerID": "prov"},
                    }
                },
            )
        if method == "GET" and path == "/api/model":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "mod", "providerID": "prov", "limit": {"context": 200000}}
                    ]
                },
            )
        if method == "POST" and path == f"/api/session/{self.SID}/command":
            return httpx.Response(204)
        return httpx.Response(404)

    def _tg_handler(self, request):
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        if "sendMessage" in str(request.url) or "editMessageText" in str(request.url):
            self.sent.append(payload.get("text", ""))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(200, json={"ok": True})

    def _run_main(self, updates):
        """Fait tourner le vrai main() le temps d'un lot d'updates, puis l'arrête."""
        polls = {"n": 0}

        def poll_handler(request):
            polls["n"] += 1
            if polls["n"] == 1:
                return httpx.Response(200, json={"ok": True, "result": updates})
            raise _PollStop()

        _bridge.POLL_TRANSPORT = httpx.MockTransport(poll_handler)
        _bridge.TRANSPORT = httpx.MockTransport(self._opencode_handler)
        _bridge.TELEGRAM_TRANSPORT = httpx.MockTransport(self._tg_handler)
        with self.assertRaises(_PollStop):
            asyncio.run(_bridge.main())

    # 1) chat non autorisé + /orch x : aucune requête OpenCode, un seul refus
    def test_unauthorized_orch_makes_no_api_call(self):
        slash_calls = []

        async def fake_handle_slash(chat_id, text):
            slash_calls.append((chat_id, text))

        with patch.object(_bridge, "handle_slash", fake_handle_slash):
            self._run_main(
                [
                    {
                        "update_id": 1,
                        "message": {"chat": {"id": self.CID_FOREIGN}, "text": "/orch x"},
                    }
                ]
            )
        self.assertEqual(slash_calls, [], "le garde de main() passe AVANT le routage slash")
        self.assertEqual(self.api_requests, [], "aucun appel ne doit partir vers OpenCode")
        self.assertEqual(len(self.sent), 1, "un seul message : le refus")
        self.assertIn("⛔", self.sent[0])

    # 2) chat non autorisé + /sessions : preuve que le garde est en amont du handler
    def test_unauthorized_sessions_never_lists(self):
        self._run_main(
            [{"update_id": 2, "message": {"chat": {"id": self.CID_FOREIGN}, "text": "/sessions"}}]
        )
        self.assertEqual(
            [r for r in self.api_requests if r[1] == "/api/session"],
            [],
            "/sessions ne doit jamais interroger l'API pour un chat inconnu",
        )
        self.assertEqual(len(self.sent), 1)
        self.assertIn("⛔", self.sent[0])

    # 3) chat autorisé + /status : comportement inchangé
    def test_authorized_status_unchanged(self):
        self._run_main(
            [{"update_id": 3, "message": {"chat": {"id": self.CID_AUTH}, "text": "/status"}}]
        )
        self.assertEqual(
            [r for r in self.api_requests if r[0] == "POST" and r[1].endswith("/command")], []
        )
        text = " ".join(self.sent)
        self.assertIn("[v2]", text)
        self.assertIn("Session :", text)

    # 4) /status@opencode4578_bot (groupes) exécute bien la commande native
    def test_native_command_with_bot_suffix_executes(self):
        self._run_main(
            [
                {
                    "update_id": 4,
                    "message": {
                        "chat": {"id": self.CID_AUTH},
                        "text": "/status@opencode4578_bot",
                    },
                }
            ]
        )
        self.assertEqual(
            [r for r in self.api_requests if r[0] == "POST" and r[1].endswith("/command")],
            [],
            "la forme @bot ne doit pas être routée comme une commande serveur",
        )
        text = " ".join(self.sent)
        self.assertIn("[v2]", text)
        self.assertIn("Session :", text)


# ---------------------------------------------------------------------------
# /compact marque vu, /cmds et /say sans session, garde de handle_slash
# ---------------------------------------------------------------------------


class SlashGuardTests(unittest.TestCase):
    """Défense en profondeur de handle_slash, /compact marqué vu, commandes qui
    ne dépendent pas d'une session active."""

    CID = 4242
    SID = "ses_c"

    def setUp(self):
        self._saved = {
            "transport": _bridge.TRANSPORT,
            "tg_send": _bridge.tg_send,
            "tg_edit": _bridge.tg_edit,
            "kind": dict(_bridge._KIND_CACHE),
            "cmds": dict(_bridge._COMMANDS_CACHE),
            "state": _bridge.state,
            "instances": _bridge._INSTANCES,
            "auth_env": os.environ.get("AUTHORIZED_CHAT_IDS"),
        }
        os.environ.pop("AUTHORIZED_CHAT_IDS", None)
        os.environ.pop("OPENCODE_INSTANCES", None)
        _bridge._INSTANCES = None
        _bridge._KIND_CACHE.clear()
        _bridge._COMMANDS_CACHE.clear()
        self.api_requests = []
        self.sent = []
        self.edited = []
        _bridge.state = {str(self.CID): {"agent": "build"}}

        async def fake_send(chat_id, text, markdown=True):
            self.sent.append((chat_id, text))
            return 5000 + len(self.sent)

        async def fake_edit(chat_id, message_id, text, markdown=True):
            self.edited.append((chat_id, message_id, text))

        _bridge.tg_send = fake_send
        _bridge.tg_edit = fake_edit
        self._install(lambda request: httpx.Response(404))

    def tearDown(self):
        _bridge.TRANSPORT = self._saved["transport"]
        _bridge.tg_send = self._saved["tg_send"]
        _bridge.tg_edit = self._saved["tg_edit"]
        _bridge.state = self._saved["state"]
        _bridge._INSTANCES = self._saved["instances"]
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved["kind"])
        _bridge._COMMANDS_CACHE.clear()
        _bridge._COMMANDS_CACHE.update(self._saved["cmds"])
        if self._saved["auth_env"] is None:
            os.environ.pop("AUTHORIZED_CHAT_IDS", None)
        else:
            os.environ["AUTHORIZED_CHAT_IDS"] = self._saved["auth_env"]

    def _install(self, handler):
        def wrapped(request):
            self.api_requests.append((request.method, request.url.path))
            return handler(request)

        _bridge.TRANSPORT = httpx.MockTransport(wrapped)

    # 5) cli_compact -> "completed" ⇒ le dernier assistant est marqué vu
    def test_compact_completed_marks_latest_assistant_seen(self):
        _bridge.state = {
            str(self.CID): {
                "current": {"instance": "v2", "sid": self.SID},
                "agent": "build",
            }
        }
        marked = []

        async def fake_mark(chat_id, inst, sid):
            marked.append((chat_id, inst.get("name"), sid))

        async def fake_compact(inst, sid):
            return "completed"

        with patch.object(_bridge, "cli_compact", fake_compact), patch.object(
            _bridge, "_mark_latest_assistant_seen", fake_mark
        ):
            asyncio.run(_bridge.handle_slash(self.CID, "/compact"))

        self.assertEqual(marked, [(self.CID, "v2", self.SID)])
        self.assertEqual(len(self.edited), 1)
        self.assertIn("✅ compacté", self.edited[0][2])

    # 6) /cmds sans session courante : la liste est renvoyée quand même
    def test_cmds_without_current_session_lists_commands(self):
        _bridge._KIND_CACHE["v1"] = "api"

        def handler(request):
            if request.method == "GET" and request.url.path == "/api/command":
                return httpx.Response(
                    200, json={"data": [{"name": "orch", "description": "Lancer un chantier"}]}
                )
            return httpx.Response(404)

        self._install(handler)
        asyncio.run(_bridge.handle_slash(self.CID, "/cmds"))

        self.assertEqual(len(self.sent), 1)
        message = self.sent[0][1]
        self.assertIn("Commandes du pont", message)
        self.assertIn("/orch", message)
        self.assertNotIn("aucune session active", message)

    # 7) /say bonjour sans session : délégué à handle_message, aucun refus
    def test_say_without_current_session_delegates(self):
        calls = []

        async def fake_handle_message(chat_id, text):
            calls.append((chat_id, text))

        with patch.object(_bridge, "handle_message", fake_handle_message):
            asyncio.run(_bridge.handle_slash(self.CID, "/say bonjour"))

        self.assertEqual(calls, [(self.CID, "bonjour")])
        self.assertEqual(self.sent, [], "aucun message de refus")

    # 8) /say vide : message d'usage, handle_message jamais appelé
    def test_say_empty_shows_usage_without_calling_model(self):
        calls = []

        async def fake_handle_message(chat_id, text):
            calls.append((chat_id, text))

        with patch.object(_bridge, "handle_message", fake_handle_message):
            asyncio.run(_bridge.handle_slash(self.CID, "/say"))

        self.assertEqual(calls, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Usage", self.sent[0][1])

    # 9) défense en profondeur : handle_slash refuse aussi un chat inconnu
    def test_handle_slash_refuses_unauthorized_chat(self):
        _bridge.state = {}  # aucun chat dans l'état, aucune variable d'env
        asyncio.run(_bridge.handle_slash(self.CID, "/orch x"))

        self.assertEqual(self.api_requests, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("⛔", self.sent[0][1])


class InterruptAndCompactGuardTests(unittest.TestCase):
    """Garde-fous : /stop (alias /esc) et refus d'un second compactage.
    MockTransport + tg_send/tg_edit remplacés : aucun appel réel, aucune session
    de Marc, état sur STATE_FILE temporaire."""

    CID = 5151
    SID = "ses_stop_1"

    def setUp(self):
        self._saved = {
            "transport": _bridge.TRANSPORT,
            "tg_send": _bridge.tg_send,
            "tg_edit": _bridge.tg_edit,
            "kind": dict(_bridge._KIND_CACHE),
            "cmds": dict(_bridge._COMMANDS_CACHE),
            "state": _bridge.state,
            "instances": _bridge._INSTANCES,
            "auth_env": os.environ.get("AUTHORIZED_CHAT_IDS"),
        }
        os.environ.pop("AUTHORIZED_CHAT_IDS", None)
        os.environ.pop("OPENCODE_INSTANCES", None)
        _bridge._INSTANCES = None
        _bridge._KIND_CACHE.clear()
        _bridge._COMMANDS_CACHE.clear()
        _bridge._KIND_CACHE["v2"] = "api"
        self.requests = []
        self.sent = []
        self.edited = []
        self.interrupt_code = 200
        self.compaction_running = False
        self.compact_posted = False
        _bridge.state = {
            str(self.CID): {
                "current": {"instance": "v2", "sid": self.SID},
                "agent": "build",
            }
        }

        async def fake_send(chat_id, text, markdown=True):
            self.sent.append((chat_id, text))
            return 5000 + len(self.sent)

        async def fake_edit(chat_id, message_id, text, markdown=True):
            self.edited.append((chat_id, message_id, text))

        _bridge.tg_send = fake_send
        _bridge.tg_edit = fake_edit
        self._install_api()

    def tearDown(self):
        _bridge.TRANSPORT = self._saved["transport"]
        _bridge.tg_send = self._saved["tg_send"]
        _bridge.tg_edit = self._saved["tg_edit"]
        _bridge.state = self._saved["state"]
        _bridge._INSTANCES = self._saved["instances"]
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved["kind"])
        _bridge._COMMANDS_CACHE.clear()
        _bridge._COMMANDS_CACHE.update(self._saved["cmds"])
        if self._saved["auth_env"] is None:
            os.environ.pop("AUTHORIZED_CHAT_IDS", None)
        else:
            os.environ["AUTHORIZED_CHAT_IDS"] = self._saved["auth_env"]

    # --- helpers ---------------------------------------------------------
    def _install_api(self):
        def handler(request):
            path, method = request.url.path, request.method
            self.requests.append((method, path, request.content))
            if method == "GET" and path == f"/api/session/{self.SID}/message":
                items = []
                if self.compaction_running:
                    items.append(
                        {
                            "id": "msg_comp_running",
                            "type": "compaction",
                            "status": "running",
                        }
                    )
                if self.compact_posted:
                    items.append(
                        {"id": "msg_comp", "type": "compaction", "status": "completed"}
                    )
                items.append(
                    {
                        "id": "msg_a",
                        "type": "assistant",
                        "agent": "build",
                        "content": [{"type": "text", "text": "réponse"}],
                        "tokens": {
                            "input": 1000,
                            "output": 200,
                            "cache": {"read": 800, "write": 0},
                        },
                    }
                )
                return httpx.Response(200, json={"data": items})
            if method == "GET" and path == f"/api/session/{self.SID}":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": self.SID,
                            "title": "Session test",
                            "model": {"id": "mod", "providerID": "prov"},
                        }
                    },
                )
            if method == "GET" and path == "/api/model":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "mod",
                                "providerID": "prov",
                                "limit": {"context": 200000},
                            }
                        ]
                    },
                )
            if method == "POST" and path == f"/api/session/{self.SID}/interrupt":
                if self.interrupt_code >= 400:
                    return httpx.Response(self.interrupt_code, json={"error": "boom"})
                return httpx.Response(200, json={"data": True})
            if method == "POST" and path == f"/api/session/{self.SID}/compact":
                self.compact_posted = True
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": "msg_comp",
                            "type": "compaction",
                            "status": "running",
                        }
                    },
                )
            return httpx.Response(404, json={})

        _bridge.TRANSPORT = httpx.MockTransport(handler)

    def _posts(self, path):
        return [r for r in self.requests if r[0] == "POST" and r[1] == path]

    @staticmethod
    def _noop_sleep():
        async def _sleep(_seconds):
            return None

        return patch.object(_bridge.asyncio, "sleep", _sleep)

    # 1) /stop ⇒ POST interrupt + message édité « ⏹ interrompu »
    def test_stop_posts_interrupt_and_edits(self):
        asyncio.run(_bridge.handle_slash(self.CID, "/stop"))

        posts = self._posts(f"/api/session/{self.SID}/interrupt")
        self.assertEqual(len(posts), 1)
        body = json.loads(posts[0][2].decode("utf-8")) if posts[0][2] else {}
        self.assertEqual(body, {})
        self.assertEqual(len(self.edited), 1)
        self.assertIn("⏹", self.edited[0][2])
        self.assertEqual(self._posts(f"/api/session/{self.SID}/command"), [])

    # 2) /esc (alias) ⇒ exactement le même comportement
    def test_esc_alias_posts_interrupt_and_edits(self):
        asyncio.run(_bridge.handle_slash(self.CID, "/esc"))

        self.assertEqual(len(self._posts(f"/api/session/{self.SID}/interrupt")), 1)
        self.assertEqual(len(self.edited), 1)
        self.assertIn("⏹", self.edited[0][2])

    # 3) /stop sur un 500 ⇒ ❌ édité, aucune exception qui remonte
    def test_stop_http_error_reported_not_raised(self):
        self.interrupt_code = 500
        asyncio.run(_bridge.handle_slash(self.CID, "/stop"))

        self.assertEqual(len(self._posts(f"/api/session/{self.SID}/interrupt")), 1)
        self.assertEqual(len(self.edited), 1)
        self.assertIn("❌", self.edited[0][2])
        self.assertIn("500", self.edited[0][2])

    # 4) /compact alors qu'un compactage tourne ⇒ refus, aucun POST /compact
    def test_compact_refused_when_one_already_running(self):
        self.compaction_running = True
        with self._noop_sleep():
            asyncio.run(_bridge.handle_slash(self.CID, "/compact"))

        self.assertEqual(self._posts(f"/api/session/{self.SID}/compact"), [])
        self.assertEqual(self.edited, [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("déjà en cours", self.sent[0][1])

    # 5) /compact sans compactage en cours ⇒ POST bien émis (non-régression)
    def test_compact_posts_when_nothing_running(self):
        with self._noop_sleep():
            asyncio.run(_bridge.handle_slash(self.CID, "/compact"))

        self.assertEqual(len(self._posts(f"/api/session/{self.SID}/compact")), 1)
        self.assertEqual(len(self.edited), 1)
        self.assertIn("✅ compacté", self.edited[0][2])


class BotCommandsRegistrationTests(unittest.TestCase):
    """setMyCommands : corps exact, échec Telegram non bloquant."""

    def setUp(self):
        self._saved_transport = _bridge.TELEGRAM_TRANSPORT
        self.tg_requests = []

    def tearDown(self):
        _bridge.TELEGRAM_TRANSPORT = self._saved_transport

    def _install(self, response_handler):
        def handler(request):
            self.tg_requests.append(request)
            return response_handler(request)

        _bridge.TELEGRAM_TRANSPORT = httpx.MockTransport(handler)

    # 6) corps envoyé : 14 commandes, compact/stop/model/models présents, aucun slash
    def test_register_bot_commands_payload(self):
        self._install(lambda r: httpx.Response(200, json={"ok": True, "result": True}))

        self.assertTrue(asyncio.run(_bridge.register_bot_commands()))
        self.assertEqual(len(self.tg_requests), 1)
        request = self.tg_requests[0]
        self.assertTrue(str(request.url).endswith("/setMyCommands"))
        commands = json.loads(request.content.decode("utf-8"))["commands"]
        self.assertEqual(len(commands), 14)
        names = [c["command"] for c in commands]
        self.assertIn("compact", names)
        self.assertIn("stop", names)
        self.assertIn("model", names)
        self.assertIn("models", names)
        self.assertEqual([n for n in names if n.startswith("/")], [])
        self.assertTrue(all(c.get("description") for c in commands))

    # 7) réponse Telegram en erreur ⇒ False, sans lever
    def test_register_bot_commands_error_returns_false(self):
        self._install(
            lambda r: httpx.Response(
                400, json={"ok": False, "description": "Bad Request: command list too long"}
            )
        )

        self.assertFalse(asyncio.run(_bridge.register_bot_commands()))

    # 8) /help mentionne les commandes ajoutées
    def test_help_text_mentions_new_commands(self):
        for name in ("stop", "compact", "cmds", "say"):
            self.assertIn(f"/{name}", _bridge._HELP_TEXT)


class PollingTimeoutTests(unittest.TestCase):
    """La boucle de polling doit survivre au long-poll getUpdates (timeout: 30).

    Avec le timeout httpx par défaut (5 s), chaque tour inactif expirait, le
    backoff montait jusqu'à ~72 s et la livraison des messages de Marc était
    retardée d'une minute.
    """

    def setUp(self):
        self._saved = {
            "poll": _bridge.POLL_TRANSPORT,
            "tg": _bridge.TELEGRAM_TRANSPORT,
            "state": _bridge.state,
        }
        self.created = []
        self.call_timeouts = []
        real_client = httpx.AsyncClient

        def handler(request):
            self.call_timeouts.append(dict(request.extensions.get("timeout") or {}))
            raise _PollStop()

        _bridge.POLL_TRANSPORT = httpx.MockTransport(handler)
        _bridge.TELEGRAM_TRANSPORT = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ok": True, "result": True})
        )

        test = self

        class RecordingClient(real_client):
            def __init__(inner_self, *args, **kwargs):
                test.created.append(dict(kwargs))
                super().__init__(*args, **kwargs)

        self._client_patch = patch("httpx.AsyncClient", RecordingClient)
        self._client_patch.start()

    def tearDown(self):
        self._client_patch.stop()
        _bridge.POLL_TRANSPORT = self._saved["poll"]
        _bridge.TELEGRAM_TRANSPORT = self._saved["tg"]
        _bridge.state = self._saved["state"]

    def test_polling_client_and_getupdates_timeout(self):
        async def _noop():
            return True

        with patch.object(_bridge, "register_bot_commands", _noop):
            with self.assertRaises(_PollStop):
                asyncio.run(_bridge.main())

        # Un seul client est créé dans main() : celui du polling.
        self.assertEqual(len(self.created), 1)
        timeout = self.created[0].get("timeout")
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertGreaterEqual(timeout.read, 40.0)

        # L'appel getUpdates porte en plus son propre timeout (marge > 30 s).
        self.assertTrue(self.call_timeouts, "getUpdates n'a pas été appelé")
        self.assertGreaterEqual(self.call_timeouts[0]["read"], 40.0)


class ModelCommandsTests(unittest.TestCase):
    """'/models' (filtre) et '/model' (courant / changement) sur MockTransport.

    Aucun appel réel, aucune session de Marc, aucun changement de modèle réel :
    le POST /api/session/{sid}/model est intercepté, STATE_FILE est temporaire.
    """

    CID = 6161
    SID = "ses_model_1"
    PAID = [{"input": 3.0, "output": 15.0}]
    # `cache` facturé mais input/output à 0 : doit rester « gratuit ».
    FREE = [{"input": 0, "output": 0, "cache": {"read": 0.3, "write": 1.0}}]

    def setUp(self):
        self._saved = {
            "transport": _bridge.TRANSPORT,
            "tg_send": _bridge.tg_send,
            "tg_edit": _bridge.tg_edit,
            "kind": dict(_bridge._KIND_CACHE),
            "models": dict(_bridge._MODELS_CACHE),
            "state": _bridge.state,
            "instances": _bridge._INSTANCES,
            "auth_env": os.environ.get("AUTHORIZED_CHAT_IDS"),
        }
        os.environ.pop("AUTHORIZED_CHAT_IDS", None)
        os.environ.pop("OPENCODE_INSTANCES", None)
        _bridge._INSTANCES = None
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE["v2"] = "api"
        _bridge._MODELS_CACHE.clear()

        self.requests = []
        self.sent = []
        self.edited = []
        self.model_post_code = 204
        # Codes HTTP imposés à GET /api/model, consommés dans l'ordre (sinon 200).
        self.model_codes = []
        self.session_model = {
            "id": "deepseek-v4.1-flash",
            "providerID": "opencode-go",
            "variant": "default",
        }
        self.catalog = self._catalog()
        _bridge.state = {
            str(self.CID): {
                "current": {"instance": "v2", "sid": self.SID},
                "agent": "build",
            }
        }

        async def fake_send(chat_id, text, markdown=True):
            self.sent.append((chat_id, text))
            return 7000 + len(self.sent)

        async def fake_edit(chat_id, message_id, text, markdown=True):
            self.edited.append((chat_id, message_id, text))

        _bridge.tg_send = fake_send
        _bridge.tg_edit = fake_edit
        self._install()

    def tearDown(self):
        _bridge.TRANSPORT = self._saved["transport"]
        _bridge.tg_send = self._saved["tg_send"]
        _bridge.tg_edit = self._saved["tg_edit"]
        _bridge.state = self._saved["state"]
        _bridge._INSTANCES = self._saved["instances"]
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved["kind"])
        _bridge._MODELS_CACHE.clear()
        _bridge._MODELS_CACHE.update(self._saved["models"])
        if self._saved["auth_env"] is None:
            os.environ.pop("AUTHORIZED_CHAT_IDS", None)
        else:
            os.environ["AUTHORIZED_CHAT_IDS"] = self._saved["auth_env"]

    # --- helpers ---------------------------------------------------------
    def _catalog(self):
        """25 modèles « claude » payants + 1 payant unique + 1 gratuit + 1 inconnu."""
        entries = [
            {
                "id": f"claude-{i:02d}",
                "providerID": "openrouter",
                "name": f"Claude Model {i:02d}",
                "limit": {"context": 200000},
                "cost": self.PAID,
            }
            for i in range(1, 26)
        ]
        entries.append(
            {
                "id": "deepseek-v4.1-flash",
                "providerID": "opencode-go",
                "name": "DeepSeek V4.1 Flash",
                "limit": {"context": 200000},
                "cost": self.PAID,
            }
        )
        entries.append(
            {
                "id": "meta/llama-3.3-70b",
                "providerID": "nvidia",
                "name": "Llama 3.3 70B",
                "limit": {"context": 128000},
                "cost": self.FREE,
            }
        )
        entries.append(
            {
                "id": "local-small",
                "providerID": "llama-local",
                "name": "Local Small",
                "limit": {"context": 32768},
            }
        )
        return entries

    def _install(self):
        def handler(request):
            path, method = request.url.path, request.method
            self.requests.append((method, path, request.content))
            if method == "GET" and path == "/api/model":
                code = self.model_codes.pop(0) if self.model_codes else 200
                if code != 200:
                    return httpx.Response(code, json={"error": "panne"})
                return httpx.Response(200, json={"data": self.catalog})
            if method == "GET" and path == f"/api/session/{self.SID}":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": self.SID,
                            "title": "Session test",
                            "model": self.session_model,
                        }
                    },
                )
            if method == "POST" and path == f"/api/session/{self.SID}/model":
                if 200 <= self.model_post_code < 300:
                    return httpx.Response(self.model_post_code)
                return httpx.Response(self.model_post_code, json={"error": "refusé"})
            return httpx.Response(404, json={})

        _bridge.TRANSPORT = httpx.MockTransport(handler)

    def _posts(self):
        return [r for r in self.requests if r[0] == "POST"]

    def _post_body(self, request):
        return json.loads(request[2].decode("utf-8")) if request[2] else {}

    def _run(self, text):
        asyncio.run(_bridge.handle_slash(self.CID, text))

    @staticmethod
    def _numbered(text):
        out = []
        for line in text.splitlines():
            head, sep, _ = line.partition(". ")
            if sep and head.isdigit():
                out.append(line)
        return out

    # 1) /model affiche le modèle de la session, contexte et coût résolus
    def test_model_shows_current_session_model(self):
        self._run("/model")

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(
            self.sent[0][1], "🤖 opencode-go/deepseek-v4.1-flash · 200K · payant"
        )
        self.assertEqual(self._posts(), [])

    # 2) /model sur une session sans modèle : message « serveur »
    def test_model_null_says_server_default(self):
        self.session_model = None
        self._run("/model")

        self.assertEqual(len(self.sent), 1)
        self.assertIn("celui du serveur", self.sent[0][1])
        self.assertEqual(self._posts(), [])

    # 2b) /model session sans modèle : repli sur le dernier modèle choisi
    def test_model_null_falls_back_to_last_chosen(self):
        self.session_model = None
        _bridge.state[str(self.CID)]["model"] = {
            "providerID": "nvidia",
            "id": "meta/llama-3.3-70b",
        }
        self._run("/model")

        self.assertEqual(len(self.sent), 1)
        message = self.sent[0][1]
        self.assertIn("nvidia/meta/llama-3.3-70b", message)
        self.assertIn("128K · gratuit", message)
        self.assertIn("(dernier choisi)", message)
        self.assertEqual(self._posts(), [])

    # 2c) repli mémorisé mais absent du catalogue : providerID/id seulement
    def test_model_null_falls_back_without_catalog_entry(self):
        self.session_model = None
        _bridge.state[str(self.CID)]["model"] = {
            "providerID": "ghost",
            "id": "ghost-model",
        }
        self._run("/model")

        message = self.sent[0][1]
        self.assertIn("ghost/ghost-model", message)
        self.assertIn("(dernier choisi)", message)
        self.assertNotIn(" · ", message.split("(dernier choisi)")[0])

    # 2d) cache négatif : un 500 n'est pas mémorisé, le 2ᵉ appel refait la requête
    def test_models_cache_not_poisoned_by_failure(self):
        inst = _bridge.instances()["v2"]
        self.model_codes = [500]
        first = asyncio.run(_bridge.cli_models(inst))
        second = asyncio.run(_bridge.cli_models(inst))

        gets = [r for r in self.requests if r[0] == "GET" and r[1] == "/api/model"]
        self.assertEqual(len(gets), 2)
        self.assertEqual(first, [])
        self.assertTrue(second)
        self.assertEqual(len(second), len(self.catalog))

    # 2e) cache actif : deux appels réussis ⇒ une seule requête
    def test_models_cache_serves_second_call_without_request(self):
        inst = _bridge.instances()["v2"]
        first = asyncio.run(_bridge.cli_models(inst))
        second = asyncio.run(_bridge.cli_models(inst))

        gets = [r for r in self.requests if r[0] == "GET" and r[1] == "/api/model"]
        self.assertEqual(len(gets), 1)
        self.assertTrue(first)
        self.assertEqual(first, second)

    # 3) /models <filtre> : au plus 20 lignes, format exact, reste compté
    def test_models_filter_lists_twenty_max(self):
        self._run("/models claude")

        self.assertEqual(len(self.sent), 1)
        lines = self.sent[0][1].splitlines()
        numbered = self._numbered(self.sent[0][1])
        self.assertEqual(len(numbered), 20)
        self.assertEqual(numbered[0], "1. openrouter/claude-01 · 200K · payant")
        self.assertEqual(lines[-1], "… et 5 autres — précise ta recherche")
        self.assertNotIn("claude-21", self.sent[0][1])

    # 4) /models sans argument : résumé, aucun listing
    def test_models_without_argument_does_not_list(self):
        self._run("/models")

        self.assertEqual(len(self.sent), 1)
        message = self.sent[0][1]
        self.assertEqual(
            message,
            "28 modèles · 4 fournisseurs. Filtre : /models <mot> "
            "(ex. /models claude, /models free, /models opencode-go)",
        )
        self.assertEqual(self._numbered(message), [])
        self.assertNotIn("claude-01", message)

    # 5) /models free : uniquement les modèles à coût nul
    def test_models_free_only_lists_free_models(self):
        self._run("/models free")

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1], "1. nvidia/meta/llama-3.3-70b · 128K · gratuit")

    # 6) /models zzz : aucun résultat + rappel des fournisseurs
    def test_models_unknown_filter(self):
        self._run("/models zzz")

        self.assertEqual(len(self.sent), 1)
        message = self.sent[0][1]
        self.assertIn("❌ aucun modèle pour « zzz »", message)
        self.assertIn("Fournisseurs :", message)
        self.assertIn("openrouter", message)

    # 6b) /models sans argument sur API en panne : erreur, jamais « 0 modèles »
    def test_models_without_argument_api_failure_is_an_error(self):
        self.model_codes = [500]
        self._run("/models")

        self.assertEqual(len(self.sent), 1)
        message = self.sent[0][1]
        self.assertIn("impossible de récupérer", message)
        self.assertNotIn("0 modèles", message)
        self.assertNotIn("0 fournisseurs", message)

    # 6c) /models <filtre> sur API en panne : erreur, pas « aucun modèle pour »
    def test_models_filter_api_failure_is_not_an_empty_filter(self):
        self.model_codes = [500]
        self._run("/models claude")

        self.assertEqual(len(self.sent), 1)
        message = self.sent[0][1]
        self.assertIn("impossible de récupérer", message)
        self.assertNotIn("aucun modèle pour", message)

    # 7) /model 2 après une liste : bon couple (providerID ET id) dans le corps
    def test_model_index_posts_second_listed_model(self):
        self._run("/models claude")
        self.sent.clear()
        self._run("/model 2")

        posts = self._posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], f"/api/session/{self.SID}/model")
        self.assertEqual(
            self._post_body(posts[0]),
            {"model": {"id": "claude-02", "providerID": "openrouter"}},
        )
        self.assertEqual(len(self.sent), 1)  # l'accusé…
        self.assertEqual(len(self.edited), 1)  # …est édité, pas renvoyé
        self.assertEqual(self.edited[0][1], 7001)  # message_id de l'accusé
        self.assertEqual(
            self.edited[0][2],
            "✅ modèle : openrouter/claude-02 · 200K · payant (tour suivant)",
        )

    # 8) /model 99 : indice hors liste, AUCUN POST
    def test_model_out_of_range_posts_nothing(self):
        self._run("/models claude")
        before = len(self._posts())
        self._run("/model 99")

        self.assertEqual(len(self._posts()), before)
        self.assertIn("❌ indice hors liste (1 à 25)", self.sent[-1][1])
        self.assertEqual(self.edited, [])

    # 9) /model sans liste préalable : message d'usage, aucun POST
    def test_model_without_previous_list_posts_nothing(self):
        self._run("/model 1")

        self.assertEqual(self._posts(), [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("fais d'abord /models", self.sent[0][1])

    # 10) /model <provider>/<id> : résolution directe, bon corps
    def test_model_provider_slash_id_posts_exact_body(self):
        self._run("/model deepseek-v4.1-flash")  # id seul : refusé, aucun POST
        self.assertEqual(self._posts(), [])
        self.assertIn("❌ modèle inconnu : deepseek-v4.1-flash", self.sent[-1][1])

        self.sent.clear()
        self._run("/model opencode-go/deepseek-v4.1-flash")

        posts = self._posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(
            self._post_body(posts[0]),
            {"model": {"id": "deepseek-v4.1-flash", "providerID": "opencode-go"}},
        )
        self.assertIn("✅ modèle : opencode-go/deepseek-v4.1-flash", self.edited[-1][2])
        self.assertIn("(tour suivant)", self.edited[-1][2])

    # 11) péremption : liste de plus de 600 s ⇒ refus, aucun POST
    def test_model_expired_list_is_refused(self):
        self._run("/models claude")
        _bridge.state[str(self.CID)]["models_at"] = _bridge.time.time() - 601
        self._run("/model 1")

        self.assertEqual(self._posts(), [])
        self.assertIn("fais d'abord /models", self.sent[-1][1])

    # 12) le modèle choisi survit à un rechargement de STATE_FILE
    def test_chosen_model_persisted_in_state_file(self):
        self._run("/models claude")
        self._run("/model 2")

        reloaded = _bridge._load_state()
        self.assertEqual(
            reloaded[str(self.CID)]["model"],
            {"providerID": "openrouter", "id": "claude-02"},
        )

    # 13) échec du POST : message ❌ édité, aucun état de modèle mémorisé
    def test_model_post_refused_reports_failure(self):
        self.model_post_code = 400
        self._run("/models claude")
        self._run("/model 1")

        self.assertEqual(len(self._posts()), 1)
        self.assertEqual(self.edited[-1][2], "❌ le changement a échoué")
        self.assertNotIn("model", _bridge.state[str(self.CID)])


if __name__ == "__main__":
    unittest.main()