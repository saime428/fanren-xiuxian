#!/usr/bin/env python3
"""Upstream-faithful Tianji Trial proof builders.

Vendored from Waaiging/fanrenxiuxian_auto at commit
04d72e9026383d49ce529c88da2c2ecb8a9aca09. Local changes are limited to
the exception type, the module boundary, wrapping solver errors on malformed
challenges, and dropping the unused per-stage log text.
"""
from __future__ import annotations

import math
from typing import Any

import networkx as nx

from miniapp_shared.errors import MiniAppError


class TianjiTrialError(MiniAppError):
    """The Tianji flow's MiniAppError (same ``code``/``uncertain`` contract)."""


def tianji_trial_progress(payload: Any) -> tuple[int, int]:
    if not isinstance(payload, dict) or payload.get("ok") is False or payload.get("error"):
        raise TianjiTrialError("trial_progress_unconfirmed", uncertain=True)
    progress = payload.get("dailyProgress")
    result = payload.get("result")
    if isinstance(progress, dict) and "completed" in progress and "limit" in progress:
        completed, limit = progress["completed"], progress["limit"]
    elif isinstance(result, dict) and "daily_progress" in result and "daily_limit" in result:
        completed, limit = result["daily_progress"], result["daily_limit"]
    else:
        raise TianjiTrialError("trial_progress_unconfirmed", uncertain=True)
    if (type(completed) is not int or type(limit) is not int or limit <= 0
            or not 0 <= completed <= limit):
        raise TianjiTrialError("trial_progress_invalid", uncertain=True)
    return completed, limit


def tianji_trial_start_progress(payload: Any) -> tuple[int, int] | None:
    """A server challenge authorizes play, but does not confirm daily progress.

    The official client starts directly from ``challenge``; ``dailyProgress``
    is optional on /start. Any supplied progress must still validate, and only
    /finish progress or an explicit daily-limit reply can confirm completion.
    """
    if not isinstance(payload, dict) or payload.get("ok") is False or payload.get("error"):
        raise TianjiTrialError("trial_progress_unconfirmed", uncertain=True)
    result = payload.get("result")
    if "dailyProgress" in payload or (isinstance(result, dict)
            and ("daily_progress" in result or "daily_limit" in result)):
        return tianji_trial_progress(payload)
    challenge = payload.get("challenge")
    if not isinstance(challenge, dict) or not challenge.get("challengeId"):
        raise TianjiTrialError("trial_challenge_missing", uncertain=True)
    return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError, OverflowError):
        return default


def tianji_trial_result_text(results: list[dict[str, Any]]) -> str:
    # The run is already settled when this summary is built; a malformed
    # reward field must not turn a confirmed result into a coordinator crash.
    rewards = 0
    bonuses = 0
    grades = []
    balance = 0
    for payload in results:
        result = payload.get("result") if isinstance(payload, dict) else {}
        result = result if isinstance(result, dict) else {}
        rewards += _as_int(result.get("reward_trace"))
        bonuses += _as_int(result.get("bonus_trace"))
        balance = _as_int(result.get("balance"), balance)
        grade = str(result.get("grade") or "").strip()
        if grade:
            grades.append(grade)
    grade_text = "/".join(grades) or "已结算"
    extra = f"（额外 {bonuses}）" if bonuses > 0 else ""
    completed, limit = tianji_trial_progress(results[-1]) if results else (0, 3)
    return (
        f"{completed or limit} 关完成，评级 {grade_text}，"
        f"天机残痕 +{rewards}{extra}，余额 {balance}"
    )


def _trial_duration_ms(challenge: dict[str, Any], event_count: int = 1) -> int:
    minimum = max(350, int(challenge.get("minDurationMs") or challenge.get("min_duration_ms") or 3200))
    return minimum + max(400, int(event_count) * 80)


def _trial_lights_neighbors(index: int, size: int) -> list[int]:
    row, column = divmod(index, size)
    result = [index]
    if row > 0:
        result.append(index - size)
    if row < size - 1:
        result.append(index + size)
    if column > 0:
        result.append(index - 1)
    if column < size - 1:
        result.append(index + 1)
    return result


