"""Slow-charge headroom: a server clock decides the result, not the planner."""
import unittest
from types import SimpleNamespace

from qyz_reference.world_boss_features import WorldBossEntry, WorldBossMonitor
from qyz_timing import VpsTimingMixin, ticket_release_headroom, _ACTIVE_WINDOW
from test_qyz_protocol import FakeClock


class Current(VpsTimingMixin,WorldBossMonitor):
    pass


class Previous(Current):
    def _ticket_plan(self,target_ms):
        return {'extra_delay_ms':0,'reason':'previous_policy'}


class HeadroomTests(unittest.TestCase):
    def plan(self,**changes):
        values=dict(target_ms=2884,now_ms=2579,charge_sent_ms=1732,charge_received_ms=2579,
                    center_ms=3000,perfect_ms=210,request_lead_ms=58,drift_ms=0)
        return ticket_release_headroom(**{**values,**changes})

    def test_slow_charge_uses_twenty_ms_and_keeps_both_edges(self):
        plan=self.plan()
        self.assertEqual(plan['extra_delay_ms'],20)
        self.assertEqual(plan['adjusted_target_ms'],2904)
        self.assertLessEqual(plan['adjusted_target_ms']+58,1732+1250-20)
        self.assertLessEqual(plan['adjusted_target_ms']+58,3000+210-40)

    def test_fast_charge_and_already_late_release_are_unchanged(self):
        self.assertEqual(self.plan(charge_received_ms=1850)['extra_delay_ms'],0)
        self.assertEqual(self.plan(now_ms=2885)['extra_delay_ms'],0)

    def test_no_window_headroom_cannot_create_delay(self):
        self.assertEqual(self.plan(center_ms=2600)['extra_delay_ms'],0)

    def test_malformed_or_impossible_timing_cannot_move_a_strike(self):
        for value in [None,True,float('nan'),float('inf'),'2884']:
            self.assertEqual(self.plan(target_ms=value)['extra_delay_ms'],0)
        self.assertEqual(self.plan(charge_received_ms=1700)['extra_delay_ms'],0)

    def test_bounded_property_over_charge_times_and_windows(self):
        for rtt in (100,400,650,847,980,1500):
            for width in (100,210,300):
                plan=self.plan(charge_received_ms=1732+rtt,perfect_ms=width)
                self.assertTrue(0<=plan['extra_delay_ms']<=20)
                if plan['extra_delay_ms']:
                    self.assertLessEqual(plan['adjusted_target_ms'],plan['latest_ms'])
                    self.assertLessEqual(plan['adjusted_target_ms'],plan['window_latest_ms'])


