// 本库工具试运行：填入参 JSON → 真实调用对方接口 → 展示状态与返回。
import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, Button, Descriptions, Input, Modal, Tag, Typography } from 'antd';
import type { KbTool, KbToolRunResult } from '@/types/domainKnowledge';
import { testKbTool } from '@/api/kbTool';

interface Props {
  open: boolean;
  tool: KbTool | null;
  onClose: () => void;
}

/** 按入参 schema 生成一份示例入参（只列字段名，值留空） */
function sampleArgs(schema: Record<string, any>): string {
  const props = (schema?.properties || {}) as Record<string, any>;
  const sample = Object.fromEntries(Object.keys(props).map((k) => [k, '']));
  return JSON.stringify(sample, null, 2);
}

const STATUS_COLOR: Record<KbToolRunResult['status'], string> = {
  executed: 'green',
  failed: 'red',
  no_runtime: 'default',
  invalid_input: 'orange',
  tool_unavailable: 'orange',
};

export default function KbToolTestModal({ open, tool, onClose }: Props) {
  const { t } = useTranslation();
  const [argsText, setArgsText] = useState('{}');
  const [argsError, setArgsError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<KbToolRunResult | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !tool) return;
    setArgsText(sampleArgs(tool.inputSchema));
    setArgsError(null);
    setResult(null);
    setRequestError(null);
  }, [open, tool]);

  const run = async () => {
    if (!tool) return;
    let args: Record<string, unknown>;
    try {
      const parsed = JSON.parse(argsText || '{}');
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not object');
      args = parsed;
    } catch {
      setArgsError(t('kbTool.testArgsInvalid'));
      return;
    }
    setArgsError(null);
    setRequestError(null);
    setRunning(true);
    try {
      setResult(await testKbTool(tool.id, args));
    } catch (e: any) {
      setResult(null);
      setRequestError(e?.message || t('common.operationFailed'));
    } finally {
      setRunning(false);
    }
  };

  return (
    <Modal
      title={t('kbTool.testTitle', { name: tool?.name || '' })}
      open={open}
      onCancel={onClose}
      width={720}
      destroyOnHidden
      footer={[
        <Button key="close" onClick={onClose}>
          {t('common.close')}
        </Button>,
        <Button key="run" type="primary" loading={running} onClick={run}>
          {t('kbTool.testRun')}
        </Button>,
      ]}
    >
      <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={t('kbTool.testWarning')} />
      <Typography.Text strong>{t('kbTool.testArgs')}</Typography.Text>
      <Input.TextArea
        rows={6}
        value={argsText}
        onChange={(e) => setArgsText(e.target.value)}
        style={{ fontFamily: 'monospace', fontSize: 12, marginTop: 6 }}
        status={argsError ? 'error' : undefined}
      />
      {argsError && <Typography.Text type="danger">{argsError}</Typography.Text>}

      {requestError && <Alert type="error" showIcon style={{ marginTop: 12 }} message={requestError} />}

      {result && (
        <div style={{ marginTop: 16 }}>
          <Descriptions size="small" column={3} bordered>
            <Descriptions.Item label={t('kbTool.testStatus')}>
              <Tag color={STATUS_COLOR[result.status]}>{t(`kbTool.status.${result.status}`)}</Tag>
            </Descriptions.Item>
            <Descriptions.Item label="HTTP">{result.httpStatus ?? '-'}</Descriptions.Item>
            <Descriptions.Item label={t('kbTool.testDuration')}>
              {result.durationMs != null ? `${result.durationMs} ms` : '-'}
            </Descriptions.Item>
          </Descriptions>
          {result.error && <Alert type="error" showIcon style={{ marginTop: 8 }} message={result.error} />}
          {!!result.missingInputs?.length && (
            <Alert
              type="warning"
              showIcon
              style={{ marginTop: 8 }}
              message={t('kbTool.testMissing', { names: result.missingInputs.join(', ') })}
            />
          )}
          {result.output !== undefined && (
            <>
              <Typography.Text strong style={{ display: 'block', marginTop: 12 }}>
                {t('kbTool.testOutput')}
                {result.outputTruncated ? `（${t('kbTool.testTruncated')}）` : ''}
              </Typography.Text>
              <pre
                style={{
                  background: '#f8fafc',
                  padding: 12,
                  borderRadius: 8,
                  fontSize: 12,
                  maxHeight: 320,
                  overflow: 'auto',
                  marginTop: 6,
                }}
              >
                {typeof result.output === 'string' ? result.output : JSON.stringify(result.output, null, 2)}
              </pre>
            </>
          )}
        </div>
      )}
    </Modal>
  );
}
