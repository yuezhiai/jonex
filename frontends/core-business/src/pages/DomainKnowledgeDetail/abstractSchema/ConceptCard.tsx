/**
 * 一个抽象实体的编辑卡片：改名、改别名、落点勾选纳入、删源、删除实体。
 *
 * v2：形状已是 `AbstractEntity`（落点带 `included`）；属性与关系本卡片不经手，
 * 一律随 `{...concept}` 原样带回（`onChange` 的每一次编辑都是整份实体替换）。
 *
 * 「合并」不在这里 —— 它是**两个**实体之间的操作，挂在页签的选中态上
 * （见 AbstractSchemaTab 的工具栏）。
 */
import { Button, Card, Empty, Input, Popconfirm, Select, Space, Tag, Tooltip, Typography } from 'antd';
import { DeleteOutlined, WarningOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { AbstractEntity, AbstractLanding } from '@/api/abstractSchema';

const { Text } = Typography;

interface Props {
  /** 实体（props 名保持 `concept`，减少 diff；形状已升 v2 `AbstractEntity`）。 */
  concept: AbstractEntity;
  /** 实体在界面上的稳定身份（本地生成，服务端不认识）。卡片只是把它原样递回 ——
   *  不能拿 `concept.name` 当身份：它是下面的可编辑字段，用户改个名就认不出来了。 */
  uid: string;
  readOnly: boolean;
  selected: boolean;
  onToggleSelect: (uid: string) => void;
  onChange: (next: AbstractEntity) => void;
}

export default function ConceptCard({
  concept, uid, readOnly, selected, onToggleSelect, onChange,
}: Props) {
  const { t } = useTranslation();

  const setAliases = (aliases: string[]) => onChange({ ...concept, aliases });

  /** 翻转一条落点的 `included`（纳入/不纳入）。缺省视为纳入（与后端「缺省即纳入」同口径）。 */
  const toggleLandingIncluded = (dataSourceId: string, entityName: string) =>
    onChange({
      ...concept,
      sources: concept.sources.map((x) =>
        x.dataSourceId === dataSourceId && x.entityName === entityName
          ? { ...x, included: x.included === false }
          : x,
      ),
    });

  /**
   * 源映射的显示名。`sourceName` 缺失时**不能**直接把 `dataSourceId` 摊给用户看：
   *
   * - `source_gone`（数据源被删）= 后端按设计回 `source_name: null`，正是「标脏」
   *   这套设计存在的唯一场景 —— 用户在这个位置必须读到「源没了」，而不是一串
   *   32 位十六进制 id（他没法照着它做任何事）。标签复用旁边那颗失效标记的同一
   *   条文案（`staleSourceGone`）：同一行里同一个意思不该有两种说法。
   * - 其余情形（老数据 / 后端未富化）保留原样回退到 id：那时它至少还是一把能
   *   拿去对账的钥匙。
   */
  const sourceLabel = (s: AbstractLanding) => {
    if (s.sourceName) return s.sourceName;
    if (s.staleReason === 'source_gone') return t('abstractSchema.staleSourceGone');
    return s.dataSourceId;
  };

  return (
    <Card
      size="small"
      className="abstract-concept-card"
      style={selected ? { borderColor: '#1677ff' } : undefined}
      title={
        <Space>
          {/* 已确认（readOnly）时勾选也要一起禁用：这个复选框是「合并所选」的唯一入口，
              漏掉它的话卡片里的输入控件虽然都灰了，用户仍能勾选 + 合并把本地 state 弄脏，
              进而解锁「保存草稿」、把已确认的对齐降级回 proposed。 */}
          <input
            type="checkbox"
            checked={selected}
            disabled={readOnly}
            aria-label={t('abstractSchema.selectConcept')}
            onChange={() => onToggleSelect(uid)}
          />
          <Input
            value={concept.name}
            disabled={readOnly}
            maxLength={128}
            style={{ width: 200 }}
            onChange={(e) => onChange({ ...concept, name: e.target.value })}
          />
          <Text type="secondary">
            {t('abstractSchema.sourceCount', { count: concept.sources.length })}
          </Text>
        </Space>
      }
      extra={
        <Popconfirm
          title={t('abstractSchema.deleteConceptConfirm', { name: concept.name })}
          onConfirm={() => onChange({ ...concept, sources: [], aliases: [] })}
          disabled={readOnly}
        >
          <Button type="text" danger size="small" icon={<DeleteOutlined />} disabled={readOnly} />
        </Popconfirm>
      }
    >
      <Space direction="vertical" style={{ width: '100%' }} size={8}>
        <Input.TextArea
          value={concept.description}
          disabled={readOnly}
          autoSize={{ minRows: 1, maxRows: 3 }}
          placeholder={t('abstractSchema.descriptionPlaceholder')}
          onChange={(e) => onChange({ ...concept, description: e.target.value })}
        />

        <Select
          mode="tags"
          value={concept.aliases}
          disabled={readOnly}
          style={{ width: '100%' }}
          placeholder={t('abstractSchema.aliasesPlaceholder')}
          onChange={setAliases}
        />

        {concept.sources.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={t('abstractSchema.noSourceWarning')}
          />
        ) : (
          <Space direction="vertical" size={4} style={{ width: '100%' }}>
            {concept.sources.map((s) => (
              <Space
                key={`${s.dataSourceId}:${s.entityName}`}
                size={6}
                // 不纳入（included=false）的落点行半透明：对齐事实仍留在卡片里
                // （重新生成时不用从头再找），但视觉上要能一眼区分它不生效。
                style={s.included === false ? { opacity: 0.45 } : undefined}
              >
                <input
                  type="checkbox"
                  checked={s.included !== false}
                  disabled={readOnly}
                  onChange={() => toggleLandingIncluded(s.dataSourceId, s.entityName)}
                />
                <Tag>{sourceLabel(s)}</Tag>
                <Text code>{s.entityName}</Text>
                {s.stale && (
                  <Tooltip
                    title={
                      s.staleReason === 'source_gone'
                        ? t('abstractSchema.staleSourceGone')
                        : t('abstractSchema.staleEntityGone')
                    }
                  >
                    <Tag icon={<WarningOutlined />} color="warning">
                      {t('abstractSchema.stale')}
                    </Tag>
                  </Tooltip>
                )}
                <Button
                  type="link"
                  size="small"
                  disabled={readOnly}
                  onClick={() =>
                    onChange({
                      ...concept,
                      sources: concept.sources.filter(
                        (x) => !(x.dataSourceId === s.dataSourceId && x.entityName === s.entityName),
                      ),
                    })
                  }
                >
                  {t('abstractSchema.removeSource')}
                </Button>
              </Space>
            ))}
          </Space>
        )}
      </Space>
    </Card>
  );
}
