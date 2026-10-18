"""Offline policy comparison using observed payout rules, never live accounts.

Uniform shuffled synthetic boards from the public layout prior are shared
between policies. Score hazards and failed payouts use observed live rules.
These estimates are never passed to the production reward/report code.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics

from .strategy import PRIOR, GROUP, choose, direction, neighbors, utility


class PreviewBoard:
    def __init__(self, seed, *, live_rules=False):
        self.types = [kind for kind, n in PRIOR.items() for _ in range(n)]
        random.Random(seed).shuffle(self.types)
        self.main = self.types.index('main')
        self.live_rules = live_rules
        self.state = {'sessionId': 'simulation', 'size': 5, 'ap': 8, 'maxAp': 8, 'score': 32,
                      'status': 'active', 'foundMain': False, 'revealedCount': 0, 'loot': [],
                      'latestHint': None, 'pendingChallenge': False,
                      'cells': [{'index': i, 'revealed': False} for i in range(25)]}

    def reveal(self, index):
        state = self.state
        assert state['ap'] > 0 and not state['cells'][index]['revealed']
        kind = self.types[index]
        cell = state['cells'][index]
        cell.update(revealed=True, type=kind)
        state['revealedCount'] += 1
        state['ap'] = max(0, state['ap'] - (3 if kind == 'monster' else 2 if kind == 'trap' else 1))
        if GROUP[kind] in ('treasure', 'resource'):
            state['loot'].append({'name': 'synthetic_bundle', 'quantity': 1})
        if kind == 'main':
            state['foundMain'] = True
        if kind == 'clue':
            marks = [{'index': i, 'kind': GROUP[self.types[i]]} for i in sorted(neighbors(index))
                     if GROUP[self.types[i]] != 'other']
            cell['hint'] = {'text': '灵气流向'+direction(index, self.main)+'。', 'markers': marks}
            state['latestHint'] = cell['hint']
        hazards = sum(c.get('type') in ('trap','monster') for c in state['cells'] if c['revealed'])
        state['score'] = max(0, min(100, state['revealedCount']*6 + state['ap']*4 + len(state['loot'])*8
                                   + 38*state['foundMain'] - (8*hazards if self.live_rules else 0)))
        if state['ap'] == 0 and not state['foundMain']:
            state['status'] = 'failed'


def evaluate(count=1000, first_seed=0):
    policies = ('random', 'four_centres', 'greedy', 'adaptive')
    outcomes = {policy: [] for policy in policies}
    for seed in range(first_seed, first_seed+count):
        for policy in policies:
            game = PreviewBoard(seed, live_rules=True)
            rng = random.Random(seed+900000)
            for _ in range(26):
                if policy == 'random':
                    decision = {'action': 'settle'} if game.state['ap'] <= 0 else {
                        'action': 'reveal', 'index': rng.choice([c['index'] for c in game.state['cells'] if not c['revealed']])}
                else:
                    decision = choose(game.state, policy=policy)
                if decision['action'] == 'settle':
                    break
                game.reveal(decision['index'])
            s = game.state
            failed = s['ap']==0 and not s['foundMain']
            outcomes[policy].append({'score': s['score'], 'main': int(s['foundMain']),
                'bundles': 0 if failed else len(s['loot']), 'ap_exhausted_without_main': int(failed),
                'utility': utility(s['score'], s['foundMain'], len(s['loot']), failed=failed)})
    result = {'model': 'public_layout_prior_with_observed_hazard_and_failed_payout_rules', 'live_outcomes': False,
              'boards': count, 'first_seed': first_seed, 'policies': {}}
    for policy, rows in outcomes.items():
        row = {key: round(statistics.mean(r[key] for r in rows), 4) for key in rows[0]}
        delta = [r['utility']-b['utility'] for r,b in zip(rows,outcomes['four_centres'])]
        mean = statistics.mean(delta)
        margin = 1.96*statistics.stdev(delta)/math.sqrt(count) if count>1 else 0
        row['utility_delta_vs_four_centres'] = round(mean, 4)
        row['paired_95pct_interval'] = [round(mean-margin, 4), round(mean+margin, 4)]
        result['policies'][policy] = row
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--boards', type=int, default=1000)
    parser.add_argument('--first-seed', type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.boards,args.first_seed),indent=2,ensure_ascii=False))
