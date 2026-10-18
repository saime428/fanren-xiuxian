"""Account-local timing evidence around the unmodified reference begin flow."""
from __future__ import annotations

from collections import deque
from contextvars import ContextVar
import math
import statistics
import time


WINDOW_READ_TIMEOUT_SECONDS = 1
# A poll whose connection never completed its handshake gets one wider budget.
WINDOW_READ_COLD_TIMEOUT_SECONDS = 2
# Turnstile verification routinely takes 100-150 s after the last /start, so
# 90 s discarded every same-event sample (2026-09-17..20: sample_count=0).
CLOCK_SAMPLE_MAX_AGE_SECONDS = 300
READ_POOL_KEEPALIVE_SECONDS = 40
READ_POOL_KEEPALIVE_MAX_SECONDS = 300
# The read sockets open before /begin so their handshakes never share the
# network with the probe whose round trip sets the battle clock (opening took
# 0.8-1.8 s on 09-24..09-27). Each /state warm-up gives up after 4 s, so this
# bound only matters if the warm-up hangs; /begin then goes ahead anyway.
READ_POOL_WARM_WAIT_SECONDS = 5
# A failed read socket is re-opened with /state off the poll path. On 09-25 new
# TLS handshakes failed for about six seconds, so allow a few wide attempts.
READ_SOCKET_REWARM_ATTEMPTS = 3
READ_SOCKET_REWARM_TIMEOUT_SECONDS = 4
LEAD_CAP_MIN_SAMPLES = 3
# The reference aims every charge at a 1000 ms server hold. Damage keeps rising
# with hold inside the 520-1250 ms PERFECT band: over 300 PERFECT receipts
# (09-15..09-23, adjusted for window index and |delta|) 1050-1099 ms paid +5%,
# 1100-1199 ms +8.5% and 1200+ ms +11.5% against 1000-1049 ms, and the >=1100 ms
# group led on each of the seven event days. Ordinary hits land within
# +54..+68 ms (p95, max +137) of the plan now that strike wakes are on time
# (v23), so a 1100 ms aim keeps them under 1250; only 10 of those 300 would
# have crossed it. Late-transit spikes overrun either way and miss on delta.
# 09-24 at 1100: PERFECT holds centred on 1085-1105, ordinary hits at most
# +50 ms over the plan (p95 +24), and damage still rose 11% per 100 ms; at
# 1150 one of that day's 84 PERFECT hits would have crossed 1250.
TARGET_SERVER_HOLD_MS = 1150

# VPS warm actions are about 110-120 ms. A 500 ms fixed cutoff admitted
# queueing spikes as clock bias; these limits scale with each battle's floor.
TIMING_POLICY_VERSION = "qyz-vps-timing-v3"
DRIFT_RTT_FLOOR_MS = 250
DRIFT_RTT_MULTIPLIER = 2.5
DRIFT_GAIN = 0.45
DRIFT_MAX_STEP_MS = 80
DRIFT_HISTORY_SIZE = 3
DRIFT_LOCAL_LATE_LIMIT_MS = 20
HOLD_RTT_RATIO_LIMIT = 1.6
HOLD_RTT_DIFFERENCE_FLOOR_MS = 60
ARRIVAL_ADVANCE_MAX_MS = 60
ARRIVAL_EARLY_GUARD_MS = 40
ARRIVAL_CHARGE_GUARD_MS = 25
TICKET_EXTRA_DELAY_MAX_MS = 20
TICKET_HOLD_EDGE_GUARD_MS = 20
_ACTIVE_WINDOW = ContextVar('qyz_arrival_budget', default=None)


