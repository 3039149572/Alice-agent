from __future__ import annotations


def empty_daily_state() -> dict:
    return {"attempt_count": 0, "attempted_event_ids": [],
            "delivered_event_ids": [], "event_attempts": {}}


def reserve_attempt(state, event_id, limit, *, now=0.0, claim_id=None, lease_until=0.0):
    """Quota counts requests; a retry releases the event, never refunds quota."""
    attempts = state.setdefault("event_attempts", {})
    previous = attempts.get(event_id, {})
    if (state["attempt_count"] >= limit or event_id in state["attempted_event_ids"]
            or event_id in state["delivered_event_ids"]
            or float(previous.get("next_retry_at", 0)) > now):
        return False
    state["attempt_count"] += 1
    state["attempted_event_ids"].append(event_id)
    if claim_id is not None:
        attempts[event_id] = {"status": "pending", "claim_id": claim_id,
                              "failure_count": int(previous.get("failure_count", 0)),
                              "next_retry_at": 0.0, "lease_until": lease_until}
    return True


def finish_attempt(state, event_id, claim_id, *, now, retryable=False,
                   error_kind=None, retry_base=60.0, retry_max=900.0):
    attempt = state.setdefault("event_attempts", {}).get(event_id)
    if not attempt or attempt.get("claim_id") != claim_id or attempt.get("status") != "pending":
        return {"changed": False}
    retryable = retryable and event_id not in state["delivered_event_ids"]
    attempt.update(status="retry" if retryable else "finished", lease_until=0.0)
    if retryable:
        attempt["failure_count"] = int(attempt.get("failure_count", 0)) + 1
        delay = min(retry_max, retry_base * 2 ** min(attempt["failure_count"] - 1, 20))
        attempt.update(next_retry_at=now + delay, error_kind=str(error_kind or "UnknownError")[:100])
        state["attempted_event_ids"] = [item for item in state["attempted_event_ids"] if item != event_id]
    return {"changed": True, **attempt}


def recover_attempts(state, now, *, retry_base=60.0, retry_max=900.0):
    recovered = []
    for event_id, attempt in list(state.setdefault("event_attempts", {}).items()):
        if attempt.get("status") == "pending" and float(attempt.get("lease_until", 0)) <= now:
            result = finish_attempt(state, event_id, attempt["claim_id"], now=now,
                                    retryable=True, error_kind="AttemptLeaseExpired",
                                    retry_base=retry_base, retry_max=retry_max)
            recovered.append({"event_id": event_id, **result})
    return recovered


def select_daily_event(events, state, now, limit):
    """Pure selection shared by the running loop and read-only inspector."""
    if state["attempt_count"] >= limit:
        return None, "attempt_limit", None
    if not events:
        return None, "daily_events_empty", None
    delivered = set(state["delivered_event_ids"])
    attempted = set(state["attempted_event_ids"])
    waits, pending, valid = [], False, []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id", ""))
        if not event_id:
            continue
        valid.append(event_id)
        attempt = state.get("event_attempts", {}).get(event_id, {})
        if event_id in delivered:
            continue
        if attempt.get("status") == "pending":
            pending = True
            waits.append(float(attempt.get("lease_until", now)))
            continue
        if event_id in attempted:
            continue
        retry_at = float(attempt.get("next_retry_at", 0))
        if retry_at > now:
            waits.append(retry_at)
            continue
        return event, None, None
    if waits:
        return None, "attempt_in_progress" if pending else "retry_wait", min(waits)
    if valid and all(event_id in delivered for event_id in valid):
        return None, "daily_events_delivered", None
    return None, "daily_events_attempted" if valid else "daily_events_invalid", None
