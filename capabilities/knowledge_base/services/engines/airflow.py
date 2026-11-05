#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""W1.4 Airflow 拉取适配器（REST v1，只读）。

与现有 OpenLineage **推送**分工：推送记录血缘/质量事件；这里拉 DAG/run/task
元数据做新鲜度与失败重跑视图。v1 不落库，调用方按需取。
"""
from __future__ import annotations

from typing import Any, Optional

import httpx


class AirflowApiError(Exception):
    """Airflow 调用失败（连接/认证/HTTP 非 2xx）。"""


class AirflowPullSource:
    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._transport = transport

    @staticmethod
    def _settings(cfg: dict[str, Any]) -> dict[str, Any]:
        url = str(cfg.get("url") or "").strip()
        if not url:
            host = str(cfg.get("host") or "127.0.0.1")
            port = int(cfg.get("port") or 8080)
            url = f"http://{host}:{port}"
        user = str(cfg.get("user") or cfg.get("username") or "")
        password = str(cfg.get("password") or "")
        token = str(cfg.get("token") or "")
        return {"url": url.rstrip("/"), "user": user, "password": password,
                "token": token}

    async def _get(self, cfg: dict[str, Any], path: str,
                   params: dict | None, timeout: float) -> dict:
        s = self._settings(cfg)
        headers = {"Authorization": f"Bearer {s['token']}"} if s["token"] else {}
        auth = (s["user"], s["password"]) if (s["user"] and not s["token"]) else None
        try:
            async with httpx.AsyncClient(
                base_url=s["url"], timeout=httpx.Timeout(timeout),
                transport=self._transport,
            ) as client:
                resp = await client.get(path, params=params, headers=headers, auth=auth)
        except Exception as exc:  # noqa: BLE001
            raise AirflowApiError(
                f"Airflow 连接失败：{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise AirflowApiError(
                f"Airflow 调用失败（HTTP {resp.status_code}）：{resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise AirflowApiError("Airflow 返回不是 JSON") from exc

    async def test_connection(self, cfg: dict[str, Any]) -> dict:
        try:
            await self._get(cfg, "/api/v1/health", None, 10.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)[:300]}
        return {"ok": True, "message": "连接成功"}

    async def list_dags(self, cfg: dict[str, Any], *, limit: int = 100,
                        offset: int = 0, timeout: float = 15.0) -> list[dict]:
        data = await self._get(cfg, "/api/v1/dags",
                               {"limit": limit, "offset": offset}, timeout)
        return [_dag(d) for d in data.get("dags") or []]

    async def list_runs(self, cfg: dict[str, Any], dag_id: str, *,
                        limit: int = 50, timeout: float = 15.0) -> list[dict]:
        data = await self._get(
            cfg, f"/api/v1/dags/{dag_id}/dagRuns",
            {"limit": limit, "order_by": "-execution_date"}, timeout)
        return [_run(r) for r in data.get("dag_runs") or []]

    async def list_tasks(self, cfg: dict[str, Any], dag_id: str, run_id: str, *,
                         timeout: float = 15.0) -> list[dict]:
        data = await self._get(
            cfg, f"/api/v1/dags/{dag_id}/dagRuns/{run_id}/taskInstances",
            {"limit": 200}, timeout)
        return [_task(t) for t in data.get("task_instances") or []]


def _dag(raw: dict) -> dict:
    return {
        "dag_id": raw.get("dag_id"),
        "is_paused": raw.get("is_paused"),
        "schedule": (raw.get("schedule_interval")
                     or raw.get("timetable_description")
                     or raw.get("timetable_summary")),
        "last_parsed_time": raw.get("last_parsed_time"),
        "owners": raw.get("owners") or [],
        "tags": raw.get("tags") or [],
    }


def _run(raw: dict) -> dict:
    return {
        "run_id": raw.get("dag_run_id") or raw.get("run_id"),
        "state": raw.get("state"),
        "execution_date": raw.get("execution_date"),
        "start_date": raw.get("start_date"),
        "end_date": raw.get("end_date"),
        "run_type": raw.get("run_type"),
    }


def _task(raw: dict) -> dict:
    return {
        "task_id": raw.get("task_id"),
        "state": raw.get("state"),
        "try_number": raw.get("try_number"),
        "start_date": raw.get("start_date"),
        "end_date": raw.get("end_date"),
        "duration": raw.get("duration"),
        "operator": raw.get("operator"),
    }


__all__ = ["AirflowPullSource", "AirflowApiError"]
