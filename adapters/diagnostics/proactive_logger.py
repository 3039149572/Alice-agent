from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from .turn_logger import _json_safe


MESSAGES = {
    "startup": "插件启动，记录主动消息配置",
    "heartbeat": "主动消息诊断仍在运行",
    "shutdown": "插件停止，主动消息循环已退出",
    "loop_started": "主动消息循环已启动",
    "loop_disabled": "后台循环已关闭，不会执行主动消息判断",
    "tick": "开始本轮检查；没有已登记会话时不会产生主动候选",
    "evaluation": "开始评估此会话",
    "daily_events_start": "读取或生成当日事件",
    "daily_events": "当日事件检查完成",
    "daily_events_error": "当日事件检查异常，本轮继续按现有规则判断",
    "candidate_availability": "检查候选来源",
    "candidate": "候选评估完成",
    "cooldown": "检查冷却及滚动24小时预算",
    "score": "候选评分完成",
    "probability_start": "读取当日概率结果；尚无结果时才计算并抽签",
    "probability": "记录当日概率结果，不额外抽签",
    "attempt": "检查每日规划尝试额度",
    "memory_start": "开始召回候选相关记忆",
    "memory": "相关记忆召回完成",
    "jev_start": "开始主动消息决策检查",
    "jev": "主动消息决策检查完成",
    "planner_start": "开始模型规划",
    "planner": "模型规划完成",
    "generation_start": "开始生成主动消息正文",
    "generation": "正文生成完成，仅记录字符数",
    "send_start": "开始平台发送，逐段重新检查有效性",
    "pre_send_blocked": "发送前检查未通过，停止后续分段",
    "platform_accepted": "框架接受此段发送；这不代表QQ送达回执",
    "platform_rejected": "框架返回False，检查会话路由、适配器日志及平台权限",
    "platform_error": "发送或发送前检查异常，仅记录异常类型",
    "delivery_commit": "已接受的发送内容已完成本地提交",
    "delivery_pending": "发送已有进展，但本地提交异常，等待恢复",
    "send_result": "平台发送流程结束；accepted/partial不代表QQ送达回执",
    "blocked": "本轮没有发送或未完成全部发送，原因见reason",
    "cancelled": "主动流程被取消，已接受的前缀仍按交付机制保存",
    "evaluation_error": "会话评估异常，本轮中断，后续Tick重试",
    "loop_error": "后台循环异常，将按退避规则重试",
}

REASONS = {
    "active conversation window": "仍在活跃聊天保护期",
    "proactive disabled": "主动消息开关已关闭",
    "outside local send window": "不在允许发送的服务器本地时段",
    "previous proactive message is unanswered": "上次主动消息尚未收到回应",
    "daily probability chose silence": "当日概率结果选择安静，今天不会重新抽签",
    "daily proactive attempt limit reached": "当日规划尝试额度已用完",
    "session inactive over 7 days": "会话超过七天未活跃",
    "daily events disabled": "每日事件已关闭",
    "no daily events or daily attempts exhausted": "没有可用的当日事件，或尝试额度已用完",
    "no concrete context worth interrupting the user for": "没有值得打扰用户的具体候选内容",
    "daily_budget": "每用户滚动24小时发送预算已用完",
    "global_cooldown": "全局发送冷却中",
    "user_cooldown": "此用户发送冷却中",
    "same_topic_cooldown": "相同话题冷却中",
    "candidate expired": "候选已过期",
    "score below conservative threshold": "候选评分低于发送门槛",
    "score is plausible but not strong enough": "候选评分不足，暂缓发送",
    "score reached proactive threshold": "候选评分达到发送门槛",
    "memory recall failed": "记忆召回失败",
    "proactive planner failed safely": "规划模型失败、超时或无可用结果",
    "planner returned invalid JSON": "规划模型返回的JSON无效",
    "planner returned unsupported action": "规划模型返回不支持的动作",
    "response generation failed safely": "正文生成失败",
    "empty proactive response": "生成正文为空",
    "sender rejected proactive action": "发送器未接受发送，具体原因见此前平台或发送前检查记录",
    "new user event invalidated daily evaluation": "新用户消息使本轮概率评估失效",
    "new user event invalidated action": "新用户消息使已规划动作失效",
    "new user event invalidated generated action": "新用户消息使已生成正文失效",
}


