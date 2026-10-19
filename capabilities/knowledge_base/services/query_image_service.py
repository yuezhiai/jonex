#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""以图问知识：查询图片的落盘、查看定位与释放清理。

设计：docs/knowledge-base/image-query-search-execution-plan.md §4-B10 / §4-B12

三件事，都挂在「历史」上：

| 时机 | 做什么 |
|---|---|
| 保存历史（`bind_for_history`） | 把识别阶段暂存在 Redis 的字节写进对象存储 + 插图片行 + 写关联行 |
| 删历史（`release_for_histories`） | 逐张判「还有没有未删除的历史引用它」，没有就删对象 + 软删行 |
| 每小时兜底（`sweep_orphans`） | 补上面两条 best-effort 失败留下的漏 |

⚠️ **事务边界与对象存储 IO 的次序是写死的**，不要"顺手"把它们并进一个事务：
    短事务 ①（插图片行）→ **事务外** `put_bytes` → 短事务 ②（写关联行 + 续期）→ **事务外**删 Redis 暂存。
    理由：对象存储是网络调用（COS 可能几百 ms），把它圈进事务等于让数据库连接一直
    被占着；而 `get_db_session()` 是「干净退出即 commit」，两个事务块就是两个 `async with`。
    任何一步中断都能自愈 —— **图片行先于对象存在**，中断留下的「无关联图片行」（不管对象
    写没写成）都会在宽限期后被 `sweep_orphans` 收走（删不存在的对象视为成功）。
    所以**不会出现没有数据库记录的孤儿对象**。

⚠️ **与 `bind_for_history` 的并发窗口是有意接受的降级**：
    删历史 A 与把同一图关联到新历史 B 同时发生时，可能 B 的关联行刚写入、A 的条件软删
    就已判定「无引用」。结果是 **B 少一张图**（列表与查看都要求图片 `is_deleted=0`，
    死引用会被过滤）——**不会显示坏图、不会越权**。一期不加行锁。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from typing import Any, Optional, Sequence

from jonex_core.common.cache import CacheUtil
from jonex_core.common.config import get_config
from jonex_core.common.database import get_db_session
from jonex_core.common.exceptions import InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate
from jonex_core.common.object_storage import (
    asset_ext_for_mime,
    build_query_image_key,
    get_object_storage,
    get_object_storage_for,
)
from jonex_core.common.tenant import require_tenant

from ..repository import KnowledgeQueryImageRepository
from .image_query_service import pending_key

logger = logging.getLogger(__name__)


def storage_backend_name() -> str:
    """落盘时的**全局**后端名，写进图片行；读取时按行里的它选实例。

    有意用全局 env 而不是「上次用的那个」：新图总是写进当前配置的后端，
    老图各自记着自己那一次的后端（平台换后端时两边并存，谁也不读错地方）。
    """
    return (os.getenv("OBJECT_STORAGE_BACKEND") or "local").strip().lower()


def _decode_payload_bytes(payload: dict) -> Optional[bytes]:
    try:
        return base64.b64decode(payload["base64"], validate=True)
    except (binascii.Error, ValueError, KeyError, TypeError):
        return None


