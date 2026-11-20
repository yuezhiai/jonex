/**
 * 编排节点的图标底座：小圆角方块 + 按类型着色。
 *
 * 抽成组件是因为它在两处出现 —— 左侧画布上的节点卡片、以及「添加业务节点」浮层里的
 * 能力选项；两处必须长得一样，否则用户会觉得「菜单里选的东西和画布上加出来的不是一个东西」。
 *
 * `kind` 收的是步骤码（`start` / `end` 是端点，中间节点是后端清单里的 `step_code`）。
 * 未知步骤码回落到通用图标：清单加了新步骤而前端还没配图标时，不能整块空白。
 */
import type { ReactNode } from 'react';
import {
  CheckCircleOutlined,
  DeploymentUnitOutlined,
  FileTextOutlined,
  PlayCircleOutlined,
} from '@ant-design/icons';
import { theme } from 'antd';

const ICONS: Record<string, ReactNode> = {
  start: <PlayCircleOutlined />,
  parse: <FileTextOutlined />,
  compile: <DeploymentUnitOutlined />,
  end: <CheckCircleOutlined />,
};

export default function WorkflowNodeIcon({ kind, size = 32 }: { kind: string; size?: number }) {
  const { token } = theme.useToken();
  // 开始/结束是端点，用绿色和灰色区分；中间能力节点走主色，与菜单里的图标一致
  const background =
    kind === 'start' ? token.colorSuccessBg : kind === 'end' ? token.colorFillSecondary : token.colorPrimaryBg;
  const color =
    kind === 'start' ? token.colorSuccess : kind === 'end' ? token.colorTextTertiary : token.colorPrimary;
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: size,
        height: size,
        flex: `0 0 ${size}px`,
        borderRadius: 8,
        background,
        color,
        fontSize: Math.round(size / 2),
      }}
    >
      {ICONS[kind] ?? <DeploymentUnitOutlined />}
    </span>
  );
}
