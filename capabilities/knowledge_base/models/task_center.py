"""Knowledge Base — 任务中心接入表（Ontology 库两步链路）。

对应表：

- `knowledge_base.task_center_documents`     —— 接管行：一份文档交给任务中心了，走到哪一步
- `knowledge_base.task_execution_registry`   —— 执行登记：这个 `execution_id` 我接过没有

两张表都是**协调信息**，不是业务数据：文档本身的状态（`status` / `ontology_status`）
仍然在 `knowledge_base.knowledge_documents` 上，本模块一个字都不改它们的口径。

## 为什么用独立表，而不是往 `knowledge_documents` 加列 + JSONB 块

1. **对账跳过要能下沉成 SQL。** 被接管的文档必须从既有对账/巡检的扫描面里排除掉
   （否则两套驱动器抢同一个文档）。写成 `NOT EXISTS` 子查询是**在 SQL 里排除**，
   扫描额度不会被被接管的文档吃掉；写成「先取一批、再在 Python 里 continue」的话，
   一个全是接管文档的 KB 会把 `limit=50` 全占满，真正需要救的文档排不进来。
2. **每步记账是一次 CAS 更新。** 任务中心链每步都要写「这一步的 execution_id / rag_task_id」，
   而 SQLAlchemy 的 JSONB 列**不追踪就地修改**（既有代码一律整块替换赋值），
   并发下整块替换必丢更新。
3. **`remove_task` 要按 `task_id` 反查。** 独立表建唯一索引即可，不动
   `knowledge_documents` 这张热表、也不需要给它加索引。

## 接管行与文档状态的写入必须在同一事务里

两份状态（接管行 + 文档行）的代价就是「可能不一致」。对它的对冲只有一条纪律：
**凡是同时改两者的地方，必须在同一次 `get_db_session()`、同一次 commit 里改完**
（见 `services/document_service.py::_upload_via_task_center` 与
`services/task_watch_service.py`）。
"""

from enum import Enum

from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from jonex_core.common.database import Base
from jonex_core.common.entity import TenantMixin, TimestampMixin


class TaskCenterDocState(str, Enum):
    """接管行状态。**只有三态**（没有 `revoked`）。

    用户中途想接管（重新解析 / 重抽 / 重置 schema）时的处置是**拦住用户、让他等**
    （见 `_ACTIVE_STATES` 的消费点），所以没有任何动作会产生「撤销」这个第四态。
    """

    SUBMITTING = "submitting"
    """任务已提交给任务中心，但还没确认建没建成（`submit` 还没回来 / 回来了但没写库）。

    ⚠️ **这个状态必须和对账一起用**：此刻文档行是 `PARSING`、且可能还没有任何
    `rag_task_id`，正是既有对账最容易误判的形态。看门狗每轮会对它**重新提交一次**
    （同一个 `task_id`，撞 1006 即 `REUSED`，天然幂等），提交成功才转 `managed`。
    """

    MANAGED = "managed"
    """任务中心链正在跑（或刚跑完某一步、在等下一步派活）。"""

    RELEASED = "released"
    """**看门狗交还**：运行方不再盯着这一步了 —— 但**任务中心那条链可能还活着**。

    三种来源：① 三步都跑完（compile 步 READY）；② 链路判死（任一步失败/超时）；
    ③ 任务被删（`remove_task`）。

    ⚠️ **`released` ≠ 「任务中心不管了」**（2026-10-14 更正）。它只说明**看门狗放弃
    等待**（典型是 `_abandon` 等满 `TC_NO_DISPATCH_SEC` 之后交还）。交还那一刻任务中心
    那条链完全可能是活的 —— 实测：`compile` 子任务一直 `pending` 在队列里等全局并发
    名额，任务还是 `running`，**只是派不出去**。

    所以「这份文档归不归任务中心管」看的是 `MANAGED_STATES`（含本态），**不是本态
    本身**；旧对账据此决定「能不能替它发起工作」。原 docstring 写的「任务中心那侧不再
    管这份文档了」是错的 —— 那正是 2026-10-14 那次事故的成因：交还之后旧对账自己把
    本体抽取跑完了（`metering.llm_usage_log` 16:40 起 31 次 `ontology_extract`），
    而任务中心那条 `compile` 永远停在「等待中」，两边各记各的账。

    收尾不受影响：文档行一个字段都不改，`PARSING` 仍由 `reconcile_documents` /
    `patrol_parsing_timeout` 兜住，`READY` 是稳定态。

    ⚠️ 来源 ③（任务被删）**与上面这条口径不自洽**：任务都没了，文档却仍算「归任务中心
    管」→ 旧对账不许驱动它，而也没有任务能驱动它 = **没人管**。当前任务删除功能未对外
    开放，故**本期不处理**；**开放删除之前必须先改 `remove_task`**（删掉接管行，或另立
    一个明确的「彻底交回」态），否则被删任务的文档会被永久搁置。详见 `remove_task` 的
    docstring。
    """


