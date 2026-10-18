"""Observed hints + bounded lookahead for a 5x5 hunt, with an explicit stop action.

The layout prior comes from the public preview. Live receipts additionally
confirm an 8-point hazard penalty and loss of the entire payout on exhaustion
without the main chest. Markers are evidence; missing markers don't certify
safety. Inspired by JSMinesweeper's probability/progress separation.
"""
from __future__ import annotations

from collections import Counter
import math
import re

from .protocol import HuntError

CENTRES = (6, 8, 16, 18)  # One-based 7, 9, 17, 19; the operator's four centres.
PRIOR = {'main': 1, 'chest': 2, 'herb': 4, 'mine': 3, 'clue': 5,
         'trap': 4, 'monster': 3, 'empty': 3}
GROUP = {'main': 'treasure', 'chest': 'treasure', 'herb': 'resource', 'mine': 'resource',
         'trap': 'risk', 'monster': 'risk', 'clue': 'other', 'empty': 'other'}
VERSION = 'hunt-direction-progress-v2'


def neighbors(index: int) -> set[int]:
    row, col = divmod(index, 5)
    return {r * 5 + c for r in range(max(0, row - 1), min(5, row + 2))
            for c in range(max(0, col - 1), min(5, col + 2)) if (r, c) != (row, col)}


def direction(start: int, target: int) -> str:
    row, col = divmod(start, 5)
    tr, tc = divmod(target, 5)
    return ('北' if tr < row else '南' if tr > row else '') + ('西' if tc < col else '东' if tc > col else '') or '此地'


def knowledge(run: dict) -> dict:
    cells = run['cells']
    hidden = {c['index'] for c in cells if not c['revealed']}
    marks, directions = {}, []
    candidates = set(hidden)
    for cell in cells:
        if not cell['revealed']:
            continue
        clue = cell.get('hint') or {}
        match = re.match(r'^灵气流向(北西|北东|南西|南东|东北|西北|东南|西南|北|南|东|西|此地)。', clue.get('text', ''))
        if match:
            d = {'东北': '北东', '西北': '北西', '东南': '南东', '西南': '南西'}.get(match[1], match[1])
            directions.append((cell['index'], d))
            candidates &= {i for i in hidden if direction(cell['index'], i) == d}
        for mark in clue.get('markers', []):
            i, kind = mark['index'], mark['kind']
            if i in marks and marks[i] != kind:
                raise HuntError('hunt_conflicting_markers')
            marks[i] = kind
    candidates -= {i for i, kind in marks.items() if kind != 'treasure'}
    if run['foundMain']:
        candidates.clear()
    elif not candidates:
        raise HuntError('hunt_conflicting_directions')
    return {'hidden': hidden, 'marks': marks, 'candidates': candidates, 'directions': directions}


def probabilities(run: dict, info: dict) -> dict[int, dict[str, float]]:
    """Priors guide unknown cells; directional/marker restrictions are exact."""
    observed = Counter(c.get('type') for c in run['cells'] if c['revealed'])
    remaining = {t: max(0, count - observed[t]) for t, count in PRIOR.items()}
    # If production composition differs, keep unmarked guesses explicitly
    # probabilistic. Never manufacture a provably safe cell from a stale prior.
    hidden, marks, candidates = info['hidden'], info['marks'], info['candidates']
    remaining['main'] = 0 if run['foundMain'] else 1
    result = {}
    # A treasure marker is stronger than a general directional candidate.
    weights = {i: (4.0 if marks.get(i) == 'treasure' else 1.0) for i in candidates}
    weight_sum = sum(weights.values())
    for index in hidden:
        pmain = weights.get(index, 0) / weight_sum if weight_sum else 0.0
        category = marks.get(index)
        possible = [t for t in PRIOR if t != 'main' and (category is None or GROUP[t] == category)]
        counts = {t: float(remaining[t]) for t in possible}
        if not sum(counts.values()):
            counts = {t: 1.0 for t in possible}
        total = sum(counts.values())
        result[index] = {t: (1 - pmain) * count / total for t, count in counts.items()}
        result[index]['main'] = pmain
    return result


def grade(score: float, found_main: bool) -> str:
    return '甲等' if found_main and score >= 82 else '乙等' if score >= 58 else '丙等' if score >= 32 else '失败'


