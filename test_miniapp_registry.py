import unittest

from miniapp_registry import get_miniapp, list_enabled_miniapps, register_miniapp


class MiniAppRegistryTests(unittest.TestCase):
    def test_only_explicitly_enabled_flows_are_registered(self):
        self.assertEqual([], list_enabled_miniapps())
        qyz = get_miniapp("qingyuanzi")
        self.assertEqual(qyz["adapter"], "qyz_protocol")
        self.assertFalse(qyz["production_enabled"])
        self.assertFalse(qyz["live_verified"])
        self.assertIsNone(get_miniapp("fishing"))

    def test_registration_rejects_unverified_production_flow(self):
        with self.assertRaises(ValueError):
            register_miniapp("trial", adapter="trial_adapter", production_enabled=True, live_verified=False)


if __name__ == "__main__":
    unittest.main()
