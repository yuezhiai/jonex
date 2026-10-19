import React from 'react';
import { useTranslation } from 'react-i18next';
import { ThoughtChain } from '@ant-design/x';
import type { ThoughtChainItemType } from '@ant-design/x';
import {
  CaretDownOutlined,
  CaretRightOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  LoadingOutlined,
  MinusCircleFilled,
  NodeIndexOutlined,
} from '@ant-design/icons';
import { toThoughtStatus } from './thoughtStatus';

/** 只声明渲染用得上的字段，避免把后端整包 detail 结构绑进这一层 */
export interface ReasoningStepLike {
  stage: string;
  title: string;
  status: string;
  summary?: string | null;
  duration_ms?: number | null;
  detail?: Record<string, unknown> | null;
}

/** `status.*` 这套 i18n key 只覆盖这四种；其余状态直接显示原文 */
const KNOWN_STATUS = ['running', 'done', 'skipped', 'failed'];

const STATUS_ICON: Record<string, React.ReactNode> = {
  loading: <LoadingOutlined style={{ color: '#3b82f6' }} />,
  success: <CheckCircleFilled style={{ color: '#10b981' }} />,
  error: <CloseCircleFilled style={{ color: '#ef4444' }} />,
  skipped: <MinusCircleFilled style={{ color: '#94a3b8' }} />,
};

const CHIP_TONE: Record<string, { color: string; bg: string }> = {
  loading: { color: '#3b82f6', bg: '#eff6ff' },
  success: { color: '#10b981', bg: '#ecfdf5' },
  error: { color: '#ef4444', bg: '#fef2f2' },
  skipped: { color: '#94a3b8', bg: '#f1f5f9' },
};

interface ThoughtStepsProps {
  steps: ReasoningStepLike[];
  totalMs?: number | null;
  finalSource?: string;
  /** 整块推理链的展开态，由页面持有（一次检索一张卡片，只有一份） */
  expanded: boolean;
  onToggle: () => void;
  /** 步骤详情交由调用方渲染：那块内容依赖实体抽屉/召回抽屉，不属于本组件 */
  renderDetail: (step: ReasoningStepLike) => React.ReactNode;
}

export default function ThoughtSteps({
  steps,
  totalMs,
  finalSource,
  expanded,
  onToggle,
  renderDetail,
}: ThoughtStepsProps) {
  const { t } = useTranslation();
  if (steps.length === 0) return null;

  const items: ThoughtChainItemType[] = steps.map((step, index) => {
    const { status, skipped } = toThoughtStatus(step.status);
    const toneKey = skipped ? 'skipped' : status;
    const tone = CHIP_TONE[toneKey];
    // 与 toThoughtStatus 同样只认四个已知状态，其余原样透出（后端加了新状态时不至于变成空白）
    const chipLabel = KNOWN_STATUS.includes(step.status) ? t(`status.${step.status}`) : step.status;
    return {
      key: `${step.stage}-${index}`,
      status,
      icon: STATUS_ICON[toneKey],
      // ⚠️ 不设 collapsible：步骤详情一直可见，与替换前的行为一致。
      // ThoughtChain 在这里给的是状态图标 + 连接线，不是新的折叠层级。
      content: renderDetail(step),
      title: (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: '#334155' }}>{step.title}</span>
          <span style={{ fontSize: 11, color: tone.color, background: tone.bg, borderRadius: 4, padding: '0 6px' }}>
            {chipLabel}
          </span>
          {step.duration_ms != null && <span style={{ fontSize: 11, color: '#a3adbb' }}>{step.duration_ms}ms</span>}
        </span>
      ),
      description: step.summary ? (
        <span style={{ fontSize: 12, color: '#64748b', lineHeight: 1.6 }}>{step.summary}</span>
      ) : undefined,
    };
  });

  return (
    <div
      style={{
        background: '#fbfaff',
        border: '1px solid #ece9fb',
        borderRadius: 10,
        padding: '12px 16px',
        marginBottom: 16,
      }}
    >
      <button
        onClick={onToggle}
        style={{
          width: '100%',
          padding: 0,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          justifyContent: 'flex-start',
          height: 'auto',
          lineHeight: 'inherit',
          border: 'none',
          background: 'transparent',
          cursor: 'pointer',
          fontSize: 13,
        }}
      >
        {expanded ? <CaretDownOutlined /> : <CaretRightOutlined />}
        <NodeIndexOutlined style={{ color: '#8b5cf6' }} />
        <span style={{ fontSize: 13, fontWeight: 600, color: '#6d28d9' }}>
          {t('knowledgeSearch.reasoningProcess')}
        </span>
        <span style={{ fontSize: 12, color: '#a78bda' }}>
          {t('knowledgeSearch.steps', { count: steps.length, source: finalSource })}
          {totalMs != null ? ` · ${totalMs}ms` : ''}
        </span>
      </button>
      {expanded && (
        <div style={{ marginTop: 12 }}>
          <ThoughtChain items={items} line="dashed" />
        </div>
      )}
    </div>
  );
}
