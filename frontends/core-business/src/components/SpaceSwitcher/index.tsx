import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Dropdown, Spin } from 'antd';
import { DownOutlined, SettingOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useStore } from '@/store';

/** 与 shell/src/components/SpaceSwitcher 一致的内联样式（视觉统一） */
const S: Record<string, React.CSSProperties> = {
  switcher: {
    display: 'flex',
    alignItems: 'center',
    paddingRight: 12,
    margin: 8,
    background: '#f1f5f9',
    borderRadius: 8,
    gap: 6,
  },
  trigger: {
    height: 42,
    flex: 1,
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    cursor: 'pointer',
    minWidth: 0,
    padding: 12,
  },
  name: {
    flex: 1,
    fontSize: 13,
    fontWeight: 500,
    color: '#1e293b',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap' as const,
  },
  arrow: { fontSize: 10, color: '#94a3b8', flexShrink: 0 },
  gear: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 24,
    height: 24,
    borderRadius: 4,
    cursor: 'pointer',
    color: '#64748b',
    fontSize: 13,
    flexShrink: 0,
  },
  collapsed: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '8px 0',
    margin: '0 8px 8px',
    cursor: 'pointer',
  },
  collapsedChar: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 32,
    height: 32,
    borderRadius: 8,
    background: '#eff6ff',
    color: '#3b82f6',
    fontSize: 16,
    fontWeight: 600,
  },
  activeItem: { color: '#3b82f6', fontWeight: 500 },
  addItem: { color: '#3b82f6' },
};

interface SpaceSwitcherProps {
  collapsed?: boolean;
}

const SpaceSwitcher = ({ collapsed }: SpaceSwitcherProps) => {
  const { global } = useStore();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [dropdownOpen, setDropdownOpen] = useState(false);

  const currentName = global.currentSpace?.name || t('domainSpace.selectSpace');

  const handleSelect = (spaceId: string) => {
    global.setCurrentSpaceId(spaceId, { persist: true, broadcast: true });
    setDropdownOpen(false);
  };

  const handleAddSpace = () => {
    setDropdownOpen(false);
    // 跳转到独立新建页面，创建成功后跳转设置页
    navigate('/domain-space/new');
  };

  const handleGear = () => {
    setDropdownOpen(false);
    navigate(global.currentSpaceId ? `/domain-space/${global.currentSpaceId}/settings` : '/domain-space');
  };

  // 折叠态只显示首个字母
  if (collapsed) {
    return (
      <div style={S.collapsed} title={currentName}>
        <span style={S.collapsedChar}>{currentName.charAt(0).toUpperCase()}</span>
      </div>
    );
  }

  const items = [
    ...(global.spacesLoaded
      ? global.spaces.map((s) => ({
          key: s.id,
          label: <span style={s.id === global.currentSpaceId ? S.activeItem : undefined}>{s.name}</span>,
          onClick: () => handleSelect(s.id),
        }))
      : []),
    { type: 'divider' as const },
    {
      key: 'add-space',
      label: (
        <span style={S.addItem}>
          <PlusOutlined style={{ marginRight: 6 }} />
          {t('domainSpace.addSpace')}
        </span>
      ),
      onClick: handleAddSpace,
    },
    {
      key: 'manage-space',
      label: (
        <span>
          <SettingOutlined style={{ marginRight: 6 }} />
          {t('domainSpace.management')}
        </span>
      ),
      onClick: () => {
        setDropdownOpen(false);
        navigate('/domain-space');
      },
    },
  ];

  return (
    <div style={S.switcher}>
      <Dropdown
        menu={{
          items,
          style: {
            boxShadow: '0 6px 16px 0 rgba(0, 0, 0, 0.08), 0 3px 6px -4px rgba(0, 0, 0, 0.12), 0 9px 28px 8px rgba(0, 0, 0, 0.05)',
          },
        }}
        open={dropdownOpen}
        onOpenChange={setDropdownOpen}
        trigger={['click']}
        placement="bottomLeft"
        styles={{ root: { minWidth: 200 } }}
      >
        <div style={S.trigger} onClick={() => setDropdownOpen(!dropdownOpen)}>
          {global.spacesLoading && !global.spacesLoaded ? (
            <Spin size="small" />
          ) : (
            <>
              <span style={S.name}>{currentName}</span>
              <DownOutlined style={S.arrow} />
            </>
          )}
        </div>
      </Dropdown>
      <span style={S.gear} onClick={handleGear}>
        <SettingOutlined />
      </span>
    </div>
  );
};

export default SpaceSwitcher;
