#!/usr/bin/python3
# -*- coding:utf-8 -*-
"""
Jonex 平台 - Milvus 向量数据库工具模块

提供向量数据库的连接、集合管理、向量插入和检索等功能
"""

from typing import List, Dict, Any, Optional, Tuple
from functools import lru_cache
from contextlib import asynccontextmanager

from .config import get_config
from .logger import get_logger
from .exceptions import DatabaseError, InvalidParameterError, ResourceNotFoundError
from jonex_core.common.i18n import translate

logger = get_logger(__name__)

try:
    from pymilvus import (
        connections,
        utility,
        Collection,
        CollectionSchema,
        FieldSchema,
        DataType,
        MilvusException,
    )
    MILVUS_AVAILABLE = True
except ImportError:
    MILVUS_AVAILABLE = False
    # 提供占位符，让类定义能通过语法检查（实例化时会在 __init__ 里抛出 DatabaseError）
    connections = None  # type: ignore
    utility = None  # type: ignore
    Collection = None  # type: ignore
    CollectionSchema = None  # type: ignore
    FieldSchema = None  # type: ignore
    DataType = None  # type: ignore
    MilvusException = Exception  # type: ignore


# ==================== 数据类型映射 ====================
if MILVUS_AVAILABLE:
    _MILVUS_TYPE_MAP = {
        "BOOL": DataType.BOOL,
        "INT8": DataType.INT8,
        "INT16": DataType.INT16,
        "INT32": DataType.INT32,
        "INT64": DataType.INT64,
        "FLOAT": DataType.FLOAT,
        "DOUBLE": DataType.DOUBLE,
        "VARCHAR": DataType.VARCHAR,
        "JSON": DataType.JSON,
        "BINARY_VECTOR": DataType.BINARY_VECTOR,
        "FLOAT_VECTOR": DataType.FLOAT_VECTOR,
    }
else:
    _MILVUS_TYPE_MAP: Dict[str, Any] = {}


