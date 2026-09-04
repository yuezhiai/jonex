/**
 * [jonex] 类型词表编辑（实体/概念通用，表格 + 前端分页，参考本体编译设置页样式）。
 *
 * - code 前端格式校验：小写 ASCII [a-z0-9 _-]，不合格标红拒绝。
 * - kind="entity"：受控词表（编译时超出词表的实体 type 静默回落「其他」）；
 *   删除一个类型 = 该类实体降级为「其他」（不是被清理）。
 * - kind="concept"：无硬校验，仅渲染进 AGENTS.md 作分类引导。
 */
import { Button, Input, Popconfirm, Table } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { LlmWikiConceptTypeItem, LlmWikiEntityTypeItem } from '@/types/domainKnowledge';

const CODE_RE = /^[a-z0-9 _-]+$/;

type Item = LlmWikiEntityTypeItem | LlmWikiConceptTypeItem;

interface Props {
  kind: 'entity' | 'concept';
  value: Item[];
  onChange: (v: Item[]) => void;
}

export default function VocabSection({ kind, value, onChange }: Props) {
  const { t } = useTranslation();

  const update = (idx: number, patch: Partial<Item>) => {
    const next = [...value];
    next[idx] = { ...next[idx], ...patch } as Item;
    onChange(next);
  };
  const remove = (idx: number) => onChange(value.filter((_, i) => i !== idx));
  const add = () => {
    const blank = kind === 'entity'
      ? { code: '', name: '', description: '', examples: [] }
      : { code: '', name: '', description: '' };
    onChange([...value, blank] as Item[]);
  };

  const codeInvalid = (code: string) => !CODE_RE.test(code.trim());
  const hasOther = value.some((v) => v.code.trim() === 'other');
  // 重复 code 集合（trim 后出现次数 >1），用于标红（与 codeInvalid 一致）
  const duplicatedCodes = (() => {
    const counts = new Map<string, number>();
    value.forEach((v) => {
      const c = v.code.trim();
      if (!c) return;
      counts.set(c, (counts.get(c) ?? 0) + 1);
    });
    return new Set([...counts.entries()].filter(([, n]) => n > 1).map(([c]) => c));
  })();

  const columns: ColumnsType<Item> = [
    {
      title: t('llmWikiSchema.code'),
      dataIndex: 'code',
      key: 'code',
      width: 200,
      render: (v: string, _row, idx) => (
        <Input
          value={v}
          status={codeInvalid(v) || duplicatedCodes.has(v.trim()) ? 'error' : undefined}
          onChange={(e) => update(idx, { code: e.target.value.trim() })}
        />
      ),
    },
    {
      title: t('llmWikiSchema.name'),
      dataIndex: 'name',
      key: 'name',
      width: 200,
      render: (v: string, _row, idx) => (
        <Input
          value={v}
          onChange={(e) => update(idx, { name: e.target.value })}
        />
      ),
    },
    {
      title: t('llmWikiSchema.description'),
      dataIndex: 'description',
      key: 'description',
      render: (v: string, _row, idx) => (
        <Input
          value={v}
          onChange={(e) => update(idx, { description: e.target.value })}
        />
      ),
    },
    {
      title: t('llmWikiSchema.actionCol'),
      key: 'action',
      width: 80,
      render: (_v, _row, idx) =>
        kind === 'entity' ? (
          <Popconfirm
            title={t('llmWikiSchema.deleteTypeConfirm')}
            description={t('llmWikiSchema.deleteTypeDesc')}
            onConfirm={() => remove(idx)}
            okText={t('common.confirm')}
            cancelText={t('common.cancel')}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        ) : (
          <Button size="small" danger icon={<DeleteOutlined />} onClick={() => remove(idx)} />
        ),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <p style={{ fontSize: 13, color: '#64748b', margin: 0, flex: 1 }}>
          {kind === 'entity'
            ? t('llmWikiSchema.entityTypeHint')
            : t('llmWikiSchema.conceptTypeHint')}
          {kind === 'entity' && !hasOther && (
            <span style={{ color: '#ef4444' }}>
              {' '}
              {t('llmWikiSchema.otherRequired')}
            </span>
          )}
        </p>
        <Button type="primary" icon={<PlusOutlined />} size="small" onClick={add}>
          {t('llmWikiSchema.addType')}
        </Button>
      </div>
      <Table<Item>
        columns={columns}
        dataSource={value}
        rowKey={(_, idx) => String(idx ?? 0)}
        size="middle"
        pagination={{ pageSize: 10, showSizeChanger: false, hideOnSinglePage: false }}
        locale={{ emptyText: t('llmWikiSchema.emptyTypes') }}
      />
    </div>
  );
}
