/**
 * 平台级数据源连接池的客户端。
 *
 * 与 `dataAccess.ts` 的分工：那个是**接入方式目录**（平台级、只读、全局一份），
 * 这个是**连接池**（按领域空间隔离、可增删改测，不直接绑知识库）。
 *
 * 与 core-business 的 `/data-sources` 也不是一回事：那边每行是「某个知识库对某条
 * 连接的引用」，这边是「连接本体」——凭证只存在这边（改密码，N 个库一起生效）。
 */
import apiClient from './request';

/** 后端 `DataSourceConnection.to_dict()` 的形状。 */
export interface ConnectionItem {
  id: string;
  /** 所属领域空间；连接只在同领域内可见、可被引用 */
  space_id: string;
  name: string;
  access_type: string;
  /** `password_ref` 已被后端脱敏成 `***` */
  config_json: Record<string, unknown>;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
}

/**
 * 创建/更新的载荷。`password` 走明文，后端 `encrypt_config` 会换成 `password_ref`。
 *
 * `url` / `token` 只给 InfluxDB v3 用；`host` / `port` / `user` 对它是空的。
 * token 也走 `password` 字段落库（Influx 适配器按 `token`/`password` 读），
 * 不发明文 `token` 键——那不在后端 `encrypt_config` 的白名单里，会明文落库。
 */
export interface ConnectionPayload {
  name: string;
  engine: string;
  host?: string;
  port?: number;
  database: string;
  user?: string;
  /** InfluxDB v3 的 HTTP 地址，如 `http://127.0.0.1:8181` */
  url?: string;
  /** InfluxDB v3 的 API token（提交时映射为 `password`） */
  token?: string;
  password?: string;
  /** PostgreSQL / SQL Server 用；MySQL 拿 database 当 schema，传了也没用 */
  schema?: string;
  /**
   * 只读账号（② NL→SQL 专用）。`undefined` = 本次不动它；`""` = 显式清空。
   * 后端只认 `readonly_user` / `readonly_password_ref`，绝不回退到写账号。
   */
  readonlyUser?: string;
  /** 只读账号的口令明文；后端 `encrypt_config` 会换成 `readonly_password_ref`。留空 = 不改 */
  readonlyPassword?: string;
  /**
   * 「该账号即只读账号」：`true` 时用户 SQL（NL→SQL / 演化真值）直接用主账号执行，
   * 显式只读账号/密码不再提交。`false` = 显式关掉（merge 语义，不送就关不掉）；
   * `undefined` = 本次不动该开关。
   */
  readonlyUseMain?: boolean;
}

/** `POST /data-source-connections/{id}/test` 的返回。**连接失败也是 HTTP 200**，看 `ok`。 */
export interface TestConnectionResult {
  ok: boolean;
  message: string;
  database?: string;
  version?: string;
}

export interface CatalogStats {
  tables: number;
  columns: number;
  tables_with_comment: number;
  columns_with_comment: number;
  declared_fks: number;
}

export interface CatalogResult {
  connection_id: string;
  engine: string;
  database: string;
  stats: CatalogStats;
}

/**
 * 表单值 → 后端 `config_json`。
 *
 * **每次都由表单字段现造一份**，绝不 spread 既有的 `config_json`：那份里带着
 * 后端脱敏后的 `password_ref: "***"`，回传上去只会把真密文盖成假密文
 * （`password_ref` 是服务端管理字段，客户端造不出合法 Fernet 密文，后端也会丢，
 * 但正确的客户端行为是压根不发它）。
 *
 * **空密码连键都不带**（创建、更新同一个口径）：
 *   - 后端 `encrypt_config` 只认**非空**明文（空的既不 `pop` 明文、也不铸 `password_ref`），
 *     而留下来的 `password: ""` 又**不在 `mask_secrets` 的 `*_ref` 白名单里** ——
 *     它会原样落库、原样回给前端，看着像「设过一个空密码」。
 *   - 更新时不带 = 后端 `merged` 里保留原 `password_ref`，即「留空 = 不改密码」
 *     而不是「把密码清空」。
 */
