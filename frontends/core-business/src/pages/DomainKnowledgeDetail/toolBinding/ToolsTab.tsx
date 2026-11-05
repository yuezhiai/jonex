// 知识库「工具」页签：本库工具（KB 级工具目录）+ 工具绑定（本体 Skill / Action → 工具）。
import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Tabs } from 'antd';
import KbToolsPanel from './KbToolsPanel';
import ToolBindingTab from './ToolBindingTab';

interface Props {
  kbId: string;
  canWrite?: boolean;
}

export default function ToolsTab({ kbId, canWrite = false }: Props) {
  const { t } = useTranslation();
  const [active, setActive] = useState('tools');
  return (
    <Tabs
      activeKey={active}
      onChange={setActive}
      // 切换页签时重新挂载：在「本库工具」里新建的工具，切到「工具绑定」打开表单时就能选到
      destroyOnHidden
      items={[
        { key: 'tools', label: t('kbTool.tabTools'), children: <KbToolsPanel kbId={kbId} canWrite={canWrite} /> },
        { key: 'bindings', label: t('kbTool.tabBindings'), children: <ToolBindingTab kbId={kbId} canWrite={canWrite} /> },
      ]}
    />
  );
}
