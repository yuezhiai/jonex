import { useCallback, useMemo } from 'react';
import { getShellContext } from '@jonex/shell-sdk';

export interface UsePermissionResult {
  /** 当前用户拥有的全部权限码（shell 下发） */
  permissions: string[];
  /** 判断是否拥有某权限：单个权限码，或数组任选其一 */
  hasPerm: (code: string | string[]) => boolean;
}

/**
 * 权限 hook：从 shell 下发的 ShellContext（window.__SHELL_CONTEXT__）读取用户权限。
 *
 * - hosted（shell 托管）与 standalone 两种模式入口都会挂载 ShellContext，故两种形态均可读；
 * - 子应用无需再各自从私有 store 提取 permissions，权限来源统一收敛到 shell-sdk。
 */
export function usePermission(): UsePermissionResult {
  const user = getShellContext()?.getCurrentUser?.() ?? null;
  const permissions = useMemo(
    () => (Array.isArray(user?.permissions) ? (user.permissions as string[]) : []),
    [user?.permissions],
  );
  const hasPerm = useCallback(
    (code: string | string[]) => {
      const codes = Array.isArray(code) ? code : [code];
      return codes.some((c) => permissions.includes(c));
    },
    [permissions],
  );
  return { permissions, hasPerm };
}

export default usePermission;
