import asyncio
import http.client
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from miniapp_shared.dwelling import (
    DwellingSession,
    JsonTransport,
    MiniAppError,
    extract_child_token,
    external_apps,
    mint_child_token,
    open_dwelling,
    safe_text,
    server_error_code,
)
from miniapp_shared.state import LockBusy, exclusive_lock, read_json, write_json
from tianji_trial.runtime import Identity


class TokenAndTextTests(unittest.TestCase):
    def test_child_token_accepts_official_api_and_bot_launches_only(self):
        self.assertEqual(
            "farm_ABC123",
            extract_child_token("/miniapp/xianxia-sect-farm?startapp=farm_ABC123",
                                web_path="/miniapp/xianxia-sect-farm", prefixes=("farm_",)),
        )
        self.assertEqual(
            "pagoda_XYZ",
            extract_child_token("https://t.me/fanrenxiuxian_bot?startapp=pagoda_XYZ",
                                web_path="/miniapp/xianxia-pagoda", prefixes=("pagoda_", "pagoda-")),
        )
        for bad in (
            "https://evil.invalid/miniapp/xianxia-sect-farm?startapp=farm_ABC123",
            "/miniapp/xianxia-pagoda?startapp=farm_ABC123",
            "http://asc.aiopenai.app/miniapp/xianxia-sect-farm?startapp=farm_ABC123",
            "",
        ):
            with self.assertRaisesRegex(MiniAppError, "child_launch_invalid"):
                extract_child_token(bad, web_path="/miniapp/xianxia-sect-farm", prefixes=("farm_",))

    def test_safe_text_redacts_urls_and_signed_pairs(self):
        text = safe_text("ok https://t.me/x?startapp=farm_SECRET initData=abc&hash=zzz done")
        self.assertNotIn("SECRET", text)
        self.assertNotIn("abc", text)
        self.assertIn("[url]", text)

    def test_server_error_code_prefers_safe_codes(self):
        self.assertEqual("wild_experience_unavailable",
                         server_error_code({"ok": False, "error": "wild_experience_unavailable"}, "x"))
        self.assertEqual("fallback", server_error_code({"error": "<html>"}, "fallback"))


class JsonTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowlist_and_mutation_flag_are_enforced_before_network(self):
        calls = []

        def post(path, payload, *, mutation):
            calls.append((path, mutation))
            return {"ok": True}

        transport = JsonTransport({"/a": False, "/b": True}, post=post)
        await transport("/a", {}, mutation=False)
        await transport("/b", {}, mutation=True)
        with self.assertRaisesRegex(MiniAppError, "endpoint_not_allowed"):
            await transport("/a", {}, mutation=True)
        with self.assertRaisesRegex(MiniAppError, "endpoint_not_allowed"):
            await transport("/c", {}, mutation=False)
        self.assertEqual([("/a", False), ("/b", True)], calls)


class DwellingSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_dwelling_pins_entry_and_parses_identities(self):
        class EntryProtocol:
            async def _pinned_cave_entry(self, _client):
                return {"token": "df_PARENT"}

            async def _cave_webview(self, _client, _entry):
                return "query_id=TEST&hash=SECRET"

        class Bridge:
            account = "tg9100000001"
            client = object()
            _entry_protocol = EntryProtocol()

        seen = []

        def post(path, payload, *, mutation):
            seen.append((path, payload, mutation))
            return {
                "identity": {
                    "selectedPlayerId": 9100000001,
                    "choices": [
                        {"playerId": 9100000001, "daoName": "示例修士一", "source": "personal"},
                        {"playerId": 9200000001, "daoName": "示例绑定角色", "source": "bound_character"},
                    ],
                },
                "account": {"externalApps": {"groups": [{"key": "daily", "apps": [
                    {"key": "pagoda", "title": "琉璃问心塔", "action": "pagoda", "available": True,
                     "url": "https://t.me/fanrenxiuxian_bot?startapp=pagoda_SECRET"}]}]}},
            }

        transport = JsonTransport({"/api/miniapp/xianxia-dwelling/start": False,
                                   "/api/miniapp/xianxia-dwelling/external": False}, post=post)
        session = await open_dwelling(Bridge(), transport)

        self.assertEqual("df_PARENT", session.entry_token)
        self.assertEqual((9100000001, 9200000001), tuple(i.player_id for i in session.identities))
        self.assertEqual(9100000001, session.personal_identity().player_id)
        self.assertEqual({"token": "df_PARENT", "initData": "query_id=TEST&hash=SECRET", "playerId": 9200000001},
                         session.base_payload(9200000001))
        self.assertEqual([("/api/miniapp/xianxia-dwelling/start", False)],
                         [(path, mutation) for path, _payload, mutation in seen])
        apps = external_apps(session.start_payload)
        self.assertEqual([{"group": "daily", "key": "pagoda", "title": "琉璃问心塔", "action": "pagoda",
                           "available": True}], apps)
        self.assertNotIn("SECRET", str(apps))

    async def test_mint_child_token_sends_player_scoped_external_action(self):
        seen = []

        def post(path, payload, *, mutation):
            seen.append((path, payload, mutation))
            return {"url": "/miniapp/xianxia-sect-farm?startapp=farm_CHILD"}

        transport = JsonTransport({"/api/miniapp/xianxia-dwelling/external": False}, post=post)
        session = DwellingSession("tg9100000004", "INIT", "df_PARENT", {},
                                  (Identity(9100000004, "竹衍衡", "personal"),))
        token = await mint_child_token(transport, session, player_id=9100000004, action="sect_farm",
                                       web_path="/miniapp/xianxia-sect-farm", prefixes=("farm_",))

        self.assertEqual("farm_CHILD", token)
        self.assertEqual([("/api/miniapp/xianxia-dwelling/external",
                           {"token": "df_PARENT", "initData": "INIT", "playerId": 9100000004,
                            "action": "sect_farm"}, False)], seen)


