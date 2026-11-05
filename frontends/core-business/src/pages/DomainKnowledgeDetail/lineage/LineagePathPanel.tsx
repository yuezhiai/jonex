/**
 * 路径清单面板（P2b）——`GET /ontology/lineage-paths` 结果的只读解释层。
 *
 * 面板只消费 `LineageGraphView` 已经算好的模型（`applyPathHighlights` 的
 * `pathSummaries` + 请求元信息），不再自己做第二套映射：
 * - 每条路径：`index`（1-based）、`score`（2 位小数）、`transfers`、`hitsTarget`、
 *   逐跳 `from_table.from_column → to_table.to_column` + origin/type 标签；
 *   箭头按 `reversed`（遍历方向是否逆数据流）取 `←`，端点始终是数据流端点、
 *   不因遍历方向互换；
 * - `explored` 恒显示（`unknown=true` 时同样是 `explored=0`）；`truncated=true` 时以
 *   警示色 Tag 标出（按 `maxPaths` 截断）—— 求值元信息不受 `unknown` 抑制；
 * - `unknown=true` 只有一种含义（该源没有可用编译 schema），显式文案，**不**渲染成
 *   「没有找到路径」；`paths=[]` 且非 unknown 才显示「没有找到路径」；
 * - 路径里若有步骤的边不在当前画布子图内（join 边不在 data-assets 投影里是常态），
 *   该路径带一枚说明 Tag —— 清单仍完整列出后端返回的每一步，不静默裁剪。
 *
 * 四态由父组件分流：loading（Spin）/ error（Alert）/ empty·unknown·success（本面板）。
 */
import { Alert, Space, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';

import type { LineagePathsResult } from '@/api/ontologyLineagePaths';
import type { PathSummary } from './lineagePathModel';

const { Text } = Typography;

/**
 * origin 档 → 既有 i18n key（与画布图例、列表页同一套文案）。
 * 未知档位（后端将来新增）直接原样显示 origin 字面量 —— 不猜、也不吞。
 */
const ORIGIN_LABEL_KEYS: Readonly<Record<string, string>> = {
  declared: 'domainKnowledge.lineage.originDeclared',
  manual: 'domainKnowledge.lineage.originManual',
  comment: 'domainKnowledge.lineage.originComment',
  platform: 'domainKnowledge.lineage.originPlatform',
  value: 'domainKnowledge.lineage.originValue',
  derived: 'domainKnowledge.lineage.originDerived',
};

/** step 类型 → 文案 key（join = 编译 schema 的 join 边，lineage = 手工血缘边）。 */
const STEP_TYPE_LABEL_KEYS: Readonly<Record<string, string>> = {
  join: 'domainKnowledge.lineage.paths.typeJoin',
  lineage: 'domainKnowledge.lineage.paths.typeLineage',
};

interface Props {
  result: LineagePathsResult;
  /** `applyPathHighlights` 产出的路径清单（1-based index，与画布高亮同序）。 */
  summaries: PathSummary[];
  /** 含「边不在当前画布子图内」的步骤的路径序号（1-based）。 */
  missedPathIndexes: number[];
  /** 本次请求的 `max_paths`（truncated 文案用，与请求参数同源）。 */
  maxPaths: number;
}

export default function LineagePathPanel({
  result,
  summaries,
  missedPathIndexes,
  maxPaths,
}: Props) {
  const { t } = useTranslation();
  const missed = new Set(missedPathIndexes);

  return (
    <div
      style={{
        marginTop: 12,
        padding: 12,
        border: '1px solid #e2e8f0',
        borderRadius: 8,
        background: '#fff',
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: 8 }}>
        {t('domainKnowledge.lineage.paths.panelTitle')}
      </div>

      {result.unknown && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 8 }}
          message={t('domainKnowledge.lineage.paths.unknown')}
        />
      )}

      {/* explored/truncated 恒显示（unknown 时后端给 explored=0 / truncated=false）：
          求值元信息与「该源有没有编译 schema」是两个维度，不能互相吞掉。 */}
      <Space
        wrap
        size={[12, 4]}
        style={{ marginBottom: 8, fontSize: 12, color: '#64748b' }}
      >
        <span>
          {t('domainKnowledge.lineage.paths.explored', { count: result.explored })}
        </span>
        {result.truncated && (
          <Tag color="warning">
            {t('domainKnowledge.lineage.paths.truncated', { max: maxPaths })}
          </Tag>
        )}
      </Space>

      {!result.unknown && summaries.length === 0 ? (
        <Alert
          type="info"
          showIcon
          message={t('domainKnowledge.lineage.paths.empty')}
        />
      ) : summaries.length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {summaries.map((summary) => (
            <div key={summary.index}>
              <Space wrap size={[8, 4]} style={{ marginBottom: 4 }}>
                <Text strong>
                  {t('domainKnowledge.lineage.paths.pathLabel', {
                    index: summary.index,
                  })}
                </Text>
                <Text type="secondary">
                  {t('domainKnowledge.lineage.paths.score', {
                    score: summary.score.toFixed(2),
                  })}
                </Text>
                <Text type="secondary">
                  {t('domainKnowledge.lineage.paths.transfers', {
                    count: summary.transfers,
                  })}
                </Text>
                <Tag color={summary.hitsTarget ? 'green' : 'default'}>
                  {summary.hitsTarget
                    ? t('domainKnowledge.lineage.paths.hitsTarget')
                    : t('domainKnowledge.lineage.paths.notHitsTarget')}
                </Tag>
                {missed.has(summary.index) && (
                  <Tag color="orange">
                    {t('domainKnowledge.lineage.paths.missedEdges')}
                  </Tag>
                )}
              </Space>

              <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                {summary.steps.map((step, index) => {
                  const originKey = ORIGIN_LABEL_KEYS[step.origin];
                  const typeKey = STEP_TYPE_LABEL_KEYS[step.type];
                  return (
                    <div
                      key={`${summary.index}-${index}`}
                      style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}
                    >
                      <Tag style={{ marginInlineEnd: 0 }}>
                        {typeKey ? t(typeKey) : step.type}
                      </Tag>
                      <Tag style={{ marginInlineEnd: 0 }}>
                        {originKey ? t(originKey) : step.origin}
                      </Tag>
                      <span style={{ fontFamily: 'monospace' }}>
                        {`${step.fromTable}.${step.fromColumn} ${
                          step.reversed ? '←' : '→'
                        } ${step.toTable}.${step.toColumn}`}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