class CausalHeadroomTests(unittest.IsolatedAsyncioTestCase):
    def monitor_context(self,clock=None):
        clock=clock or FakeClock()
        monitor=Current(SimpleNamespace(client=None,config={},state={}), 'tg9100000001',
                        monotonic=clock.now,sleep=clock.sleep)
        context={'owner':monitor,'ticket_granted':True,'battle_start':clock.now(),
                 'charge_sent_ms':1732,'charge_received_ms':2579,
                 'window':{'centerMs':3000,'perfectMs':210},'request_lead_ms':58}
        return monitor,clock,context

    async def test_replan_signal_inside_extra_wait_keeps_the_existing_deadline(self):
        monitor,clock,context=self.monitor_context()
        token=_ACTIVE_WINDOW.set(context)
        try:
            await clock.sleep(2.579)
            first=monitor._ticket_plan(2884)
            self.assertEqual(first['extra_delay_ms'],20)
            await clock.sleep(.315)
            monitor._notify_timing_update()
            self.assertEqual(monitor._ticket_plan(2884),first)
            # Changed feedback must recheck bounds instead of reusing the wait.
            monitor._drift_ms=10
            revised=monitor._ticket_plan(2874)
            self.assertEqual(revised['extra_delay_ms'],0)
            self.assertEqual(revised['reason'],'release_already_due')
        finally:
            _ACTIVE_WINDOW.reset(token)

    async def test_window_contexts_cannot_reuse_each_others_ticket_plan(self):
        import asyncio
        shared_clock=FakeClock()
        async def plan(target,elapsed):
            monitor,clock,context=self.monitor_context(shared_clock)
            token=_ACTIVE_WINDOW.set(context)
            try:
                await clock.sleep(elapsed)
                await asyncio.sleep(0)
                return monitor._ticket_plan(target)
            finally:
                _ACTIVE_WINDOW.reset(token)
        first,late=await asyncio.gather(plan(2884,2.579),plan(2884,2.9))
        self.assertEqual(first['extra_delay_ms'],20)
        self.assertEqual(late['extra_delay_ms'],0)
        self.assertIsNone(_ACTIVE_WINDOW.get())

    async def test_waiter_replans_inside_extra_wait_without_releasing_early(self):
        import asyncio
        monitor,clock,context=self.monitor_context()
        token=_ACTIVE_WINDOW.set(context)
        try:
            await clock.sleep(2.579)
            async def signal():
                await clock.sleep(.315)
                monitor._notify_timing_update()
            changed=asyncio.create_task(signal())
            self.assertTrue(await monitor._wait_for_timing_target(
                lambda:context['battle_start']+2.884))
            await changed
            self.assertAlmostEqual((clock.now()-context['battle_start'])*1000,2904,delta=1)
            self.assertEqual(context['ticket_guard']['extra_delay_ms'],20)
        finally:
            _ACTIVE_WINDOW.reset(token)

    async def strike(self,cls,*,charge_up=707,charge_down=140,hit_up=65,hit_down=61):
        clock=FakeClock();start=clock.now();issued=None;calls=[];sent={}
        async def request(origin,path,body,timeout):
            nonlocal issued
            name=path.rsplit('/',1)[-1];calls.append(name)
            sent[name]=(clock.now()-start)*1000
            if name=='charge-start':
                await clock.sleep(charge_up/1000);issued=clock.now()
                await clock.sleep(charge_down/1000)
                return {'ok':True,'chargeTicket':'fixture_ticket'}
            self.assertEqual(name,'hit')
            self.assertAlmostEqual(body['holdMs'],sent['hit']-sent['charge-start'],delta=1)
            await clock.sleep(hit_up/1000)
            hold=(clock.now()-issued)*1000;arrival=(clock.now()-start)*1000;delta=abs(arrival-3000)
            await clock.sleep(hit_down/1000)
            return {'ok':True,'hit':{'perfect':520<=hold<=1250 and delta<=210,'holdMs':hold,'deltaMs':delta,'damageYi':1}}
        monitor=cls(SimpleNamespace(client=None,config={},state={}), 'tg9100000001',
                    post_json=request,monotonic=clock.now,sleep=clock.sleep)
        entry=WorldBossEntry(message_id=1,chat_id=1,bot_username='fixture',origin='https://asc.aiopenai.app',
                             fingerprint='a'*64,token='fixture')
        result=await monitor._hit_window(entry,'fixture','fixture','fixture',start,
                 {'id':'fixture_window','centerMs':3000,'perfectMs':210,'hitMs':620},window_index=1,request_lead_ms=58)
        self.assertEqual(calls,['charge-start','hit'])
        self.assertIsNone(_ACTIVE_WINDOW.get())
        return result,sent,monitor

    async def test_observed_like_short_hold_becomes_perfect_without_fabricating_hold(self):
        old,old_sent,old_monitor=await self.strike(Previous)
        new,new_sent,new_monitor=await self.strike(Current)
        self.assertFalse(old['accepted_perfect']);self.assertTrue(new['accepted_perfect'])
        self.assertLess(old['diagnostic']['server_hold_ms'],520)
        self.assertAlmostEqual(new_sent['hit']-old_sent['hit'],20,delta=1)
        self.assertAlmostEqual(new['diagnostic']['server_hold_ms']-old['diagnostic']['server_hold_ms'],20,delta=1)
        self.assertAlmostEqual(new['diagnostic']['wake_lateness_ms'],0,delta=1)
        self.assertAlmostEqual(new_monitor._drift_ms,old_monitor._drift_ms,delta=1)

    async def test_same_rtt_with_delay_on_response_side_preserves_perfect(self):
        old,_,_=await self.strike(Previous,charge_up=55,charge_down=792,hit_up=55)
        new,_,_=await self.strike(Current,charge_up=55,charge_down=792,hit_up=55)
        self.assertTrue(old['accepted_perfect']);self.assertTrue(new['accepted_perfect'])
        self.assertLessEqual(new['diagnostic']['server_hold_ms'],1250)

    async def test_fast_path_keeps_identical_send_times(self):
        old,old_sent,_=await self.strike(Previous,charge_up=58,charge_down=58)
        new,new_sent,_=await self.strike(Current,charge_up=58,charge_down=58)
        self.assertEqual(old_sent,new_sent)
        self.assertEqual(old['accepted_perfect'],new['accepted_perfect'])

    async def test_unpredictable_long_hit_still_reports_nonperfect(self):
        result,_,_=await self.strike(Current,hit_up=800)
        self.assertFalse(result['accepted_perfect'])
