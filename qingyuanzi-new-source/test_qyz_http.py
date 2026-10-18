"""Explicit HTTP contract fixtures; no live combat endpoints are called by this suite."""
import importlib
import importlib.util
import json
import unittest
import httpx
import asyncio
from unittest.mock import patch
from qyz_protocol import ProtocolError
from qyz_http import HttpTransport


class HttpContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_retains_timing_only_never_httpcore_request_info(self):
        async def fixture(request):
            trace = request.extensions["trace"]
            for name in ("connection.connect_tcp.started", "connection.connect_tcp.complete",
                         "connection.start_tls.started", "connection.start_tls.complete",
                         "http11.send_request_headers.started", "http11.send_request_body.complete",
                         "http11.receive_response_headers.complete"):
                await trace(name, {"headers": "fixture_credential_do_not_log", "url": "fixture_private_url"})
            return httpx.Response(200, json={"ok": True})
        async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
            await client.post_json("start", {"token": "fixture_secret", "initData": "fixture_init", "playerId": ""})
            rows = client.request_diagnostics
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["connection_reused"])
        self.assertIn("tls_completed_ms", rows[0]["timings"])
        self.assertEqual(rows[0]["pool"], "action")
        self.assertTrue(rows[0]["response_complete"])
        self.assertGreaterEqual(rows[0]["response_wait_ms"], 0)
        self.assertGreaterEqual(rows[0]["response_body_ms"], 0)
        self.assertNotIn("fixture_", json.dumps(rows))

    async def test_diagnostics_keep_the_battle_opening_and_the_latest_rows(self):
        from qyz_http import DIAGNOSTIC_HEAD_ROWS, DIAGNOSTIC_ROWS
        async def fixture(request):
            return httpx.Response(200, json={"ok": True})
        body = {"token": "fixture", "initData": "fixture"}
        async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
            await client.post_json("start", {**body, "playerId": ""})
            for _ in range(DIAGNOSTIC_ROWS + 100):
                await client.post_json("state", body)
            rows = client.request_diagnostics
        self.assertEqual(len(rows), DIAGNOSTIC_ROWS)
        self.assertEqual(rows[0]["phase"], "start")
        self.assertEqual([row["phase"] for row in rows[1:]], ["state"] * (DIAGNOSTIC_ROWS - 1))
        self.assertLess(DIAGNOSTIC_HEAD_ROWS, DIAGNOSTIC_ROWS)

    async def test_partial_read_body_failure_drops_only_that_warm_socket(self):
        for failure in ("timeout", "read_error"):
            with self.subTest(failure=failure):
                class PartialBody(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'{"ok":'
                        if failure == "timeout":
                            await asyncio.sleep(1)
                        raise httpx.ReadError("fixture_private_url")

                failed_once = False
                async def fixture(request):
                    nonlocal failed_once
                    if request.url.path.endswith("/window") and not failed_once:
                        failed_once = True
                        return httpx.Response(200, stream=PartialBody())
                    return httpx.Response(200, json={"ok": True})

                auth = {"token": "fixture", "initData": "fixture"}
                async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
                    self.assertEqual(await client.warm_read_pool(auth), 2)
                    body = {**auth, "challengeId": "c", "afterWindowId": ""}
                    with self.assertRaises(ProtocolError) as caught:
                        await client.post_json("window", body, timeout=.01)
                    self.assertEqual(caught.exception.code, "read_transport_failed")
                    failed = client.request_diagnostics[-1]
                    self.assertEqual(failed["http_status"], 200)
                    self.assertFalse(failed["response_complete"])
                    self.assertEqual(client.cold_read_sockets(), 1)
                    await client.post_json("window", body)
                    self.assertNotEqual(client.request_diagnostics[-1]["read_socket"], failed["read_socket"])
                    self.assertTrue(client.request_diagnostics[-1]["response_complete"])
                    self.assertEqual(await client.warm_read_pool(auth, cold_only=True), 1)
                    self.assertEqual(client.cold_read_sockets(), 0)
                    self.assertNotIn("fixture_", json.dumps(client.request_diagnostics))

    async def test_complete_server_refusal_keeps_transport_warm(self):
        async def fixture(request):
            return httpx.Response(409, json={"ok": False, "error": "boss_window_not_ready"})
        async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
            with self.assertRaises(ProtocolError):
                await client.post_json("window", {"token": "fixture", "initData": "fixture",
                    "challengeId": "c", "afterWindowId": ""})
            self.assertTrue(client.request_diagnostics[-1]["response_complete"])
            self.assertTrue(client.read_socket_warm())

    async def test_observed_outside_window_error_keeps_its_specific_code(self):
        async def fixture(request):
            return httpx.Response(409, json={"ok": False, "error": "boss_hit_outside_window"})
        async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
            with self.assertRaises(ProtocolError) as caught:
                await client.post_json("hit", {"token": "fixture", "initData": "fixture",
                    "challengeId": "c", "windowId": "w", "chargeTicket": "t", "elapsedMs": 1, "holdMs": 1000})
        self.assertEqual(caught.exception.code, "boss_hit_outside_window")

    async def test_expired_charge_keeps_the_official_reason(self):
        async def fixture(request):
            return httpx.Response(409, json={"ok": False, "error": "boss_window_expired"})
        async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
            with self.assertRaises(ProtocolError) as caught:
                await client.post_json("charge-start", {"token": "fixture", "initData": "fixture",
                    "challengeId": "c", "windowId": "w"})
        self.assertEqual(caught.exception.code, "boss_window_expired")
        self.assertFalse(caught.exception.uncertain)

    async def test_short_finish_keeps_the_official_reason(self):
        async def fixture(request):
            return httpx.Response(400, json={"ok": False, "error": "boss_duration_too_short"})
        async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
            with self.assertRaises(ProtocolError) as caught:
                await client.post_json("finish", {
                    "token": "fixture", "initData": "fixture", "bossProof": {"durationMs": 1},
                })
        self.assertEqual(caught.exception.code, "boss_duration_too_short")
        self.assertFalse(caught.exception.uncertain)

    async def test_unknown_server_error_never_echoes_credentials(self):
        async def fixture(request):
            return httpx.Response(400, json={"ok": False, "error": "fixture_session_secret"})
        async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
            with self.assertRaises(ProtocolError) as caught:
                await client.post_json("start", {"token": "fixture_session_secret", "initData": "fixture_init", "playerId": ""})
        self.assertNotIn("fixture_session_secret", str(caught.exception))

    async def test_mutation_connection_loss_is_not_replayed(self):
        calls = []
        async def fixture(request):
            calls.append(request)
            raise httpx.ReadError("fixture_secret_url", request=request)
        async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
            with self.assertRaises(ProtocolError) as caught:
                await client.post_json("charge-start", {"token": "fixture_session", "initData": "fixture_init", "challengeId": "c", "windowId": "w"})
        self.assertEqual(len(calls), 1)
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn("fixture_secret_url", str(caught.exception))

    async def test_overall_deadline_covers_a_slow_response(self):
        calls = []
        async def fixture(request):
            calls.append(request)
            await asyncio.sleep(1)
            return httpx.Response(200, json={"ok": True})
        async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
            with self.assertRaises(ProtocolError) as caught:
                await client.post_json("start", {"token": "fixture_session", "initData": "fixture_init", "playerId": ""}, timeout=0.01)
        self.assertEqual(len(calls), 1)
        self.assertTrue(caught.exception.uncertain)

    async def test_redirects_and_invalid_json_are_unknown_mutations(self):
        for response in [httpx.Response(302, headers={"Location": "https://invalid.example/"}),
                         httpx.Response(200, text="not-json"), httpx.Response(503, json={"ok": False})]:
            with self.subTest(status=response.status_code):
                calls = []
                async def fixture(request):
                    calls.append(request)
                    return response
                async with HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
                    with self.assertRaises(ProtocolError) as caught:
                        await client.post_json("start", {"token": "fixture_session", "initData": "fixture_init", "playerId": ""})
                self.assertEqual(len(calls), 1)
                self.assertTrue(caught.exception.uncertain)

    async def test_real_client_preserves_official_json_contract(self):
        self.assertIsNotNone(importlib.util.find_spec("qyz_http"), "Account-isolated HTTP transport missing")
        qyz = importlib.import_module("qyz_http")
        requests = []
        async def fixture(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, text='window.__QYZ_TURNSTILE_CONFIG__ = {"enabled":true,"siteKey":"fixture_public_key","action":"qyz_world_boss_begin"};')
            return httpx.Response(200, json={"ok": True, "boss": {"roomStatus": "battle"}})
        async with qyz.HttpTransport("tg9100000001", http_transport=httpx.MockTransport(fixture)) as client:
            policy = await client.security_policy()
            self.assertIs(policy["enabled"], True)
            result = await client.post_json("state", {"token":"fixture_session", "initData":"fixture_init"})
            self.assertEqual(result["boss"]["roomStatus"], "battle")
        self.assertEqual(requests[0].url.path, "/miniapp/xianxia-world-boss")
        self.assertEqual(requests[1].url.path, "/api/miniapp/xianxia-world-boss/state")
        self.assertEqual(requests[1].url.host, "asc.aiopenai.app")
        self.assertEqual(json.loads(requests[1].content), {"token":"fixture_session", "initData":"fixture_init"})
        self.assertEqual(requests[1].headers["content-type"], "application/json")


class ConnectionReuseTests(unittest.IsolatedAsyncioTestCase):
    """Use real HTTP/1.1 loopback sockets, including a held-open poll response."""
    async def asyncSetUp(self):
        self.writers = set()
        self.connections = 0
        self.poll_received = asyncio.Event()
        self.release_poll = asyncio.Event()
        self.server = await asyncio.start_server(self.serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.origin = patch("qyz_http.ORIGIN", f"http://127.0.0.1:{port}")
        self.origin.start()
        self.api = HttpTransport("tg9100000001")

    async def asyncTearDown(self):
        self.release_poll.set()
        await self.api.close()
        self.origin.stop()
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
            await writer.wait_closed()

    async def serve(self, reader, writer):
        self.connections += 1
        connection = self.connections
        self.writers.add(writer)
        try:
            while True:
                headers = (await reader.readuntil(b"\r\n\r\n")).decode()
                length = next((int(line.split(":", 1)[1]) for line in headers.splitlines()
                               if line.lower().startswith("content-length:")), 0)
                await reader.readexactly(length)
                if headers.splitlines()[0].split()[1].endswith("/window"):
                    self.poll_received.set()
                    await self.release_poll.wait()
                body = json.dumps({"ok": True, "connection": connection}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()
            await writer.wait_closed()

    async def start(self):
        return await self.api.post_json("start", {"token": "fixture", "initData": "fixture", "playerId": ""})

    async def charge(self):
        return await self.api.post_json("charge-start", {"token": "fixture", "initData": "fixture",
                                                       "challengeId": "c", "windowId": "w"})

    async def test_action_connection_survives_inter_window_idle_gap(self):
        first = await self.start()
        # The old httpx default closed the socket after five idle seconds.
        await asyncio.sleep(5.2)
        later = await self.charge()
        self.assertEqual(first["connection"], later["connection"])

    async def test_polling_cannot_borrow_the_warm_action_connection(self):
        first = await self.start()
        poll = asyncio.create_task(self.api.post_json("window", {"token": "fixture", "initData": "fixture",
                                                     "challengeId": "c", "afterWindowId": ""}))
        try:
            await asyncio.wait_for(self.poll_received.wait(), 1)
            charge = await self.charge()
        finally:
            self.release_poll.set()
            polled = await poll
        self.assertEqual(first["connection"], charge["connection"])
        self.assertNotEqual(polled["connection"], charge["connection"])

    async def test_warm_read_pool_opens_read_sockets_and_clears_cold_failure_counter(self):
        self.api.read_pool_cold_failures = 2
        opened = await self.api.warm_read_pool({"token": "fixture", "initData": "fixture"})
        self.assertEqual(opened, 2)
        self.assertEqual(self.api.read_pool_cold_failures, 0)
        self.assertEqual({row["phase"] for row in self.api.request_diagnostics}, {"state"})
        # Warm-up never touches the action pool.
        first = await self.start()
        self.assertEqual(self.connections, 3)
        self.assertEqual(first["connection"], 3)

    async def test_window_timeout_cancels_real_response_and_leaves_action_pool_usable(self):
        first = await self.start()
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            with self.assertRaises(ProtocolError) as caught:
                await self.api.post_json("window", {"token": "fixture", "initData": "fixture",
                    "challengeId": "c", "afterWindowId": ""}, timeout=.08)
            self.assertEqual(caught.exception.code, "read_transport_failed")
            self.assertFalse(caught.exception.uncertain)
            self.assertLess(loop.time() - started, .4)
            charge = await self.charge()
            self.assertEqual(first["connection"], charge["connection"])
        finally:
            self.release_poll.set()

    async def test_polls_alternate_between_two_warm_read_sockets(self):
        self.release_poll.set()
        self.assertEqual(await self.api.warm_read_pool({"token": "fixture", "initData": "fixture"}), 2)
        body = {"token": "fixture", "initData": "fixture", "challengeId": "c", "afterWindowId": ""}
        used = [(await self.api.post_json("window", dict(body)))["connection"] for _ in range(4)]
        # Both sockets carry polls, so neither idles into a server-side close.
        self.assertEqual(self.connections, 2)
        self.assertNotEqual(used[0], used[1])
        self.assertEqual((used[0], used[1]), (used[2], used[3]))

    async def test_stalled_read_socket_fails_over_to_the_other_warm_socket(self):
        # 2026-09-25 @demo_player_05: a reused read socket stalled, new TLS
        # handshakes failed for six seconds and window 13 expired unrevealed.
        auth = {"token": "fixture", "initData": "fixture"}
        self.assertEqual(await self.api.warm_read_pool(auth), 2)
        body = {"token": "fixture", "initData": "fixture", "challengeId": "c", "afterWindowId": ""}
        with self.assertRaises(ProtocolError) as caught:
            await self.api.post_json("window", dict(body), timeout=.08)
        self.assertEqual(caught.exception.code, "read_transport_failed")
        self.assertEqual(self.api.cold_read_sockets(), 1)
        self.assertTrue(self.api.read_socket_warm())
        self.release_poll.set()
        for _ in range(2):
            await self.api.post_json("window", dict(body), timeout=.5)
            # Polls ride the other warm socket: no handshake, no new connection.
            self.assertIs(self.api.request_diagnostics[-1]["connection_reused"], True)
        self.assertEqual(self.connections, 2)
        # Re-warming touches only the failed socket.
        self.assertEqual(await self.api.warm_read_pool(auth, cold_only=True), 1)
        self.assertEqual(self.api.cold_read_sockets(), 0)
        self.assertEqual(self.connections, 3)


if __name__ == "__main__":
    unittest.main()
