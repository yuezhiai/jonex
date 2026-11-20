"""Live Wiki searches: reuse the regular pipeline, keep only public progress in Redis."""
import asyncio
import json
import logging
import uuid

from jonex_core.common.cache import get_redis_client
from jonex_core.common.exceptions import ResourceNotFoundError
from jonex_core.common.wiki_search_session import SESSION_TTL, session_key

logger = logging.getLogger(__name__)

# [jonex] 落历史快照的进度轨迹体积闸。引擎侧已封顶 96 条 + 每条 3 个预览，
# 但检索历史是**长期**存储（reasoning JSONB 随快照一起读），这里再收一次：
# 只留最近 48 条、每条预览 ≤3 个、单字段 ≤240 字符、摘录 ≤180 字符。
# 实测一次 Wiki 检索 6~20 条事件，正常不会触发截断。
_TRACE_MAX_EVENTS = 48
_TRACE_MAX_PREVIEWS = 3
_TRACE_MAX_FIELD_CHARS = 240
_TRACE_MAX_EXCERPT_CHARS = 180


def _bounded_preview(preview) -> dict | None:
    if not isinstance(preview, dict):
        return None
    out = {}
    for key in ("title", "path", "excerpt"):
        value = preview.get(key)
        if not isinstance(value, str) or not value:
            continue
        limit = _TRACE_MAX_EXCERPT_CHARS if key == "excerpt" else _TRACE_MAX_FIELD_CHARS
        out[key] = value[:limit]
    return out or None


def _bounded_event(event: dict, kb_id: str) -> dict:
    """公开进度事件 → 可长期保存的轨迹条目（字段白名单 + 体积上限）。"""
    out: dict = {"kb_id": kb_id, "seq": event.get("seq")}
    for key in ("type", "tool", "title", "operation_id", "status", "result_summary"):
        value = event.get(key)
        if isinstance(value, str) and value:
            out[key] = value[:_TRACE_MAX_FIELD_CHARS]
    if event.get("started_at"):
        out["started_at"] = str(event["started_at"])[:64]
    if isinstance(event.get("duration_ms"), int):
        out["duration_ms"] = event["duration_ms"]
    details = event.get("details")
    if isinstance(details, dict):
        bounded = {}
        for key in ("query", "table_id", "operation", "input", "output"):
            value = details.get(key)
            if isinstance(value, str) and value:
                bounded[key] = value[:1200 if key == 'output' else 500 if key == 'input' else _TRACE_MAX_FIELD_CHARS]
        if details.get("resource_kind") in {"article", "source", "table"}:
            bounded["resource_kind"] = details["resource_kind"]
        paths = details.get("paths")
        if isinstance(paths, list):
            paths = [p[:_TRACE_MAX_FIELD_CHARS] for p in paths[:_TRACE_MAX_PREVIEWS] if isinstance(p, str) and p]
            if paths:
                bounded["paths"] = paths
        span = details.get("range")
        if isinstance(span, list) and len(span) == 2 and all(isinstance(n, int) for n in span):
            bounded["range"] = span
        previews = [_bounded_preview(p) for p in (details.get("previews") or [])]
        previews = [p for p in previews[: _TRACE_MAX_PREVIEWS] if p]
        if previews:
            bounded["previews"] = previews
        if bounded:
            out["details"] = bounded
    return out


def _flatten_progress(state: dict) -> list[dict]:
    """Redis hash → 按时间排好的进度事件（各 KB 的 progress:<kb_id> 摊平）。"""
    events: list[dict] = []
    for field, value in state.items():
        if not field.startswith("progress:"):
            continue
        try:
            snapshot = json.loads(value)
        except (TypeError, ValueError):
            continue
        kb_id = field[9:]
        events.extend(dict(e, kb_id=kb_id) for e in snapshot.get("events", []) if isinstance(e, dict))
    events.sort(key=lambda e: (str(e.get("started_at") or ""), str(e.get("kb_id") or ""), e.get("seq") or 0))
    return events


