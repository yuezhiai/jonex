import React from 'react';
import { CaretRightOutlined, NodeIndexOutlined } from '@ant-design/icons';
import styles from './ReasoningCard.module.scss';

/**
 * [jonex] 推理链统一卡片（方案 C）。
 *
 * 两条检索链（本体/RAG 的 `ThoughtSteps`、jonex_wiki 的 `WikiTrace`）此前各写
 * 了一套外壳与行样式，同一份「推理过程」在不同链路上长得不一样。这里把外壳、
 * 头部 chips、编号卡、状态点、耗时抽成一份，两条链只提供各自的卡片正文。
 *
 * 设计稿：docs/knowledge-base/knowledge-search-reasoning-trace-unified-prototype.html
 * （方案 C；`?only=C` 可单版查看）。
 */

/** 图标底块语义色，对应原型里的 .k-* 一套 */
export type TraceTone = 'intent' | 'match' | 'route' | 'fact' | 'read' | 'table' | 'plan' | 'synth';

const TONE_CLASS: Record<TraceTone, string> = {
  intent: styles.kIntent,
  match: styles.kMatch,
  route: styles.kRoute,
  fact: styles.kFact,
  read: styles.kRead,
  table: styles.kTable,
  plan: styles.kPlan,
  synth: styles.kSynth,
};

/** 步骤状态：后端 `running | done | skipped | failed | degraded` 之外的值按 done 处理 */
const DOT_CLASS: Record<string, string> = {
  running: styles.dotRun,
  done: styles.dotOk,
  failed: styles.dotBad,
  degraded: styles.dotWarn,
  skipped: styles.dotSkip,
};

/** 耗时展示：<1s 用 ms，<60s 保留两位小数秒，再长拆成 m + s */
export function fmtTraceMs(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms) || ms < 0) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)}s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export type ChipTone = 'default' | 'primary' | 'success' | 'warn' | 'danger';

const CHIP_CLASS: Record<ChipTone, string> = {
  default: '',
  primary: styles.chipPrimary,
  success: styles.chipSuccess,
  warn: styles.chipWarn,
  danger: styles.chipDanger,
};

export function TraceChip({ children, tone = 'default' }: { children: React.ReactNode; tone?: ChipTone }) {
  return <span className={`${styles.chip} ${CHIP_CLASS[tone]}`}>{children}</span>;
}

export interface TraceShellProps {
  /** 头部标题（两条链都是「推理过程」） */
  title: string;
  ariaLabel?: string;
  expanded: boolean;
  onToggle: () => void;
  /** 头部 chips（完成态 / 步数 / 耗时 / 策略 / 来源…） */
  meta?: React.ReactNode;
  /** 运行中的当前动作徽标（只有 wiki 链给） */
  live?: React.ReactNode;
  children?: React.ReactNode;
}

/** 外壳：折叠头部 + 卡片流容器 */
export function TraceShell({ title, ariaLabel, expanded, onToggle, meta, live, children }: TraceShellProps) {
  return (
    <section className={styles.shell} aria-label={ariaLabel || title}>
      <button type="button" className={styles.head} onClick={onToggle} aria-expanded={expanded}>
        <CaretRightOutlined className={`${styles.caret} ${expanded ? styles.caretOpen : ''}`} />
        <NodeIndexOutlined className={styles.headIcon} />
        <span className={styles.headTitle}>{title}</span>
        <span className={styles.meta}>
          {meta}
          {live}
        </span>
      </button>
      {expanded && <div className={styles.cards}>{children}</div>}
    </section>
  );
}

export interface TraceCardProps {
  /** 1-based 序号，统一补零成 01 / 02… */
  index: number;
  tone: TraceTone;
  icon: React.ReactNode;
  title: React.ReactNode;
  /** running | done | failed | degraded | skipped */
  status: string;
  /** 非完成态时补一个状态 chip（完成态只留状态点，别把头部挤满） */
  statusLabel?: React.ReactNode;
  /** 缓存命中等附加 chip */
  badges?: React.ReactNode;
  /** 已格式化的耗时 */
  duration?: string | null;
  /** 步骤摘要（本体链的 summary；wiki 链一般不用） */
  summary?: string | null;
  /** 有更多内容可展开时传：整行变可点 + 右侧 chevron */
  onClick?: () => void;
  expanded?: boolean;
  children?: React.ReactNode;
}

/** 编号步骤卡：序号 + 图标块 + 标题 + 状态点 + 耗时，正文由调用方给 */
export function TraceCard({
  index,
  tone,
  icon,
  title,
  status,
  statusLabel,
  badges,
  duration,
  summary,
  onClick,
  expanded,
  children,
}: TraceCardProps) {
  const running = status === 'running';
  const Row = (onClick ? 'button' : 'div') as React.ElementType;
  return (
    <div className={`${styles.card} ${running ? styles.hot : ''} ${onClick ? styles.clickable : ''}`}>
      <span className={styles.idx}>{String(index).padStart(2, '0')}</span>
      <div className={styles.cardBody}>
        <Row
          {...(onClick ? { type: 'button', onClick, 'aria-expanded': expanded } : {})}
          className={styles.row}
        >
          <span className={`${styles.kib} ${TONE_CLASS[tone]}`}>{icon}</span>
          <span className={styles.cardTitle}>{title}</span>
          {statusLabel}
          {badges}
          <span className={`${styles.dot} ${DOT_CLASS[status] ?? styles.dotOk}`} />
          {duration && <span className={styles.ms}>{duration}</span>}
          {onClick && <CaretRightOutlined className={`${styles.chevron} ${expanded ? styles.chevronOpen : ''}`} />}
        </Row>
        {summary && <div className={styles.summary}>{summary}</div>}
        {children}
      </div>
    </div>
  );
}

export interface TraceIOProps {
  /** 标签：本体链「输入 / 输出」，wiki 链「调用 / 返回」 */
  label: string;
  /** 运行中高亮标签 */
  hot?: boolean;
  /** 等宽（工具名 + 参数） */
  mono?: boolean;
  children?: React.ReactNode;
}

/** 正文统一的「标签 / 值」两列语法 */
export function TraceIO({ label, hot, mono, children }: TraceIOProps) {
  return (
    <div className={styles.io}>
      <span className={`${styles.ioK} ${hot ? styles.ioKHot : ''}`}>{label}</span>
      <span className={`${styles.ioV} ${mono ? styles.mono : ''}`}>{children}</span>
    </div>
  );
}

export { styles as traceStyles };
