import { useEffect, useRef } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import useDocumentTitle from '@/hooks/useDocumentTitle';
import { useStore } from '@/store';
import { onSpaceChanged, onSpacesInvalidated } from '@jonex/shell-sdk';
import RouteSync from '@/components/RouteSync';

const AppLayout = () => {
  const { global } = useStore();
  const navigate = useNavigate();
  const location = useLocation();
  // 订阅回调仅在挂载时注册（deps 为空），闭包内的 location 会过期；
  // 用 ref 始终指向最新 location，避免切换空间时误判当前路径。
  const locationRef = useRef(location);
  locationRef.current = location;
  useDocumentTitle();

  useEffect(() => {
    global.loadSpaces();

    const unsubChanged = onSpaceChanged((spaceId) => {
      // 时序：先更新 store，保证列表页挂载时读到的是新空间 ID，再决定是否跳转。
      global.setCurrentSpaceId(spaceId, { persist: false, broadcast: false });

      // 知识库详情类页面（domain-knowledge/:id/**）切换空间 → 跳回列表页。
      // 列表页 /domain-knowledge 不含第二段，startsWith('/domain-knowledge/') 不命中，不误跳。
      if (locationRef.current.pathname.startsWith('/domain-knowledge/')) {
        navigate('/domain-knowledge');
      }
    });
    const unsubInvalidated = onSpacesInvalidated(() => {
      global.refreshSpaces();
    });

    return () => {
      unsubChanged();
      unsubInvalidated();
    };
  }, []);

  return (
    <>
      <RouteSync />
      <Outlet />
    </>
  );
};

export default AppLayout;
