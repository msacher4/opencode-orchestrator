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


if __name__ == "__main__":
    unittest.main()
