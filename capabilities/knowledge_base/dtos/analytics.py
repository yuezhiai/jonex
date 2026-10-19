"""数据运营（Data Operations）响应 DTO。

这些 DTO 是 5 个数据运营聚合接口（overview / data-assets / knowledge-assets /
search-analytics / abnormal-assets）返回结构的代码化定义，与 `AnalyticsService`
返回的 dict 字段一一对应。
字段即**前后端契约**：改名 / 删除 / 换语义都必须与前端 `Do*` 类型同步改，
否则前端静默拿到 `undefined`，而接口本身不报错。

二期新增字段（相对一期）在注释里都标了 `[二期]`，便于前端对照增量。
"""

from typing import Dict, List, Optional

try:
    from pydantic.v1 import BaseModel, Field
except ImportError:
    from pydantic import BaseModel, Field


class TrendPoint(BaseModel):
    date: str
    count: int


class AssetCategoryCount(BaseModel):
    category: str
    # [二期] 前端口径的资产类型（表格：spreadsheet → table），前端可直接用
    asset_type: Optional[str] = None
    count: int


class CitedAssetItem(BaseModel):
    asset_id: str
    file_name: str
    mime_type: Optional[str] = None
    cite_count: int
    # 所属知识库。前端「前往知识库」要跳 /apps/core-business/domain-knowledge/{kb_id}。
    # 文档被删/跨租户时富化不到，允许为空 —— 前端据此禁用按钮而不是跳到空白页
    kb_id: Optional[str] = None
    # [二期] 所属知识库名称（原型表格列）
    kb_name: Optional[str] = None
    # [二期] 资产类型（前端口径 7 类）
    asset_type: Optional[str] = None
    # [二期] 最近一次被引用时间（窗口内）
    last_cited_at: Optional[str] = None


class UnusedAssetItem(BaseModel):
    """长期未使用资产的一项。

    ⚠️ **判据是两半取交集**：`created_at`（上传时间）早于「今天 − `unused_threshold_days`」
    **且**  `last_cited_at` 为空或早于同一个阈值。只判后者会让「今天刚上传、一次没被
    引用过」的文档当场进榜 —— 那不是「长期未使用」，是「还没来得及用」。
    两个时间字段都在这里透出，前端才能让判据在表格上自洽。
    """

    asset_id: str
    file_name: str
    # None = 从未被引用过（不是「取不到」，前端要渲染「从未使用」）
    last_cited_at: Optional[str] = None
    # [二期] 资产类型（前端口径 7 类）
    asset_type: Optional[str] = None
    # [二期] 所属知识库，供「前往知识库」跳转
    kb_id: Optional[str] = None
    # 所属知识库名称；富化不到（库已删）时回落 `kb_id`（与「高频引用资产」同口径）
    kb_name: Optional[str] = None
    # [二期] 文档最近更新时间
    updated_at: Optional[str] = None
    # 上传时间（= 年龄判据的依据，语义同 `AssetDetail.uploaded_at`）。
    # 仅看 `updated_at` 分不清「老资产重新解析过」与「刚上传的新资产」
    created_at: Optional[str] = None


class ProcessingTaskItem(BaseModel):
    task_id: str
    asset_id: str
    file_name: str
    task_type: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    # [二期] 资产类型（前端口径 7 类）
    asset_type: Optional[str] = None


class AbnormalAssetItem(BaseModel):
    asset_id: str
    file_name: str
    task_type: str
    status: str
    error: Optional[str] = None
    # 一期字段，语义 = occurred_at（保留，别删）
    last_failed_at: Optional[str] = None

    # [二期] 资产类型（前端口径 7 类）
    asset_type: Optional[str] = None
    # [二期] 所属知识库名称（原型表格列）
    kb_name: Optional[str] = None
    # [二期] 是否已处置：同一 (资产, 阶段) 之后有更新的成功任务
    handled: bool = False
    # [二期] 该 (资产, 阶段) 的失败次数（列表被截断后前端算不出来，故后端给）
    retry_count: int = 0
    # [二期] 失败发生时间（= finished_at / started_at / created_at 首个非空）
    occurred_at: Optional[str] = None
    # [二期] 下钻抽屉的字段包，键与前端 `dataOperations.anomaly.field.*` 一一对应
    detail: Dict[str, str] = Field(default_factory=dict)


