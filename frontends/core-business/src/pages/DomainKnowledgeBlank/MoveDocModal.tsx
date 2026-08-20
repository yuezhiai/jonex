import React, { useEffect, useState } from 'react';
import { Modal, Select } from 'antd';
import { useTranslation } from 'react-i18next';
import type { FolderItem } from '@/types/domainKnowledge';

/** 根目录哨兵（folder_id 为空；与后端 __unclassified__ 语义对应，真实 folder_id 为 uuid4 hex 无碰撞）。 */
const UNCLASSIFIED = '__unclassified__';

interface MoveDocModalProps {
  open: boolean;
  title: string;
  folders: FolderItem[];
  /** 单选传文档当前目录 doc.folder_id（null = 文档在未分类）；批量不传（undefined = 不禁选）。 */
  disabledFolderId?: string | null;
  confirmLoading: boolean;
  onOk: (folderId: string | null) => void;
  onCancel: () => void;
}

export default function MoveDocModal({
  open,
  title,
  folders,
  disabledFolderId,
  confirmLoading,
  onOk,
  onCancel,
}: MoveDocModalProps) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<string | undefined>(undefined);

  const options = [
    {
      value: UNCLASSIFIED,
      label: t('common.rootFolder'),
      disabled: disabledFolderId === null,
    },
    ...folders.map((f) => ({
      value: f.id,
      label: f.name,
      disabled: disabledFolderId === f.id,
    })),
  ];

  // 打开时重置为无选中；用户须主动选择目标目录，未选时确定按钮禁用。
  useEffect(() => {
    if (open) {
      setSelected(undefined);
    }
  }, [open]);

  const handleOk = () => {
    if (selected === undefined) return;
    onOk(selected === UNCLASSIFIED ? null : selected);
  };

  return (
    <Modal
      title={title}
      open={open}
      onOk={handleOk}
      onCancel={onCancel}
      confirmLoading={confirmLoading}
      destroyOnHidden
      okButtonProps={{ disabled: !selected }}
    >
      <Select
        value={selected}
        onChange={setSelected}
        options={options}
        placeholder={t('common.moveToFolder')}
        style={{ width: '100%', marginTop: 8 }}
      />
    </Modal>
  );
}
