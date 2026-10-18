import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from automation_dossiers import evidence_paths, summarize_file
from flow_registry import active_entries, launchagent_labels
from miniapp_shared.state import write_json
from scripts.interval_flow_dispatch import dispatch_intervals

from . import ACCOUNT, FLOW, PLAYER_ID, VERSION
from .health import health
from .protocol import day
from .run_cycle import execute
from .runtime import Store
from .test_protocol import NOW
from .test_runtime import FakeClient


def entry():
    return {"account": ACCOUNT, "flow": FLOW, "kinds": ["send"], "enabled": True,
            "miniapp_module": FLOW, "interval_dispatch_seconds": 60}


def test_existing_dispatcher_owns_new_interval_without_new_daemon(tmp_path):
    registry = {"flows": [entry()]}
    assert launchagent_labels(active_entries(registry)) == ["ai.hermes.xiuxian.return-deadline-dispatch"]
    wrapper = tmp_path / "scripts" / f"send_{FLOW}_{ACCOUNT}.sh"
    wrapper.parent.mkdir(); wrapper.write_text("#!/bin/bash\nexit 0\n")
    launches = []
    def spawn(*args, **kwargs):
        launches.append((args, kwargs)); return SimpleNamespace(pid=123)
    for at in (NOW, NOW+30, NOW+60):
        dispatch_intervals(tmp_path, tmp_path/"state", registry, now=at, spawn=spawn)
    assert len(launches) == 2
    assert str(wrapper) in launches[0][0][0]


def test_dossier_has_numbers_and_hash_sources_without_pending_text_or_auth(tmp_path):
    directory = tmp_path/"state/accounts"/ACCOUNT/FLOW
    directory.mkdir(parents=True)
    data = {"flow": FLOW, "account": ACCOUNT, "actor_id": PLAYER_ID, "status": "waiting",
            "pending": {"last_reply": "DO_NOT_COPY", "token": "DO_NOT_COPY"},
            "board": {"star": "太阴", "points": 50, "debt": 0, "token": "DO_NOT_COPY"},
            "journey": {"count": 8, "limit": 8, "auth": "DO_NOT_COPY"},
            "events": [{"confirmed": True, "raw_reply": "DO_NOT_COPY"}],
            "outbox": [{"status": "sent", "text": "DO_NOT_COPY"}]}
    p = directory/"state.json"; p.write_text(json.dumps(data))
    before = p.read_bytes()
    result = summarize_file(p, tmp_path)
    assert result["exploration"]["journey"] == {"count": 8, "limit": 8}
    assert result["pending"] is True
    assert "DO_NOT_COPY" not in json.dumps(result)
    assert p.read_bytes() == before
    assert p in evidence_paths(tmp_path, f"ai.hermes.xiuxian.{ACCOUNT}.{FLOW}.send", entry())


def test_health_distinguishes_pending_result_from_future_cooldown(tmp_path):
    store = Store(tmp_path, clock=lambda: NOW)
    store.data.update(status="waiting", next_due_at=NOW+3600, journey={"count": 8, "limit": 8})
    store.save()
    assert health(store.directory, ACCOUNT, NOW)[0] == "ok"
    store.data["outbox"] = [{"status": "failed", "at": NOW}]
    store.save()
    assert health(store.directory, ACCOUNT, NOW)[1] == "exploration_report_retry"
    store.data["outbox"] = []
    store.data["pending"] = {"at": NOW-500, "command": ".探寻裂缝"}
    store.save()
    assert health(store.directory, ACCOUNT, NOW)[0] == "bad"
    assert health(store.directory, ACCOUNT, NOW)[1] == "exploration_result_unconfirmed"


def test_health_never_claims_old_day_is_today(tmp_path):
    store = Store(tmp_path, clock=lambda: NOW)
    store.data.update(status="waiting", next_due_at=NOW+172800)
    store.save()
    assert health(store.directory, ACCOUNT, NOW+86400)[1] == "exploration_new_day_pending"


def test_idle_ticks_do_not_reauthenticate_or_duplicate_reports(tmp_path, monkeypatch):
    import tianxing_exploration.run_cycle as module
    monkeypatch.setattr(module, "assert_account_allowed", lambda *a, **kw: None)
    client = FakeClient(count=8, history={"observed_day": day(NOW), "actions": [
        {"kind": "node", "at": NOW-60, "status": "settled", "confirmed": True},
        {"kind": "rift", "at": NOW-60, "status": "success", "confirmed": True}]})
    openings, notifications = [], []
    class Bridge:
        async def __aenter__(self):
            openings.append(True); return self
        async def __aexit__(self, *args):
            pass
    class Notifier:
        def __init__(self, *args): pass
        def send_once(self, key, text):
            notifications.append((key, text))
            return {"status": "delivered", "receipt": {"message_id": 200}}
    async def run():
        options = dict(bridge_factory=lambda *a: Bridge(), client_factory=lambda *a, **kw: client,
                       notifier_factory=Notifier, clock=lambda: NOW)
        first = await execute(tmp_path, **options)
        second = await execute(tmp_path, **options)
        return first, second
    first, second = asyncio.run(run())
    assert first["status"] == "waiting" and second["status"] == "scheduled_wait"
    assert len(openings) == len(notifications) == 1
    assert "本流程确认 0 次深入" in notifications[0][1]


def test_preparation_unknown_observation_not_falsely_marked_complete(tmp_path):
    from .runtime import Cycle
    from .protocol import ExplorationError
    from .test_runtime import no_sleep
    import pytest
    client = FakeClient(count=8, history={"actions": [], "observed_day": None})
    store = Store(tmp_path, clock=lambda: NOW)
    store.begin("preparation", ".观命", transport="miniapp")
    cycle = Cycle(store, client, clock=lambda: NOW, sleep=no_sleep)
    with pytest.raises(ExplorationError, match="observation_outcome_pending"):
        asyncio.run(cycle.run())
    assert store.data["observed_day"] is None
    assert store.data["pending"]