class QueryImageService:
    """查询图片的持久化与生命周期。"""

    def __init__(self) -> None:
        self._config = get_config()

    # ── 保存历史：落盘 + 关联（四步） ────────────────

    async def bind_for_history(
        self,
        tenant_id: str,
        user_id: str,
        history_id: str,
        image_ids: Optional[Sequence[str]],
    ) -> list[dict]:
        """把 ``image_ids`` 关联到刚保存的历史上，返回**成功关联**的图片引用列表。

        逐张判定来源：
        1. 本人已有该 id 的持久化图 → **复用**（事务 ② 里条件续期；影响 0 行说明
           恰好被并发清理收走了 → 这张跳过）；
        2. 否则读 Redis 暂存（键含 tenant + user，别人的 id 读不到）；
           - 暂存里 sha 已是本人的持久化图 → 复用**已有行**的 id，删暂存；
           - 未命中 → **新落盘**：事务 ① 插行 → 事务外 `put_bytes`；
        3. 插行撞唯一索引（并发同图）→ 改用已存在的行 id，**不重复落盘**；
        4. 暂存不存在 / 任一步失败 → **该张跳过并打 warning**。

        ⚠️ **整条链上任何一张失败都不影响历史保存本身**——
        历史文本是主数据，不能因为图片丢失而失败。返回的列表里没有它，
        `query_images` 就是空的或短的，其余一切照常。

        ⚠️ 返回的顺序是**用户附图顺序**，靠 `position` 排回来，不是靠 id 比对：
        走了「复用已有行」这条路的图，最终 id 与原入参 id 不同（比如暂存里那张
        和库里已有那张是同内容），按 id 过滤会把它们整批漏掉。
        """
        tenant_id = require_tenant(tenant_id)
        if not image_ids:
            return []

        cfg = self._config
        ordered = self._dedupe(image_ids, int(cfg.IMAGE_QUERY_MAX_IMAGES))
        if not ordered:
            return []

        # 每张的处置计划：position 保序，row_id 是最终落库的那一行
        plan: list[dict] = []
        pending_to_clear: list[str] = []

        async with get_db_session() as session:
            repo = KnowledgeQueryImageRepository(session)
            for position, image_id in enumerate(ordered):
                row = await repo.get_owned(tenant_id, user_id, image_id)
                if row is not None:
                    plan.append({"position": position, "row_id": row.id, "source": "reused"})
                    continue

                payload = await self._read_pending(tenant_id, user_id, image_id)
                if payload is None:
                    logger.warning(
                        "以图问知识：图片既不在库也无暂存，跳过关联 tenant=%s image_id=%s"
                        "（暂存过期 / 非本人 / 前端传了别人的 id）",
                        tenant_id, image_id,
                    )
                    continue

                existing = await repo.find_by_sha(tenant_id, user_id, payload["sha256"])
                if existing is not None:
                    # 同用户同图已有落盘：复用它的行，不再写一遍对象
                    pending_to_clear.append(image_id)
                    plan.append(
                        {"position": position, "row_id": existing.id, "source": "existing_sha"}
                    )
                    logger.info(
                        "以图问知识：同图已有落盘，复用 tenant=%s sha=%s 复用 id=%s",
                        tenant_id, payload["sha256"][:12], existing.id,
                    )
                    continue

                plan.append(
                    {
                        "position": position,
                        "row_id": image_id,
                        # 暂存键用的是**原入参 id**；插行撞唯一索引时 row_id 会变，
                        # 但暂存键永远认这个 —— 所以它必须单独记一份
                        "pending_id": image_id,
                        "source": "new",
                        "payload": payload,
                    }
                )

        # ── 短事务 ①：插图片行（还没有关联，属"无引用"）──
        to_persist = [item for item in plan if item["source"] == "new"]
        if to_persist:
            async with get_db_session() as session:
                repo = KnowledgeQueryImageRepository(session)
                for item in to_persist:
                    payload = item["payload"]
                    try:
                        row, created = await repo.insert_if_absent(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            image_id=item["row_id"],
                            storage_backend=storage_backend_name(),
                            storage_key=build_query_image_key(
                                tenant_id,
                                item["row_id"],
                                asset_ext_for_mime(payload["mime_type"]) or "png",
                            ),
                            mime_type=payload["mime_type"],
                            file_size=int(payload["file_size"]),
                            sha256=payload["sha256"],
                            recognized_query=payload.get("recognized_query"),
                            description=payload.get("description"),
                            ocr_text=payload.get("ocr_text"),
                            entities=list(payload.get("entities") or []),
                            prompt_version=payload.get("prompt_version"),
                            vlm_model=payload.get("model"),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "以图问知识：插入图片行失败，跳过该张 tenant=%s image_id=%s err=%s",
                            tenant_id, item["row_id"], exc,
                        )
                        item["source"] = "failed"
                        continue
                    item["row_id"] = row.id
                    item["needs_upload"] = created
                    # 落盘时**直接用这一行自己的 storage_key**，不要再算一遍：
                    # `build_query_image_key` 内含 `datetime.now()` 的 `{yyyymm}`，
                    # 两次调用跨过月初/月末就会算出不同的键 —— 对象写到了 A 键、
                    # 数据库记的是 B 键，A 那个对象从此没有任何记录，永远收不走。
                    item["storage_key"] = row.storage_key
                    # 暂存那批字节已经被读出来了，无论落成没落成都该清掉（TTL 是兜底）
                    pending_to_clear.append(item["pending_id"])
                    if not created:
                        # 撞唯一索引（并发同图）：改用已有行，不重复落盘
                        item["source"] = "existing_sha"
                        logger.info(
                            "以图问知识：并发同图，复用已存在行 tenant=%s sha=%s id=%s",
                            tenant_id, payload["sha256"][:12], row.id,
                        )

        # ── 事务外：put_bytes（网络调用，绝不圈进事务）──
        storage = get_object_storage()
        for item in plan:
            if not item.get("needs_upload"):
                continue
            payload = item["payload"]
            try:
                raw = _decode_payload_bytes(payload)
                if raw is None:
                    raise ValueError("暂存里的 base64 解不开")
                await storage.put_bytes(
                    item["storage_key"],
                    raw,
                    content_type=payload["mime_type"],
                )
                item["uploaded"] = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "以图问知识：查询图片落盘失败，该张不关联 tenant=%s image_id=%s err=%s",
                    tenant_id, item["row_id"], exc,
                )
                item["uploaded"] = False

        # ── 短事务 ②：写关联行 + 续期复用行；落盘失败的行在这里软删 ──
        async with get_db_session() as session:
            repo = KnowledgeQueryImageRepository(session)

            for item in plan:
                if item.get("needs_upload") and not item.get("uploaded"):
                    await repo.soft_delete_unlinked(item["row_id"], tenant_id, user_id)
                    item["source"] = "failed"

            final_ids: list[str] = []
            for item in sorted(plan, key=lambda x: x["position"]):
                if item["source"] == "failed":
                    continue
                if item["source"] == "new":
                    final_ids.append(item["row_id"])
                    continue
                # 复用路径：条件续期；影响 0 行 = 恰好被并发清理收走了 → 不写关联（免留死引用）
                if await repo.touch(item["row_id"], tenant_id) > 0:
                    final_ids.append(item["row_id"])
                else:
                    logger.info(
                        "以图问知识：图片已被并发清理，跳过关联 tenant=%s image_id=%s",
                        tenant_id, item["row_id"],
                    )

            if final_ids:
                await repo.add_links(tenant_id, user_id, history_id, final_ids)

            refs: list[dict] = []
            for image_id in final_ids:
                row = await repo.get_owned(tenant_id, user_id, image_id)
                if row is not None:
                    refs.append(row.to_ref())

        # ── 事务外：删掉暂存（不管关联成没成，那批字节已经用过了）──
        for pending_id in dict.fromkeys(pending_to_clear):
            await self._clear_pending(tenant_id, user_id, pending_id)

        logger.info(
            "以图问知识：历史关联图片 tenant=%s history=%s 传入=%d 成功=%d",
            tenant_id, history_id, len(ordered), len(refs),
        )
        return refs

    # ── 删历史：释放无引用的图片 ───────────────────

    async def release_for_histories(
        self,
        tenant_id: str,
        user_id: str,
        history_ids: Optional[Sequence[str]],
    ) -> int:
        """这些历史被删掉后，清理因此失去全部引用的图片。返回清理张数。

        **best-effort**：失败只打 warning，**绝不影响历史删除本身成功**。
        漏掉的由 `sweep_orphans` 补。

        「判断无引用」与「标记删除」合成**一条条件 UPDATE**（仓储
        `release_if_unreferenced` 里的 `NOT EXISTS` 子查询），把与
        `bind_for_history` 的竞争窗口压到最小；剩余窗口见模块 docstring。
        """
        tenant_id = require_tenant(tenant_id)
        ids = [h for h in (history_ids or []) if h]
        if not ids:
            return 0

        try:
            async with get_db_session() as session:
                repo = KnowledgeQueryImageRepository(session)
                image_ids = await repo.image_ids_of_histories(tenant_id, user_id, ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("以图问知识：查询待释放图片失败（不影响历史删除）: %s", exc)
            return 0

        if not image_ids:
            return 0

        released: list[tuple[str, str, str]] = []   # (image_id, backend, key)
        for image_id in set(image_ids):
            try:
                async with get_db_session() as session:
                    repo = KnowledgeQueryImageRepository(session)
                    loc = await repo.release_if_unreferenced(tenant_id, user_id, image_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "以图问知识：释放图片失败，留给兜底扫描 tenant=%s image_id=%s err=%s",
                    tenant_id, image_id, exc,
                )
                continue
            if loc:
                released.append((image_id, loc[0], loc[1]))

        if not released:
            return 0

        # 删对象（失败只 warning：行已软删，由 sweep 按 object_deleted=0 补删）
        deleted: list[str] = []
        for image_id, backend, key in released:
            if await self._delete_object(backend, key, tenant_id, image_id):
                deleted.append(image_id)

        if deleted:
            try:
                async with get_db_session() as session:
                    repo = KnowledgeQueryImageRepository(session)
                    for image_id in deleted:
                        await repo.mark_object_deleted(image_id, tenant_id)
                    await repo.delete_links_for_images(deleted)
            except Exception as exc:  # noqa: BLE001
                logger.warning("以图问知识：标记对象已删除失败（兜底扫描会补）: %s", exc)

        logger.info(
            "以图问知识：释放无引用图片 tenant=%s 历史=%d 张 释放=%d 删对象成功=%d",
            tenant_id, len(ids), len(released), len(deleted),
        )
        return len(released)

    # ── 查看 ─────────────────────────────────────

    async def check_viewable(self, tenant_id: str, user_id: str, image_id: str) -> None:
        """校验该图可被此人查看；不可见一律 ``not_found``（不暴露"存在但无权"）。

        两重判据：
        1. 图归本人（tenant + user + 未删除）；
        2. **仍被本人至少一条未删除历史引用** —— 兜底清理有延迟，这条保证
           「删掉历史 → 图片立刻不可见」，而不是等对象真被删掉。
        """
        await self._load_viewable(tenant_id, user_id, image_id)

    async def get_location(self, tenant_id: str, user_id: str, image_id: str) -> dict:
        """返回图片位置信息 ``{storage_backend, storage_key, mime_type, presigned_url}``。

        ``presigned_url`` 由**该行自己的后端**签发（`get_object_storage_for`）——
        平台换过后端时老图仍指向老后端，按全局 env 取会读错地方。
        local 后端恒返回空串，调用方回 `FileResponse`。
        """
        row = await self._load_viewable(tenant_id, user_id, image_id)
        presigned = ""
        try:
            storage = get_object_storage_for(row.storage_backend)
            presigned = await storage.presigned_url(
                row.storage_key,
                tenant_id,
                expires=int(self._config.IMAGE_QUERY_VIEW_TOKEN_TTL),
            ) or ""
        except Exception as exc:  # noqa: BLE001
            # 签不出来不是"图不存在"：让调用方回退到本地文件读取（local 后端本来就空串）
            logger.warning(
                "以图问知识：图片预签名失败，回退本地读取 backend=%s: %s",
                row.storage_backend, exc,
            )
        return {
            "storage_backend": row.storage_backend,
            "storage_key": row.storage_key,
            "mime_type": row.mime_type,
            "presigned_url": presigned,
        }

    async def _load_viewable(self, tenant_id: str, user_id: str, image_id: str):
        tenant_id = require_tenant(tenant_id)
        # ⚠️ 用 `ResourceNotFoundError`（1002 / HTTP 404）而不是参数错误：
        # 这是「这张图你看不到」，不是「你传的参数不合法」。同租户他人、跨租户、
        # 已清理、没被历史引用 —— 一律同一个 404，不区分 403/404、不暴露存在性。
        not_found = ResourceNotFoundError(
            message=translate(
                "err.query_image.not_found",
                fallback="图片不存在或已清理",
            )
        )
        # 匿名身份：同租户所有人共用一个"本人"坑位，直接按不存在处理
        if not user_id or user_id == "anonymous":
            raise not_found
        async with get_db_session() as session:
            repo = KnowledgeQueryImageRepository(session)
            row = await repo.get_owned(tenant_id, user_id, image_id)
            if row is None:
                raise not_found
            if not await repo.is_referenced_by_user(tenant_id, user_id, image_id):
                raise not_found
        return row

    # ── 兜底清理 ───────────────────────────────────────

    async def sweep_orphans(self, limit: int = 200) -> dict[str, Any]:
        """补同步清理漏掉的：① 无未删除引用的老图；② 已软删但对象还在的图。

        **多副本并发跑是安全的**：删对象与软删都幂等，不需要选主。
        每轮最多 ``limit`` 条（一次全清会让一轮循环拖很久）。
        """
        cfg = self._config
        result = {"orphans": 0, "objects": 0}

        # ① 无引用且过了宽限期 → 删对象 + 软删行
        try:
            async with get_db_session() as session:
                repo = KnowledgeQueryImageRepository(session)
                orphans = await repo.list_orphans(
                    int(cfg.IMAGE_QUERY_ORPHAN_GRACE_MINUTES), limit
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("以图问知识：兜底扫描（无引用）查询失败: %s", exc)
            orphans = []

        # ⚠️ 顺序**必须先条件软删、再删对象**（与 `release_if_unreferenced` 同一口径）：
        # 「扫出来无引用」与「对象已删」之间如果新建立了引用，先删对象就会让那条历史
        # 指向一张对象已消失的图（历史里那张图直接不见）。先条件软删则这次竞争会撞在
        # `NOT EXISTS` 上、影响 0 行，本轮不动它 —— 最坏也只是"少收一张，下轮再来"。
        purged: list[str] = []
        row_tenant: dict[str, str] = {}   # id → tenant，回写时带上租户条件
        for row in orphans:
            try:
                async with get_db_session() as session:
                    repo = KnowledgeQueryImageRepository(session)
                    loc = await repo.purge_if_unreferenced(row["id"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("以图问知识：兜底扫描软删失败，下一轮重试: %s", exc)
                continue
            if not loc:
                # 期间被引用上了（或已被别处收走）→ 不动它
                continue
            # 行已是软删状态；对象删成功才置 object_deleted=1，
            # 删失败就留着 0 —— 下一轮由「待删对象」那一类兜住（自愈）
            if await self._delete_object(loc[0], loc[1], row["tenant_id"], row["id"]):
                purged.append(row["id"])
                row_tenant[row["id"]] = row["tenant_id"]
        if purged:
            try:
                async with get_db_session() as session:
                    repo = KnowledgeQueryImageRepository(session)
                    for image_id in purged:
                        # 行已经在上面的条件软删里置好了 is_deleted/purged_at，
                        # 这里只补「对象已删」这一笔（别再调 purge_row 覆盖 purged_at）
                        await repo.mark_object_deleted(image_id, row_tenant.get(image_id))
                    # 顺带清掉关联行：它们不参与任何可见性判断，清掉只为控制表体积
                    await repo.delete_links_for_images(purged)
                result["orphans"] = len(purged)
            except Exception as exc:  # noqa: BLE001
                logger.warning("以图问知识：兜底扫描（无引用）回写失败: %s", exc)

        # ② 已软删但对象还没删掉 → 补删
        try:
            async with get_db_session() as session:
                repo = KnowledgeQueryImageRepository(session)
                pending = await repo.list_objects_pending_delete(limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("以图问知识：兜底扫描（待删对象）查询失败: %s", exc)
            pending = []

        done: list[str] = []
        for row in pending:
            # 这里没有 tenant 上下文（跨租户维护查询），只用 id 记账
            # 这个查询是跨租户维护的（没有 tenant 入参），但**行里存着 tenant**，
            # 回写时带上它 —— 纵深防御，让跨租户误写在 SQL 层不可能发生
            if await self._delete_object(
                row["storage_backend"], row["storage_key"], "", row["id"]
            ):
                done.append(row["id"])
                row_tenant[row["id"]] = row["tenant_id"]
        if done:
            try:
                async with get_db_session() as session:
                    repo = KnowledgeQueryImageRepository(session)
                    for image_id in done:
                        await repo.mark_object_deleted(image_id, row_tenant.get(image_id))
                result["objects"] = len(done)
            except Exception as exc:  # noqa: BLE001
                logger.warning("以图问知识：兜底扫描（待删对象）回写失败: %s", exc)

        return result

    # ── 内部工具 ──────────────────────────────────────────────

    @staticmethod
    def _dedupe(image_ids: Sequence[str], limit: int) -> list[str]:
        """去重（保序）并截到上限。Service 层再做一次是因为前端可能传超。"""
        out: list[str] = []
        seen: set[str] = set()
        for image_id in image_ids or []:
            if not image_id or image_id in seen:
                continue
            seen.add(image_id)
            out.append(image_id)
            if len(out) >= limit:
                break
        return out

    async def _read_pending(self, tenant_id: str, user_id: str, image_id: str) -> Optional[dict]:
        try:
            raw = await CacheUtil.get(pending_key(tenant_id, user_id, image_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("以图问知识：暂存读取失败，该张不落盘: %s", exc)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except (ValueError, AttributeError):
            logger.warning("以图问知识：暂存内容不是合法 JSON，该张跳过 image_id=%s", image_id)
            return None
        if not isinstance(data, dict) or not data.get("base64"):
            return None
        return data

    async def _clear_pending(self, tenant_id: str, user_id: str, image_id: str) -> None:
        try:
            await CacheUtil.delete(pending_key(tenant_id, user_id, image_id))
        except Exception as exc:  # noqa: BLE001
            # 删不掉不是问题：TTL 到了自己就没了
            logger.warning("以图问知识：暂存删除失败（TTL 会兜底）: %s", exc)

    async def _delete_object(
        self, backend: Optional[str], key: Optional[str], tenant_id: str, image_id: str
    ) -> bool:
        """删对象。**删不存在的对象视为成功**（中断可能根本没写成功）。"""
        if not key:
            return True
        try:
            storage = get_object_storage_for(backend)
            # 服务端永久凭证删除：COS 预签名用的 STS 临时密钥只授权 Put/Get，没有 Delete
            await storage.delete(key)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "以图问知识：删除对象失败，留给下一轮兜底 tenant=%s image_id=%s key=%s err=%s",
                tenant_id, image_id, key, exc,
            )
            return False


__all__ = ["QueryImageService", "storage_backend_name"]
