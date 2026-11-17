import React from 'react';

export interface EntryGridItem {
  /** 稳定 key，一般用路由 path */
  key: string;
  /** 已翻译的标题 */
  label: string;
  /** 已翻译的说明 */
  description?: string;
  icon: React.ReactNode;
  /** 图标底色，形如 #3b82f6（会按 12% 透明度铺底） */
  color: string;
  /** 目标路由；点击时交给调用方跳转 */
  path: string;
  /** 尚未上线的入口：弱化显示并带「即将上线」标记 */
  upcoming?: boolean;
  /** 自定义右上角标记，优先级高于 upcoming 默认文案 */
  tag?: React.ReactNode;
}

export interface EntryGridProps {
  items: EntryGridItem[];
  onNavigate: (path: string) => void;
  /** 弱化入口的提示文案，如「即将上线」 */
  upcomingText?: React.ReactNode;
}

/**
 * 应用首页的入口宫格：图标 + 标题 + 说明。
 * 栅格尺寸在 layout.css 的 .yx-entry-grid（auto-fill），不需要传断点。
 */
const EntryGrid: React.FC<EntryGridProps> = ({ items, onNavigate, upcomingText }) => (
  <div className="yx-entry-grid">
    {items.map((item) => {
      const tag = item.tag ?? (item.upcoming ? upcomingText : undefined);
      return (
        <button
          key={item.key}
          type="button"
          className={`yx-entry-card${item.upcoming ? ' yx-entry-card--upcoming' : ''}`}
          onClick={() => onNavigate(item.path)}
        >
          <span
            className="yx-entry-card-icon"
            style={{ color: item.color, background: `${item.color}1f` }}
            aria-hidden
          >
            {item.icon}
          </span>
          <span className="yx-entry-card-text">
            <span className="yx-entry-card-label">
              {item.label}
              {tag ? <span className="yx-entry-card-tag">{tag}</span> : null}
            </span>
            {item.description ? <span className="yx-entry-card-desc">{item.description}</span> : null}
          </span>
        </button>
      );
    })}
  </div>
);

export default EntryGrid;
