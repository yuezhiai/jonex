import { useState } from 'react';
import type { ReactNode } from 'react';
import { Button, Divider, Popover, Typography, message, theme } from 'antd';
import {
  ArrowRightOutlined,
  BlockOutlined,
  CloseOutlined,
  CopyOutlined,
  FileTextOutlined,
  ReadOutlined,
  ShareAltOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import copyToClipboard from 'copy-to-clipboard';
import { navigateCrossApp } from '@jonex/shell-sdk';
import type { SubtaskConsoleItem } from '../../api/taskCenter';
import { hasExecutionResult } from '../../constants/taskCenter';
import { fmtDuration } from './format';
import { describeRef, inputFileName } from './resultRefs';
import type { RefView } from './resultRefs';

/**
 * 「执行结果」面板：这一步吃了什么、出了什么、产物去哪儿看。
 *
 * 只有 `success` 的步骤才渲染入口：失败的没有产物可看，跑着的还没回报引用。
 *
 * 数据全部来自子任务列表自带的 `input_refs` / `output_refs`（**不再发请求**）——
 * 任务中心只搬运引用、不放正文，正文与它的权限都留在运行方那侧（设计文档 §4.4）。
 * 所以这里**不发明数字**：拿不到的（如解析器、切片条数）显示「未记录」，而不是猜。
 *
 * 版式上把「输入 / 产物」画成**条目卡片**（图标 + 名称 + 类型 + 动作）、把「输出」
 * 画成**读数格**：这一层是给人扫的，不是给人读表格的，标签值竖排在这种窄浮层里
 * 会把英文标签挤成两行。
 */

interface Props {
  item: SubtaskConsoleItem;
}

/** 面板的固定宽度。窄屏时由 `maxWidth` 收回来，避免把表格挤到不可读。 */
const PANEL_WIDTH = 380;

export default function SubtaskResult({ item }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  // 只有成功的步骤才谈得上「执行结果」：失败的没有产物可看，跑着的还没有。
  // 入口整个不出现，比给一个点开全是空的面板干净。
  if (!hasExecutionResult(item)) return null;

  const openTarget = (path: string): void => {
    // 先收起浮层：跨应用跳转在本窗是 SPA 路由，浮层会跟着 DOM 一起留着，看起来像没跳
    setOpen(false);
    navigateCrossApp(path);
  };

  const content = <SubtaskResultBody item={item} onNavigate={openTarget} onClose={() => setOpen(false)} />;

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      trigger="click"
      placement="bottomLeft"
      content={content}
      // 内容自带内边距与分节，浮层默认的 padding 会把标题顶进去
      styles={{ content: { padding: 0, width: PANEL_WIDTH, maxWidth: 'calc(100vw - 32px)' } }}
    >
      <Typography.Link style={{ whiteSpace: 'nowrap' }}>{t('taskCenter.execution.trigger')}</Typography.Link>
    </Popover>
  );
}

