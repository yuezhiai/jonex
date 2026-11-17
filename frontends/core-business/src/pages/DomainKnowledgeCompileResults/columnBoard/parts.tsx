/**
 * [jonex] 列式基础件：列外壳 / 卡片 / 加载块。
 *
 * 从 `ColumnBrowseTab`（领域本体·列式浏览）整块搬出，样式与 DOM 结构一字未改——
 * 目的只有一个：Wiki 的列式图谱与本体链路共用同一套视觉与交互，不再各写一套。
 * 视觉常量与连线逻辑在 `./core`。
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Empty, Input, Spin, Tag, Tooltip } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import {
  CARD_ACTIVE,
  CARD_HOVER,
  CARD_IDLE,
  CARD_TOOLTIP,
  COL_WIDTH,
  TAG_MAX_WIDTH,
  THUMB_SIZE,
  TYPE_THUMB_SIZE,
  type ColumnItem,
} from './core';

export { ColumnStage, useAppearedKeys, useColumnConnectors, EMPTY_SET, type ColumnItem } from './core';

// ── 列外壳（列头 kicker/title/count + 搜索 + 卡片列表）────────

export function ColumnShell({
  kicker,
  title,
  count,
  searchable,
  search,
  onSearch,
  headerExtra,
  children,
}: {
  kicker: string;
  title: string;
  count?: number;
  searchable?: boolean;
  search?: string;
  onSearch?: (v: string) => void;
  /** 列头下方的额外交互（Wiki 列式图谱用它放「按页面/按断言」切换） */
  headerExtra?: React.ReactNode;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <section
      className="graph-column"
      style={{
        width: COL_WIDTH,
        flexShrink: 0,
        minHeight: 548,
        border: '1px solid #dfe7f2',
        borderTop: '3px solid #4b84eb',
        borderRadius: 10,
        background: 'rgba(255,255,255,0.98)',
        boxShadow: '0 8px 24px rgba(35,73,125,0.08)',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
      }}
    >
      <header
        style={{
          padding: '15px 15px 13px',
          borderBottom: '1px solid #e5ebf3',
          background: 'linear-gradient(180deg, #fff, #f9fbfe)',
        }}
      >
        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            minHeight: 20,
            padding: '0 7px',
            borderRadius: 4,
            color: '#2f72df',
            background: '#eaf2ff',
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: '0.08em',
          }}
        >
          {kicker}
        </span>
        <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
          <h3 style={{ margin: 0, color: '#18365f', fontSize: 15, fontWeight: 700, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {title}
          </h3>
          {typeof count === 'number' && (
            <span
              style={{
                minWidth: 26,
                padding: '2px 7px',
                border: '1px solid #d5e2f4',
                borderRadius: 999,
                color: '#3775d7',
                background: '#f2f6fd',
                fontSize: 12,
                fontWeight: 600,
                textAlign: 'center',
              }}
            >
              {t('compile.columnBrowse.itemCount', { count })}
            </span>
          )}
        </div>
        {searchable && onSearch && (
          <Input
            size="small"
            prefix={<SearchOutlined style={{ color: '#9aa8ba' }} />}
            placeholder={t('compile.columnBrowse.searchPlaceholder')}
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            allowClear
            style={{ marginTop: 11, height: 34 }}
          />
        )}
        {headerExtra ? <div style={{ marginTop: 9 }}>{headerExtra}</div> : null}
      </header>
      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 10px 14px', background: '#fbfcfe', display: 'flex', flexDirection: 'column', gap: 7 }}>
        {children}
      </div>
    </section>
  );
}

// ── 卡片 ──────────────────────────────────────────────────

