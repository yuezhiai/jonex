import React, { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  FileSearchOutlined,
  FileTextOutlined,
  LoadingOutlined,
  ReadOutlined,
  TableOutlined,
  ToolOutlined,
} from '@ant-design/icons';
import {
  TraceCard,
  TraceChip,
  TraceIO,
  TraceShell,
  fmtTraceMs,
  traceStyles as styles,
  type TraceTone,
} from './ReasoningCard';
import {
  returnOf,
  runningStep,
  totalDurationMs,
  type WikiReturn,
  type WikiTraceStep,
} from './wikiTraceModel';

/**
 * [jonex] jonex_wiki 检索轨迹。
 *
 * 与本体推理链共用同一套编号步骤卡（ReasoningCard）：一步一卡，
 * 序号 + 图标 + 动作短语 + 状态点 + 耗时，卡内按实际有没有来长：
 *   调用行  工具名 + 真实参数（运行中「工具中…」）—— 只有工具步骤
 *   返回行  引擎自己的计数摘要                —— 有结果时
 *   片段    命中页标题 + 两行摘录（默认 1 条）  —— 有 previews 时
 *
 * 两条链的差异只在文案：wiki 用「调用 / 返回」，本体链用「输入 / 输出」。
 * 点开某一步看全部：所有片段 + 没被片段覆盖到的路径。收起时保持一屏可读。
 */

const KIND_ICON: Record<WikiTraceStep['kind'], React.ReactNode> = {
  search: <FileSearchOutlined />,
  read: <ReadOutlined />,
  table: <TableOutlined />,
  plan: <ToolOutlined />,
  synthesis: <FileTextOutlined />,
};

/** 图标底色：与本体链的 stage 配色同一套 --k-*（semantic token） */
const KIND_TONE: Record<WikiTraceStep['kind'], TraceTone> = {
  search: 'intent',
  read: 'read',
  table: 'table',
  plan: 'plan',
  synthesis: 'synth',
};

/** 原动作文案的键（wikiAction 是改造前就有的那套，不换词） */
const ACTION_KEY: Record<WikiTraceStep['kind'], string> = {
  search: 'search',
  read: 'read',
  table: 'table',
  plan: 'analyze',
  synthesis: 'answer',
};

export interface WikiTraceProps {
  steps: WikiTraceStep[];
  running: boolean;
  /** 流式开始时间戳：运行中按它算总耗时 */
  startedAt?: number;
  durationMs?: number | null;
  source?: string;
  expanded: boolean;
  onToggle: () => void;
  /** 命中页可点开（能对上引用时由调用方提供） */
  onOpenPath?: (path: string) => void;
}