def _solve_lights_out(challenge: dict[str, Any]) -> dict[str, Any]:
    size = max(4, min(5, int(challenge.get("gridSize") or challenge.get("grid_size") or 4)))
    cells = [1 if int(value or 0) else 0 for value in (challenge.get("cells") or [])]
    if len(cells) != size * size:
        raise TianjiTrialError("trial_lights_invalid")
    target = 1 if int(challenge.get("targetState", challenge.get("target_state", 1)) or 0) else 0
    count = size * size
    rows = []
    right = []
    for cell in range(count):
        mask = 0
        for press in range(count):
            if cell in _trial_lights_neighbors(press, size):
                mask |= 1 << press
        rows.append(mask)
        right.append(cells[cell] ^ target)

    pivot_columns: list[int] = []
    pivot_row = 0
    for column in range(count):
        selected = next(
            (row for row in range(pivot_row, count) if (rows[row] >> column) & 1),
            None,
        )
        if selected is None:
            continue
        rows[pivot_row], rows[selected] = rows[selected], rows[pivot_row]
        right[pivot_row], right[selected] = right[selected], right[pivot_row]
        for row in range(count):
            if row != pivot_row and ((rows[row] >> column) & 1):
                rows[row] ^= rows[pivot_row]
                right[row] ^= right[pivot_row]
        pivot_columns.append(column)
        pivot_row += 1
        if pivot_row >= count:
            break
    for row in range(pivot_row, count):
        if rows[row] == 0 and right[row]:
            raise TianjiTrialError("trial_lights_unsolvable")

    free_columns = [column for column in range(count) if column not in pivot_columns]
    best_solution = None
    for free_mask in range(1 << len(free_columns)):
        solution = [0] * count
        for offset, column in enumerate(free_columns):
            solution[column] = (free_mask >> offset) & 1
        solution_mask = sum((value << index) for index, value in enumerate(solution))
        for row, column in enumerate(pivot_columns):
            parity = (rows[row] & solution_mask).bit_count() % 2
            solution[column] = right[row] ^ parity
        if best_solution is None or sum(solution) < sum(best_solution):
            best_solution = solution
    solution = best_solution or [0] * count
    presses = [index for index, value in enumerate(solution) if value]
    final_cells = list(cells)
    for index in presses:
        for target_index in _trial_lights_neighbors(index, size):
            final_cells[target_index] ^= 1
    if any(value != target for value in final_cells):
        raise TianjiTrialError("trial_lights_unsolved")
    duration = _trial_duration_ms(challenge, len(presses))
    step = max(80, duration // max(1, len(presses) + 1))
    return {
        "mode": "tianjiLightsOutV1",
        "challengeId": challenge.get("challengeId"),
        "durationMs": duration,
        "events": [
            {"index": index, "t": step * (event_index + 1)}
            for event_index, index in enumerate(presses)
        ],
        "cells": final_cells,
    }


def _solve_memory(challenge: dict[str, Any]) -> dict[str, Any]:
    cards = [card for card in (challenge.get("cards") or []) if isinstance(card, dict)]
    pairs: dict[str, list[str]] = {}
    for card in cards:
        card_id = str(card.get("id") or "").strip()
        pair = str(card.get("pair") or "").strip()
        if card_id and pair:
            pairs.setdefault(pair, []).append(card_id)
    ordered = []
    for pair in pairs.values():
        if len(pair) != 2:
            raise TianjiTrialError("trial_memory_invalid")
        ordered.extend(pair)
    if len(ordered) != len(cards) or not ordered:
        raise TianjiTrialError("trial_memory_invalid")
    preview_ms = max(
        1800,
        int(challenge.get("previewMs") or challenge.get("preview_ms") or 3600),
    )
    first_event = preview_ms + 300
    step = 180
    duration = max(
        _trial_duration_ms(challenge, len(ordered)),
        first_event + step * max(0, len(ordered) - 1) + 450,
    )
    return {
        "mode": "tianjiMemoryV1",
        "challengeId": challenge.get("challengeId"),
        "durationMs": duration,
        "events": [
            {"id": card_id, "index": index, "t": first_event + step * index}
            for index, card_id in enumerate(ordered)
        ],
        "mismatches": 0,
    }


def _solve_stargaze(challenge: dict[str, Any]) -> dict[str, Any]:
    stars = [star for star in (challenge.get("stars") or []) if isinstance(star, dict)]
    locked_ids = set(
        str(item)
        for item in (challenge.get("lockedNodeIds") or challenge.get("locked_node_ids") or [])
    )
    angles = {}
    moves = 0
    for star in stars:
        star_id = str(star.get("id") or "").strip()
        if not star_id:
            continue
        current = float(star.get("angle") or 0)
        target = float(star.get("targetAngle", star.get("target_angle", current)) or 0)
        locked = bool(star.get("locked")) or star_id in locked_ids
        angles[star_id] = (current if locked else target) % 360
        if not locked:
            moves += 1
    if not angles:
        raise TianjiTrialError("trial_angles_invalid")
    return {
        "mode": "tianjiStargazeV1",
        "challengeId": challenge.get("challengeId"),
        "durationMs": _trial_duration_ms(challenge, moves),
        "angles": angles,
        "moves": moves,
        "misses": 0,
    }


def _solve_meridian(challenge: dict[str, Any]) -> dict[str, Any]:
    sequence = [str(item) for item in (challenge.get("sequence") or []) if str(item)]
    if not sequence:
        raise TianjiTrialError("trial_sequence_invalid")
    preview_end = 620 + len(sequence) * 430
    first_event = preview_end + 300
    step = 200
    duration = max(
        _trial_duration_ms(challenge, len(sequence)),
        first_event + step * max(0, len(sequence) - 1) + 450,
    )
    return {
        "mode": "tianjiMeridianV1",
        "challengeId": challenge.get("challengeId"),
        "durationMs": duration,
        "events": [
            {"id": point_id, "index": index, "t": first_event + step * index}
            for index, point_id in enumerate(sequence)
        ],
        "moves": len(sequence),
        "misses": 0,
    }


def _trial_orientation(
    left: dict[str, float],
    middle: dict[str, float],
    right: dict[str, float],
) -> float:
    return (middle["x"] - left["x"]) * (right["y"] - left["y"]) - (
        middle["y"] - left["y"]
    ) * (right["x"] - left["x"])


def _trial_point_on_segment(
    left: dict[str, float],
    middle: dict[str, float],
    right: dict[str, float],
) -> bool:
    epsilon = 0.000001
    return (
        min(left["x"], right["x"]) - epsilon
        <= middle["x"]
        <= max(left["x"], right["x"]) + epsilon
        and min(left["y"], right["y"]) - epsilon
        <= middle["y"]
        <= max(left["y"], right["y"]) + epsilon
        and abs(_trial_orientation(left, right, middle)) <= epsilon
    )


def _trial_segments_cross(
    first_left: dict[str, float],
    first_right: dict[str, float],
    second_left: dict[str, float],
    second_right: dict[str, float],
) -> bool:
    """Mirror the Mini App's crossing test for non-adjacent graph edges."""
    epsilon = 0.000001
    first_a = _trial_orientation(first_left, first_right, second_left)
    first_b = _trial_orientation(first_left, first_right, second_right)
    second_a = _trial_orientation(second_left, second_right, first_left)
    second_b = _trial_orientation(second_left, second_right, first_right)
    if first_a * first_b < -epsilon and second_a * second_b < -epsilon:
        return True
    return bool(
        abs(first_a) <= epsilon
        and _trial_point_on_segment(first_left, second_left, first_right)
        or abs(first_b) <= epsilon
        and _trial_point_on_segment(first_left, second_right, first_right)
        or abs(second_a) <= epsilon
        and _trial_point_on_segment(second_left, first_left, second_right)
        or abs(second_b) <= epsilon
        and _trial_point_on_segment(second_left, first_right, second_right)
    )


def _trial_crossing_count(
    edges: list[tuple[str, str]],
    positions: dict[str, dict[str, float]],
) -> int:
    count = 0
    for index, (first_left, first_right) in enumerate(edges):
        if first_left not in positions or first_right not in positions:
            return len(edges) + 1
        for second_left, second_right in edges[index + 1 :]:
            if {first_left, first_right} & {second_left, second_right}:
                continue
            if second_left not in positions or second_right not in positions:
                return len(edges) + 1
            if _trial_segments_cross(
                positions[first_left],
                positions[first_right],
                positions[second_left],
                positions[second_right],
            ):
                count += 1
    return count


def _trial_minimum_node_distance(positions: dict[str, dict[str, float]]) -> float:
    points = list(positions.values())
    minimum = float("inf")
    for index, left in enumerate(points):
        for right in points[index + 1 :]:
            minimum = min(
                minimum,
                math.hypot(left["x"] - right["x"], left["y"] - right["y"]),
            )
    return minimum


def _trial_layout_valid(
    edges: list[tuple[str, str]],
    positions: dict[str, dict[str, float]],
    *,
    minimum_distance: float = 4.0,
) -> bool:
    return bool(
        positions
        and all(
            math.isfinite(point["x"])
            and math.isfinite(point["y"])
            and 4.0 <= point["x"] <= 96.0
            and 4.0 <= point["y"] <= 96.0
            for point in positions.values()
        )
        and _trial_crossing_count(edges, positions) == 0
        and _trial_minimum_node_distance(positions) >= minimum_distance
    )


def _trial_fixed_planar_layout(
    raw_layout: dict[str, Any],
    locked_positions: dict[str, dict[str, float]],
    edges: list[tuple[str, str]],
) -> dict[str, dict[str, float]] | None:
    """Fit a straight-line planar drawing onto one fixed trial node."""
    if len(locked_positions) != 1:
        return None
    locked_id, locked_point = next(iter(locked_positions.items()))
    if locked_id not in raw_layout:
        return None
    base_x, base_y = raw_layout[locked_id]
    vectors = {
        node_id: (float(point[0]) - float(base_x), float(point[1]) - float(base_y))
        for node_id, point in raw_layout.items()
    }
    best: tuple[float, dict[str, dict[str, float]]] | None = None
    for degree in range(0, 360, 5):
        radians = math.radians(degree)
        cosine = math.cos(radians)
        sine = math.sin(radians)
        rotated = {
            node_id: (
                cosine * vector[0] - sine * vector[1],
                sine * vector[0] + cosine * vector[1],
            )
            for node_id, vector in vectors.items()
        }
        scale_limits = []
        for delta_x, delta_y in rotated.values():
            if delta_x > 0:
                scale_limits.append((96.0 - locked_point["x"]) / delta_x)
            elif delta_x < 0:
                scale_limits.append((locked_point["x"] - 4.0) / -delta_x)
            if delta_y > 0:
                scale_limits.append((96.0 - locked_point["y"]) / delta_y)
            elif delta_y < 0:
                scale_limits.append((locked_point["y"] - 4.0) / -delta_y)
        positive_limits = [limit for limit in scale_limits if limit > 0]
        if not positive_limits:
            continue
        scale = min(positive_limits) * 0.98
        positions = {
            node_id: {
                "x": locked_point["x"] + delta_x * scale,
                "y": locked_point["y"] + delta_y * scale,
            }
            for node_id, (delta_x, delta_y) in rotated.items()
        }
        positions[locked_id] = dict(locked_point)
        if not _trial_layout_valid(edges, positions):
            continue
        score = _trial_minimum_node_distance(positions)
        if best is None or score > best[0]:
            best = (score, positions)
    return best[1] if best else None


def _solve_planarity(challenge: dict[str, Any]) -> dict[str, Any]:
    nodes = [node for node in (challenge.get("nodes") or []) if isinstance(node, dict)]
    edges = [edge for edge in (challenge.get("edges") or []) if isinstance(edge, dict)]
    graph = nx.Graph()
    node_by_id = {}
    for node in nodes:
        node_id = str(node.get("id") or "").strip()
        if node_id:
            graph.add_node(node_id)
            node_by_id[node_id] = node
    graph_edges = []
    for edge in edges:
        left = str(edge.get("from") or "").strip()
        right = str(edge.get("to") or "").strip()
        if left in graph and right in graph and left != right:
            graph.add_edge(left, right)
            normalized = tuple(sorted((left, right)))
            if normalized not in graph_edges:
                graph_edges.append(normalized)
    planar, embedding = nx.check_planarity(graph)
    if not planar or not graph.nodes:
        raise TianjiTrialError("trial_planarity_invalid")
    locked = set(
        str(item)
        for item in (challenge.get("lockedNodeIds") or challenge.get("locked_node_ids") or [])
    )
    locked.update(
        str(node.get("id") or "") for node in nodes if bool(node.get("locked"))
    )

    def scaled_layout(raw_layout: dict[str, Any]) -> dict[str, dict[str, float]]:
        xs = [float(point[0]) for point in raw_layout.values()]
        ys = [float(point[1]) for point in raw_layout.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(1.0, max_x - min_x)
        span_y = max(1.0, max_y - min_y)
        return {
            node_id: {
                "x": 8.0 + (float(point[0]) - min_x) * 84.0 / span_x,
                "y": 8.0 + (float(point[1]) - min_y) * 84.0 / span_y,
            }
            for node_id, point in raw_layout.items()
        }

    raw_layout = nx.combinatorial_embedding_to_pos(embedding)
    locked_positions = {
        node_id: {
            "x": float(
                50
                if node_by_id[node_id].get("x") is None
                else node_by_id[node_id].get("x")
            ),
            "y": float(
                50
                if node_by_id[node_id].get("y") is None
                else node_by_id[node_id].get("y")
            ),
        }
        for node_id in locked
        if node_id in node_by_id
    }
    positions = scaled_layout(raw_layout)
    fixed_layout = _trial_fixed_planar_layout(
        raw_layout,
        locked_positions,
        graph_edges,
    )
    if fixed_layout is not None:
        positions = fixed_layout
    elif locked:
        # Locked trial nodes must keep their exact coordinates.  Replacing the
        # locked set with one helper vertex preserves all other adjacencies and
        # lets the planar embedding place movable nodes around that fixed core.
        helper = "__locked_trial_core__"
        reduced = graph.copy()
        locked_neighbors = set()
        for node_id in locked:
            if node_id in reduced:
                locked_neighbors.update(reduced.neighbors(node_id))
        reduced.remove_nodes_from(locked)
        reduced.add_node(helper)
        reduced.add_edges_from(
            (helper, neighbor)
            for neighbor in locked_neighbors
            if neighbor in reduced and neighbor != helper
        )
        reduced_planar, reduced_embedding = nx.check_planarity(reduced)
        if not reduced_planar:
            raise TianjiTrialError("trial_planarity_invalid")
        positions.update(
            {
                node_id: point
                for node_id, point in scaled_layout(
                    nx.combinatorial_embedding_to_pos(reduced_embedding)
                ).items()
                if node_id != helper
            }
        )
        positions.update(locked_positions)
    if not _trial_layout_valid(graph_edges, positions):
        raise TianjiTrialError("trial_planarity_unsolved")
    moves = sum(
        1
        for node in nodes
        if str(node.get("id") or "") not in locked and not node.get("locked")
    )
    return {
        "mode": "tianjiPlanarityV1",
        "challengeId": challenge.get("challengeId"),
        "durationMs": _trial_duration_ms(challenge, moves),
        "positions": positions,
        "moves": moves,
        "misses": 0,
    }


def solve_tianji_trial_challenge(challenge: Any) -> dict[str, Any]:
    if not isinstance(challenge, dict) or not challenge.get("challengeId"):
        raise TianjiTrialError("trial_challenge_missing")
    mode = str(challenge.get("mode") or "")
    solvers = {
        "tianjiLightsOutV1": _solve_lights_out,
        "tianjiMemoryV1": _solve_memory,
        "tianjiStargazeV1": _solve_stargaze,
        "tianjiMeridianV1": _solve_meridian,
        "tianjiPlanarityV1": _solve_planarity,
    }
    solver = solvers.get(mode)
    if solver is None:
        raise TianjiTrialError("trial_mode_unsupported")
    try:
        return solver(challenge)
    except TianjiTrialError:
        raise
    except (ValueError, TypeError, LookupError, ArithmeticError, nx.NetworkXException) as exc:
        # Malformed server fields (int("x"), a missing key, a degenerate graph):
        # the runner records a retry for this identity and moves on.
        raise TianjiTrialError("trial_challenge_invalid") from exc
