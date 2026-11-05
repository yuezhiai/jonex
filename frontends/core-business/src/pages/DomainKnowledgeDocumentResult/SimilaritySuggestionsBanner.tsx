import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Space, message } from 'antd';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import {
  dismissSimilaritySuggestion,
  getSimilaritySuggestions,
  mergeSimilaritySuggestion,
} from '@/api/domainKnowledge';
import type { SimilaritySuggestion } from '@/types/domainKnowledge';

interface Props {
  docId: string;
  canWrite: boolean;
  /** 父级详情刷新标记（如 doc.updatedAt）：变化时重新拉取建议，避免不可合并状态长期陈旧。 */
  refreshKey?: string;
  /** 合并成功后源文档被删除，父级负责跳转/刷新。 */
  onMerged: () => void;
}

type BusyState = { id: string; action: 'merge' | 'dismiss' } | null;

const BLOCK_STATES = ['unready', 'pending', 'compiling', 'failed'] as const;
type BlockState = (typeof BLOCK_STATES)[number];

function isBlockState(value: string): value is BlockState {
  return (BLOCK_STATES as readonly string[]).includes(value);
}

/** 后端 block_reason 枚举 → 可读文案；未知码原样返回，便于排查后端新状态。 */
function describeReason(reason: string, t: TFunction): string {
  return isBlockState(reason) ? t(`domainKnowledge.similarity.state.${reason}`) : reason;
}

export default function SimilaritySuggestionsBanner({ docId, canWrite, refreshKey, onMerged }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<SimilaritySuggestion[]>([]);
  // 单条操作在途标记：只有被点击的按钮转圈；其余按钮统一禁用，避免并发操作。
  const [busy, setBusy] = useState<BusyState>(null);
  // 请求归属：docId/refreshKey 切换或轮询乱序返回时丢弃过期结果。
  const seq = useRef(0);

  const load = useCallback(
    async ({ clear = false }: { clear?: boolean } = {}) => {
      // refreshKey 只作为重载触发器，不参与请求参数。
      void refreshKey;
      const my = ++seq.current;
      if (clear) setItems([]);
      try {
        const next = await getSimilaritySuggestions(docId);
        if (my !== seq.current) return;
        setItems(next);
      } catch {
        if (my !== seq.current) return;
        // 建议条是辅助信息：加载失败静默降级为不展示，不打断文档详情主流程。
        setItems([]);
      }
    },
    [docId, refreshKey],
  );

  useEffect(() => {
    void load({ clear: true });
  }, [load]);

  // 有不可合并的条目时每 10s 刷新一次（源/目标文档可能仍在解析或编译）；
  // 全部可合并（或组件卸载）即停止。轮询失败只更新数据，不弹全局提示。
  const hasBlocked = items.some((item) => !item.canMerge);
  useEffect(() => {
    if (!hasBlocked) return;
    const timer = setInterval(() => {
      void load();
    }, 10_000);
    return () => clearInterval(timer);
  }, [hasBlocked, load]);

  if (!items.length) return null;

  const merge = async (item: SimilaritySuggestion) => {
    setBusy({ id: item.id, action: 'merge' });
    let merged = false;
    try {
      const result = await mergeSimilaritySuggestion(docId, item.id);
      merged = true;
      // 合并成功：源文档已删除，先从列表移除该条。
      setItems((prev) => prev.filter((x) => x.id !== item.id));
      if (result.reparseError) {
        // 合并已成功，只是重新解析失败：提示用户，但仍需让父级跳转（源文档已删除）。
        message.error(t('domainKnowledge.similarity.mergeReparseFailed'));
      } else {
        message.success(t('domainKnowledge.similarity.merged', { version: result.versionNo }));
      }
    } catch (err) {
      message.error((err as { message?: string })?.message || t('domainKnowledge.similarity.mergeFailed'));
    } finally {
      setBusy(null);
    }
    // 父级回调放在 try/catch 之外：跳转/刷新抛错不得误报为「合并失败」。
    if (!merged) return;
    try {
      onMerged();
    } catch {
      // 合并已成功，父级回调异常不影响结果提示。
    }
  };

  const dismiss = async (item: SimilaritySuggestion) => {
    setBusy({ id: item.id, action: 'dismiss' });
    try {
      await dismissSimilaritySuggestion(docId, item.id);
      setItems((prev) => prev.filter((x) => x.id !== item.id));
    } catch (err) {
      message.error((err as { message?: string })?.message || t('domainKnowledge.similarity.dismissFailed'));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Space orientation="vertical" size={8} style={{ width: '100%', marginBottom: 12 }}>
      {items.map((item) => {
        // blockReason 表示目标文档不可合并；空时回退到源文档自身原因，避免禁用按钮无解释。
        const reason = item.blockReason || item.sourceBlockReason;
        return (
          <Alert
            key={item.id}
            type="warning"
            showIcon
            title={t('domainKnowledge.similarity.banner', {
              name: item.targetFileName,
              percent: Math.round(item.similarity * 100),
            })}
            description={
              !item.canMerge && reason
                ? t('domainKnowledge.similarity.notCompiled', { state: describeReason(reason, t) })
                : undefined
            }
            action={
              canWrite ? (
                <Space>
                  <Button
                    size="small"
                    disabled={!item.canMerge || busy !== null}
                    loading={busy?.id === item.id && busy.action === 'merge'}
                    onClick={() => void merge(item)}
                  >
                    {t('domainKnowledge.similarity.merge')}
                  </Button>
                  <Button
                    size="small"
                    disabled={busy !== null}
                    loading={busy?.id === item.id && busy.action === 'dismiss'}
                    onClick={() => void dismiss(item)}
                  >
                    {t('domainKnowledge.similarity.dismiss')}
                  </Button>
                </Space>
              ) : undefined
            }
          />
        );
      })}
    </Space>
  );
}
