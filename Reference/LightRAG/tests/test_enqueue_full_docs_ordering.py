#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""[jonex] `apipeline_enqueue_documents` 的写入顺序回归。

事故（2026-10-19，引擎灌库）：一份 68 条 chunk 的文档读回少 1 条
（锚 54），一份 93 条的少 1 条（锚 1），编译任务因此判 `[A18] 读回校验失败`。
LightRAG 日志给出了直接证据：

    Deleted inconsistent entry: doc-… (…|chunk=54|…)
    Deleted inconsistent entry: doc-… (…|chunk=1|…)

成因是提交 `b570691f fix:孤儿 track_id` 把 `doc_status.insert_if_absent()`
提到 `full_docs.upsert()` **之前**：`/documents/text` 每条都会起一个后台任务，
并发的处理循环在「status 已可见、正文还没落」的窗口里快照到这条记录，
按 `inconsistent_docs` 分支把它删掉 —— 上传接口仍回 `success`，chunk 静默丢失。

本文件锁住顺序契约：**doc_status 对处理循环可见的时刻，full_docs 必须已有正文**。
"""
import asyncio

from lightrag import LightRAG


class _FullDocs:
    def __init__(self):
        self.data = {}
        self.upserts = 0

    async def upsert(self, docs):
        self.data.update(docs)
        self.upserts += 1

    async def index_done_callback(self):
        pass


class _DocStatus:
    """`insert_if_absent` 里模拟「状态一旦可见，处理循环立刻按 full_docs 校验」。"""

    def __init__(self, full_docs):
        self.full_docs = full_docs
        self.inconsistent = []

    async def insert_if_absent(self, new_docs):
        for doc_id in new_docs:
            if doc_id not in self.full_docs.data:
                self.inconsistent.append(doc_id)
        return set(new_docs)

    async def get_by_id(self, doc_id):
        return None

    async def upsert(self, docs):
        raise AssertionError("doc_status 不应在 insert_if_absent 之后再次 upsert")


async def _run():
    rag = object.__new__(LightRAG)
    full_docs = _FullDocs()
    doc_status = _DocStatus(full_docs)
    rag.full_docs = full_docs
    rag.doc_status = doc_status

    await rag.apipeline_enqueue_documents(input=["甲" * 20])

    assert doc_status.inconsistent == [], (
        "doc_status 已对处理循环可见，但 full_docs 还没有正文 —— "
        f"这些记录会被当成 inconsistent 删除: {doc_status.inconsistent}"
    )
    assert full_docs.upserts == 1


def test_status_never_visible_before_full_docs_content():
    asyncio.run(_run())


if __name__ == "__main__":
    test_status_never_visible_before_full_docs_content()
    print("ALL PASSED")
