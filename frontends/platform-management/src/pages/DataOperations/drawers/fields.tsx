import type { ReactNode } from 'react';
import { Descriptions, Flex, Tag, Typography } from 'antd';
import type { DescriptionsProps } from 'antd';
import { codeLabel } from './format';

const { Text } = Typography;

/** 非终态用中性、成功用绿、失败用红 —— 三个状态列共用的配色判据 */
function statusTone(code: string | null): 'success' | 'danger' | 'neutral' {
  if (code === 'failed') return 'danger';
  if (code === 'ready' || code === 'compiled') return 'success';
  return 'neutral';
}

/** 处理状态的一格：软底标签，未知取值回落原始码，缺值显示占位 */
export function StatusText({ code, labels }: { code: string | null; labels: Record<string, string> }) {
  if (!code) return <Text type="secondary">{codeLabel(labels, code)}</Text>;
  return (
    <Tag variant="filled" className={`yx-do-status-tag yx-do-status-tag--${statusTone(code)}`}>
      {codeLabel(labels, code)}
    </Tag>
  );
}

/**
 * 抽屉头部：实体名 + 一行灰色标识（「资产 ID: xxx」这种）。
 *
 * `meta` 由调用方拼好整句再传进来 —— 组件不猜该用哪个字段名，四个抽屉的标识各不相同。
 * 为空时整行不渲染，不留一条空行。
 */
export function DetailHeader({ title, meta }: { title: ReactNode; meta?: ReactNode }) {
  return (
    <div className="yx-do-detail-header">
      <div className="yx-do-detail-header-title">{title}</div>
      {meta && <div className="yx-do-detail-header-meta">{meta}</div>}
    </div>
  );
}

/**
 * 抽屉的一节：可选节标题 + 标签在上的两列字段网格 +（可选）自定义内容。
 *
 * 节与节之间的发丝线由 CSS 的 `.yx-do-detail-section + .yx-do-detail-section` 画：
 * 在这里插 Divider 的话第一节顶上会多出一条线，还得反过来用选择器去掉。
 *
 * `items` 与 `children` 可以同时给（如 C 抽屉的「本体」一节：两个计数 + 降级提示条）。
 */
export function DetailSection({
  title,
  items,
  children,
}: {
  title?: ReactNode;
  items?: DescriptionsProps['items'];
  children?: ReactNode;
}) {
  return (
    <section className="yx-do-detail-section">
      {title && <h3 className="yx-do-detail-section-title">{title}</h3>}
      {items?.length ? (
        <Descriptions
          className="yx-do-detail-fields"
          layout="vertical"
          column={2}
          size="small"
          colon={false}
          items={items}
        />
      ) : null}
      {children}
    </section>
  );
}

/** 一组「名称 + 次要说明」的标签列表；为空时显示一句占位，不留白 */
export function NameTags({
  items,
  empty,
}: {
  items: { key: string; label: string; hint?: string }[];
  empty: ReactNode;
}) {
  if (!items.length) return <Text type="secondary">{empty}</Text>;
  return (
    <Flex wrap gap={8}>
      {items.map((item) => (
        <Tag key={item.key}>
          {item.label}
          {item.hint && <Text type="secondary"> · {item.hint}</Text>}
        </Tag>
      ))}
    </Flex>
  );
}
