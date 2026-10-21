import React, { useEffect } from 'react';
import { Form, Modal, Input, Radio, Tag, Space, Alert } from 'antd';
import { PlusOutlined, EditOutlined, LockOutlined, ExclamationCircleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { DomainKnowledgeItem, KnowledgeBaseType } from '@/types/domainKnowledge';

interface CreateEditModalProps {
  open: boolean;
  editingKb: DomainKnowledgeItem | null;
  submitting: boolean;
  /** kb_type 仅在「创建」模式下回传；编辑模式不回传（类型创建后不可变） */
  onOk: (data: { name: string; description?: string; kb_type?: KnowledgeBaseType }) => void;
  onCancel: () => void;
  /**
   * [jonex] 本部署**可创建**的类型（后端 env `KB_ENABLED_TYPES`，见
   * `GET /knowledge-base/kb-types`）。
   *
   * 只过滤**创建**时的选项；被禁类型的**存量库**照常可读可检索，不受影响。
   * 未就绪或拉取失败传 `undefined` = 不过滤（fail-open）：真正的闸门在后端
   * `create`（被禁类型会被拒并给出 `err.kb.kb_type_disabled`），所以这里
   * 拿不到配置时**不该**把用户挡在「一个类型都建不了」的死路上。
   */
  kbTypes?: KnowledgeBaseType[];
  /** 弹窗初始选中类型（后端给的首个可用类型）；缺省/不在可用集合内时回退到第一个选项 */
  defaultKbType?: KnowledgeBaseType;
}

/** 知识库类型选项（值与后端 VALID_KB_TYPES 一致） */
const KB_TYPE_OPTIONS: { value: KnowledgeBaseType; labelKey: string; descKey: string }[] = [
  {
    value: 'lightrag',
    labelKey: 'domainKnowledge.kbTypeLightrag',
    descKey: 'domainKnowledge.kbTypeLightragDesc',
  },
  {
    value: 'openkb',
    labelKey: 'domainKnowledge.kbTypeOpenkb',
    descKey: 'domainKnowledge.kbTypeOpenkbDesc',
  },
  {
    value: 'jonex_wiki',
    labelKey: 'domainKnowledge.kbTypeJonexWiki',
    descKey: 'domainKnowledge.kbTypeJonexWikiDesc',
  },
];

const KB_TYPE_TAG_COLOR: Record<KnowledgeBaseType, string> = {
  lightrag: 'blue',
  openkb: 'purple',
  jonex_wiki: 'magenta',
};

/**
 * 字段长度上限（2026-10-16）。
 *
 * 描述框的卡顿不是这段代码的问题：Chromium 在 macOS 系统字体（`-apple-system`，
 * 即平台主题字体栈）下给 `<textarea>` 排「数千字符且**不含换行**」的单行文本时，
 * 每次按键都要对整段重做软换行布局，成本随字数超线性增长 —— 实测 8k 字符
 * 94ms/键、20k 字符 570ms/键，同一段文本每 80 字换行则 20k 也只要 13ms/键，
 * 换成 monospace / Arial 同样回到 13ms。限长是唯一既治本又不改主题的手段：
 * 上限之内这个输入框根本到不了那个量级。
 */
const KB_NAME_MAX_LENGTH = 50;
const KB_DESCRIPTION_MAX_LENGTH = 200;

export default function CreateEditModal({ open, editingKb, submitting, onOk, onCancel, kbTypes, defaultKbType }: CreateEditModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const isEdit = !!editingKb;

  // [jonex] KB_ENABLED_TYPES 过滤：**只作用于创建时的单选组**。
  // 编辑态那个只读 Tag 仍从**未过滤**的 KB_TYPE_OPTIONS 查（见下面 editingOption）——
  // 否则被禁类型的老库会连自己的类型都显示不出来。
  const filteredOptions = kbTypes?.length
    ? KB_TYPE_OPTIONS.filter((o) => kbTypes.includes(o.value))
    : KB_TYPE_OPTIONS;
  // 后端只会回 VALID_KB_TYPES 里的值，理论上过滤结果不为空；真为空（前后端版本错配）
  // 就退回不过滤 —— 渲染一个**没有任何选项**的弹窗比多显示一个选项糟得多。
  const availableOptions = filteredOptions.length ? filteredOptions : KB_TYPE_OPTIONS;
  // 默认值必须跟着可用集合走：写死 'lightrag' 会在它被禁时给出一个「一提交就被拒」的初值
  const defaultType: KnowledgeBaseType =
    defaultKbType && availableOptions.some((o) => o.value === defaultKbType)
      ? defaultKbType
      : availableOptions[0].value;

  useEffect(() => {
    if (open) {
      form.setFieldsValue({
        name: editingKb?.name ?? '',
        description: editingKb?.description ?? '',
        // 编辑模式回填以便只读展示；创建模式用后端给的默认可用类型
        kb_type: editingKb?.kbType ?? defaultType,
      });
    }
  }, [open, editingKb, form, defaultType]);

  const handleOk = async () => {
    try {
      const values = await form.validateFields();
      // [jonex] 编辑模式不回传 kb_type：类型创建后不可变，后端 update() 收到该字段会直接报错
      onOk({
        name: values.name,
        description: values.description,
        ...(isEdit ? {} : { kb_type: values.kb_type as KnowledgeBaseType }),
      });
    } catch {
      /* 校验失败，由 Form 提示 */
    }
  };

  const editingKbType: KnowledgeBaseType = editingKb?.kbType ?? 'lightrag';
  // ⚠ 故意查**未过滤**的 KB_TYPE_OPTIONS：老库的类型可能已在本部署被禁，
  // 但它自己得显示得出来（本弹窗在编辑态不提供改类型的入口，只做只读展示）。
  const editingOption = KB_TYPE_OPTIONS.find((o) => o.value === editingKbType) ?? KB_TYPE_OPTIONS[0];

  return (
    <Modal
      open={open}
      onCancel={onCancel}
      onOk={handleOk}
      confirmLoading={submitting}
      okText={isEdit ? t('common.save') : t('common.create')}
      cancelText={t('common.cancel')}
      width={560}
      destroyOnClose
      title={
        <span>
          {isEdit ? (
            <EditOutlined style={{ color: '#3b82f6', marginRight: 8 }} />
          ) : (
            <PlusOutlined style={{ color: '#3b82f6', marginRight: 8 }} />
          )}
          {isEdit ? t('domainKnowledge.editKnowledgeBase') : t('domainKnowledge.createKnowledgeBase')}
        </span>
      }
    >
      <Form form={form} layout="vertical" style={{ marginTop: 8 }}>
        <Form.Item
          name="name"
          label={t('domainKnowledge.knowledgeBaseName')}
          rules={[{ required: true, message: t('domainKnowledge.knowledgeBaseNameRequired') }]}
        >
          <Input
            placeholder={t('domainKnowledge.namePlaceholder')}
            maxLength={KB_NAME_MAX_LENGTH}
            showCount
          />
        </Form.Item>

        {/* [jonex] 知识库类型：创建时可选，创建后不可变 → 编辑模式只读展示 */}
        <Form.Item
          name="kb_type"
          label={t('domainKnowledge.kbType')}
          extra={
            isEdit
              ? undefined
              : (
                  <span
                    style={{
                      color: '#ff4d4f',
                      fontSize: 12,
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 4,
                    }}
                  >
                    <ExclamationCircleOutlined />
                    {t('domainKnowledge.kbTypeCreateOnlyHint')}
                  </span>
                )
          }
        >
          {isEdit ? (
            <Space size={8} style={{ paddingTop: 2 }}>
              <Tag color={KB_TYPE_TAG_COLOR[editingKbType]} style={{ marginInlineEnd: 0 }}>
                {t(editingOption.labelKey)}
              </Tag>
              <span style={{ color: '#94a3b8', fontSize: 12 }}>
                <LockOutlined style={{ marginRight: 4 }} />
                {t('domainKnowledge.kbTypeImmutable')}
              </span>
            </Space>
          ) : (
            <Radio.Group style={{ width: '100%' }}>
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {availableOptions.map((opt) => (
                  <Radio
                    key={opt.value}
                    value={opt.value}
                    style={{
                      display: 'flex',
                      alignItems: 'flex-start',
                      width: '100%',
                      padding: '10px 12px',
                      border: '1px solid #e2e8f0',
                      borderRadius: 8,
                      marginInlineEnd: 0,
                    }}
                  >
                    <div style={{ marginLeft: 2 }}>
                      <div style={{ fontWeight: 500, color: '#0b2b5c' }}>{t(opt.labelKey)}</div>
                      <div style={{ color: '#64748b', fontSize: 12, marginTop: 2, whiteSpace: 'normal' }}>
                        {t(opt.descKey)}
                      </div>
                    </div>
                  </Radio>
                ))}
              </Space>
            </Radio.Group>
          )}
        </Form.Item>

        <Form.Item name="description" label={t('domainKnowledge.description')}>
          <Input.TextArea
            rows={3}
            placeholder={t('domainKnowledge.descPlaceholder')}
            maxLength={KB_DESCRIPTION_MAX_LENGTH}
            showCount
          />
        </Form.Item>

        {/* [jonex] 新建 KB 默认配置说明（仅创建模式） */}
        {!isEdit && (
          <Alert
            type="info"
            showIcon
            message={t('domainKnowledge.kbDefaultConfigHint')}
            style={{ marginBottom: 8 }}
          />
        )}
      </Form>
    </Modal>
  );
}
