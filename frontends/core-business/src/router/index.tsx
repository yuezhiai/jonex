import React, { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import {
  createBrowserRouter,
  createMemoryRouter,
  createRoutesFromElements,
  RouterProvider,
  Route,
  useRouteError,
  isRouteErrorResponse,
  redirect,
  type LoaderFunctionArgs,
} from 'react-router-dom';
import { Result } from 'antd';
import i18next from 'i18next';
import { getRoutes } from './routes.config';
import { getMenuFromRoutes } from '@/router/menu';
import AppLayout from '@/components/AppLayout';
import { readAccessToken, readCachedUser, clearAuthStorage, redirectToLogin } from '@jonex/shell-sdk';

interface RouteConfigItem {
  path?: string;
  title?: string;
  children?: RouteConfigItem[];
  element?: React.ComponentType;
  index?: boolean;
}

interface MenuItem {
  path?: string;
  permissionCode?: string;
}

interface AuthLoaderOptions {
  basename: string;
  mode: string;
  shellContext?: {
    user?: { permissions?: string[] };
  } | null;
}

const whiteList = ['/home', '/404', '/403', '/error'];
const VITE_LOGIN = (import.meta as any).env?.VITE_LOGIN || '/login';
const VITE_APP_ID = (import.meta as any).env?.VITE_APP_ID || 'core-business';
const STANDALONE_BASENAME = (import.meta as any).env?.VITE_STANDALONE_BASE || '/';

const normalizeBasename = (basename: string) => {
  if (!basename || basename === '/') return '';
  return basename.endsWith('/') ? basename.slice(0, -1) : basename;
};

const getHostedInitialEntry = (basename: string) => {
  if (typeof window === 'undefined') return '/home';

  const normalizedBasename = normalizeBasename(basename);
  const { pathname, search, hash } = window.location;

  // RouteSync 已将子应用内部路由同步到浏览器 URL，优先从中提取
  let path = pathname;
  if (normalizedBasename) {
    if (path === normalizedBasename || path === `${normalizedBasename}/`) {
      path = '/home';
    } else if (path.startsWith(`${normalizedBasename}/`)) {
      path = path.slice(normalizedBasename.length) || '/home';
    } else {
      // basename 不匹配时，尝试按已知子应用前缀截取内部路由
      const knownPrefixes = [
        '/core-business',
        '/apps/core-business',
        '/expert-call',
        '/ecosystem-management',
        '/platform-management',
      ];
      const matched = knownPrefixes.find((p) => path.startsWith(p + '/') || path === p);
      if (matched) {
        path = path.slice(matched.length) || '/home';
      } else {
        path = '/home';
      }
    }
  }

  if (!path.startsWith('/')) path = `/${path}`;
  return `${path}${search}${hash}`;
};

const createAuthLoader =
  ({ basename, mode, shellContext }: AuthLoaderOptions) =>
  ({ request }: LoaderFunctionArgs) => {
    const url = new URL(request.url);
    let path = url.pathname;
    if (!path.startsWith('/')) path = '/' + path;

    if (path.startsWith(basename)) {
      path = path.slice(basename.length) || '/';
    }

    if (path === '/' || path === '/home') return redirect('/knowledge-search');

    if (mode === 'hosted') {
      const user = shellContext?.user;

      if (user) {
        const userPerms: string[] = user.permissions || [];
        const menuItem = getMenuFromRoutes(getRoutes(), (s: string) => s).find((item) => item.path === path);
        if (menuItem?.permissionCode && !userPerms.includes(menuItem.permissionCode)) {
          return redirect('/error?page=403');
        }
      }
      return null;
    }

    if (!readAccessToken()) {
      if (whiteList.includes(path)) return null;
      if (typeof window !== 'undefined') {
        clearAuthStorage({ keepLocale: true });
        redirectToLogin({ loginUrl: VITE_LOGIN || '/login', appId: VITE_APP_ID });
      }
      return null;
    }

    const userInfo = readCachedUser<Record<string, any>>() || {};
    const userPerms: string[] = Array.isArray(userInfo?.permissions) ? userInfo.permissions : [];
    const menuItem = getMenuFromRoutes(getRoutes(), (s: string) => s).find((item) => item.path === path);
    if (menuItem?.permissionCode && !userPerms.includes(menuItem.permissionCode)) {
      return redirect('/error?page=403');
    }

    return null;
  };

const routesRender = (routesConfig: RouteConfigItem[] = [], authLoader: ReturnType<typeof createAuthLoader>) => {
  return routesConfig.map(({ path = '', title = '', children = [], element: Element = null, index = false }) => {
    const RouteComponent = Element;
    const routeProps: Record<string, unknown> = {
      path: path || undefined,
      element: RouteComponent ? <RouteComponent /> : undefined,
      errorElement: <ErrorBoundary />,
      loader: authLoader,
      handle: { title },
    };
    if (index) routeProps.index = true;
    return (
      <Route key={path || 'index'} {...(routeProps as any)}>
        {children?.length > 0 && routesRender(children, authLoader)}
      </Route>
    );
  });
};

const ErrorBoundary = () => {
  const error = useRouteError();
  if (isRouteErrorResponse(error)) {
    if (error.status === 404) {
      return <Result status="warning" title="404" subTitle={i18next.t('error.404')} />;
    }
    if (error.status === 403) {
      return <Result status="warning" title="403" subTitle={i18next.t('error.403')} />;
    }
  }
  return <Result status="warning" title={i18next.t('error.network')} />;
};

interface AppRouteProps {
  basename?: string;
  mode?: 'standalone' | 'hosted';
  shellContext?: AuthLoaderOptions['shellContext'];
}

const AppRoute = ({ basename, mode = 'standalone', shellContext }: AppRouteProps) => {
  const { t } = useTranslation();
  const actualBasename = basename || (mode === 'hosted' ? '/apps/core-business' : STANDALONE_BASENAME);
  const authLoader = createAuthLoader({ basename: actualBasename, mode, shellContext });

  const router = useMemo(() => {
    const routeData = getRoutes(mode, t);
    const routes = createRoutesFromElements(
      <Route path="/" element={<AppLayout />}>
        {routesRender(routeData as RouteConfigItem[], authLoader)}
      </Route>,
    );

    if (mode === 'hosted') {
      return createMemoryRouter(routes, {
        initialEntries: [getHostedInitialEntry(actualBasename)],
      });
    }

    return createBrowserRouter(routes, { basename: actualBasename });
  }, [actualBasename, mode, t]);

  return <RouterProvider router={router} />;
};

export default AppRoute;
