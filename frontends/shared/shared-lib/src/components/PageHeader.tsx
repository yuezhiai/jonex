import React from 'react';
import { Button, Typography } from 'antd';
import { LeftOutlined } from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

export interface PageHeaderProps {
  /** 页头标题。 */
  title: React.ReactNode;
  /** 标题下方的说明文字，用于原来各页面手写的副标题段落。 */
  subtitle?: React.ReactNode;
  /** 标题旁的附加内容，与标题同一行。 */
  extra?: React.ReactNode;
  /** 返回按钮，渲染在标题左侧。传路径按该路径跳转，传函数自行处理；不传则不渲染。 */
  backTo?: string | (() => void);
  /** 返回按钮文案。不传只显示图标；传 true 用 common.back 的翻译，传字符串按该字符串显示。 */
  backText?: boolean | string;
  /** 覆盖默认样式，与 DEFAULT_STYLE 浅合并。 */
  style?: React.CSSProperties;
}

/** 字面值取自 theme.css 的 token。 */
const DEFAULT_STYLE: React.CSSProperties = {
  marginTop: 0, // 挡掉 UA 的 0.67em 和 antd 紧随块级元素注入的 1.2em
  marginBottom: 16, // 原 .yx-page-title 容器的下边距改由标题承载
  fontSize: 'var(--yx-font-size-page-title)',
  lineHeight: 'normal',
  fontWeight: 'var(--yx-font-weight-page-title)',
  color: 'var(--yx-brand-dark)',
  letterSpacing: '0.5px',
};

const EXTRA_GAP = 12;

/** 标题作为 flex item：不 grow（否则 extra 被顶到行尾），minWidth 解除 auto 下限让长标题可换行。 */
const TITLE_IN_ROW_STYLE: React.CSSProperties = {
  flex: '0 1 auto',
  minWidth: 0,
};

/** null / undefined / false / 空串一律视作「没有附加内容」。 */
const hasContent = (node: React.ReactNode): boolean =>
  node !== undefined && node !== null && node !== false && node !== '';

/** 同上，空串路径同样视作「没有返回按钮」。 */
const hasBack = (backTo: PageHeaderProps['backTo']): backTo is string | (() => void) =>
  backTo !== undefined && backTo !== null && backTo !== '';

/**
 * 单独成组件是为了把 useNavigate / useTranslation 关在里面：hook 提到 PageHeader 顶层后，
 * 不在 Router 内渲染的既有调用方会直接抛错。
 */
const BackButton: React.FC<{ backTo: string | (() => void); backText?: boolean | string }> = ({ backTo, backText }) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const label = typeof backText === 'string' ? backText : t('common.back');

  return (
    <Button
      type="text"
      icon={<LeftOutlined />}
      // 纯图标按钮没有可读文本，用文案兜住读屏器；有可见文案时它本身就是可读文本
      aria-label={backText ? undefined : label}
      onClick={typeof backTo === 'function' ? backTo : () => navigate(backTo)}
    >
      {backText ? label : null}
    </Button>
  );
};

/** 页头行：返回按钮 + 标题 + 附加内容。 */
const ROW_STYLE: React.CSSProperties = {
  display: 'flex',
  alignItems: 'flex-end',
  gap: EXTRA_GAP,
};

/**
 * 页面头部：可选返回按钮 + h1 标题 + 同行附加内容 + 可选副标题；都不传时退化成裸 h1。
 */
const PageHeader: React.FC<PageHeaderProps> = ({ title, subtitle, extra, backTo, backText, style }) => {
  const merged = { ...DEFAULT_STYLE, ...style };
  const hasSubtitle = hasContent(subtitle);

  if (!hasBack(backTo) && !hasContent(extra) && !hasSubtitle) {
    return (
      // level={1} 而非 level={3}：level 同时决定 DOM 标签，用 1 保住 <h1> 语义（字号已被覆盖）
      <Typography.Title level={1} style={merged}>
        {title}
      </Typography.Title>
    );
  }

  // 下边距挂在这一行上：flex item 的 margin 不参与塌陷，留在标题上会把标题顶高、与其他元素底边错开。
  // alignItems 用 flex-end 让 extra 的底边与标题对齐，返回按钮更矮、对齐后视觉上即居中。
  const { marginBottom, ...titleStyle } = merged;

  const row = (
    <div style={ROW_STYLE}>
      {hasBack(backTo) && <BackButton backTo={backTo} backText={backText} />}
      <Typography.Title level={1} style={{ ...titleStyle, marginBottom: 0, ...TITLE_IN_ROW_STYLE }}>
        {title}
      </Typography.Title>
      {extra}
    </div>
  );

  if (!hasSubtitle) {
    // 外层只负责下边距，**不能再套一层 flex**：套了之后内层行会收缩成内容宽度，
    // extra 的 marginLeft:auto 只能推到「标题右边」而不是页头最右侧
    return <div style={{ marginBottom }}>{row}</div>;
  }

  return (
    <div style={{ marginBottom }}>
      {row}
      {/* 样式取 layout.css 的 .yx-page-subtitle（token 化），不在这里另定一套 */}
      <div className="yx-page-subtitle">{subtitle}</div>
    </div>
  );
};

export default PageHeader;
