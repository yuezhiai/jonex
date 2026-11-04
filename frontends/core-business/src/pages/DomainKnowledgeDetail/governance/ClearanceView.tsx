/** 治理 → 用户密级：租户用户列表 + 授予等级。 */
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Empty, message, Select, Spin, Table, Tag } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import {
  listGovernanceClearance, setGovernanceClearance,
  type GovernanceClearance,
} from '@/api/governance';
import { LEVEL_ORDER, LEVEL_TAGS, tagFor } from './governanceModel';

interface Props {
  kbId: string;
  canWrite?: boolean;
}

export default function ClearanceView({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<GovernanceClearance[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setItems((await listGovernanceClearance(kbId)).items);
    } catch (e: any) {
      setError(e?.message || t('domainKnowledge.governance.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [kbId, t]);

  useEffect(() => { void load(); }, [load]);

  const change = async (user: GovernanceClearance, level: string) => {
    setBusy(user.user_id);
    try {
      await setGovernanceClearance(kbId, {
        user_id: user.user_id,
        level: level as GovernanceClearance['level'],
      });
      await load();
    } catch (e: any) {
      message.error(e?.message || t('domainKnowledge.governance.saveFailed'));
    } finally {
      setBusy(null);
    }
  };

  const columns: ColumnsType<GovernanceClearance> = [
    {
      title: t('domainKnowledge.governance.user'), key: 'user',
      render: (_: unknown, row) => row.display_name || row.username || row.user_id,
    },
    {
      title: t('domainKnowledge.governance.level'), dataIndex: 'level', key: 'level', width: 220,
      render: (level: string, row) => (
        canWrite ? (
          <Select
            value={level}
            loading={busy === row.user_id}
            style={{ width: 180 }}
            onChange={(v) => void change(row, v)}
            options={LEVEL_ORDER.map((lv) => {
              const meta = tagFor(LEVEL_TAGS, lv);
              return { value: lv, label: t(meta.key) };
            })}
          />
        ) : (
          <Tag color={tagFor(LEVEL_TAGS, level).color}>
            {t(tagFor(LEVEL_TAGS, level).key)}
          </Tag>
        )
      ),
    },
    { title: t('domainKnowledge.governance.grantedBy'), dataIndex: 'granted_by', key: 'by' },
    { title: t('domainKnowledge.governance.note'), dataIndex: 'note', key: 'note' },
  ];

  if (loading) return <div style={{ padding: 24, textAlign: 'center' }}><Spin /></div>;
  if (error) {
    return <Alert type="error" showIcon
                  message={t('domainKnowledge.governance.loadFailed')} description={error}
                  action={<Button size="small" onClick={() => void load()}>{t('common.retry')}</Button>} />;
  }

  return (
    <div>
      <p className="yx-kb-graph-desc">
        {t('domainKnowledge.governance.clearanceHint')}
      </p>
      {items.length === 0 ? (
        <Empty description={t('domainKnowledge.governance.clearanceEmpty')} />
      ) : (
        <Table<GovernanceClearance> rowKey="user_id" columns={columns} dataSource={items}
                                   pagination={{ pageSize: 20, hideOnSinglePage: true }}
                                   size="middle" />
      )}
    </div>
  );
}