/** 面板正文。与触发入口分开，浮层里只负责套壳与开关。 */
function SubtaskResultBody({
  item,
  onNavigate,
  onClose,
}: {
  item: SubtaskConsoleItem;
  onNavigate: (path: string) => void;
  onClose?: () => void;
}) {
  const { t, i18n } = useTranslation();
  const { token } = theme.useToken();
  // 产物类型是多项并列，中文用顿号、英文用逗号；写死顿号会让英文夹一个全角符号
  const listSep = i18n.language?.startsWith('zh') ? '、' : ', ';

  /** 复制到剪贴板：http 非 localhost 下没有 clipboard API，copy-to-clipboard 内部自带降级。 */
  const copy = async (text: string): Promise<void> => {
    if (await copyToClipboard(text)) message.success(t('common.copySuccess'));
    else message.error(t('common.copyFailed'));
  };

  const inputs = (item.input_refs ?? []).map((ref) => describeRef(ref, 'input'));
  const outputs = (item.output_refs ?? []).map((ref) => describeRef(ref, 'output'));
  const kindLabel = (view: RefView): string => (view.labelKey ? t(view.labelKey) : view.kind);
  const notReported = t('taskCenter.execution.notReported');

  /** 三节各自的「复制」文本：给人贴到工单里的纯文本，所以按 `标签：值` 竖排。 */
  const inputText = inputs
    .map((view) => {
      if (view.kind === 'knowledge_base.document') {
        return `${t('taskCenter.execution.inputFile')}：${inputFileName(view) ?? notReported}`;
      }
      if (view.kind === 'knowledge_base.chunks') {
        return `${t('taskCenter.execution.inputChunks')}：${t('taskCenter.execution.chunksValue')}`;
      }
      return `${kindLabel(view)}：${notReported}`;
    })
    .join('\n');
  const outputText = [
    `${t('taskCenter.execution.duration')}：${fmtDuration(item.duration_seconds)}`,
    `${t('taskCenter.execution.artifactTypes')}：${
      outputs.length ? outputs.map(kindLabel).join(listSep) : notReported
    }`,
  ].join('\n');
  const artifactText = outputs
    .map((view) => `${view.artifact ? t(view.artifact.title) : view.kind}（${kindLabel(view)}）`)
    .join('\n');

  return (
    // 高度封顶：宿主是把子应用嵌在 iframe 里的，浮层比 iframe 内的可用空间高时会被裁掉。
    // 头部固定、内容区自己滚，用户滚的是产物列表，不会把标题滚没
    <div
      style={{
        position: 'relative',
        display: 'flex',
        flexDirection: 'column',
        maxHeight: 'min(56vh, 380px)',
        background: token.colorBgElevated,
        borderRadius: token.borderRadiusLG,
        overflow: 'hidden',
      }}
    >
      <div style={{ flex: '0 0 auto', padding: '10px 16px 6px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Typography.Text strong style={{ fontSize: 14 }}>
            {t('taskCenter.execution.title')}
          </Typography.Text>
          {/* 不再挂状态 Tag：入口只在成功时出现，这一格永远是「已完成」，是纯噪声 */}
          <div style={{ flex: 1 }} />
          {onClose && (
            <Button
              type="text"
              size="small"
              icon={<CloseOutlined />}
              aria-label={t('taskCenter.execution.close')}
              onClick={onClose}
            />
          )}
        </div>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {item.subtask_name || item.node_id}
        </Typography.Text>
      </div>

      <div style={{ flex: '1 1 auto', minHeight: 0, overflowY: 'auto', padding: '0 16px 10px' }}>
        <Section
          title={t('taskCenter.execution.input')}
          copyText={inputText}
          copyLabel={t('common.copy')}
          onCopied={copy}
        >
          {inputs.length ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {inputs.map((view, index) => {
                if (view.kind === 'knowledge_base.document') {
                  return (
                    <EntryCard
                      key={`in-${index}`}
                      icon={<FileTextOutlined />}
                      title={inputFileName(view) ?? notReported}
                      meta={kindLabel(view)}
                    />
                  );
                }
                if (view.kind === 'knowledge_base.chunks') {
                  return (
                    <EntryCard
                      key={`in-${index}`}
                      icon={<BlockOutlined />}
                      title={t('taskCenter.execution.chunksValue')}
                      meta={kindLabel(view)}
                      action={view.path ? t('taskCenter.execution.viewChunks') : null}
                      onAction={view.path ? () => onNavigate(view.path as string) : undefined}
                    />
                  );
                }
                return (
                  <EntryCard
                    key={`in-${index}`}
                    icon={<FileTextOutlined />}
                    title={kindLabel(view)}
                    meta={notReported}
                  />
                );
              })}
            </div>
          ) : (
            <EmptyLine>{t('taskCenter.execution.emptyInput')}</EmptyLine>
          )}
        </Section>

        <Divider style={{ margin: '10px 0' }} />

        <Section
          title={t('taskCenter.execution.output')}
          copyText={outputText}
          copyLabel={t('common.copy')}
          onCopied={copy}
        >
          {/* 状态已经在标题行，这里不再重复一格；两格更宽，产物类型不至于被截成省略号 */}
          <div style={{ display: 'flex', gap: 8 }}>
            <Metric label={t('taskCenter.execution.duration')}>{fmtDuration(item.duration_seconds)}</Metric>
            <Metric label={t('taskCenter.execution.artifactTypes')}>
              {outputs.length ? outputs.map(kindLabel).join(listSep) : <Muted>{notReported}</Muted>}
            </Metric>
          </div>
        </Section>

        <Divider style={{ margin: '10px 0' }} />

        <Section
          title={t('taskCenter.execution.artifacts', { count: outputs.length })}
          copyText={artifactText}
          copyLabel={t('common.copy')}
          onCopied={copy}
        >
          {outputs.length ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {outputs.map((view, index) => (
                <EntryCard
                  key={`out-${index}`}
                  icon={refIcon(view.kind)}
                  title={view.artifact ? t(view.artifact.title) : view.kind}
                  meta={kindLabel(view)}
                  action={view.path && view.artifact ? t(view.artifact.action) : null}
                  onAction={view.path ? () => onNavigate(view.path as string) : undefined}
                />
              ))}
            </div>
          ) : (
            <EmptyLine>{t('taskCenter.execution.emptyOutput')}</EmptyLine>
          )}
        </Section>

      </div>

      {/* 内容比浮层高时底部渐隐，提示「下面还有」；否则卡片被齐边切断，看着像渲染坏了 */}
      <div
        aria-hidden
        style={{
          position: 'absolute',
          insetInline: 0,
          bottom: 0,
          height: 14,
          background: `linear-gradient(to top, ${token.colorBgElevated}, transparent)`,
          pointerEvents: 'none',
        }}
      />
    </div>
  );
}

