const BUILT_IN_ACCESS_METHOD_IDS = new Set([
  'dam_demo_api',
  'dam_api_push_demo',
  'dam_demo_storage',
  'dam_demo_file',
  'dam_demo_mqtt',
  /** 数据库接入（038 补的种子）。不列进来就会显示库里的原始 name，而不是本地化文案 */
  'dam_database',
]);

const BUILT_IN_DATA_SOURCE_DEFAULT_NAMES: Record<string, Set<string>> = {
  ds_demo_internet_file: new Set(['文件上传', 'File Upload']),
  ds_demo_credit_file: new Set(['文件上传', 'File Upload']),
  ds_demo_medical_file: new Set(['文件上传', 'File Upload']),
  ds_demo_hwfin_file: new Set(['文件上传', 'File Upload']),
  ds_demo_llm_file: new Set(['文件上传', 'File Upload']),
};

const ACCESS_TYPE_KEY_SUFFIX: Record<string, string> = {
  api: 'api',
  api_push: 'apiPush',
  storage: 'storage',
  file: 'file',
  mqtt: 'mqtt',
  /** 数据库接入（038 补的种子）。漏掉它，界面上会露出原始串 `database`：
   *  「添加数据源」的默认名称直接取这里的结果，新建出来的数据源就叫 `database`。 */
  database: 'database',
  /** ETL 来源（ETL 集成页签的运维对象）。KB 卡片的数据源标签来自
   *  `knowledge_info.data_source_types`（统计所有 active 源），会带出 `etl`；
   *  漏掉这一项就在卡片上露出原始串 `etl`。 */
  etl: 'etl',
};

type Translate = (key: string) => string;

export function accessTypeDisplayName(accessType: string, t: Translate): string {
  const suffix = ACCESS_TYPE_KEY_SUFFIX[accessType];
  return suffix ? t(`domainKnowledge.dataSourceType.${suffix}`) : accessType;
}

/**
 * 排除 ETL 来源（「按真实数据源列/选」的消费面专用）。
 *
 * ETL 来源是「ETL 集成」页签的运维对象：config 指向一个 database 数据源，
 * 自己**没有**编译 schema。多源视图 / 实体落点 / 指标落点若把它当普通数据源，
 * 会出现没有 schema 的幻影源列，以及永远解析不到的落点目标。
 * 其他 accessType（api / storage / file / database）行为不变。
 */
export function excludeEtlSources<T extends { accessType: string }>(sources: T[]): T[] {
  return sources.filter((source) => source.accessType !== 'etl');
}

/**
 * 只保留**有 ds 级编译 schema** 的数据源（database）。
 *
 * file / api / api_push / storage 没有编译 schema；把它们列进「全部数据源」、
 * 实体落点、指标落点会得到一条永远空白的幻影源。ETL 也被此判据排除。
 */
export function compiledSchemaSources<T extends { accessType: string }>(sources: T[]): T[] {
  return sources.filter((source) => source.accessType === 'database');
}

export function accessMethodDisplayName(
  method: { id: string; accessType: string; name: string },
  t: Translate,
): string {
  return BUILT_IN_ACCESS_METHOD_IDS.has(method.id) ? accessTypeDisplayName(method.accessType, t) : method.name;
}

/**
 * Only localize the untouched names of product-owned seed instances.
 * A user-renamed instance—even when it retains a seed ID—must remain unchanged.
 */
export function dataSourceInstanceDisplayName(
  source: { id: string; accessType: string; name: string },
  t: Translate,
): string {
  const defaultNames = BUILT_IN_DATA_SOURCE_DEFAULT_NAMES[source.id];
  return defaultNames?.has(source.name) ? accessTypeDisplayName(source.accessType, t) : source.name;
}
