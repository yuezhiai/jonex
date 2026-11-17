import type { ReactNode } from 'react';

export interface SectionCardProps {
  title: ReactNode;
  /** 标题右侧的说明小字，如「共 8 个对象」「按最终回答引用次数排序」 */
  hint?: ReactNode;
  /** 右上角操作区：筛选器、分段器、下拉等 */
  extra?: ReactNode;
  /** 标题下方的说明行 */
  description?: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * 页面内分区的统一外壳：白卡 + 标题行 + 内容。
 *
 * 视觉细节全在 layout.css 的 `.yx-section-*`，这里不写样式，
 * 也不允许调用方再传 style 覆盖 —— 需要例外就改主题变量。
 */
export default function SectionCard({ title, hint, extra, description, children, className }: SectionCardProps) {
  return (
    <section className={`yx-card yx-section-card${className ? ` ${className}` : ''}`}>
      <header className="yx-section-head">
        <div className="yx-section-title-wrap">
          <h3 className="yx-section-title">{title}</h3>
          {hint && <span className="yx-section-hint">{hint}</span>}
        </div>
        {extra && <div className="yx-section-extra">{extra}</div>}
      </header>
      {description && <p className="yx-section-desc">{description}</p>}
      <div className="yx-section-body">{children}</div>
    </section>
  );
}
