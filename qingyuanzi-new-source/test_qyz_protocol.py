"""Reference integration fixtures are synthetic, never live server evidence."""
import asyncio
import heapq
import itertools
import json
import unittest
from types import SimpleNamespace

from qyz_protocol import ProtocolError, ProtocolRuntime, WebViewAuth
from qyz_verification import MemoryTurnstileBroker


class FakeClock:
    """Advance concurrent sleeps by deadline, like a monotonic event loop."""
    def __init__(self):
        self.value = 100.0
        self.pending = []
        self.ids = itertools.count()
        self.handle = None
    def now(self):
        return self.value
    async def sleep(self, seconds):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        heapq.heappush(self.pending, (self.value + max(0, seconds), next(self.ids), future))
        if self.handle is None:
            self.handle = loop.call_later(0.00001, self.advance)
        await future
    def advance(self):
        self.handle = None
        # Drain runnable callbacks (including cancelled/replanned waiters)
        # before moving virtual time. A nested asyncio.wait may need several
        # loop turns to register its timer; wall-clock microseconds are not
        # evidence that all runnable work has finished.
        loop = asyncio.get_running_loop()
        if loop._ready:
            self.handle = loop.call_soon(self.advance)
            return
        while self.pending and self.pending[0][2].done():
            heapq.heappop(self.pending)
        if not self.pending:
            return
        self.value = max(self.value, self.pending[0][0])
        while self.pending and self.pending[0][0] <= self.value:
            _, _, future = heapq.heappop(self.pending)
            if not future.done():
                future.set_result(None)
        if self.pending:
            self.handle = asyncio.get_running_loop().call_later(0.00001, self.advance)


class BattleFixture:
    """Official-shaped contract fixture with dynamic, empty and premature-done reveals."""
    account = "tg9100000001"
    def __init__(self, clock, *, count=16, security_required=False):
        self.clock = clock
        self.count = count
        self.security_required = security_required
        self.calls = []
        self.reveals = 0
        self.hit_payloads = []
        self.finish_payload = None
        self.windows = [{"id": f"fixture_w_{i}", "centerMs": 2200+i*3200, "hitMs": 560, "perfectMs": 180} for i in range(count)]
    async def security_policy(self):
        return {"enabled": self.security_required, "source": "official_page_fixture"}
    async def post_json(self, endpoint, body, *, timeout=8):
        self.calls.append(endpoint)
        await self.clock.sleep(0.01)
        if endpoint == "start":
            return {"ok": True, "sessionToken": "fixture_session_secret", "boss": {"roomStatus": "battle", "actionLimit": 1, "actionsRemaining": 1}, "player": {"maxHp": 100}, "challenge": {"challengeId": "fixture_challenge", "windowCount": self.count, "windows": [], "durationMs": 60000, "minDurationMs": 42000, "maxDurationMs": 72000}}
        if endpoint == "begin":
            return {"ok": True, "startsInMs": 0}
        if endpoint == "window":
            self.reveals += 1
            if self.reveals in (1, 2):
                return {"ok": True, "window": None, "done": self.reveals == 2, "windowCount": self.count}
            i = self.reveals-3
            if i >= self.count:
                return {"ok": True, "window": None, "done": True, "windowCount": self.count}
            return {"ok": True, "window": dict(self.windows[i]), "done": i == self.count-1, "windowCount": self.count}
        if endpoint == "charge-start":
            return {"ok": True, "chargeTicket": "fixture_charge_"+body["windowId"]}
        if endpoint == "hit":
            self.hit_payloads.append(dict(body))
            window = next(w for w in self.windows if w["id"] == body["windowId"])
            perfect = (abs(body["elapsedMs"] - window["centerMs"]) <= window["perfectMs"]
                       and 520 <= body["holdMs"] <= 1250)
            return {"ok": True, "hit": {"windowId": body["windowId"], "perfect": perfect, "damageYi": 1}, "boss": {"roomStatus": "battle"}}
        if endpoint == "finish":
            self.finish_payload = dict(body)
            return {"ok": True, "result": {"grade": "甲等", "score": 100, "player_hp": 100}}
        raise AssertionError("Unexpected fixture endpoint: "+endpoint)


