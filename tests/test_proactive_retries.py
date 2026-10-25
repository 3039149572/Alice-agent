from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from astrbot_plugin_alice_agent.adapters.diagnostics import DailyProactiveLogger
from astrbot_plugin_alice_agent.adapters.jev.typesafe import TypesafeJevAdapter
from astrbot_plugin_alice_agent.adapters.storage.in_memory import InMemoryStorageAdapter
from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig
from astrbot_plugin_alice_agent.alice_agent.daily_events.service import DailyEventService
from astrbot_plugin_alice_agent.alice_agent.decision.fallback import DecisionAPIError, DecisionTimeoutError
from astrbot_plugin_alice_agent.alice_agent.decision.models import DecisionMode
from astrbot_plugin_alice_agent.alice_agent.decision.service import DecisionService
from astrbot_plugin_alice_agent.alice_agent.domain.event import AgentEvent
from astrbot_plugin_alice_agent.alice_agent.generation.context_builder import ContextBuilder
from astrbot_plugin_alice_agent.alice_agent.loop.scheduler import ContextScheduler
from astrbot_plugin_alice_agent.alice_agent.loop.store import SQLiteAutonomyStore
from astrbot_plugin_alice_agent.alice_agent.ports.logging import NullLogger
from astrbot_plugin_alice_agent.alice_agent.proactive.daily_policy import DailyProactivePolicy
from astrbot_plugin_alice_agent.alice_agent.proactive.decision_gate import ProactiveDecisionGate, ProactiveGateDecision
from astrbot_plugin_alice_agent.alice_agent.proactive.evaluator import ProactiveEvaluator
from astrbot_plugin_alice_agent.alice_agent.proactive.models import ScheduledContextEvent
from astrbot_plugin_alice_agent.alice_agent.proactive.policies import LocalTimeWindow
from astrbot_plugin_alice_agent.alice_agent.state.manager import StateManager
from astrbot_plugin_alice_agent.alice_agent.state.reducer import StateReducer
from astrbot_plugin_alice_agent.tests.test_autonomy import FakeClock, FakeDailyEvents, build_loop
from astrbot_plugin_alice_agent.tests.test_daily_events import EventLLM
from astrbot_plugin_alice_agent.tests.test_daily_proactive import Model
from astrbot_plugin_alice_agent.tests.test_decision_layer import MockDecisionModel


DAY = "2026-10-24"
NOW = datetime(2026, 10, 24, 12).timestamp()
EVENTS = [{"event_id": "one", "day": DAY, "text": "Alice整理了日常安排。",
           "topic_hook": "今天的安排", "importance": 0.35}]


class Audit:
    def __init__(self):
        self.rows = []

    async def record(self, stage, **details):
        self.rows.append({"stage": stage, **details})


def decision_model(failure=None):
    return MockDecisionModel(confidence=0.99, choices={"action": "send"},
        nouls={"natural_reason_exists": 0.99, "likely_to_interrupt": 0.01,
               "relevant_context_exists": 0.99, "requires_deep_reasoning": 0.01},
        scores={"appropriateness": 4}, failure=failure)


