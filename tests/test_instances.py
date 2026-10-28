import asyncio
import importlib.util
import os
import unittest

import httpx

# Le module quitte si TELEGRAM_TOKEN est manquant : on pose le token AVANT l'import.
os.environ["TELEGRAM_TOKEN"] = "test"

BRIDGE_PATH = "/home/marcs/.telegram-bridge-venv/bridge.py"
_spec = importlib.util.spec_from_file_location("bridge_under_test", BRIDGE_PATH)
_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bridge)


# État partagé pour la vérification Basic (v2) : accessible depuis le handler du
# mock ET depuis les assertions du test (la version précédente était locale et
# donc inaccessible depuis l'appel au mock).
_API_AUTH_STATE = {"msg_calls": 0, "last_auth": None}


def _v1_session(sid="ses_v1"):
    """Forme legacy (v1) : model est un dict {id}."""
    return {
        "id": sid,
        "title": "New session - 2026-10-28T12:07:08.639Z",
        "agent": "build",
        "model": {"id": "base"},
        "tokens": {"input": 100, "output": 50, "reasoning": 0, "cache": {"read": 10, "write": 5}},
        "time": {"created": 1793189228639},
    }


def _v2_session(sid="ses_v2"):
    """Forme api (v2) : model est un string (Model.Ref)."""
    return {
        "id": sid,
        "title": "New session - 2026-10-28T12:07:08.639Z",
        "agent": "build",
        "model": "base",
        "tokens": {"input": 200, "output": 100, "reasoning": 0, "cache": {"read": 20, "write": 10}},
        "time": {"created": 1793189274187},
    }


def _assert_normalized(obj):
    """Mêmes champs normalisés quel que soit le serveur."""
    expected_keys = {"instance", "sid", "title", "agent", "model", "tokens", "created_ms", "updated_ms"}
    assert set(obj.keys()) == expected_keys
    assert isinstance(obj["instance"], str)
    assert isinstance(obj["sid"], str)
    assert isinstance(obj["title"], str)
    assert isinstance(obj["agent"], str)
    assert isinstance(obj["model"], str)
    assert isinstance(obj["tokens"], int)
    assert obj["created_ms"] is None or isinstance(obj["created_ms"], (int, float))
    assert obj["updated_ms"] is None or isinstance(obj["updated_ms"], (int, float))


class ParseInstancesTests(unittest.TestCase):
    def test_default(self):
        for k in ("OPENCODE_INSTANCES", "OPENCODE_V2_AUTH"):
            os.environ.pop(k, None)
        insts = _bridge.parse_instances()
        self.assertEqual(set(insts.keys()), {"v1", "v2"})
        self.assertEqual(insts["v1"]["url"], "http://127.0.0.1:4096")
        self.assertEqual(insts["v2"]["url"], "http://127.0.0.1:4097")
        self.assertEqual(insts["v1"]["auth"], "")
        self.assertEqual(insts["v2"]["auth"], "")

    def test_custom(self):
        os.environ["OPENCODE_INSTANCES"] = "alpha=http://a:1,beta=http://b:2"
        os.environ["OPENCODE_V2_AUTH"] = "user:pass"
        try:
            insts = _bridge.parse_instances()
            self.assertEqual(set(insts.keys()), {"alpha", "beta"})
            self.assertEqual(insts["alpha"]["url"], "http://a:1")
            self.assertEqual(insts["beta"]["url"], "http://b:2")
            self.assertEqual(insts["alpha"]["auth"], "user:pass")
            self.assertEqual(insts["beta"]["auth"], "user:pass")
        finally:
            os.environ.pop("OPENCODE_INSTANCES", None)
            os.environ.pop("OPENCODE_V2_AUTH", None)


