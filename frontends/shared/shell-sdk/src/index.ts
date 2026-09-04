export type {
  ShellUser,
  ShellContext,
  ShellNavigateOptions,
  AppManifestEntry,
  AppManifestEntryV1,
  AppManifestEntryV2,
  AppManifestV1,
  AppManifestV2,
  AppManifest,
} from './types';

export { isManifestV2 } from './types';

export { createStandaloneShellContext } from './createStandaloneShellContext';
export { EMBED_QUERY_PARAM, isEmbedded } from './embed';
export * from './spaceContext';
export * from './authStorage';
export { isPlatformAdmin } from './platformAdmin';
export * from './authRedirect';
export { bootstrapStandaloneAuth } from './authBootstrap';
export * from './sessionExpired';
export type { AuthBootstrapOptions, AuthBootstrapResult } from './authBootstrap';

const SHELL_CONTEXT_KEY = '__SHELL_CONTEXT__';

declare global {
  interface Window {
    [SHELL_CONTEXT_KEY]?: import('./types').ShellContext;
  }
}

export function getShellContext(): import('./types').ShellContext | undefined {
  return window[SHELL_CONTEXT_KEY];
}

export function isHosted(): boolean {
  return !!window[SHELL_CONTEXT_KEY] && window[SHELL_CONTEXT_KEY]!.mode === 'hosted';
}
