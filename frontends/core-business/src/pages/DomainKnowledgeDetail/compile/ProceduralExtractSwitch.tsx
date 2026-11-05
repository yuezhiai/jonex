// 动态本体开关：本库后续抽取是否额外跑技能 / 动作（Skill / Action）规程抽取。
// 只挂在本体型（lightrag）库的编译页；Wiki 编译型库不走本体抽取，后端也会拒绝开启。
import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Space, Switch, Tag, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { getDomainKnowledgeDetail, updateKnowledgeInfo } from '@/api/domainKnowledge';

interface Props {
  kbId: string;
  canWrite: boolean;
}

export default function ProceduralExtractSwitch({ kbId, canWrite }: Props) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [enabled, setEnabled] = useState(false);
  const [available, setAvailable] = useState(false);
  const [saving, setSaving] = useState(false);
  // 异步返回时丢弃过期结果：切库或卸载后不再改状态、不再弹提示
  const kbIdRef = useRef(kbId);
  const mountedRef = useRef(true);
  kbIdRef.current = kbId;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setLoadError(false);
    setEnabled(false);
    getDomainKnowledgeDetail(kbId)
      .then((detail) => {
        if (!alive) return;
        setEnabled(detail.proceduralExtract === true);
        setAvailable(detail.proceduralExtractAvailable === true);
      })
      .catch(() => {
        if (alive) setLoadError(true);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [kbId, reloadToken]);

  const handleToggle = async (checked: boolean) => {
    const requestKbId = kbId;
    const previous = enabled;
    setEnabled(checked);
    setSaving(true);
    try {
      await updateKnowledgeInfo(requestKbId, { procedural_extract: checked });
      if (!mountedRef.current || kbIdRef.current !== requestKbId) return;
      message.success(checked ? t('compile.procedural.enabledMsg') : t('compile.procedural.disabledMsg'));
    } catch (err) {
      if (!mountedRef.current || kbIdRef.current !== requestKbId) return;
      setEnabled(previous);
      message.error((err as { message?: string })?.message || t('common.saveFailed'));
    } finally {
      if (mountedRef.current && kbIdRef.current === requestKbId) setSaving(false);
    }
  };

  if (loading) return null;

  if (loadError) {
    return (
      <Alert
        type="error"
        showIcon
        style={{ marginBottom: 12 }}
        title={t('compile.procedural.loadFailed')}
        action={
          <Button size="small" onClick={() => setReloadToken((v) => v + 1)}>
            {t('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div style={{ marginBottom: 12, padding: '12px 16px', background: '#f8fafc', borderRadius: 8 }}>
      <Space orientation="vertical" size={6} style={{ width: '100%' }}>
        <Space wrap>
          <Typography.Text strong>{t('compile.procedural.title')}</Typography.Text>
          <Switch
            aria-label={t('compile.procedural.title')}
            checked={enabled}
            loading={saving}
            disabled={!canWrite || saving}
            onChange={(checked) => {
              void handleToggle(checked);
            }}
          />
          {enabled && !available && <Tag color="orange">{t('compile.procedural.notEffective')}</Tag>}
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          {t('compile.procedural.hint')}
        </Typography.Text>
        {!available && (
          <Alert type="warning" showIcon title={t('compile.procedural.masterOff')} />
        )}
        {!canWrite && <Alert type="info" showIcon title={t('compile.procedural.readonly')} />}
      </Space>
    </div>
  );
}
