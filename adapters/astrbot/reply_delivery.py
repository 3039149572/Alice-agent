from __future__ import annotations

import asyncio
import math
import time
from contextlib import aclosing, asynccontextmanager
from datetime import datetime, timezone

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent
from ...alice_agent.domain.event import AgentEvent
from ...alice_agent.domain.response import AgentResponse
from .pending_messages import PendingReplyBatch
from .text_segments import split_text
from .delivery import delivered_transcript
from .config_values import _finite_float, _positive_int


class ReplyDeliveryMixin:
    """Platform output, media leases and actual-prefix finalization.

    The composition root supplies config, renderer, agent, logger, diagnostic
    writer and data service. No provider or framework objects reach the Core.
    Generator close/cancellation must finalize before releasing ownership.
    """

    def _segment_settings(self) -> tuple[bool, int, float]:
        return (
            bool(self._raw_config.get("segmented_send_enabled", True)),
            _positive_int(self._raw_config.get("segmented_send_max_chars", 120), 120),
            max(0.0, _finite_float(
                self._raw_config.get("segmented_send_interval_seconds", 1.0), 1.0
            )),
        )

    async def _record_test_turn(
        self, agent_event, response, delivery,
    ) -> None:
        service = getattr(self, "_data_service", None)
        async with self._guard_user_data(agent_event.user_id if service is not None else ""):
            if service is None or service.is_current(agent_event):
                await self._record_test_turn_with_data(agent_event, response, delivery)

    async def _record_test_turn_with_data(
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

    @asynccontextmanager
    async def _guard_user_data(self, user_id):
        data_service = getattr(self, "_data_service", None)
        if data_service is None:
            yield
        else:
            async with data_service.guard(user_id):
                yield

    async def _deliver_response_locked(
        self, framework_event, agent_event, response, batch, version, epoch, delivery, outcome,
    ):
        data_service = getattr(self, "_data_service", None)
        async with self._guard_user_data(agent_event.user_id if data_service is not None else ""):
            if data_service is not None and not data_service.is_current(agent_event):
                outcome["stale_before_output"] = True
                return
            async with aclosing(self._deliver_response_with_data(framework_event, agent_event, response, batch, version, epoch, delivery, outcome)) as stream:
                async for result in stream:
                    yield result

    async def _deliver_response_with_data(
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
        rendered = None
        render_task = None

        async def render_media():
            from .media_renderer import RenderedMedia
            started = time.perf_counter()
            budget = self._media_config.render_timeout_seconds
            deadline = agent_event.metadata.get("_turn_deadline_monotonic")
            if isinstance(deadline, (int, float)) and math.isfinite(deadline):
                budget = min(budget, max(0.0, deadline - time.monotonic()))
            try:
                return await asyncio.wait_for(self._media_renderer.render(
                    session_id, response.text, response.metadata.get("media_decision"),
                ), timeout=budget)
            except TimeoutError:
                delivery["media_render_timed_out"] = True
                return RenderedMedia(timed_out=True)
            finally:
                delivery["media_render_ms"] = round((time.perf_counter() - started) * 1000.0, 3)

        async def finalize() -> None:
            nonlocal finalized
            if finalized or outcome.get("stale_before_output"):
                return
            finalized = True
            text_segments = delivery.get("yielded_text_segments", [])
            delivered_text = delivered_transcript(
                text_segments,
                getattr(rendered, "spoken_source_text", None) if audio_delivered else None,
            )
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

            split_enabled, max_chars, interval = self._segment_settings()
            text_segments = split_text(response.text, max_chars) if split_enabled else [response.text]
            if self._media_config.text_first and self._media_config.send_text_with_audio:
                render_task = asyncio.create_task(render_media())
                batch.phase = "delivering"
                for index, segment in enumerate(text_segments):
                    if not is_current():
                        outcome["stopped_for_new_input"] = True
                        break
                    delivery["yielded_text_segments"].append(segment)
                    delivery["segments_yielded"] = int(delivery["segments_yielded"]) + 1
                    yield framework_event.plain_result(segment)
                    if interval > 0 and index + 1 < len(text_segments):
                        await asyncio.sleep(interval)
                if not outcome.get("stopped_for_new_input") and is_current():
                    rendered = await render_task
                    unsupported = (
                        agent_event.source in {"qq_official", "qq_official_webhook"}
                        and type(getattr(getattr(framework_event, "message_obj", None), "raw_message", None)).__name__
                        in {"Message", "DirectMessage"}
                    )
                    if rendered.audio_path and not unsupported and is_current():
                        audio_delivered = True
                        delivery["audio_yielded"] = True
                        delivery["audio_route"] = "standalone_after_text"
                        delivery["segments_yielded"] = int(delivery["segments_yielded"]) + 1
                        yield framework_event.chain_result([Comp.Record(file=rendered.audio_path)])
                    if rendered.emoji_path and is_current():
                        delivery["emoji_send_completed"] = await self._send_emoji_delayed(
                            framework_event, rendered.emoji_path, is_current,
                        )
                else:
                    outcome["stopped_for_new_input"] = True
                if outcome.get("stopped_for_new_input"):
                    outcome["stopped_after_output"] = True
                delivery.update({
                    "status": "partial_after_new_input" if outcome.get("stopped_for_new_input") else "handed_to_framework",
                    "text_first": True, "text_segments_planned": len(text_segments),
                    "text_segments_yielded": len(delivery["yielded_text_segments"]),
                })
                await finalize()
                return

            rendered = await render_media()
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

        except GeneratorExit:
            if delivery.get("segments_yielded"):
                delivery["status"] = "closed_after_partial_output"
                await finalize()
            raise
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

        finally:
            if render_task is not None:
                if not render_task.done():
                    render_task.cancel()
                result = await asyncio.gather(render_task, return_exceptions=True)
                if rendered is None and result and not isinstance(result[0], BaseException):
                    rendered = result[0]
            if rendered is not None and rendered.audio_path:
                release = getattr(self._media_renderer, "release", None)
                if callable(release):
                    release(rendered.audio_path)
