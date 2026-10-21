import { Alert } from 'antd';
import { useTranslation } from 'react-i18next';
import { useMetaFiltersState } from '../useMetaFilters';

/**
 * M1 筛选元数据拉取失败提示。
 *
 * 下拉会回退到静态候选、页面照常可用，所以是提示而不是错误态 —— 但不说一声，
 * 用户选不到某个来源 / 协议时会以为数据本身不存在。
 */
export default function MetaHint() {
  const { t } = useTranslation();
  const { failed } = useMetaFiltersState();

  if (!failed) return null;

  return (
    <Alert
      type="info"
      showIcon
      message={t('platformMonitor.metaUnavailable.title')}
      description={t('platformMonitor.metaUnavailable.description')}
    />
  );
}
