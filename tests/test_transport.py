import asyncio
import importlib.util
import json
import os
import tempfile
import unittest

import httpx

# Le module quitte si TELEGRAM_TOKEN est manquant : on pose le token AVANT l'import.
os.environ["TELEGRAM_TOKEN"] = "test"

BRIDGE_PATH = "/home/marcs/.telegram-bridge-venv/bridge.py"
_spec = importlib.util.spec_from_file_location("bridge_under_test", BRIDGE_PATH)
_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bridge)


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
        self.requests.append((request.method, request.url.path, body))
        return await self._mock.handle_async_request(request)


class TransportTests(unittest.TestCase):
    def _set_transport(self, handler):
        _bridge.TELEGRAM_TRANSPORT = _RecordingTransport(handler)

    # 1) Repli en texte brut après 400
    def test_fallback_plain_apres_400(self):
        def handler(request):
            payload = json.loads(request.content)
            if payload.get("parse_mode"):
                return httpx.Response(
                    400, json={"ok": False, "description": "No HTML"}
                )
            return httpx.Response(200, json={"ok": True, "message_id": 1})

        self._set_transport(handler)
        # tg_send : pas d'erreur
        asyncio.run(_bridge.tg_send(1, "mot _non_apparie_"))
        self.assertEqual(len(_bridge.TELEGRAM_TRANSPORT.requests), 2)
        second = _bridge.TELEGRAM_TRANSPORT.requests[1]
        self.assertEqual(second[0], "POST")
        self.assertNotIn("parse_mode", json.loads(second[2]))

    # 2) Découpage respectant les frontières
    def test_chunking(self):
        text = "a" * 9000
        chunks = _bridge.chunk_text(text)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 3500 for c in chunks))
        self.assertEqual("".join(chunks), text)

    # 3) ok:false toujours faux -> TelegramError
    def test_ok_false_leve(self):
        def handler(request):
            return httpx.Response(
                400, json={"ok": False, "description": "Bad Request"}
            )

        self._set_transport(handler)
        with self.assertRaises(_bridge.TelegramError):
            asyncio.run(_bridge.tg_send(1, "salut"))

    # 4) État atomique : tmp + os.replace, pas de .tmp résiduel
    def test_state_atomique(self):
        tmp = tempfile.mkdtemp()
        state_file = os.path.join(tmp, "tg-sessions.json")
        _bridge.STATE_FILE = state_file
        state = {"chat": "1155339708", "current": "ses_abc"}
        _bridge._save_state(state)
        loaded = _bridge._load_state()
        self.assertEqual(loaded, state)
        # Pas de fichier .tmp laissé derrière
        self.assertFalse(os.path.exists(state_file + ".tmp"))


if __name__ == "__main__":
    unittest.main()
