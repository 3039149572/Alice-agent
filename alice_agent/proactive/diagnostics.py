from __future__ import annotations

from datetime import datetime, timedelta

from .models import UserAvailability
from .policies import LocalTimeWindow


REASONS = {
    "loop_disabled": "后台循环已关闭", "loop_stopped": "后台循环未运行",
    "proactive_disabled": "主动消息已关闭", "session_unregistered": "此会话尚未登记",
    "active_conversation": "仍在活跃聊天保护期", "outside_window": "不在允许发送的本地时段",
    "unanswered": "上次主动消息尚未收到回应", "session_inactive": "会话已超过七天未活跃",
    "daily_events_disabled": "每日事件已关闭", "events_not_generated": "今天的事件尚未生成",
    "no_candidate": "当前没有可分享的事件或到期待办", "attempt_limit": "今天的规划尝试已用完",
    "probability_silence": "今天的概率判断选择安静", "score_below_threshold": "候选评分未达到门槛",
    "daily_budget": "滚动24小时预算已用完", "global_cooldown": "全局发送冷却中",
    "user_cooldown": "当前用户发送冷却中", "same_topic_cooldown": "同话题冷却中",
}


def next_window_open(window: LocalTimeWindow, now: float) -> float:
    if window.contains(now):
        return now
    moment = datetime.fromtimestamp(now)
    opening = datetime.combine(moment.date(), window.start)
    if opening.timestamp() <= now:
        opening += timedelta(days=1)
    return opening.timestamp()