def arrival_budget(*, center_ms, perfect_ms, elapsed_ms, hold_ms,
                   request_lead_ms, drift_ms, account_offset_ms=0):
    """Spend known window headroom, never a future request's latency.

    Move BOTH charge and release earlier. A late reveal cannot pay for this
    margin by shortening the planned hold. Reserve one maximum feedback step
    plus an early-edge guard for a normal fast reply after a queue drains.
    """
    values = [center_ms, perfect_ms, elapsed_ms, hold_ms, request_lead_ms, drift_ms, account_offset_ms]
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in values):
        return {'advance_ms': 0, 'reason': 'invalid_timing'}
    charge_start = center_ms + account_offset_ms - request_lead_ms - drift_ms - hold_ms
    charge_room = charge_start - elapsed_ms
    early_room = perfect_ms - ARRIVAL_EARLY_GUARD_MS - DRIFT_MAX_STEP_MS - max(0, drift_ms - account_offset_ms)
    advance = max(0, math.floor(min(ARRIVAL_ADVANCE_MAX_MS, charge_room - ARRIVAL_CHARGE_GUARD_MS,
                                    charge_start - ARRIVAL_CHARGE_GUARD_MS, early_room)))
    return {'advance_ms': advance, 'charge_room_ms': round(charge_room, 3),
            'early_room_ms': round(early_room, 3), 'reason': 'window_headroom' if advance else
            'late_reveal' if charge_room <= ARRIVAL_CHARGE_GUARD_MS else 'early_edge_budget'}


def ticket_release_headroom(*, target_ms, now_ms, charge_sent_ms, charge_received_ms,
                            center_ms, perfect_ms, request_lead_ms, drift_ms):
    """Spend at most half the existing 40ms hold-edge margin after a slow charge.

    The reference's earliest possible ticket timestamp makes its minimum-age
    rescue infeasible on a slow reply. Retain 20ms at that hold edge and the
    entire 40ms arrival-edge guard. No future hit RTT or verdict is available.
    """
    values=(target_ms,now_ms,charge_sent_ms,charge_received_ms,center_ms,perfect_ms,request_lead_ms,drift_ms)
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in values):
        return {'extra_delay_ms':0,'reason':'invalid_timing'}
    if charge_sent_ms<0 or charge_received_ms<charge_sent_ms or perfect_ms<=0:
        return {'extra_delay_ms':0,'reason':'invalid_charge'}
    if now_ms>target_ms:
        return {'extra_delay_ms':0,'reason':'release_already_due'}
    from qyz_reference import world_boss_features as reference
    arrival=max(0,request_lead_ms+drift_ms)
    credit=max(0,request_lead_ms)+arrival
    minimum=max(charge_sent_ms+reference.WORLD_BOSS_HOLD_MIN_MS,
                charge_received_ms+reference.WORLD_BOSS_HOLD_MIN_MS+reference.WORLD_BOSS_HOLD_RESCUE_MARGIN_MS-credit)
    window_cap=center_ms+perfect_ms-arrival-40
    old_cap=min(window_cap,charge_sent_ms+reference.WORLD_BOSS_HOLD_MAX_MS-arrival-40)
    cap=min(window_cap,charge_sent_ms+reference.WORLD_BOSS_HOLD_MAX_MS-arrival-TICKET_HOLD_EDGE_GUARD_MS)
    delay=max(0,math.floor(min(TICKET_EXTRA_DELAY_MAX_MS,minimum-target_ms,cap-target_ms))) if minimum>old_cap else 0
    return {'extra_delay_ms':delay,'reference_target_ms':target_ms,'adjusted_target_ms':target_ms+delay,
            'minimum_age_target_ms':minimum,'reference_latest_ms':old_cap,'latest_ms':cap,
            'window_latest_ms':window_cap,'hold_edge_guard_ms':TICKET_HOLD_EDGE_GUARD_MS,
            'reason':'bounded_ticket_headroom' if delay else 'no_additional_headroom'}


