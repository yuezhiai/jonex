import React from 'react';
import { Drawer } from 'antd';
import CitationDetailPanel from './CitationDetailPanel';
import type { KnowledgeReference } from '@/types/knowledgeSearch';

interface CitationDrawerProps {
  open: boolean;
  /** 当前角标下标（0-based） */
  index: number;
  /** 引用总数，用于「1 / 4」计数 */
  total: number;
  reference: KnowledgeReference | null;
  onPrev: () => void;
  onNext: () => void;
  onClose: () => void;
  onOpenReference: (ref: KnowledgeReference) => void;
}

/**
 * 引用详情抽屉。
 *
 * 由卡片内左右分栏改为右侧抽屉：对话流收窄后正文列只剩约 280px，
 * 再分栏中文一行放不下 20 字。抽屉盖住历史栏，正文宽度全程不变。
 */
export default function CitationDrawer({
  open,
  index,
  total,
  reference,
  onPrev,
  onNext,
  onClose,
  onOpenReference,
}: CitationDrawerProps) {
  return (
    <Drawer
      open={open}
      onClose={onClose}
      placement="right"
      width={720}
      closable={false}
      styles={{
        body: { padding: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' },
      }}
      destroyOnHidden
    >
      {reference && (
        <CitationDetailPanel
          index={index}
          total={total}
          reference={reference}
          onPrev={onPrev}
          onNext={onNext}
          onClose={onClose}
          onOpenReference={onOpenReference}
          inDrawer
        />
      )}
    </Drawer>
  );
}
