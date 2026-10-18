import unittest

from miniapp_shared.dwelling import DWELLING_EXTERNAL, DwellingSession, JsonTransport, MiniAppError
from tianji_trial.runtime import Identity
from tower_miniapp.adapter import (
    ALLOWED_PATHS,
    PAGODA_CHALLENGE,
    PAGODA_START,
    TowerIdentityClient,
    official_transport,
)


def _dwelling():
    return DwellingSession("tg9100000001", "query_id=TEST&hash=SECRET", "df_PARENT", {},
                           (Identity(9100000001, "示例修士一", "personal"), Identity(9200000001, "示例绑定角色", "bound_character")))


class TowerAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_endpoint_and_wrong_mutation_flag_are_not_executable(self):
        with self.assertRaisesRegex(MiniAppError, "endpoint_not_allowed"):
            await official_transport()("/api/miniapp/xianxia-pagoda/reset", {}, mutation=True)
        with self.assertRaisesRegex(MiniAppError, "endpoint_not_allowed"):
            await official_transport()(PAGODA_CHALLENGE, {}, mutation=False)

    async def test_start_and_challenge_use_identity_scoped_child_token(self):
        calls = []

        def post(path, payload, *, mutation):
            calls.append((path, dict(payload), mutation))
            if path == DWELLING_EXTERNAL:
                return {"url": f"/miniapp/xianxia-pagoda?startapp=pagoda_CHILD{payload['playerId']}"}
            if path == PAGODA_START:
                return {"state": {"canChallenge": True}}
            return {"state": {"canChallenge": False, "todayHighest": 8}, "replay": {"clearedCount": 8}}

        client = TowerIdentityClient(JsonTransport(ALLOWED_PATHS, post=post), _dwelling(), 9200000001)
        start = await client.start()
        result = await client.challenge()

        self.assertTrue(start["state"]["canChallenge"])
        self.assertEqual(8, result["replay"]["clearedCount"])
        self.assertEqual([(DWELLING_EXTERNAL, False), (PAGODA_START, False), (PAGODA_CHALLENGE, True)],
                         [(path, mutation) for path, _payload, mutation in calls])
        self.assertEqual({"token": "df_PARENT", "initData": "query_id=TEST&hash=SECRET", "playerId": 9200000001,
                          "action": "pagoda"}, calls[0][1])
        self.assertEqual({"token": "pagoda_CHILD9200000001", "initData": "query_id=TEST&hash=SECRET"}, calls[2][1])

    async def test_start_refreshes_token_once_on_auth_expiry_but_challenge_never_replays(self):
        minted = []
        starts = []

        def post(path, payload, *, mutation):
            if path == DWELLING_EXTERNAL:
                minted.append(payload["playerId"])
                return {"url": "/miniapp/xianxia-pagoda?startapp=pagoda_CHILD"}
            if path == PAGODA_START:
                starts.append(1)
                if len(starts) == 1:
                    raise MiniAppError("token_expired")
                return {"state": {"canChallenge": True}}
            raise MiniAppError("request_failed", uncertain=True)

        client = TowerIdentityClient(JsonTransport(ALLOWED_PATHS, post=post), _dwelling(), 9100000001)
        self.assertTrue((await client.start())["state"]["canChallenge"])
        self.assertEqual([9100000001, 9100000001], minted)
        with self.assertRaises(MiniAppError) as ctx:
            await client.challenge()
        self.assertTrue(ctx.exception.uncertain)
        self.assertEqual(2, len(minted))


if __name__ == "__main__":
    unittest.main()
