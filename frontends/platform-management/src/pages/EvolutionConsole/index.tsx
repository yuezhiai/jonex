import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Spin, Tabs } from 'antd';
import { useTranslation } from 'react-i18next';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { getEvolutionStatus, type EvolutionStatus } from '../../api/evolution';
import FailuresTab from './FailuresTab';
import RunsTab from './RunsTab';
import LedgersTab from './LedgersTab';
import DislikesTab from './DislikesTab';
import CorpusTab from './CorpusTab';
import { toEvolutionTabKey } from './constants';

/** 演化台容器：服务状态 + 降级 + 五 Tab（tab 落 ?tab=）。
 * 五个 Tab：失败诊断 / 运行列表 / 台账 / 用户点踩 / 语料（均已接线）。
 * `/status` 是租户管理员视图：失败（如 KB 管理员 403）只显示提示条，
 * 五个 Tab 照常可用。 */
export default function EvolutionConsole() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [status, setStatus] = useState<EvolutionStatus | null>(null);
  const [loading, setLoading] = useState(true);
  // `status` 是租户级视图（仅租户管理员）：KB 管理员会 403。它失败只降级成
  // 提示条，不能把整页（RunsTab / CorpusTab 等）一起挡掉。
  const [statusError, setStatusError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setStatusError(null);
    try {
      setStatus(await getEvolutionStatus());
    } catch (e: unknown) {
      setStatus(null);
      setStatusError(e instanceof Error ? e.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading && !status && !statusError) {
    return (
      <div className="yx-card" style={{ textAlign: 'center', padding: 64 }}>
        <Spin />
      </div>
    );
  }

  const activeTab = toEvolutionTabKey(searchParams.get('tab'));
  const activeRun = status?.active_run ?? null;

  return (
    <div>
      <div className="yx-page-title">
        <h1>{t('evolutionConsole.title')}</h1>
        <p style={{ color: '#64748b', margin: '4px 0 0', fontSize: 14 }}>
          {t('evolutionConsole.subtitle')}
        </p>
      </div>
      <div className="yx-card">
        {statusError && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('evolutionConsole.statusUnavailable')}
            description={statusError}
            action={<a onClick={load}>{t('common.retry')}</a>}
          />
        )}
        {activeRun && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('evolutionConsole.activeRunBanner', {
              runId: activeRun.run_id,
              stage: activeRun.stage,
            })}
            action={
              <a onClick={() => navigate(`/evolution/${encodeURIComponent(activeRun.run_id)}`)}>
                {t('evolutionConsole.viewRun')}
              </a>
            }
          />
        )}
        <Tabs
          activeKey={activeTab}
          onChange={(key) => setSearchParams({ tab: key })}
          items={[
            { key: 'failures', label: t('evolutionConsole.tabFailures'), children: <FailuresTab /> },
            {
              key: 'runs',
              label: t('evolutionConsole.tabRuns'),
              children: <RunsTab status={status} onChanged={load} />,
            },
            { key: 'ledgers', label: t('evolutionConsole.tabLedgers'), children: <LedgersTab /> },
            { key: 'dislikes', label: t('evolutionConsole.tabDislikes'), children: <DislikesTab /> },
            { key: 'corpus', label: t('evolutionConsole.tabCorpus'), children: <CorpusTab /> },
          ]}
        />
      </div>
    </div>
  );
}
