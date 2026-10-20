import type { ReactNode } from 'react';
import { Alert, Button, Drawer, Skeleton } from 'antd';
import { useTranslation } from 'react-i18next';
import type { DetailError } from './useDetail';

interface Props {
  /** 非空即打开；关闭传 false（与 target 同源，由页面决定） */
  open: boolean;
  title: ReactNode;
  onClose: () => void;
  loading: boolean;
  error: DetailError | null;
  onRetry: () => void;
  children: ReactNode;
  width?: number;
}

/**
 * 四个钻取抽屉的公共外壳：抽屉 + 三态。
 *
 * 四个详情接口的失败处理只有「404 与否」这一处分歧，其余（骨架、错误提示、重试）完全一致，
 * 放在这里一次说清；各内容组件只管「拿到数据后长什么样」。
 *
 * 三态的判定顺序是 loading → error → children：请求期间必须让 Skeleton 顶掉旧数据，
 * 否则切换行时抽屉会先显示上一行的内容再跳变。
 */
export default function DetailDrawer({ open, title, onClose, loading, error, onRetry, children, width = 640 }: Props) {
  const { t } = useTranslation();

  return (
    <Drawer title={title} open={open} onClose={onClose} width={width} destroyOnHidden>
      {loading ? (
        <Skeleton active title={false} paragraph={{ rows: 5 }} />
      ) : error ? (
        <Alert
          type="error"
          showIcon
          message={error.notFound ? t('dataOperations.drawer.notFound') : error.message}
          // 404 是终局，重试还是同一条 404；其余失败才给重试
          action={
            error.notFound ? undefined : (
              <Button size="small" onClick={onRetry}>
                {t('dataOperations.common.retry')}
              </Button>
            )
          }
        />
      ) : (
        children
      )}
    </Drawer>
  );
}