class AccountTableTests(unittest.TestCase):
    def test_flows_share_one_account_and_credential_source_table(self):
        from miniapp_shared.accounts import ALL_ACCOUNTS, CREDENTIAL_SOURCE, DWELLING_SESSIONS
        from tianji_trial.adapter import ACCOUNT_CONFIG
        self.assertEqual({"tg9100000006": "tg9100000007"},
                         {account: source for account, source in CREDENTIAL_SOURCE.items() if account != source})
        self.assertEqual(ALL_ACCOUNTS, tuple(ACCOUNT_CONFIG))
        self.assertEqual({"source": "tg9100000007", "session": "state/tg9100000007.session"}, ACCOUNT_CONFIG["tg9100000006"])
        self.assertEqual("state/accounts/tg9100000001/session.session", DWELLING_SESSIONS["tg9100000001"])
        self.assertEqual("state/tg9100000002.session", DWELLING_SESSIONS["tg9100000002"])


class ImportBoundaryTests(unittest.TestCase):
    def test_dwelling_import_pulls_in_no_flow_package_or_telethon(self):
        code = ("import sys, miniapp_shared.dwelling; "
                "heavy = sorted({m.split('.')[0] for m in sys.modules} & {'fishing', 'tianji_trial', "
                "'miniapp_fishing_integration', 'fishing_accounts', 'telethon', 'networkx'}); "
                "print(heavy); raise SystemExit(bool(heavy))")
        done = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    def test_tianji_errors_are_miniapp_errors(self):
        from miniapp_shared.dwelling import error_code
        from tianji_trial.protocol import TianjiTrialError
        exc = TianjiTrialError("trial_daily_limit", uncertain=True)
        self.assertIsInstance(exc, MiniAppError)
        self.assertEqual(("trial_daily_limit", True), (error_code(exc), exc.uncertain))
        self.assertEqual("ValueError", error_code(ValueError("secret")))


class StateHelperTests(unittest.TestCase):
    def test_json_roundtrip_and_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            write_json(path, {"a": 1})
            self.assertEqual({"a": 1}, read_json(path))
            self.assertEqual({}, read_json(Path(tmp) / "missing.json"))
            lock = Path(tmp) / "run.lock"
            with exclusive_lock(lock):
                with self.assertRaises(LockBusy):
                    with exclusive_lock(lock):
                        pass


