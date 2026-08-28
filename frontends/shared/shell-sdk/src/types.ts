export interface ShellUser {
  id: string;
  username: string;
  displayName?: string;
  realName?: string;
  email?: string;
  tenantId?: string;
  tenantName?: string;
  roles: string[];
  isPlatformAdmin: boolean;
  isTenantAdmin: boolean;
  permissions: string[];
  impersonated?: boolean;
  originalTenantId?: string;
}

export interface ShellNavigateOptions {
  replace?: boolean;
  state?: unknown;
}

export interface ShellContext {
  appId: string;
  basePath: string;
  mode: 'hosted' | 'standalone';
  token: string | null;
  user: ShellUser | null;
  locale: string;
  theme: Record<string, unknown>;
  navigate: (to: string, options?: ShellNavigateOptions) => void;
  logout: () => void;
  getToken: () => string | null;
  getCurrentUser: () => ShellUser | null;
  emitEvent: (name: string, payload?: unknown) => void;
  onEvent: (name: string, handler: (payload?: unknown) => void) => () => void;
  reportError: (error: unknown, extra?: Record<string, unknown>) => void;
  reportMetric: (name: string, value: number, tags?: Record<string, string>) => void;
}

export interface AppManifestEntryV1 {
  id: string;
  name: string;
  description?: string;
  enabled: boolean;
  basePath: string;
  standaloneUrl: string;
  entry: string;
  scope: string;
  module: string;
  apiPrefix: string;
  roles: string[];
  order: number;
  category?: string;
}

export interface AppManifestEntryV2 {
  id: string;
  name: string;
  description?: string;
  enabled: boolean;
  order: number;
  category?: string;
  icon?: string;
  routes: {
    hostedBase: string;
    standaloneBase: string;
  };
  remote: {
    type: 'module-federation';
    scope: string;
    module: string;
    entry: string;
  };
  standaloneUrl: string;
  basePath: string;
  entry: string;
  scope: string;
  module: string;
  apiPrefix?: string;
  roles: string[];
  health?: {
    url: string;
    timeoutMs: number;
  };
  permissions?: {
    visibleRoles: string[];
    requiredFeatures?: string[];
  };
  fallback?: {
    mode: 'standalone' | 'error';
    url?: string;
  };
  version?: {
    expected: string;
    source: string;
  };
}

export type AppManifestEntry = AppManifestEntryV1 | AppManifestEntryV2;

export interface AppManifestV1 {
  apps: AppManifestEntryV1[];
}

export interface AppManifestV2 {
  schemaVersion: 2;
  updatedAt?: string;
  apps: AppManifestEntryV2[];
}

export type AppManifest = AppManifestV1 | AppManifestV2;

export function isManifestV2(manifest: AppManifest): manifest is AppManifestV2 {
  return 'schemaVersion' in manifest && manifest.schemaVersion === 2;
}