class ProcessingPipeline(BaseModel):
    """四级漏斗（状态列 ∪ 事件表，逐级单调）。"""

    total: int
    parsed: int
    extracted: int
    compiled: int


class PipelineTaskStats(BaseModel):
    """流水任务状态分布（后端预计算，前端不算）。"""

    success: int
    failed: int
    running: int
    # 恒 0：资产建档即进入 parsing，没有等待调度的中间态
    pending: int
    # 平均耗时只统计两端时间都齐的任务，样本为空时 None
    avg_duration_ms: Optional[int] = None


class AbnormalStageCount(BaseModel):
    stage: str
    count: int


class AbnormalSummary(BaseModel):
    """异常汇总：按失败任务计数，total 恒等于 by_stage 之和（未处置口径）。"""

    total: int
    by_stage: List[AbnormalStageCount] = Field(default_factory=list)


class DataAssetsData(BaseModel):
    total_assets: int
    total_data_sources: int
    cited_assets: int
    usage_rate: float
    # [二期] 窗口内新增资产（= access_trend 之和）
    new_assets_in_window: int = 0
    # [二期] 窗口内新增数据源
    new_data_sources_in_window: int = 0
    access_trend: List[TrendPoint] = Field(default_factory=list)
    composition: List[AssetCategoryCount] = Field(default_factory=list)
    top_cited_assets: List[CitedAssetItem] = Field(default_factory=list)
    unused_assets: List[UnusedAssetItem] = Field(default_factory=list)
    # [二期] 数据处理流水（四级漏斗 + 任务状态分布 + 明细）
    processing_pipeline: ProcessingPipeline
    pipeline_task_stats: PipelineTaskStats
    processing_tasks: List[ProcessingTaskItem] = Field(default_factory=list)
    # [二期] 异常资产（汇总 + 页内清单，清单只含未处置的）
    abnormal_summary: AbnormalSummary
    abnormal_assets: List[AbnormalAssetItem] = Field(default_factory=list)


class AbnormalAssetsPage(BaseModel):
    """异常资产下钻（`get_abnormal_assets_analytics`）的分页响应。"""

    items: List[AbnormalAssetItem] = Field(default_factory=list)
    total: int
    page: int
    page_size: int


class OntologyAggregate(BaseModel):
    entities: int
    relations: int
    active_entities: int
    activity_rate: float


class WikiAggregate(BaseModel):
    entities: int
    concepts: int
    active_pages: int
    activity_rate: float


class EntityTypeCount(BaseModel):
    entity_type: str
    count: int


class KbEntityDistributionItem(BaseModel):
    kb_id: Optional[str] = None
    kb_name: str
    count: int
    ratio: float


class UsedEntityItem(BaseModel):
    object_id: str
    canonical_name: str
    entity_type: str
    hit_count: int
    # 结构性恒 0：采用事件只记 document，实体采用无处可取
    adopted_count: int
    last_used_at: Optional[str] = None
    # [二期] 所属知识库
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None


class UnusedEntityItem(BaseModel):
    """长期未使用知识（本体实例）的一项。

    ⚠️ **判据同样是两半取交集**：实体创建满 `unused_threshold_days` 天
    **且** 该阈值窗口内 0 次命中。只判后者的话，昨天刚编译出来的实体会当天进榜。

    `created_at` 缺失（历史数据）的实体**不进这张榜** —— 年龄这半判不出来时放进榜，
    等于用「未知」冒充「长期未使用」。
    """

    object_id: str
    canonical_name: str
    entity_type: str
    # 实体创建时间（= 年龄判据的依据）。返回值里恒非 None：缺失的已被过滤掉
    created_at: Optional[str] = None
    # 全时段最近命中时间；None = 从未命中过
    last_used_at: Optional[str] = None
    # [二期] 所属知识库
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None
    updated_at: Optional[str] = None


class KnowledgeAssetsData(BaseModel):
    ontology: OntologyAggregate
    wiki: WikiAggregate
    entity_type_distribution: List[EntityTypeCount] = Field(default_factory=list)
    kb_entity_distribution: List[KbEntityDistributionItem] = Field(default_factory=list)
    entity_growth_trend: List[TrendPoint] = Field(default_factory=list)
    top_used_entities: List[UsedEntityItem] = Field(default_factory=list)
    unused_entities: List[UnusedEntityItem] = Field(default_factory=list)