class TransportFailureTests(unittest.TestCase):
    def test_truncated_http_response_is_an_uncertain_request_failure(self):
        transport = JsonTransport({"/api/miniapp/xianxia-dwelling/start": True})
        with patch("urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"")):
            with self.assertRaises(MiniAppError) as caught:
                asyncio.run(transport.__call__("/api/miniapp/xianxia-dwelling/start", {}, mutation=True))
        self.assertEqual((caught.exception.code, caught.exception.uncertain), ("request_failed", True))


if __name__ == "__main__":
    unittest.main()


class DwellingEntryRotationTests(unittest.IsolatedAsyncioTestCase):
    """The pinned 【洞府公共入口】 rotates ~monthly: newest candidate first, rejected tokens skipped."""

    def _bridge(self, candidates, webview_fail=()):
        remembered = []

        class EntryProtocol:
            pinned_cave_entry = None

            async def _dwelling_entry_candidates(self, _client):
                return list(candidates)

            async def _pinned_cave_entry(self, _client):
                raise AssertionError("candidate listing must be used")

            async def _cave_webview(self, _client, entry):
                if entry["token"] in webview_fail:
                    raise RuntimeError("cave_webview_host_not_allowed")
                return f"query_id=TEST&hash=SECRET-{entry['token']}"

            def _remember_entry(self, candidate):
                remembered.append(candidate["token"])

        class Bridge:
            account = "tg9100000001"
            client = object()
            _entry_protocol = EntryProtocol()

        return Bridge(), remembered

    def _transport(self, rejected):
        seen = []

        def post(path, payload, *, mutation):
            seen.append(payload["token"])
            if payload["token"] in rejected:
                raise MiniAppError("dwelling_token_missing")
            return {"identity": {"selectedPlayerId": 9100000001,
                                 "choices": [{"playerId": 9100000001, "daoName": "示例修士一", "source": "personal"}]}}

        return JsonTransport({"/api/miniapp/xianxia-dwelling/start": False}, post=post), seen

    async def test_newest_entry_is_used_and_remembered(self):
        new = {"token": "df_NEW16", "bot_username": "hantianzun16_bot", "source": "pinned"}
        old = {"token": "df_OLD34", "bot_username": "hantianzun34_bot", "source": "notice"}
        bridge, remembered = self._bridge([new, old])
        transport, seen = self._transport(rejected=set())
        session = await open_dwelling(bridge, transport)
        self.assertEqual("df_NEW16", session.entry_token)
        self.assertEqual(["df_NEW16"], seen)
        self.assertEqual(["df_NEW16"], remembered)

    async def test_server_rejected_token_falls_through_to_the_next_candidate(self):
        stale = {"token": "df_STALE", "bot_username": "hantianzun34_bot", "source": "pinned"}
        good = {"token": "df_GOOD", "bot_username": "hantianzun16_bot", "source": "notice"}
        bridge, remembered = self._bridge([stale, good])
        transport, seen = self._transport(rejected={"df_STALE"})
        session = await open_dwelling(bridge, transport)
        self.assertEqual("df_GOOD", session.entry_token)
        self.assertEqual(["df_STALE", "df_GOOD"], seen)
        self.assertEqual(["df_GOOD"], remembered)

    async def test_webview_failure_on_one_candidate_does_not_block_the_rest(self):
        broken = {"token": "df_BROKEN", "bot_username": "hantianzun99_bot", "source": "pinned"}
        good = {"token": "df_GOOD", "bot_username": "hantianzun16_bot", "source": "pinned"}
        bridge, _ = self._bridge([broken, good], webview_fail={"df_BROKEN"})
        transport, seen = self._transport(rejected=set())
        session = await open_dwelling(bridge, transport)
        self.assertEqual("df_GOOD", session.entry_token)
        self.assertEqual(["df_GOOD"], seen)

    async def test_all_candidates_rejected_raises_the_last_server_code(self):
        bridge, _ = self._bridge([{"token": "df_A", "source": "pinned"}, {"token": "df_B", "source": "pinned"}])
        transport, _seen = self._transport(rejected={"df_A", "df_B"})
        with self.assertRaisesRegex(MiniAppError, "dwelling_token_missing"):
            await open_dwelling(bridge, transport)

    async def test_rejected_cached_entry_triggers_one_full_rescan(self):
        passes = []

        class EntryProtocol:
            pinned_cave_entry = None

            async def _dwelling_entry_candidates(self, _client, *, refresh=False):
                passes.append(refresh)
                return [{"token": "df_NEW16", "source": "pinned"}] if refresh else [{"token": "df_OLD34", "source": "cache"}]

            async def _cave_webview(self, _client, entry):
                return f"query_id=TEST&hash=SECRET-{entry['token']}"

        class Bridge:
            account = "tg9100000001"
            client = object()
            _entry_protocol = EntryProtocol()

        transport, seen = self._transport(rejected={"df_OLD34"})
        session = await open_dwelling(Bridge(), transport)
        self.assertEqual(("df_NEW16", [False, True], ["df_OLD34", "df_NEW16"]), (session.entry_token, passes, seen))
        passes.clear()
        transport, _seen = self._transport(rejected={"df_OLD34", "df_NEW16"})
        with self.assertRaisesRegex(MiniAppError, "dwelling_token_missing"):
            await open_dwelling(Bridge(), transport)
        self.assertEqual([False, True], passes)  # one rescan, no loop

    async def test_legacy_single_entry_protocol_still_works(self):
        class EntryProtocol:
            async def _pinned_cave_entry(self, _client):
                return {"token": "df_LEGACY"}

            async def _cave_webview(self, _client, _entry):
                return "query_id=TEST&hash=SECRET"

        class Bridge:
            account = "tg9100000001"
            client = object()
            _entry_protocol = EntryProtocol()

        transport, seen = self._transport(rejected=set())
        session = await open_dwelling(Bridge(), transport)
        self.assertEqual("df_LEGACY", session.entry_token)
        self.assertEqual(["df_LEGACY"], seen)
