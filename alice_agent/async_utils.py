from __future__ import annotations

import asyncio


async def finish_before_cancelling(awaitable):
    """Finish a bounded local commit before propagating caller cancellation.

    Cancelling an asyncio.to_thread waiter cannot stop its SQLite transaction.
    Its owning task must retain its locks and finish invalidation/recovery
    bookkeeping before another request is allowed through.
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()  # Retrieve any failure; durable jobs remain retryable.
        raise asyncio.CancelledError
    return task.result()
