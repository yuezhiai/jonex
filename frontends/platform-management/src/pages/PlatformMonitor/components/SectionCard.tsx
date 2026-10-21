import type { ReactNode } from 'react';

interface Props {
  title: ReactNode;
  /** 右上角的灰色小字，如「共 8 个对象」「点击任务类型展开执行阶段」。渲染在 extra 之后 */
  hint?: ReactNode;
  /** 右上角操作区：筛选器、下拉等 */
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * 页内所有分区的统一外壳：白卡 + 标题行 + 内容。
 *
 * `hint` 放在右侧而不是紧贴标题：设计稿里所有的说明性小字（行数、口径、操作提示）
 * 都在卡片右上角，贴标题会让每张卡的标题行长度参差不齐。
 */
export default function SectionCard({ title, hint, extra, children, className }: Props) {
  return (
    <section className={`yx-card yx-pm-card${className ? ` ${className}` : ''}`}>
      <header className="yx-pm-card-head">
        <h3 className="yx-pm-card-title">{title}</h3>
        {(extra || hint) && (
          <div className="yx-pm-card-extra">
            {extra}
            {hint && <span className="yx-pm-card-hint">{hint}</span>}
          </div>
        )}
      </header>
      <div className="yx-pm-card-body">{children}</div>
    </section>
  );
}