#: 「还在接管中」的两个状态 —— **回答「还有人在为它干活吗」**。
#:
#: 消费点：对账/巡检的**收尾**判据（`_takeover_is_in_flight`）、用户接管拦截
#: （`exists_active`）、看门狗扫描（`list_active`）、`begin_step` / `mark_released`
#: 的 CAS、jonex_wiki 的 fencing。
#:
#: ⚠️ **不含 `released`**，而且**不许**把它换成 `MANAGED_STATES`：本集合要回答的是
#: 「有没有人在跑」，交还之后确实没人跑了，对账**必须**能看见这些文档去收尾
#: （否则交还的解析任务没人管，文档永久卡 `parsing`）。「归谁管」是另一个问题，
#: 见 `MANAGED_STATES`。
ACTIVE_STATES: tuple[str, str] = (
    TaskCenterDocState.SUBMITTING.value,
    TaskCenterDocState.MANAGED.value,
)


#: 「归任务中心管」的三个状态 —— **回答「这份文档归谁管」**（2026-10-14 加）。
#:
#: 与 `ACTIVE_STATES` 的差别只有一个 `released`，但这一格就是全部意义所在：
#: 交还之后**任务中心仍然管着这份文档**（任务还在，只是它暂时派不出活），所以
#: **旧对账不许替它发起任何下游工作**（解析 / 本体抽取 / wiki 编译 / 补提交）。
#: 用户想重跑，走「重新解析 / 重新编译」两个按钮 —— 那两条路会 `rearm_for_step`
#: 把卡打回 `managed` 并请任务中心重跑，授权链是完整的。
#:
#: ⚠️ **专用于旧对账的「发起」闸门**，别拿去当收尾判据 —— 两处用错都会出事：
#:   · 收尾用本集合 → 交还的文档被判成「有人在管」而永远 skip，永久卡 `parsing`；
#:   · 发起用 `ACTIVE_STATES` → 就是本次要修的 bug（旧对账替交还的文档发起工作）。
MANAGED_STATES: tuple[str, str, str] = (
    TaskCenterDocState.SUBMITTING.value,
    TaskCenterDocState.MANAGED.value,
    TaskCenterDocState.RELEASED.value,
)