export default function WikiTrace({
  steps,
  running,
  startedAt,
  durationMs,
  source,
  expanded,
  onToggle,
  onOpenPath,
}: WikiTraceProps) {
  const { t } = useTranslation();
  const [openDetail, setOpenDetail] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  // 运行中默认展开（要看得到每一步在干什么），但允许用户当场收起；
  // 结束后交回页面持有的展开态（历史回放默认收起，别把正文挤下去）。
  const [liveCollapsed, setLiveCollapsed] = useState(false);
  const open = running ? !liveCollapsed : expanded;
  const toggle = () => {
    if (running) setLiveCollapsed((value) => !value);
    else onToggle();
  };

  // 运行中才需要每秒刷新总耗时 / 当前步耗时，停表后立刻清掉 interval。
  useEffect(() => {
    if (!running) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, startedAt]);

  const active = running ? runningStep(steps) : undefined;
  const elapsed = running && startedAt != null
    ? Math.max(0, now - startedAt)
    : (durationMs ?? totalDurationMs(steps));

  const actionOf = (step: WikiTraceStep) => t(step.tool === 'image_find' ? 'knowledgeSearch.wikiImageFind' : step.tool === 'image_inspect' ? 'knowledgeSearch.wikiImageInspect' : `knowledgeSearch.wikiAction.${ACTION_KEY[step.kind]}`);

  /** 调用参数：照引擎真实字段加工，不拼假参数。 */
  const argsOf = (step: WikiTraceStep) => {
    if (step.kind === 'search') return step.query ? `“${step.query}”` : '';
    if (step.kind === 'read') {
      const [first, ...rest] = step.paths;
      if (!first) return '';
      const range = step.range ? ` [${step.range[0]}–${step.range[1]}]` : '';
      return rest.length ? `${first}${range} +${rest.length}` : `${first}${range}`;
    }
    if (step.tool === 'table_locate') {
      if (step.literal) return `“${step.literal}”`;
      return step.tableId ?? '';
    }
    const parts: string[] = [];
    if (step.tableId) parts.push(step.tableId);
    if (step.rows) parts.push(t('knowledgeSearch.wikiTrace.rows', { from: step.rows[0], to: step.rows[1] }));
    if (step.operation) parts.push(step.operation);
    return parts.join(' · ');
  };

  /** 返回文案：计数全部来自引擎声明，没有就整行不渲染。 */
  const returnText = (ret: WikiReturn): string => {
    const join = (parts: Array<string | undefined>) => parts.filter(Boolean).join(' · ');
    if (ret.kind === 'search') {
      return join([
        t('knowledgeSearch.wikiTrace.hits', { count: ret.count }),
        ret.excerpts && ret.excerpts > 1 ? t('knowledgeSearch.wikiTrace.excerpts', { count: ret.excerpts }) : undefined,
      ]);
    }
    if (ret.kind === 'read') {
      if (ret.failed) return t('knowledgeSearch.wikiTrace.readFailed');
      return join([
        t('knowledgeSearch.wikiTrace.readPages', { count: ret.count }),
        ret.excerpts && ret.excerpts > 1 ? t('knowledgeSearch.wikiTrace.excerpts', { count: ret.excerpts }) : undefined,
      ]);
    }
    const head = ret.kind === 'compute'
      ? (ret.matched != null
        ? t('knowledgeSearch.wikiTrace.matched', { count: ret.matched })
        : t('knowledgeSearch.wikiTrace.computed'))
      : (ret.count != null
        ? t('knowledgeSearch.wikiTrace.cells', { count: ret.count })
        : ret.matched != null
          ? t('knowledgeSearch.wikiTrace.matched', { count: ret.matched })
          : t('knowledgeSearch.wikiTrace.complete'));
    const tail = ret.count != null
      ? (ret.hasMore ? t('knowledgeSearch.wikiTrace.morePages') : t('knowledgeSearch.wikiTrace.noMorePages'))
      : undefined;
    return join([head, tail]);
  };

  const meta = useMemo(() => (
    <>
      <TraceChip>{t('knowledgeSearch.wikiTrace.stepCount', { count: steps.length })}</TraceChip>
      {source && <TraceChip>{t('knowledgeSearch.wikiTrace.sourceFrom', { source })}</TraceChip>}
      {elapsed != null && elapsed > 0 && <TraceChip>{fmtTraceMs(elapsed)}</TraceChip>}
    </>
  ), [t, steps.length, source, elapsed]);

  const live = running ? (
    <span className={styles.live} title={active ? actionOf(active) : t('knowledgeSearch.reasoningProcess')}>
      <span className={styles.pulse} />
      <span className={styles.liveText}>
        {active ? actionOf(active) : t('knowledgeSearch.wikiStarting')}
      </span>
    </span>
  ) : undefined;

  /** 展开后才有内容的步骤才可点：多段片段，或片段覆盖不到的路径。 */
  const extraOf = (step: WikiTraceStep) => {
    const seen = new Set(step.previews.map((preview) => preview.path).filter(Boolean));
    const paths = step.paths.filter((path) => !seen.has(path));
    return { paths, more: step.previews.length > 1 || paths.length > 0 };
  };

  return (
    <TraceShell
      title={t('knowledgeSearch.reasoningProcess')}
      expanded={open}
      onToggle={toggle}
      meta={meta}
      live={live}
    >
      {steps.length === 0 && running && (
        <TraceCard
          index={1}
          tone="plan"
          icon={<LoadingOutlined />}
          title={t('knowledgeSearch.wikiStarting')}
          status="running"
        />
      )}
      {steps.map((step, index) => {
        const detailOpen = openDetail === index;
        const extra = extraOf(step);
        const call = step.tool ? argsOf(step) : '';
        const ret = step.done ? returnOf(step) : undefined;
        // 运行中的那一行算**自己**的耗时；没有起始时刻就不显示，别拿总时长冒充
        const stepMs = step.done
          ? step.durationMs
          : step.startedAtMs != null
            ? Math.max(0, now - step.startedAtMs)
            : undefined;
        const previews = detailOpen ? step.previews : step.previews.slice(0, 1);
        return (
          <TraceCard
            key={step.id}
            index={index + 1}
            tone={KIND_TONE[step.kind]}
            icon={step.done ? KIND_ICON[step.kind] : <LoadingOutlined />}
            title={actionOf(step)}
            status={step.done ? 'done' : 'running'}
            badges={step.cached ? <TraceChip tone="success">{t('knowledgeSearch.wikiTrace.cached')}</TraceChip> : undefined}
            duration={stepMs != null ? fmtTraceMs(stepMs) : undefined}
            onClick={extra.more ? () => setOpenDetail(detailOpen ? null : index) : undefined}
            expanded={detailOpen}
          >
            {call && (
              <TraceIO label={t('knowledgeSearch.wikiTrace.call')} hot={!step.done} mono>
                <b>{step.tool}</b>
                {step.done ? (
                  <span> {call}</span>
                ) : (
                  <>
                    <span className={styles.pending}> {t('knowledgeSearch.wikiTrace.toolRunning')}</span>
                    {call && <span>（{call}）</span>}
                  </>
                )}
              </TraceIO>
            )}

            {ret && (
              <TraceIO label={t('knowledgeSearch.wikiTrace.return')}>
                <span className={styles.out}>{returnText(ret)}</span>
              </TraceIO>
            )}

            {(previews.length > 0 || extra.more) && (
              <div className={styles.snippets}>
                {previews.map((preview, i) => (
                  <div className={styles.pv} key={`${preview.path ?? preview.title ?? i}`}>
                    {(preview.title || preview.path) && (
                      <div className={styles.pvTitle}>
                        {preview.title || preview.path}
                        {preview.title && preview.path && <i>{preview.path}</i>}
                      </div>
                    )}
                    {preview.excerpt && <p className={styles.pvText}>{preview.excerpt}</p>}
                  </div>
                ))}

                {extra.more &&
                  (detailOpen ? (
                    <>
                      {extra.paths.length > 0 && (
                        <div className={styles.paths}>
                          {extra.paths.map((path) => (
                            onOpenPath ? (
                              <button type="button" key={path} title={path} onClick={() => onOpenPath(path)}>
                                {path}
                              </button>
                            ) : <code key={path} title={path}>{path}</code>
                          ))}
                        </div>
                      )}
                      <button type="button" className={styles.more} onClick={() => setOpenDetail(null)}>
                        {t('knowledgeSearch.wikiTrace.collapse')}
                      </button>
                    </>
                  ) : (
                    <button type="button" className={styles.more} onClick={() => setOpenDetail(index)}>
                      {step.previews.length > 1
                        ? t('knowledgeSearch.wikiTrace.morePreviews', { count: step.previews.length - 1 })
                        : t('knowledgeSearch.wikiTrace.morePaths', { count: extra.paths.length })}
                    </button>
                  ))}
              </div>
            )}
          </TraceCard>
        );
      })}
    </TraceShell>
  );
}
