import asyncio
import importlib.util
import os
import tempfile
import unittest

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


class SessionsPaginationTests(unittest.TestCase):
    # 3) pagination (10 puis suite) : page_rows mémorise la page affichée.
    # 4) cohérence /sessions → /select : le 1er de la page 2 est bien celui
    #    sélectionné (lu dans page_rows, sans recalcul).
    def setUp(self):
        self._saved_transport = _bridge.TRANSPORT
        self._saved_state = _bridge.state
        self._cid = "999"

    def tearDown(self):
        _bridge.TRANSPORT = self._saved_transport
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