function toConfigJson(payload: ConnectionPayload): Record<string, unknown> {
  // InfluxDB v3 不是「host:port 的 SQL 库」：它用 HTTP URL + token。token 必须
  // 借 `password` 字段落库，让后端 `encrypt_config` 铸 `password_ref` —— 后端
  // 只会加密 `password`，写成明文 `token` 键会**原样落库并回显**。
  if (payload.engine === 'influx_v3') {
    const config: Record<string, unknown> = {
      engine: payload.engine,
      url: payload.url ?? '',
      database: payload.database,
    };
    if (payload.token) config.password = payload.token;
    // 只读凭据是 ② NL→SQL 的写库凭据口径，时序源不适用：一律不写。
    return config;
  }
  const config: Record<string, unknown> = {
    engine: payload.engine,
    host: payload.host,
    port: payload.port,
    database: payload.database,
    user: payload.user,
  };
  if (payload.schema && payload.engine !== 'mysql') config.schema = payload.schema;
  if (payload.password) config.password = payload.password;
  // 「该账号即只读账号」优先于显式只读凭据：勾选后两者互斥，凭据不写进 config
  //（后端 `reconcile_readonly_mode` 是同一口径，这里先做一次，避免把没用的键发出去）。
  if (payload.readonlyUseMain === true) {
    config.readonly_use_main = true;
    return config;
  }
  // 取消勾选必须显式送 `false`：更新是 merge 语义，不送就关不掉。
  if (payload.readonlyUseMain === false) config.readonly_use_main = false;
  // 只读凭据（② NL→SQL 用）。口径与写密码一致：明文进、后端换密文引用。
  // `undefined` 不写键（创建时不留空壳）；空串**要写**——更新是 merge 语义，
  // 不显式送 `""` 就删不掉已登记的只读账号。
  if (payload.readonlyUser !== undefined) config.readonly_user = payload.readonlyUser;
  if (payload.readonlyPassword) config.readonly_password = payload.readonlyPassword;
  return config;
}

export async function listConnections(spaceId: string): Promise<ConnectionItem[]> {
  const res = await apiClient.get<{ items: ConnectionItem[] }>(
    '/knowledge-base/data-source-connections',
    { params: { space_id: spaceId } },
  );
  return res.items || [];
}

export async function createConnection(
  payload: ConnectionPayload,
  spaceId: string,
): Promise<ConnectionItem> {
  return apiClient.post<ConnectionItem>('/knowledge-base/data-source-connections', {
    space_id: spaceId,
    name: payload.name,
    access_type: 'database',
    config_json: toConfigJson(payload),
  });
}

/**
 * 更新连接。
 *
 * **不传 password 时后端保留原密码** —— 所以编辑弹窗里密码留空是「不改密码」，
 * 而不是「把密码清空」。`toConfigJson` 把空密码整个键都剔掉，正是为了走那条分支。
 *
 * `space_id` 必须**同时**放进 query：网关的 PATCH 端点是 `space_id: str = Query(...)`
 * 必填（`api_gateway/routes/knowledge_base.py` 的 `update_ds_connection`），只放 body
 * 会在进函数前就回 1001 / `loc=["query","space_id"]`（2026-11-05 真机踩过，编辑连接
 * 100% 失败）。body 里那份照旧保留：网关拿到 query 后会覆盖它，且与新建端点的 body
 * 口径一致，脚本/老客户端按 body 读也不会错位。
 */
export async function updateConnection(
  id: string,
  payload: ConnectionPayload,
  spaceId: string,
): Promise<ConnectionItem> {
  return apiClient.patch<ConnectionItem>(
    `/knowledge-base/data-source-connections/${id}`,
    {
      space_id: spaceId,
      name: payload.name,
      config_json: toConfigJson(payload),
    },
    { params: { space_id: spaceId } },
  );
}

export async function deleteConnection(id: string, spaceId: string): Promise<void> {
  await apiClient.delete(`/knowledge-base/data-source-connections/${id}`, undefined,
    { params: { space_id: spaceId } });
}

/** 测连接。入参是**已落库的** id——后端这个端点只吃 id，不支持未保存的配置。 */
export async function testConnection(
  id: string,
  spaceId: string,
): Promise<TestConnectionResult> {
  return apiClient.post<TestConnectionResult>(
    `/knowledge-base/data-source-connections/${id}/test`, undefined,
    { params: { space_id: spaceId } },
  );
}

export async function readCatalog(id: string, spaceId: string): Promise<CatalogResult> {
  return apiClient.get<CatalogResult>(
    `/knowledge-base/data-source-connections/${id}/catalog`,
    { params: { space_id: spaceId } },
  );
}