class ProactiveInspector:
    """Read existing data only: no tick, model, draw, action claim, or sender."""

    def __init__(self, loop):
        self.loop = loop

    async def inspect(self, session_id: str, user_id: str) -> dict:
        loop, blockers, conditions, times = self.loop, [], [], []
        config, now = loop._config, loop._clock.now()
        moment = datetime.fromtimestamp(now).astimezone()
        day = moment.date().isoformat()
        result = {
            "read_only": True, "local_time": moment.isoformat(), "timezone": moment.tzname(),
            "loop_enabled": config.agent_loop_enabled, "loop_running": loop.running,
            "proactive_enabled": config.proactive_enabled, "daily_events_enabled": config.daily_events_enabled,
            "send_window": [config.proactive_window_start, config.proactive_window_end],
            "candidate_source": None, "candidate_score": None, "daily_probability": None,
            "blockers": blockers, "next_conditions": conditions, "next_evaluable_at": None,
            "not_executed": ["event_generation", "new_probability_draw", "jev", "planner", "sender"],
        }
        if not config.agent_loop_enabled:
            blockers.append("loop_disabled")
        if not loop.running:
            blockers.append("loop_stopped")
        if not config.proactive_enabled:
            blockers.append("proactive_disabled")
        sessions = await loop._store.list_sessions()
        row = next((item for item in sessions if item["session_id"] == session_id and item["user_id"] == user_id), None)
        if row is None:
            blockers.append("session_unregistered")
            conditions.append("先在此私聊会话中正常交谈一次")
            return result
        last = row.get("last_interaction_at")
        if last is not None and now - float(last) < config.active_conversation_window:
            blockers.append("active_conversation")
            times.append(float(last) + config.active_conversation_window)
        due = [item for item in await loop._scheduler.due(now)
               if item.session_id == session_id and item.user_id == user_id]
        scheduled = due[0] if due else None
        state = await loop._states.get(session_id)
        daily = state.daily_events if state.daily_event_day == day else []
        daily_state = await loop._store.get_daily_proactive_state(session_id, day)
        cached = await loop._store.get_daily_proactive_decision(session_id, day)
        result["daily_probability"] = {key: cached.get(key) for key in ("eligible", "probability", "roll", "source")} if cached else None
        result["daily_attempts"] = int(daily_state.get("attempt_count", 0))
        result["daily_attempt_limit"] = config.proactive_daily_attempt_limit
        result["existing_daily_event_count"] = len(daily)
        stats = await loop._store.send_stats(user_id, None, now - 86400.0, session_id)
        result["budget_24h_used"], result["budget_24h_limit"] = int(stats["daily_count"] or 0), config.proactive_daily_budget
        if not scheduled:
            window = LocalTimeWindow(config.proactive_window_start, config.proactive_window_end)
            if not window.contains(now):
                blockers.append("outside_window")
                times.append(next_window_open(window, now))
            if last is not None and now - float(last) > 7 * 86400:
                blockers.append("session_inactive")
                conditions.append("需要新的正常会话输入")
            if last is not None and stats.get("session_last") is not None and float(last) <= stats["session_last"]:
                blockers.append("unanswered")
                conditions.append("先回应上次主动消息")
            if not config.daily_events_enabled:
                blockers.append("daily_events_disabled")
            elif state.daily_event_day != day:
                blockers.append("events_not_generated")
            if result["daily_attempts"] >= config.proactive_daily_attempt_limit:
                blockers.append("attempt_limit")
                conditions.append("下一本地日期重新评估日常分享")
            if cached and not cached.get("eligible"):
                blockers.append("probability_silence")
                conditions.append("下一本地日期重新抽签；本命令不会重抽")
        attempted = set(daily_state.get("attempted_event_ids", [])) | set(daily_state.get("delivered_event_ids", []))
        event = next((item for item in daily if str(item.get("event_id", "")) not in attempted), None)
        drives = await loop._drives.get(session_id, user_id)
        availability = UserAvailability(last_interaction_time=last, known_active_session=True)
        candidate = loop._evaluator.evaluate(drives, availability, now, scheduled, daily_event=event)
        from .models import CandidateActionType
        if candidate.action_type == CandidateActionType.DO_NOTHING:
            blockers.append("no_candidate")
        else:
            result["candidate_source"] = "scheduled_followup" if scheduled else "daily_event" if event else "unfinished_context"
            relation = await loop._relationships.get(user_id)
            score = loop._scorer.score(candidate, drives, relation,
                threshold_override=config.proactive_daily_score_threshold if event and not scheduled else None)
            result["candidate_score"] = round(score.score, 4)
            if score.decision != "allow":
                blockers.append("score_below_threshold")
            allowed, reason = await loop._cooldown.check(user_id, candidate.topic, now, session_id=session_id)
            if not allowed:
                blockers.append(reason)
        if result["budget_24h_used"] >= config.proactive_daily_budget and "daily_budget" not in blockers:
            blockers.append("daily_budget")
        if "daily_budget" in blockers:
            conditions.append("滚动24小时发送预算恢复后再评估")
        for field, code in (("global_last", "global_cooldown"), ("user_last", "user_cooldown")):
            sent_at = stats.get(field)
            if sent_at is not None and now < sent_at + config.proactive_min_interval:
                if code not in blockers:
                    blockers.append(code)
                times.append(sent_at + config.proactive_min_interval)
        result["blockers"] = list(dict.fromkeys(blockers))
        if times:
            result["next_evaluable_at"] = datetime.fromtimestamp(max(times)).astimezone().isoformat()
            conditions.append("该时间仅表示已知时间门槛；其余条件仍需通过")
        if not blockers:
            conditions.append("本地预筛可继续，仍需正常Tick的概率/模型/平台步骤；诊断不会发送")
        return result


def format_status(status: dict) -> str:
    blockers = status["blockers"]
    lines = ["Alice 主动消息诊断（只读）", f"本地时间：{status['local_time']}（{status['timezone']}）",
             "当前原因：" + ("；".join(REASONS.get(code, code) for code in blockers) if blockers else "本地预筛可继续"),
             f"候选来源：{status.get('candidate_source') or '暂无'}；评分：{status.get('candidate_score')}"]
    if status.get("daily_probability"):
        draw = status["daily_probability"]
        lines.append(f"今日已保存抽签：{'允许进一步规划' if draw.get('eligible') else '安静'}；概率 {draw.get('probability')}")
    if status.get("next_evaluable_at"):
        lines.append("已知时间门槛：" + status["next_evaluable_at"])
    lines.extend(status["next_conditions"])
    lines.append("未生成事件、未调用模型、未重抽签、未执行主动发送。")
    return "\n".join(lines)
