import React from 'react';
import { Button, Modal } from 'antd';
import { DeleteOutlined } from '@ant-design/icons';

export interface DangerConfirmModalProps {
  open: boolean;
  /** 主问题，例如「确认删除「知识库 A」？」 */
  title: React.ReactNode;
  /** 次要说明，用于「不可恢复」一类警示 */
  description?: React.ReactNode;
  /** 主按钮文案（由调用方传入已翻译的文案） */
  okText: string;
  /** 次按钮文案（由调用方传入已翻译的文案） */
  cancelText: string;
  /** 提交中：主按钮转圈并禁用 */
  confirmLoading?: boolean;
  width?: number;
  onOk: () => void;
  onCancel: () => void;
}

const BODY_STYLE: React.CSSProperties = { textAlign: 'center', padding: '12px 0' };
const ICON_STYLE: React.CSSProperties = {
  fontSize: 48,
  color: 'var(--yx-error-text)',
  marginBottom: 16,
  display: 'block',
};
const TITLE_STYLE: React.CSSProperties = {
  fontSize: 'var(--yx-font-size-card-title)',
  fontWeight: 500,
  lineHeight: 1.5,
  color: 'var(--yx-text-primary)',
};
const DESC_STYLE: React.CSSProperties = {
  fontSize: 'var(--yx-font-size-small)',
  color: 'var(--yx-text-muted)',
  marginTop: 8,
  lineHeight: 1.6,
};
const FOOTER_STYLE: React.CSSProperties = { display: 'flex', justifyContent: 'center', gap: 12 };

/**
 * 危险操作二次确认弹窗。
 *
 * 全平台的删除/停用类操作统一走这里：48px 图标 + 居中标题 + 居中双按钮，
 * 色值、字号、间距全部取 --yx-* 变量，避免每个页面各拼一套。
 */
export default function DangerConfirmModal({
  open,
  title,
  description,
  okText,
  cancelText,
  confirmLoading = false,
  width = 420,
  onOk,
  onCancel,
}: DangerConfirmModalProps) {
  return (
    <Modal
      open={open}
      onCancel={onCancel}
      title={null}
      width={width}
      footer={
        <div style={FOOTER_STYLE}>
          <Button onClick={onCancel}>{cancelText}</Button>
          <Button danger type="primary" loading={confirmLoading} onClick={onOk}>
            {okText}
          </Button>
        </div>
      }
    >
      <div style={BODY_STYLE}>
        <DeleteOutlined style={ICON_STYLE} />
        <div style={TITLE_STYLE}>{title}</div>
        {description ? <div style={DESC_STYLE}>{description}</div> : null}
      </div>
    </Modal>
  );
}
