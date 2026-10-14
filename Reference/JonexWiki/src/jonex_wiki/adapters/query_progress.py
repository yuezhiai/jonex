"""Bounded progress snapshots: two writes/second while running, plus the final snapshot."""
import asyncio
import logging
from datetime import datetime, timezone
from contextlib import suppress
from sqlalchemy import update
from jonex_wiki.storage.models import QueryTaskRow

log = logging.getLogger(__name__)


class QueryProgressAdapter:
    def __init__(self, session_factory, tenant_id, query_id):
        self.factory, self.tenant, self.query_id = session_factory, tenant_id, query_id
        self.events, self.seq, self.dirty = [], 0, False

    def record(self, event):
        self.seq += 1
        fields = ("type", "title", "tool", "started_at", "duration_ms")
        row = {k: event[k][:300] if isinstance(event[k], str) else event[k]
               for k in fields if event.get(k) is not None}
        row.setdefault("started_at", datetime.now(timezone.utc).isoformat())
        # Details are produced from bounded retrieval inputs/results, never from model spans.
        if event.get("tool") and isinstance(event.get("details"), dict):
            row["details"] = event["details"]
        row["seq"] = self.seq
        self.events = (self.events + [row])[-96:]
        self.dirty = True

    async def _flush(self):
        while True:
            await asyncio.sleep(.5)
            await self._flush_once()

    async def _flush_once(self):
        if not self.dirty:
            return
        self.dirty = False
        snapshot = {"seq": self.seq, "events": list(self.events)}
        try:
            async with self.factory() as session:
                await session.execute(update(QueryTaskRow).where(
                    QueryTaskRow.tenant_id == self.tenant,
                    QueryTaskRow.query_id == self.query_id,
                    QueryTaskRow.status == "running",
                ).values(progress=snapshot))
                await session.commit()
        except Exception:
            log.warning("Unable to persist Wiki query progress", exc_info=True)

    async def __aenter__(self):
        self.writer = asyncio.create_task(self._flush())
        return self

    async def __aexit__(self, *args):
        self.writer.cancel()
        with suppress(asyncio.CancelledError):
            await self.writer
        if not args or args[0] is None:
            # Retain the last short tool call even when it finished before the next tick.
            self.dirty = bool(self.events)
            try:
                await asyncio.wait_for(self._flush_once(), timeout=1)
            except asyncio.TimeoutError:
                log.warning("Final Wiki progress snapshot timed out")