class VpsTimingMixin:
    """Causal account-local feedback around the unchanged reference protocol."""

    async def _hit_window(self, entry, init_data, session_token, challenge_id,
                          battle_start, window, window_index=0, request_lead_ms=0,
                          charge_required=True):
        context = {'owner': self, 'window_id': window.get('id'), 'battle_start': battle_start,
                   'request_lead_ms': request_lead_ms, 'charge_required': charge_required,
                   'window':window, 'charge_sent_ms':None, 'charge_received_ms':None, 'ticket_granted':False,
                   'budget': {'advance_ms': 0, 'reason': 'not_planned'}}
        token = _ACTIVE_WINDOW.set(context)
        try:
            result = await super()._hit_window(entry, init_data, session_token, challenge_id,
                                               battle_start, window, window_index,
                                               request_lead_ms, charge_required)
            if isinstance(result.get('diagnostic'), dict):
                result['diagnostic']['arrival_budget'] = dict(context['budget'])
                guard=context.get('ticket_guard') or {}
                result['diagnostic']['ticket_guard_budget']=dict(guard)
                if guard.get('extra_delay_ms',0)>0:
                    diagnostic=result['diagnostic']
                    diagnostic['reference_target_ms']=diagnostic.get('target_ms')
                    diagnostic['target_ms']=guard['adjusted_target_ms']
                    diagnostic['wake_lateness_ms']=diagnostic.get('sent_elapsed_ms',guard['adjusted_target_ms'])-guard['adjusted_target_ms']
            return result
        finally:
            _ACTIVE_WINDOW.reset(token)

    async def _request(self,origin,path,payload,**options):
        context=_ACTIVE_WINDOW.get()
        charge=(context is not None and context['owner'] is self and path.endswith('/charge-start')
                and payload.get('windowId')==context['window_id'])
        if charge:
            context['charge_sent_ms']=round((self.monotonic()-context['battle_start'])*1000)
        result=await super()._request(origin,path,payload,**options)
        if charge:
            context['charge_received_ms']=round((self.monotonic()-context['battle_start'])*1000)
            context['ticket_granted']=isinstance(result.get('chargeTicket'),str) and bool(result['chargeTicket'].strip())
        return result

    def _ticket_plan(self,target_ms):
        context=_ACTIVE_WINDOW.get()
        if context is None or context['owner'] is not self or not context.get('ticket_granted'):
            return {'extra_delay_ms':0,'reason':'no_confirmed_ticket'}
        drift_ms=self._drift_lead_ms()
        inputs=(target_ms,context['charge_sent_ms'],context['charge_received_ms'],
                context['window'].get('centerMs'),context['window'].get('perfectMs'),
                context['request_lead_ms'],drift_ms)
        # An unrelated timing signal may arrive inside the extra 20ms wait.
        # Keep an already planned deadline when its causal inputs are unchanged;
        # a genuinely revised target must pass the guards again at the new time.
        if context.get('ticket_guard_inputs')==inputs:
            return context['ticket_guard']
        guard=ticket_release_headroom(target_ms=target_ms,now_ms=round((self.monotonic()-context['battle_start'])*1000),
            charge_sent_ms=context['charge_sent_ms'],charge_received_ms=context['charge_received_ms'],
            center_ms=context['window'].get('centerMs'),perfect_ms=context['window'].get('perfectMs'),
            request_lead_ms=context['request_lead_ms'],drift_ms=drift_ms)
        context['ticket_guard_inputs']=inputs
        context['ticket_guard']=guard
        return guard

    def _window_progress(self,window,window_index,battle_start,phase,**fields):
        if phase=='charging' and type(fields.get('release_target_ms')) in (int,float):
            guard=self._ticket_plan(fields['release_target_ms'])
            if guard.get('extra_delay_ms',0)>0:
                fields={**fields,'release_target_ms':guard['adjusted_target_ms'],
                        'hold_guard':{**fields.get('hold_guard',{}),'ticket_headroom':dict(guard)}}
        super()._window_progress(window,window_index,battle_start,phase,**fields)
        return fields

    async def _wait_for_timing_target(self,plan):
        context=_ACTIVE_WINDOW.get()
        if context is None or context['owner'] is not self or not context.get('ticket_granted'):
            return await super()._wait_for_timing_target(plan)
        def adjusted_plan():
            target=plan()
            target_ms=round((target-context['battle_start'])*1000)
            guard=self._ticket_plan(target_ms)
            return target+guard.get('extra_delay_ms',0)/1000
        return await super()._wait_for_timing_target(adjusted_plan)

    def _hit_offset_ms(self, window):
        original = super()._hit_offset_ms(window)
        context = _ACTIVE_WINDOW.get()
        if (context is None or context['owner'] is not self or context['window_id'] != window.get('id')
                or not context['charge_required']):
            return original
        budget = arrival_budget(center_ms=window.get('centerMs'), perfect_ms=window.get('perfectMs'),
                                elapsed_ms=(self.monotonic() - context['battle_start']) * 1000,
                                hold_ms=self._planned_hold_ms(), request_lead_ms=context['request_lead_ms'],
                                drift_ms=self._drift_lead_ms(), account_offset_ms=original)
        context['budget'] = budget
        # The reference already subtracts intentional account offsets from its
        # drift feedback and reports the actual release/hold in the proof.
        return original - budget['advance_ms']

    def _record_drift(self, server_delta_ms, center_ms=None, sent_elapsed_ms=None,
                      request_completed_elapsed_ms=None, *, request_lead_ms=0,
                      account_offset_ms=0, request_interval_ms=None, tolerance_ms=None):
        context=_ACTIVE_WINDOW.get()
        if context is not None and context['owner'] is self:
            account_offset_ms += (context.get('ticket_guard') or {}).get('extra_delay_ms',0)
        options = dict(request_lead_ms=request_lead_ms, account_offset_ms=account_offset_ms,
                       request_interval_ms=request_interval_ms, tolerance_ms=tolerance_ms)
        if center_ms is None or (sent_elapsed_ms is None and request_interval_ms is None):
            return super()._record_drift(server_delta_ms, center_ms, sent_elapsed_ms,
                                         request_completed_elapsed_ms, **options)
        result = self._infer_arrival_direction(server_delta_ms, center_ms, sent_elapsed_ms,
                                               request_completed_elapsed_ms, **options)
        if result is None:
            return None
        rtt = self._finite_ms(result.get("request_rtt_ms"))
        if rtt is not None and rtt <= 0:
            return super()._record_drift(server_delta_ms, center_ms, sent_elapsed_ms,
                                         request_completed_elapsed_ms, **options)
        before = float(self._drift_ms)
        lead = self._finite_ms(request_lead_ms) or 0.0
        limit = max(DRIFT_RTT_FLOOR_MS, 2 * max(0, lead) * DRIFT_RTT_MULTIPLIER)
        result.update(drift_before_ms=self._rounded_ms(before), drift_after_ms=self._rounded_ms(before),
                      update_applied=False, update_mode="vps_skipped", timing_policy=TIMING_POLICY_VERSION,
                      rtt_limit_ms=round(limit, 3))
        direction = result.get("direction")
        self._drift_direction_counts[direction] = self._drift_direction_counts.get(direction, 0) + 1
        if direction not in {"early", "late"}:
            result["feedback_reason"] = "direction_unconfirmed"
            return result
        if rtt is None or rtt > limit:
            result["feedback_reason"] = "request_tail_latency"
            return result
        error = self._finite_ms(result.get("arrival_offset_after_account_ms"))
        residual = self._finite_ms(result.get("sample_drift_ms"))
        confidence = self._finite_ms(result.get("direction_confidence"))
        if error is None or residual is None or confidence is None or confidence < .7:
            result["feedback_reason"] = "insufficient_timing_evidence"
            return result
        # A blocked charge or late local wake is not a persistent network shift.
        wake = before + error - residual
        result["local_lateness_ms"] = round(wake, 3)
        if abs(wake) > DRIFT_LOCAL_LATE_LIMIT_MS:
            result["feedback_reason"] = "local_schedule_delay"
            return result
        sign = 1 if error > 0 else -1 if error < 0 else 0
        if not sign:
            result["feedback_reason"] = "on_target"
            return result
        if getattr(self, "_vps_feedback_sign", None) != sign:
            self._vps_feedback_history = []
        self._vps_feedback_sign = sign
        history = self._vps_feedback_history
        # This is an absolute residual delay, NOT an error to be confused with
        # the next absolute lead (or repeatedly integrated after a queue spike).
        history.append(residual)
        del history[:-DRIFT_HISTORY_SIZE]
        target = float(statistics.median(history))
        step = DRIFT_GAIN * confidence * (target - before)
        if step * sign < 0:
            # Even same-sign samples can lag a newly changed estimate.
            target = residual
            step = DRIFT_GAIN * confidence * (target - before)
        if step * sign <= 0:
            result["feedback_reason"] = "no_directional_correction"
            return result
        from qyz_reference import world_boss_features as reference
        step = max(-DRIFT_MAX_STEP_MS, min(DRIFT_MAX_STEP_MS, step))
        self._drift_ms = max(reference.WORLD_BOSS_DRIFT_MIN_MS,
                             min(reference.WORLD_BOSS_DRIFT_MAX_MS, before + step))
        self._drift_samples += 1
        self._drift_history = list(history)
        self._drift_weight_history = [(value, 1.0) for value in history]
        if self._drift_ms != before:
            self._notify_timing_update()
        result.update(drift_after_ms=self._rounded_ms(self._drift_ms), update_applied=self._drift_ms != before,
                      update_mode="vps_residual_ewma", feedback_reason="bounded_directional_correction",
                      gain=DRIFT_GAIN * confidence, residual_target_ms=round(target, 3), history_size=len(history))
        return result

    @classmethod
    def _hold_feedback_quality(cls, charge_request, hit_request, request_lead_ms):
        quality = super()._hold_feedback_quality(charge_request, hit_request, request_lead_ms)
        if not quality["eligible"]:
            return quality
        charge, hit = quality["charge_request_ms"], quality["hit_request_ms"]
        limit = max(DRIFT_RTT_FLOOR_MS, 2 * max(0, request_lead_ms) * DRIFT_RTT_MULTIPLIER)
        quality.update(request_limit_ms=limit, timing_policy=TIMING_POLICY_VERSION)
        if max(charge, hit) > limit:
            return {**quality, "eligible": False, "reason": "vps_request_tail_latency"}
        difference = abs(charge - hit)
        # An asymmetric one-off RPC must not shorten every following charge.
        if (difference > max(HOLD_RTT_DIFFERENCE_FLOOR_MS, request_lead_ms)
                and max(charge, hit) > max(1, min(charge, hit)) * HOLD_RTT_RATIO_LIMIT):
            return {**quality, "eligible": False, "reason": "vps_asymmetric_requests"}
        return quality

    def _planned_hold_ms(self):
        from qyz_reference import world_boss_features as reference
        # Clamp after applying the server target and learned skew together.
        # Adding 150 AFTER the old clamp incorrectly raised the minimum to 710.
        correction = max(-reference.WORLD_BOSS_HOLD_SKEW_SAMPLE_MAX_MS,
                         min(reference.WORLD_BOSS_HOLD_SKEW_SAMPLE_MAX_MS, self._hold_skew_ms))
        return int(round(max(reference.WORLD_BOSS_HOLD_PLAN_MIN_MS,
                             min(reference.WORLD_BOSS_HOLD_PLAN_MAX_MS, TARGET_SERVER_HOLD_MS - correction))))


