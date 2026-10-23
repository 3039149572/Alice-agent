from __future__ import annotations

import asyncio
import secrets
import time
import weakref
from collections import OrderedDict
from contextlib import asynccontextmanager

from .governance import MemoryScope
from ..ports.logging import NullLogger
from ..async_utils import finish_before_cancelling


_HELP = (
    "仅管理你自己的记忆：\n"
    "/alice-data list [页码]\n/alice-data journal [页码]\n/alice-data show 记忆ID\n"
    "/alice-data correct 记忆ID 更正后的明确自述\n"
    "/alice-data delete 记忆ID\n/alice-data clear-memory\n"
    "删除须在5分钟内用 /alice-data confirm 确认码 确认。\n"
    "纠错/删除会清空相关会话的近期上下文及衍生缓存，防止旧内容再次使用。"
    "迁移备份、平台聊天记录和外部日志需另行管理；SQLite删除不承诺磁盘物理擦除。"
)


class MemoryDataService:
    """Self-service governance plus opt-in retention, independent of diagnostics.

    User gates serialize data actions with generation and actual delivery. They
    are reentrant for the same task; arrival registration remains non-blocking
    so response merging and interruption still work.
    """

    def __init__(self, port, metadata, *, invalidate_context=None, logger=None,
                 journal_days=0, memory_days=0, inactive_fact_days=0,
                 maintenance_extra=None, clock=time.time, token_clock=time.monotonic):
        self._port, self._metadata = port, metadata
        self._invalidate = invalidate_context
        self._logger = logger or NullLogger()
        self._retention = {"journal_days": journal_days, "memory_days": memory_days,
                           "inactive_fact_days": inactive_fact_days}
        self._extra = maintenance_extra
        self._clock, self._token_clock = clock, token_clock
        self._locks = weakref.WeakValueDictionary()
        self._owners = {}
        self._epochs = {}
        self._pending = OrderedDict()
        self._maintenance_task = None
        self._last_prune = float("-inf")
        self.delivery_recovery = None

    @asynccontextmanager
    async def guard(self, user_id, *, recover_jobs=True):
        task = asyncio.current_task()
        if self._owners.get(user_id) is task:
            yield
            return
        async with self._lock_for(user_id):
            self._owners[user_id] = task
            try:
                if recover_jobs:
                    await self.recover(user_id)
                yield
            finally:
                self._owners.pop(user_id, None)

    def _lock_for(self, user_id):
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def invalidate_user(self, user_id):
        self._epochs[user_id] = self._epochs.get(user_id, 0) + 1

    def stamp(self, event):
        event.metadata.setdefault("_alice_data_epoch", self._epochs.get(event.user_id, 0))

    def is_current(self, event):
        current = self._epochs.get(event.user_id, 0)
        return event.metadata.get("_alice_data_epoch", current) == current

    async def _finish_job(self, job):
        await self._metadata.delete_many(job["memory_ids"])
        if self._invalidate is not None and job["session_ids"]:
            await self._invalidate(job)
        await self._port.manage("finish", job["id"])

    async def recover(self, user_id=None):
        if self.delivery_recovery is not None:
            await self.delivery_recovery(user_id)
        for job in await self._port.manage("jobs"):
            if user_id is None or job.get("user_id") == user_id:
                await self._finish_job(job)

    async def command(self, scope: MemoryScope, text: str, *, trace_id=None):
        return await finish_before_cancelling(self._command(scope, text, trace_id=trace_id))

    async def _command(self, scope: MemoryScope, text: str, *, trace_id=None):
        parts = text.strip().split(maxsplit=2)
        action = parts[0].lower() if parts else "help"
        async with self.guard(scope.user_id, recover_jobs=False):
            try:
                await self.recover(scope.user_id)
                if action in {"list", "journal"}:
                    if len(parts) > 2:
                        raise ValueError("list accepts only a page number")
                    page = int(parts[1]) if len(parts) == 2 else 1
                    if not 1 <= page <= 10000:
                        raise ValueError("invalid page")
                    result = await self._port.manage(action, scope, page)
                    if action == "journal":
                        lines = [f"你的原始轮次共{result['total']}条，第{page}页："]
                        lines.extend(f"用户：{x['user_text'][:500]}\n回复：{(x['assistant_text'] or '')[:500]}" for x in result["items"])
                        return "\n".join(lines)
                    lines = [f"你的当前记忆共{result['total']}条，第{page}页："]
                    lines.extend(f"{x['id']}：{x['content'][:100]}" for x in result["items"])
                    return "\n".join(lines)
                if action == "show" and len(parts) == 2:
                    result = await self._port.manage("show", scope, parts[1])
                    item = result["item"]
                    lines = [f"{item['id']}\n{item['content'][:2000]}"]
                    lines.extend(f"版本{x['version']} / {x['status']}：{x['value']}" for x in result["versions"])
                    lines.extend(f"来源：{x['evidence'][:300]}" for x in result["sources"])
                    lines.extend(f"更正{x['revision']}：{x['old_content'][:300]} → {x['new_content'][:300]}" for x in result["edits"])
                    return "\n".join(lines)
                if action == "correct" and len(parts) == 3:
                    result = await self._port.manage("correct", scope, parts[1], parts[2], trace_id or secrets.token_hex(16), self._clock())
                    self.invalidate_user(scope.user_id)
                    if result["job"]:
                        await self._finish_job(result["job"])
                    return f"已更正，当前记忆ID：{result['memory_id']}。旧版本作为历史保留，可用delete删除整条事实。"
                if (action == "delete" and len(parts) == 2) or (action == "clear-memory" and len(parts) == 1):
                    memory_id = parts[1] if action == "delete" else None
                    preview = await self._port.manage("preview", scope, memory_id)
                    token = secrets.token_hex(4)
                    now = self._token_clock()
                    self._pending = OrderedDict((k, v) for k, v in self._pending.items() if v[0] > now)
                    self._pending[(scope.user_id, scope.session_id)] = (now + 300, token, memory_id, preview["fingerprint"])
                    while len(self._pending) > 1024:
                        self._pending.popitem(last=False)
                    target = "本范围全部记忆和原始轮次" if memory_id is None else "此记忆（事实含全部版本）和对应原始轮次"
                    return (f"将删除{target}，涉及{preview['count']}条记忆；清空相关近期上下文、衍生缓存和本插件诊断日志。"
                            f"关系数值和角色规则保留，迁移备份及平台聊天记录保留。\n确认：/alice-data confirm {token}")
                if action == "confirm" and len(parts) == 2:
                    key = (scope.user_id, scope.session_id)
                    pending = self._pending.get(key)
                    if not pending or pending[0] <= self._token_clock() or not secrets.compare_digest(pending[1], parts[1]):
                        return "确认码无效、已用或过期，请重新发起删除。"
                    del self._pending[key]
                    job = await self._port.manage("delete", scope, pending[2], pending[3])
                    self.invalidate_user(scope.user_id)
                    await self._finish_job(job)
                    return "已删除范围内记忆、对应来源和索引，并清理相关上下文。迁移备份及平台聊天记录仍保留。"
                return _HELP
            except KeyError:
                return "未找到属于你当前范围的记忆。"
            except (ValueError, TypeError):
                return "参数无效或数据已变化。事实纠错须针对当前版本，使用同类型的一条明确自述；删除请重新预览。\n" + _HELP
            except Exception:
                self._logger.exception("Alice memory data action needs recovery")
                return "操作未全部完成，已保存的清理任务会重试；暂不确认删除完成。"

    async def maintain(self, *, force=False):
        return await finish_before_cancelling(self._maintain(force=force))

    async def _maintain(self, *, force=False):
        if self.delivery_recovery is not None:
            await self.delivery_recovery()
        # Recovery is retried every minute, retention at most hourly. This task
        # starts even when agent_loop_enabled/test_log_enabled are false.
        for job in await self._port.manage("jobs"):
            user = job.get("user_id") or ""
            async with self._lock_for(user):
                live = {item["id"]: item for item in await self._port.manage("jobs")}
                if job["id"] in live:
                    await self._finish_job(live[job["id"]])
        now = self._clock()
        if force or now - self._last_prune >= 3600:
            if any(value > 0 for value in self._retention.values()):
                pinned = await self._metadata.pinned_ids()
                result = await self._port.manage("prune", now, **self._retention, pinned_ids=pinned)
                if result["job"]:
                    await self._finish_job(result["job"])
            if self._extra is not None:
                await self._extra(now)
            self._last_prune = now

    async def start(self):
        if self._maintenance_task is not None:
            return
        async def run():
            while True:
                try:
                    await self.maintain()
                except Exception:
                    self._logger.exception("Alice data maintenance will retry")
                await asyncio.sleep(60)
        try:
            await self.maintain(force=True)
        except Exception:
            self._logger.exception("Alice data startup maintenance will retry")
        self._maintenance_task = asyncio.create_task(run(), name="alice-data-maintenance")

    async def close(self):
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
            self._maintenance_task = None
