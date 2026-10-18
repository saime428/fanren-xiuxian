"""The runtime tree holds one execution path: the retired modules stay gone."""
import importlib.machinery
import unittest
from pathlib import Path
import qingyuanzi_host as host

class CapabilityTests(unittest.TestCase):
    def test_retired_execution_modules_are_physically_absent(self):
        root = Path(host.__file__).parent
        retired = {
            "world_boss", "world_boss_features", "world_boss_turnstile", "world_boss_browser",
            "world_boss_main_entry", "turnstile_token_runner", "turnstile_probe",
            "qyz_turnstile_browser_worker", "qyz_browser_execution", "qyz_original_adapter",
            "miniapp_beast", "log_utils", "qyz_drag_verification",
        }
        artifacts = sorted(
            str(path.relative_to(root))
            for path in (*root.glob("*.py"), *root.glob("*.pyc"), *root.glob("__pycache__/*.pyc"))
            if path.name.split(".", 1)[0] in retired
        )
        self.assertEqual(artifacts, [], "Retired code and bytecode must not remain in the runtime tree")
        for module in sorted(retired):
            with self.subTest(module=module):
                self.assertIsNone(importlib.machinery.PathFinder.find_spec(module, [str(root)]))
