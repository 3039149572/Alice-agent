from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import time
import weakref
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import astrbot.api.message_components as Comp
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
from .adapters.memory import build_memory_port
from .adapters.jev import DEFAULT_SYSTEMONE_ENDPOINT, TypesafeJevAdapter
from .adapters.storage import InMemoryStorageAdapter, SQLiteStorageAdapter
from .adapters.vad import HTTPVADModelAdapter
from .adapters.diagnostics import AtomicJSONTurnLogger
from .adapters.astrbot.text_segments import split_text
from .alice_agent.config import AgentConfig
from .alice_agent.core.agent import AliceAgent
from .alice_agent.core.pipeline import AgentPipeline
from .alice_agent.core.session_manager import SessionManager
from .alice_agent.generation.context_builder import ContextBuilder
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
from .alice_agent.loop.lifecycle import SystemClock
from .alice_agent.loop.scheduler import ContextScheduler
from .alice_agent.loop.store import InMemoryAutonomyStore, SQLiteAutonomyStore
from .alice_agent.proactive.cooldown import CooldownConfig, CooldownManager
from .alice_agent.proactive.evaluator import ProactiveEvaluator
from .alice_agent.proactive.planner import ProactivePlanner
from .alice_agent.proactive.scorer import ActionScoreConfig, ActionScorer
from .alice_agent.proactive.decision_gate import ProactiveDecisionGate
from .alice_agent.decision.models import DecisionMode
from .alice_agent.decision.policies import DecisionEscalationPolicy
from .alice_agent.decision.service import DecisionService
from .alice_agent.decision.response_timing import ResponseTimingService
from .alice_agent.decision.response_timing import ReplyTimingDecision
from .alice_agent.domain.response import AgentResponse
from .alice_agent.domain.event import AgentEvent
from .adapters.astrbot.pending_messages import (
    PendingMessage,
    PendingReplyBatch,
    PendingReplyCoordinator,
)
from .alice_agent.memory.decision_gate import MemoryDecisionGate
from .alice_agent.perception.decision_service import DecisionAwarePerceptionService
from .alice_agent.planner.decision_service import DecisionAwarePlannerService


class SessionSendCoordinator:
    """Share one short-lived send lock per active session."""

    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock


def _finite_float(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _positive_int(value: object, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, number)


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
        perception_enabled=bool(config.get("perception_enabled", True)),
        planner_enabled=bool(config.get("planner_enabled", True)),
        memory_enabled=bool(config.get("memory_enabled", True)),
        memory_metadata_enabled=bool(config.get("memory_metadata_enabled", True)),
        memory_backend=str(config.get("memory_backend", "local_sqlite")),
        history_limit=int(config.get("history_limit", 8)),
        session_persistence_enabled=bool(
            config.get("session_persistence_enabled", True)
        ),
        history_restore_max_age_hours=_finite_float(
            config.get("history_restore_max_age_hours"), 72.0
        ),
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
        proactive_daily_score_threshold=float(config.get("proactive_daily_score_threshold", 0.45)),
        proactive_defer_threshold=float(config.get("proactive_defer_threshold", 0.60)),
        proactive_min_interval=float(config.get("proactive_min_interval", 21600.0)),
        proactive_same_topic_interval=float(config.get("proactive_same_topic_interval", 86400.0)),
        proactive_daily_budget=int(config.get("proactive_daily_budget", 1)),
        proactive_window_start=str(config.get("proactive_window_start", "09:00")),
        proactive_window_end=str(config.get("proactive_window_end", "21:00")),
        proactive_daily_attempt_limit=int(config.get("proactive_daily_attempt_limit", 2)),
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
    )


