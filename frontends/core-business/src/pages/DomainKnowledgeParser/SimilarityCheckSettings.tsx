import { useEffect, useId, useRef, useState } from 'react';
import { Alert, Button, InputNumber, Space, Switch, Typography, message } from 'antd';
import { useTranslation } from 'react-i18next';
import { getDomainKnowledgeDetail, updateKnowledgeInfo } from '@/api/domainKnowledge';
import { effectiveThreshold, normalizeThresholdInput } from './SimilarityCheckSettings.utils';

interface Props {
  kbId: string;
  canWrite: boolean;
}

interface SimilarityPatch {
  similarity_check_enabled?: boolean;
  similarity_check_threshold?: number | null;
}

type SaveResult = 'saved' | 'failed' | 'stale';

/** 百分数展示保留一位小数（0.955 → 95.5），避免 95.5 被静默改写成 96。 */
function thresholdToInput(value: number | null): string {
  return String(Math.round(effectiveThreshold(value) * 1000) / 10);
}

export default function SimilarityCheckSettings({ kbId, canWrite }: Props) {
  const { t } = useTranslation();
  const hintId = useId();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [loadedKbId, setLoadedKbId] = useState('');
  const [reloadToken, setReloadToken] = useState(0);
  const [enabled, setEnabled] = useState(false);
  const [threshold, setThreshold] = useState<number | null>(null);
  // 输入框独立维护字符串态：直接受控于派生数字会让每次按键都被改写、无法录入。
  const [thresholdInput, setThresholdInput] = useState(thresholdToInput(null));

  // 请求归属与在途保护：异步返回时据此丢弃过期结果，避免回滚/提示串到别的 KB 或已卸载组件。
  const mountedRef = useRef(true);
  const kbIdRef = useRef(kbId);
  const savingRef = useRef(false);
  const saveSeqRef = useRef(0);
  kbIdRef.current = kbId;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!kbId) {
      setLoading(false);
      return;
    }
    let alive = true;
    // 立即清空旧值：kbId 变化的首帧不得显示上一个知识库的数据。
    setLoading(true);
    setLoadError(false);
    setEnabled(false);
    setThreshold(null);
    setThresholdInput(thresholdToInput(null));
    void getDomainKnowledgeDetail(kbId)
      .then((detail) => {
        if (!alive) return;
        setEnabled(detail.similarityCheckEnabled);
        setThreshold(detail.similarityCheckThreshold);
        setThresholdInput(thresholdToInput(detail.similarityCheckThreshold));
      })
      .catch(() => {
        if (!alive) return;
        setLoadError(true);
      })
      .finally(() => {
        if (!alive) return;
        setLoadedKbId(kbId);
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [kbId, t, reloadToken]);

  const save = async (patch: SimilarityPatch): Promise<SaveResult> => {
    if (loadError) return 'failed';
    const requestKbId = kbId;
    const seq = ++saveSeqRef.current;
    savingRef.current = true;
    setSaving(true);
    try {
      await updateKnowledgeInfo(requestKbId, patch);
      if (!mountedRef.current || kbIdRef.current !== requestKbId) return 'stale';
      message.success(t('common.saveSuccess'));
      return 'saved';
    } catch (err) {
      if (!mountedRef.current || kbIdRef.current !== requestKbId) return 'stale';
      message.error((err as { message?: string })?.message || t('common.saveFailed'));
      return 'failed';
    } finally {
      if (seq === saveSeqRef.current) {
        savingRef.current = false;
        if (mountedRef.current && kbIdRef.current === requestKbId) setSaving(false);
      }
    }
  };

  const commitThreshold = async () => {
    if (savingRef.current) return;
    const next = normalizeThresholdInput(thresholdInput);
    if (next === null) {
      message.warning(t('parserConfig.similarity.thresholdInvalid'));
      setThresholdInput(thresholdToInput(threshold));
      return;
    }
    // 与「有效值」比较：null（继承全局）时输入框显示的就是全局默认，未改动不应落库为显式值。
    if (Math.abs(next - effectiveThreshold(threshold)) < 1e-9) {
      setThresholdInput(thresholdToInput(threshold));
      return;
    }
    const previous = threshold;
    setThreshold(next);
    // 成功后保留用户输入的字符串，不再按精度重排（95.5 不该被改成 96）。
    const result = await save({ similarity_check_threshold: next });
    if (result === 'failed') {
      setThreshold(previous);
      setThresholdInput(thresholdToInput(previous));
    }
  };

  const handleToggle = async (checked: boolean) => {
    if (savingRef.current) return;
    const previous = enabled;
    setEnabled(checked);
    const result = await save({ similarity_check_enabled: checked });
    if (result === 'failed') setEnabled(previous);
  };

  const handleReset = async () => {
    if (savingRef.current) return;
    const previous = threshold;
    setThreshold(null);
    setThresholdInput(thresholdToInput(null));
    const result = await save({ similarity_check_threshold: null });
    if (result === 'failed') {
      setThreshold(previous);
      setThresholdInput(thresholdToInput(previous));
    }
  };

  if (!kbId) return null;
  // kbId 尚未加载完成（含切换知识库的首帧）一律不渲染，避免旧值闪一下。
  if (loading || loadedKbId !== kbId) return null;

  if (loadError) {
    return (
      <div className="similarity-check-settings" style={{ marginBottom: 16 }}>
        <Alert
          type="error"
          showIcon
          title={t('common.loadFailed')}
          action={
            <Button size="small" onClick={() => setReloadToken((value) => value + 1)}>
              {t('common.retry')}
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div
      className="similarity-check-settings"
      style={{ marginBottom: 16, padding: 16, background: '#f8fafc', borderRadius: 8 }}
    >
      <Space orientation="vertical" size={8} style={{ width: '100%' }}>
        <Space>
          <Typography.Text strong>{t('parserConfig.similarity.enable')}</Typography.Text>
          {/* 点击开关不应先触发输入框 blur：否则 blur 提交开启 saving，开关点击会被吞掉。 */}
          <span onMouseDown={(event) => event.preventDefault()}>
            <Switch
              aria-label={t('parserConfig.similarity.enable')}
              checked={enabled}
              disabled={!canWrite || saving}
              onChange={(checked) => {
                void handleToggle(checked);
              }}
            />
          </span>
        </Space>
        <Typography.Text type="secondary" id={hintId}>
          {t('parserConfig.similarity.hint')}
        </Typography.Text>
        {enabled && (
          <Space>
            <Typography.Text>{t('parserConfig.similarity.threshold')}</Typography.Text>
            <InputNumber<string>
              min="50"
              max="99.9"
              step={0.1}
              precision={1}
              suffix="%"
              aria-label={t('parserConfig.similarity.threshold')}
              aria-describedby={hintId}
              value={thresholdInput}
              disabled={!canWrite || saving}
              onChange={(value) => setThresholdInput(value == null ? '' : String(value))}
              onBlur={() => {
                void commitThreshold();
              }}
              onPressEnter={() => {
                void commitThreshold();
              }}
            />
            <Button
              size="small"
              disabled={!canWrite || saving || threshold === null}
              // 先于 blur 阻止默认行为：点重置不应顺带提交输入框里的脏值。
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => void handleReset()}
            >
              {t('parserConfig.similarity.reset')}
            </Button>
          </Space>
        )}
        {!canWrite && (
          <div role="status">
            <Alert type="info" showIcon title={t('parserConfig.similarity.readonly')} />
          </div>
        )}
      </Space>
    </div>
  );
}