def utility(score: float, found_main: bool, loot_count: int, *, failed: bool = False) -> float:
    # Contribution is capped by score; grade/main are strategic goals. Every
    # additional bundle retains value, including after a score reaches 100.
    if failed:
        return 0.0
    contribution = 30 + int(score) // 4 if score >= 32 else 0
    return contribution + loot_count * 9 + (18 if found_main else 0) + (8 if grade(score, found_main) == '甲等' else 0)


def predicted(run: dict, kind: str) -> tuple[int, bool, int, int]:
    cost = 3 if kind == 'monster' else 2 if kind == 'trap' else 1
    ap = max(0, run['ap'] - cost)
    found = run['foundMain'] or kind == 'main'
    count = len(run['loot']) + int(kind in ('main', 'chest', 'herb', 'mine'))
    hazards = sum(c.get('type') in ('trap','monster') for c in run['cells'] if c['revealed'])
    hazards += kind in ('trap','monster')
    score = max(0, min(100, (run['revealedCount'] + 1) * 6 + ap * 4 + count * 8 + 38 * found - 8 * hazards))
    return score, found, count, ap


def choose(run: dict, *, policy: str = 'adaptive') -> dict:
    if run.get('pendingChallenge'):
        return {'action': 'settle', 'reason': 'pending_challenge_cash_out'}
    if run['status'] != 'active' or run['ap'] <= 0:
        return {'action': 'settle', 'reason': 'run_finished'}
    info = knowledge(run)
    hidden, marks, candidates = info['hidden'], info['marks'], info['candidates']
    if not hidden:
        return {'action': 'settle', 'reason': 'board_complete'}
    if not run['foundMain'] and len(candidates) == 1:
        return {'action': 'reveal', 'index': next(iter(candidates)), 'reason': 'main_deduced', 'main_probability': 1.0}
    probs = probabilities(run, info)
    current = utility(run['score'], run['foundMain'], len(run['loot']))
    ranked = []
    for index, p in probs.items():
        if marks.get(index) == 'risk':
            continue
        expected = 0.0
        for kind, probability in p.items():
            score, found, bundles, ap = predicted(run, kind)
            expected += probability * utility(score, found, bundles, failed=ap == 0 and not found)
        risk = p.get('trap', 0) + p.get('monster', 0)
        # A clue can reveal exact local categories and narrow the main target.
        partitions = Counter(direction(index, i) for i in candidates if i != index)
        n = sum(partitions.values())
        entropy = -sum((v / n) * math.log2(v / n) for v in partitions.values()) if n else 0
        new_neighbors = len((neighbors(index) & hidden) - marks.keys())
        future = min(1.0, max(0, run['ap'] - 1) / 4)
        information = p.get('clue', 0) * (new_neighbors * 1.4 + entropy * 3.5) * future
        target = p.get('main', 0) * min(12, 2 * run['ap']) if not run['foundMain'] else 0
        rank = expected - current + information + target
        if policy == 'greedy':
            rank = expected - current
        ranked.append((rank, index, expected - current, information, risk, p.get('main', 0)))
    if not ranked:
        return {'action': 'settle', 'reason': 'only_known_risks_remain'}
    # The user's baseline: explore the four interior centres, unless a centre
    # is known dangerous or the main treasure has already been found.
    if policy == 'four_centres' and not run['foundMain'] and run['ap'] >= 4:
        centre = next((i for i in CENTRES if i in hidden and marks.get(i) != 'risk'), None)
        if centre is not None:
            return {'action': 'reveal', 'index': centre, 'reason': 'four_centres'}
    # Four centres cover new regions well. This tie break makes the opening
    # reproducible; strong observed hints always override it.
    rank, index, immediate, information, risk, main = max(
        ranked, key=lambda r: (round(r[0], 7), r[1] in CENTRES, -r[1]))
    if rank <= 0 and run['revealedCount'] > 0:
        return {'action': 'settle', 'reason': 'cash_out_expected_value', 'expected_gain': round(rank, 3)}
    return {'action': 'reveal', 'index': index, 'reason': 'hint_value_and_progress',
            'expected_gain': round(rank, 3), 'information': round(information, 3),
            'risk_probability': round(risk, 4), 'main_probability': round(main, 4)}
