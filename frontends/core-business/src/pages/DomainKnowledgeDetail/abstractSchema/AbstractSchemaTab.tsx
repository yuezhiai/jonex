/**
 * 知识库设置页 →「概念对齐」页签。
 *
 * 这里管的是**跨数据源的抽象 schema**：LLM 从各源的 entity_types 提出候选对齐簇
 * （「不良」下面挂着 UC3 的 EmployeeDefectRate 和资产库的 DefectRecord），
 * 人工勾选/改名/合并/删源后**确认**才生效（`status: 'active'`）。
 *
 * v2 形状：这里的「概念」已升为**实体**（`AbstractEntity`）—— 每条挂 N 个**源落点**，
 * 落点可逐个勾选纳入/排除（`included`；不纳入 = 对齐保留但不生效）。卡片页只编
 * 改名/描述/别名/删源/落点纳入，**属性与关系原样带回**（属性在编辑器里编、关系本页不编）。
 *
 * 未确认（`proposed`）的对齐**不参与检索** —— 人还没看过的东西不该影响答案。
 *
 * 五种状态按 CLAUDE.md 的要求都覆盖：loading / empty / error / success / refresh。
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Alert, Button, Empty, Space, Spin, Tag, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  generateAbstractSchema,
  getAbstractSchema,
  saveAbstractSchema,
  type AbstractAttribute,
  type AbstractEntity,
  type AbstractLanding,
  type AbstractSchema,
  type FocusSeedInput,
} from '@/api/abstractSchema';
import ConceptCard from './ConceptCard';
import DataAlignmentSection from './DataAlignmentSection';
import FocusGenerateModal from './FocusGenerateModal';

const { Text, Paragraph } = Typography;

/**
 * 界面上的实体 = 落库形状（v2 `AbstractEntity`：改名/描述/别名/落点/属性）+ 一个
 * **只在界面层存在**的稳定身份 `uid`。
 *
 * ⚠️ 为什么不能拿 `name` 当身份（它是卡片里的可编辑字段，用户随时会改）：
 * 勾上 A / B 后把 A 改个名，A 就掉出选择集 —— 按钮上「合并所选（2）」还挂着 2，
 * 点下去却只解析出 1 条、静默什么都不做；反过来，把没选中的实体改成已选中的
 * 那个名字，两张卡片会一起变勾，其中一条的源被另一条顶掉。身份必须跟着条目走，
 * 不跟着用户敲的字走 —— 选中集、React key、回写定位一律认 uid。
 *
 * `uid` **绝不发给服务端**：`doSave` 先经 `stripUid` 摘掉本地身份；`api/abstractSchema.ts`
 * 的 payload 映射（F1）再逐字段重建载荷（snake_case，只带业务字段）——两道都只放业务字段过。
 */
interface UiEntity extends AbstractEntity {
  uid: string;
}

/** 本地身份自增序号。不用 `crypto.randomUUID()`：它在非安全上下文（内网 http
 *  部署）下不存在、会直接抛，而这里要的只是「同一次会话内两两不同」。 */
let uidSeq = 0;
const withUid = (list: AbstractEntity[]): UiEntity[] =>
  list.map((e) => ({ ...e, uid: `c${++uidSeq}` }));

/** 只取会发给服务端的业务字段（丢掉本地 uid）。 */
const stripUid = (list: UiEntity[]): AbstractEntity[] =>
  list.map(({ uid: _uid, ...rest }) => rest);

interface Props {
  kbId: string;
  canWrite: boolean;
}