async def collect_progress(tenant_id: str, session_id: str) -> list[dict]:
    """[jonex] 读回本次检索的公开进度轨迹（落历史快照用）。

    与 SSE 轮询同源：都读 `progress:<kb_id>`。区别是这里做长期保存的体积收口，
    且**任何异常都不外抛** —— 轨迹是展示信息，读不到不该让检索失败。
    """
    try:
        redis = get_redis_client()
        raw = await redis.hgetall(session_key(tenant_id, session_id))
        state = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                 for k, v in raw.items()}
        events = _flatten_progress(state)
        if not events:
            return []
        trimmed = [_bounded_event(event, str(event.get("kb_id") or "")) for event in events[-_TRACE_MAX_EVENTS:]]
        return [event for event in trimmed if event.get("tool") or event.get("type") in ("synthesis", "model")]
    except Exception:
        logger.warning("Unable to read Wiki progress trace", exc_info=True)
        return []


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
        redis, key, state = await self._owned(tenant_id, user_id, session_id)
        # 有人还在看（首连或离开后重连）就续期：长检索不该在轮询中途被 TTL 清掉。
        await redis.expire(key, SESSION_TTL)
        # 与 collect_progress 共用摊平/排序：轮询与落库看到的顺序必须一致，
        # 否则历史回放的步骤顺序会和当时流式看到的不一样。
        events = _flatten_progress(state)
        # Tool previews and completed answers can contain source text. Recheck
        # access against the pinned Bundle's complete document scope, including
        # documents that were read but did not become final citations.
        required_kbs = {event['kb_id'] for event in events
                        if isinstance(event.get('details'), dict) and
                        (event['details'].get('input') or event['details'].get('output'))}
        if state['status'] == 'done':
            try:
                outcomes = json.loads(state.get('result') or '{}').get('query_outcomes') or []
            except (TypeError, ValueError, AttributeError):
                outcomes = []
            required_kbs.update(item['kb_id'] for item in outcomes if isinstance(item, dict) and
                                isinstance(item.get('kb_id'), str) and item['kb_id'] and
                                item.get('stop_reason') not in ('empty_kb', 'engine_error'))
        if required_kbs or state['status'] == 'done':
            await self._assert_session_scopes(tenant_id, user_id, state, required_kbs)
        return {"status": state["status"], "events": events[-96:],
                "result": json.loads(state["result"]) if state.get("result") else None,
                "error": state.get("error")}

    async def _assert_session_scopes(self, tenant_id, user_id, state, required_kbs=frozenset()):
        from types import SimpleNamespace
        from jonex_core.common.database import get_db_session
        from .search_history_service import SearchHistoryService

        scopes = []
        for field in state:
            if not field.startswith('query:'):
                continue
            kb_id = field[6:]
            try:
                source_ids = json.loads(state.get('scope:' + kb_id, 'null'))
            except (TypeError, ValueError):
                source_ids = None
            if (not isinstance(source_ids, list) or len(source_ids) > 10000 or
                    any(not isinstance(value, str) or not value for value in source_ids)):
                raise ResourceNotFoundError(message='检索来源范围无法核验')
            scopes.append((kb_id, source_ids))
        if not required_kbs.issubset({kb_id for kb_id, _ in scopes}):
            raise ResourceNotFoundError(message='检索来源范围无法核验')
        if not scopes:
            if state.get('status') == 'done':
                try:
                    outcomes = json.loads(state.get('result') or '{}').get('query_outcomes') or []
                except (TypeError, ValueError, AttributeError):
                    outcomes = []
                if outcomes and all(isinstance(item, dict) and
                                    item.get('stop_reason') in ('empty_kb', 'engine_error') for item in outcomes):
                    return  # No source-bearing engine query was started.
            raise ResourceNotFoundError(message='检索来源范围无法核验')
        checker = SearchHistoryService()
        async with get_db_session() as session:
            for kb_id, source_ids in scopes:
                snapshot = SimpleNamespace(
                    knowledge_base_id=kb_id, references=[],
                    extra_metadata={'knowledge_base_ids': [kb_id], 'query_outcomes': [
                        {'kb_id': kb_id, 'source_document_ids': source_ids}]},
                )
                await checker._assert_history_readable(session, tenant_id, user_id, snapshot)

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
        if (not docs or docs[0].knowledge_base_id != data["knowledge_base_id"]
                or getattr(docs[0], 'status', None) in {'archived', 'deleting', 'deleted'}):
            raise ResourceNotFoundError(message="Evidence source not found")
        return result
    finally:
        await client._client.aclose()
