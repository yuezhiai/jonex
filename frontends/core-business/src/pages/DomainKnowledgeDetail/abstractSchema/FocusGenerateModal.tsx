/**
 * 「聚焦生成」两阶段弹窗：① 提示词 →「解析」→ ② 种子确认（可删）+ 邻居预览 →
 * 「确认生成」。
 *
 * 分工：本组件只管**弹窗内的流程与展示**；真正的生成（含 409 force 二次确认、
 * 生成后重新 GET、toast）在父组件 `AbstractSchemaTab.doGenerate` 里 —— 那条路径
 * 已是既有的、经过验证的，不要在这里复制一份。
 *
 * 三个关键语义：
 * - **解析失败留窗**（含「没有解析出相关实体」）：绝不静默回退全量 —— 用户点了
 *   聚焦再悄悄生成全量，比报错更难发现；
 * - 「全量生成（不聚焦）」是一个显式按钮，不是「留空自动兜底」：留空时「解析」
 *   按钮就禁用，两条路不靠运气区分；
 * - 生成时（`generating`）全窗禁操作：防与保存/确认抢 CAS。
 */
import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Checkbox, Collapse, Input, Modal, Segmented, Space, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  previewFocusAbstractSchema,
  type FocusPreview,
  type FocusSeed,
  type FocusSeedInput,
} from '@/api/abstractSchema';
import { buildNeighborGroups } from './focusModel';

const { Text, Paragraph } = Typography;

interface Props {
  open: boolean;
  kbId: string;
  /** 父组件生成中（确认/全量两条路都走它）。 */
  generating: boolean;
  onClose: () => void;
  /**
   * 提交生成：`seeds` 缺省 = 全量生成；`mode` = 全部重新构造 / 追加；
   * 返回是否成功（成功时父组件已关窗）。
   */
  onGenerate: (
    seeds?: FocusSeedInput[],
    options?: {
      mode: 'replace' | 'append';
      seed: 'document' | 'structured';
      autoLandings: boolean;
    },
  ) => Promise<boolean>;
}