class NormalizeTests(unittest.TestCase):
    def test_both_shapes(self):
        legacy = _bridge.cli_normalize_session(_v1_session(), "v1", "legacy")
        api = _bridge.cli_normalize_session(_v2_session(), "v2", "api")
        _assert_normalized(legacy)
        _assert_normalized(api)
        self.assertEqual(legacy["tokens"], 150)
        self.assertEqual(api["tokens"], 300)
        # model v1 (dict) et v2 (str) normalisés au même type str
        self.assertEqual(legacy["model"], "base")
        self.assertEqual(api["model"], "base")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self._saved = _bridge.TRANSPORT

    def tearDown(self):
        _bridge.TRANSPORT = self._saved

    # --- legacy (v1) : réponse directe dans parts[].text ---
    def _legacy_transport(self):
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/session":
                return httpx.Response(200, json=[_v1_session()])
            if request.method == "POST" and path == "/session/ses_v1/message":
                return httpx.Response(200, json={"parts": [{"type": "text", "text": "réponse v1"}]})
            if request.method == "GET" and path == "/session/ses_v1":
                return httpx.Response(200, json=_v1_session())
            if request.method == "PATCH" and path == "/session/ses_v1":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        _bridge.TRANSPORT = httpx.MockTransport(handler)

    def test_legacy_list_and_prompt(self):
        self._legacy_transport()
        inst = {"name": "v1", "url": "http://127.0.0.1:4096", "auth": "", "kind": "legacy"}
        results = asyncio.run(_bridge.cli_list_sessions({"v1": inst}))
        _assert_normalized(results["v1"]["sessions"][0])
        text = asyncio.run(_bridge.cli_prompt(inst, "ses_v1", "salut"))
        self.assertEqual(text, "réponse v1")

    def test_legacy_detect_kind(self):
        self._legacy_transport()
        inst = _bridge.parse_instances("v1=http://127.0.0.1:4096")["v1"]
        kind = asyncio.run(_bridge.detect_kind(inst))
        self.assertEqual(kind, "legacy")

    # --- api (v2) : enveloppe data + polling jusqu'à assistant ---
    def _api_transport(self):
        _API_AUTH_STATE["msg_calls"] = 0
        _API_AUTH_STATE["last_auth"] = None

        def handler(request):
            auth = request.headers.get("Authorization")
            _API_AUTH_STATE["last_auth"] = auth
            path = request.url.path
            if path == "/api/session":
                return httpx.Response(
                    200,
                    json={"data": [_v2_session()], "cursor": {"previous": None, "next": None}},
                )
            if path == "/api/session/ses_v2/prompt":
                return httpx.Response(
                    200,
                    json={"data": {"id": "msg_1", "type": "user", "payload": {"text": "salut"}}},
                )
            if path == "/api/session/ses_v2/message":
                _API_AUTH_STATE["msg_calls"] += 1
                if _API_AUTH_STATE["msg_calls"] == 1:
                    # 1er polling : pas encore de réponse assistant
                    return httpx.Response(
                        200, json={"data": [], "cursor": {"previous": None, "next": None}}
                    )
                # 2e polling : message assistant avec content[].text
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "type": "assistant",
                                "agent": "build",
                                "content": [{"type": "text", "text": "réponse v2"}],
                                "finish": "stop",
                            }
                        ],
                        "cursor": {"previous": None, "next": None},
                    },
                )
            return httpx.Response(404)

        _bridge.TRANSPORT = httpx.MockTransport(handler)

    def test_api_list_and_prompt(self):
        self._api_transport()
        inst = {"name": "v2", "url": "http://127.0.0.1:4097", "auth": "opencode:pass", "kind": "api"}
        results = asyncio.run(_bridge.cli_list_sessions({"v2": inst}))
        _assert_normalized(results["v2"]["sessions"][0])
        text = asyncio.run(_bridge.cli_prompt(inst, "ses_v2", "salut"))
        # même comportement que legacy : texte attendu
        self.assertEqual(text, "réponse v2")
        # auth Basic envoyée sur l'api
        self.assertTrue(_API_AUTH_STATE["last_auth"].startswith("Basic "))

    def test_api_detect_kind(self):
        self._api_transport()
        inst = _bridge.parse_instances("v2=http://127.0.0.1:4097")["v2"]
        kind = asyncio.run(_bridge.detect_kind(inst))
        self.assertEqual(kind, "api")

    def test_api_timeout_polling(self):
        """Polling sans jamais voir un assistant : TimeoutError lisible."""
        def handler(request):
            return httpx.Response(200, json={"data": [], "cursor": {"previous": None, "next": None}})

        _bridge.TRANSPORT = httpx.MockTransport(handler)
        inst = {"name": "v2", "url": "http://127.0.0.1:4097", "auth": "opencode:pass", "kind": "api"}
        with self.assertRaises(TimeoutError):
            asyncio.run(_bridge._poll_api(inst, "ses_v2", timeout=0.2, interval=0.05))

    # --- instance morte ne bloque pas le listing des autres ---
    def _mixed_transport(self):
        def handler(request):
            if request.url.path == "/session":
                # "good" (g:1) répond ; "bad" (b:2) est injoignable → 500 sur
                # /api/session ET /session, donc détectée unreachable.
                if "g:1" in str(request.url):
                    return httpx.Response(200, json=[_v1_session()])
                return httpx.Response(500)
            return httpx.Response(500)

        _bridge.TRANSPORT = httpx.MockTransport(handler)

    def test_dead_instance_does_not_block(self):
        self._mixed_transport()
        insts = _bridge.parse_instances("good=http://g:1,bad=http://b:2")
        results = asyncio.run(_bridge.cli_list_sessions(insts))
        # good détecté legacy : /api/session 500 puis /session 200 (liste)
        self.assertEqual(results["good"]["kind"], "legacy")
        self.assertFalse(results["good"]["unreachable"])
        self.assertTrue(results["bad"]["unreachable"])
        _assert_normalized(results["good"]["sessions"][0])
        self.assertEqual(len(results["good"]["sessions"]), 1)
        self.assertEqual(results["bad"]["sessions"], [])