class ProactiveRetryTests(unittest.IsolatedAsyncioTestCase):
    async def make_loop(self, *, model=None, mode=DecisionMode.HYBRID, limit=3, clock=None, store=None, register=True):
        clock = clock or FakeClock(NOW)
        loop, original_store, planner, sender = build_loop(clock)
        store = store or original_store
        loop._store = loop._drives._store = loop._cooldown._store = store
        loop._scheduler = ContextScheduler(store)
        loop._config = replace(loop._config, daily_events_enabled=True, proactive_daily_attempt_limit=limit)
        loop._evaluator = ProactiveEvaluator("09:00", "22:00")
        loop._daily_events = FakeDailyEvents(copy.deepcopy(EVENTS))
        willingness = Model()
        loop._daily_policy = DailyProactivePolicy(store, llm=willingness, random_value=lambda: 0)
        model = model or decision_model(DecisionAPIError("offline"))
        service = DecisionService(model, mode=mode, cache_ttl_seconds=0)
        self.addAsyncCleanup(service.close)
        loop._decision_gate = ProactiveDecisionGate(service)
        loop._audit_sink = Audit()
        if register:
            await loop.on_user_event(AgentEvent("e", "trace", "USER_MESSAGE", "test", "s", "u", clock.now() - 3600, "hello"))
        return loop, store, planner, sender, model, willingness

    async def test_technical_failure_retries_same_event_without_redraw(self):
        loop, store, planner, sender, model, willingness = await self.make_loop()
        await loop.tick()
        first = await store.get_daily_proactive_state("s", DAY)
        self.assertEqual(first["attempt_count"], 1)
        self.assertEqual(first["attempted_event_ids"], [])
        self.assertEqual(first["event_attempts"]["one"]["next_retry_at"], NOW + 60)
        for _ in range(4):
            await loop.tick()
        self.assertEqual(model.calls, 1)
        model.failure = None
        loop._clock.advance(60)
        await loop.tick()
        after = await store.get_daily_proactive_state("s", DAY)
        self.assertEqual(after["attempt_count"], 2)
        self.assertEqual(after["delivered_event_ids"], ["one"])
        self.assertEqual((model.calls, willingness.calls, len(sender.messages)), (2, 1, 1))
        self.assertEqual(planner.calls, 0)
        await loop.tick()
        self.assertEqual(model.calls, 2)

    async def test_failure_retries_exhaust_quota_without_refunding_or_sending(self):
        loop, store, _, sender, model, _ = await self.make_loop(limit=2)
        await loop.tick()
        loop._clock.advance(60)
        await loop.tick()
        loop._clock.advance(1000)
        await loop.tick()
        self.assertEqual(model.calls, 2)
        self.assertEqual((await store.get_daily_proactive_state("s", DAY))["attempt_count"], 2)
        self.assertEqual(loop._audit_sink.rows[-1]["reason_code"], "attempt_limit")
        self.assertFalse(sender.messages)

    async def test_valid_model_refusal_is_final_and_not_retried(self):
        model = decision_model()
        model.choices["action"] = "do_nothing"
        loop, store, _, sender, _, _ = await self.make_loop(model=model)
        await loop.tick()
        loop._clock.advance(1000)
        await loop.tick()
        state = await store.get_daily_proactive_state("s", DAY)
        self.assertEqual(state["attempt_count"], 1)
        self.assertEqual(state["attempted_event_ids"], ["one"])
        self.assertEqual(state["event_attempts"]["one"]["status"], "finished")
        self.assertEqual(model.calls, 1)
        self.assertFalse(sender.messages)
        self.assertEqual(loop._audit_sink.rows[-1]["reason_code"], "daily_events_attempted")

    async def test_shadow_failure_keeps_existing_planner_path(self):
        loop, store, planner, sender, model, _ = await self.make_loop(mode=DecisionMode.SHADOW)
        await loop.tick()
        self.assertEqual((planner.calls, model.calls, len(sender.messages)), (1, 1, 1))
        self.assertEqual((await store.get_daily_proactive_state("s", DAY))["event_attempts"]["one"]["status"], "finished")
        self.assertFalse(any(r["stage"] == "retry_scheduled" for r in loop._audit_sink.rows))

    async def test_cancel_during_jev_releases_event_and_retains_quota(self):
        loop, store, *_ = await self.make_loop()
        entered = asyncio.Event()
        class Blocked:
            async def evaluate(self, *args):
                entered.set()
                await asyncio.Event().wait()
        loop._decision_gate = Blocked()
        task = asyncio.create_task(loop.tick())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        state = await store.get_daily_proactive_state("s", DAY)
        self.assertEqual(state["attempt_count"], 1)
        self.assertEqual(state["attempted_event_ids"], [])
        self.assertEqual(state["event_attempts"]["one"]["error_kind"], "CancelledError")

    async def test_sqlite_restart_lease_recovery_and_stale_owner_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "autonomy.db"
            first = SQLiteAutonomyStore(path)
            self.assertTrue(await first.claim_daily_proactive_attempt("s", DAY, "one", 3,
                now=NOW, claim_id="old", lease_until=NOW + 10))
            await first.close()
            store = SQLiteAutonomyStore(path)
            self.addAsyncCleanup(store.close)
            await store.recover_daily_proactive_attempts("s", DAY, NOW + 10)
            self.assertFalse(await store.claim_daily_proactive_attempt("s", DAY, "one", 3,
                now=NOW + 20, claim_id="early", lease_until=NOW + 50))
            self.assertTrue(await store.claim_daily_proactive_attempt("s", DAY, "one", 3,
                now=NOW + 70, claim_id="new", lease_until=NOW + 300))
            late = await store.finish_daily_proactive_attempt("s", DAY, "one", "old", now=NOW + 71, retryable=True)
            self.assertFalse(late["changed"])
            state = await store.get_daily_proactive_state("s", DAY)
            self.assertEqual(state["attempt_count"], 2)
            self.assertEqual(state["event_attempts"]["one"]["claim_id"], "new")
            await store.close()

    async def test_repeated_cancellation_finishes_local_cleanup_and_resets_context(self):
        loop, store, *_ = await self.make_loop()
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Blocked:
            async def evaluate(self, *args):
                entered.set()
                await asyncio.Event().wait()
        loop._decision_gate = Blocked()
        original = store.finish_daily_proactive_attempt
        async def slow_finish(*args, **kwargs):
            cleaning.set()
            await release.wait()
            return await original(*args, **kwargs)
        store.finish_daily_proactive_attempt = slow_finish
        async def run():
            try:
                await loop.tick()
            except asyncio.CancelledError:
                self.assertIsNone(loop._attempt_context.get())
                self.assertIsNone(loop._audit_context.get())
                raise
        task = asyncio.create_task(run())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await store.get_daily_proactive_state("s", DAY))["attempted_event_ids"], [])

    async def test_sqlite_concurrent_claim_is_charged_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "autonomy.db"
            stores = [SQLiteAutonomyStore(path), SQLiteAutonomyStore(path)]
            claimed = await asyncio.gather(*(store.claim_daily_proactive_attempt("s", DAY, "one", 3,
                now=NOW, claim_id=str(i), lease_until=NOW + 300) for i, store in enumerate(stores)))
            self.assertEqual(sum(claimed), 1)
            self.assertEqual((await stores[0].get_daily_proactive_state("s", DAY))["attempt_count"], 1)
            for store in stores:
                await store.close()

    async def test_old_worker_losing_lease_cannot_generate_or_send(self):
        loop, store, planner, sender, *_ = await self.make_loop()
        entered, release = asyncio.Event(), asyncio.Event()
        class Slow:
            async def evaluate(self, *args):
                entered.set()
                await release.wait()
                return ProactiveGateDecision("send", "approved")
        loop._decision_gate = Slow()
        task = asyncio.create_task(loop.tick())
        await asyncio.wait_for(entered.wait(), 2)
        loop._clock.advance(600)
        await store.recover_daily_proactive_attempts("s", DAY, loop._clock.now())
        loop._clock.advance(60)
        self.assertTrue(await store.claim_daily_proactive_attempt("s", DAY, "one", 3,
            now=loop._clock.now(), claim_id="new-owner", lease_until=loop._clock.now() + 300))
        release.set()
        await task
        self.assertFalse(sender.messages)
        self.assertEqual(planner.calls, 0)
        state = await store.get_daily_proactive_state("s", DAY)
        self.assertEqual(state["event_attempts"]["one"]["claim_id"], "new-owner")
        self.assertEqual(state["event_attempts"]["one"]["status"], "pending")
        self.assertEqual(loop._audit_sink.rows[-1]["reason_code"], "attempt_ownership_lost")

    async def test_sqlite_claim_rolls_back_if_lease_write_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "autonomy.db"
            store = SQLiteAutonomyStore(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TRIGGER fail_lease BEFORE INSERT ON daily_proactive_attempt_leases "
                           "BEGIN SELECT RAISE(ABORT,'test fault'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                await store.claim_daily_proactive_attempt("s", DAY, "one", 3, now=NOW, claim_id="x", lease_until=NOW + 100)
            self.assertEqual((await store.get_daily_proactive_state("s", DAY))["attempt_count"], 0)
            await store.close()

    async def test_delivered_event_cannot_be_released_for_retry(self):
        loop, store, *_ = await self.make_loop()
        await store.claim_daily_proactive_attempt("s", DAY, "one", 3, now=NOW, claim_id="x", lease_until=NOW + 300)
        await store.record_daily_proactive_delivery("s", DAY, "one")
        result = await store.finish_daily_proactive_attempt("s", DAY, "one", "x", now=NOW, retryable=True)
        self.assertEqual(result["status"], "finished")
        self.assertFalse(await store.claim_daily_proactive_attempt("s", DAY, "one", 3,
            now=NOW + 1000, claim_id="y", lease_until=NOW + 1300))

    async def test_upgrade_preserves_legacy_attempts_and_negative_draw(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE daily_proactive_state(session_id TEXT,day TEXT,attempt_count INTEGER,"
                           "attempted_event_ids TEXT,delivered_event_ids TEXT,PRIMARY KEY(session_id,day))")
                db.execute("INSERT INTO daily_proactive_state VALUES(?,?,?,?,?)", ("s", DAY, 1, '["one"]', '[]'))
                db.execute("CREATE TABLE daily_proactive_decisions(session_id TEXT,day TEXT,payload TEXT,PRIMARY KEY(session_id,day))")
                db.execute("INSERT INTO daily_proactive_decisions VALUES(?,?,?)", ("s", DAY, '{"eligible":false}'))
            store = SQLiteAutonomyStore(path)
            self.assertEqual(await store.recover_daily_proactive_attempts("s", DAY, NOW), [])
            state = await store.get_daily_proactive_state("s", DAY)
            self.assertEqual((state["attempt_count"], state["attempted_event_ids"]), (1, ["one"]))
            self.assertEqual(await store.get_daily_proactive_decision("s", DAY), {"eligible": False})
            await store.close()

    async def test_scheduled_retry_survives_restart_and_stops_after_three_attempts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "autonomy.db"
            first = SQLiteAutonomyStore(path)
            loop, _, _, _, model, willingness = await self.make_loop(store=first)
            await loop.schedule(ScheduledContextEvent("follow", "u", "s", "exam", "考试", "summary", NOW, NOW, NOW + 2000))
            await loop.tick()
            self.assertEqual(model.calls, 1)
            await first.close()
            second = SQLiteAutonomyStore(path)
            loop, _, _, sender, _, _ = await self.make_loop(store=second, model=model, register=False)
            await loop.tick()
            self.assertEqual(model.calls, 1)
            for delay in (60, 120):
                loop._clock.advance(delay)
                await loop.tick()
            await loop.tick()
            self.assertEqual(model.calls, 3)
            self.assertFalse(await loop._scheduler.due(loop._clock.now()))
            self.assertFalse(sender.messages)
            self.assertEqual(willingness.calls, 0)
            self.assertEqual((await second.get_daily_proactive_state("s", DAY))["attempt_count"], 0)
            await second.close()

    async def test_scheduled_retry_success_is_not_sent_twice(self):
        loop, _, _, sender, model, _ = await self.make_loop()
        await loop.schedule(ScheduledContextEvent("follow", "u", "s", "exam", "考试", "summary", NOW, NOW, NOW + 2000))
        await loop.tick()
        model.failure = None
        loop._clock.advance(60)
        await loop.tick()
        await loop.tick()
        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(model.calls, 2)

    async def test_night_tick_does_not_generate_draw_or_claim(self):
        clock = FakeClock(datetime(2026, 10, 24, 8, 30).timestamp())
        loop, store, _, sender, model, willingness = await self.make_loop(clock=clock)
        await loop.tick()
        self.assertEqual(loop._daily_events.calls, 0)
        self.assertEqual((model.calls, willingness.calls), (0, 0))
        self.assertEqual((await store.get_daily_proactive_state("s", DAY))["attempt_count"], 0)
        self.assertFalse(sender.messages)

    async def test_event_generation_crossing_window_does_not_draw_or_claim(self):
        clock = FakeClock(datetime(2026, 10, 24, 21, 59, 59).timestamp())
        loop, store, _, _, model, willingness = await self.make_loop(clock=clock)
        original = loop._daily_events.ensure
        async def slow(*args, **kwargs):
            clock.advance(2)
            return await original(*args, **kwargs)
        loop._daily_events.ensure = slow
        await loop.tick()
        self.assertEqual((model.calls, willingness.calls), (0, 0))
        self.assertEqual((await store.get_daily_proactive_state("s", DAY))["attempt_count"], 0)

    async def test_willingness_crossing_window_does_not_draw_or_claim(self):
        clock = FakeClock(datetime(2026, 10, 24, 21, 59, 59).timestamp())
        loop, store, _, _, model, willingness = await self.make_loop(clock=clock)
        original = willingness.generate
        async def slow(request):
            clock.advance(2)
            return await original(request)
        willingness.generate = slow
        await loop.tick()
        self.assertEqual(willingness.calls, 1)
        self.assertEqual(model.calls, 0)
        self.assertIsNone(await store.get_daily_proactive_decision("s", DAY))
        self.assertEqual((await store.get_daily_proactive_state("s", DAY))["attempt_count"], 0)

    def daily_service(self, clock, *, llm=None, decisions=None, window=("09:00", "22:00")):
        storage = InMemoryStorageAdapter()
        states = StateManager(storage, StateReducer())
        llm = llm or EventLLM()
        decisions = decisions or DecisionService(None)
        self.addAsyncCleanup(decisions.close)
        service = DailyEventService(storage, states, llm, ContextBuilder(), decisions,
            enabled=True, generation_window=LocalTimeWindow(*window), clock=clock.now)
        return service, storage, states, llm

    async def test_daily_service_defers_outside_window_and_reuses_cache_afterward(self):
        clock = FakeClock(datetime(2026, 10, 24, 8).timestamp())
        service, storage, states, llm = self.daily_service(clock)
        diagnostic = {}
        with patch.object(service, "_count", return_value=1):
            self.assertEqual(await service.ensure("s", "u", clock.now(), diagnostics=diagnostic), [])
            self.assertEqual(diagnostic["source"], "deferred")
            self.assertEqual(llm.calls, 0)
            self.assertIsNone(await storage.get(service._NAMESPACE, "s\0" + DAY))
            clock.advance(3600)
            events = await service.ensure("s", "u", clock.now(), diagnostics=diagnostic)
            self.assertEqual(diagnostic["source"], "generated")
            self.assertEqual(llm.calls, 1)
            clock.value = datetime(2026, 10, 24, 23).timestamp()
            self.assertEqual(await service.ensure("s", "u", clock.now(), diagnostics=diagnostic), events)
            self.assertTrue(diagnostic["cache_hit"])
            self.assertEqual(diagnostic["generation_model_attempts"], 0)
            self.assertEqual(llm.calls, 1)

    async def test_each_daily_model_call_rechecks_window_after_slow_expansion(self):
        clock = FakeClock(datetime(2026, 10, 24, 21, 59, 59).timestamp())
        class Slow(EventLLM):
            async def generate(self, request):
                clock.advance(2)
                return await super().generate(request)
        model = decision_model()
        service, _, _, llm = self.daily_service(clock, llm=Slow(),
            decisions=DecisionService(model, mode=DecisionMode.HYBRID))
        with patch.object(service, "_count", return_value=1):
            diagnostic = {}
            await service.ensure("s", "u", clock.now(), diagnostics=diagnostic)
        self.assertEqual(llm.calls, 1)
        self.assertEqual(model.calls, 0)
        self.assertEqual(diagnostic["generation_model_attempts"], 1)

    async def test_midnight_generation_does_not_commit_expired_day(self):
        clock = FakeClock(datetime(2026, 10, 24, 23, 59, 59).timestamp())
        class Slow(EventLLM):
            async def generate(self, request):
                clock.advance(2)
                return await super().generate(request)
        service, storage, states, _ = self.daily_service(clock, llm=Slow(), window=("22:00", "06:00"))
        with patch.object(service, "_count", return_value=1):
            diagnostic = {}
            self.assertEqual(await service.ensure("s", "u", clock.now(), diagnostics=diagnostic), [])
        self.assertEqual(diagnostic["source"], "expired")
        self.assertIsNone(await storage.get(service._NAMESPACE, "s\0" + DAY))
        self.assertNotEqual((await states.get("s")).daily_event_day, DAY)

    async def test_jev_http_failures_are_specific_and_logs_exclude_secrets(self):
        cases = [(status, reason, "private upstream body") for status, reason in
                 ((401, "authentication"), (429, "rate_limit"), (503, "upstream_error"), (200, "invalid_response"))]
        cases.extend((200, "invalid_response", json.dumps(body)) for body in (
            {"answers": {"action": {"type": "score", "score": "private upstream body"}}},
            {"answers": {"action": {"type": []}}},
            {"answers": {}, "usage": {"input_tokens": "private upstream body"}},
        ))
        for status, reason, body in cases:
            with self.subTest(status=status, body=body), tempfile.TemporaryDirectory() as folder:
                client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, text=body)))
                adapter = TypesafeJevAdapter(endpoint="https://jev.invalid", api_key="private-api-key", client=client)
                loop, _, _, _, _, _ = await self.make_loop()
                service = DecisionService(adapter, mode=DecisionMode.HYBRID)
                self.addAsyncCleanup(service.close)
                loop._decision_gate = ProactiveDecisionGate(service)
                audit = DailyProactiveLogger(Path(folder) / "today.log", NullLogger(), clock=loop._clock.now)
                loop._audit_sink = audit
                await loop.tick()
                rows = [json.loads(line) for line in audit.path.read_text(encoding="utf-8").splitlines()]
                result = next(row for row in rows if row.get("stage") == "jev")
                self.assertEqual(result["failure_reason"], reason)
                self.assertEqual(result["http_status"], status if status != 200 else None)
                self.assertTrue(result["retryable"])
                self.assertIn("trace_id", result)
                text = audit.path.read_text(encoding="utf-8")
                self.assertNotIn("private upstream body", text)
                self.assertNotIn("private-api-key", text)
                await service.close()
                await client.aclose()

    async def test_readonly_status_reports_wait_without_mutation_or_model_calls(self):
        loop, store, _, _, model, willingness = await self.make_loop(model=decision_model(DecisionTimeoutError()))
        await loop.tick()
        await loop._states.apply_daily("s", DAY, copy.deepcopy(EVENTS))
        before = copy.deepcopy(store.daily_proactive)
        for _ in range(3):
            status = await loop.inspect_session("s", "u")
            self.assertIn("retry_wait", status["blockers"])
        self.assertEqual(store.daily_proactive, before)
        self.assertEqual((model.calls, willingness.calls), (1, 1))

    def test_retry_configuration_rejects_nonfinite_and_boolean_values(self):
        config = AgentConfig(proactive_retry_base_seconds=True, proactive_retry_max_seconds=float("nan"))
        self.assertEqual((config.proactive_retry_base_seconds, config.proactive_retry_max_seconds), (60, 900))
        config = AgentConfig(proactive_retry_base_seconds=10, proactive_retry_max_seconds=-1)
        self.assertEqual(config.proactive_retry_max_seconds, 10)