class PipelineRate(BaseModel):
    count: int
    rate: float


class PipelineUsage(BaseModel):
    valid_requests: int
    ontology: PipelineRate
    rag: PipelineRate
    wiki: PipelineRate


class TopQuestionItem(BaseModel):
    query: str
    count: int


class QuestionTopicItem(BaseModel):
    topic: str
    count: int


class KbUsageItem(BaseModel):
    kb_id: str
    kb_name: str
    request_count: int
    last_used_at: Optional[str] = None
    # [二期] 库类型（lightrag / openkb / jonex_wiki），前端显示库类型徽标
    kb_type: Optional[str] = None


class ServiceUsageItem(BaseModel):
    service_id: str
    service_name: str
    request_count: int
    kb_count: int
    last_used_at: Optional[str] = None


class SearchAnalyticsData(BaseModel):
    total_requests: int
    active_users: int
    avg_requests_per_user: Optional[float] = None
    searched_kbs: int
    total_kbs: int
    unused_kbs: int
    used_services: int
    request_trend: List[TrendPoint] = Field(default_factory=list)
    pipeline_usage: PipelineUsage
    top_questions: List[TopQuestionItem] = Field(default_factory=list)
    question_topics: List[QuestionTopicItem] = Field(default_factory=list)
    kb_usage: List[KbUsageItem] = Field(default_factory=list)
    service_usage: List[ServiceUsageItem] = Field(default_factory=list)


# ══════════════════════════════════════════════════════════════
# [三期] 列表钻取详情（原型「点名称进抽屉」）
# ══════════════════════════════════════════════════════════════
#
# 三条口径先钉住，改这三个 DTO 前先读：
#
# ① **定位键一律是「真实键」，不是列表里的展示 id。**
#    本体实例的 `object_id` 是 `sha256(tenant|kb|entity_type|canonical_name)[:32]`
#    的单向哈希（`search_event_writer.ontology_object_id`），反解不回三元组；
#    且 `canonical_name` 可被改名，历史哈希会指向一个不存在的键。所以接口 B 收的是
#    `kb_id` + `entity_type` + `canonical_name` 三元组 —— 前端必须把这三个一起传回来。
#
# ② **抽屉里的次数必须与列表同窗口。** 「引用次数 / 命中次数 / 检索请求数」在原型上
#    都等于列表里的那个数字，所以三个接口都接受与列表同一组时间参数（`days` 或
#    `start_date`+`end_date`）并复用同一个聚合函数。前端打开抽屉时必须把打开列表时
#    用的那组参数原样带上 —— 不带不会报错，只会静默按默认 7 天算、数字对不上。
#
# ③ **权限前置在 capability 的 `_ANALYTICS_ACTIONS` 分支**（传 domain_space_id 则空间
#    成员、不传则仅租户管理员）；接口 C 额外复用 `KnowledgeInfoService.get()` 的
#    KB 级 404 防探测判据，不另写一套。


class AssetDetail(BaseModel):
    """抽屉 A · 数据资产详情（`get_data_asset_detail`）。"""

    asset_id: str
    file_name: str
    # 资产类型（前端口径 7 类），与 `CitedAssetItem.asset_type` 同源
    asset_type: Optional[str] = None
    # 展示用格式串（扩展名大写，兜底 mime 子类型）；后端推、别让前端按 mime 猜
    file_format: Optional[str] = None
    file_size: Optional[int] = None
    # 数据来源**原始码**：file / api / api_push / storage（文案由前端映射）
    source: Optional[str] = None
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None
    uploaded_at: Optional[str] = None
    # 最近处理时间 = 文档行的最后变更时刻（各链路共用一个时间戳列）
    last_processed_at: Optional[str] = None
    # ⚠️ 三条处理链各有自己的状态码，**没有一个能单独代表「当前处理状态」**：
    #   `status` = 解析/入库链路（pending/parsing/ingesting/ready/failed）
    #   `ontology_status` = 本体抽取（pending/extracting/ready/failed）
    #   `llm_wiki_compile_status` = LLM-Wiki 编译（compiling/compiled/stale/failed，非 wiki 库恒 None）
    # 三个都透出原始码，由前端按 kb_type 组合展示（原型那一个「处理状态」是合成的）。
    status: Optional[str] = None
    ontology_status: Optional[str] = None
    llm_wiki_compile_status: Optional[str] = None
    # 窗口内被采用次数（`search_adoption_event`，与「高频引用资产 Top10」同口径）
    cite_count: int = 0
    last_cited_at: Optional[str] = None


