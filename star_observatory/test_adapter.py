import unittest

from miniapp_shared.dwelling import DWELLING_EXTERNAL, DWELLING_START, JsonTransport, MiniAppError
from star_observatory.adapter import ALLOWED_PATHS, FARM_ACTION, FARM_START, StarAccountSession, official_transport


class _EntryProtocol:
    async def _pinned_cave_entry(self, _client):
        return {"token": "df_PARENT"}

    async def _cave_webview(self, _client, _entry):
        return "query_id=SAFE_TEST&hash=SECRET"


class _Bridge:
    account = "tg9100000004"
    client = object()
    _entry_protocol = _EntryProtocol()


class StarAccountSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_transport_rejects_unknown_endpoints_before_network(self):
        with self.assertRaisesRegex(MiniAppError, "endpoint_not_allowed"):
            await official_transport()("/api/miniapp/xianxia-sect-farm/reset", {}, mutation=True)
        with self.assertRaisesRegex(MiniAppError, "endpoint_not_allowed"):
            await official_transport()(FARM_START, {}, mutation=True)
        self.assertTrue(ALLOWED_PATHS[FARM_ACTION])

    async def test_prepare_and_action_use_only_official_request_shapes(self):
        calls = []

        def post(path, payload, *, mutation):
            calls.append((path, dict(payload), mutation))
            if path == DWELLING_START:
                return {"identity": {"selectedPlayerId": 9100000004, "choices": [
                    {"playerId": 9100000004, "daoName": "竹衍衡", "source": "personal"}]}}
            if path == DWELLING_EXTERNAL:
                return {"url": "/miniapp/xianxia-sect-farm?startapp=farm_CHILD123"}
            if path == FARM_START:
                return {"domain": {"mode": "stars", "plots": [{"key": "1", "empty": True}]}}
            return {"actionResult": {"ok": True}, "domain": {"mode": "stars", "plots": []}}

        session = StarAccountSession(_Bridge(), JsonTransport(ALLOWED_PATHS, post=post))
        start = await session.prepare()
        await session.action({"action": "pull", "plotKey": "1", "starName": "天雷星"})

        self.assertEqual("stars", start["domain"]["mode"])
        self.assertEqual([(DWELLING_START, False), (DWELLING_EXTERNAL, False), (FARM_START, False),
                          (FARM_ACTION, True)], [(path, mutation) for path, _payload, mutation in calls])
        self.assertEqual({"token": "df_PARENT", "initData": "query_id=SAFE_TEST&hash=SECRET",
                          "playerId": 9100000004, "action": "sect_farm"}, calls[1][1])
        self.assertEqual({"token": "farm_CHILD123", "initData": "query_id=SAFE_TEST&hash=SECRET",
                          "action": "pull", "plotKey": "1", "starName": "天雷星"}, calls[3][1])

    async def test_only_tianlei_can_be_pulled_and_identity_must_match_account(self):
        def post(path, payload, *, mutation):
            if path == DWELLING_START:
                return {"identity": {"choices": [{"playerId": 9100000004, "daoName": "x", "source": "personal"}]}}
            if path == DWELLING_EXTERNAL:
                return {"url": "/miniapp/xianxia-sect-farm?startapp=farm_CHILD123"}
            return {"domain": {"mode": "stars", "plots": [{"key": "1", "empty": True}]}}

        session = StarAccountSession(_Bridge(), JsonTransport(ALLOWED_PATHS, post=post))
        await session.prepare()
        with self.assertRaisesRegex(MiniAppError, "star_not_allowed"):
            await session.action({"action": "pull", "plotKey": "1", "starName": "帝魂星"})
        with self.assertRaisesRegex(MiniAppError, "action_not_allowed"):
            await session.action({"action": "reset", "plotKey": "1"})

        class OtherBridge(_Bridge):
            account = "tg9100000005"

        with self.assertRaisesRegex(MiniAppError, "identity_mismatch"):
            await StarAccountSession(OtherBridge(), JsonTransport(ALLOWED_PATHS, post=post)).prepare()


if __name__ == "__main__":
    unittest.main()
