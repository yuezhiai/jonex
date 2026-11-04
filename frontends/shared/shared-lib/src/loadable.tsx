import React from 'react';
import loadable from '@loadable/component';
import { Spin } from 'antd';

const retryImport = (fn: () => Promise<unknown>, retries = 2, interval = 1000): Promise<unknown> =>
  new Promise((resolve, reject) => {
    fn()
      .then(resolve)
      .catch((err) => {
        if (retries <= 0) {
          reject(err);
          return;
        }
        setTimeout(() => {
          retryImport(fn, retries - 1, interval).then(resolve, reject);
        }, interval);
      });
  });

const SpinnerFallback = () => (
  <Spin
    style={{
      position: 'absolute',
      left: '50%',
      top: '50%',
      transform: 'translate(-50%,-50%)',
    }}
    size="large"
  />
);

interface LoadableOptions {
  retries?: number;
  interval?: number;
  fallback?: React.ReactNode;
}

const loadableComponent = (loader: () => Promise<unknown>, options: LoadableOptions = {}) =>
  (loadable as any)(() => retryImport(loader, options.retries ?? 2, options.interval ?? 1000), {
    fallback: options.fallback ?? <SpinnerFallback />,
  });

declare global {
  interface Window {
    __CHUNK_LOAD_HANDLER_INSTALLED?: boolean;
  }
}

// 自动刷新是"发版后拿到新资源"的兜底，但同一个失败如果刷新也治不好（典型：Vite dev 的
// 504 Outdated Optimize Dep，依赖预构建一直没有提交成功），就会变成无限重载风暴。
// 这里做两层限流：同一批错误只排一次刷新；两次自动刷新之间至少间隔 60s。
const RELOAD_GUARD_KEY = 'jonex:chunk-load-reload-at';
const RELOAD_GUARD_WINDOW_MS = 60_000;
let reloadScheduled = false;

function shouldReloadForLatestAssets(): boolean {
  if (reloadScheduled) {
    return false;
  }
  try {
    const last = Number(window.sessionStorage.getItem(RELOAD_GUARD_KEY) || 0);
    if (last && Date.now() - last < RELOAD_GUARD_WINDOW_MS) {
      return false;
    }
  } catch {
    // sessionStorage 不可用（隐私模式等）时只依赖内存标记
  }
  return true;
}

if (typeof window !== 'undefined' && !window.__CHUNK_LOAD_HANDLER_INSTALLED) {
  window.__CHUNK_LOAD_HANDLER_INSTALLED = true;

  window.addEventListener('error', (event) => {
    try {
      const message = event?.message || '';
      const isChunkLoadFailed =
        message.includes('Loading chunk') ||
        message.includes('Loading module') ||
        message.includes('Failed to fetch dynamically imported module');

      if (isChunkLoadFailed) {
        if (!shouldReloadForLatestAssets()) {
          console.error(
            '[loadable] chunk load failed again right after an auto reload, stop reloading. ' +
              '通常是 dev server 的依赖预构建缓存异常（504 Outdated Optimize Dep）或构建产物不一致：' +
              '请重启 dev server（必要时删除 node_modules/.vite 下对应 app 的缓存）后重试。',
            message,
          );
          return;
        }
        reloadScheduled = true;
        try {
          window.sessionStorage.setItem(RELOAD_GUARD_KEY, String(Date.now()));
        } catch {
          // ignore
        }
        console.warn('[loadable] chunk load failed, reloading page for latest assets', message);
        setTimeout(() => {
          window.location.reload();
        }, 500);
      }
    } catch {
      // ignore
    }
  });
}

export default loadableComponent;
