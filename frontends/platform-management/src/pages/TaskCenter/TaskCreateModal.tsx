import { useEffect, useState } from 'react';
import { Form, Modal, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { createDefinition } from '../../api/taskCenterDefinitions';
import { TaskBasicInfoFields } from './TaskBasicInfoModal';
import type { TaskBasicInfoValues } from './TaskBasicInfoModal';

interface TaskCreateModalProps {
  open: boolean;
  /** 主应用当前选中的空间；调用方只在有空间时才打开本弹窗。 */
  spaceId: string | null;
  onCancel: () => void;
  /** 建好后回调新定义的 id —— 调用方直接带进编辑器编排 */
  onCreated: (definitionId: string) => void;
}

/**
 * 「新建任务」弹窗：只收名称与描述，建出来是一份**空草稿**。
 *
 * 编排（开始节点入参、执行步骤、结束节点产出）不在这一步收集 —— 它要拉内置步骤
 * 清单、要校验连线，塞进创建弹窗会让「建个空壳」变成一次长表单。空间不做选择器：
 * 定义必须落在主应用当前空间里，与列表的筛选口径同一个来源。
 */
export default function TaskCreateModal({ open, spaceId, onCancel, onCreated }: TaskCreateModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<TaskBasicInfoValues>();
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    // 每次打开都清空：上一次取消留下的草稿不该出现在下一次
    if (open) form.resetFields();
  }, [open, form]);

  const handleOk = async (): Promise<void> => {
    if (!spaceId) return;
    let values: TaskBasicInfoValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setCreating(true);
    try {
      const created = await createDefinition({
        name: values.name.trim(),
        description: values.description?.trim() || undefined,
        space_id: spaceId,
      });
      message.success(t('taskCenter.definition.create.success'));
      onCreated(created.definition.id);
    } catch (e: unknown) {
      message.error(e instanceof Error && e.message ? e.message : t('common.operationFailed'));
    } finally {
      setCreating(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t('taskCenter.definition.create.title')}
      width={560}
      onCancel={onCancel}
      onOk={handleOk}
      okText={t('common.create')}
      cancelText={t('common.cancel')}
      confirmLoading={creating}
      // 表单常驻挂载：打开时就要 resetFields，控件还没挂上会触发「useForm 未连接」告警
      forceRender
    >
      <Typography.Paragraph type="secondary">{t('taskCenter.definition.create.hint')}</Typography.Paragraph>
      <Form form={form} layout="vertical" preserve={false}>
        <TaskBasicInfoFields />
      </Form>
    </Modal>
  );
}
