import subprocess
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parent / "scripts/lib/account_flow_paths.sh"


class demo_player_01SessionFallbackTests(unittest.TestCase):
    def test_all_flows_use_account_level_authoritative_session_seed(self):
        # An account env (or the old template) may still export a per-account
        # fallback; the helper must override it for this account. Runs in a
        # scratch directory so no private env file or state/ is needed.
        command = r"""
set -euo pipefail
export TG_SESSION_FALLBACK_NAME=state/tg9100000001
export XIU_ACCOUNT_SLUG=tg9100000001
export XIU_FLOW_SLUG=session_contract_test
source "$1"
printf '%s\n' "$TG_SESSION_FALLBACK_NAME"
"""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["/bin/bash", "-c", command, "bash", str(LIB)],
                cwd=tmp,
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(
            "state/accounts/tg9100000001/session",
            result.stdout.strip(),
        )

    def test_env_template_leaves_routing_and_session_seed_to_the_helper(self):
        template = (LIB.parents[2] / "config/accounts/account.env.example").read_text(encoding="utf-8")
        for name in ("TG_TARGET", "TG_TOPIC_ID", "TG_REPLY_SCAN_TARGET", "TG_SESSION_FALLBACK_NAME"):
            self.assertNotIn(f"export {name}=", template)

    def test_jiyin_does_not_override_account_level_session_seed(self):
        text = Path("scripts/run_jiyin_choice_account.sh").read_text(encoding="utf-8")
        self.assertNotIn('export TG_SESSION_FALLBACK_NAME="state/${source_slug}"', text)
        self.assertIn(
            'export TG_SESSION_FALLBACK_NAME="${TG_SESSION_FALLBACK_NAME:-state/${source_slug}}"',
            text,
        )


if __name__ == "__main__":
    unittest.main()