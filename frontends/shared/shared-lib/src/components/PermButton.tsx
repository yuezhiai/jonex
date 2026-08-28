import React from 'react';
import { Button, Tooltip } from 'antd';
import type { ButtonProps } from 'antd';
import { useTranslation } from 'react-i18next';

export interface PermButtonProps extends ButtonProps {
  /** 当前用户拥有的全部权限码（shell 下发的 permissions 数组） */
  permissions: string[];
  /** 按钮所需权限：单个权限码，或多个任选其一即放行 */
  requiredPerm: string | string[];
  /** 无权限时是否用 Tooltip 提示原因（默认 true） */
  noPermissionTip?: boolean;
  /** 自定义无权限提示文案（默认取 i18n common.noPermission） */
  noPermissionText?: string;
}

/**
 * 权限按钮：在 antd Button 的全部参数基础上增加权限判断。
 *
 * - `permissions` 传入用户全部权限，`requiredPerm` 为当前按钮所需权限；
 *   两者任一（requiredPerm 数组任一项）命中即放行。
 * - 无权限时按钮强制禁用，外层包 `span` + Tooltip 保证禁用态下提示「无权限」仍可触发。
 */
const PermButton: React.FC<PermButtonProps> = ({
  permissions,
  requiredPerm,
  noPermissionTip = true,
  noPermissionText,
  disabled,
  ...rest
}) => {
  const { t } = useTranslation();
  const required = Array.isArray(requiredPerm) ? requiredPerm : [requiredPerm];
  const granted = required.some((p) => permissions.includes(p));

  if (granted) {
    return <Button {...rest} disabled={disabled} />;
  }

  const button = <Button {...rest} disabled />;
  if (!noPermissionTip) return button;
  return (
    <Tooltip title={noPermissionText ?? t('common.noPermission')}>
      <span style={{ display: 'inline-block' }}>{button}</span>
    </Tooltip>
  );
};

export default PermButton;