class AliceAgentPlugin(Star):
    """Thin AstrBot entry point for the framework-independent Alice core."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        raw_config = config if config is not None else {}
        super().__init__(context, raw_config)
        self._raw_config = raw_config
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
        llm_service = LLMService(AstrBotLLMAdapter(context), self._config.llm_timeout)
        context_builder = ContextBuilder(profile=self._config.persona_profile)
        decision_mode = (
            DecisionMode(self._config.jev_mode)
            if self._config.jev_enabled else DecisionMode.OFF
        )
        decision_port = None
        if decision_mode != DecisionMode.OFF and self._config.jev_api_key:
            decision_port = TypesafeJevAdapter(
                endpoint=self._config.jev_endpoint,
                api_key=self._config.jev_api_key,
                model=self._config.jev_model,
                timeout_ms=self._config.jev_timeout_ms,
            )
        elif decision_mode != DecisionMode.OFF:
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
        )
        self._test_logger = AtomicJSONTurnLogger(data_dir / "test_turns")
        self._send_coordinator = SessionSendCoordinator()
        self._pending_replies = PendingReplyCoordinator()
        self._timing_service = ResponseTimingService(
            decision_service, enabled=self._config.response_timing_enabled
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
                decision_service,
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
                decision_service,
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
            decision_service,
            enabled=self._config.daily_events_enabled,
            max_events=self._config.daily_event_max,
            llm_enabled=self._config.daily_event_llm_enabled,
            jev_enabled=self._config.daily_event_jev_enabled,
            logger=self.logger,
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
            config=self._config,
            perception=DecisionAwarePerceptionService(
                existing_perception,
                decision_service,
                enabled=self._config.jev_perception_enabled,
                vad_model=self._vad_model,
                vad_enabled=self._vad_enabled,
                jev_weight=float(raw_config.get("jev_vad_weight", 0.5)),
            ),
            state_manager=state_manager,
            memory=memory,
            planner=DecisionAwarePlannerService(
                existing_planner,
                decision_service,
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
            config=self._config,
            event_bus=EventBus(),
            clock=SystemClock(),
            store=autonomy_store,
            scheduler=ContextScheduler(autonomy_store),
            drive_manager=drive_manager,
            state_manager=state_manager,
            daily_events=daily_events,
            relationship_manager=relationship,
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
                decision_service, enabled=self._config.jev_proactive_enabled
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

    async def initialize(self) -> None:
        await self._decision_service.start()
        if not self._media_initialized:
            await self._media_renderer.initialize()
            self._media_initialized = True
        await self._agent_loop.start()

    def _segment_settings(self) -> tuple[bool, int, float]:
        return (
            bool(self._raw_config.get("segmented_send_enabled", True)),
            _positive_int(self._raw_config.get("segmented_send_max_chars", 120), 120),
            max(0.0, _finite_float(
                self._raw_config.get("segmented_send_interval_seconds", 1.0), 1.0
            )),
        )

    async def _record_test_turn(
        self,
        agent_event,
        response: AgentResponse,
        delivery: dict[str, object],
    ) -> None:
        # Read the live config object at write time so turning this switch off
        # immediately stops the next completed turn from being persisted.
        if (
            not self._test_logging_active
            or not bool(self._raw_config.get("test_log_enabled", False))
        ):
            return
        diagnostics = response.metadata.get("turn_diagnostics", {})
        record = {
            "schema_version": "alice_turn_test_v1",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": agent_event.trace_id,
            "session_id": agent_event.session_id,
            "user_id": agent_event.user_id,
            "source": agent_event.source,
            "event_type": agent_event.event_type or agent_event.type,
            "event_timestamp": agent_event.timestamp,
            "recent_conversation": (
                diagnostics.get("history_before_turn", [])
                if isinstance(diagnostics, dict) else []
            ),
            "user_text": agent_event.text or "",
            "user_messages": list(
                agent_event.payload.get("user_messages", [])
                if isinstance(agent_event.payload, dict) else []
            ),
            "response_timing": (
                agent_event.metadata.get("response_timing")
                if isinstance(agent_event.metadata, dict) else None
            ),
            # Store what was actually yielded to AstrBot separately from the
            # full generated answer. A partial failure must not look like a
            # complete reply in the dialogue log.
            "assistant_text": "".join(
                str(part) for part in delivery.get("yielded_text_segments", [])
            ),
            "generated_assistant_text": response.text,
            "yielded_text_segments": list(
                delivery.get("yielded_text_segments", [])
            ),
            "action": response.action,
            "fallback": bool(
                response.metadata.get("fallback")
                or delivery.get("fallback_yielded")
            ),
            "diagnostics": diagnostics,
            "decision_comparison": (
                diagnostics.get("decision_comparison")
                if isinstance(diagnostics, dict) else None
            ),
            "delivery": delivery,
        }
        try:
            await asyncio.to_thread(
                self._test_logger.write,
                record,
                enabled=lambda: self._test_logging_active
                and bool(self._raw_config.get("test_log_enabled", False)),
                retention_days=min(3650, max(0, int(_finite_float(
                    self._raw_config.get("test_log_retention_days", 0), 0.0
                )))),
            )
        except Exception:
            self.logger.exception(
                f"[{agent_event.trace_id}] Alice test-turn JSON write failed safely"
            )

    async def _send_emoji_delayed(
        self, event: AstrMessageEvent, path: str, is_current=None
    ) -> bool:
        try:
            await asyncio.sleep(self._media_config.emoji_delay_seconds)
            if is_current is not None and not is_current():
                return False
            result = await event.send(
                event.chain_result([Comp.Image.fromFileSystem(path=path)])
            )
            if result is False:
                self.logger.warning("Alice media: delayed emoji send was rejected")
                return False
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Alice media: delayed emoji send failed safely")
            return False

    def _begin_input_registration(self, session_id: str, event_id: str) -> bool:
        key = (session_id, event_id)
        if key in self._recent_event_ids:
            return False
        self._recent_event_ids[key] = None
        while len(self._recent_event_ids) > 4096:
            self._recent_event_ids.popitem(last=False)
        if self._registration_pending[session_id] == 0:
            self._registrations_ready[session_id].clear()
        self._registration_pending[session_id] += 1
        # This synchronous epoch bump is deliberately before every await: it
        # invalidates any old generation before database or Jev latency.
        self._reactive_epoch[session_id] += 1
        return True

    def _finish_input_registration(self, session_id: str) -> None:
        self._registration_pending[session_id] = max(
            0, self._registration_pending[session_id] - 1
        )
        if self._registration_pending[session_id] == 0:
            self._registrations_ready[session_id].set()

    async def _prepare_pending_message(
        self, framework_event: AstrMessageEvent
    ) -> tuple[PendingReplyBatch, PendingMessage, bool] | None:
        agent_event = self._message_adapter.to_agent_event(framework_event)
        session_id = agent_event.session_id
        if not self._begin_input_registration(session_id, agent_event.event_id):
            framework_event.stop_event()
            return None
        item: PendingMessage | None = None
        try:
            revision = await self._agent_loop.register_user_event(agent_event)
            if revision is None:
                framework_event.stop_event()
                return None
            item = PendingMessage(
                agent_event=agent_event,
                framework_event=framework_event,
                merge_safe=bool(agent_event.metadata.get("merge_safe", False)),
                revision=revision,
            )
            batch, is_owner = await self._pending_replies.submit(
                item,
                boundary=ResponseTimingService.has_batch_boundary(agent_event.text or ""),
                cancel_current=ResponseTimingService.is_no_reply_command(agent_event.text or ""),
            )
        finally:
            self._finish_input_registration(session_id)
        try:
            await self._agent_loop.apply_user_activity(agent_event)
        except Exception:
            self.logger.exception(
                f"[{agent_event.trace_id}] Alice user activity update failed safely"
            )
        classifier = asyncio.create_task(self._classify_pending_item(batch, item))
        item.classification_task = classifier
        self._classification_tasks.add(classifier)
        classifier.add_done_callback(self._classification_tasks.discard)
        return batch, item, is_owner

    async def _classify_pending_item(
        self, batch: PendingReplyBatch, item: PendingMessage
    ) -> None:
        try:
            async with self._timing_locks[batch.session_id]:
                if item.timing is not None or batch.phase == "cancelled":
                    return
                messages = [candidate.agent_event.text or "" for candidate in batch.items]
                history = await self._session_manager.load_history(batch.session_id)
                decision = await self._timing_service.decide(
                    item.agent_event,
                    history=history,
                    current_messages=messages,
                )
                if item.timing is None:
                    item.timing = decision
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                f"[{item.agent_event.trace_id}] response timing failed; using local fallback ({type(exc).__name__})"
            )
            if item.timing is None:
                item.timing = self._timing_service.local_fallback(item.agent_event)
        finally:
            if item.timing is None and batch.phase != "cancelled":
                item.timing = self._timing_service.local_fallback(item.agent_event)
            item.timing_ready.set()

    async def _await_pending_registrations(self, session_id: str) -> None:
        if self._registration_pending[session_id] > 0:
            await self._registrations_ready[session_id].wait()

    async def _resolve_batch_timing(
        self, batch: PendingReplyBatch
    ) -> tuple[str, bool, float]:
        hard_deadline = batch.created_at + self._config.response_wait_max_seconds
        while not self._closing:
            remaining = hard_deadline - time.monotonic()
            try:
                await asyncio.wait_for(
                    batch.wait_for_classification(), timeout=max(0.001, remaining)
                )
            except asyncio.TimeoutError:
                # Jev latency cannot extend the first-message hard deadline.
                # Freeze unfinished classifications to their deterministic
                # local rules and cancel their late model requests.
                for item in batch.items:
                    if item.timing is None:
                        item.timing = self._timing_service.local_fallback(
                            item.agent_event, status="hard_deadline_fallback"
                        )
                        item.timing_ready.set()
                        if item.classification_task is not None:
                            item.classification_task.cancel()
                frozen_action = batch.action()
                if frozen_action == "ignore":
                    return "ignore", True, max(0.0, time.monotonic() - batch.created_at)
                return "reply_now", True, max(0.0, time.monotonic() - batch.created_at)
            if batch.phase == "cancelled" or self._closing:
                return "cancelled", False, max(0.0, time.monotonic() - batch.created_at)

            action = batch.action()
            if action in {"ignore", "reply_now"}:
                return action, False, max(0.0, time.monotonic() - batch.created_at)
            if any(not item.merge_safe for item in batch.items):
                return "reply_now", False, max(0.0, time.monotonic() - batch.created_at)
            now = time.monotonic()
            if now >= hard_deadline:
                return "reply_now", True, max(0.0, now - batch.created_at)
            quiet_deadline = batch.last_input_at + self._config.response_wait_quiet_seconds
            target = min(quiet_deadline, hard_deadline)
            if now >= target:
                return "reply_now", target == hard_deadline, max(0.0, now - batch.created_at)
            batch.changed.clear()
            try:
                await asyncio.wait_for(batch.changed.wait(), timeout=target - now)
            except asyncio.TimeoutError:
                continue
        return "cancelled", False, max(0.0, time.monotonic() - batch.created_at)

    @staticmethod
    def _batch_timing_diagnostics(
        batch: PendingReplyBatch, actual_wait_seconds: float
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for item in batch.items:
            timing = item.timing
            if timing is None:
                diagnostic: dict[str, object] = {"status": "pending"}
            else:
                diagnostic = dict(timing.diagnostic)
                diagnostic.update({
                    "selected_action": timing.action,
                    "selected_source": timing.source,
                    "confidence": timing.confidence,
                })
            diagnostic["event_id"] = item.agent_event.event_id
            diagnostic["actual_wait_seconds"] = round(actual_wait_seconds, 3)
            result.append(diagnostic)
        return result

    def _make_batch_event(
        self,
        batch: PendingReplyBatch,
        *,
        actual_wait_seconds: float,
        hard_deadline_reached: bool,
    ) -> AgentEvent:
        items = list(batch.items)
        first = items[0].agent_event
        last = items[-1].agent_event
        texts = [item.agent_event.text or "" for item in items]
        joined_text = "\n".join(texts)
        messages = [
            {
                "event_id": item.agent_event.event_id,
                "timestamp": item.agent_event.timestamp,
                "text": item.agent_event.text or "",
            }
            for item in items
        ]
        timing_diagnostics = self._batch_timing_diagnostics(batch, actual_wait_seconds)
        metadata = dict(first.metadata)
        metadata.update({
            "merge_safe": all(item.merge_safe for item in items),
            "timing_force_reply": True,
            "response_timing": {
                "decisions": timing_diagnostics,
                "actual_wait_seconds": round(actual_wait_seconds, 3),
                "hard_deadline_reached": hard_deadline_reached,
                "message_count": len(items),
            },
        })
        payload = dict(first.payload)
        payload["text"] = joined_text
        payload["user_messages"] = messages
        return AgentEvent(
            event_id=first.event_id,
            trace_id=first.trace_id,
            type=first.type,
            source=first.source,
            session_id=first.session_id,
            user_id=first.user_id,
            timestamp=last.timestamp,
            text=joined_text,
            metadata=metadata,
            event_type=first.event_type,
            payload=payload,
        )

    def _is_batch_current(
        self, batch: PendingReplyBatch, version: int, epoch: int
    ) -> bool:
        return (
            not self._closing
            and batch.phase != "cancelled"
            and batch.version == version
            and self._reactive_epoch[batch.session_id] == epoch
        )

    async def _handle_without_timing(self, framework_event: AstrMessageEvent):
        """Compatibility path: no merge window, Jev timing, or stale-send cutoff."""
        task = asyncio.current_task()
        if task is not None:
            self._reactive_tasks.add(task)
        try:
            agent_event = self._message_adapter.to_agent_event(framework_event)
            if not await self._agent_loop.on_user_event(agent_event):
                framework_event.stop_event()
                return
            item = PendingMessage(
                agent_event=agent_event,
                framework_event=framework_event,
                merge_safe=False,
                revision=0,
                timing=ReplyTimingDecision(
                    "reply_now", "disabled", 1.0,
                    {"status": "timing_disabled", "selected_action": "reply_now"},
                ),
            )
            item.timing_ready.set()
            batch = PendingReplyBatch(
                session_id=agent_event.session_id,
                items=[item],
                phase="processing",
                max_messages=1,
            )
            version = batch.version
            epoch = self._reactive_epoch[agent_event.session_id]
            async with self._send_coordinator.lock_for(agent_event.session_id):
                async def always_current() -> bool:
                    return not self._closing

                response = await self._agent.handle_event(
                    agent_event, is_current=always_current
                )
                if not response.text:
                    delivery: dict[str, object] = {
                        "status": "no_text",
                        "segments_yielded": 0,
                        "yielded_text_segments": [],
                        "audio_yielded": False,
                        "emoji_send_completed": False,
                        "delivery_confirmation": "platform_acceptance_unknown",
                    }
                    await self._agent.commit_delivery(agent_event, response, "")
                    await self._record_test_turn(agent_event, response, delivery)
                    framework_event.stop_event()
                    return
                delivery: dict[str, object] = {
                    "status": "not_sent",
                    "segments_yielded": 0,
                    "yielded_text_segments": [],
                    "audio_yielded": False,
                    "emoji_send_completed": False,
                    "delivery_confirmation": "platform_acceptance_unknown",
                }
                outcome = {"stale_before_output": False, "stopped_for_new_input": False}
                async for result in self._deliver_response_locked(
                    framework_event, agent_event, response, batch,
                    version, epoch, delivery, outcome,
                ):
                    yield result
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Alice Agent adapter failed while processing a private message")
            yield framework_event.plain_result(self._config.fallback_text)
            framework_event.stop_event()
        finally:
            if task is not None:
                self._reactive_tasks.discard(task)

    async def _deliver_response_locked(
        self,
        framework_event: AstrMessageEvent,
        agent_event: AgentEvent,
        response: AgentResponse,
        batch: PendingReplyBatch,
        version: int,
        epoch: int,
        delivery: dict[str, object],
        outcome: dict[str, bool],
    ):
        """Yield platform results and commit only the content handed to AstrBot."""
        session_id = agent_event.session_id
        is_current = lambda: self._is_batch_current(batch, version, epoch)
        audio_delivered = False
        finalized = False

        async def finalize() -> None:
            nonlocal finalized
            if finalized or outcome.get("stale_before_output"):
                return
            finalized = True
            text_segments = delivery.get("yielded_text_segments", [])
            delivered_text = "".join(str(part) for part in text_segments) if isinstance(text_segments, list) else ""
            if audio_delivered:
                # A rendered voice file represents the complete generated text.
                delivered_text = response.text or delivered_text
            if delivered_text or audio_delivered:
                await self._agent.commit_delivery(agent_event, response, delivered_text)
            else:
                await self._agent.record_suppressed(
                    agent_event,
                    agent_event.metadata.get("response_timing", {}),
                )
            await self._record_test_turn(agent_event, response, delivery)
            framework_event.stop_event()

        try:
            if not is_current():
                outcome["stale_before_output"] = True
                return
            if not response.text:
                fallback_text = self._config.fallback_text
                batch.phase = "delivering"
                delivery.update({"status": "handed_to_framework", "fallback_yielded": True})
                text_segments = delivery["yielded_text_segments"]
                if isinstance(text_segments, list):
                    text_segments.append(fallback_text)
                delivery["segments_yielded"] = 1
                yield framework_event.plain_result(fallback_text)
                await finalize()
                return

            rendered = await self._media_renderer.render(
                session_id,
                response.text,
                response.metadata.get("media_decision"),
            )
            if not is_current():
                outcome["stale_before_output"] = True
                return
            diagnostics = response.metadata.get("turn_diagnostics")
            if isinstance(diagnostics, dict):
                diagnostics["media_render"] = {
                    "media_enabled": self._media_config.enabled,
                    "voice_runtime_available": bool(getattr(self._media_renderer, "voice_runtime_available", False)),
                    "voice_requested": bool(getattr(response.metadata.get("media_decision"), "send_voice", False)),
                    "audio_available": bool(rendered.audio_path),
                    "emoji_available": bool(rendered.emoji_path),
                    "text_with_audio": self._media_config.send_text_with_audio,
                }

            split_enabled, max_chars, interval = self._segment_settings()
            text_segments = split_text(response.text, max_chars) if split_enabled else [response.text]
            text_is_sent = not rendered.audio_path or self._media_config.send_text_with_audio
            qq_voice_standalone = bool(
                rendered.audio_path
                and agent_event.source in {"qq_official", "qq_official_webhook"}
            )
            qq_message_kind = type(
                getattr(getattr(framework_event, "message_obj", None), "raw_message", None)
            ).__name__
            delivery["audio_route"] = (
                "standalone_qq_official" if qq_voice_standalone
                else "combined_with_text" if rendered.audio_path and text_is_sent
                else "standalone" if rendered.audio_path else "none"
            )
            qq_guild_voice_unsupported = bool(
                qq_voice_standalone and qq_message_kind in {"Message", "DirectMessage"}
            )
            if qq_guild_voice_unsupported:
                delivery["audio_route"] = "qq_official_guild_adapter_unsupported"
                self.logger.warning(
                    "Alice media: installed QQ official guild adapter has no voice upload path"
                )
                # Do not mark an unsupported QQ guild-DM Record as delivered.
                # Fall back to plain text even when text_with_audio is disabled.
                qq_voice_standalone = False
                text_is_sent = True
                render_audio_path = None
            else:
                render_audio_path = rendered.audio_path

            batch.phase = "delivering"
            if qq_voice_standalone and render_audio_path:
                if not is_current():
                    outcome["stopped_for_new_input"] = True
                else:
                    audio_delivered = True
                    delivery["audio_yielded"] = True
                    delivery["segments_yielded"] = int(delivery["segments_yielded"]) + 1
                    yield framework_event.chain_result([Comp.Record(file=render_audio_path)])
            if not outcome.get("stopped_for_new_input") and render_audio_path and not text_is_sent:
                if not qq_voice_standalone:
                    if is_current():
                        audio_delivered = True
                        delivery["audio_yielded"] = True
                        delivery["segments_yielded"] = int(delivery["segments_yielded"]) + 1
                        yield framework_event.chain_result([Comp.Record(file=render_audio_path)])
                    else:
                        outcome["stopped_for_new_input"] = True
            elif not outcome.get("stopped_for_new_input"):
                for index, segment in enumerate(text_segments):
                    if not is_current():
                        outcome["stopped_for_new_input"] = True
                        break
                    if index == 0 and render_audio_path and not qq_voice_standalone:
                        result = framework_event.chain_result([
                            Comp.Record(file=render_audio_path), Comp.Plain(segment)
                        ])
                        audio_delivered = True
                        delivery["audio_yielded"] = True
                    else:
                        result = framework_event.plain_result(segment)
                    text_out = delivery["yielded_text_segments"]
                    if isinstance(text_out, list):
                        text_out.append(segment)
                    delivery["segments_yielded"] = int(delivery["segments_yielded"]) + 1
                    yield result
                    if interval > 0 and index + 1 < len(text_segments):
                        await asyncio.sleep(interval)

            if rendered.emoji_path and is_current():
                delivery["emoji_send_completed"] = await self._send_emoji_delayed(
                    framework_event, rendered.emoji_path, is_current
                )
            delivery["status"] = (
                "partial_after_new_input" if outcome.get("stopped_for_new_input")
                else "handed_to_framework"
            )
            delivery["text_segments_planned"] = len(text_segments) if text_is_sent else 0
            delivery["text_segments_yielded"] = len(delivery["yielded_text_segments"])
            delivery["segmented"] = bool(split_enabled and len(text_segments) > 1)
            if outcome.get("stopped_for_new_input"):
                outcome["stopped_after_output"] = True
            await finalize()
        except asyncio.CancelledError:
            if delivery.get("segments_yielded"):
                delivery["status"] = "cancelled_after_partial_output"
                await finalize()
            elif not outcome.get("stale_before_output"):
                delivery["status"] = "cancelled_before_output"
                await finalize()
            raise
        except Exception as exc:
            self.logger.exception("Alice Agent adapter failed while delivering a private reply")
            delivery["error_kind"] = type(exc).__name__
            if not delivery.get("segments_yielded") and is_current():
                fallback_text = self._config.fallback_text
                batch.phase = "delivering"
                text_out = delivery["yielded_text_segments"]
                if isinstance(text_out, list):
                    text_out.append(fallback_text)
                delivery["segments_yielded"] = 1
                delivery["fallback_yielded"] = True
                delivery["status"] = "fallback_handed_to_framework"
                yield framework_event.plain_result(fallback_text)
            elif delivery.get("segments_yielded"):
                delivery["status"] = "partial_failure"
            await finalize()

    async def _run_pending_batch(self, batch: PendingReplyBatch):
        owner_event = batch.items[0].framework_event
        session_id = batch.session_id
        try:
            while not self._closing:
                action, hard_deadline_reached, actual_wait = await self._resolve_batch_timing(batch)
                if action == "cancelled" or self._closing:
                    return
                snapshot_version = batch.version
                snapshot_epoch = self._reactive_epoch[session_id]
                agent_event = self._make_batch_event(
                    batch,
                    actual_wait_seconds=actual_wait,
                    hard_deadline_reached=hard_deadline_reached,
                )
                if not await self._pending_replies.wait_turn(batch):
                    return
                if not self._is_batch_current(batch, snapshot_version, snapshot_epoch):
                    continue

                delivery: dict[str, object] = {
                    "status": "not_sent",
                    "segments_yielded": 0,
                    "yielded_text_segments": [],
                    "audio_yielded": False,
                    "emoji_send_completed": False,
                    "delivery_confirmation": "platform_acceptance_unknown",
                    "timing_wait_seconds": round(actual_wait, 3),
                }
                outcome = {"stale_before_output": False, "stopped_for_new_input": False}
                if not self._is_batch_current(batch, snapshot_version, snapshot_epoch):
                    continue
                if action == "ignore":
                    batch.phase = "delivering"
                    response = AgentResponse(
                        text=None,
                        trace_id=agent_event.trace_id,
                        action="ignore",
                        metadata={
                            "turn_diagnostics": {
                                "response_timing": agent_event.metadata.get("response_timing", {})
                            }
                        },
                    )
                    delivery["status"] = "ignored_by_timing"
                    await self._agent.record_suppressed(
                        agent_event,
                        agent_event.metadata.get("response_timing", {}),
                    )
                    await self._record_test_turn(agent_event, response, delivery)
                    owner_event.stop_event()
                    return

                # Generation is intentionally outside the shared send lock:
                # intake can invalidate this revision immediately, and
                # proactive sends are not held behind a potentially slow LLM.
                batch.phase = "processing"

                async def guard() -> bool:
                    return self._is_batch_current(batch, snapshot_version, snapshot_epoch)

                try:
                    response = await self._agent.handle_event(
                        agent_event, is_current=guard
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.exception("Alice Agent pipeline failed while handling a private message")
                    response = AgentResponse(
                        text=self._config.fallback_text,
                        trace_id=agent_event.trace_id,
                        metadata={
                            "fallback": True,
                            "turn_diagnostics": {
                                "pipeline_failure": {"error_kind": type(exc).__name__}
                            },
                        },
                    )
                if response.action == "superseded" or not self._is_batch_current(
                    batch, snapshot_version, snapshot_epoch
                ):
                    outcome["stale_before_output"] = True
                else:
                    async with self._send_coordinator.lock_for(session_id):
                        if not self._is_batch_current(batch, snapshot_version, snapshot_epoch):
                            outcome["stale_before_output"] = True
                        else:
                            async for result in self._deliver_response_locked(
                                owner_event,
                                agent_event,
                                response,
                                batch,
                                snapshot_version,
                                snapshot_epoch,
                                delivery,
                                outcome,
                            ):
                                yield result

                if outcome["stale_before_output"]:
                    await self._await_pending_registrations(session_id)
                    continue
                owner_event.stop_event()
                return
        except asyncio.CancelledError:
            if batch.phase not in {"done", "cancelled", "delivering"} and not self._closing:
                try:
                    event = self._make_batch_event(
                        batch,
                        actual_wait_seconds=max(0.0, time.monotonic() - batch.created_at),
                        hard_deadline_reached=False,
                    )
                    await self._agent.record_suppressed(
                        event, event.metadata.get("response_timing", {})
                    )
                except Exception:
                    self.logger.exception("Alice Agent could not preserve cancelled input safely")
            raise
        finally:
            await self._pending_replies.finish(batch)

    # The event-type decorator must run first so its priority is retained by
    # AstrBot's shared handler metadata when the custom filter is attached.
    @filter.custom_filter(AlicePrivateMessageFilter, False)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=300000)
    async def on_private_message(self, event: AstrMessageEvent):
        if not self._config.enabled or self._closing:
            return
        if not self._config.response_timing_enabled:
            async for result in self._handle_without_timing(event):
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
            async for result in self._run_pending_batch(batch):
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
        await self._agent_loop.close()
        await self._agent.close()
        await self._media_renderer.close()
        if self._vad_model is not None:
            await self._vad_model.close()