export default function AbstractSchemaTab({ kbId, canWrite }: Props) {
  const { t } = useTranslation();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');
  const [schema, setSchema] = useState<AbstractSchema | null>(null);
  const [entities, setEntities] = useState<UiEntity[]>([]);
  /** 存的是 **uid**，不是 name —— 见文件上方 `UiEntity` 的说明。 */
  const [selected, setSelected] = useState<string[]>([]);
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [focusOpen, setFocusOpen] = useState(false);

  const readOnly = !canWrite || schema?.status === 'active';
  const dirty = useMemo(
    // 比对的是**剥掉 uid 之后**的业务形状：uid 每轮重新生成，直接 stringify
    // `entities` 会永远跟服务端形状的 `schema.entities` 对不上 —— 页面一进来
    // 就挂着「未保存」，而且「保存草稿」按钮无端可用。
    () => JSON.stringify(stripUid(entities)) !== JSON.stringify(schema?.schema.entities ?? []),
    [entities, schema],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await getAbstractSchema(kbId);
      setSchema(data);
      // 整表替换 = 重新下发一轮 uid（与选择集一起换，不会新旧混用）
      setEntities(withUid(data?.schema.entities ?? []));
      setSelected([]);
    } catch (e: any) {
      setError(e?.message || t('abstractSchema.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, t]);

  useEffect(() => { void load(); }, [load]);

  /**
   * 生成（force=覆盖 active 二次确认；focusSeeds 非空 = 聚焦路径）。
   * `options.mode` 决定全部重新构造（replace）/ 追加（append）；`autoLandings`
   * 只在「不聚焦 + 文档本体种子」路径生效（后端口径）。返回是否成功。
   */
  const doGenerate = async (
    force = false,
    focusSeeds?: FocusSeedInput[],
    options?: {
      mode: 'replace' | 'append';
      seed: 'document' | 'structured';
      autoLandings: boolean;
    },
  ): Promise<boolean> => {
    setGenerating(true);
    try {
      const data = await generateAbstractSchema(kbId, force, focusSeeds, {
        mode: options?.mode ?? 'replace',
        // 聚焦路径只处理闭包内的源实体，文档种子会旁路聚焦（后端口径同此）。
        seed: focusSeeds?.length ? 'structured' : options?.seed ?? 'document',
        autoLandings: options?.autoLandings ?? false,
      });
      // ⚠️ **生成后同样必须重新 GET**（与 `doSave` 同一个坑、同一个解法）。
      // `generate()` 回带的实体与 `save()` 一样是**未标注**的原始形状：
      // 没有 `source_name`、没有 `stale`（只有 `get()` 会现算标注）。若直接
      // `setEntities(data.schema.entities)`，源标签就走 `s.sourceName || s.dataSourceId`
      // 的兜底分支、显示成 32 位 uuid —— 而跨源视图里源标签正是信息量最大的那一格，
      // 用户得手动点一次「刷新」它才变回数据源名字。
      // 注意 `droppedSources`/`focus` 只在 generate 的回包里，取重新 GET 的结果会丢，故下面仍用 data。
      // ⚠️ 但**重拉失败不能落进外层 catch**：此刻生成已写库成功，若按「生成失败」处理，
      // 用户会看到与实际不符的错误 toast、弹窗停在第二步，重试一次即第二次全量覆盖生成。
      // 重拉失败就退回 generate 的回包继续成功路径（源标签短暂显示 uuid 的降级），
      // 另发一条警告引导用户手动点「刷新」。
      let next: AbstractSchema = data;
      let refreshFailed = false;
      try {
        next = (await getAbstractSchema(kbId)) ?? data;
      } catch {
        refreshFailed = true;
      }
      setSchema(next);
      setEntities(withUid(next.schema.entities));
      setSelected([]);
      message.success(t('abstractSchema.generated', { count: next.schema.entities.length }));
      if (data.focus) {
        message.info(
          t('abstractSchema.focus.focused', {
            total: data.focus.includedTables,
            seeds: data.focus.seeds,
            partners: data.focus.partners,
            neighbors: data.focus.neighbors,
            excluded: data.focus.excludedTables,
          }),
        );
        if (data.focus.droppedSeeds > 0) {
          message.warning(t('abstractSchema.focus.droppedSeeds', { count: data.focus.droppedSeeds }));
        }
      }
      if (data.droppedSources > 0) {
        message.warning(t('abstractSchema.droppedSources', { count: data.droppedSources }));
      }
      if (data.landings.error) {
        message.warning(t('abstractSchema.landingsFailed', { error: data.landings.error }));
      } else if (data.landings.entities > 0 || data.landings.attributes > 0) {
        message.info(
          t('abstractSchema.landingsDone', {
            entities: data.landings.entities,
            attributes: data.landings.attributes,
          }),
        );
      }
      if (refreshFailed) {
        message.warning(t('abstractSchema.refreshAfterGenerateFailed'));
      }
      return true;
    } catch (e: any) {
      // 已有已确认的对齐时后端回 409：显式二次确认后才带 force 重试
      if (e?.status === 409 || e?.bizCode === 1003) {
        if (window.confirm(t('abstractSchema.overwriteConfirm'))) {
          return await doGenerate(true, focusSeeds, options);
        }
        return false;
      }
      message.error(e?.message || t('abstractSchema.generateFailed'));
      return false;
    } finally {
      setGenerating(false);
    }
  };

  const doSave = async (status: 'proposed' | 'active') => {
    setSaving(true);
    try {
      const body = await saveAbstractSchema({
        knowledgeBaseId: kbId,
        schema: {
          // 本地身份（uid）先在这里摘掉；逐字段重建（转 snake_case）交给
          // `saveAbstractSchema`（F1）—— 两道都只放业务字段过。
          entities: stripUid(entities),
          // ⚠️⚠️ **关系必须原样带回**：卡片页不编辑关系（关系在抽象编辑器里编），
          // 但保存是**整份 schema 覆盖**——这里若不带 `relations`，本页保存一次
          // （哪怕只改了一个别名）就会把全部关系连背书一起清空。这是本页最贵的回归点。
          relations: schema?.schema.relations ?? [],
        },
        status,
        expectedSchemaVersion: schema?.schemaVersion ?? 0,
      });
      // ⚠️ **保存后必须重新 GET，不能拿 save 的响应替换本地状态。**
      // `save()` 回带的实体是**未标注**的原始形状（没有 `stale` /
      // `stale_reason` / `source_name`）—— 只有 `get()` 会现算标注。若直接
      // `setEntities(body.schema.entities)`，「已失效」标签会全部消失，而顶部的
      // `stale_count` 仍在说「N 条对齐已失效」，界面自相矛盾。
      // （后端行为见 `abstract_schema_service.py` 的 `get` 与 `save`：
      //   两者都回 `stale_count`，但只有 `get` 逐条标注。）
      const fresh = await getAbstractSchema(kbId);
      setSchema(fresh ?? body);
      setEntities(withUid(fresh?.schema.entities ?? body.schema.entities));
      // 与 `load` / `doGenerate` 同一口径：这一行把整表 uid 全换了一轮，选择集里
      // 存的却是**旧 uid**。不清空的话，勾过的条目会变成对不上任何一张卡片的死条目 ——
      // 屏上没有一个勾，工具栏却还写着「合并所选（2）」且可点，点下去只会报
      // 「请先选中至少两个概念再合并」；而且 `toggleSelect` 只能追加活 uid，死条目
      // 赖到下次合并或刷新才会消失。
      setSelected([]);
      message.success(
        status === 'active' ? t('abstractSchema.confirmed') : t('abstractSchema.saved'),
      );
      const stale = fresh?.staleCount ?? body.staleCount;
      if (stale > 0) {
        message.warning(t('abstractSchema.staleWarning', { count: stale }));
      }
    } catch (e: any) {
      // 版本冲突：别人改过，必须刷新后重来（与本体画布同一口径）
      message.error(e?.message || t('abstractSchema.conflict'));
    } finally {
      setSaving(false);
    }
  };

  const toggleSelect = (uid: string) =>
    setSelected((prev) => (prev.includes(uid) ? prev.filter((u) => u !== uid) : [...prev, uid]));

  /** 合并所选实体：名字取第一个，落点/别名/属性取并集（去重）。身份只看 uid，不看名字。 */
  const mergeSelected = () => {
    const picked = entities.filter((e) => selected.includes(e.uid));
    if (picked.length < 2) {
      // 按 uid 认身份之后这里正常到不了（按钮本就 disabled）；真到了也必须说一声：
      // 静默 return 正是这次要修的缺陷的一半 —— 用户点了按钮、屏上什么也没发生，
      // 分不清是自己没选够还是页面坏了。
      message.warning(t('abstractSchema.mergeNeedTwo'));
      return;
    }
    const [head, ...rest] = picked;
    // 落点并集：同 (ds, 实体) 只留一条，**先到者胜（含它的 included）** —— 与后端
    // `normalize` 对重复落点的口径一致：后来那条的 included 不覆盖先到的那条。
    const sourceMap = new Map<string, AbstractLanding>();
    // 属性并集：同名去重，**先到者胜**（免得后来的同名属性把先到的源映射顶掉）。
    const attributeMap = new Map<string, AbstractAttribute>();
    for (const e of picked) {
      for (const s of e.sources) {
        const key = `${s.dataSourceId}:${s.entityName}`;
        if (!sourceMap.has(key)) sourceMap.set(key, s);
      }
      for (const a of e.attributes) {
        if (!attributeMap.has(a.name)) attributeMap.set(a.name, a);
      }
    }
    const merged: UiEntity = {
      // 合并结果承接头一条的身份：改名不影响它，列表里那格 DOM 也不会被重挂
      uid: head.uid,
      name: head.name,
      displayName: head.displayName,
      description: head.description || rest.map((e) => e.description).filter(Boolean).join('；'),
      aliases: Array.from(new Set([...picked.flatMap((e) => e.aliases), ...rest.map((e) => e.name)])),
      sources: Array.from(sourceMap.values()),
      attributes: Array.from(attributeMap.values()),
    };
    const pickedUids = new Set(picked.map((e) => e.uid));
    // 只摘掉**被选中的那几条**（按 uid），合并结果插到最前。
    // ⚠️ 这里不再按名去重：那会把「恰好与合并结果同名」的另一条实体整条丢掉、连同它
    // 的源映射（跨源视图里源就是全部信息量）。重名交给保存时收敛 —— 服务端
    // `normalize` 对同名条目是**合并**（源与别名取并集），丢源正是它要防的事。
    setEntities((prev) => [merged, ...prev.filter((e) => !pickedUids.has(e.uid))]);
    setSelected([]);
  };

  // 「数据资产对齐」区同样挂在**两个 return 分支**里：即使抽象 schema 还没生成，
  // 已有的对齐行也要能看/能审。**审批只受写权限约束**（`!canWrite`）—— 这里的
  // `readOnly` 还包含「schema 已确认」的锁定，若复用它做审批门控，确认后审批区就
  // 永远点不动了（后端审批与 schema 状态无关）。
  // 「生成建议」另有门控：后端只从 **active** 概念生成候选，schema 未确认时必然
  // 0 条，故由 `suggestDisabled` 单独禁用并常显原因。
  const canSuggestAlignments = canWrite && schema?.status === 'active';
  const dataAlignmentSection = (
    <DataAlignmentSection
      kbId={kbId}
      readOnly={!canWrite}
      suggestDisabled={!canSuggestAlignments}
    />
  );

  // 弹窗挂在**两个 return 分支**里（Empty 态那次生成也要能走聚焦路径：成功后
  // `schema` 才有值、才切到主分支），所以提成一个元素复用，不复制一份。
  const focusModal = (
    <FocusGenerateModal
      open={focusOpen}
      kbId={kbId}
      generating={generating}
      onClose={() => setFocusOpen(false)}
      onGenerate={async (focusSeeds, options) => {
        const ok = await doGenerate(false, focusSeeds, options);
        if (ok) setFocusOpen(false);
        return ok;
      }}
    />
  );

  if (loading) return <div style={{ padding: 24, textAlign: 'center' }}><Spin /></div>;
  if (error) return <Alert type="error" showIcon message={t('abstractSchema.loadFailed')} description={error} />;

  if (!schema) {
    return (
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {dataAlignmentSection}
        <Empty description={t('abstractSchema.empty')}>
          <Button
            type="primary"
            loading={generating}
            disabled={!canWrite}
            onClick={() => setFocusOpen(true)}
          >
            {t('abstractSchema.generate')}
          </Button>
        </Empty>
        {focusModal}
      </Space>
    );
  }

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap>
        <Tag color={schema.status === 'active' ? 'green' : 'orange'}>
          {schema.status === 'active'
            ? t('abstractSchema.statusActive')
            : t('abstractSchema.statusProposed')}
        </Tag>
        <Text type="secondary">{t('abstractSchema.versionTag', { v: schema.schemaVersion })}</Text>
        {schema.staleCount > 0 && (
          <Tag color="warning">{t('abstractSchema.staleCount', { count: schema.staleCount })}</Tag>
        )}
        {dirty && <Tag color="blue">{t('abstractSchema.unsaved')}</Tag>}
      </Space>

      {schema.status === 'active' ? (
        <Alert
          type="info"
          showIcon
          message={t('abstractSchema.activeNotice')}
          description={t('abstractSchema.activeNoticeBody')}
        />
      ) : (
        <Alert type="warning" showIcon message={t('abstractSchema.proposedNotice')} />
      )}

      <Space wrap>
        <Button
          loading={generating}
          disabled={!canWrite}
          onClick={() => setFocusOpen(true)}
        >
          {t('abstractSchema.regenerate')}
        </Button>
        {/* 已确认（readOnly）时合并也要禁用：勾选 + 合并不看 readOnly 的话，
            本地 state 一变 `dirty` 就为真，「保存草稿」随之恢复可用，一次误点
            就能把已确认的对齐以 `status: 'proposed'` 落库 —— 等于静默撤销确认。 */}
        <Button
          disabled={!canWrite || readOnly || selected.length < 2 || generating}
          onClick={mergeSelected}
        >
          {t('abstractSchema.mergeSelected', { count: selected.length })}
        </Button>
        <Button
          loading={saving}
          disabled={!canWrite || !dirty || generating}
          onClick={() => void doSave('proposed')}
        >
          {t('abstractSchema.saveDraft')}
        </Button>
        <Button
          type="primary"
          loading={saving}
          disabled={!canWrite || schema.status === 'active' || generating}
          onClick={() => void doSave('active')}
        >
          {t('abstractSchema.confirm')}
        </Button>
        <Button onClick={() => void load()}>{t('common.refresh')}</Button>
      </Space>

      {/* 数据资产对齐：文档实体类型 ↔ 数据源实体（人审后 active 才进投影）。
          放在概念卡片列表上方，保持本区紧凑、不改变卡片结构。 */}
      {dataAlignmentSection}

      {entities.length === 0 ? (
        <Empty description={t('abstractSchema.noConcepts')} />
      ) : (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {/* ⚠️ key 用 **uid**（实体的本地身份）。两件都不能改回去：
              ① 不能改回 `key={e.name}` —— 实体名正是这张卡片里的可编辑字段，拿它
                 当 key 的话每敲一个字 key 就跟着变 → React 判定为新元素、卸载重挂
                 整张卡片 → 输入框当场失焦，用户得敲一个字点一次输入框；
              ② 也不能用下标 —— 合并会摘掉被合并的条目、把结果插到最前，下标随之
                 整体错位，React 会把 A 的 DOM 复用到 B 上（改名/删源张冠李戴）。 */}
          {entities.map((e) => (
            <ConceptCard
              key={e.uid}
              uid={e.uid}
              concept={e}
              readOnly={readOnly}
              selected={selected.includes(e.uid)}
              onToggleSelect={toggleSelect}
              onChange={(next) =>
                // 按 uid 定位这次编辑落到哪一条；uid 由这里补回（卡片的 onChange
                // 只经手业务字段），身份不随改名漂移、也不会写错人。
                setEntities((prev) =>
                  prev.map((x) => (x.uid === e.uid ? { ...next, uid: x.uid } : x)),
                )
              }
            />
          ))}
        </Space>
      )}

      <Paragraph type="secondary" style={{ marginBottom: 0 }}>
        {t('abstractSchema.footerHint')}
      </Paragraph>

      {focusModal}
    </Space>
  );
}
