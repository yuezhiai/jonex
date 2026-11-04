import { Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import type { PmCallProtocol } from '../types';

interface Props {
  /** 原始标识（能力 ID / 模型名 / MCP 工具名）；只出现在 title 与复制入口 */
  value: string | null | undefined;
  /** 当前语言下的展示文本；由调用方经 `capabilityLabel()` 得到 */
  label: string;
  /** 表格主列使用，沿用既有 `.yx-pm-cell-strong` 视觉 */
  strong?: boolean;
  className?: string;
}

/**
 * 标识单元格。
 *
 * 可见文本只有释义；原始 ID 藏在 hover 的 `title` 与复制按钮里。
 * 空值显示占位符且不给复制入口 —— 复制一个空串没有意义。
 */
export default function IdentifierText({ value, label, strong, className }: Props) {
  const { t } = useTranslation();

  if (!value) return <span className="yx-pm-cell-strong">-</span>;

  return (
    <Typography.Text
      className={[strong ? 'yx-pm-cell-strong' : '', className ?? ''].filter(Boolean).join(' ')}
      title={value}
      // 表格整行可点（下钻详情）：点复制按钮不应顺带打开抽屉，但点名称文本仍可下钻
      onClick={(event) => {
        if ((event.target as HTMLElement).closest('.ant-typography-copy')) event.stopPropagation();
      }}
      copyable={{
        text: value,
        tooltips: [t('platformMonitor.identifier.copy'), t('platformMonitor.identifier.copied')],
      }}
    >
      {label || value}
    </Typography.Text>
  );
}

/**
 * 标识 + 协议标签。
 *
 * 只有「同一展示名挂多个协议」的行才传入非空 `protocols`；正常情况下标签不出现。
 */
export function IdentifierWithProtocols({
  value,
  label,
  protocols,
}: Props & { protocols: readonly PmCallProtocol[] }) {
  const { t } = useTranslation();

  return (
    <span className="yx-pm-identifier-cell">
      <IdentifierText value={value} label={label} strong />
      {protocols.map((item) => (
        <Tag key={item} className="yx-pm-protocol-tag">
          {t(`platformMonitor.callProtocol.${item}`)}
        </Tag>
      ))}
    </span>
  );
}
