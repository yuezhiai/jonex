import { useEffect, useState } from 'react';
import { Divider, Form, Modal, Tag, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { updateDefinition } from '../../api/taskCenterDefinitions';
import type { TaskDefinitionItem } from '../../api/taskCenterDefinitions';
import { TaskBasicInfoFields } from './TaskBasicInfoModal';
import type { TaskBasicInfoValues } from './TaskBasicInfoModal';

interface TaskSettingsModalProps {
  open: boolean;
  task: TaskDefinitionItem | null;
  onCancel: () => void;
  onSaved: () => void;
}

/**
 * 「任务设置」弹窗：基础信息 + 触发方式。
 *
 * 触发方式目前只有「手动执行」一档 —— 调度接口还没开，画一个开关出来只会存下一个
 * 后端不认识的字段。排版保留，等 `def.*` 提供触发器后再补。
 */
export default function TaskSettingsModal({ open, task, onCancel, onSaved }: TaskSettingsModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<TaskBasicInfoValues>();
  const [saving, setSaving] = useState(false);

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
      message.success(t('taskCenter.definition.settings.saveSuccess'));
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
      title={t('taskCenter.definition.settings.title')}
      width={640}
      onCancel={onCancel}
      onOk={handleOk}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      // 表单常驻挂载：打开时 setFieldsValue 回填，控件还没挂上会触发「useForm 未连接」告警
      forceRender
    >
      <Form form={form} layout="vertical" preserve={false}>
        <Typography.Title level={5} style={{ marginTop: 0 }}>
          {t('taskCenter.definition.settings.basicSection')}
        </Typography.Title>
        <TaskBasicInfoFields />

        <Divider style={{ margin: '8px 0 16px' }} />

        <Typography.Title level={5}>{t('taskCenter.definition.settings.triggerSection')}</Typography.Title>
        {/* 说明文案照抄设计：它解释了「保存即生效」和「停用任务不接受新触发」两件事 */}
        <Typography.Paragraph type="secondary" style={{ marginBottom: 16 }}>
          {t('taskCenter.definition.settings.triggerHint')}
        </Typography.Paragraph>

        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
          <div style={{ flex: 1 }}>
            <Typography.Text strong>{t('taskCenter.definition.settings.manual')}</Typography.Text>
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t('taskCenter.definition.settings.manualDesc')}
              </Typography.Text>
            </div>
          </div>
          {/* 手动执行没有开关：它恒为开，右侧只放一个状态标签说清楚 */}
          <Tag color="success" style={{ marginTop: 2 }}>
            {t('taskCenter.definition.settings.manualDefault')}
          </Tag>
        </div>
      </Form>
    </Modal>
  );
}