export function NodeCard({
  item,
  selected = false,
  grayed,
  disabled,
  onClick,
  onOpen,
  openHint,
}: {
  item: ColumnItem;
  /** 展开态（当前列下钻的那一张）；只读列可不传 */
  selected?: boolean;
  grayed?: boolean;
  disabled?: boolean;
  onClick: () => void;
  /** 双击直达（Wiki 列式图谱：单击下钻一列，双击打开这一页本身） */
  onOpen?: () => void;
  /** 双击的提示文案（挂在整张卡片的 title 上） */
  openHint?: string;
}) {
  const thumbText = item.label.trim().charAt(0) || '点';
  // 描述行只放实体描述；类型列的实例数量是 sub，走名称下方的副标题位
  const detail = item.desc ?? '';
  // 只有本体列保留原头像尺寸
  const thumbSize = item.isType ? TYPE_THUMB_SIZE : THUMB_SIZE;
  const thumbBg = item.isType
    ? 'linear-gradient(135deg, #665fd6, #918bed)'
    : 'linear-gradient(135deg, #3478ee, #72a7ff)';
  const grayThumbBg = 'linear-gradient(135deg, #c2cbd8, #d6deea)';
  // 悬停态必须走 state：命令式改 el.style 会被 React 的 style diff 忽略，
  // 一次 hover 移开后边框色就回不到原位（选中态的蓝边也会一并丢掉）
  const [hovered, setHovered] = useState(false);
  const visual = selected ? CARD_ACTIVE : hovered ? CARD_HOVER : CARD_IDLE;
  return (
    <div
      className={`node-card${selected ? ' is-selected' : ''}${disabled ? ' is-disabled' : ''}`}
      onClick={disabled ? undefined : onClick}
      onDoubleClick={disabled ? undefined : onOpen}
      title={onOpen ? openHint : undefined}
      onMouseEnter={disabled ? undefined : () => setHovered(true)}
      onMouseLeave={disabled ? undefined : () => setHovered(false)}
      style={{
        position: 'relative',
        minHeight: 64,
        padding: '8px 9px',
        // 上排「头像 + 名称/标签」、下排描述通栏，描述因此与头像左对齐
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
        // 边框一律 1px，颜色 / 底色 / 投影都随三态走
        border: `1px solid ${visual.border}`,
        borderRadius: 8,
        color: '#284463',
        background: disabled ? '#eef1f5' : visual.background,
        boxShadow: visual.shadow,
        cursor: disabled ? 'default' : 'pointer',
      }}
    >
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: `${thumbSize}px minmax(0, 1fr) 16px`,
          alignItems: 'center',
          gap: 9,
        }}
      >
        <span
          style={{
            width: thumbSize,
            height: thumbSize,
            borderRadius: item.isType ? 8 : '50%',
            color: '#fff',
            background: grayed || disabled ? grayThumbBg : thumbBg,
            boxShadow: '0 4px 10px rgba(52,120,238,0.17)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            // 字号跟头像尺寸一起还原，本体列的首字才不会显小
            fontSize: item.isType ? 16 : 15,
            fontWeight: 700,
            flexShrink: 0,
          }}
        >
          {thumbText}
        </span>
        <span style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
          <Tooltip title={item.label} placement="top" {...CARD_TOOLTIP}>
            <span
              style={{
                minWidth: 0,
                color: '#203d62',
                fontSize: 12,
                fontWeight: 650,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {item.label}
            </span>
          </Tooltip>
          {item.tag ? (
            <Tooltip title={item.tag} placement="top" {...CARD_TOOLTIP}>
              <Tag
                color="blue"
                style={{
                  // column 布局会把 inline-block 拉伸到整行，先按内容收窄再限宽截断
                  alignSelf: 'flex-start',
                  marginInlineEnd: 0,
                  padding: '0 5px',
                  fontSize: 10,
                  lineHeight: '16px',
                  maxWidth: TAG_MAX_WIDTH,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {item.tag}
              </Tag>
            </Tooltip>
          ) : null}
          {item.sub ? (
            // 副标题贴在名称下方、与名称左对齐（类型列的实例数量）
            <span
              style={{
                color: '#8291a6',
                fontSize: 10,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {item.sub}
            </span>
          ) : null}
        </span>
        <span style={{ color: selected ? '#3478ee' : '#7da2d9', fontSize: 16, justifySelf: 'end' }}>
          {disabled ? '' : '›'}
        </span>
      </div>
      {detail ? (
        // 列宽只有 270px，描述放开会把卡片撑高撑破，所以截一行、全文交给 Tooltip
        <Tooltip title={detail} placement="right" {...CARD_TOOLTIP}>
          <span
            style={{
              color: '#8291a6',
              fontSize: 10,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {detail}
          </span>
        </Tooltip>
      ) : null}
      {item.relation && (
        <span
          title={item.relation}
          style={{
            position: 'absolute',
            right: 8,
            // 原「已展开」徽标的位置
            top: 6,
            maxWidth: '55%',
            fontSize: 10,
            lineHeight: '16px',
            color: 'var(--yx-text-muted)',
            background: 'var(--yx-bg-muted)',
            borderRadius: 4,
            padding: '0 6px',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {item.relation}
        </span>
      )}
    </div>
  );
}

// ── 加载 / 错误块 ─────────────────────────────────────────

export function ColumnLoading() {
  return (
    <div style={{ padding: 24, textAlign: 'center' }}>
      <Spin size="small" />
    </div>
  );
}

export function ColumnError({ message, onRetry }: { message: string; onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <div style={{ padding: 24, textAlign: 'center' }}>
      <div style={{ fontSize: 13, color: '#ef4444', marginBottom: 8 }}>{message}</div>
      <Button size="small" onClick={onRetry}>
        {t('compile.columnBrowse.retry')}
      </Button>
    </div>
  );
}

export function ColumnEmpty({ text }: { text: string }) {
  return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={text} style={{ marginTop: 20 }} />;
}

