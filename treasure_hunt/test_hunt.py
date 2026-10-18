import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from miniapp_shared.accounts import ALL_ACCOUNTS
from miniapp_shared.state import read_json, day_key
from treasure_hunt import protocol as p
from treasure_hunt.runtime import fresh_state, play, summarize, load_state
from treasure_hunt.strategy import choose, knowledge, direction, predicted, utility
from treasure_hunt.simulate import PreviewBoard
from treasure_hunt.reports import build

ACCOUNT = 'tg9100000002'
PLAYER = int(ACCOUNT[2:])
NOW = 1791612000.0  # Outside the priority window; a fixed Shanghai calendar day.


def detail(used=0, player=PLAYER):
    return {'ok': True, 'account': {'playerId': player, 'authUserId': player},
            'dwelling': {'hasDwelling': True, 'hunt': {'used':used,'limit':3,'remaining':3-used,'actionPoints':8}}}


class FakeClient:
    def __init__(self, used=0):
        self.used = used
        self.calls = []
        self.game = None
        self.fail = None

    async def details(self):
        return detail(self.used)

    async def mutate(self, operation, *, session_id=None, index=None):
        self.calls.append((operation, index))
        if self.fail == operation:
            raise p.HuntError('request_failed', uncertain=True)
        if operation == 'start':
            self.used += 1
            self.game = PreviewBoard(self.used)
            self.game.state['sessionId'] = 'test-run-'+str(self.used)
            return {**detail(self.used), 'huntRun': deepcopy(self.game.state)}
        if operation == 'reveal':
            assert session_id == self.game.state['sessionId']
            self.game.reveal(index)
            return {'ok': True, 'huntRun': deepcopy(self.game.state)}
        if operation == 'settle':
            s = self.game.state
            return {**detail(self.used), 'huntResult': {'score':s['score'], 'grade':'乙等',
                'contribution':30, 'loot':[{'name':'灵石','quantity':25}],
                'foundMain':s['foundMain'], 'revealedCount':s['revealedCount']}}
        raise AssertionError(operation)


async def no_sleep(_):
    pass


class HuntTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def play(self, client, **kwargs):
        return asyncio.run(play(self.root, ACCOUNT, client, clock=lambda:NOW, sleep=no_sleep, **kwargs))

    def state(self):
        return read_json(self.root/'state/accounts'/ACCOUNT/'treasure_hunt/state.json')

    def test_other_actor_and_impossible_quota_are_rejected(self):
        with self.assertRaises(p.HuntError): p.quota(detail(player=1), PLAYER)
        value=detail();value['dwelling']['hunt']['remaining']=1
        with self.assertRaises(p.HuntError): p.quota(value, PLAYER)
        value=detail();value['dwelling']['hunt']['used']=True
        with self.assertRaises(p.HuntError): p.quota(value, PLAYER)

    def test_hunt_errors_are_miniapp_errors_with_their_code(self):
        from miniapp_shared.dwelling import MiniAppError, error_code, is_uncertain
        exc=p.HuntError('hunt_session_missing',uncertain=True)
        self.assertIsInstance(exc,MiniAppError)
        self.assertEqual((error_code(exc),is_uncertain(exc)),('hunt_session_missing',True))

    def test_hidden_server_fields_cannot_be_used_as_knowledge(self):
        game=PreviewBoard(0)
        for cell in game.state['cells']:
            cell.update(type='main',hint={'text':'灵气流向北。','markers':[{'index':0,'kind':'treasure'}]})
        clean=p.run({'ok':True,'huntRun':game.state})
        self.assertEqual(knowledge(clean)['marks'],{})
        self.assertEqual(len(knowledge(clean)['candidates']),25)

    def test_mutation_snapshot_may_omit_unchanged_dwelling_flag(self):
        patch=detail(1);del patch['dwelling']['hasDwelling']
        with self.assertRaises(p.HuntError):p.quota(patch,PLAYER)
        self.assertEqual(p.quota(patch,PLAYER,known_dwelling=True)['used'],1)
        patch['account']['playerId']=1
        with self.assertRaises(p.HuntError):p.quota(patch,PLAYER,known_dwelling=True)

    def test_partial_start_and_settle_snapshots_finish_without_extra_attempts(self):
        class Partial(FakeClient):
            async def mutate(self,*args,**kwargs):
                reply=await super().mutate(*args,**kwargs)
                if 'dwelling' in reply:reply['dwelling'].pop('hasDwelling',None)
                return reply
        result=self.play(Partial())
        self.assertEqual((result['status'],result['settled_runs']),('daily_complete',3))

    def test_two_directional_hints_find_the_only_main_candidate(self):
        game=PreviewBoard(0);s=game.state
        for i in (6,8):
            s['cells'][i].update(revealed=True,type='clue',hint={'text':'灵气流向'+direction(i,7)+'。','markers':[]})
        s['revealedCount']=2
        decision=choose(s)
        self.assertEqual((decision['index'],decision['reason']),(7,'main_deduced'))

    def test_known_danger_is_never_a_four_centre_opening(self):
        s=PreviewBoard(1).state
        s['cells'][12].update(revealed=True,type='clue',hint={'text':'','markers':[{'index':6,'kind':'risk'}]})
        s['revealedCount']=1
        self.assertNotEqual(choose(s,policy='four_centres')['index'],6)

    def test_zero_ap_and_unimplemented_challenge_cash_out(self):
        s=PreviewBoard(1).state;s['ap']=0
        self.assertEqual(choose(s)['action'],'settle')

    def test_last_ap_without_known_main_banks_existing_payout(self):
        s=PreviewBoard(4).state
        for i in range(6):s['cells'][i].update(revealed=True,type='mine')
        s.update(ap=1,score=76,revealedCount=6,loot=[{'name':'灵石','quantity':25}]*5)
        self.assertEqual(choose(s)['action'],'settle')

    def test_exhaustion_without_main_loses_all_predicted_payout(self):
        self.assertEqual(utility(90,False,7,failed=True),0)
        self.assertGreater(utility(90,True,7,failed=False),0)

    def test_hazard_score_penalty_matches_observed_first_room(self):
        s=PreviewBoard(1).state
        # Initial score 32: trap costs 2 AP and a further 8 points.
        self.assertEqual(predicted(s,'trap'),(22,False,0,6))
        self.assertEqual(predicted(s,'monster'),(18,False,0,5))
        s['ap']=5;s['pendingChallenge']=True
        self.assertEqual(choose(s)['action'],'settle')

    def test_no_attempt_when_server_daily_quota_is_exhausted(self):
        c=FakeClient(3);r=self.play(c)
        self.assertEqual(c.calls,[])
        self.assertEqual((r['status'],r['preexisting_used'],r['settled_runs']),('daily_complete',3,0))
        self.assertFalse(r['confirmed'])

    def test_every_available_run_is_settled_and_receipted(self):
        c=FakeClient();r=self.play(c)
        self.assertEqual(r['status'],'daily_complete')
        self.assertEqual(r['settled_runs'],3)
        self.assertEqual(r['rewards'],{'灵石':75})
        self.assertEqual(len(set(r['receipt_keys'])),3)
        self.assertEqual(sum(op=='start' for op,_ in c.calls),3)
        self.assertEqual(sum(op=='settle' for op,_ in c.calls),3)
        self.assertIsNone(self.state()['pending'])

    def test_existing_canary_resumes_without_spending_a_second_attempt(self):
        c=FakeClient(1);c.game=PreviewBoard(1);c.game.state['sessionId']='old-run'
        s=fresh_state(ACCOUNT,NOW);s.update(run=c.game.state,quota=detail(1)['dwelling']['hunt'])
        r=self.play(c,state=s,max_runs=1)
        self.assertEqual(r['settled_runs'],1)
        self.assertEqual(r['preexisting_used'],0)
        self.assertNotIn(('start',None),c.calls)

    def test_lost_start_response_is_parked_and_never_replayed(self):
        c=FakeClient();c.fail='start'
        r=self.play(c);self.assertEqual(r['pending_operation'],'start')
        c.fail=None;before=len(c.calls)
        r=self.play(c)
        self.assertEqual(len(c.calls),before)
        self.assertEqual(r['status'],'unconfirmed_start')

    def test_active_run_error_is_retried_before_the_long_account_backoff(self):
        c=FakeClient();c.fail='reveal';result=self.play(c)
        self.assertEqual(result['pending_operation'],'reveal')
        self.assertLessEqual(result['next_due_at'],NOW+60)
        self.assertTrue(self.state()['run'])

    def test_lost_reveal_response_cash_out_uses_server_receipt(self):
        c=FakeClient(1);c.game=PreviewBoard(4);c.game.state['sessionId']='old-run';c.game.reveal(6)
        s=fresh_state(ACCOUNT,NOW);s.update(run=deepcopy(c.game.state),
            pending={'operation':'reveal','index':8},preexisting_used=0)
        r=self.play(c,state=s,max_runs=1)
        self.assertEqual(c.calls,[('settle',None)])
        self.assertEqual(r['rewards'],{'灵石':25})

    def test_contradictory_clues_settle_the_run_instead_of_retrying_until_it_expires(self):
        c=FakeClient(1);c.game=PreviewBoard(1);c.game.state['sessionId']='old-run'
        for i,kind in ((0,'treasure'),(1,'risk')):
            c.game.state['cells'][i].update(revealed=True,type='clue',hint={'text':'','markers':[{'index':12,'kind':kind}]})
        c.game.state['revealedCount']=2
        s=fresh_state(ACCOUNT,NOW);s.update(run=c.game.state,quota=detail(1)['dwelling']['hunt'])
        r=self.play(c,state=s,max_runs=1)
        self.assertEqual(c.calls,[('settle',None)])
        self.assertEqual(r['settled_runs'],1)
        self.assertEqual(self.state()['decisions'][-1]['reason'],'strategy_hunt_conflicting_markers')

    def test_a_held_fleet_lock_times_out_as_lock_busy_without_logging_in(self):
        from unittest.mock import patch
        from miniapp_shared.state import exclusive_lock
        from treasure_hunt import runtime
        def forbidden(*_):
            raise AssertionError('no login while another account holds the fleet lock')
        with exclusive_lock(self.root/'state/treasure_hunt/fleet.lock'), patch.object(runtime,'FLEET_WAIT_SECONDS',0):
            r=asyncio.run(runtime.run_account(self.root,ACCOUNT,clock=lambda:NOW,bridge_factory=forbidden))
        self.assertEqual(r['status'],'lock_busy')

    def test_lost_settlement_response_never_becomes_a_fake_reward(self):
        c=FakeClient();c.fail='settle'
        r=self.play(c);self.assertEqual(r['settled_runs'],0)
        self.assertEqual(r['pending_operation'],'settle')
        self.assertEqual(r['rewards'],{})
        c.fail=None;before=len(c.calls)
        r=self.play(c)
        self.assertEqual(len(c.calls),before)
        self.assertEqual(r['status'],'unconfirmed_settle')

    def test_actor_mismatch_during_settlement_retains_intent(self):
        class WrongActor(FakeClient):
            async def mutate(self,*args,**kwargs):
                reply=await super().mutate(*args,**kwargs)
                if args[0]=='settle':reply['account']['playerId']=1
                return reply
        r=self.play(WrongActor())
        self.assertEqual(r['settled_runs'],0)
        self.assertEqual(r['pending_operation'],'settle')

    def test_run_handle_and_authentication_never_enter_summary(self):
        c=FakeClient();c.fail='reveal';r=self.play(c)
        import json
        output=json.dumps(r)
        self.assertNotIn('sessionId',output)
        self.assertNotIn('test-run-',output)
        self.assertNotIn('initData',output)

    def test_report_distinguishes_preexisting_attempts_from_receipts(self):
        rows=[]
        for a in ALL_ACCOUNTS:
            s=fresh_state(a,NOW);s.update(status='daily_complete',quota={'used':3,'limit':3,'remaining':0},preexisting_used=3)
            rows.append(summarize(s))
        value=build(rows,day_key(NOW))
        self.assertTrue(value['complete'])
        self.assertEqual((value['settled_runs'],value['contribution'],value['rewards']),(0,0,{}))
        self.assertIn('收益没有回执',value['text'])

    def test_definite_expired_run_is_recorded_and_remaining_attempts_continue(self):
        class Expired(FakeClient):
            async def mutate(self, operation, **kwargs):
                if kwargs.get('session_id') == 'expired-run':
                    self.calls.append((operation,kwargs.get('index')))
                    raise p.HuntError('hunt_session_missing')
                return await super().mutate(operation,**kwargs)
        c=Expired(1);s=fresh_state(ACCOUNT,NOW);g=PreviewBoard(3)
        g.state['sessionId']='expired-run';s['run']=g.state
        r=self.play(c,state=s)
        self.assertEqual(r['status'],'daily_complete_with_unknown')
        self.assertEqual((r['settled_runs'],r['unconfirmed_runs']),(2,1))
        self.assertEqual(r['rewards'],{'灵石':50})
        self.assertEqual(sum(op=='start' for op,_ in c.calls),2)

    def test_next_day_resets_quota_tracking_and_preserves_previous_receipts(self):
        client=FakeClient();first=self.play(client)
        self.assertEqual(first['settled_runs'],3)
        new=load_state(self.root,ACCOUNT,NOW+86400)
        self.assertEqual(new['receipts'],[])
        self.assertNotIn('preexisting_used',new)
        old=read_json(self.root/'state/accounts'/ACCOUNT/'treasure_hunt/days'/f'{day_key(NOW)}.json')
        self.assertEqual(len(old['receipts']),3)

    def test_report_rejects_unconfirmed_bag_rewards_and_missing_accounts(self):
        row={'account':ACCOUNT,'day':day_key(NOW),'status':'active','settled_runs':1,
             'confirmed':False,'rewards':{'灵石':999999},'contribution':999999}
        value=build([row],day_key(NOW))
        self.assertFalse(value['complete']);self.assertEqual(value['rewards'],{})


if __name__=='__main__':unittest.main()
