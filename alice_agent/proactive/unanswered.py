from __future__ import annotations


async def unanswered_status(store, config, session_id, last_interaction_at, now):
    """Read-only contact policy: finite waiting plus a rolling unanswered cap."""
    window = config.proactive_unanswered_window_seconds
    limit = config.proactive_unanswered_limit
    result = {
        "allowed": True, "has_unanswered": False, "recent_unanswered_count": 0,
        "unanswered_limit": limit, "window_seconds": window,
        "wait_seconds": config.proactive_unanswered_wait_seconds,
        "reason_code": None, "reason_codes": [], "next_evaluable_at": None,
        "last_proactive_at": None,
        "history_source": "none",
    }
    if last_interaction_at is None:
        return result
    stats = await store.unanswered_stats(session_id, last_interaction_at, now - window, limit)
    last_sent = stats["last_sent_at"]
    result["last_proactive_at"] = last_sent
    result["history_source"] = "send_history" if last_sent is not None else "none"
    if last_sent is None or last_sent < last_interaction_at:
        return result
    count, times = stats["recent_count"], list(stats["recent_send_times"])
    # An old installation may have pruned the send row but retained its timestamp.
    # Count that known send without inventing any unavailable older history.
    if count == 0 and last_sent > now - window:
        count, times = 1, [last_sent]
        result["history_source"] = "last_timestamp_only"
    result.update(has_unanswered=True, recent_unanswered_count=count)
    deadlines = []
    wait_until = last_sent + config.proactive_unanswered_wait_seconds
    if now < wait_until:
        result["reason_codes"].append("unanswered_wait")
        deadlines.append(wait_until)
    if count >= limit:
        # The Nth newest send must leave the window before another is allowed.
        # This also works when an operator lowers the limit below the old count.
        limit_until = times[limit - 1] + window
        if now < limit_until:
            result["reason_codes"].append("unanswered_limit")
            deadlines.append(limit_until)
    if deadlines:
        result.update(allowed=False, reason_code=result["reason_codes"][-1],
                      next_evaluable_at=max(deadlines))
    return result