class GetSessionContextTokensTests(unittest.TestCase):
    """B9 — get_session_context_tokens lit l'objet session (pas les messages),
    gère v1 et v2, repli tolérant à 0 sans lever."""

    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_kind_cache = dict(_bridge._KIND_CACHE)

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved_kind_cache)

    def _legacy_transport_with_tokens(self, input_tokens=150, output_tokens=75):
        """Transport legacy (v1) avec session contenant tokens."""
        def handler(request):
            path = request.url.path
            if request.method == "GET" and path == "/session":
                return httpx.Response(200, json=[_v1_session()])
            if request.method == "GET" and path == "/session/ses_v1":
                return httpx.Response(200, json={
                    "id": "ses_v1",
                    "title": "Test Session",
                    "agent": "build",
                    "model": {"id": "base"},
                    "tokens": {"input": input_tokens, "output": output_tokens, "reasoning": 0, "cache": {"read": 10, "write": 5}},
                    "time": {"created": 1793189228639},
                })
            if request.method == "POST" and path == "/session/ses_v1/message":
                return httpx.Response(200, json={"parts": [{"type": "text", "text": "réponse v1"}]})
            if request.method == "PATCH" and path == "/session/ses_v1":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        _bridge.TRANSPORT = httpx.MockTransport(handler)

    def _api_transport_with_tokens(self, input_tokens=300, output_tokens=150):
        """Transport api (v2) avec session contenant tokens dans l'enveloppe data."""
        _API_AUTH_STATE["msg_calls"] = 0
        _API_AUTH_STATE["last_auth"] = None

        def handler(request):
            auth = request.headers.get("Authorization")
            _API_AUTH_STATE["last_auth"] = auth
            path = request.url.path
            if path == "/api/session":
                return httpx.Response(
                    200,
                    json={"data": [_v2_session()], "cursor": {"previous": None, "next": None}},
                )
            if path == "/api/session/ses_v2":
                return httpx.Response(
                    200,
                    json={"data": {
                        "id": "ses_v2",
                        "title": "Test Session v2",
                        "agent": "build",
                        "model": "base",
                        "tokens": {"input": input_tokens, "output": output_tokens, "reasoning": 0, "cache": {"read": 20, "write": 10}},
                        "time": {"created": 1793189274187},
                    }},
                )
            if path == "/api/session/ses_v2/prompt":
                return httpx.Response(
                    200,
                    json={"data": {"id": "msg_1", "type": "user", "payload": {"text": "salut"}}},
                )
            if path == "/api/session/ses_v2/message":
                _API_AUTH_STATE["msg_calls"] += 1
                if _API_AUTH_STATE["msg_calls"] == 1:
                    return httpx.Response(
                        200, json={"data": [], "cursor": {"previous": None, "next": None}}
                    )
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "type": "assistant",
                                "agent": "build",
                                "content": [{"type": "text", "text": "réponse v2"}],
                                "finish": "stop",
                            }
                        ],
                        "cursor": {"previous": None, "next": None},
                    },
                )
            return httpx.Response(404)

        _bridge.TRANSPORT = httpx.MockTransport(handler)

    def test_legacy_tokens_from_session_object(self):
        """v1 (legacy) : tokens = session.tokens.input + session.tokens.output."""
        self._legacy_transport_with_tokens(input_tokens=150, output_tokens=75)
        inst = {"name": "v1", "url": "http://127.0.0.1:4096", "auth": "", "kind": "legacy"}
        n = asyncio.run(_bridge.get_session_context_tokens(inst, "ses_v1"))
        self.assertEqual(n, 225)  # 150 + 75

    def test_api_tokens_from_session_object(self):
        """v2 (api) : tokens = session.data.tokens.input + session.data.tokens.output."""
        self._api_transport_with_tokens(input_tokens=300, output_tokens=150)
        inst = {"name": "v2", "url": "http://127.0.0.1:4097", "auth": "opencode:pass", "kind": "api"}
        n = asyncio.run(_bridge.get_session_context_tokens(inst, "ses_v2"))
        self.assertEqual(n, 450)  # 300 + 150

    def test_missing_tokens_field_returns_zero(self):
        """Session sans champ tokens -> 0, pas d'exception."""
        def handler(request):
            if request.method == "GET" and request.url.path == "/session":
                return httpx.Response(200, json=[{"id": "ses_no_tokens", "title": "Test", "agent": "build"}])
            if request.method == "GET" and request.url.path == "/session/ses_no_tokens":
                return httpx.Response(200, json={"id": "ses_no_tokens", "title": "Test", "agent": "build", "model": "base"})
            return httpx.Response(404)

        _bridge.TRANSPORT = httpx.MockTransport(handler)
        inst = {"name": "v1", "url": "http://127.0.0.1:4096", "auth": "", "kind": "legacy"}
        n = asyncio.run(_bridge.get_session_context_tokens(inst, "ses_no_tokens"))
        self.assertEqual(n, 0)

    def test_unreachable_instance_returns_zero_no_exception(self):
        """Instance injoignable -> 0, pas d'exception (repli tolérant)."""
        def handler(request):
            return httpx.Response(500)

        _bridge.TRANSPORT = httpx.MockTransport(handler)
        inst = {"name": "v1", "url": "http://127.0.0.1:4096", "auth": "", "kind": None}
        n = asyncio.run(_bridge.get_session_context_tokens(inst, "ses_xxx"))
        self.assertEqual(n, 0)

    def test_session_not_found_returns_zero_no_exception(self):
        """Session 404 -> 0, pas d'exception (repli tolérant)."""
        def handler(request):
            if request.method == "GET" and request.url.path == "/session/ses_missing":
                return httpx.Response(404)
            return httpx.Response(404)

        _bridge.TRANSPORT = httpx.MockTransport(handler)
        inst = {"name": "v1", "url": "http://127.0.0.1:4096", "auth": "", "kind": "legacy"}
        n = asyncio.run(_bridge.get_session_context_tokens(inst, "ses_missing"))
        self.assertEqual(n, 0)