class DailyProactiveLogger:
    """One UTF-8 JSON-lines file, appended today and replaced on local-date change."""

    def __init__(self, path: str | Path, logger, *, clock=time.time) -> None:
        self.path = Path(path)
        self._logger, self._clock = logger, clock
        self._lock = threading.RLock()
        self._day = None
        self._task = None
        self._last_warning = float("-inf")

    def _replace(self, lines) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             prefix=".proactive-", suffix=".tmp",
                                             dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                for line in lines:
                    stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _ensure_day(self, moment: datetime) -> None:
        day = moment.date().isoformat()
        if self.path.is_symlink():
            raise OSError("daily proactive log must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._day == day and self.path.is_file():
            return
        try:
            with self.path.open(encoding="utf-8") as stream:
                header = json.loads(stream.readline())
            same_day = header.get("schema") == "alice_proactive_daily_v1" and header.get("date") == day
        except (OSError, ValueError, AttributeError):
            same_day = False
        if not same_day:
            self._replace([json.dumps({
                "schema": "alice_proactive_daily_v1", "date": day,
                "timezone": moment.tzname(), "utc_offset": moment.strftime("%z"),
                "说明": "只保留服务器本地今天的主动消息过程；跨日覆盖。包含会话标识和简短决策原因，不保存聊天正文、提示词或密钥。框架接受不等于QQ送达。",
            }, ensure_ascii=False)])
        else:
            # Recover an interrupted final append without joining two JSON objects.
            with self.path.open("rb+") as stream:
                stream.seek(0, 2)
                if stream.tell():
                    stream.seek(-1, 2)
                    if stream.read(1) != b"\n":
                        stream.seek(0, 2)
                        stream.write(b"\n")
        self._day = day

    def _write(self, stage: str, details: dict) -> None:
        with self._lock:
            moment = datetime.fromtimestamp(self._clock()).astimezone()
            self._ensure_day(moment)
            record = {"time": moment.isoformat(timespec="seconds"), "stage": stage,
                      "message": MESSAGES.get(stage, stage), **_json_safe(details)}
            reason = record.get("reason")
            if reason:
                record["reason"] = REASONS.get(str(reason), str(reason)[:300])
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")

    async def record(self, stage: str, **details) -> None:
        try:
            await asyncio.to_thread(self._write, stage, details)
        except Exception as exc:
            # A full/read-only disk must not prevent the original proactive behavior.
            current = time.monotonic()
            if current - self._last_warning >= 300:
                self._last_warning = current
                self._logger.warning(f"Alice daily proactive log unavailable ({type(exc).__name__}); path={self.path}")

    async def start(self, snapshot) -> None:
        if self._task is not None:
            return
        await self.record("startup", **snapshot())

        async def watch():
            previous_hour = None
            while True:
                moment = datetime.fromtimestamp(self._clock()).astimezone()
                hour = moment.strftime("%Y-%m-%dT%H%z")
                if hour != previous_hour:
                    await self.record("heartbeat", **snapshot())
                    previous_hour = hour
                else:
                    try:
                        await asyncio.to_thread(self._rollover)
                    except Exception:
                        # record() emits a rate-limited, content-free error report.
                        await self.record("heartbeat", **snapshot())
                await asyncio.sleep(60)

        self._task = asyncio.create_task(watch(), name="alice-proactive-daily-log")

    def _rollover(self):
        with self._lock:
            self._ensure_day(datetime.fromtimestamp(self._clock()).astimezone())

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
            await self.record("shutdown")

    def delete_user(self, user_id: str, session_ids: set[str]) -> int:
        """Participate in scoped data cleanup, preserving unrelated process records."""
        with self._lock:
            if not self.path.exists():
                return 0
            self._ensure_day(datetime.fromtimestamp(self._clock()).astimezone())
            removed = 0
            def kept_lines():
                nonlocal removed
                with self.path.open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(row, dict):
                            continue
                        if row.get("session_id") in session_ids or (user_id and row.get("user_id") == user_id):
                            removed += 1
                        else:
                            yield line.rstrip("\n")
            self._replace(kept_lines())
            return removed
