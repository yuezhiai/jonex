import type { AppManifest, AppManifestEntry } from '@jonex/shell-sdk';
import { isManifestV2 } from '@jonex/shell-sdk';
import { apiClient } from './auth';

const PLATFORM_MANIFEST_URL = '/platform/frontend/apps';
const FALLBACK_MANIFEST_URL = '/app-manifest.json';

let cachedManifest: AppManifest | null = null;
let cacheTime = 0;
const CACHE_TTL = 5 * 60 * 1000;

export async function fetchAppManifest(): Promise<AppManifest> {
  const now = Date.now();
  if (cachedManifest && now - cacheTime < CACHE_TTL) {
    return cachedManifest;
  }

  let data: AppManifest | null = null;

  try {
    // 平台清单走后端统一请求入口（apiClient.get<T> 解包返回 manifest 本体）
    data = await apiClient.get<AppManifest>(PLATFORM_MANIFEST_URL);
  } catch (error) {
    console.warn('[shell] platform manifest unavailable, using local fallback', error);
    // 本地静态 fallback 不走 apiClient（baseURL=/api/v1 会错误拼前缀），用 fetch 直读
    data = await loadManifestFallback();
  }

  if (!isManifestV2(data)) {
    throw new Error('Unsupported manifest schema version');
  }

  cachedManifest = data;
  cacheTime = now;
  return cachedManifest;
}

async function loadManifestFallback(): Promise<AppManifest> {
  const res = await fetch(FALLBACK_MANIFEST_URL);
  if (!res.ok) {
    throw new Error(`Failed to load fallback manifest (HTTP ${res.status})`);
  }
  return res.json();
}

export function getEnabledApps(manifest: AppManifest, userRoles: string[]): AppManifestEntry[] {
  if (!manifest?.apps) return [];

  return manifest.apps
    .filter((app) => {
      if (!app.enabled) return false;
      const roles = app.roles ?? (app as any).permissions?.visibleRoles;
      if (!roles || roles.length === 0) return true;
      return roles.some((r: string) => userRoles.includes(r));
    })
    .sort((a, b) => (a.order ?? 999) - (b.order ?? 999));
}
