import React from 'react';
import { Tooltip } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  ClockCircleOutlined,
  LoadingOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  WarningFilled,
  HourglassOutlined,
} from '@ant-design/icons';
import { deriveDocPhase, PHASE_DISPLAY, type DocPhase, type PhaseIcon } from '@/utils/docPhase';

const ICON_MAP: Record<PhaseIcon, React.ComponentType<{ style?: React.CSSProperties }>> = {
  clock: ClockCircleOutlined,
  spinner: LoadingOutlined,
  check: CheckCircleFilled,
  error: CloseCircleFilled,
  warning: WarningFilled,
  hourglass: HourglassOutlined,
};

export interface DocumentStatusBadgeProps {
  /** 后端原始 status（pending/parsing/ready/failed/deleting/deleted）。 */
  docStatus?: string | null;
  /** 后端原始 ontology_status（pending/extracting/ready/failed）。 */
  ontologyStatus?: string | null;
  /** [jonex] 知识库类型：openkb 时编译状态以 llm_wiki_compile_status 为准。 */
  kbType?: string | null;
  /** [jonex] openkb 文档的 llm_wiki_compile_status（NULL/compiling/compiled/stale/failed）。 */
  llmWikiCompileStatus?: string | null;
  /** [jonex] openkb 文档的 llm_wiki_compile_error（compile_failed 时 hover 展示）。 */
  llmWikiCompileError?: string | null;
  /** 解析失败原因（parse_failed 时 hover 展示）。 */
  errorMessage?: string | null;
  /** 编译失败原因（compile_failed 时 hover 展示；openkb 文档用 llmWikiCompileError）。 */
  ontologyError?: string | null;
}

/**
 * 单一线性状态徽章。图标 + 文案 + 颜色由 (status, ontology_status) 派生（utils/docPhase）；
 * openkb 文档以 llm_wiki_compile_status 为准（其 ontology_status 恒 ready，不代表编译完成）。
 * - parse_failed / compile_failed 支持 hover 错误详情。
 * - 待编译/编译中/编译失败带「已可搜索/仍可搜索」副提示，让用户知道文档已能用。
 */
export default function DocumentStatusBadge({
  docStatus,
  ontologyStatus,
  kbType,
  llmWikiCompileStatus,
  llmWikiCompileError,
  errorMessage,
  ontologyError,
}: DocumentStatusBadgeProps) {
  const { t } = useTranslation();
  const phase: DocPhase | null = deriveDocPhase(docStatus, ontologyStatus, llmWikiCompileStatus, kbType);
  if (!phase) return <span style={{ color: '#cbd5e1' }}>—</span>;

  const d = PHASE_DISPLAY[phase];
  const Icon = ICON_MAP[d.icon];

  const badge = (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        fontSize: 12,
        fontWeight: 500,
        lineHeight: '20px',
      }}
    >
      <span
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 4,
          padding: '2px 8px',
          borderRadius: 4,
          background: `${d.color}1a`, // 12% 透明底色
          color: d.color,
        }}
      >
        <Icon style={{ fontSize: 12 }} />
        {t(d.label)}
      </span>
      {/* [jonex] openkb 文档不显示「已可搜索/仍可搜索」hint——openkb 检索依赖 Wiki
          编译产物，只有 compiled 后才可搜索；待编译/编译中/编译失败都不该提示可搜
          （compiled 本无 hint）。lightrag（向量已入库）保持原 hint。 */}
      {kbType !== 'openkb' && d.hint && (
        <span style={{ fontSize: 11, color: '#94a3b8' }}>{t(d.hint)}</span>
      )}
    </span>
  );

  // 失败态挂错误 tooltip（有原因才挂）；openkb 文档的编译失败原因在 llmWikiCompileError
  const errText =
    phase === 'parse_failed'
      ? errorMessage
      : phase === 'compile_failed'
        ? (kbType === 'openkb' ? llmWikiCompileError : ontologyError)
        : undefined;
  if (errText) {
    return <Tooltip title={errText}>{badge}</Tooltip>;
  }
  return badge;
}
