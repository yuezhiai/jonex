import { useEffect, useState } from 'react';
import type { FC } from 'react';
import { Form, Input, Modal, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { updateDefinition } from '../../api/taskCenterDefinitions';
import type { TaskDefinitionItem } from '../../api/taskCenterDefinitions';

/** 基础信息两个字段的值。两个弹窗（编辑基础信息 / 任务设置）共用同一套校验。 */
export interface TaskBasicInfoValues {
  name: string;
  description?: string;
}

/**
 * 基础信息字段。放在这里而不是各弹窗里各写一遍：任务名称的必填与长度上限、
 * 描述的标签文案，两处必须一致，否则「任务设置」里改的名字会被另一处的规则打回。
 */
export const TaskBasicInfoFields: FC = () => {
  const { t } = useTranslation();
  return (
    <>
      <Form.Item
        name="name"
        label={t('taskCenter.definition.column.name')}
        rules={[
          // whitespace：只填空格时 required 仍算通过，必须显式拦，否则会「保存成功」而名字没变
          { required: true, whitespace: true, message: t('taskCenter.definition.basicInfo.nameRequired') },
          // 128 / 512 是后端 DTO 的上限（`_MAX_DEF_NAME` / `_MAX_DEF_DESCRIPTION`），
          // 前端收得更紧只会把后端本来就收的名字提前打回
          { max: 128, message: t('taskCenter.definition.basicInfo.nameTooLong') },
        ]}
      >
        <Input maxLength={128} placeholder={t('taskCenter.definition.basicInfo.namePlaceholder')} allowClear />
      </Form.Item>
      <Form.Item name="description" label={t('taskCenter.definition.basicInfo.descriptionLabel')}>
        {/* 与截图一致的多行输入 */}
        <Input.TextArea rows={3} maxLength={512} placeholder={t('taskCenter.definition.basicInfo.descriptionPlaceholder')} />
      </Form.Item>
    </>
  );
};

interface TaskBasicInfoModalProps {
  open: boolean;
  task: TaskDefinitionItem | null;
  onCancel: () => void;
  /** 保存成功后回调，页面据此刷新列表（名称与描述都可能变） */
  onSaved: () => void;
}

/**
 * 「编辑基础信息」弹窗：任务名称 + 任务描述，保存 / 取消。
 *
 * 只改 `def.update` 收的那两个字段；编排在「编辑任务」页，链路不能混。
 * 描述按后端 PATCH 的三态语义原样下发（空串 = 清空），不做「空就不传」——
 * 用户把描述删光再保存，期望的是真的清掉。
 */
export default function TaskBasicInfoModal({ open, task, onCancel, onSaved }: TaskBasicInfoModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<TaskBasicInfoValues>();
  const [saving, setSaving] = useState(false);

  // 每次打开都用当前行回填：同一条任务连点两次编辑，第二次不能还显示上一次的输入
  useEffect(() => {
    if (!open) return;
    form.setFieldsValue({ name: task?.name ?? '', description: task?.description ?? '' });
  }, [open, task, form]);

  const handleOk = async (): Promise<void> => {
    if (!task) return;
    // 校验失败时 antd 的 onOk 不会替你接住 reject —— 自己吞掉，否则控制台会飘一条未处理异常
    let values: TaskBasicInfoValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    try {
      await updateDefinition(task.id, {
        name: values.name.trim(),
        description: values.description ?? '',
      });
      message.success(t('taskCenter.definition.basicInfo.saveSuccess'));
      onSaved();
    } catch (e: unknown) {
      message.error(e instanceof Error && e.message ? e.message : t('taskCenter.definition.basicInfo.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t('taskCenter.definition.basicInfo.title')}
      width={560}
      onCancel={onCancel}
      onOk={handleOk}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      // 表单常驻挂载：打开时 setFieldsValue 回填，控件还没挂上会触发「useForm 未连接」告警
      forceRender
    >
      <Form form={form} layout="vertical" preserve={false}>
        <TaskBasicInfoFields />
      </Form>
    </Modal>
  );
}