class MilvusClient:
    """Milvus 向量数据库客户端"""

    def __init__(self, alias: Optional[str] = None):
        """
        初始化 Milvus 客户端

        Args:
            alias: 连接别名，默认为配置中的 MILVUS_ALIAS
        """
        if not MILVUS_AVAILABLE:
            logger.warning("pymilvus 未安装，向量数据库功能不可用")
            raise DatabaseError("pymilvus 未安装，请执行: pip install pymilvus>=2.3.0")

        config = get_config()
        self.alias = alias or config.MILVUS_ALIAS
        self.host = config.MILVUS_HOST
        self.port = config.MILVUS_PORT
        self.user = config.MILVUS_USER
        self.password = config.MILVUS_PASSWORD
        self.connect_timeout = config.MILVUS_CONNECT_TIMEOUT

        self._connected = False
        self._default_dim = config.MILVUS_DEFAULT_DIM
        self._default_metric = config.MILVUS_DEFAULT_METRIC
        self._default_index = config.MILVUS_DEFAULT_INDEX

    def connect(self) -> None:
        """建立 Milvus 连接"""
        if self._connected:
            return

        try:
            connect_params = {
                "host": self.host,
                "port": self.port,
                "timeout": self.connect_timeout,
            }

            if self.user and self.password:
                connect_params["user"] = self.user
                connect_params["password"] = self.password

            connections.connect(alias=self.alias, **connect_params)
            self._connected = True
            logger.info(f"✅ Milvus 连接成功: {self.host}:{self.port}")
        except MilvusException as e:
            raise DatabaseError(f"Milvus 连接失败: {str(e)}") from e

    def disconnect(self) -> None:
        """关闭 Milvus 连接"""
        if self._connected:
            try:
                connections.disconnect(self.alias)
                self._connected = False
                logger.info("✅ Milvus 连接已关闭")
            except MilvusException as e:
                logger.warning(f"Milvus 关闭连接异常: {e}")

    def check_health(self) -> bool:
        """
        检查 Milvus 健康状态

        Returns:
            bool: 健康状态
        """
        try:
            if not self._connected:
                self.connect()
            version = utility.get_server_version()
            logger.info(f"Milvus 服务正常，版本: {version}")
            return True
        except Exception as e:
            logger.error(f"Milvus 健康检查失败: {e}")
            return False

    # ==================== 集合管理 ====================

    def has_collection(self, collection_name: str) -> bool:
        """
        检查集合是否存在

        Args:
            collection_name: 集合名称

        Returns:
            bool: 是否存在
        """
        if not self._connected:
            self.connect()
        return utility.has_collection(collection_name, using=self.alias)

    def create_collection(
        self,
        collection_name: str,
        fields: List[Dict[str, Any]],
        description: str = "",
        auto_id: bool = False,
        enable_dynamic_field: bool = True,
    ) -> Collection:
        """
        创建向量集合

        Args:
            collection_name: 集合名称
            fields: 字段定义列表，每个字段包含: name, type, params, description 等
            description: 集合描述
            auto_id: 是否自动生成主键
            enable_dynamic_field: 是否启用动态字段

        Returns:
            Collection: 创建的集合对象

        Example:
            fields = [
                {"name": "id", "type": "INT64", "is_primary": True, "auto_id": False},
                {"name": "vector", "type": "FLOAT_VECTOR", "params": {"dim": 1536}},
                {"name": "content", "type": "VARCHAR", "params": {"max_length": 65535}},
                {"name": "metadata", "type": "JSON"},
            ]
            client.create_collection("my_collection", fields)
        """
        if self.has_collection(collection_name):
            logger.warning(f"集合已存在: {collection_name}")
            return Collection(collection_name, using=self.alias)

        schema_fields = []
        for field in fields:
            field_type = _MILVUS_TYPE_MAP.get(field["type"].upper())
            if not field_type:
                raise InvalidParameterError(message=translate("err.vector.unsupported_field_type", params={"type": field['type']}, fallback=f"不支持的字段类型: {field['type']}"))

            field_params = field.get("params", {})
            if field_type == DataType.FLOAT_VECTOR and "dim" not in field_params:
                field_params["dim"] = self._default_dim

            schema_fields.append(
                FieldSchema(
                    name=field["name"],
                    dtype=field_type,
                    description=field.get("description", ""),
                    is_primary=field.get("is_primary", False),
                    auto_id=field.get("auto_id", False),
                    **field_params,
                )
            )

        schema = CollectionSchema(
            fields=schema_fields,
            description=description,
            enable_dynamic_field=enable_dynamic_field,
        )

        collection = Collection(
            name=collection_name,
            schema=schema,
            using=self.alias,
            auto_id=auto_id,
        )

        logger.info(f"✅ 集合创建成功: {collection_name}")
        return collection

    def get_collection(self, collection_name: str) -> Collection:
        """
        获取集合对象

        Args:
            collection_name: 集合名称

        Returns:
            Collection: 集合对象
        """
        if not self.has_collection(collection_name):
            raise ResourceNotFoundError(message=translate("err.vector.collection_not_found", params={"collection_name": collection_name}, fallback=f"集合不存在: {collection_name}"))
        return Collection(collection_name, using=self.alias)

    def drop_collection(self, collection_name: str) -> None:
        """
        删除集合

        Args:
            collection_name: 集合名称
        """
        if self.has_collection(collection_name):
            utility.drop_collection(collection_name, using=self.alias)
            logger.info(f"✅ 集合删除成功: {collection_name}")

    def list_collections(self) -> List[str]:
        """
        列出所有集合

        Returns:
            List[str]: 集合名称列表
        """
        if not self._connected:
            self.connect()
        return utility.list_collections(using=self.alias)

    # ==================== 索引管理 ====================

    def create_index(
        self,
        collection_name: str,
        field_name: str,
        index_type: Optional[str] = None,
        metric_type: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        创建向量索引

        Args:
            collection_name: 集合名称
            field_name: 向量字段名
            index_type: 索引类型（IVF_FLAT, IVF_SQ8, IVF_PQ, HNSW, FLAT 等）
            metric_type: 距离度量类型（COSINE, L2, IP）
            params: 索引参数（如 nlist, M, efConstruction 等）
        """
        collection = self.get_collection(collection_name)

        index_type = index_type or self._default_index
        metric_type = metric_type or self._default_metric
        params = params or {}

        # 根据索引类型设置默认参数
        if index_type == "IVF_FLAT" and "nlist" not in params:
            params["nlist"] = 1024
        elif index_type == "HNSW" and "M" not in params:
            params["M"] = 16
            params["efConstruction"] = 256

        index_params = {
            "metric_type": metric_type,
            "index_type": index_type,
            "params": params,
        }

        collection.create_index(field_name, index_params)
        logger.info(f"✅ 索引创建成功: {collection_name}.{field_name} ({index_type})")

    def load_collection(self, collection_name: str) -> None:
        """
        将集合加载到内存（查询前必须调用）

        Args:
            collection_name: 集合名称
        """
        collection = self.get_collection(collection_name)
        collection.load()
        logger.debug(f"集合已加载: {collection_name}")

    def release_collection(self, collection_name: str) -> None:
        """
        从内存释放集合

        Args:
            collection_name: 集合名称
        """
        collection = self.get_collection(collection_name)
        collection.release()
        logger.debug(f"集合已释放: {collection_name}")

    # ==================== 数据操作 ====================

    def insert(self, collection_name: str, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        插入向量数据

        Args:
            collection_name: 集合名称
            data: 数据列表，每项为字段名到值的映射

        Returns:
            Dict: 插入结果信息

        Example:
            data = [
                {"id": 1, "vector": [0.1, 0.2, ...], "content": "测试文本", "metadata": {"source": "doc1"}},
                {"id": 2, "vector": [0.3, 0.4, ...], "content": "测试文本2", "metadata": {"source": "doc2"}},
            ]
            client.insert("my_collection", data)
        """
        collection = self.get_collection(collection_name)

        # 转换为按字段分组的列表格式（Milvus API 要求）
        field_names = collection.schema.names
        insert_data = []
        for field in collection.schema.fields:
            if field.auto_id:
                continue
            field_values = [item.get(field.name) for item in data]
            insert_data.append(field_values)

        result = collection.insert(insert_data)

        logger.info(f"✅ 数据插入成功: {collection_name}, 数量: {len(data)}")
        return {
            "insert_count": result.insert_count,
            "primary_keys": result.primary_keys,
        }

    def delete(self, collection_name: str, expr: str) -> int:
        """
        按表达式删除数据

        Args:
            collection_name: 集合名称
            expr: 删除表达式，如 "id in [1, 2, 3]"

        Returns:
            int: 删除数量
        """
        collection = self.get_collection(collection_name)
        result = collection.delete(expr)
        logger.info(f"✅ 数据删除成功: {collection_name}, 数量: {result.delete_count}")
        return result.delete_count

    def search(
        self,
        collection_name: str,
        query_vectors: List[List[float]],
        vector_field: str = "vector",
        filter_expr: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
        limit: int = 10,
        params: Optional[Dict[str, Any]] = None,
    ) -> List[List[Dict[str, Any]]]:
        """
        向量相似度搜索

        Args:
            collection_name: 集合名称
            query_vectors: 查询向量列表
            vector_field: 向量字段名
            filter_expr: 过滤表达式
            output_fields: 返回字段列表
            limit: 返回结果数量
            params: 搜索参数（如 ef, nprobe 等）

        Returns:
            List[List[Dict]]: 搜索结果，每个查询对应一个结果列表，
            每个结果包含 id, score, 以及 output_fields 指定的字段

        Example:
            results = client.search(
                "my_collection",
                [[0.1, 0.2, ...]],
                filter_expr="status == 'active'",
                output_fields=["id", "content", "metadata"],
                limit=5,
            )
        """
        collection = self.get_collection(collection_name)

        # 确保集合已加载
        try:
            collection.load()
        except MilvusException:
            # 可能已加载
            pass

        params = params or {"nprobe": 16}

        results = collection.search(
            data=query_vectors,
            anns_field=vector_field,
            param=params,
            limit=limit,
            expr=filter_expr,
            output_fields=output_fields or [],
        )

        # 转换为更友好的格式
        formatted_results = []
        for hits in results:
            hit_list = []
            for hit in hits:
                result_item = {
                    "id": hit.id,
                    "score": hit.score,
                }
                if output_fields:
                    result_item.update({field: hit.entity.get(field) for field in output_fields})
                hit_list.append(result_item)
            formatted_results.append(hit_list)

        return formatted_results

    def query(
        self,
        collection_name: str,
        expr: str,
        output_fields: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        标量查询（基于非向量字段）

        Args:
            collection_name: 集合名称
            expr: 查询表达式，如 "id > 100"
            output_fields: 返回字段列表
            limit: 返回结果数量限制

        Returns:
            List[Dict]: 查询结果列表
        """
        collection = self.get_collection(collection_name)

        results = collection.query(
            expr=expr,
            output_fields=output_fields or [],
            limit=limit,
        )

        return results

    def upsert(self, collection_name: str, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        更新或插入数据（Upsert）

        Args:
            collection_name: 集合名称
            data: 数据列表

        Returns:
            Dict: 操作结果
        """
        # Milvus 目前没有原生 upsert，这里先删除再插入
        # 注意：需要主键字段
        collection = self.get_collection(collection_name)
        primary_key = None

        for field in collection.schema.fields:
            if field.is_primary:
                primary_key = field.name
                break

        if not primary_key:
            raise InvalidParameterError(message=translate("err.vector.upsert_requires_pk", fallback="Upsert 需要集合有主键字段"))

        # 提取所有主键并删除旧数据
        ids = [item[primary_key] for item in data]
        if ids:
            id_str = ", ".join(str(i) for i in ids)
            self.delete(collection_name, f"{primary_key} in [{id_str}]")

        # 插入新数据
        return self.insert(collection_name, data)


# ==================== 全局实例 ====================
_global_milvus_client: Optional[MilvusClient] = None


def get_milvus_client() -> MilvusClient:
    """
    获取全局 Milvus 客户端实例（单例）

    Returns:
        MilvusClient: 客户端实例
    """
    global _global_milvus_client
    if _global_milvus_client is None:
        _global_milvus_client = MilvusClient()
    return _global_milvus_client


def check_milvus_health() -> bool:
    """
    检查 Milvus 健康状态（便捷函数）

    Returns:
        bool: 健康状态
    """
    if not MILVUS_AVAILABLE:
        return False
    try:
        client = MilvusClient()
        return client.check_health()
    except Exception:
        return False


@asynccontextmanager
async def milvus_context():
    """
    Milvus 异步上下文管理器

    用法:
        async with milvus_context() as client:
            results = client.search(...)
    """
    client = get_milvus_client()
    try:
        client.connect()
        yield client
    finally:
        # 保持连接以供复用，不主动关闭
        pass
