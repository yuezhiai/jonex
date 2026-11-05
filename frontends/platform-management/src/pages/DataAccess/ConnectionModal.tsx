/**
 * 添加 / 编辑数据库连接（平台级，**没有知识库选择器**）。
 *
 * ## 为什么是「创建并测试」而不是「先测再建」
 *
 * 后端的连接测试端点 `POST /data-source-connections/{id}/test` **只接受已落库的 id**
 * （routes/knowledge_base.py 的签名里没有 body）。想在保存前测，得另开一个
 * 「拿配置直接测」的端点——本次不做，于是退而求其次：
 *
 *   创建 → 立刻测 → 把结果摊在弹窗里
 *
 * 测失败**不回滚**（连接已经建出来了），但给出「删除重来」和「仍然保留」两条路，
 * 不把用户卡在中间，也不偷偷留一个连不上的连接。
 *
 * 编辑模式（`editing` 非空）不测连接：那是改配置，测不测由用户在列表里点。
 */
import { forwardRef, useCallback, useEffect, useImperativeHandle, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Checkbox, Divider, Form, Input, InputNumber, Modal, Select, Space, message } from 'antd';
import {
  createConnection,
  deleteConnection,
  testConnection,
  updateConnection,
  type ConnectionItem,
  type TestConnectionResult,
} from '../../api/dataSourceConnection';

export interface ConnectionModalHandle {
  open: (editing?: ConnectionItem) => void;
}

interface Props {
  /** 当前领域；连接只能建在该领域下。null = 用户还没选领域。 */
  spaceId: string | null;
  onSaved: () => void;
}

/** 引擎默认端口 —— 三个都被这个 POC 的库表结构实测过。 */
const ENGINE_PORTS: Record<string, number> = { postgresql: 5432, mysql: 3306, sqlserver: 1433 };

/** 后端 `schema_source` 白名单，顺序与 `supported_engines()` 一致。 */
const ENGINES = ['postgresql', 'mysql', 'sqlserver'] as const;

