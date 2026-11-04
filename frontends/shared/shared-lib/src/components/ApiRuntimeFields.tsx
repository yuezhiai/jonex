// 可执行工具的运行时配置表单段（平台工具目录 / 知识库工具共用）。
// 字段格式与后端 jonex_core/common/tool_runtime.py 一一对应：echo 回显 / api 调用 HTTP API。
// 必须放在一个 antd <Form> 里使用（经 Form.useFormInstance 读写字段）。
import React from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Checkbox, Form, Input, InputNumber, Select, Space } from 'antd';

const { TextArea } = Input;

export type RuntimeType = 'none' | 'echo' | 'api';
export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
export type AuthType = 'none' | 'bearer' | 'api_key' | 'basic';

export interface ApiRuntimeFormValues {
  runtime_type: RuntimeType;
  api_method?: HttpMethod;
  api_url?: string;
  api_query?: string;
  api_headers?: string;
  api_body?: string;
  auth_type?: AuthType;
  auth_in?: 'header' | 'query';
  auth_name?: string;
  auth_username?: string;
  secret?: string;
  clear_secret?: boolean;
  api_extract?: string;
  timeout?: number | null;
  /** echo 的模拟查表（JSON 文本）：{"by": 入参名, "rows": {值: 数据}, "default"?} */
  echo_mock?: string;
}

export interface RuntimeSubmitPart {
  runtime: Record<string, unknown> | null;
  /** 新凭据；留空表示不修改 */
  secret?: string;
  clear_secret?: boolean;
}

export const RUNTIME_FIELD_NAMES = [
  'runtime_type', 'api_method', 'api_url', 'api_query', 'api_headers', 'api_body',
  'auth_type', 'auth_in', 'auth_name', 'auth_username', 'secret', 'clear_secret',
  'api_extract', 'timeout', 'echo_mock',
] as const;

const MAX_TIMEOUT = 30;

function pretty(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (typeof value === 'object' && !Array.isArray(value) && Object.keys(value as object).length === 0) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return '';
  }
}

/** 解析 JSON 文本：空 → undefined；非法 → 抛错（由字段校验先拦住） */
function parseJson(text?: string): unknown {
  const raw = (text ?? '').trim();
  if (!raw) return undefined;
  return JSON.parse(raw);
}

/** 后端运行时 → 表单初值 */
export function runtimeToFormValues(runtime: Record<string, any> | null | undefined): ApiRuntimeFormValues {
  if (!runtime || !runtime.type) return { runtime_type: 'none', api_method: 'POST', auth_type: 'none' };
  if (runtime.type === 'echo') {
    return { runtime_type: 'echo', api_method: 'POST', auth_type: 'none', echo_mock: pretty(runtime.mock) };
  }
  const auth = runtime.auth || { type: 'none' };
  return {
    runtime_type: 'api',
    api_method: runtime.method || 'POST',
    api_url: runtime.url || '',
    api_query: pretty(runtime.query),
    api_headers: pretty(runtime.headers),
    api_body: pretty(runtime.body),
    auth_type: auth.type || 'none',
    auth_in: auth.in || 'header',
    auth_name: auth.name || '',
    auth_username: auth.username || '',
    api_extract: pretty(runtime.response?.extract),
    timeout: runtime.timeout ?? null,
  };
}

/** 表单值 → 提交给后端的 runtime / secret / clear_secret */
export function formValuesToRuntime(values: ApiRuntimeFormValues): RuntimeSubmitPart {
  if (values.runtime_type === 'none') return { runtime: null };
  if (values.runtime_type === 'echo') {
    const mock = parseJson(values.echo_mock);
    return { runtime: mock !== undefined ? { type: 'echo', mock } : { type: 'echo' } };
  }
  const auth: Record<string, unknown> = { type: values.auth_type || 'none' };
  if (values.auth_type === 'api_key') {
    auth.in = values.auth_in || 'header';
    if ((values.auth_name || '').trim()) auth.name = values.auth_name!.trim();
  }
  if (values.auth_type === 'basic') auth.username = (values.auth_username || '').trim();
  const runtime: Record<string, unknown> = {
    type: 'api',
    method: values.api_method || 'POST',
    url: (values.api_url || '').trim(),
    auth,
  };
  const query = parseJson(values.api_query);
  if (query !== undefined) runtime.query = query;
  const headers = parseJson(values.api_headers);
  if (headers !== undefined) runtime.headers = headers;
  const body = parseJson(values.api_body);
  if (body !== undefined) runtime.body = body;
  const extract = parseJson(values.api_extract);
  if (extract !== undefined) runtime.response = { extract };
  if (values.timeout) runtime.timeout = values.timeout;
  const out: RuntimeSubmitPart = { runtime };
  if ((values.secret || '').length) out.secret = values.secret;
  if (values.clear_secret) out.clear_secret = true;
  return out;
}

