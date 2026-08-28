import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, Select, Radio, Space, Spin, Tag, Typography } from 'antd';
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import type { WriteGrant } from '@/api/mcpKeys';
import { listKbFolders, type KbFolderItem, type KnowledgeBaseBrief } from '@/api/spaces';

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
}

/** 写入范围编辑器（WRITE-02）：多个知识库，每个一项 { kb, mode(all|specified), directories[] }。
 *
 * - 同 space 约束：以第一个已选 KB 的空间为参照，过滤后续 KB 选择器选项；
 * - all 模式不展示目录多选（切换时清空 directories）；
 * - specified 模式按需加载该 KB 的目录做多选。
 */
export default function WriteGrantsEditor({ value, onChange, kbList, spaceNameMap, disabled }: WriteGrantsEditorProps) {
  const { t } = useTranslation();
  const grants: WriteGrant[] = value ?? [];
  const [folderMap, setFolderMap] = useState<Record<string, KbFolderItem[]>>({});
  const [loadingFolders, setLoadingFolders] = useState<Record<string, boolean>>({});

  // 知识库 id → 名称 映射（选项 label 用）
  const kbNameMap = useMemo(() => {
    const m = new Map<string, string>();
    kbList.forEach((k) => m.set(k.id, k.name));
    return m;
  }, [kbList]);

  // 按需加载每个 specified 模式 grant 的 KB 目录（编辑回填时也触发）
  useEffect(() => {
    grants.forEach((g) => {
      if (g.mode === 'specified' && g.kb && !folderMap[g.kb] && !loadingFolders[g.kb]) {
        setLoadingFolders((prev) => ({ ...prev, [g.kb]: true }));
        listKbFolders(g.kb)
          .then((items) => setFolderMap((prev) => ({ ...prev, [g.kb]: items })))
          .catch(() => setFolderMap((prev) => ({ ...prev, [g.kb]: [] })))
          .finally(() => setLoadingFolders((prev) => ({ ...prev, [g.kb]: false })));
      }
    });
  }, [grants, folderMap, loadingFolders]);

  /** 更新第 index 个 grant（all ↔ specified 切换时清空/保底 directories） */
  const updateGrant = (index: number, patch: Partial<WriteGrant>) => {
    const next = grants.map((g, i) => (i === index ? { ...g, ...patch } : g));
    // mode 切换到 all：目录必须清空（后端 400）
    if (patch.mode === 'all') {
      next[index] = { ...next[index], directories: [] };
    }
    onChange?.(next);
  };

  const addGrant = () => {
    onChange?.([...grants, { kb: '', mode: 'all', directories: [] }]);
  };

  const removeGrant = (index: number) => {
    onChange?.(grants.filter((_, i) => i !== index));
  };

  /** 参照空间：第一个已选 KB 的 space_id（未选时为空 → 不约束） */
  const refSpace = useMemo(() => {
    const firstKb = grants.find((g) => g.kb)?.kb;
    return kbList.find((k) => k.id === firstKb)?.space_id ?? null;
  }, [grants, kbList]);

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
        grants.map((g, index) => {
          const folders = folderMap[g.kb] ?? [];
          const loading = !!loadingFolders[g.kb];
          return (
            <Card
              key={index}
              size="small"
              style={{ marginBottom: 12 }}
              title={
                <Space size={8}>
                  <Typography.Text>{t('mcpKeyManagement.grantItem', { index: index + 1 })}</Typography.Text>
                  {g.mode === 'all' ? (
                    <Tag color="success">{t('mcpKeyManagement.modeAll')}</Tag>
                  ) : (
                    <Tag color="processing">{t('mcpKeyManagement.modeSpecified')}</Tag>
                  )}
                </Space>
              }
              extra={
                !disabled && (
                  <Button
                    type="text"
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => removeGrant(index)}
                  />
                )
              }
            >
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <div>
                  <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 4 }}>
                    {t('mcpKeyManagement.kbLabel')}
                  </Typography.Text>
                  <Select
                    style={{ width: '100%' }}
                    placeholder={t('mcpKeyManagement.kbPlaceholder')}
                    value={g.kb || undefined}
                    disabled={disabled}
                    onChange={(kb) => updateGrant(index, { kb, directories: [] })}
                    options={kbOptionsFor(index, g.kb)}
                    showSearch
                    optionFilterProp="label"
                  />
                </div>
                <div>
                  <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 4 }}>
                    {t('mcpKeyManagement.modeLabel')}
                  </Typography.Text>
                  <Radio.Group
                    value={g.mode}
                    disabled={disabled}
                    onChange={(e) => updateGrant(index, { mode: e.target.value })}
                  >
                    <Radio value="all">{t('mcpKeyManagement.modeAllDesc')}</Radio>
                    <Radio value="specified">{t('mcpKeyManagement.modeSpecifiedDesc')}</Radio>
                  </Radio.Group>
                </div>
                {g.mode === 'specified' && (
                  <div>
                    <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 4 }}>
                      {t('mcpKeyManagement.directoriesLabel')}
                    </Typography.Text>
                    {loading ? (
                      <Spin size="small" />
                    ) : (
                      <Select
                        mode="multiple"
                        style={{ width: '100%' }}
                        placeholder={t('mcpKeyManagement.directoriesPlaceholder')}
                        value={g.directories}
                        disabled={disabled || !g.kb}
                        onChange={(dirs) => updateGrant(index, { directories: dirs })}
                        options={folders.map((f: KbFolderItem) => ({ value: f.id, label: f.name }))}
                        maxTagCount={3}
                        notFoundContent={t('mcpKeyManagement.directoriesEmpty')}
                      />
                    )}
                  </div>
                )}
              </Space>
            </Card>
          );
        })
      )}

      {!disabled && (
        <Button type="dashed" block icon={<PlusOutlined />} onClick={addGrant}>
          {t('mcpKeyManagement.addGrant')}
        </Button>
      )}

      {/* 提示当前选择的 KB 名称（辅助确认写入范围） */}
      {grants.length > 0 && (
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
          {grants.map((g, i) => `${i + 1}. ${g.kb ? kbNameMap.get(g.kb) ?? g.kb : t('mcpKeyManagement.kbUnselected')}（${g.mode === 'all' ? t('mcpKeyManagement.modeAll') : `${g.directories.length} ${t('mcpKeyManagement.directoryUnit')}`}）`).join('；')}
        </Typography.Paragraph>
      )}
    </div>
  );
}