export default function FocusGenerateModal({
  open, kbId, generating, onClose, onGenerate,
}: Props) {
  const { t } = useTranslation();
  const [step, setStep] = useState<'input' | 'confirm'>('input');
  const [prompt, setPrompt] = useState('');
  const [parsing, setParsing] = useState(false);
  const [preview, setPreview] = useState<FocusPreview | null>(null);
  const [seeds, setSeeds] = useState<FocusSeed[]>([]);
  const [parseError, setParseError] = useState('');
  const [mode, setMode] = useState<'replace' | 'append'>('replace');
  /** 复用文档本体（KB 级 compiled schema）作为构造种子。 */
  const [reuseDocument, setReuseDocument] = useState(true);
  const [autoLandings, setAutoLandings] = useState(false);

  // 每次打开重置：上一次的解析结果不能泄漏到这一次（提示词会不同）
  useEffect(() => {
    if (open) {
      setStep('input');
      setPrompt('');
      setPreview(null);
      setSeeds([]);
      setParseError('');
      setMode('replace');
      setReuseDocument(true);
      setAutoLandings(false);
    }
  }, [open]);

  const groups = useMemo(() => buildNeighborGroups(preview?.neighbors ?? []), [preview]);

  const doParse = async () => {
    setParsing(true);
    setParseError('');
    try {
      const data = await previewFocusAbstractSchema(kbId, prompt.trim());
      setPreview(data);
      setSeeds(data.seeds);
      setStep('confirm');
    } catch (e: any) {
      // 留窗改词：一律优先透出服务端 message —— 文案按请求语言（X-Lang，共享请求层
      // 自动注入）在服务端渲染，比本地文案更精确。同为 1001 的两支（「没有解析出
      // 相关实体」focus_empty 与「没有任何已生成 schema 的数据源」no_source_schema）
      // 靠它区分；按 1001 特判会把后者误显示成前者。服务端没给 message 才回落地兜底。
      setParseError(e?.message || t('abstractSchema.focus.parseFailed'));
    } finally {
      setParsing(false);
    }
  };

  const doConfirm = async () => {
    if (seeds.length === 0) return;
    await onGenerate(
      seeds.map((s) => ({
        dataSourceId: s.dataSourceId,
        entityName: s.entityName,
        role: s.role,
      })),
      { mode, seed: reuseDocument ? 'document' : 'structured', autoLandings },
    );
  };

  const busy = parsing || generating;

  return (
    <Modal
      open={open}
      title={t('abstractSchema.focus.title')}
      width={720}
      maskClosable={false}
      closable={!busy}
      keyboard={!busy}
      onCancel={onClose}
      styles={{ body: { maxHeight: '60vh', overflowY: 'auto' } }}
      footer={
        step === 'input'
          ? [
              <Button key="cancel" onClick={onClose} disabled={busy}>
                {t('common.cancel')}
              </Button>,
              <Button
                key="full"
                onClick={() => void onGenerate(undefined, {
                  mode,
                  seed: reuseDocument ? 'document' : 'structured',
                  autoLandings,
                })}
                loading={generating}
                disabled={parsing}
              >
                {t('abstractSchema.focus.skipToFull')}
              </Button>,
              <Button
                key="parse"
                type="primary"
                loading={parsing}
                disabled={!prompt.trim() || generating}
                onClick={() => void doParse()}
              >
                {t('abstractSchema.focus.parse')}
              </Button>,
            ]
          : [
              <Button
                key="reparse"
                onClick={() => {
                  setStep('input');
                  setPreview(null);
                  setParseError('');
                }}
                disabled={generating}
              >
                {t('abstractSchema.focus.reparse')}
              </Button>,
              <Button
                key="ok"
                type="primary"
                loading={generating}
                disabled={seeds.length === 0 || generating}
                onClick={() => void doConfirm()}
              >
                {t('abstractSchema.focus.confirm')}
              </Button>,
            ]
      }
    >
      {step === 'input' ? (
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Paragraph type="secondary" style={{ marginBottom: 0 }}>
            {t('abstractSchema.focus.intro')}
          </Paragraph>
          <Space direction="vertical" size={4}>
            <Text strong>{t('abstractSchema.focus.modeTitle')}</Text>
            <Segmented
              value={mode}
              onChange={(value) => setMode(value as 'replace' | 'append')}
              options={[
                { value: 'replace', label: t('abstractSchema.focus.modeReplace') },
                { value: 'append', label: t('abstractSchema.focus.modeAppend') },
              ]}
              disabled={busy}
            />
            <Checkbox
              checked={reuseDocument}
              disabled={busy}
              onChange={(event) => setReuseDocument(event.target.checked)}
            >
              {t('abstractSchema.focus.reuseDocument')}
            </Checkbox>
            <Text type="secondary">{t('abstractSchema.focus.reuseDocumentHint')}</Text>
            <Checkbox
              checked={autoLandings}
              disabled={busy || !reuseDocument}
              onChange={(event) => setAutoLandings(event.target.checked)}
            >
              {t('abstractSchema.focus.autoLandings')}
            </Checkbox>
            <Text type="secondary">{t('abstractSchema.focus.autoLandingsHint')}</Text>
          </Space>
          <Input.TextArea
            rows={3}
            value={prompt}
            maxLength={500}
            showCount
            placeholder={t('abstractSchema.focus.promptPlaceholder')}
            onChange={(e) => setPrompt(e.target.value)}
            disabled={busy}
          />
          {parseError && <Alert type="error" showIcon message={parseError} />}
        </Space>
      ) : (
        preview && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {preview.warnings.length > 0 && (
              <Alert type="warning" showIcon message={preview.warnings.join('；')} />
            )}
            <Text strong>{t('abstractSchema.focus.seedsTitle')}</Text>
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              {seeds.map((s, i) => (
                <Space key={`${s.dataSourceId}/${s.entityName}`} wrap>
                  <Tag color={s.role === 'partner' ? 'purple' : 'blue'}>
                    {s.role === 'partner'
                      ? t('abstractSchema.focus.seedPartnerTag')
                      : t('abstractSchema.focus.seedFocusTag')}
                  </Tag>
                  <Text strong>{s.entityName}</Text>
                  <Text type="secondary">{s.sourceName || s.dataSourceId}</Text>
                  {s.description && <Text type="secondary">· {s.description}</Text>}
                  <Button
                    type="text"
                    danger
                    size="small"
                    disabled={generating}
                    onClick={() => setSeeds((prev) => prev.filter((_, idx) => idx !== i))}
                  >
                    ×
                  </Button>
                </Space>
              ))}
            </Space>
            {seeds.length === 0 && (
              <Text type="danger">{t('abstractSchema.focus.needSeed')}</Text>
            )}

            <Text strong>{t('abstractSchema.focus.neighborsTitle')}</Text>
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              {t('abstractSchema.focus.neighborsHint')}
            </Paragraph>
            {groups.length === 0 ? (
              <Text type="secondary">{t('abstractSchema.focus.neighborsEmpty')}</Text>
            ) : (
              <Collapse
                size="small"
                items={groups.map((g) => ({
                  key: g.relationName,
                  label: `${t('abstractSchema.focus.viaLabel', { relation: g.relationName })}（${g.items.length}）`,
                  children: (
                    <Space wrap>
                      {g.items.map((it) => (
                        <Tag key={`${it.dataSourceId}/${it.entityName}`}>
                          {it.entityName}（{it.sourceName || it.dataSourceId}）
                        </Tag>
                      ))}
                    </Space>
                  ),
                }))}
              />
            )}
          </Space>
        )
      )}
    </Modal>
  );
}
