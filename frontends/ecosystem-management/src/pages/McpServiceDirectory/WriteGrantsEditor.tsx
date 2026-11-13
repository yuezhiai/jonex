import React, { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Select, Space, Tag, Typography } from 'antd';
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import type { WriteGrant } from '@/api/mcpKeys';
import type { KnowledgeBaseBrief } from '@/api/spaces';

interface WriteGrantsEditorProps {
  /** 受控值：grants[] */
  value?: WriteGrant[];
  onChange?: (grants: WriteGrant[]) => void;
  /** 知识库列表（含 space_id），由父级加载传入（提交时做同 space 校验） */
  kbList: KnowledgeBaseBrief[];
  /** 领域空间 ID → 名称（同 space 约束提示展示） */
  spaceNameMap?: Record<string, string>;
  /** 禁用整个编辑器 */
  disabled?: boolean;
  /** Key 自身领域空间（创建态=全局空间，编辑态=原 Key 空间），约束 KB 下拉同空间过滤 */
  spaceId?: string | null;
}

/** 写入范围编辑器：按知识库授权，整库可写（不再提供写入模式 / 目录级选择）。
 *
 * - 同 space 约束：以 Key 自身领域空间（spaceId）为参照，过滤 KB 选择器选项；
 *   空间缺失（历史无空间 Key）时回退到第一个已选 KB 的空间；
 * - 后端 WriteGrant 仍要求 mode/directories 字段，这里统一按「整库」提交
 *   （mode='all'、directories=[]），历史 specified 数据在保存后收敛为整库。
 */
export default function WriteGrantsEditor({ value, onChange, kbList, spaceNameMap, disabled, spaceId }: WriteGrantsEditorProps) {
  const { t } = useTranslation();
  const grants = useMemo<WriteGrant[]>(() => value ?? [], [value]);

  // 知识库 id → 名称 映射（选项 label 用）
  const kbNameMap = useMemo(() => {
    const m = new Map<string, string>();
    kbList.forEach((k) => m.set(k.id, k.name));
    return m;
  }, [kbList]);

  const updateGrant = (index: number, patch: Partial<WriteGrant>) => {
    onChange?.(grants.map((g, i) => (i === index ? { ...g, ...patch } : g)));
  };

  const addGrant = () => {
    onChange?.([...grants, { kb: '', mode: 'all', directories: [] }]);
  };

  const removeGrant = (index: number) => {
    onChange?.(grants.filter((_, i) => i !== index));
  };

  /** 参照空间：优先 Key 自身领域空间（spaceId）；为空（历史无空间 Key）回退第一个已选 KB 的空间 */
  const refSpace = useMemo(() => {
    if (spaceId) return spaceId;
    const firstKb = grants.find((g) => g.kb)?.kb;
    return kbList.find((k) => k.id === firstKb)?.space_id ?? null;
  }, [spaceId, grants, kbList]);

  /** KB 选择器选项：参照空间过滤 + 排除其他行已占用 KB + 保留当前行已选值 */
  const kbOptionsFor = (index: number, currentKb: string) => {
    // 其他行已占用的知识库 ID（排除当前行自身、kb 非空才算占用）
    const usedKbIds = grants
      .filter((g, i) => i !== index && g.kb)
      .map((g) => g.kb);

    const options = kbList
      .filter((k) => !refSpace || k.space_id === refSpace) // 原同空间过滤
      .filter((k) => !usedKbIds.includes(k.id)) // 新增：排除其他行占用
      .map((k) => {
        const spaceName = k.space_id ? spaceNameMap?.[k.space_id] : null;
        return {
          value: k.id,
          label: spaceName ? `${k.name}（${spaceName}）` : k.name,
        };
      });
    // ⚠️ append 兜底块必须原样保留：从 kbList 全集查找、不受同空间 filter 限制，
    // 覆盖跨空间回填（currentKb 属其他空间时仍能显示）与 currentKb 恰被 usedKbIds
    // 命中（编辑回填已有行时仍能显示）两种情况。
    if (currentKb && !options.some((o) => o.value === currentKb)) {
      const cur = kbList.find((k) => k.id === currentKb);
      if (cur) {
        options.unshift({ value: cur.id, label: cur.name });
      }
    }
    return options;
  };

  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        <Space size={4} wrap>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t('mcpKeyManagement.grantsHint')}
          </Typography.Text>
          {refSpace && spaceNameMap?.[refSpace] && (
            <Tag color="blue" style={{ fontSize: 12 }}>
              {spaceNameMap[refSpace]}
            </Tag>
          )}
        </Space>
      </div>

      {grants.length === 0 ? (
        <Typography.Text type="secondary">{t('mcpKeyManagement.grantsEmpty')}</Typography.Text>
      ) : (
        grants.map((g, index) => (
          <div className="mcp-grant-item" key={index}>
            <div className="mcp-grant-item-head">
              <Typography.Text strong>{t('mcpKeyManagement.grantItem', { index: index + 1 })}</Typography.Text>
              {!disabled && (
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => removeGrant(index)}
                />
              )}
            </div>
            <Select
              style={{ width: '100%' }}
              placeholder={t('mcpKeyManagement.kbPlaceholder')}
              value={g.kb || undefined}
              disabled={disabled}
              onChange={(kb) => updateGrant(index, { kb, mode: 'all', directories: [] })}
              options={kbOptionsFor(index, g.kb)}
              showSearch
              optionFilterProp="label"
            />
          </div>
        ))
      )}

      {!disabled && (
        <Button type="dashed" block icon={<PlusOutlined />} onClick={addGrant}>
          {t('mcpKeyManagement.addGrant')}
        </Button>
      )}

      {/* 提示当前选择的 KB 名称（辅助确认写入范围） */}
      {grants.length > 0 && (
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
          {grants
            .map((g, i) => `${i + 1}. ${g.kb ? kbNameMap.get(g.kb) ?? g.kb : t('mcpKeyManagement.kbUnselected')}`)
            .join('；')}
        </Typography.Paragraph>
      )}
    </div>
  );
}
