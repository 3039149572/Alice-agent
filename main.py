from __future__ import annotations

import asyncio
from contextlib import aclosing
import json
import os
from collections import OrderedDict, defaultdict
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .adapters.astrbot import (
    AlicePrivateMessageFilter,
    AstrBotLLMAdapter,
    AstrBotMediaRenderer,
    AstrBotMessageAdapter,
    AstrBotProactiveSender,
)
from .adapters.astrbot.reactive import ReactiveMessageMixin
from .adapters.astrbot.reply_delivery import ReplyDeliveryMixin
from .adapters.astrbot.send_coordinator import SessionSendCoordinator
from .adapters.astrbot.config_values import _finite_float, _positive_int
from .adapters.memory import build_memory_port
from .adapters.jev import DEFAULT_SYSTEMONE_ENDPOINT, TypesafeJevAdapter
from .adapters.storage import InMemoryStorageAdapter, SQLiteStorageAdapter
from .adapters.vad import HTTPVADModelAdapter
from .adapters.diagnostics import AtomicJSONTurnLogger, DailyProactiveLogger
from .alice_agent.config import AgentConfig
from .alice_agent.core.agent import AliceAgent
from .alice_agent.core.pipeline import AgentPipeline
from .alice_agent.core.session_manager import SessionManager
from .alice_agent.generation.context_builder import ContextBuilder
from .alice_agent.knowledge import NovelContextConfig
from .alice_agent.generation.response_generator import ResponseGenerator
from .alice_agent.llm.service import LLMService
from .alice_agent.memory.manager import MemoryManager
from .alice_agent.memory.importance import MemoryImportanceEvaluator
from .alice_agent.memory.metadata_store import (
    InMemoryMetadataStore,
    SQLiteMemoryMetadataStore,
)
from .alice_agent.memory.reranker import MemoryRerankConfig, MemoryReranker
from .alice_agent.memory.retriever import MemoryRetriever
from .alice_agent.memory.writer import MemoryWriter
from .alice_agent.media import DecisionAwareMediaSelector, MediaCatalog, MediaConfig
from .alice_agent.perception.parser import PerceptionParser
from .alice_agent.perception.service import PerceptionService
from .alice_agent.planner.parser import ActionPlanParser
from .alice_agent.planner.service import PlannerService
from .alice_agent.state.manager import StateManager
from .alice_agent.state.reducer import StateReducer
from .alice_agent.state.time_reducer import TimeStateReducer
from .alice_agent.daily_events.service import DailyEventService
from .alice_agent.relationship.manager import RelationshipManager
from .alice_agent.relationship.reducer import RelationshipReducer
from .alice_agent.relationship.store import (
    InMemoryRelationshipStore,
    SQLiteRelationshipStore,
)
from .alice_agent.drives.manager import DriveManager
from .alice_agent.drives.reducer import DriveReducer, DriveReducerConfig
from .alice_agent.events.bus import EventBus
from .alice_agent.loop.agent_loop import AgentLoop
from .alice_agent.proactive.daily_policy import DailyProactivePolicy
from .alice_agent.loop.lifecycle import SystemClock
from .alice_agent.loop.scheduler import ContextScheduler
from .alice_agent.loop.store import InMemoryAutonomyStore, SQLiteAutonomyStore
from .alice_agent.proactive.cooldown import CooldownConfig, CooldownManager
from .alice_agent.proactive.evaluator import ProactiveEvaluator
from .alice_agent.proactive.policies import LocalTimeWindow
from .alice_agent.proactive.planner import ProactivePlanner
from .alice_agent.proactive.scorer import ActionScoreConfig, ActionScorer
from .alice_agent.proactive.decision_gate import ProactiveDecisionGate
from .alice_agent.decision.models import DecisionMode
from .alice_agent.decision.policies import DecisionEscalationPolicy
from .alice_agent.decision.service import DecisionService
from .alice_agent.decision.response_timing import ResponseTimingService
from .adapters.astrbot.pending_messages import (
    PendingReplyCoordinator,
)
from .alice_agent.memory.decision_gate import MemoryDecisionGate
from .alice_agent.perception.decision_service import DecisionAwarePerceptionService
from .alice_agent.planner.decision_service import DecisionAwarePlannerService


