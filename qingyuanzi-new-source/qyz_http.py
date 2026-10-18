"""Account-owned HTTPS pools; polling cannot borrow a warm combat connection."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import random
import time
from typing import Any

import httpx

from qyz_protocol import ProtocolError
from xiuxian_humanize import shuffle_headers

ORIGIN = "https://asc.aiopenai.app"
PAGE_PATH = "/miniapp/xianxia-world-boss"
API_PREFIX = "/api/miniapp/xianxia-world-boss/"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 128 * 1024
KEEPALIVE_SECONDS = 60.0
# One transport serves one battle, which polls /window about 900 times. Keep
# its opening (start polls, begin, first charges) and the most recent rows.
DIAGNOSTIC_ROWS = 512
DIAGNOSTIC_HEAD_ROWS = 64
ENDPOINT_KEYS = {
    "start": frozenset({"token", "initData", "playerId"}),
    "state": frozenset({"token", "initData"}),
    "ws-ticket": frozenset({"token", "initData"}),
    "begin": frozenset({"token", "initData", "challengeId", "turnstileToken", "turnstileIdempotencyKey"}),
    "window": frozenset({"token", "initData", "challengeId", "afterWindowId"}),
    "charge-start": frozenset({"token", "initData", "challengeId", "windowId"}),
    "hit": frozenset({"token", "initData", "challengeId", "windowId", "chargeTicket", "elapsedMs", "holdMs"}),
    "finish": frozenset({"token", "initData", "bossProof"}),
}
READ_ENDPOINTS = frozenset({"state", "window", "ws-ticket"})
# Only protocol codes already observed in the official client may reach logs.
# A syntactically valid arbitrary server string can still contain a credential.
SERVER_ERROR_CODES = frozenset({
    "api_timeout", "api_unreachable", "request_failed", "bad_response", "server_busy",
    "rate_limited", "server_error", "boss_token_missing", "boss_token_expired", "boss_token_used",
    "boss_battle_not_started", "boss_challenge_expired", "boss_event_closed", "boss_action_limit",
    "boss_join_closed", "boss_not_enough_participants", "boss_window_not_ready",
    "boss_hit_outside_window", "boss_window_expired", "boss_duration_too_short", "boss_client_clock_mismatch",
    "boss_charge_ticket_invalid", "turnstile_required", "turnstile_failed", "turnstile_unavailable",
})


class HttpTransport:
    def __init__(self, account: str, *, http_transport: httpx.AsyncBaseTransport | None = None):
        if not re.fullmatch(r"tg\d{6,16}", account):
            raise ProtocolError("account_invalid")
        self.account = account
        self.request_diagnostics: list[dict[str, Any]] = []
        header_rng = random.Random()

        async def randomize_header_order(request: httpx.Request) -> None:
            # Run after httpx merges defaults and adds content headers. Passing
            # reordered headers to stream() alone would retain default ordering.
            request.headers = httpx.Headers(shuffle_headers(dict(request.headers), rng=header_rng))

        def client(connections: int) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url=ORIGIN, transport=http_transport,
                timeout=httpx.Timeout(8), follow_redirects=False, trust_env=False,
                event_hooks={"request": [randomize_header_order]},
                limits=httpx.Limits(max_connections=connections, max_keepalive_connections=connections,
                                   keepalive_expiry=KEEPALIVE_SECONDS),
                headers={"Accept": "application/json", "Cache-Control": "no-store",
                         "Origin": ORIGIN, "Referer": ORIGIN + PAGE_PATH,
                         "User-Agent": "Mozilla/5.0 Telegram-Android/11.0"},
            )
        # /start and /begin warm the action pool. The old shared pool let a
        # /window poll occupy it and expired idle action sockets after five
        # seconds, adding a TLS handshake to a timed charge or strike.
        # Keep the same total six-connection ceiling per account, and preserve
        # concurrent strikes when an earlier response is still pending.
        self._client = client(4)
        # Two single-socket read clients that polls alternate between, so both
        # stay warm. On 2026-09-25 13:42:16 a reused read socket of @demo_player_05
        # stalled, then every new TLS handshake failed for six seconds while
        # already-open sockets answered in ~300 ms; the one-client pool had no
        # second warm socket and window 13 expired unrevealed.
        self._read_clients = (client(1), client(1))
        self._read_warm = [False, False]
        self._read_busy = [0, 0]
        self._read_turn = 0
        # Consecutive read-pool failures whose connection never finished its
        # handshake. A 1-second poll can never succeed on a cold TLS socket when
        # the round trip is elevated, so the owner widens only those polls.
        self.read_pool_cold_failures = 0

    async def __aenter__(self) -> HttpTransport:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            await self._client.aclose()
        finally:
            for client in self._read_clients:
                await client.aclose()

    def read_socket_warm(self) -> bool:
        """Whether the next poll can use a read socket whose last request was answered."""
        return self.cold_read_sockets() < len(self._read_clients)

    def cold_read_sockets(self) -> int:
        return sum(not warm for warm in self._read_warm)

    def _pick_read_socket(self) -> int:
        # Alternate for keep-alive; prefer an idle warm socket, never queue a
        # poll behind an in-flight re-warm when the other socket is free.
        order = [(self._read_turn + step) % len(self._read_clients) for step in range(len(self._read_clients))]
        self._read_turn = (self._read_turn + 1) % len(self._read_clients)
        return min(order, key=lambda index: (self._read_busy[index] > 0, not self._read_warm[index]))

    async def _request(self, method: str, path: str, *, phase: str,
                       payload: dict[str, Any] | None = None, timeout: float = 8,
                       read_socket: int | None = None) -> tuple[int, bytes]:
        mutable = method == "POST" and phase not in READ_ENDPOINTS
        if not 0 < timeout <= 30:
            raise ProtocolError("request_budget_invalid", phase=phase)
        pool = "read" if phase in READ_ENDPOINTS else "action"
        diagnostic: dict[str, Any] = {"phase": phase, "pool": pool, "http_status": 0,
                                      "response_complete": False}
        if pool == "read":
            if read_socket is None:
                read_socket = self._pick_read_socket()
            client = self._read_clients[read_socket]
            diagnostic["read_socket"] = read_socket
        else:
            client = self._client
        started = time.monotonic()
        if payload and isinstance(payload.get("windowId"), str):
            diagnostic["window_fingerprint"] = hashlib.sha256(payload["windowId"].encode()).hexdigest()
        timings: dict[str, float] = {}
        trace_fields = {
            "connect_tcp.started": "connect_started_ms", "connect_tcp.complete": "connect_completed_ms",
            "start_tls.started": "tls_started_ms", "start_tls.complete": "tls_completed_ms",
            "send_request_headers.started": "headers_started_ms",
            "send_request_body.complete": "body_completed_ms",
            "receive_response_headers.complete": "response_headers_ms",
        }
        async def trace(name: str, _info: dict[str, Any]) -> None:
            # Never retain httpcore's info: it can contain headers, credentials,
            # URLs or exceptions. Only known event names and elapsed times.
            field = trace_fields.get(name.partition(".")[2])
            if field:
                timings[field] = round((time.monotonic() - started) * 1000, 3)
        if pool == "read":
            self._read_busy[read_socket] += 1
        try:
            async with asyncio.timeout(timeout):
                async with client.stream(method, path, json=payload, timeout=timeout,
                                         extensions={"trace": trace}) as response:
                    diagnostic.update(http_status=response.status_code, http_version=response.http_version)
                    if response.is_redirect:
                        raise ProtocolError("redirect_rejected", phase=phase, uncertain=mutable, http_status=response.status_code)
                    pieces = []
                    size = 0
                    async for block in response.aiter_bytes():
                        size += len(block)
                        if size > MAX_RESPONSE_BYTES:
                            raise ProtocolError("response_too_large", phase=phase, uncertain=mutable)
                        pieces.append(block)
                    diagnostic["response_complete"] = True
                    return response.status_code, b"".join(pieces)
        except ProtocolError:
            raise
        except (TimeoutError, httpx.HTTPError):
            # The client does not know whether a mutating request reached the server.
            # This transport never retries by itself. The owning runtime
            # retries the same request within its applicable action window.
            raise ProtocolError("transport_outcome_unknown" if mutable else "read_transport_failed",
                                phase=phase, uncertain=mutable) from None
        finally:
            diagnostic.update(duration_ms=round((time.monotonic() - started) * 1000, 3), timings=timings)
            if "response_headers_ms" in timings and "body_completed_ms" in timings:
                diagnostic["response_wait_ms"] = round(max(0.0, timings["response_headers_ms"] - timings["body_completed_ms"]), 3)
            if "response_headers_ms" in timings:
                diagnostic["response_body_ms"] = round(max(0.0, diagnostic["duration_ms"] - timings["response_headers_ms"]), 3)
            if "headers_started_ms" in timings:
                diagnostic["connection_reused"] = "connect_started_ms" not in timings
            if pool == "read":
                self._read_busy[read_socket] -= 1
                # Receiving headers does not keep an aborted body stream alive.
                # HTTPX closes that connection; fail over to the other warm socket
                # and let the existing cold-only rewarmer repair it off-path.
                self._read_warm[read_socket] = diagnostic["response_complete"]
                if not diagnostic["response_complete"] and "connect_started_ms" in timings:
                    self.read_pool_cold_failures += 1
                    diagnostic["cold_failure"] = True
                elif diagnostic["response_complete"]:
                    self.read_pool_cold_failures = 0
            self.request_diagnostics.append(diagnostic)
            if len(self.request_diagnostics) > DIAGNOSTIC_ROWS:
                del self.request_diagnostics[DIAGNOSTIC_HEAD_ROWS]

    async def warm_read_pool(self, auth: dict[str, Any], *, connections: int = 2, timeout: float = 4,
                             cold_only: bool = False) -> int:
        """Open the read-pool TLS sockets with harmless ``/state`` reads.

        Before the battle every window poll runs on a cold connection; on
        2026-09-20 each cold poll needed 1.0-1.6 s and the 1-second budget
        cancelled all of them (25 failures, ``boss_windows_invalid``). Warming
        is read-only, ignores errors and never delays the caller's battle clock.
        ``cold_only`` re-opens just the sockets whose last read failed, off the
        poll path, while polls continue on the other one.
        """
        body = {"token": auth.get("token"), "initData": auth.get("initData")}
        if not all(isinstance(body[key], str) and body[key] for key in body):
            return 0
        async def one(index: int) -> bool:
            try:
                await self._post_json("state", dict(body), timeout=timeout, read_socket=index)
                return True
            except Exception:  # noqa: BLE001 - warm-up is best effort
                return False
        sockets = [index for index in range(len(self._read_clients))
                   if not (cold_only and self._read_warm[index]) and self._read_busy[index] == 0]
        results = await asyncio.gather(*(one(index) for index in sockets[:max(1, connections)]))
        return sum(1 for ok in results if ok)

    async def security_policy(self) -> dict[str, Any]:
        status, content = await self._request("GET", PAGE_PATH, phase="security_policy", timeout=20)
        if status != 200:
            raise ProtocolError("official_security_policy_unavailable", phase="preflight", http_status=status)
        try:
            page = content.decode("utf-8")
            matches = re.findall(r"window\.__QYZ_TURNSTILE_CONFIG__\s*=\s*(\{[^;]+\})\s*;", page)
            if len(matches) != 1:
                raise ValueError("configuration must be unique")
            policy = json.loads(matches[0])
            if not isinstance(policy, dict) or not isinstance(policy.get("enabled"), bool):
                raise ValueError("explicit boolean required")
            if policy["enabled"] and (not policy.get("siteKey") or policy.get("action") != "qyz_world_boss_begin"):
                raise ValueError("unknown widget contract")
        except (ValueError, UnicodeError):
            raise ProtocolError("official_security_policy_invalid", phase="preflight") from None
        return {"enabled": policy["enabled"], "source": "official_page",
                "page_sha256": hashlib.sha256(content).hexdigest()}

    async def post_json(self, endpoint: str, body: dict[str, Any], *, timeout: float = 8) -> dict[str, Any]:
        return await self._post_json(endpoint, body, timeout=timeout)

    async def _post_json(self, endpoint: str, body: dict[str, Any], *, timeout: float = 8,
                         read_socket: int | None = None) -> dict[str, Any]:
        optional = {"turnstileToken", "turnstileIdempotencyKey"} if endpoint == "begin" else set()
        if (endpoint not in ENDPOINT_KEYS or not isinstance(body, dict)
                or not ENDPOINT_KEYS[endpoint] - optional <= set(body) <= ENDPOINT_KEYS[endpoint]
                or (endpoint == "begin" and bool("turnstileToken" in body) != bool("turnstileIdempotencyKey" in body))):
            raise ProtocolError("endpoint_or_payload_rejected", phase="preflight")
        for key in ("token", "initData"):
            if not isinstance(body.get(key), str) or not 1 <= len(body[key]) <= 16384:
                raise ProtocolError("authentication_invalid", phase="preflight")
        try:
            encoded = json.dumps(body, allow_nan=False).encode()
        except (ValueError, TypeError):
            raise ProtocolError("payload_invalid", phase=endpoint) from None
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ProtocolError("payload_too_large", phase=endpoint)
        status, content = await self._request("POST", API_PREFIX + endpoint, phase=endpoint, payload=body,
                                              timeout=timeout, read_socket=read_socket)
        try:
            data = json.loads(content)
        except (ValueError, UnicodeError):
            raise ProtocolError("response_invalid", phase=endpoint, uncertain=endpoint not in READ_ENDPOINTS, http_status=status) from None
        if not isinstance(data, dict):
            raise ProtocolError("response_invalid", phase=endpoint, uncertain=endpoint not in READ_ENDPOINTS, http_status=status)
        if status != 200 or data.get("ok") is not True:
            code = data.get("error")
            if not isinstance(code, str) or code not in SERVER_ERROR_CODES:
                code = "server_response_rejected"
            error = ProtocolError(code, phase=endpoint, uncertain=status >= 500 and endpoint not in READ_ENDPOINTS,
                                  http_status=status)
            from qyz_reference.world_boss_features import _diagnostic_value
            error.details = _diagnostic_value(data)
            raise error
        return data
