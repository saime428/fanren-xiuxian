import unittest
from pathlib import Path


class ShendubiguanConnectRetryTests(unittest.TestCase):
    def test_send_flow_uses_bounded_connect_retry(self):
        source = Path("send_shendubiguan.py").read_text(encoding="utf-8")
        self.assertIn('label="shendubiguan client.start"', source)
        self.assertIn("reconnect=lambda: force_reconnect(client)", source)
        self.assertNotIn("await asyncio.wait_for(client.start(), timeout=run_max_seconds)", source)


if __name__ == "__main__":
    unittest.main()