class CallbackFixtureBroker(MemoryTurnstileBroker):
    """A deterministic browser stand-in used only by offline protocol tests."""
    def __init__(self):
        super().__init__()
        self.generated = []
    def create_request(self, **kwargs):
        row = super().create_request(**kwargs)
        token = 'fixture_cf_' + kwargs['account'] + '_' + str(len(self.generated))
        self.generated.append((row['request_id'], token, kwargs['account']))
        self.record_browser_event(row['request_id'], 'token_generated', source='automatic')
        self.submit_token(row['request_id'], token)
        return row


class WindowLatencyAttributionTests(unittest.TestCase):
    def test_late_charge_reply_is_not_reported_as_scheduler_lateness(self):
        runner = ProtocolRuntime(account='tg9100000001', username='fixture', transport=SimpleNamespace())
        runner.combat.windows['fixture_window'] = {'centerMs': 86200, 'hitMs': 620, 'perfectMs': 210}
        row = runner._window_state('fixture_window', 'awaiting_hit_reply',
            release_target_ms=86041, charge_confirmed_ms=86333, release_ms=86339)
        self.assertEqual(row['charge_reply_block_ms'], 292)
        self.assertEqual(row['release_after_ready_ms'], 6)

    def test_early_charge_leaves_only_actual_release_delay(self):
        runner = ProtocolRuntime(account='tg9100000001', username='fixture', transport=SimpleNamespace())
        runner.combat.windows['fixture_window'] = {'centerMs': 1200, 'hitMs': 620, 'perfectMs': 210}
        row = runner._window_state('fixture_window', 'awaiting_hit_reply',
            release_target_ms=1000, charge_confirmed_ms=600, release_ms=1004)
        self.assertEqual(row['charge_reply_block_ms'], 0)
        self.assertEqual(row['release_after_ready_ms'], 4)

    def test_missing_charge_evidence_does_not_invent_a_breakdown(self):
        runner = ProtocolRuntime(account='tg9100000001', username='fixture', transport=SimpleNamespace())
        runner.combat.windows['fixture_window'] = {'centerMs': 1200, 'hitMs': 620, 'perfectMs': 210}
        row = runner._window_state('fixture_window', 'awaiting_hit_reply', release_target_ms=1000, release_ms=1004)
        self.assertNotIn('charge_reply_block_ms', row)
        self.assertNotIn('release_after_ready_ms', row)


class ProtocolLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, api, clock, *, broker=None, trace=None, actor=None):
        runner = ProtocolRuntime(account=api.account, username='fixture', transport=api,
                                 monotonic=clock.now, sleep=clock.sleep, broker=broker, trace=trace, actor=actor)
        return await runner.run(WebViewAuth(api.account, 'fixture_init', 'qyz_fixture_entry', 'a'*64))

    async def test_all_dynamic_windows_require_server_hits_and_finish(self):
        for count in (3, 16, 19):
            with self.subTest(count=count):
                clock = FakeClock()
                api = BattleFixture(clock, count=count)
                result = await self.run_fixture(api, clock)
                self.assertEqual(result['status'], 'settled_perfect')
                self.assertEqual((result['expected_windows'], result['accepted_hits'], result['server_perfects']), (count,)*3)
                self.assertTrue(result['finish_confirmed'])
                self.assertEqual(api.calls.count('charge-start'), count)
                self.assertEqual(api.calls.count('finish'), 1)
                self.assertEqual(len(api.finish_payload['bossProof']['actions']), count)
                self.assertEqual(len({row['windowId'] for row in api.hit_payloads}), count)
                self.assertTrue(all(520 <= row['holdMs'] <= 1250 for row in api.hit_payloads))

    async def test_charges_aim_at_the_longer_server_hold_inside_the_perfect_band(self):
        from qyz_timing import TARGET_SERVER_HOLD_MS
        clock = FakeClock()
        api = BattleFixture(clock, count=3)
        result = await self.run_fixture(api, clock)
        self.assertEqual(result['server_perfects'], 3)
        self.assertEqual(TARGET_SERVER_HOLD_MS, 1150)
        holds = [row['holdMs'] for row in api.hit_payloads]
        self.assertTrue(all(1130 <= hold <= 1170 for hold in holds), holds)

    async def test_expired_reveal_records_counter_damage_and_resets_combo_in_finish(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=3)
        original = api.post_json
        trace = []
        async def request(endpoint, body, **options):
            data = await original(endpoint, body, **options)
            if endpoint == 'window' and (data.get('window') or {}).get('id') == api.windows[1]['id']:
                await clock.sleep(max(0, 106.2 - clock.now()))
            if endpoint == 'finish':
                data['result']['player_hp'] = body['bossProof']['playerHp']
            return data
        api.post_json = request
        result = await self.run_fixture(api, clock, trace=trace.append)
        proof = api.finish_payload['bossProof']
        self.assertEqual(proof['playerHp'], 84)
        self.assertFalse(proof['dead'])
        self.assertEqual((proof['clientStats']['combo'], proof['clientStats']['bestCombo']), (1, 1))
        self.assertEqual(result['accepted_hits'], 2)
        self.assertEqual(result['local_player_hp'], 84)
        self.assertEqual(len([row for row in trace if row['phase'] == 'window_missed']), 1)

    async def test_four_phase_three_misses_stop_at_local_death_without_global_boss_stop(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=16)
        original = api.post_json
        async def request(endpoint, body, **options):
            data = await original(endpoint, body, **options)
            if endpoint == 'start':
                data['challenge']['phase'] = 3
            if endpoint == 'window' and data.get('window'):
                deadline = 100.02 + (data['window']['centerMs'] + data['window']['hitMs'] + 200) / 1000
                await clock.sleep(max(0, deadline - clock.now()))
            if endpoint == 'finish':
                data['result']['player_hp'] = body['bossProof']['playerHp']
            return data
        api.post_json = request
        result = await self.run_fixture(api, clock)
        proof = api.finish_payload['bossProof']
        self.assertEqual(proof['playerHp'], 0)
        self.assertTrue(proof['dead'])
        self.assertTrue(result['local_dead'])
        self.assertEqual(result['accepted_hits'], 0)
        self.assertEqual(api.calls.count('finish'), 1)
        # Death stops strikes, never the clock: no charge or hit after it, but
        # /finish still waits for the real schedule so the proof's duration is
        # the elapsed time the server also measured (2026-09-22 clock mismatch).
        self.assertEqual(api.calls.count('hit'), 0)
        last_end = max(w['centerMs'] + w['hitMs'] for w in api.windows)
        self.assertGreaterEqual(proof['durationMs'], last_end)
        self.assertGreaterEqual(clock.now() - 100.0, last_end / 1000)
        self.assertFalse(result['reference_outcome']['diagnostics']['strategy']['boss_defeated'])

    async def test_required_verification_uses_initial_probe_then_real_callback_contract(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=3, security_required=True)
        broker = CallbackFixtureBroker()
        original = api.post_json
        begins = []
        async def request(endpoint, body, **options):
            if endpoint == 'begin':
                begins.append(dict(body))
                if not body.get('turnstileToken'):
                    raise ProtocolError('turnstile_required', phase='begin', http_status=403)
                self.assertEqual(body['turnstileToken'], broker.generated[-1][1])
            return await original(endpoint, body, **options)
        api.post_json = request
        result = await self.run_fixture(api, clock, broker=broker)
        self.assertEqual(result['status'], 'settled_perfect')
        self.assertEqual(len(begins), 2)
        self.assertNotIn('turnstileToken', begins[0])
        self.assertTrue(begins[1]['turnstileIdempotencyKey'])
        self.assertEqual(broker.get_request(broker.generated[0][0])['status'], 'accepted')
        self.assertNotIn(broker.generated[0][1], json.dumps(result))
        self.assertFalse(broker._tokens)

    async def test_rejected_or_uncertain_cf_token_is_replaced_and_never_replayed(self):
        for code, status in [('turnstile_failed', 403), ('api_timeout', 503)]:
            with self.subTest(code=code):
                clock = FakeClock()
                api = BattleFixture(clock, count=1)
                broker = CallbackFixtureBroker()
                original = api.post_json
                attempts = []
                async def request(endpoint, body, **options):
                    if endpoint == 'begin':
                        attempts.append(dict(body))
                        if len(attempts) == 1:
                            raise ProtocolError('turnstile_required', http_status=403)
                        if len(attempts) == 2:
                            raise ProtocolError(code, http_status=status, uncertain=status>=500)
                    return await original(endpoint, body, **options)
                api.post_json = request
                result = await self.run_fixture(api, clock, broker=broker)
                self.assertTrue(result['finish_confirmed'])
                self.assertEqual(len(attempts), 3)
                self.assertNotEqual(attempts[1]['turnstileToken'], attempts[2]['turnstileToken'])
                self.assertNotEqual(attempts[1]['turnstileIdempotencyKey'], attempts[2]['turnstileIdempotencyKey'])
                rows = [broker.get_request(x[0]) for x in broker.generated]
                self.assertEqual([row['status'] for row in rows], ['rejected','accepted'])

    async def test_six_concurrent_accounts_keep_their_own_auth_and_receipts(self):
        accounts = ['tg9100000001','tg9100000002','tg9100000003','tg9100000004','tg9100000005','tg9100000006']
        clock = FakeClock()
        broker = CallbackFixtureBroker()
        apis = []
        async def run(account):
            api = BattleFixture(clock)
            api.account = account
            apis.append(api)
            original = api.post_json
            async def request(endpoint, body, **options):
                self.assertEqual(body['initData'], 'fixture_init_'+account)
                if endpoint == 'begin':
                    if not body.get('turnstileToken'):
                        raise ProtocolError('turnstile_required', http_status=403)
                    self.assertTrue(body['turnstileToken'].startswith('fixture_cf_'+account+'_'))
                response = await original(endpoint, body, **options)
                if endpoint == 'start':
                    response['sessionToken'] = 'fixture_session_'+account
                elif endpoint != 'start':
                    self.assertEqual(body['token'], 'fixture_session_'+account)
                return response
            api.post_json = request
            runner = ProtocolRuntime(account=account, username='fixture', transport=api,
                                     monotonic=clock.now, sleep=clock.sleep, broker=broker)
            return await runner.run(WebViewAuth(account,'fixture_init_'+account,'qyz_fixture_'+account,account))
        results = await asyncio.gather(*(run(account) for account in accounts))
        self.assertEqual({row['account'] for row in results}, set(accounts))
        self.assertTrue(all(row['status']=='settled_perfect' and row['accepted_hits']==16 for row in results))
        self.assertEqual(len(broker.generated), 6)
        self.assertTrue(all(broker.get_request(row[0])['status']=='accepted' for row in broker.generated))
        self.assertFalse(broker._tokens)

    async def test_missing_server_perfect_field_is_not_inferred_from_local_hits(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=3)
        original = api.post_json
        async def request(endpoint, body, **options):
            result = await original(endpoint, body, **options)
            if endpoint == 'hit':
                result['hit'].pop('perfect')
            return result
        api.post_json = request
        result = await self.run_fixture(api, clock)
        self.assertEqual(result['status'], 'settled_partial')
        self.assertEqual(result['server_perfects'], 0)
        self.assertEqual(result['reference_outcome']['perfect_count'], 0)
        self.assertEqual(result['accepted_hits'], 3)

    async def test_latest_window_count_and_duplicate_reveals_preserve_unique_hits(self):
        for initial in (2, 16):
            clock = FakeClock()
            api = BattleFixture(clock, count=3)
            original = api.post_json
            duplicate_sent = False
            async def request(endpoint, body, **options):
                nonlocal duplicate_sent
                if endpoint == 'window' and body['afterWindowId'] == api.windows[0]['id'] and not duplicate_sent:
                    duplicate_sent = True
                    return {'ok':True,'window':api.windows[0],'windowCount':3,'done':False}
                result = await original(endpoint, body, **options)
                if endpoint == 'start':
                    result['challenge']['windowCount'] = initial
                return result
            api.post_json = request
            result = await self.run_fixture(api, clock)
            self.assertEqual(result['status'], 'settled_perfect')
            self.assertEqual(result['expected_windows'], 3)
            self.assertEqual(api.calls.count('hit'), 3)

    async def test_no_hit_without_charge_ticket_and_no_false_full_settlement(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=3)
        original = api.post_json
        async def request(endpoint, body, **options):
            if endpoint=='charge-start' and body['windowId']==api.windows[1]['id']:
                return {'ok':True}
            return await original(endpoint, body, **options)
        api.post_json = request
        result = await self.run_fixture(api, clock)
        self.assertEqual(result['status'], 'settled_partial')
        self.assertEqual(result['accepted_hits'], 2)
        self.assertFalse(any(row['windowId']==api.windows[1]['id'] for row in api.hit_payloads))
        self.assertEqual(api.finish_payload['bossProof']['playerHp'], 100)
        self.assertEqual(result['local_missed_windows'], 0)

    async def test_missing_finish_receipt_is_not_success(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=1)
        original = api.post_json
        async def request(endpoint, body, **options):
            if endpoint=='finish':
                return {'ok':True,'result':{}}
            return await original(endpoint, body, **options)
        api.post_json = request
        with self.assertRaisesRegex(ProtocolError, 'finish_unconfirmed'):
            await self.run_fixture(api, clock)

    async def finish_reply_fixture(self, server_result):
        clock = FakeClock()
        api = BattleFixture(clock, count=1)
        original = api.post_json
        async def request(endpoint, body, **options):
            data = await original(endpoint, body, **options)
            return {'ok': True, 'result': server_result} if endpoint == 'finish' else data
        api.post_json = request
        return await self.run_fixture(api, clock)

    async def test_finish_without_player_hp_still_settles(self):
        result = await self.finish_reply_fixture({'grade': '甲等', 'score': 100})
        self.assertEqual(result['status'], 'settled_perfect')
        self.assertFalse(result['server_dead'])
        self.assertNotIn('player_hp', result['server_result'])

    async def test_strict_check_after_accepted_reply_keeps_server_evidence(self):
        with self.assertRaises(ProtocolError) as caught:
            await self.finish_reply_fixture({'grade': '甲等', 'score': 100, 'player_hp': -1})
        error = caught.exception
        self.assertEqual(error.code, 'invalid_player_hp')
        self.assertTrue(error.uncertain)
        self.assertTrue(error.details['begin_confirmed'])
        self.assertEqual((error.details['expected_windows'], error.details['accepted_hits']), (1, 1))

    async def test_finish_counts_override_local_receipt_counts_without_rewriting_receipts(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=3)
        original = api.post_json

        async def request(endpoint, body, **options):
            data = await original(endpoint, body, **options)
            if endpoint == 'finish':
                data['result'].update(hits=2, realtime_hit_count=2, perfects=1)
            return data

        api.post_json = request
        result = await self.run_fixture(api, clock)
        self.assertEqual(result['accepted_hits'], 2)
        self.assertEqual(result['server_perfects'], 1)
        self.assertEqual(len(result['receipts']), 3)
        self.assertEqual(result['status'], 'settled_partial')
        counts = result['reference_outcome']['diagnostics']['finish']['counts']
        self.assertEqual(counts['hits_source'], 'finish.hits')
        self.assertTrue(counts['mismatch'])

    async def test_main_identity_selection_uses_explicit_personal_role(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=1)
        original = api.post_json
        start_players = []
        async def request(endpoint, body, **options):
            if endpoint=='start':
                start_players.append(body['playerId'])
                if body['playerId']=='':
                    return {'ok':True,'needsIdentitySelection':True,'identityChoices':[
                        {'playerId':11,'source':'avatar','displayName':'unrelated'},
                        {'playerId':22,'source':'personal'}]}
            return await original(endpoint, body, **options)
        api.post_json = request
        result = await self.run_fixture(api, clock)
        self.assertEqual(start_players, ['',22])
        self.assertEqual(result['status'], 'settled_perfect')

    async def test_identity_mismatch_and_runtime_reuse_are_rejected(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=1)
        runner = ProtocolRuntime(account=api.account,username='fixture',transport=api,monotonic=clock.now,sleep=clock.sleep)
        with self.assertRaisesRegex(ProtocolError,'account_binding_mismatch'):
            await runner.run(WebViewAuth('tg1234567','fixture','qyz_fixture','a'*64))
        self.assertFalse(api.calls)
        with self.assertRaisesRegex(ProtocolError,'runtime_already_consumed'):
            await runner.run(WebViewAuth(api.account,'fixture','qyz_fixture','a'*64))

    async def test_cancelled_verification_removes_pending_request(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=1)
        original = api.post_json
        broker = MemoryTurnstileBroker()
        waiting = asyncio.Event()
        async def request(endpoint, body, **options):
            if endpoint=='begin':
                raise ProtocolError('turnstile_required',http_status=403)
            return await original(endpoint, body, **options)
        async def sleep(seconds):
            if broker.list_requests():
                waiting.set()
                await asyncio.Event().wait()
            await clock.sleep(seconds)
        api.post_json = request
        runner = ProtocolRuntime(account=api.account,username='fixture',transport=api,broker=broker,
                                 monotonic=clock.now,sleep=sleep)
        task = asyncio.create_task(runner.run(WebViewAuth(api.account,'fixture','qyz_fixture','a'*64)))
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(broker.list_requests())
        self.assertFalse(broker._tokens)

    async def test_slow_receipt_does_not_delay_later_window_charge(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=3)
        original = api.post_json
        first_done = False
        second_charge_while_pending = False
        async def request(endpoint, body, **options):
            nonlocal first_done, second_charge_while_pending
            if endpoint=='hit' and body['windowId']==api.windows[0]['id']:
                await clock.sleep(5)
                first_done = True
            if endpoint=='charge-start' and body['windowId']==api.windows[1]['id']:
                second_charge_while_pending = not first_done
            return await original(endpoint, body, **options)
        api.post_json = request
        result = await self.run_fixture(api, clock)
        self.assertTrue(second_charge_while_pending)
        self.assertEqual(result['accepted_hits'], 3)
        self.assertTrue(result['finish_confirmed'])
        self.assertEqual(api.finish_payload['bossProof']['playerHp'], 100)
        self.assertEqual(result['local_missed_windows'], 0)

    async def test_browser_wait_and_siteverify_cost_are_excluded_from_battle_clock(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=1)
        class DelayedBroker(CallbackFixtureBroker):
            def create_request(self, **kwargs):
                clock.value += 35
                return super().create_request(**kwargs)
        broker = DelayedBroker()
        original = api.post_json
        async def request(endpoint, body, **options):
            if endpoint=='begin':
                if not body.get('turnstileToken'):
                    await clock.sleep(.1)
                    raise ProtocolError('turnstile_required',http_status=403)
                await clock.sleep(2)
            return await original(endpoint,body,**options)
        api.post_json = request
        result = await self.run_fixture(api,clock,broker=broker)
        sync = result['reference_outcome']['diagnostics']['clock_sync']
        self.assertEqual(sync['round_trip_ms'], 100)
        self.assertEqual(sync['request']['clock_rtt_source'],'pre_verification_begin')
        self.assertGreater(sync['request']['total_duration_ms'], 37000)
        self.assertEqual(result['status'],'settled_perfect')

    def overloaded_fixture(self, failed, code, status, failures):
        clock = FakeClock()
        api = BattleFixture(clock, count=1)
        original = api.post_json
        sent = []
        async def request(endpoint, body, **options):
            if endpoint == failed:
                sent.append(endpoint)
                if len(sent) <= failures:
                    raise ProtocolError(code, phase=endpoint, http_status=status, uncertain=status >= 500)
            return await original(endpoint, body, **options)
        api.post_json = request
        return api, clock, sent

    async def test_server_overload_is_retried_as_often_as_the_reference(self):
        # /start twice, the token-free /begin probe and /finish once each.
        for failed, code, status, retries in (('start', 'rate_limited', 429, 2), ('begin', 'server_busy', 503, 1),
                                              ('finish', 'server_response_rejected', 503, 1)):
            with self.subTest(endpoint=failed):
                api, clock, sent = self.overloaded_fixture(failed, code, status, retries)
                result = await self.run_fixture(api, clock)
                self.assertEqual(result['status'], 'settled_perfect')
                self.assertEqual(len(sent), retries + 1)
            with self.subTest(endpoint=failed, exhausted=True):
                api, clock, sent = self.overloaded_fixture(failed, code, status, retries + 1)
                with self.assertRaises(ProtocolError) as caught:
                    await self.run_fixture(api, clock)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(sent), retries + 1)

    async def test_host_journal_barrier_is_not_measured_as_round_trip(self):
        clock = FakeClock()
        api = BattleFixture(clock, count=1)
        def trace(record):
            # The host blocks the battle thread on these until they are on disk.
            if record['phase'] in ('start_submitted', 'begin_submitted'):
                clock.value += .5
        runner = ProtocolRuntime(account=api.account, username='fixture', transport=api,
                                 monotonic=clock.now, sleep=clock.sleep, trace=trace)
        result = await runner.run(WebViewAuth(api.account, 'fixture_init', 'qyz_fixture_entry', 'a'*64))
        self.assertEqual(result['status'], 'settled_perfect')
        self.assertEqual(result['reference_outcome']['diagnostics']['clock_sync']['round_trip_ms'], 10)
        self.assertEqual([value for _at, value in runner.admission_clock.samples], [10.0])

    async def test_cancellation_drains_outstanding_charge_requests(self):
        clock = FakeClock()
        api = BattleFixture(clock,count=3)
        original = api.post_json
        charging = asyncio.Event()
        drained = []
        async def request(endpoint, body, **options):
            if endpoint=='charge-start':
                charging.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.append(body['windowId'])
            return await original(endpoint,body,**options)
        api.post_json = request
        task = asyncio.create_task(self.run_fixture(api,clock))
        await asyncio.wait_for(charging.wait(),2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(drained)
        self.assertNotIn('finish',api.calls)

    async def test_uncertain_hit_and_finish_retry_only_inside_the_action_deadline(self):
        for failed_endpoint in ('hit', 'finish'):
            with self.subTest(endpoint=failed_endpoint):
                clock = FakeClock()
                api = BattleFixture(clock, count=1)
                original = api.post_json
                attempts = []
                async def request(endpoint, body, **options):
                    if endpoint == failed_endpoint:
                        attempts.append(endpoint)
                        raise ProtocolError('transport_outcome_unknown', phase=endpoint,
                                            uncertain=True, http_status=503)
                    return await original(endpoint, body, **options)
                api.post_json = request
                if failed_endpoint == 'finish':
                    with self.assertRaises(ProtocolError) as caught:
                        await self.run_fixture(api, clock)
                    self.assertTrue(caught.exception.uncertain)
                else:
                    result = await self.run_fixture(api, clock)
                    self.assertEqual(result['status'], 'settled_partial')
                    self.assertEqual(result['accepted_hits'], 0)
                self.assertGreater(len(attempts), 1)
                self.assertLess(len(attempts), 140)

    async def test_quota_conflict_reconciles_once_and_stops_only_the_affected_account(self):
        clock = FakeClock()
        conflicted = BattleFixture(clock)
        peer = BattleFixture(clock)
        peer.account = 'tg9100000003'
        original = conflicted.post_json
        trace = []
        async def request(endpoint, body, **options):
            if endpoint == 'state':
                conflicted.calls.append(endpoint)
                await clock.sleep(.2)
                return {'ok': True, 'boss': {'roomStatus': 'battle', 'actionsRemaining': 0,
                                             'actionLimit': 1}, 'token': 'fixture_do_not_log'}
            data = await original(endpoint, body, **options)
            if endpoint == 'hit' and len(conflicted.hit_payloads) == 6:
                raise ProtocolError('boss_action_limit', phase='hit', http_status=409)
            return data
        conflicted.post_json = request
        stopped, completed = await asyncio.gather(self.run_fixture(conflicted, clock, trace=trace.append),
                                                  self.run_fixture(peer, clock), return_exceptions=True)
        self.assertIsInstance(stopped, ProtocolError)
        self.assertEqual(stopped.code, 'boss_action_limit')
        self.assertTrue(stopped.uncertain)
        self.assertEqual(conflicted.calls.count('state'), 1)
        self.assertEqual(conflicted.calls.count('hit'), 6)
        self.assertNotIn('finish', conflicted.calls)
        self.assertEqual(completed['status'], 'settled_perfect')
        self.assertEqual(completed['server_perfects'], 16)
        self.assertTrue(any(row['phase'] == 'account_action_conflict' for row in trace))
        self.assertNotIn('fixture_do_not_log', json.dumps(stopped.details))

    async def test_conflict_readback_failure_does_not_resume_mutations(self):
        clock = FakeClock()
        api = BattleFixture(clock)
        original = api.post_json
        async def request(endpoint, body, **options):
            if endpoint == 'state':
                api.calls.append(endpoint)
                raise ProtocolError('read_transport_failed', phase='state')
            data = await original(endpoint, body, **options)
            if endpoint == 'charge-start':
                raise ProtocolError('boss_action_limit', phase=endpoint, http_status=409)
            return data
        api.post_json = request
        with self.assertRaises(ProtocolError) as caught:
            await self.run_fixture(api, clock)
        self.assertEqual(caught.exception.code, 'boss_action_limit')
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(api.calls.count('state'), 1)
        self.assertEqual(api.calls.count('charge-start'), 1)
        self.assertNotIn('hit', api.calls)
        self.assertNotIn('finish', api.calls)