class TaskCenterDocument(Base, TenantMixin, TimestampMixin):
    """一份被任务中心接管的文档 —— 一行，主键是 `document_id`。

    主键取 `document_id` 而不是自增 id：查它的人（对账跳过、用户接管拦截、
    看门狗、handler）**手上都只有文档 id**，按它直接命中最省事，也天然做到
    「一份文档在链路上只可能有一个位置」。

    代价要写清楚：**二次进链（如将来的重新解析）会覆盖旧行、历史不可追溯。**
    当前不做流水表 —— 需要审计时另起一张，不要在這一行的列上堆。
    """

    __tablename__ = "task_center_documents"
    __table_args__ = (
        # 索引名 / 列 / 唯一性**逐条对齐 DDL**（`deploy/postgres/update/037_knowledge_task_center.sql`，
        # 2026-09-29 由 `038_ontology_task_center.sql` 改名而来）。
        # 线上建表走的是那份 DDL，但这几条**不能用列上的 `index=True` 代替**：`index=True` 生成的
        # 名字是 `ix_knowledge_base_task_center_documents_*`，与 DDL 里的 `ix_kb_tc_doc_*` **不同名**，
        # 于是「`create_all` 先建表 + 自己那套索引，041 再 `CREATE INDEX IF NOT EXISTS` 补一套」
        # 会留下**同列两条索引**。task_center 的模型注释里记着同一条纪律（别用 `index=True`）。
        # `remove_task` 只拿得到 `task_id`（报文里就三件套），按它反查接管行。
        # 唯一：task_id 是服务端 uuid4 生成的，一份文档一条任务，不可能两条接管行共用一个。
        Index("uq_kb_tc_doc_task", "task_id", unique=True),
        # 看门狗每轮取在途行来盯终态；`knowledge_base_id` 供库级拦截（重置本体）用。
        Index("ix_kb_tc_doc_state", "state"),
        Index("ix_kb_tc_doc_kb", "knowledge_base_id"),
        # 租户索引：本表没有「只按租户过滤」的查询（点查走主键、库级拦截走 kb），
        # 建它是与本 schema 的惯例一致 + 堵住 `TenantMixin` 那条自动命名的索引（见 tenant_id）。
        Index("idx_kb_tc_doc_tenant", "tenant_id"),
        {"schema": "knowledge_base"},
    )

    document_id = Column(String(64), primary_key=True)
    #: ⚠️ **本列刻意在子类里重写一遍**（覆盖 `TenantMixin` 那条 `Column(..., index=True)`）。
    #: mixin 自带索引的名字是自动生成的 `ix_knowledge_base_task_center_documents_tenant_id`，
    #: 与 DDL 里的 `idx_kb_tc_doc_tenant` **不同名** —— 于是「`create_all` 先建表 + 自己那条，
    #: 041 再 `CREATE INDEX IF NOT EXISTS` 补一条」会在**同一列上留下两条索引**（写入多维护
    #: 一棵 B 树、谁也不报错）。所以这里自己声明一次（类型/可空与 mixin 逐字相同，只去掉
    #: `index=True`）、把索引名写进 `__table_args__`，由测试钉住两边同名。
    tenant_id = Column(String(64), nullable=False)
    knowledge_base_id = Column(String(128), nullable=False)
    #: 提交给任务中心时用的空间（`init_task` 会校验它属于本租户）。
    #: 存下来是因为**后台重新提交 / 看门狗判死**那两条路都不在请求上下文里，
    #: 拿不到 KB 的 space_id —— 不能靠 `TenantContext` 兜。
    space_id = Column(String(64), nullable=False)
    task_id = Column(String(64), nullable=False)
    #: 发起这次上传的人（`user_id`，落任务中心 `tasks.created_by` 的语义）。
    #: 空值是合法状态（内部链路 / 拿不到身份）—— 看门狗重新提交时原样带上。
    operator_id = Column(String(64), nullable=True)

    state = Column(String(16), nullable=False, default=TaskCenterDocState.SUBMITTING.value)

    #: 当前在跑哪一步：`parse` / `ingest` / `compile`（运行方只认这三个字面量）。
    #: **本步上报成功后不被清空**（它就是「最后完成到哪一步」的记录），
    #: 于是它 + `execution_id` 是否为 NULL 一起回答了看门狗最关心的那个问题。
    current_step = Column(String(32), nullable=True)
    #: 本步的受理凭证（任务中心生成），上报时原样带回。
    #:
    #: ⚠️ **本步上报成功后同一句 UPDATE 里被清空**（值不丢，同时填进 `handled_execution_id`）。
    #: 不清的话，「第 1 步已上报、正在等第 2 步派活」这个正常窗口会被看门狗
    #: 读成「有一个 execution 却迟迟没发出下游」而误判死 —— 这两个形态在
    #: 「有 execution_id、无 rag_task_id」上是完全同形的，只能靠清空来区分。
    execution_id = Column(String(64), nullable=True)
    #: **最后一次上报过终态**的那次执行编号（值来自同一句 UPDATE 里那个被清空的
    #: `execution_id`，所以本步报完之后这一对恒为 `(NULL, <刚报的那次>)`）。
    #:
    #: 两个用途：① 审计 —— 回答「现在这个文档状态是哪一次执行做出来的」；
    #: ② 幂等条件 —— `mark_step_handled` 的 WHERE 带着
    #: `handled_execution_id IS DISTINCT FROM :execution_id`，兜的是「上报与
    #: 清空之间进程崩掉、下一轮又拿同一行去报」这种残余窗口（第二次 rowcount=0）。
    #: ⚠️ **看门狗自己不读这一列**，也没有「`handled == execution` 就只 release」这种
    #: 分档：那种状态要求同一个 `execution_id` 在报完之后又被派一次，而执行登记表
    #: 那一关就把它挡回去了（`ALREADY_TAKEN`），不可达。
    handled_execution_id = Column(String(64), nullable=True)
    #: 本步发给 atomic-rag 的任务号。**上报成功后清空** —— 让「有 execution_id、
    #: 无 rag_task_id」只衡量**任务中心的派活延迟**（`TC_NO_DOWNSTREAM_SEC`），
    #: 而不是 atomic-rag 的执行时长（两个完全不同的东西不能共用一个阈值）。
    rag_task_id = Column(String(128), nullable=True)

    #: **最后一次收口的那份上报报文**（JSONB）。NULL = 还没收口、或收口是**中性**的一次。
    #:
    #: 2026-10-11 加，**只服务「管理方主动问」**（`RunnerPort.query_step_status`）。
    #:
    #: ⚠️ **为什么存整份报文、而不是只存一个 `success`/`failed` 字样**（用户 2026-10-11 定）：
    #: 管理方问出「成功了」之后**不能只写个状态** —— 它还要拿这一步的**产出**（`outputs`）
    #: 去填下一步的输入（`bind_inputs`：比如「解析」的产出里有 `document_id`，「编译」要用），
    #: 而**产出只有运行方知道**。所以把当初要上报的那份报文**原样存下来、原样回** ——
    #: 管理方拿到它就能**当一条上报直接消化，零转换、不可能分叉**（形状见协议层
    #: `StepStatusQuery.report`，与上报口 `ReportStatusRequest` 逐字一致）。
    #:
    #: ⚠️ **别拿 `error_message` 代替它**：`error_message` 非空**不等于失败** ——
    #: `remove_task`（任务被删除）也往那儿写一句说明，但那是**中性**的（既不算成、也不算败）。
    #:
    #: **谁写**：成功收口（`mark_step_handled`）写成功报文；判死（`_fail_step` /
    #: `_fail_document` / `_abandon` → `mark_released(report=...)`）写失败报文；
    #: 「任务被删除」那种中性收口**不写**（保持 NULL → 管理方问出「这活不归我」）。
    last_report = Column(JSONB, nullable=True)

    #: 看门狗的计时基准。**不是 `created_at`** —— 那个是「接管开始」的时刻，
    #: 而每步的活是从「本步开始」算的，共用一个基准会把前一步的耗时算到后一步头上。
    #:
    #: 本步结束后**清空**（与 `execution_id` 同一次 UPDATE）；为 NULL 时看门狗
    #: 退回用 `updated_at` 计时 —— 那个时刻恰好就是「清空的那一次写入」或
    #: 「转 managed 的那一次写入」，与「本步尚未派活」的起点重合。
    step_started_at = Column(DateTime, nullable=True)
    #: 重新提交的次数（`submitting` 卡住时看门狗会补试）。
    submit_attempts = Column(Integer, nullable=False, default=0)
    #: 判死原因 / 最后一次提交失败的原因，只做排障留痕。
    error_message = Column(Text, nullable=True)

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "tenant_id": self.tenant_id,
            "knowledge_base_id": self.knowledge_base_id,
            "space_id": self.space_id,
            "task_id": self.task_id,
            "operator_id": self.operator_id,
            "state": self.state,
            "current_step": self.current_step,
            "execution_id": self.execution_id,
            "handled_execution_id": self.handled_execution_id,
            "rag_task_id": self.rag_task_id,
            "last_report": self.last_report,
            "step_started_at": self.step_started_at.isoformat() if self.step_started_at else None,
            "submit_attempts": self.submit_attempts or 0,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TaskExecutionRegistry(Base, TimestampMixin):
    """执行登记行 —— 「这个 `execution_id` 我接过没有」（`ExecutionRegistry` 端口的持久实现）。

    **为什么非持久不可**：任务中心**确实会重派**同一个 `execution_id`（出站没够着运行方时
    当轮同步重发，最多 3 次）。认不出重派就会**重复干活** —— 重复建下游 atomic-rag 任务、
    重复扣配额、产出重复向量。而 SDK 默认给的 `InMemoryExecutionRegistry`
    **进程重启即全丢、多副本之间互相看不见**，任务一跑就是几十分钟
    （`RAG_TASK_HARD_TIMEOUT_SEC` 默认 21600 秒），重启窗口是真实存在的。
    所以本实现是**上生产的必要条件**，KB 启动时会自检 `persistent`（见 `capability.py`）。

    ⚠️ **没有 `tenant_id` 的软删与租户过滤**：`execution_id` 是任务中心用 `uuid4()`
    生成的、事实上全局唯一，所以键只取它一个（端口的 docstring 已钉死这一点）。
    这一列留着纯粹是为了排障时能一眼看出这条登记属于谁。
    """

    __tablename__ = "task_execution_registry"
    __table_args__ = {"schema": "knowledge_base"}

    execution_id = Column(String(64), primary_key=True)
    #: 不参与定位，只为排障留痕。
    tenant_id = Column(String(64), nullable=True)
    space_id = Column(String(64), nullable=True)
    task_id = Column(String(64), nullable=True)
    step_code = Column(String(32), nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    #: `NULL` = **当前被持有**；非空 = 已释放、可以被重新领取。
    #:
    #: 「已释放的可再占」是重发安全性的前提：重发时 `execution_id` 不变，
    #: 我们必须能重新接下它，否则重发全被自己人挡住、任务永远卡在 `running`。
    released_at = Column(DateTime, nullable=True)
    #: 领过几次（含重派重新领的）。只做观测，不参与判定。
    claim_count = Column(Integer, nullable=False, default=1)

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "tenant_id": self.tenant_id,
            "space_id": self.space_id,
            "task_id": self.task_id,
            "step_code": self.step_code,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "released_at": self.released_at.isoformat() if self.released_at else None,
            "claim_count": self.claim_count or 0,
        }


__all__ = [
    "ACTIVE_STATES",
    "MANAGED_STATES",
    "TaskCenterDocState",
    "TaskCenterDocument",
    "TaskExecutionRegistry",
]
