import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { ToolBinding } from '@/types/domainKnowledge';
import { listToolBindings } from '@/api/ontologyToolBinding';

export interface UseToolBindingsResult {
  data: ToolBinding[];
  total: number;
  page: number;
  pageSize: number;
  loading: boolean;
  error: string | null;
  refresh: () => void;
  setPage: (page: number) => void;
}

export function useToolBindings(kbId: string, pageSize = 20): UseToolBindingsResult {
  const { t } = useTranslation();
  const [data, setData] = useState<ToolBinding[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!kbId) return;
    setLoading(true);
    setError(null);
    listToolBindings(kbId, page, pageSize)
      .then((res) => {
        setData(res.items);
        setTotal(res.total);
      })
      .catch((e: any) => setError(e?.message || t('toolBinding.fetchFailed')))
      .finally(() => setLoading(false));
  }, [kbId, page, pageSize, t]);

  useEffect(() => {
    load();
  }, [load]);

  return { data, total, page, pageSize, loading, error, refresh: load, setPage };
}
