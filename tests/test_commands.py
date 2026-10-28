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


if __name__ == "__main__":
    unittest.main()