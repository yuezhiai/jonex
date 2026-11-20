import { useEffect, useMemo, useState } from 'react';
import { Alert, Form, Input, InputNumber, Modal, Spin, Switch, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { getDefinition, getDefinitionVersion, runDefinition } from '../../api/taskCenterDefinitions';
import type { FieldSpec } from '../../api/taskCenterDefinitions';

interface RunTaskModalProps {
  open: boolean;
  definitionId: string | null;
  onCancel: () => void;
  /** 触发成功后回调（列表刷新 / 跳转由调用方决定） */
  onDone?: () => void;
}

interface RunField {
  name: string;
  spec: FieldSpec;
}

const asMessage = (e: unknown, fallback: string): string =>
  e instanceof Error && e.message ? e.message : fallback;

/**
 * 已发布版本的入参声明缓存（按定义 id）。
 *
 * 表单必须按**当前已发布版本**的 `start.inputs` 生成，而列表行里没有这份数据，
 * 打开弹窗就得先取一次。已发布版本不可变，按「定义 + 版本号」缓存没有失效问题；
 * 定义重新发布后版本号变了，会自然重取。
 */
const inputsCache = new Map<string, { versionNo: number; inputs: Record<string, FieldSpec> }>();

/** 请求够快时干脆不画加载框：延迟一小段再显示，避免弹窗里闪一下 */
const SPINNER_DELAY_MS = 150;

/**
 * 「执行任务」弹窗：按**当前已发布版本**开始节点的入参声明生成表单。
 *
 * 为什么不在列表行上直接触发：后端 `def.run` 会按 `start.inputs` 校验入参，
 * 文件/知识库变量不给值就派不下去。把入参收集收进一个弹窗，列表与二级页共用同一份。
 *
 * 文件变量的值可以是文档 ID 字符串（后端 `validate_file_inputs` 认这一形态），
 * 所以这里先给一个文档 ID 输入框，不做文档选择器 —— 任务中心拿不到知识库的文档列表。
 */
export default function RunTaskModal({ open, definitionId, onCancel, onDone }: RunTaskModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<Record<string, unknown>>();
  const [fields, setFields] = useState<RunField[]>([]);
  const [loading, setLoading] = useState(false);
  /** 延迟显示的加载态：请求很快时整个弹窗里不会闪出 Spinner */
  const [showSpinner, setShowSpinner] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open || !definitionId) return;
    let cancelled = false;
    setLoading(true);
    setShowSpinner(false);
    setLoadError(null);
    setFields([]);
    form.resetFields();
    const spinnerTimer = setTimeout(() => {
      if (!cancelled) setShowSpinner(true);
    }, SPINNER_DELAY_MS);

    const applyInputs = (inputs: Record<string, FieldSpec>): void => {
      const next = Object.entries(inputs).map(([name, spec]) => ({ name, spec }));
      setFields(next);
      form.setFieldsValue(
        Object.fromEntries(
          next
            .filter(({ spec }) => spec.default !== undefined && spec.default !== null)
            .map(({ name, spec }) => [name, spec.default]),
        ),
      );
    };

    // 命中缓存就先把表单铺出来，再在后台确认版本号有没有变（已发布版本不可变）
    const cached = inputsCache.get(definitionId);
    if (cached) {
      applyInputs(cached.inputs);
      setLoading(false);
    }

    (async () => {
      try {
        const detail = await getDefinition(definitionId);
        const versionNo = detail.definition.current_version_no;
        if (cancelled) return;
        if (!versionNo) {
          if (!cached) applyInputs({});
          return;
        }
        if (cached && cached.versionNo === versionNo) return;
        const version = await getDefinitionVersion(definitionId, versionNo);
        if (cancelled) return;
        const inputs = version.config?.start?.inputs ?? {};
        inputsCache.set(definitionId, { versionNo, inputs });
        applyInputs(inputs);
      } catch (e: unknown) {
        // 缓存已经铺出表单时，后台刷新失败不打断用户
        if (!cancelled && !cached) setLoadError(asMessage(e, ''));
      } finally {
        if (!cancelled) {
          setLoading(false);
          setShowSpinner(false);
        }
      }
    })();
    return () => {
      cancelled = true;
      clearTimeout(spinnerTimer);
    };
    // t 不进依赖：换语言时不需要重新拉数据，而把它放进来会让 effect 随渲染重跑
  }, [open, definitionId, form]);

  /** 对象 / 数组入参在表单里按 JSON 文本收，提交前解析回真值 */
  const jsonFields = useMemo(
    () => new Set(fields.filter(({ spec }) => spec.type === 'object' || spec.type === 'array').map((f) => f.name)),
    [fields],
  );

  const submit = async (): Promise<void> => {
    if (!definitionId) return;
    let raw: Record<string, unknown>;
    try {
      raw = await form.validateFields();
    } catch {
      // 表单自身的红字已经说明问题，不再弹一次
      return;
    }
    const payload: Record<string, unknown> = {};
    for (const { name } of fields) {
      const value = raw[name];
      if (value === undefined || value === null || value === '') continue;
      if (jsonFields.has(name)) {
        try {
          payload[name] = JSON.parse(String(value));
        } catch {
          // validateFields 的 rule 已经拦过一次；这里兜直接调用 / 空串以外的情况
          message.error(t('taskCenter.run.invalidJson'));
          return;
        }
      } else {
        payload[name] = typeof value === 'string' ? value.trim() : value;
      }
    }
    setSubmitting(true);
    try {
      await runDefinition(definitionId, payload);
      message.success(t('taskCenter.run.success'));
      onDone?.();
    } catch (e: unknown) {
      message.error(asMessage(e, t('common.operationFailed')));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t('taskCenter.run.title')}
      width={560}
      onCancel={onCancel}
      onOk={submit}
      okText={t('taskCenter.run.title')}
      cancelText={t('common.cancel')}
      confirmLoading={submitting}
      okButtonProps={{ disabled: loading || Boolean(loadError) }}
      // 表单常驻挂载：载入期间就会 resetFields / setFieldsValue，控件没挂上会报「useForm 未连接」
      forceRender
    >
      {/* 载入态给一块有高度的居中区域：裸 Spin 贴在弹窗左上角，看起来像没渲染好 */}
      {loading && showSpinner ? (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 120 }}>
          <Spin />
        </div>
      ) : null}
      {loadError ? <Alert type="error" showIcon title={loadError} /> : null}
      {!loading && !loadError && fields.length === 0 ? (
        <Typography.Text type="secondary">{t('taskCenter.run.noInputs')}</Typography.Text>
      ) : null}
      <Form
        form={form}
        layout="vertical"
        preserve={false}
        // 仅隐藏不卸载：卸载后表单实例断连，载入完成的回填写不进去
        style={{ display: loading || loadError || fields.length === 0 ? 'none' : undefined }}
      >
        {fields.map(({ name, spec }) => (
            <Form.Item
              key={name}
              name={name}
              label={name}
              extra={spec.description || undefined}
              valuePropName={spec.type === 'boolean' ? 'checked' : 'value'}
              rules={[
                { required: spec.required, message: t('taskCenter.run.required', { name }) },
                ...(jsonFields.has(name)
                  ? [
                      {
                        validator: (_: unknown, value: unknown) =>
                          value === undefined || value === '' || isJson(String(value))
                            ? Promise.resolve()
                            : Promise.reject(new Error(t('taskCenter.run.invalidJson'))),
                      },
                    ]
                  : []),
              ]}
            >
              {spec.type === 'boolean' ? (
                <Switch />
              ) : spec.type === 'number' ? (
                <InputNumber style={{ width: '100%' }} />
              ) : jsonFields.has(name) ? (
                <Input.TextArea rows={3} placeholder={t('taskCenter.run.json')} />
              ) : spec.type === 'file' ? (
                <Input placeholder={t('taskCenter.run.documentIdPlaceholder')} allowClear />
              ) : (
                <Input allowClear />
              )}
            </Form.Item>
          ))}
      </Form>
    </Modal>
  );
}

function isJson(value: string): boolean {
  try {
    JSON.parse(value);
    return true;
  } catch {
    return false;
  }
}
