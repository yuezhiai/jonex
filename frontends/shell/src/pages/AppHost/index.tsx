import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { Alert, Spin, Result, Button } from 'antd';
import { fetchAppManifest } from '../../api/manifest';
import { getAccessToken, getUser, logout } from '../../api/auth';
import { loadRemoteApp } from '../../app-registry/loadRemoteApp';
import { EMBED_QUERY_PARAM } from '@jonex/shell-sdk';
import type { AppManifestEntry, ShellUser } from '@jonex/shell-sdk';

type MountFn = (container: HTMLElement, context: unknown) => () => void;

interface RemoteLifecycle {
  appId: string;
  mountNode: HTMLDivElement;
  unmount: () => void;
  disposed: boolean;
}

const safeRemoveMountNode = (mountNode: HTMLDivElement) => {
  if (mountNode.parentNode) {
    mountNode.parentNode.removeChild(mountNode);
  }
};

const joinStandalonePath = (baseUrl: string, subPath: string, search: string, hash: string) => {
  const normalizedBase = baseUrl.endsWith('/') ? baseUrl.slice(0, -1) : baseUrl;
  const normalizedSubPath = subPath ? (subPath.startsWith('/') ? subPath : `/${subPath}`) : '';
  return `${normalizedBase}${normalizedSubPath || '/'}${search}${hash}`;
};