/** 产物图标按 `kind` 分：切片 / 本体 / Wiki 各自一眼能认出来，比同一个图标重复三次好读。 */
function refIcon(kind: string): ReactNode {
  switch (kind) {
    case 'knowledge_base.ontology':
      return <ShareAltOutlined />;
    case 'knowledge_base.wiki':
      return <ReadOutlined />;
    case 'knowledge_base.chunks':
      return <BlockOutlined />;
    default:
      return <FileTextOutlined />;
  }
}

function Section({
  title,
  copyText,
  copyLabel,
  onCopied,
  children,
}: {
  title: string;
  copyText: string;
  copyLabel: string;
  onCopied: (text: string) => Promise<void>;
  children: ReactNode;
}) {
  const { token } = theme.useToken();
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <Typography.Text strong style={{ fontSize: 12, color: token.colorTextSecondary }}>
          {title}
        </Typography.Text>
        <div style={{ flex: 1 }} />
        <Button
          type="text"
          size="small"
          icon={<CopyOutlined />}
          aria-label={copyLabel}
          style={{ color: token.colorTextTertiary, fontSize: 12 }}
          onClick={() => void onCopied(copyText)}
        >
          {copyLabel}
        </Button>
      </div>
      {children}
    </div>
  );
}

/** 输出读数的一格：标签在上、值在下，三格等宽。 */
function Metric({ label, children }: { label: string; children: ReactNode }) {
  const { token } = theme.useToken();
  return (
    <div
      style={{
        flex: '1 1 0',
        minWidth: 0,
        padding: '6px 10px',
        borderRadius: token.borderRadius,
        background: token.colorFillQuaternary,
      }}
    >
      <div style={{ fontSize: 12, color: token.colorTextTertiary, whiteSpace: 'nowrap' }}>{label}</div>
      {/* 值宁可折行也不截断：产物类型是这一格的全部信息，截成「Chunks、On…」等于没有 */}
      <div
        style={{
          marginTop: 2,
          fontSize: 13,
          fontWeight: 500,
          lineHeight: '18px',
          wordBreak: 'break-word',
        }}
      >
        {children}
      </div>
    </div>
  );
}

/** 输入 / 产物共用的一条卡片：图标块 + 名称 + 类型 + 右侧动作。 */
function EntryCard({
  icon,
  title,
  meta,
  action,
  onAction,
}: {
  icon: ReactNode;
  title: string;
  meta: string;
  action?: string | null;
  onAction?: () => void;
}) {
  const { token } = theme.useToken();
  const clickable = Boolean(action && onAction);
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '8px 10px',
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: token.borderRadius,
        background: token.colorFillQuaternary,
      }}
    >
      <div
        style={{
          flex: '0 0 28px',
          height: 28,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderRadius: token.borderRadiusSM,
          background: token.colorPrimaryBg,
          color: token.colorPrimary,
        }}
      >
        {icon}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 500,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
          title={title}
        >
          {title}
        </div>
        <div style={{ fontSize: 12, color: token.colorTextTertiary, whiteSpace: 'nowrap' }}>{meta}</div>
      </div>
      {clickable ? (
        <Typography.Link style={{ fontSize: 12, whiteSpace: 'nowrap' }} onClick={onAction}>
          {action}
          <ArrowRightOutlined style={{ marginInlineStart: 4 }} />
        </Typography.Link>
      ) : null}
    </div>
  );
}

function EmptyLine({ children }: { children: ReactNode }) {
  return (
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
      {children}
    </Typography.Text>
  );
}

function Muted({ children }: { children: ReactNode }) {
  const { token } = theme.useToken();
  return <span style={{ color: token.colorTextTertiary }}>{children}</span>;
}
