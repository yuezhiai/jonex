import React, { useState, useRef, forwardRef, useImperativeHandle } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Form, Input, Tag, Typography, Descriptions, Space, Button, Divider } from 'antd';
import { testCallService } from '@/api/mcpServices';
import type { McpServiceItem, TestCallResponse } from '@/api/mcpServices';
import { safeMessage } from '@/utils/safeMessage';
import './index.css';

/** 测试调用弹窗对外暴露的句柄 */
export type TestCallModalHandle = {
  /** 打开弹窗并绑定指定服务 */
  open: (record: McpServiceItem) => void;
};

interface TestCallModalProps {}

/** 一次测试调用结果（附带递增 id 与原始 query，便于多次测试追加展示） */
interface TestCallResultItem extends TestCallResponse {
  id: number;
  query: string;
}

const TestCallModal = forwardRef<TestCallModalHandle, TestCallModalProps>((_props, ref) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [record, setRecord] = useState<McpServiceItem | null>(null);
  const [testing, setTesting] = useState(false);
  const [results, setResults] = useState<TestCallResultItem[]>([]);
  const [form] = Form.useForm<{ query: string }>();
  const resultIdRef = useRef(0);

  // 对外暴露打开方法：重置表单与历史结果，然后展示弹窗
  useImperativeHandle(
    ref,
    () => ({
      open(rec) {
        setRecord(rec);
        setResults([]);
        form.resetFields();
        setOpen(true);
      },
    }),
    [form],
  );

  /** 发起测试调用（一期为模拟回答），结果置顶追加展示 */
  const handleTest = async () => {
    if (!record) return;
    let values: { query: string };
    try {
      values = await form.validateFields();
    } catch {
      return; // 校验失败，错误已由 Form 展示
    }
    setTesting(true);
    try {
      const result = await testCallService(record.id, values.query.trim());
      setResults((prev) => [{ ...result, id: resultIdRef.current++, query: values.query.trim() }, ...prev]);
    } catch (err: unknown) {
      safeMessage.error(err instanceof Error ? err.message : t('mcpServiceDirectory.testCallFailed'));
    } finally {
      setTesting(false);
    }
  };

  return (
    <Modal
      title={t('mcpServiceDirectory.testCallTitle', { name: record?.name || '-' })}
      open={open}
      onCancel={() => setOpen(false)}
      footer={
        <Space>
          <Button onClick={() => setOpen(false)}>{t('common.close')}</Button>
          <Button type="primary" loading={testing} onClick={handleTest}>
            {t('mcpServiceDirectory.runTest')}
          </Button>
        </Space>
      }
      width={640}
      destroyOnHidden
    >
      <Form form={form} layout="vertical">
        <Form.Item
          name="query"
          label={t('mcpServiceDirectory.queryLabel')}
          rules={[
            { required: true, whitespace: true, message: t('mcpServiceDirectory.queryRequired') },
            { max: 2000, message: t('mcpServiceDirectory.queryMaxLength') },
          ]}
        >
          <Input.TextArea rows={4} placeholder={t('mcpServiceDirectory.queryPlaceholder')} maxLength={2000} showCount />
        </Form.Item>
      </Form>

      {results.length > 0 && (
        <>
          <Divider />
          <div style={{ maxHeight: 320, overflow: 'auto' }}>
            {results.map((r) => (
              <div key={r.id} className="mcp-svc-test-result" style={{ marginBottom: 12 }}>
                <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
                  Q: {r.query}
                </Typography.Text>
                <Typography.Paragraph style={{ marginBottom: 8 }}>
                  <Tag color="orange" style={{ marginRight: 8 }}>
                    {t('mcpServiceDirectory.resultMock')}
                  </Tag>
                  {r.answer}
                </Typography.Paragraph>
                <Space size={4} wrap style={{ marginBottom: 8 }}>
                  {r.kb_names && r.kb_names.length > 0 ? (
                    r.kb_names.map((n) => <Tag key={n}>{n}</Tag>)
                  ) : (
                    <Tag>{t('mcpServiceDirectory.noKb')}</Tag>
                  )}
                </Space>
                <Descriptions column={2} size="small">
                  <Descriptions.Item label={t('mcpServiceDirectory.resultRelevance')}>
                    {(r.relevance * 100).toFixed(1)}%
                  </Descriptions.Item>
                  <Descriptions.Item label={t('mcpServiceDirectory.resultLatency')}>
                    {t('mcpServiceDirectory.resultLatencyMs', { ms: r.latency_ms })}
                  </Descriptions.Item>
                </Descriptions>
              </div>
            ))}
          </div>
        </>
      )}
    </Modal>
  );
});

TestCallModal.displayName = 'TestCallModal';

export default TestCallModal;