export default function AppHost() {
  const { t } = useTranslation();
  const { appId } = useParams<{ appId: string }>();
  const location = useLocation();
  const navigate = useNavigate();

  // Use state (via callback ref) instead of a plain ref so the mount effect
  // re-runs the moment the container node is actually committed to the DOM.
  // The container is only rendered once loading=false & appConfig is ready,
  // which can land in a later render than appConfig itself. A plain ref is not
  // a dependency, so the effect would early-return on the render where the
  // container is still absent and never re-run — leaving the remote unmounted.
  const [containerEl, setContainerEl] = useState<HTMLDivElement | null>(null);
  const lifecycleRef = useRef<RemoteLifecycle | null>(null);

  const [manifest, setManifest] = useState<{ apps: AppManifestEntry[] } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [remoteError, setRemoteError] = useState<string | null>(null);
  const [embeddedUrl, setEmbeddedUrl] = useState<string | null>(null);

  useEffect(() => {
    fetchAppManifest()
      .then(setManifest)
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  const appConfig = useMemo(() => {
    if (!manifest?.apps) return null;
    return manifest.apps.find((a) => a.id === appId) ?? null;
  }, [manifest, appId]);
  const fallbackUrl =
    (appConfig as { fallback?: { url?: string } } | null)?.fallback?.url || appConfig?.standaloneUrl || null;
  const preferStandalone = Boolean(
    (import.meta as { env?: { DEV?: boolean } }).env?.DEV &&
    (appConfig as { fallback?: { mode?: string } } | null)?.fallback?.mode === 'standalone' &&
    fallbackUrl,
  );
  const embeddedFallbackUrl = useMemo(() => {
    if (!appConfig?.basePath || !fallbackUrl) return null;
    const subPath = location.pathname.startsWith(appConfig.basePath)
      ? location.pathname.slice(appConfig.basePath.length)
      : '';
    // 带上 ?embed=1，让被内嵌的 standalone 子应用渲染无壳布局（不显示自己的侧边栏/顶栏），
    // 无需再由 Shell 注入 CSS 隐藏菜单。
    const searchParams = new URLSearchParams(location.search);
    searchParams.set(EMBED_QUERY_PARAM, '1');
    const embedSearch = `?${searchParams.toString()}`;
    return joinStandalonePath(fallbackUrl, subPath, embedSearch, location.hash);
  }, [appConfig?.basePath, fallbackUrl, location.hash, location.pathname, location.search]);

  const user = getUser();
  const userRoles: string[] = user?.roles ?? [];
  const appRoles: string[] = appConfig?.roles ?? [];
  const hasAppAccess = appRoles.length === 0 || appRoles.some((r) => userRoles.includes(r));

  const disposeLifecycle = useCallback((lifecycle: RemoteLifecycle | null) => {
    if (!lifecycle) return;
    if (lifecycle.disposed) return;

    lifecycle.disposed = true;
    if (lifecycleRef.current === lifecycle) {
      lifecycleRef.current = null;
    }

    try {
      lifecycle.unmount();
    } catch (err) {
      console.error(`[shell] Failed to unmount remote app "${lifecycle.appId}":`, err);
    } finally {
      safeRemoveMountNode(lifecycle.mountNode);
    }
  }, []);

  const disposeCurrentRemote = useCallback(() => {
    disposeLifecycle(lifecycleRef.current);
  }, [disposeLifecycle]);

  useEffect(() => {
    return () => {
      disposeCurrentRemote();
    };
  }, [disposeCurrentRemote]);

  useEffect(() => {
    if (!appConfig?.entry || !appConfig.enabled || !hasAppAccess) {
      disposeCurrentRemote();
      setRemoteLoading(false);
      setRemoteError(null);
      setEmbeddedUrl(null);
      return;
    }

    if (preferStandalone && embeddedFallbackUrl) {
      disposeCurrentRemote();
      setRemoteLoading(false);
      setRemoteError(null);
      setEmbeddedUrl(embeddedFallbackUrl);
      return;
    }

    if (embeddedUrl && embeddedFallbackUrl && embeddedUrl === embeddedFallbackUrl) {
      disposeCurrentRemote();
      setRemoteLoading(false);
      setRemoteError(null);
      return;
    }

    if (!containerEl) {
      disposeCurrentRemote();
      setRemoteLoading(false);
      setRemoteError(null);
      return;
    }

    let cancelled = false;
    let localLifecycle: RemoteLifecycle | null = null;
    const hostNode = containerEl;
    const mountNode = document.createElement('div');
    mountNode.dataset.remoteApp = appConfig.id;
    mountNode.style.minHeight = '100%';

    disposeCurrentRemote();
    hostNode.appendChild(mountNode);

    setRemoteLoading(true);
    setRemoteError(null);
    setEmbeddedUrl(null);

    const shellContext = {
      appId: appConfig.id,
      mode: 'hosted' as const,
      basePath: appConfig.basePath,
      token: getAccessToken(),
      user: getUser(),
      locale: localStorage.getItem('jonex_locale') || localStorage.getItem('locale') || 'en',
      theme: {},
      navigate: (to: string) => {
        const target = to.startsWith('/') ? to : `/${to}`;
        navigate(`${appConfig.basePath}${target}`);
      },
      logout: () => logout(),
      getToken: () => getAccessToken(),
      getCurrentUser: () => getUser(),
      emitEvent: () => {},
      onEvent: () => () => {},
      reportError: (error: unknown) => console.error('[shell] Sub-app error:', error),
      reportMetric: () => {},
    };

    loadRemoteApp<MountFn>({
      entry: appConfig.entry,
      scope: appConfig.scope,
      module: appConfig.module || './Mount',
    })
      .then((mountFn) => {
        if (cancelled) return;
        if (typeof mountFn !== 'function') {
          throw new Error(`Remote module is not a function (got ${typeof mountFn})`);
        }

        const unmount = mountFn(mountNode, shellContext);
        const lifecycle: RemoteLifecycle = {
          appId: appConfig.id,
          mountNode,
          unmount: typeof unmount === 'function' ? unmount : () => {},
          disposed: false,
        };
        localLifecycle = lifecycle;
        lifecycleRef.current = lifecycle;

        if (cancelled) {
          disposeLifecycle(lifecycle);
          return;
        }

        setRemoteLoading(false);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        safeRemoveMountNode(mountNode);
        if (embeddedFallbackUrl) {
          setEmbeddedUrl(embeddedFallbackUrl);
          setRemoteError(null);
          setRemoteLoading(false);
          return;
        }
        setRemoteError(err.message);
        setRemoteLoading(false);
      });

    return () => {
      cancelled = true;
      if (localLifecycle) {
        disposeLifecycle(localLifecycle);
      } else {
        safeRemoveMountNode(mountNode);
      }
    };
  }, [
    appConfig,
    containerEl,
    disposeCurrentRemote,
    disposeLifecycle,
    embeddedFallbackUrl,
    hasAppAccess,
    navigate,
    preferStandalone,
  ]);

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 120 }}>
        <Spin size="large" />
      </div>
    );
  }

  if (error) {
    return <Alert type="error" message={t('shell.manifestLoadFailed')} description={error} showIcon />;
  }

  if (!appConfig) {
    return <Result status="404" title={t('shell.appNotFound')} subTitle={t('shell.appNotFoundDetail', { appId })} />;
  }

  if (!appConfig.enabled) {
    return (
      <Result
        status="warning"
        title={t('shell.appDisabled')}
        subTitle={t('shell.appDisabledDetail', { name: appConfig.name })}
      />
    );
  }

  if (!hasAppAccess) {
    return (
      <Result status="403" title={t('shell.noAccess')} subTitle={t('shell.noAccessDetail', { name: appConfig.name })} />
    );
  }

  if (remoteError) {
    return (
      <Result
        status="warning"
        title={t('shell.loadFailed')}
        subTitle={t('shell.loadFailedDetail', { name: appConfig.name, error: remoteError })}
        extra={[
          <Button key="retry" type="primary" onClick={() => window.location.reload()}>
            {t('common.retry')}
          </Button>,
          appConfig.standaloneUrl ? (
            <Button key="standalone" onClick={() => window.open(appConfig.standaloneUrl, '_self')}>
              {t('shell.openStandalone')}
            </Button>
          ) : null,
        ].filter(Boolean)}
      />
    );
  }

  return (
    <div style={{ minHeight: '100%', position: 'relative' }}>
      {embeddedUrl ? (
        <iframe
          title={`${appConfig.name}-standalone`}
          src={embeddedUrl}
          style={{
            width: '100%',
            minHeight: 'calc(100vh - 132px)',
            border: 'none',
          }}
        />
      ) : (
        <div ref={setContainerEl} style={{ minHeight: '100%' }} />
      )}
      {remoteLoading && (
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            justifyContent: 'center',
            alignItems: 'center',
            background: 'rgba(245, 247, 250, 0.72)',
          }}
        >
          <Spin size="large" tip={t('shell.loadingApp')} />
        </div>
      )}
    </div>
  );
}