class AdmissionClock:
    """Reject an inflated begin RTT using this battle's recent warm /start calls.

    Begin can include TLS setup, queuing, and Siteverify processing before the
    server creates startsInMs. Applying half that duration twice (clock and
    release lead) advances every strike. Ordinary same-account /start replies
    provide a fallback without additional traffic, another begin, or future hits.
    """
    def __init__(self, monotonic=time.monotonic):
        self.now = monotonic
        self.samples = deque(maxlen=5)

    def observe_start(self, trace):
        attempts = trace.get("attempts", []) if isinstance(trace, dict) else []
        if len(attempts) != 1 or not isinstance(attempts[0], dict):
            return
        attempt = attempts[0]
        value = attempt.get("duration_ms")
        if (attempt.get("ok") is not True or isinstance(value, bool)
                or not isinstance(value, (int, float)) or not math.isfinite(value)
                or not 0 < value <= 10000):
            return
        self.samples.append((self.now(), float(value)))

    def refine_begin(self, trace):
        baseline = trace.get("clock_rtt_ms")
        if (isinstance(baseline, bool) or not isinstance(baseline, (int, float))
                or not math.isfinite(baseline) or baseline <= 0):
            return
        now = self.now()
        samples = [(at, value) for at, value in self.samples
                   if 0 <= now - at <= CLOCK_SAMPLE_MAX_AGE_SECONDS]
        values = [value for _, value in samples]
        evidence = {"source": "same_account_same_event_start", "sample_count": len(values),
                    "sample_rtts_ms": values, "begin_reference_ms": baseline,
                    "applied": False}
        trace["network_reference"] = evidence
        if len(values) < 3:
            evidence["decision"] = "insufficient_recent_samples"
            return
        # Server-side work (room joins, long polls) only inflates a round trip;
        # the network floor is the minimum, supported by a second nearby sample.
        reference = min(values)
        stable = sum(abs(value - reference) <= max(60, reference * .35) for value in values)
        evidence.update(median_ms=statistics.median(values), floor_ms=reference, stable_samples=stable,
                        oldest_age_ms=round((now - samples[0][0]) * 1000))
        # Only a clear inflation changes the audited source estimate; normal begin
        # variation stays. A round trip can only be inflated, never shortened, so
        # the floor needs no second sample near it: on 2026-09-22 @demo_player_04's
        # /start samples were 817/1092/338/2753/1507 ms (one near the floor) and
        # its /begin probe 2483 ms; keeping the probe set the battle clock about
        # one second early and every window was charged late (5/16, 0 PERFECT).
        if baseline < reference * 1.5 or baseline - reference < 150:
            evidence["decision"] = "keep_reference_begin"
            return
        trace["clock_rtt_ms"] = int(round(reference))
        trace["clock_rtt_source"] = "recent_account_start"
        evidence.update(applied=True, decision="inflated_begin_rtt")


