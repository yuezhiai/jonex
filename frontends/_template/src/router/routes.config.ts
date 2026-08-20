import { redirect } from 'react-router-dom';
import { isEmbedded } from '@jonex/shell-sdk';
import loadableComponent from '@/utils/loadable';

const BasicLayout = loadableComponent(() => import('@/components/BasicLayout'));
const HostedLayout = loadableComponent(() => import('@/components/HostedLayout'));
const Home = loadableComponent(() => import('@/pages/Home'));
const NotFound = loadableComponent(() => import('@/pages/NotFound'));

export function getRoutes(mode: 'standalone' | 'hosted' = 'standalone') {
  const Layout = mode === 'hosted' || isEmbedded() ? HostedLayout : BasicLayout;

  return [
    {
      path: '/',
      loader: () => redirect('/home'),
    },
    {
      path: '',
      element: Layout,
      children: [
        {
          path: 'home',
          element: Home,
          title: 'navigation.home',
          menu: { icon: 'HomeOutlined', order: 1 },
        },
      ],
    },
    {
      path: '*',
      element: NotFound,
      title: 'error.404',
    },
  ];
}

export default getRoutes('standalone');