const ConnectionModal = forwardRef<ConnectionModalHandle, Props>(({
  spaceId, onSaved,
}, ref) => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<ConnectionItem | null>(null);
  const [submitting, setSubmitting] = useState(false);
  /** 已创建出来的连接——测连接要用它的 id；也是「删除重来」的目标 */
  const [created, setCreated] = useState<ConnectionItem | null>(null);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(null);

  const engine = Form.useWatch('engine', form) as string | undefined;
  /** 勾选「该账号即只读账号」时，显式只读凭据不展示、也不提交 */
  const readonlyUseMain = Form.useWatch('readonly_use_main', form) as boolean | undefined;

  const reset = useCallback(() => {
    form.resetFields();
    form.setFieldsValue({ engine: 'postgresql', port: ENGINE_PORTS.postgresql });
    setCreated(null);
    setTestResult(null);
  }, [form]);

  // 编辑模式下把已落库的值回填进表单（密码不回填——后端只回 ***，也读不回来）
  useEffect(() => {
    if (!open) return;
    if (!editing) return;
    const cfg = (editing.config_json || {}) as Record<string, unknown>;
    form.setFieldsValue({
      name: editing.name,
      engine: cfg.engine ?? 'postgresql',
      host: cfg.host,
      port: cfg.port,
      database: cfg.database,
      user: cfg.user,
      schema: cfg.schema,
      password: undefined,
      // 只读账号回填；密码同理只写不回显（后端只回 ***）
      readonly_user: cfg.readonly_user,
      readonly_password: undefined,
      // 模式声明原样回传（后端不脱敏），用宽松比较兜住老的字符串值
      readonly_use_main: cfg.readonly_use_main === true || cfg.readonly_use_main === 'true',
    });
  }, [open, editing, form]);

  useImperativeHandle(ref, () => ({
    open: (item?: ConnectionItem) => {
      reset();
      setEditing(item ?? null);
      setOpen(true);
    },
  }));

  const submit = async (): Promise<void> => {
    let values: Record<string, unknown>;
    try {
      values = await form.validateFields();
    } catch {
      return; // antd 已经在字段上标红了
    }
    if (!spaceId) {
      message.error(t('dataSource.spaceRequired'));
      return;
    }
    // 载荷由当前表单值现造：`config_json` 里绝不回带已被脱敏的 `password_ref`
    //
    // 只读账号三态：填了 → 原样送；清空且原来有 → 送 `""`（后端 merge，不送删不掉）；
    // 清空且原来就没有 → 不送（不给连接留个空键）。
    const hadReadonly = Boolean(
      String(((editing?.config_json || {}) as Record<string, unknown>).readonly_user || '').trim(),
    );
    const readonlyUserRaw = String(values.readonly_user ?? '').trim();
    // 勾选「该账号即只读账号」时，显式只读字段一律不提交（两者互斥）
    const useMain = values.readonly_use_main === true;
    const hadUseMain = Boolean(
      ((editing?.config_json || {}) as Record<string, unknown>).readonly_use_main,
    );
    const payload = {
      name: String(values.name),
      engine: String(values.engine),
      host: String(values.host),
      port: Number(values.port),
      database: String(values.database),
      user: String(values.user),
      // 留空 = undefined → 更新时 `toConfigJson` 不带该字段 → 后端保留原密码
      password: values.password ? String(values.password) : undefined,
      schema: values.schema ? String(values.schema) : undefined,
      // 创建时不勾选就不写这个键；编辑时若原来开着，必须显式送 false 才关得掉
      readonlyUseMain: useMain ? true : (editing && hadUseMain ? false : undefined),
      readonlyUser: useMain
        ? undefined
        : readonlyUserRaw || (editing && hadReadonly ? '' : undefined),
      readonlyPassword: useMain
        ? undefined
        : values.readonly_password
          ? String(values.readonly_password)
          : undefined,
    };

    setSubmitting(true);
    try {
      if (editing) {
        await updateConnection(editing.id, payload, spaceId);
        message.success(t('dataSource.connectionSaved'));
        onSaved();
        setOpen(false);
        return;
      }
      const conn = await createConnection(payload, spaceId);
      setCreated(conn);
      onSaved();

      // 建完立刻测——这是整个流程里用户最想知道的一件事
      const result = await testConnection(conn.id, spaceId);
      setTestResult(result);
      if (result.ok) message.success(t('dataSource.testOk'));
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('dataSource.createFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  /** 测失败后的「删除重来」：把刚建的删掉，回到表单，不留半成品。
   *
   *  **保留全部已填字段，只清密码。** 「retry」的语义是「连接信息没错、多半是密码敲错了」，
   *  把 host/库名/用户名一起清掉，用户就得从头再填一遍。密码必须清：认证失败时
   *  它就是要改的那个东西，留着只会反复失败。 */
  const discardAndRetry = async (): Promise<void> => {
    if (!created || !spaceId) return;
    setSubmitting(true);
    try {
      await deleteConnection(created.id, spaceId);
      onSaved();
      const keepValues = form.getFieldsValue(true) as Record<string, unknown>;
      reset();
      form.setFieldsValue({ ...keepValues, password: undefined });
      setCreated(null);
      setTestResult(null);
      message.info(t('dataSource.discarded'));
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      title={editing ? t('dataSource.editConnection') : t('dataSource.addConnection')}
      width={560}
      destroyOnHidden
      onCancel={() => setOpen(false)}
      footer={
        <Space>
          {created && testResult && !testResult.ok ? (
            <Button danger loading={submitting} onClick={discardAndRetry}>
              {t('dataSource.discardAndRetry')}
            </Button>
          ) : null}
          {created ? (
            // 刚建出来：连接已经落库了，这里没有可提交的东西，「完成」只负责关窗。
            // 「完成」是收尾动作，只能在请求在飞的时候挡住它，
            // 别拿 `created` 当禁用的理由——那会让这条唯一的出口永远点不动
            <Button type="primary" onClick={() => setOpen(false)} disabled={submitting}>
              {t('dataSource.done')}
            </Button>
          ) : editing ? (
            // 编辑：**这颗按钮必须真的提交**。`created` 在编辑模式下恒为 null，
            // 所以它绝不能和上面那条共用「onClick = setOpen(false)」——那等于
            // 用户改完 host/密码点保存，弹窗一关、什么都没写、还没有任何报错。
            // 关窗由 `submit()` 自己收尾（更新成功后 `setOpen(false)`），这里不要再关一次。
            <Button type="primary" loading={submitting} onClick={submit}>
              {t('common.save')}
            </Button>
          ) : (
            <Button type="primary" loading={submitting} onClick={submit}>
              {t('dataSource.createAndTest')}
            </Button>
          )}
        </Space>
      }
    >
      <Form form={form} layout="vertical" disabled={!!created}>
        <Form.Item name="name" label={t('dataSource.name')} rules={[{ required: true }]}>
          <Input placeholder={t('dataSource.namePlaceholder')} />
        </Form.Item>

        <Form.Item name="engine" label={t('dataSource.engine')} rules={[{ required: true }]}>
          <Select
            onChange={(value: string) => form.setFieldsValue({ port: ENGINE_PORTS[value] })}
            options={ENGINES.map((e) => ({ value: e, label: e }))}
          />
        </Form.Item>

        <Space.Compact style={{ width: '100%' }}>
          <Form.Item name="host" label={t('dataSource.host')} rules={[{ required: true }]} style={{ flex: 3 }}>
            {/* 容器里连宿主机的库要走 host.docker.internal —— 这个 POC 的库就在宿主机上 */}
            <Input placeholder="host.docker.internal" />
          </Form.Item>
          <Form.Item name="port" label={t('dataSource.port')} rules={[{ required: true }]} style={{ flex: 2 }}>
            <InputNumber min={1} max={65535} style={{ width: '100%' }} />
          </Form.Item>
        </Space.Compact>

        <Form.Item name="database" label={t('dataSource.database')} rules={[{ required: true }]}>
          <Input />
        </Form.Item>

        {/* schema 只有 pg / sqlserver 用得上；MySQL 拿 database 当 schema，填了也没用 */}
        {engine !== 'mysql' ? (
          <Form.Item name="schema" label={t('dataSource.schema')} extra={t('dataSource.schemaHint')}>
            <Input placeholder={engine === 'sqlserver' ? 'dbo' : 'public'} />
          </Form.Item>
        ) : null}

        <Form.Item name="user" label={t('dataSource.user')} rules={[{ required: true }]}>
          <Input autoComplete="off" />
        </Form.Item>

        <Form.Item
          name="password"
          label={t('dataSource.password')}
          extra={editing ? t('dataSource.passwordKeepHint') : undefined}
        >
          {/* 只写不回显：后端 to_dict 把 password_ref 脱敏成 ***，本来也读不回来。
              编辑时留空 = 不改密码（后端「不传即保留」）。 */}
          <Input.Password autoComplete="new-password" placeholder={editing ? '••••••' : undefined} />
        </Form.Item>

        {/* ── 只读凭据：② NL→SQL 专用（不是写账号的替代） ──
            后端解析只认 readonly_user / readonly_password_ref，**绝不回退**到上面的
            写账号；没登记 ⇒ 数据问答直接回 no_readonly_creds，fail-closed。 */}
        <Divider style={{ margin: '8px 0 16px' }}>{t('dataSource.readonlyTitle')}</Divider>

        <Form.Item
          name="readonly_use_main"
          valuePropName="checked"
          extra={t('dataSource.readonlyUseMainHint')}
        >
          <Checkbox
            onChange={(e) => {
              // 勾上就把显式只读字段清掉：两者互斥，别让残留值在取消勾选后神秘复活
              if (e.target.checked) {
                form.setFieldsValue({ readonly_user: undefined, readonly_password: undefined });
              }
            }}
          >
            {t('dataSource.readonlyUseMain')}
          </Checkbox>
        </Form.Item>

        {!readonlyUseMain && (
          <>
            <Form.Item
              name="readonly_user"
              label={t('dataSource.readonlyUser')}
              extra={t('dataSource.readonlyUserHint')}
            >
              <Input autoComplete="off" placeholder={t('dataSource.readonlyUserPlaceholder')} />
            </Form.Item>

            <Form.Item
              name="readonly_password"
              label={t('dataSource.readonlyPassword')}
              extra={editing ? t('dataSource.passwordKeepHint') : undefined}
            >
              <Input.Password autoComplete="new-password" placeholder={editing ? '••••••' : undefined} />
            </Form.Item>
          </>
        )}
      </Form>

      {testResult ? (
        <Alert
          type={testResult.ok ? 'success' : 'error'}
          showIcon
          message={testResult.ok ? t('dataSource.testOk') : t('dataSource.testFailed')}
          description={
            testResult.ok ? `${testResult.database ?? ''} · ${testResult.version ?? ''}` : testResult.message
          }
        />
      ) : null}
    </Modal>
  );
});

ConnectionModal.displayName = 'ConnectionModal';

export default ConnectionModal;