def effective_request_lead(request_lead_ms: int, diagnostics: list[dict] | None, *,
                           min_samples: int = LEAD_CAP_MIN_SAMPLES) -> tuple[int, dict]:
    """Cap the static strike lead at half the observed warm action round trip.

    The lead comes from the pre-verification ``/begin`` probe. Since 09-17 that
    probe measured 560-810 ms while warm strikes took 360-400 ms, so every hit
    was scheduled 100-200 ms early and the drift controller's -200 ms floor could
    not recover (09-20 @demo_player_05: 5/16 PERFECT, server deltaMs 210-390).
    Only completed, connection-reused action requests of this battle count.
    """
    evidence: dict = {"input_lead_ms": int(request_lead_ms), "applied": False, "sample_count": 0}
    values = []
    for row in diagnostics or ():
        if not isinstance(row, dict) or row.get("pool") != "action" or row.get("http_status") != 200:
            continue
        if row.get("connection_reused") is not True:
            continue
        value = row.get("duration_ms")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 10000:
            continue
        values.append(float(value))
    evidence["sample_count"] = len(values)
    if len(values) < min_samples:
        evidence["decision"] = "insufficient_warm_samples"
        return int(request_lead_ms), evidence
    floor = min(values)
    cap = int(math.ceil(floor / 2))
    evidence.update(floor_rtt_ms=round(floor), cap_ms=cap)
    if request_lead_ms <= cap:
        evidence["decision"] = "lead_within_cap"
        return int(request_lead_ms), evidence
    evidence.update(applied=True, decision="lead_capped_to_warm_floor", lead_ms=cap)
    return cap, evidence
