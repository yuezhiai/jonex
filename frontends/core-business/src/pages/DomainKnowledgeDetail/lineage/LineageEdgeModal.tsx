/**
 * 血缘边 新增 / 编辑 弹窗。
 *
 * ⚠️ 不提供 `origin` 选择项：档位是「可信度声明」（`declared` 意味着「这是外键声明」），
 *    人工在管理页敲的不配那一档 —— 后端会强制压成 `manual`，前端不给这个错觉。
 * ⚠️ 不提供 `data_source_id` 选择项：由页签**当前选中的源**带入（表名是物理表名，
 *    选错源等于把边写到一张不存在的表上）。编辑态更不传 —— 边不能换源。
 *
 * 四列级字段全部必填：表级血缘进不了路径搜索（设计 §5.3），后端也会 1001 拒绝。
 */
import { useEffect } from 'react';
import { Form, Input, Modal } from 'antd';
import { useTranslation } from 'react-i18next';
import type { LineageEdge } from '@/api/lineage';

export interface LineageEdgeForm {
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  note?: string;
}

interface Props {
  open: boolean;
  /** 传入 = 编辑态；不传 = 新增态 */
  edge?: LineageEdge | null;
  submitting?: boolean;
  onCancel: () => void;
  onSubmit: (v: LineageEdgeForm) => void;
}

const EMPTY: LineageEdgeForm = {
  from_table: '',
  from_column: '',
  to_table: '',
  to_column: '',
  note: '',
};

export default function LineageEdgeModal({ open, edge, submitting, onCancel, onSubmit }: Props) {
  const { t } = useTranslation();
  const [form] = Form.useForm<LineageEdgeForm>();

  useEffect(() => {
    if (!open) return;
    form.setFieldsValue(
      edge
        ? {
            from_table: edge.from_table,
            from_column: edge.from_column,
            to_table: edge.to_table,
            to_column: edge.to_column,
            note: edge.note ?? '',
          }
        : EMPTY,
    );
  }, [open, edge, form]);

  return (
    <Modal
      open={open}
      title={
        edge
          ? t('domainKnowledge.lineage.editEdgeTitle')
          : t('domainKnowledge.lineage.addEdgeTitle')
      }
      onCancel={onCancel}
      confirmLoading={submitting}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      onOk={() => form.validateFields().then(onSubmit)}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" preserve={false}>
        {/* 四列级字段全部必填：表级血缘进不了路径搜索（设计 §5.3） */}
        <Form.Item
          name="from_table"
          label={t('domainKnowledge.lineage.fromTable')}
          rules={[{ required: true, message: t('domainKnowledge.lineage.requiredField') }]}
        >
          <Input placeholder="dbo.jobs" maxLength={255} />
        </Form.Item>
        <Form.Item
          name="from_column"
          label={t('domainKnowledge.lineage.fromColumn')}
          rules={[{ required: true, message: t('domainKnowledge.lineage.requiredField') }]}
        >
          <Input placeholder="id" maxLength={255} />
        </Form.Item>
        <Form.Item
          name="to_table"
          label={t('domainKnowledge.lineage.toTable')}
          rules={[{ required: true, message: t('domainKnowledge.lineage.requiredField') }]}
        >
          <Input placeholder="dbo.job_forward" maxLength={255} />
        </Form.Item>
        <Form.Item
          name="to_column"
          label={t('domainKnowledge.lineage.toColumn')}
          rules={[{ required: true, message: t('domainKnowledge.lineage.requiredField') }]}
        >
          <Input placeholder="job_id" maxLength={255} />
        </Form.Item>
        <Form.Item name="note" label={t('domainKnowledge.lineage.noteOptional')}>
          <Input.TextArea rows={2} maxLength={500} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