def _parse_persona_profile(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _build_config(config: dict) -> AgentConfig:
    return AgentConfig(
        enabled=bool(config.get("enabled", True)),
        debug=bool(config.get("debug", False)),
        llm_timeout=float(config.get("llm_timeout", 30.0)),
        turn_timeout=_finite_float(config.get("turn_timeout"), 60.0),
        llm_provider_attempt_timeout=_finite_float(config.get("llm_provider_attempt_timeout"), 15.0),
        llm_fallback_reserve_seconds=_finite_float(config.get("llm_fallback_reserve_seconds"), 5.0),
        perception_enabled=bool(config.get("perception_enabled", True)),
        planner_enabled=bool(config.get("planner_enabled", True)),
        memory_enabled=bool(config.get("memory_enabled", True)),
        memory_metadata_enabled=bool(config.get("memory_metadata_enabled", True)),
        memory_fact_extraction_enabled=bool(config.get("memory_fact_extraction_enabled", True)),
        memory_retention_days=config.get("memory_retention_days", 0.0),
        memory_journal_retention_days=config.get("memory_journal_retention_days", 0.0),
        memory_inactive_fact_retention_days=config.get("memory_inactive_fact_retention_days", 0.0),
        memory_backend=str(config.get("memory_backend", "local_sqlite")),
        history_limit=int(config.get("history_limit", 8)),
        session_persistence_enabled=bool(
            config.get("session_persistence_enabled", True)
        ),
        history_restore_max_age_hours=_finite_float(
            config.get("history_restore_max_age_hours"), 72.0
        ),
        novel_context_enabled=bool(config.get("novel_context_enabled", True)),
        novel_context_dir=str(config.get("novel_context_dir") or ""),
        novel_context_stage=str(config.get("novel_context_stage", "integrity_knight")),
        novel_fact_limit=config.get("novel_fact_limit", 3),
        novel_dialogue_limit=config.get("novel_dialogue_limit", 3),
        novel_context_max_chars=config.get("novel_context_max_chars", 6000),
        memory_recall_limit=int(config.get("memory_recall_limit", 5)),
        memory_selected_limit=int(config.get("memory_selected_limit", 3)),
        memory_importance_threshold=float(config.get("memory_importance_threshold", 0.6)),
        memory_min_relevance=float(config.get("memory_min_relevance", 0.12)),
        memory_semantic_weight=float(config.get("memory_semantic_weight", 0.65)),
        memory_importance_weight=float(config.get("memory_importance_weight", 0.15)),
        memory_emotion_weight=float(config.get("memory_emotion_weight", 0.07)),
        memory_relationship_weight=float(config.get("memory_relationship_weight", 0.08)),
        memory_recency_weight=float(config.get("memory_recency_weight", 0.05)),
        relationship_enabled=bool(config.get("relationship_enabled", True)),
        relationship_max_delta_per_turn=float(
            config.get("relationship_max_delta_per_turn", 0.02)
        ),
        agent_loop_enabled=bool(config.get("agent_loop_enabled", True)),
        agent_tick_interval=float(config.get("agent_tick_interval", 30.0)),
        daily_events_enabled=bool(config.get("daily_events_enabled", False)),
        daily_event_max=int(config.get("daily_event_max", 2)),
        daily_event_llm_enabled=bool(config.get("daily_event_llm_enabled", True)),
        daily_event_jev_enabled=bool(config.get("daily_event_jev_enabled", True)),
        response_timing_enabled=bool(config.get("response_timing_enabled", True)),
        response_wait_quiet_seconds=_finite_float(config.get("response_wait_quiet_seconds"), 0.8),
        response_wait_max_seconds=_finite_float(config.get("response_wait_max_seconds"), 3.0),
        persona_profile=_parse_persona_profile(config.get("persona_profile")),
        drive_social_enabled=bool(config.get("drive_social_enabled", True)),
        drive_curiosity_enabled=bool(config.get("drive_curiosity_enabled", True)),
        drive_rest_enabled=bool(config.get("drive_rest_enabled", True)),
        drive_social_growth_per_hour=float(config.get("drive_social_growth_per_hour", 0.025)),
        drive_curiosity_growth_per_hour=float(config.get("drive_curiosity_growth_per_hour", 0.035)),
        drive_rest_growth_per_hour=float(config.get("drive_rest_growth_per_hour", 0.04)),
        proactive_enabled=bool(config.get("proactive_enabled", True)),
        proactive_score_threshold=float(config.get("proactive_score_threshold", 0.72)),
        proactive_daily_score_threshold=float(config.get("proactive_daily_score_threshold", 0.35)),
        proactive_daily_probability=_finite_float(config.get("proactive_daily_probability"), 0.55),
        proactive_daily_probability_llm_enabled=bool(config.get("proactive_daily_probability_llm_enabled", True)),
        proactive_defer_threshold=float(config.get("proactive_defer_threshold", 0.60)),
        proactive_min_interval=float(config.get("proactive_min_interval", 21600.0)),
        proactive_same_topic_interval=float(config.get("proactive_same_topic_interval", 86400.0)),
        proactive_daily_budget=int(config.get("proactive_daily_budget", 1)),
        proactive_window_start=str(config.get("proactive_window_start", "09:00")),
        proactive_window_end=str(config.get("proactive_window_end", "21:00")),
        proactive_daily_attempt_limit=int(config.get("proactive_daily_attempt_limit", 2)),
        proactive_retry_base_seconds=config.get("proactive_retry_base_seconds", 60.0),
        proactive_retry_max_seconds=config.get("proactive_retry_max_seconds", 900.0),
        active_conversation_window=float(config.get("active_conversation_window", 900.0)),
        planner_timeout=float(config.get("planner_timeout", 20.0)),
        proactive_social_weight=float(config.get("proactive_social_weight", 0.18)),
        proactive_curiosity_weight=float(config.get("proactive_curiosity_weight", 0.24)),
        proactive_relationship_weight=float(config.get("proactive_relationship_weight", 0.15)),
        proactive_relevance_weight=float(config.get("proactive_relevance_weight", 0.55)),
        proactive_urgency_weight=float(config.get("proactive_urgency_weight", 0.20)),
        proactive_interruption_weight=float(config.get("proactive_interruption_weight", 0.15)),
        proactive_rest_weight=float(config.get("proactive_rest_weight", 0.08)),
        jev_enabled=bool(config.get("jev_enabled", False)),
        jev_mode=str(config.get("jev_mode", "shadow")),
        jev_endpoint=str(config.get("jev_endpoint", DEFAULT_SYSTEMONE_ENDPOINT)),
        jev_api_key=str(
            config.get("jev_api_key", "")
            or os.environ.get("TYPESAFE_API_KEY", "")
            or os.environ.get("JEV_API_KEY", "")
        ),
        jev_model=str(config.get("jev_model", "jev-latest")),
        jev_timeout_ms=int(config.get("jev_timeout_ms", 2500)),
        jev_auto_threshold=float(config.get("jev_auto_threshold", 0.85)),
        jev_review_threshold=float(config.get("jev_review_threshold", 0.80)),
        jev_task_policies=_parse_persona_profile(config.get("jev_task_policies", {})),
        jev_affect_intervention_enabled=bool(config.get("jev_affect_intervention_enabled", False)),
        jev_perception_enabled=bool(config.get("jev_perception_enabled", True)),
        jev_memory_enabled=bool(config.get("jev_memory_enabled", True)),
        jev_planner_enabled=bool(config.get("jev_planner_enabled", True)),
        jev_proactive_enabled=bool(config.get("jev_proactive_enabled", True)),
        jev_memory_relevance_blend=float(config.get("jev_memory_relevance_blend", 0.25)),
        fallback_text=str(
            config.get("fallback_text", "抱歉，我刚刚没能组织好回应。可以再说一遍吗？")
        ),
    )


def _build_media_config(config: dict) -> MediaConfig:
    plugin_root = Path(__file__).resolve().parent
    legacy_root = plugin_root.parent / "deeplx_genie_tts"
    legacy_config_path = plugin_root.parent.parent / "config" / "deeplx_genie_tts_config.json"
    legacy_config: dict = {}
    try:
        value = json.loads(legacy_config_path.read_text(encoding="utf-8-sig"))
        if isinstance(value, dict):
            legacy_config = value
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass

    def configured_value(key: str, legacy_key: str, default):
        value = config.get(key)
        if value not in (None, ""):
            return value
        legacy = legacy_config.get(legacy_key)
        return legacy if legacy not in (None, "") else default

    def configured_path(key: str, legacy_key: str, fallback: Path) -> str:
        value = str(config.get(key, "") or "").strip()
        legacy = str(legacy_config.get(legacy_key, "") or "").strip()
        return value or legacy or (str(fallback) if fallback.exists() else "")

    genie_host = str(configured_value("media_genie_host", "genie_host", "localhost"))
    if genie_host in {"0.0.0.0", "::"}:
        genie_host = "127.0.0.1"

    return MediaConfig(
        enabled=bool(config.get("media_enabled", False)),
        voice_enabled=bool(config.get("media_voice_enabled", True)),
        emoji_enabled=bool(config.get("media_emoji_enabled", True)),
        jev_enabled=bool(config.get("jev_media_enabled", True)),
        min_chars=int(config.get("media_min_chars", 10)),
        cooldown_seconds=float(config.get("media_cooldown", 0.0)),
        voice_probability=float(config.get("media_voice_probability", 1.0)),
        emoji_probability=float(config.get("media_emoji_probability", 1.0)),
        candidate_limit=int(config.get("media_candidate_limit", 5)),
        audio_score_threshold=float(config.get("media_audio_score_threshold", 0.30)),
        emoji_score_threshold=float(config.get("media_emoji_score_threshold", 0.30)),
        audio_index_path=configured_path("media_audio_index_path", "audio_index_path", legacy_root / "audio_index.json"),
        emoji_index_path=configured_path("media_emoji_index_path", "emoji_index_path", legacy_root / "emoji_index.json"),
        audio_dir=configured_path("media_audio_dir", "audio_ref_dir", legacy_root / "audio"),
        emoji_dir=configured_path("media_emoji_dir", "emoji_dir", legacy_root / "images"),
        genie_host=genie_host,
        genie_port=int(configured_value("media_genie_port", "genie_port", 8000)),
        character_name=str(configured_value("media_character_name", "character_name", "")),
        onnx_model_dir=str(configured_value("media_onnx_model_dir", "onnx_model_dir", "")),
        language=str(configured_value("media_tts_language", "language", "japanese")),
        deepl_api_key=str(config.get("media_deepl_api_key") or os.environ.get("DEEPL_API_KEY", "")),
        default_ref_audio=str(configured_value("media_default_ref_audio", "default_ref_audio", "")),
        default_ref_text=str(configured_value("media_default_ref_text", "default_ref_text", "")),
        send_text_with_audio=bool(configured_value("media_send_text_with_audio", "send_text_with_audio", True)),
        tts_max_chars=int(configured_value("media_tts_max_chars", "tts_max_chars", 80)),
        emoji_delay_seconds=float(config.get("media_emoji_delay", 0.5)),
        log_tts_inputs=bool(config.get("media_log_tts_inputs", False)),
        text_first=bool(config.get("media_text_first", True)),
        render_timeout_seconds=_finite_float(config.get("media_render_timeout_seconds"), 12.0),
    )


class AliceAgentPlugin(ReactiveMessageMixin, ReplyDeliveryMixin, Star):
    """Thin AstrBot entry point for the framework-independent Alice core."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        raw_config = config if config is not None else {}
        super().__init__(context, raw_config)
        self._raw_config = raw_config
        try:
            self._plugin_version = next(line.split(":", 1)[1].strip() for line in
                Path(__file__).with_name("metadata.yaml").read_text(encoding="utf-8").splitlines()
                if line.startswith("version:"))
        except (OSError, StopIteration):
            self._plugin_version = "unknown"
        self._test_logging_active = True
        self._config = _build_config(raw_config)
        self._media_config = _build_media_config(raw_config)
        self._vad_enabled = bool(raw_config.get("vad_enabled", True))
        self._vad_model = (
            HTTPVADModelAdapter(
                str(raw_config.get("vad_endpoint", "http://127.0.0.1:8001")),
                timeout_seconds=max(
                    0.1, float(raw_config.get("vad_timeout_ms", 5000)) / 1000.0
                ),
            )
            if self._vad_enabled else None
        )
        llm_service = LLMService(AstrBotLLMAdapter(
            context, attempt_timeout=self._config.llm_provider_attempt_timeout,
            fallback_reserve=self._config.llm_fallback_reserve_seconds,
        ), self._config.llm_timeout)
        context_builder = ContextBuilder(
            profile=self._config.persona_profile,
            novel_config=NovelContextConfig(
                enabled=self._config.novel_context_enabled,
                directory=self._config.novel_context_dir,
                stage=self._config.novel_context_stage,
                fact_limit=self._config.novel_fact_limit,
                dialogue_limit=self._config.novel_dialogue_limit,
                max_chars=self._config.novel_context_max_chars,
            ),
            logger=self.logger,
        )
        decision_mode = (
            DecisionMode(self._config.jev_mode)
            if self._config.jev_enabled else DecisionMode.OFF
        )
        decision_port = None
        any_decision_enabled = self._config.jev_enabled and (
            decision_mode != DecisionMode.OFF
            or any(policy.get("mode", "off") != "off" for policy in self._config.jev_task_policies.values())
        )
        if any_decision_enabled and self._config.jev_api_key:
            decision_port = TypesafeJevAdapter(
                endpoint=self._config.jev_endpoint,
                api_key=self._config.jev_api_key,
                model=self._config.jev_model,
                timeout_ms=self._config.jev_timeout_ms,
            )
        elif any_decision_enabled:
            self.logger.warning(
                "Jev is enabled but no API key is configured; existing decision paths remain active"
            )
        data_dir = StarTools.get_data_dir("astrbot_plugin_alice_agent")
        metrics_storage = None
        if bool(raw_config.get("decision_metrics_persistence_enabled", True)):
            try:
                metrics_storage = SQLiteStorageAdapter(data_dir / "alice_metrics.db")
            except Exception as exc:
                self.logger.warning(
                    f"Alice decision metrics SQLite unavailable ({type(exc).__name__})"
                )
        decision_service = DecisionService(
            decision_port,
            mode=decision_mode,
            model=self._config.jev_model,
            policy=DecisionEscalationPolicy(
                auto_threshold=self._config.jev_auto_threshold,
                review_threshold=self._config.jev_review_threshold,
            ),
            logger=self.logger,
            metrics_storage=metrics_storage,
            task_policies=self._config.jev_task_policies if self._config.jev_enabled else {},
        )
        self._test_logger = AtomicJSONTurnLogger(data_dir / "test_turns")
        self._proactive_audit = DailyProactiveLogger(data_dir / "proactive_today.log", self.logger)
        self._send_coordinator = SessionSendCoordinator()
        self._pending_replies = PendingReplyCoordinator()
        self._timing_service = ResponseTimingService(
            decision_service.for_task("response_timing"), enabled=self._config.response_timing_enabled
        )
        self._media_renderer = AstrBotMediaRenderer(
            self._media_config,
            llm_service,
            self.logger,
            data_dir / "temp_audio",
        )
        media_selector = None
        if self._media_config.enabled:
            media_catalog = MediaCatalog.from_files(
                audio_index_path=self._media_config.audio_index_path,
                emoji_index_path=self._media_config.emoji_index_path,
                audio_dir=self._media_config.audio_dir,
                emoji_dir=self._media_config.emoji_dir,
            )
            media_selector = DecisionAwareMediaSelector(
                media_catalog,
                decision_service.for_task("media"),
                self._media_config,
                default_reference_available=bool(
                    self._media_config.default_ref_audio
                    and self._media_config.default_ref_text
                ),
                voice_runtime_available=self._media_renderer.voice_runtime_available,
            )
            self.logger.info(
                f"Alice media catalog loaded audio={len(media_catalog.audio)} "
                f"emoji={len(media_catalog.emoji)}"
            )
        memory_port = build_memory_port(
            self._config,
            raw_config,
            self.logger,
            data_dir=data_dir,
        )
        intelligence_path = data_dir / "alice_intelligence.db"
        if self._config.memory_backend.strip().lower() == "in_memory":
            metadata_store = InMemoryMetadataStore()
            relationship_store = InMemoryRelationshipStore()
        else:
            try:
                metadata_store = SQLiteMemoryMetadataStore(intelligence_path)
                relationship_store = SQLiteRelationshipStore(intelligence_path)
            except Exception:
                self.logger.exception(
                    "Alice intelligence SQLite could not start; using volatile stores"
                )
                metadata_store = InMemoryMetadataStore()
                relationship_store = InMemoryRelationshipStore()
        importance_evaluator = MemoryImportanceEvaluator()
        memory = MemoryManager(
            memory_port,
            MemoryRetriever(memory_port, self._config.memory_recall_limit),
            MemoryWriter(
                memory_port,
                self._config.memory_importance_threshold,
                importance_evaluator,
                fact_extraction_enabled=self._config.memory_fact_extraction_enabled,
            ),
            MemoryReranker(MemoryRerankConfig(
                min_relevance=self._config.memory_min_relevance,
                semantic_weight=self._config.memory_semantic_weight,
                importance_weight=self._config.memory_importance_weight,
                emotion_weight=self._config.memory_emotion_weight,
                relationship_weight=self._config.memory_relationship_weight,
                recency_weight=self._config.memory_recency_weight,
                jev_relevance_blend=self._config.jev_memory_relevance_blend,
            )),
            self._config.memory_recall_limit,
            enabled=self._config.memory_enabled,
            metadata_store=metadata_store,
            metadata_enabled=self._config.memory_metadata_enabled,
            selected_limit=self._config.memory_selected_limit,
            importance_evaluator=importance_evaluator,
            logger=self.logger,
            debug=self._config.debug,
            decision_gate=MemoryDecisionGate(
                decision_service.for_task("memory"),
                enabled=self._config.jev_memory_enabled,
                min_relevance=self._config.memory_min_relevance,
            ),
        )
        relationship = RelationshipManager(
            relationship_store,
            RelationshipReducer(self._config.relationship_max_delta_per_turn),
            enabled=self._config.relationship_enabled,
        )
        state_storage = InMemoryStorageAdapter()
        if (
            self._config.daily_events_enabled
            or self._config.session_persistence_enabled
        ):
            try:
                state_storage = SQLiteStorageAdapter(data_dir / "alice_mood.db")
            except Exception:
                self.logger.exception(
                    "Alice state SQLite could not start; state and daily events are volatile"
                )
        state_manager = StateManager(state_storage, StateReducer(), TimeStateReducer())
        daily_events = DailyEventService(
            state_storage, state_manager, llm_service, context_builder,
            decision_service.for_task("daily_event"),
            enabled=self._config.daily_events_enabled,
            max_events=self._config.daily_event_max,
            llm_enabled=self._config.daily_event_llm_enabled,
            jev_enabled=self._config.daily_event_jev_enabled,
            logger=self.logger,
            generation_window=LocalTimeWindow(self._config.proactive_window_start, self._config.proactive_window_end),
            clock=SystemClock().now,
            audit=self._proactive_audit,
        )
        history_storage = None
        if (
            self._config.session_persistence_enabled
            and self._config.history_limit > 0
        ):
            try:
                history_storage = SQLiteStorageAdapter(
                    data_dir / "alice_sessions.db"
                )
            except Exception:
                self.logger.exception(
                    "Alice session history SQLite could not start; history is volatile"
                )
        session_manager = SessionManager(
            self._config.history_limit,
            storage=history_storage,
            logger=self.logger,
            max_age_seconds=self._config.history_restore_max_age_hours * 3600.0,
        )
        self._session_manager = session_manager
        from .alice_agent.memory.data_service import MemoryDataService

        async def invalidate_context(job):
            user_id = job["user_id"]
            sessions = set(job["session_ids"])
            if job.get("scope_session") is None:
                sessions.update(await autonomy_store.sessions_for_user(user_id))
            for session_id in sessions:
                await session_manager.forget(session_id)
                await state_manager.clear_context(session_id)
                await state_storage.delete_prefix("alice_daily_events_v1", session_id + "\0")
            await autonomy_store.invalidate_data_context(user_id, sorted(sessions))
            await asyncio.to_thread(self._test_logger.delete_user, user_id, job.get("scope_session"))
            await asyncio.to_thread(self._proactive_audit.delete_user,
                                    "" if job.get("scope_session") else user_id, sessions)

        async def maintain_logs(now):
            await session_manager.maintenance()
            days = min(3650, max(0, int(_finite_float(raw_config.get("test_log_retention_days", 0), 0.0))))
            if days:
                await asyncio.to_thread(self._test_logger.cleanup_expired, days, now=now)

        self._data_service = MemoryDataService(
            memory_port, metadata_store, invalidate_context=invalidate_context, logger=self.logger,
            journal_days=self._config.memory_journal_retention_days,
            memory_days=self._config.memory_retention_days,
            inactive_fact_days=self._config.memory_inactive_fact_retention_days,
            maintenance_extra=maintain_logs,
        )
        response_generator = ResponseGenerator(llm_service, context_builder)
        existing_perception = PerceptionService(
            llm_service,
            context_builder,
            PerceptionParser(),
            enabled=self._config.perception_enabled,
        )
        existing_planner = PlannerService(
            llm_service,
            context_builder,
            ActionPlanParser(),
            enabled=self._config.planner_enabled,
        )
        pipeline = AgentPipeline(
            delivery_journal=self._build_delivery_journal(data_dir),
            data_service=self._data_service,
            config=self._config,
            perception=DecisionAwarePerceptionService(
                existing_perception,
                decision_service.for_task("perception"),
                enabled=self._config.jev_perception_enabled,
                affect_intervention_enabled=self._config.jev_affect_intervention_enabled,
                vad_model=self._vad_model,
                vad_enabled=self._vad_enabled,
                jev_weight=float(raw_config.get("jev_vad_weight", 0.5)),
            ),
            state_manager=state_manager,
            memory=memory,
            planner=DecisionAwarePlannerService(
                existing_planner,
                decision_service.for_task("planner"),
                enabled=self._config.jev_planner_enabled,
            ),
            response_generator=response_generator,
            session_manager=session_manager,
            logger=self.logger,
            relationship_manager=relationship,
            media_selector=media_selector,
            daily_events=daily_events,
        )
        self._agent = AliceAgent(pipeline, llm_service, decision_service)
        self._data_service.delivery_recovery = pipeline.recover_deliveries
        self._decision_service = decision_service
        self._message_adapter = AstrBotMessageAdapter()
        if self._config.memory_backend.strip().lower() == "in_memory":
            autonomy_store = InMemoryAutonomyStore()
        else:
            try:
                autonomy_store = SQLiteAutonomyStore(data_dir / "alice_autonomy.db")
            except Exception:
                self.logger.exception(
                    "Alice autonomy SQLite could not start; using volatile autonomy state"
                )
                autonomy_store = InMemoryAutonomyStore()
        drive_manager = DriveManager(
            autonomy_store,
            DriveReducer(DriveReducerConfig(
                social_enabled=self._config.drive_social_enabled,
                curiosity_enabled=self._config.drive_curiosity_enabled,
                rest_enabled=self._config.drive_rest_enabled,
                social_growth_per_hour=self._config.drive_social_growth_per_hour,
                curiosity_growth_per_hour=self._config.drive_curiosity_growth_per_hour,
                rest_growth_per_hour=self._config.drive_rest_growth_per_hour,
            )),
        )
        self._agent_loop = AgentLoop(
            audit=self._proactive_audit,
            delivery_coordinator=pipeline._delivery,
            data_service=self._data_service,
            config=self._config,
            event_bus=EventBus(),
            clock=SystemClock(),
            store=autonomy_store,
            scheduler=ContextScheduler(autonomy_store),
            drive_manager=drive_manager,
            state_manager=state_manager,
            daily_events=daily_events,
            relationship_manager=relationship,
            daily_policy=DailyProactivePolicy(
                autonomy_store,
                llm=llm_service if self._config.proactive_daily_probability_llm_enabled else None,
                base_probability=self._config.proactive_daily_probability,
                timeout=self._config.planner_timeout,
            ),
            memory=memory,
            proactive_planner=ProactivePlanner(
                llm_service, context_builder, self._config.planner_timeout
            ),
            response_generator=response_generator,
            scorer=ActionScorer(ActionScoreConfig(
                threshold=self._config.proactive_score_threshold,
                defer_threshold=self._config.proactive_defer_threshold,
                social_weight=self._config.proactive_social_weight,
                curiosity_weight=self._config.proactive_curiosity_weight,
                relationship_weight=self._config.proactive_relationship_weight,
                relevance_weight=self._config.proactive_relevance_weight,
                urgency_weight=self._config.proactive_urgency_weight,
                interruption_weight=self._config.proactive_interruption_weight,
                rest_weight=self._config.proactive_rest_weight,
            )),
            evaluator=ProactiveEvaluator(
                window_start=self._config.proactive_window_start,
                window_end=self._config.proactive_window_end,
            ),
            cooldown=CooldownManager(autonomy_store, CooldownConfig(
                min_interval=self._config.proactive_min_interval,
                same_topic_interval=self._config.proactive_same_topic_interval,
                daily_budget=self._config.proactive_daily_budget,
            )),
            sender=AstrBotProactiveSender(
                context,
                self.logger,
                audit=self._proactive_audit,
                split_enabled=bool(raw_config.get("segmented_send_enabled", True)),
                split_max_chars=_positive_int(
                    raw_config.get("segmented_send_max_chars", 120), 120
                ),
                split_interval_seconds=max(0.0, _finite_float(
                    raw_config.get("segmented_send_interval_seconds", 1.0), 1.0
                )),
                session_lock=self._send_coordinator.lock_for,
            ),
            session_manager=session_manager,
            logger=self.logger,
            decision_gate=ProactiveDecisionGate(
                decision_service.for_task("proactive"), enabled=self._config.jev_proactive_enabled
            ),
        )
        self._media_tasks: set[asyncio.Task] = set()
        self._reactive_tasks: set[asyncio.Task] = set()
        self._classification_tasks: set[asyncio.Task] = set()
        self._reactive_epoch: defaultdict[str, int] = defaultdict(int)
        self._registration_pending: defaultdict[str, int] = defaultdict(int)
        self._registrations_ready: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self._timing_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._recent_event_ids: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._closing = False
        self._media_initialized = False

    def _build_delivery_journal(self, data_dir):
        from .adapters.storage.delivery_journal import SQLiteDeliveryJournal
        path = Path(":memory:") if self._config.memory_backend.strip().lower() == "in_memory" else data_dir / "alice_operations.db"
        return SQLiteDeliveryJournal(path)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.command("alice-status")
    async def alice_status(self, event: AstrMessageEvent):
        from .alice_agent.proactive.diagnostics import format_status
        mapped = self._message_adapter.to_agent_event(event)
        status = await self._agent_loop.inspect_session(mapped.session_id, mapped.user_id)
        yield event.plain_result(format_status(status))
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.command("alice-data")
    async def alice_data(self, event: AstrMessageEvent):
        from .alice_agent.memory.governance import MemoryScope
        mapped = self._message_adapter.to_agent_event(event)
        raw = str(getattr(getattr(event, "message_obj", None), "message_str", None) or getattr(event, "message_str", ""))
        parts = raw.strip().split(maxsplit=1)
        arguments = parts[1] if len(parts) > 1 else "help"
        reply = await self._data_service.command(MemoryScope(mapped.user_id, mapped.session_id), arguments, trace_id=mapped.trace_id)
        from .adapters.astrbot.text_segments import split_bounded_text
        for part in split_bounded_text(reply, 1000):
            yield event.plain_result(part)
        event.stop_event()

    async def initialize(self) -> None:
        await self._proactive_audit.start(lambda: {
            "plugin_version": self._plugin_version,
            "enabled": self._config.enabled, "loop_enabled": self._config.agent_loop_enabled,
            "loop_running": self._agent_loop.running, "proactive_enabled": self._config.proactive_enabled,
            "daily_events_enabled": self._config.daily_events_enabled, "event_limit": self._config.daily_event_max,
            "send_window": [self._config.proactive_window_start, self._config.proactive_window_end],
            "tick_interval_seconds": self._config.agent_tick_interval,
            "active_window_seconds": self._config.active_conversation_window,
            "daily_budget": self._config.proactive_daily_budget,
            "min_interval_seconds": self._config.proactive_min_interval,
            "score_threshold": self._config.proactive_score_threshold,
            "daily_score_threshold": self._config.proactive_daily_score_threshold,
            "daily_attempt_limit": self._config.proactive_daily_attempt_limit,
            "daily_attempt_period": "local_calendar_day", "daily_budget_period": "rolling_24h",
            "retry_base_seconds": self._config.proactive_retry_base_seconds,
            "retry_max_seconds": self._config.proactive_retry_max_seconds,
            "scheduled_attempt_limit": 3,
        })
        await self._data_service.start()
        await self._decision_service.start()
        if not self._media_initialized:
            await self._media_renderer.initialize()
            self._media_initialized = True
        await self._agent_loop.start()


    # The event-type decorator must run first so its priority is retained by
    # AstrBot's shared handler metadata when the custom filter is attached.
    @filter.custom_filter(AlicePrivateMessageFilter, False)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=300000)
    async def on_private_message(self, event: AstrMessageEvent):
        if not self._config.enabled or self._closing:
            return
        if not self._config.response_timing_enabled:
            async with aclosing(self._handle_without_timing(event)) as stream:
                async for result in stream:
                    yield result
            return
        task = asyncio.current_task()
        if task is not None:
            self._reactive_tasks.add(task)
        prepared = None
        try:
            prepared = await self._prepare_pending_message(event)
            if prepared is None:
                return
            batch, _item, is_owner = prepared
            if not is_owner:
                # The current owner will answer the entire merged batch.
                event.stop_event()
                return
            async with aclosing(self._run_pending_batch(batch)) as stream:
                async for result in stream:
                    yield result
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Alice Agent adapter failed while preparing a private message")
            agent_event = None
            if prepared is not None:
                agent_event = prepared[1].agent_event
            if agent_event is not None:
                try:
                    await self._agent.record_suppressed(agent_event, {"status": "adapter_failure"})
                except Exception:
                    self.logger.exception("Alice Agent could not preserve failed input")
            yield event.plain_result(self._config.fallback_text)
            event.stop_event()
        finally:
            if task is not None:
                self._reactive_tasks.discard(task)

    async def terminate(self) -> None:
        self._closing = True
        self._test_logging_active = False
        # Stop new autonomous output before waiting on data maintenance/cleanup.
        # Its stores stay open until the remaining local commits have settled.
        await self._agent_loop.stop()
        await self._proactive_audit.close()
        await self._pending_replies.cancel_all()
        current = asyncio.current_task()
        tasks = {
            task for task in self._reactive_tasks | self._classification_tasks
            if task is not current and not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reactive_tasks.clear()
        self._classification_tasks.clear()
        for task in tuple(self._media_tasks):
            task.cancel()
        if self._media_tasks:
            await asyncio.gather(*self._media_tasks, return_exceptions=True)
        self._media_tasks.clear()
        await self._data_service.close()
        await self._agent_loop.close()
        await self._agent.close()
        await self._media_renderer.close()
        if self._vad_model is not None:
            await self._vad_model.close()
