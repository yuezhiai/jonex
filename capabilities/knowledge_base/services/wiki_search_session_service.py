"""Live Wiki searches: reuse the regular pipeline, keep only public progress in Redis."""
import asyncio
import json
import uuid

from jonex_core.common.cache import get_redis_client
from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.common.wiki_search_session import SESSION_TTL, session_key


class WikiSearchSessionService:
    _tasks: dict[str, asyncio.Task] = {}

    @staticmethod
    async def _owned(tenant_id, user_id, session_id):
        redis = get_redis_client()
        key = session_key(tenant_id, session_id)
        raw = await redis.hgetall(key)
        state = {(k.decode() if isinstance(k, bytes) else k):
                 (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}
        if not state or state.get("owner") != str(user_id):
            raise ResourceNotFoundError(message="Wiki search session not found")
        return redis, key, state

    async def start(self, tenant_id, user_id, payload, search, trace_id):
        session_id = uuid.uuid4().hex
        key = session_key(tenant_id, session_id)
        redis = get_redis_client()
        await redis.hset(key, mapping={"owner": str(user_id), "status": "running"})
        await redis.expire(key, SESSION_TTL)

        async def run():
            try:
                result = await search.search_jonex_wiki(
                    tenant_id, user_id, payload, trace_id=trace_id, progress_session=session_id)
                # Cancellation wins a race with the final result, without reviving a session.
                await redis.eval("""
                  if redis.call('HGET', KEYS[1], 'status') == 'running' then
                    redis.call('HSET', KEYS[1], 'result', ARGV[1], 'status', 'done')
                  end
                  return 1
                """, 1, key, json.dumps(result, ensure_ascii=False))
            except asyncio.CancelledError:
                pass
            except Exception:
                await redis.eval("""
                  if redis.call('HGET', KEYS[1], 'status') == 'running' then
                    redis.call('HSET', KEYS[1], 'status', 'failed', 'error', ARGV[1])
                  end
                  return 1
                """, 1, key, "Wiki search failed; please retry")
            finally:
                self._tasks.pop(key, None)

        self._tasks[key] = asyncio.create_task(run())
        return {"session_id": session_id}

    async def poll(self, tenant_id, user_id, session_id):
        _, _, state = await self._owned(tenant_id, user_id, session_id)
        events = []
        for field, value in state.items():
            if field.startswith("progress:"):
                events.extend(dict(e, kb_id=field[9:]) for e in json.loads(value).get("events", []))
        events.sort(key=lambda e: str(e.get("started_at") or ""))
        return {"status": state["status"], "events": events[-96:],
                "result": json.loads(state["result"]) if state.get("result") else None,
                "error": state.get("error")}

    async def cancel(self, tenant_id, user_id, session_id):
        redis, key, state = await self._owned(tenant_id, user_id, session_id)
        if state["status"] != "running":
            return {"status": state["status"]}
        await redis.hset(key, "status", "cancelled")
        from jonex_core.capability.atomic.jonex_wiki.client import RemoteJonexWikiClient
        client = RemoteJonexWikiClient()
        try:
            for field, query_id in state.items():
                if field.startswith("query:"):
                    await client._invoke("cancel_query", {"query_id": query_id}, tenant_id, field[6:])
        finally:
            await client._client.aclose()
            task = self._tasks.get(key)
            if task is not None:
                task.cancel()
        return {"status": "cancelled"}


async def resolve_wiki_evidence(tenant_id, data):
    from jonex_core.capability.atomic.jonex_wiki.client import RemoteJonexWikiClient
    from jonex_core.common.database import get_db_session
    from ..repository.document_repository import KnowledgeDocumentRepository
    # Validate the platform's current document visibility too; the engine checks its own copy.
    client = RemoteJonexWikiClient()
    try:
        result = await client._invoke("resolve_evidence", {"query_id": data["query_id"], "handle": data["handle"]},
                                      tenant_id, data["knowledge_base_id"])
        async with get_db_session() as session:
            docs = await KnowledgeDocumentRepository(session).get_by_ids([result["document_id"]], tenant_id)
        if not docs or docs[0].knowledge_base_id != data["knowledge_base_id"]:
            raise ResourceNotFoundError(message="Evidence source not found")
        return result
    finally:
        await client._client.aclose()
