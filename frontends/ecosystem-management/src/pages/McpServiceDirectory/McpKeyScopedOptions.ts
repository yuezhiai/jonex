import { useCallback, useState } from 'react';
import { listKnowledgeBases } from '@/api/spaces';
import { listMcpServices } from '@/api/mcpServices';

/**
 * 按领域空间加载知识库 / 已发布领域服务下拉选项（创建 / 编辑弹窗共用）。
 *
 * - loadScopedOptions(spaceId)：spaceId 为空加载全量（编辑 legacy Key 无 space 场景）
 * - clearScopedOptions()：清空选项（创建弹窗未选 space 时）
 */
export function useMcpScopedOptions() {
  const [scopedKbOptions, setScopedKbOptions] = useState<{ label: string; value: string }[]>([]);
  const [scopedServiceOptions, setScopedServiceOptions] = useState<{ label: string; value: string }[]>([]);

  const loadScopedOptions = useCallback(async (spaceId?: string) => {
    try {
      const kbs = await listKnowledgeBases(spaceId);
      setScopedKbOptions(kbs.map((kb) => ({ label: kb.name ?? kb.id, value: kb.id })));
    } catch {
      // 失败静默，下拉为空
      setScopedKbOptions([]);
    }
    try {
      const svcs = await listMcpServices(spaceId ? { status: 'published', space_id: spaceId } : { status: 'published' });
      setScopedServiceOptions(svcs.items.map((s) => ({ label: s.name, value: s.id })));
    } catch {
      // 失败静默，下拉为空
      setScopedServiceOptions([]);
    }
  }, []);

  const clearScopedOptions = useCallback(() => {
    setScopedKbOptions([]);
    setScopedServiceOptions([]);
  }, []);

  return { scopedKbOptions, scopedServiceOptions, loadScopedOptions, clearScopedOptions };
}
