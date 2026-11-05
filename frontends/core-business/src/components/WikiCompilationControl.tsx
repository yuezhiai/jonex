import { Alert, Button, Space } from 'antd';
import { PauseOutlined, CaretRightOutlined, ReloadOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useKbPermission } from '@/hooks/useKbPermission';
import { useWikiCompilation } from '@/hooks/useWikiCompilation';

export default function WikiCompilationControl({ kbId, control, compact = false, showToggle = true }: {
  kbId: string;
  control: ReturnType<typeof useWikiCompilation>;
  compact?: boolean;
  /** 是否显示「暂停/恢复自动编译」按钮；jonex_wiki 知识库关闭，只保留状态提示 */
  showToggle?: boolean;
}) {
  const { t } = useTranslation();
  const { canWrite } = useKbPermission(kbId);
  const { state, loading, error, refresh, toggle } = control;
  const key = 'domainKnowledge.wikiCompilation';
  const status = control.status;
  const action = !showToggle ? null : error
    ? <Button icon={<ReloadOutlined />} onClick={() => void refresh()}>{t('common.retry')}</Button>
    : <Button disabled={!canWrite || !control.canToggle} loading={loading} onClick={() => void toggle()}
        icon={state?.paused ? <CaretRightOutlined /> : <PauseOutlined />}>
        {t(`${key}.${state?.paused ? 'resume' : 'pause'}`)}
      </Button>;
  if (compact) return <Space><span>{t(`${key}.${status}`)}</span>{action}</Space>;
  return <Alert type={error || status === 'failed' ? 'error' : state?.paused ? 'warning' : status === 'complete' ? 'success' : 'info'}
    title={t(`${key}.${status}`)} action={action}
    description={state ? t(`${key}.counts`, { published: state.published_source_count,
      parsed: state.parsed_source_count, tasks: state.tasks.length, pending: state.pending_source_count || 0, waiting: state.waiting_source_count || 0, failed: state.failed_source_count || 0 }) : undefined}
    style={{ marginBottom: 16, flexShrink: 0 }} />;
}