class OntologyAttribute(BaseModel):
    """本体实例的一个属性（键值对）。`value` 统一转字符串 —— 属性值类型不定。"""

    key: str
    value: str


class OntologyRelationTarget(BaseModel):
    canonical_name: str
    entity_type: Optional[str] = None


class OntologyRelationGroup(BaseModel):
    """按关系类型归组的目标实体列表（抽屉「主要关系」按此渲染）。

    ⚠️ **正反向边合并成同一组**：后端不暴露方向，`targets` 里既有本体指出去的目标、
    也有别的实体指过来的。拆分方向会出现两个同名组（如两个「发行方」），
    而原型那一列本来就不分方向。
    """

    relation_type: str
    targets: List[OntologyRelationTarget] = Field(default_factory=list)


class OntologyEntityDetail(BaseModel):
    """抽屉 B · Ontology 对象详情（`get_data_ontology_entity_detail`）。"""

    kb_id: str
    kb_name: Optional[str] = None
    canonical_name: str
    entity_type: str
    aliases: List[str] = Field(default_factory=list)
    description: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # 原样透出图里存的属性；**后端不补默认字段**（原型那套兜底模板由前端渲染 `—`）
    attributes: List[OntologyAttribute] = Field(default_factory=list)
    # 全部关系数（不是 relation_groups 展开后的条数 —— 邻居拉取有上限）
    relation_total: int = 0
    relation_groups: List[OntologyRelationGroup] = Field(default_factory=list)
    # 关系明细被上限截断（`relation_total > 实际返回条数` 时为 True）
    relation_truncated: bool = False
    # 窗口内命中次数（`search_hit_event`，与「高频使用本体实例 Top10」同口径）
    hit_count: int = 0
    # 结构性恒 0：采用事件只记 document，实体采用无处可取（同 `UsedEntityItem`）
    adopted_count: int = 0
    last_used_at: Optional[str] = None
    # Neo4j 不可用 → 属性/关系为空 + 本标记；`hit_count` 仍照常返回（它在 PG）
    degraded: bool = False


class KbOntologyStats(BaseModel):
    """知识库的编译资产数量。

    `object_instances` / `relation_instances` 为 `None` = **未取到**（而非「真的是 0」）：
    上游 `KnowledgeInfoService.get(with_stats=True)` 在 Neo4j / 编译服务不可用时
    **把计数填 0** 并置 `ontology_degraded=True`，本层把那个 0 转回 `None`。
    前端据此渲染 `—` —— 透出 0 等于对用户谎称「这个库里没有对象」。

    `degraded` 单独标记「本该有、但取不到」。**真的为 0 的库**两个字段是 `0` 且
    `degraded=False`（两种情况前端要分开渲染）。

    注：抽屉 C 每次都传 `with_stats=True`，所以**不存在**「llm-wiki 库首屏不加载统计」
    这一情况（那是知识库详情页的优化，与本接口无关）。
    """

    object_instances: Optional[int] = None
    relation_instances: Optional[int] = None
    degraded: bool = False


class KbAssetTypeCount(BaseModel):
    asset_type: str
    count: int


class KbServiceRef(BaseModel):
    """知识库关联的领域服务（抽屉 C「关联领域服务」跳转用）。"""

    service_id: str
    service_name: str


class KbDetail(BaseModel):
    """抽屉 C · 知识库详情（`get_data_kb_detail`）。"""

    kb_id: str
    kb_name: str
    kb_type: Optional[str] = None
    ontology: KbOntologyStats
    # 数据资产（READY 文档）按前端口径 7 类拆解，与「数据资产」页签的构成同源
    asset_total: int = 0
    assets_by_type: List[KbAssetTypeCount] = Field(default_factory=list)
    services: List[KbServiceRef] = Field(default_factory=list)
    # 知识库行的最近更新时间
    updated_at: Optional[str] = None
    # 窗口内该库的检索请求数 / 最近一次检索时间（与「知识库使用情况」表同口径）
    request_count: int = 0
    last_request_at: Optional[str] = None


class ServiceKbRef(BaseModel):
    """领域服务关联的知识库（抽屉 D「关联知识库」）。与 `KbServiceRef` 正好互为反向。"""

    kb_id: str
    kb_name: str


