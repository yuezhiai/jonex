import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  ApartmentOutlined,
  BranchesOutlined,
  BulbOutlined,
  CaretDownOutlined,
  CaretRightOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  ReadOutlined,
  ToolOutlined,
} from '@ant-design/icons';
import type { ReasoningIOItem } from '@/types/knowledgeSearch';
import { ReasoningRawJson, ReasoningStepIO } from './ReasoningStepIO';
import { fmtTraceMs, TraceCard, TraceChip, TraceShell, traceStyles, type TraceTone } from './ReasoningCard';

/** 只声明渲染用得上的字段，避免把后端整包 detail 结构绑进这一层 */
export interface ReasoningStepLike {
  stage: string;
  title: string;
  status: 'running' | 'done' | 'skipped' | 'failed' | 'degraded';
  summary?: string | null;
  duration_ms?: number | null;
  detail?: Record<string, unknown> | null;
  /** 展示型输入/输出（已脱敏；旧历史快照可能缺失） */
  input?: ReasoningIOItem[] | null;
  output?: ReasoningIOItem[] | null;
}

/** `status.*` 这套 i18n key 只覆盖这四种；其余状态直接显示原文 */
const KNOWN_STATUS = ['running', 'done', 'skipped', 'failed'];

/** stage → 图标底块语义色 + 图标；同一套语义色与 wiki 链共用（见 ReasoningCard） */
const STAGE_VISUAL: Array<{ test: RegExp; tone: TraceTone; icon: React.ReactNode }> = [
  { test: /intent|rewrite|clarif/i, tone: 'intent', icon: <BulbOutlined /> },
  { test: /match|entit|ontology/i, tone: 'match', icon: <ApartmentOutlined /> },
  { test: /route|router|dispatch/i, tone: 'route', icon: <BranchesOutlined /> },
  { test: /fact|inverse|stub/i, tone: 'fact', icon: <DatabaseOutlined /> },
  { test: /context|recall|rag|retriev/i, tone: 'read', icon: <ReadOutlined /> },
  { test: /wiki|openkb|synth|answer/i, tone: 'synth', icon: <FileTextOutlined /> },
];

const FALLBACK_VISUAL = { tone: 'plan' as TraceTone, icon: <ToolOutlined /> };

function stageVisual(stage: string) {
  return STAGE_VISUAL.find((item) => item.test.test(stage)) ?? FALLBACK_VISUAL;
}

/** 检索策略 chip 文案：兼容后端 llm_wiki / jonex_wiki 两种写法。 */
function strategyLabel(t: (key: string) => string, value?: string | null): string {
  const raw = String(value || '').toLowerCase();
  if (!raw) return '—';
  const key = raw.replace(/_/g, '-');
  const known = new Set(['mixed', 'ontology', 'rag', 'llm-wiki', 'jonex-wiki', 'none']);
  return known.has(key) ? t(`knowledgeSearch.strategy_${key}`) : String(value);
}

interface ThoughtStepsProps {
  steps: ReasoningStepLike[];
  totalMs?: number | null;
  finalSource?: string;
  /** 实际检索策略（mixed / ontology / rag / …）；旧快照缺失时回退 finalSource */
  strategy?: string | null;
  /** 整份 trace 因体积被服务端裁剪过 */
  ioTruncated?: boolean;
  /** 整块推理链的展开态，由页面持有（一次检索一张卡片，只有一份） */
  expanded: boolean;
  onToggle: () => void;
  /** 步骤详情交由调用方渲染：那块内容依赖实体抽屉/召回抽屉，不属于本组件 */
  renderDetail: (step: ReasoningStepLike) => React.ReactNode;
}

/**
 * 本体 / RAG 链的推理过程：外壳与编号卡与 wiki 链统一（方案 C），
 * 卡片正文保留各自的详情 chips 与步骤级「输入 / 输出」。
 */
export default function ThoughtSteps({
  steps,
  totalMs,
  finalSource,
  strategy,
  ioTruncated,
  expanded,
  onToggle,
  renderDetail,
}: ThoughtStepsProps) {
  const { t } = useTranslation();
  // 步骤级展开态：默认全部收起，展开某步才渲染输入/输出。
  // 按 `stage-index` 存（同一 stage 在一条链里可能出现多次，如 mix 的双侧融合）。
  const [openSteps, setOpenSteps] = React.useState<Record<string, boolean>>({});
  if (steps.length === 0) return null;

  const failedCount = steps.filter((s) => s.status === 'failed').length;

  return (
    <TraceShell
      title={t('knowledgeSearch.reasoningProcess')}
      expanded={expanded}
      onToggle={onToggle}
      meta={
        <>
          {failedCount > 0 ? (
            <TraceChip tone="danger">{t('knowledgeSearch.reasoningPartialFailed', { count: failedCount })}</TraceChip>
          ) : (
            <TraceChip tone="success">{t('knowledgeSearch.reasoningDone')}</TraceChip>
          )}
          <TraceChip>{t('knowledgeSearch.stepCount', { count: steps.length })}</TraceChip>
          {totalMs != null && <TraceChip>{fmtTraceMs(totalMs)}</TraceChip>}
          <TraceChip tone="primary">{strategyLabel(t, strategy || finalSource)}</TraceChip>
          {ioTruncated && <TraceChip>{t('knowledgeSearch.ioTraceTruncated')}</TraceChip>}
        </>
      }
    >
      {steps.map((step, index) => {
        const key = `${step.stage}-${index}`;
        const open = Boolean(openSteps[key]);
        const hasIO = (step.input?.length ?? 0) > 0 || (step.output?.length ?? 0) > 0;
        const visual = stageVisual(step.stage);
        // 只给四个已知状态配 i18n 文案，其余原样透出（后端加了新状态时不至于变成空白）
        const chipLabel = KNOWN_STATUS.includes(step.status) ? t(`status.${step.status}`) : step.status;
        return (
          <TraceCard
            key={key}
            index={index + 1}
            tone={visual.tone}
            icon={visual.icon}
            title={step.title}
            status={step.status}
            statusLabel={step.status !== 'done' ? <TraceChip tone="warn">{chipLabel}</TraceChip> : undefined}
            duration={step.duration_ms != null ? fmtTraceMs(step.duration_ms) : null}
            summary={step.summary}
          >
            {renderDetail(step)}
            {hasIO && (
              <>
                <button
                  type="button"
                  className={traceStyles.toggle}
                  aria-expanded={open}
                  onClick={() => setOpenSteps((prev) => ({ ...prev, [key]: !prev[key] }))}
                >
                  {open ? <CaretDownOutlined /> : <CaretRightOutlined />}{' '}
                  {t('knowledgeSearch.ioInput')} / {t('knowledgeSearch.ioOutput')}
                </button>
                {open && (
                  <div>
                    <ReasoningStepIO step={step} />
                    <ReasoningRawJson step={step} />
                  </div>
                )}
              </>
            )}
          </TraceCard>
        );
      })}
    </TraceShell>
  );
}