class CliPromptPayloadTests(unittest.TestCase):
    """Tests pour le payload cli_prompt v2 : format agents[name] et erreur 400 explicite."""

    def setUp(self):
        self._saved = _bridge.TRANSPORT
        self._saved_kind_cache = dict(_bridge._KIND_CACHE)

    def tearDown(self):
        _bridge.TRANSPORT = self._saved
        _bridge._KIND_CACHE.clear()
        _bridge._KIND_CACHE.update(self._saved_kind_cache)

    def _api_transport_with_prompt_capture(self):
        """Capture le payload envoyé à /prompt pour vérifier le format."""
        self.captured_payload = None

        def handler(request):
            path = request.url.path
            if path == "/api/session":
                return httpx.Response(
                    200,
                    json={"data": [_v2_session()], "cursor": {"previous": None, "next": None}},
                )
            if path == "/api/session/ses_v2/prompt":
                import json as _json
                self.captured_payload = _json.loads(request.content.decode())
                return httpx.Response(
                    200,
                    json={"data": {"id": "msg_1", "type": "user", "payload": self.captured_payload, "delivery": "steer"}},
                )
            if path == "/api/session/ses_v2/message":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "type": "assistant",
                                "agent": "build",
                                "content": [{"type": "text", "text": "réponse v2"}],
                                "finish": "stop",
                            }
                        ],
                        "cursor": {"previous": None, "next": None},
                    },
                )
            return httpx.Response(404)

        _bridge.TRANSPORT = httpx.MockTransport(handler)

    def test_api_prompt_payload_uses_name_not_id(self):
        """Le payload envoyé à /prompt doit utiliser agents[].name (pas .id)."""
        self._api_transport_with_prompt_capture()
        inst = {"name": "v2", "url": "http://127.0.0.1:4097", "auth": "opencode:pass", "kind": "api"}
        asyncio.run(_bridge.cli_prompt(inst, "ses_v2", "salut", agent="build"))
        # Vérifier le payload capturé
        self.assertIsNotNone(self.captured_payload)
        self.assertIn("agents", self.captured_payload)
        agents = self.captured_payload["agents"]
        self.assertIsInstance(agents, list)
        self.assertEqual(len(agents), 1)
        self.assertIn("name", agents[0])
        self.assertEqual(agents[0]["name"], "build")
        self.assertNotIn("id", agents[0])

    def _api_transport_always_400(self):
        """Simule un 400 systématique sur /prompt pour tester la gestion d'erreur."""
        def handler(request):
            path = request.url.path
            if path == "/api/session":
                return httpx.Response(
                    200,
                    json={"data": [_v2_session()], "cursor": {"previous": None, "next": None}},
                )
            if path == "/api/session/ses_v2/prompt":
                return httpx.Response(
                    400,
                    json={"_tag": "InvalidRequestError", "message": "Invalid payload: missing required field", "kind": "Payload"},
                )
            if path == "/api/session/ses_v2/message":
                return httpx.Response(
                    200,
                    json={"data": [], "cursor": {"previous": None, "next": None}},
                )
            return httpx.Response(404)

        _bridge.TRANSPORT = httpx.MockTransport(handler)

    def test_api_prompt_400_raises_explicit_error(self):
        """Un 400 sur /prompt lève une HTTPStatusError avec un message lisible."""
        self._api_transport_always_400()
        inst = {"name": "v2", "url": "http://127.0.0.1:4097", "auth": "opencode:pass", "kind": "api"}
        import json
        try:
            asyncio.run(_bridge.cli_prompt(inst, "ses_v2", "salut", agent="build"))
            self.fail("Expected HTTPStatusError for 400")
        except httpx.HTTPStatusError as e:
            self.assertIn("400", str(e))
            self.assertIn("Invalid payload", str(e))
            self.assertIn("prompt", str(e))


if __name__ == "__main__":
    unittest.main()