export interface ApiRuntimeFieldsProps {
  /** 是否允许「无运行时」（平台目录里的 MCP 制品类工具没有可调入口） */
  allowNone?: boolean;
  /** 已保存过凭据：凭据框留空表示不修改 */
  hasSecret?: boolean;
}

export default function ApiRuntimeFields({ allowNone = true, hasSecret = false }: ApiRuntimeFieldsProps) {
  const { t } = useTranslation();
  const form = Form.useFormInstance();
  const runtimeType = Form.useWatch('runtime_type', form) as RuntimeType | undefined;
  const authType = Form.useWatch('auth_type', form) as AuthType | undefined;
  const method = Form.useWatch('api_method', form) as HttpMethod | undefined;
  const clearSecret = Form.useWatch('clear_secret', form) as boolean | undefined;

  const jsonRule = (kind: 'object' | 'any') => ({
    validator: (_: unknown, value?: string) => {
      const raw = (value ?? '').trim();
      if (!raw) return Promise.resolve();
      try {
        const parsed = JSON.parse(raw);
        if (kind === 'object' && (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed))) {
          return Promise.reject(new Error(t('toolRuntime.jsonObjectInvalid')));
        }
        if (kind === 'any' && (parsed === null || typeof parsed !== 'object')) {
          return Promise.reject(new Error(t('toolRuntime.jsonBodyInvalid')));
        }
        return Promise.resolve();
      } catch {
        return Promise.reject(new Error(t('toolRuntime.jsonInvalid')));
      }
    },
  });

  const typeOptions = [
    ...(allowNone ? [{ label: t('toolRuntime.typeNone'), value: 'none' }] : []),
    { label: t('toolRuntime.typeEcho'), value: 'echo' },
    { label: t('toolRuntime.typeApi'), value: 'api' },
  ];
  const needsSecret = runtimeType === 'api' && authType && authType !== 'none';
  const hasBody = method === 'POST' || method === 'PUT' || method === 'PATCH';
  const mono: React.CSSProperties = { fontFamily: 'monospace', fontSize: 12 };

  return (
    <>
      <Form.Item name="runtime_type" label={t('toolRuntime.type')} extra={t('toolRuntime.typeHint')}>
        <Select options={typeOptions} />
      </Form.Item>

      {runtimeType === 'echo' && (
        <Form.Item
          name="echo_mock"
          label={t('toolRuntime.mock')}
          extra={t('toolRuntime.mockHint')}
          rules={[jsonRule('object')]}
        >
          <TextArea
            rows={5}
            style={mono}
            placeholder={'{"by": "code", "rows": {"P001": {"name": "示例产品", "category": "信用卡"}}}'}
          />
        </Form.Item>
      )}

      {runtimeType === 'api' && (
        <>
          <div style={{ display: 'flex', gap: 12 }}>
            <Form.Item name="api_method" label={t('toolRuntime.method')} style={{ width: 130 }}>
              <Select options={['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((m) => ({ label: m, value: m }))} />
            </Form.Item>
            <Form.Item
              name="api_url"
              label={t('toolRuntime.url')}
              style={{ flex: 1 }}
              extra={t('toolRuntime.urlHint')}
              rules={[
                { required: true, whitespace: true, message: t('toolRuntime.urlRequired') },
                { pattern: /^https?:\/\/[^\s{}/]+(\/\S*)?$/i, message: t('toolRuntime.urlInvalid') },
              ]}
            >
              <Input placeholder="https://api.example.com/cards/{card_no}/status" />
            </Form.Item>
          </div>

          <Form.Item name="api_query" label={t('toolRuntime.query')} extra={t('toolRuntime.queryHint')} rules={[jsonRule('object')]}>
            <TextArea rows={2} style={mono} placeholder='{"channel": "{channel}", "lang": "zh"}' />
          </Form.Item>
          <Form.Item name="api_headers" label={t('toolRuntime.headers')} extra={t('toolRuntime.headersHint')} rules={[jsonRule('object')]}>
            <TextArea rows={2} style={mono} placeholder='{"X-Biz-Channel": "ontology"}' />
          </Form.Item>
          {hasBody && (
            <Form.Item name="api_body" label={t('toolRuntime.body')} extra={t('toolRuntime.bodyHint')} rules={[jsonRule('any')]}>
              <TextArea rows={4} style={mono} placeholder='{"card_no": "{card_no}", "reason": "{reason}"}' />
            </Form.Item>
          )}

          <Space style={{ width: '100%' }} align="start" wrap>
            <Form.Item name="auth_type" label={t('toolRuntime.auth')} style={{ minWidth: 180 }}>
              <Select
                options={[
                  { label: t('toolRuntime.authNone'), value: 'none' },
                  { label: 'Bearer Token', value: 'bearer' },
                  { label: 'API Key', value: 'api_key' },
                  { label: 'Basic', value: 'basic' },
                ]}
              />
            </Form.Item>
            {authType === 'api_key' && (
              <>
                <Form.Item name="auth_in" label={t('toolRuntime.authIn')} style={{ minWidth: 120 }}>
                  <Select
                    options={[
                      { label: t('toolRuntime.authInHeader'), value: 'header' },
                      { label: t('toolRuntime.authInQuery'), value: 'query' },
                    ]}
                  />
                </Form.Item>
                <Form.Item name="auth_name" label={t('toolRuntime.authName')} style={{ minWidth: 180 }}>
                  <Input placeholder="X-API-Key" />
                </Form.Item>
              </>
            )}
            {authType === 'basic' && (
              <Form.Item
                name="auth_username"
                label={t('toolRuntime.authUsername')}
                style={{ minWidth: 200 }}
                rules={[{ required: true, whitespace: true, message: t('toolRuntime.authUsernameRequired') }]}
              >
                <Input autoComplete="off" />
              </Form.Item>
            )}
          </Space>

          {needsSecret && (
            <>
              <Form.Item
                name="secret"
                label={authType === 'basic' ? t('toolRuntime.secretPassword') : t('toolRuntime.secret')}
                extra={hasSecret ? t('toolRuntime.secretKeepHint') : t('toolRuntime.secretHint')}
                rules={hasSecret ? [] : [{ required: true, message: t('toolRuntime.secretRequired') }]}
              >
                <Input.Password
                  autoComplete="new-password"
                  disabled={!!clearSecret}
                  placeholder={hasSecret ? t('toolRuntime.secretKeepPlaceholder') : undefined}
                />
              </Form.Item>
              {hasSecret && (
                <Form.Item name="clear_secret" valuePropName="checked" style={{ marginTop: -12 }}>
                  <Checkbox>{t('toolRuntime.clearSecret')}</Checkbox>
                </Form.Item>
              )}
            </>
          )}

          <Form.Item name="api_extract" label={t('toolRuntime.extract')} extra={t('toolRuntime.extractHint')} rules={[jsonRule('object')]}>
            <TextArea rows={2} style={mono} placeholder='{"status": "$.data.status"}' />
          </Form.Item>
          <Form.Item name="timeout" label={t('toolRuntime.timeout')} extra={t('toolRuntime.timeoutHint', { max: MAX_TIMEOUT })}>
            <InputNumber min={0.5} max={MAX_TIMEOUT} step={1} style={{ width: 160 }} />
          </Form.Item>
          <Alert type="info" showIcon style={{ marginBottom: 16 }} message={t('toolRuntime.securityHint')} />
        </>
      )}
    </>
  );
}