class ServiceDetail(BaseModel):
    """抽屉 D · 领域服务详情（`get_data_service_detail`）。

    ⚠️ **本接口不 404**（与 A/B/C 三处**有意不同**）：

    抽屉的主体是列表那一行**归因记录**，服务实体只是它的装饰。列表本身在
    `services` 查不到时就把 `service_name` 回落成 `service_id`
    （`search_analytics` 的 `r.service_name or r.service_id`），所以「行在、实体不在」
    是列表**已经承认**的合法状态 —— 软删过的服务，以及历史回填把**空间 id** 写进了
    `entry_service_id` 的遗留数据（`scripts/backfill_search_events.py` 的
    `entry_service_of` 取的是 `extra_metadata.domain_id`，旧概念里那就是空间）。
    这里若 404，用户会看到「列表上明明有一行，点开说它不存在」—— 比空壳更坏。

    因此：**属性块按服务实体填，取不到就留空**，使用情况照常返回（它来自事件表，
    与服务实体无关）；范围外服务的属性同样不填（回落 id），两种情形对外**完全同形**，
    不构成「这个 id 在别的空间存不存在」的探测口子。
    """

    service_id: str
    # 服务实体取不到 / 不在本次范围 → 回落 `service_id`（与列表同口径）
    service_name: str
    # `services.status` **原始码**：列默认 `active`，但**不是枚举**
    # （`domain_service.update` 的 `updatable` 集合里含它，可被改成任意串）
    # → 前端要有兜底文案，**别写死枚举**。
    status: Optional[str] = None
    # `services.enabled` 启停开关（`enable`/`disable` 接口切换的就是它）。
    # 原型那个「运行中」建议用这个判据，`status` 只作次要信息。
    # 服务实体取不到 → None（前端渲染「—」）。
    enabled: Optional[bool] = None
    # ⚠️ **只列本次范围内的库**（过滤条件与列表行 `kb_count` 逐字相同），
    # 所以 `len(knowledge_bases)` 恒等于列表那一行的 `kb_count`。
    # 范围外的绑定不在此透出（那是别的空间的事）。
    knowledge_bases: List[ServiceKbRef] = Field(default_factory=list)
    # 窗口内调用次数 / 最近调用时间，与「领域服务使用情况」表那一行同口径同窗口
    request_count: int = 0
    last_request_at: Optional[str] = None


class OverviewDataAssets(BaseModel):
    total_assets: int
    total_data_sources: int
    cited_assets: int
    usage_rate: float


class OverviewKnowledgeAssets(BaseModel):
    ontology_entities: int
    ontology_relations: int
    wiki_entities: int
    wiki_concepts: int


class OverviewSearchAnalytics(BaseModel):
    total_requests: int
    searched_kbs: int
    total_kbs: int
    used_services: int
    unused_kbs: int
    active_users: int


class OverviewData(BaseModel):
    data_assets: OverviewDataAssets
    knowledge_assets: OverviewKnowledgeAssets
    search_analytics: OverviewSearchAnalytics


__all__ = [
    "AbnormalAssetItem",
    "AbnormalAssetsPage",
    "AbnormalStageCount",
    "AbnormalSummary",
    "AssetCategoryCount",
    "AssetDetail",
    "CitedAssetItem",
    "DataAssetsData",
    "EntityTypeCount",
    "KbAssetTypeCount",
    "KbDetail",
    "KbEntityDistributionItem",
    "KbOntologyStats",
    "KbServiceRef",
    "KbUsageItem",
    "KnowledgeAssetsData",
    "OntologyAggregate",
    "OntologyAttribute",
    "OntologyEntityDetail",
    "OntologyRelationGroup",
    "OntologyRelationTarget",
    "OverviewData",
    "OverviewDataAssets",
    "OverviewKnowledgeAssets",
    "OverviewSearchAnalytics",
    "PipelineRate",
    "PipelineTaskStats",
    "PipelineUsage",
    "ProcessingPipeline",
    "ProcessingTaskItem",
    "QuestionTopicItem",
    "SearchAnalyticsData",
    "ServiceDetail",
    "ServiceKbRef",
    "ServiceUsageItem",
    "TopQuestionItem",
    "TrendPoint",
    "UnusedAssetItem",
    "UnusedEntityItem",
    "UsedEntityItem",
    "WikiAggregate",
]
