import React, { useState, useEffect, useRef } from 'react';
import { Button, Table, Select, Input, message } from 'antd';
import { TeamOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { DomainKnowledgePermissionMember } from '@/types/domainKnowledge';
import { listUsers, type PlatformUser } from '@/api/user';

interface PermissionTabProps {
  members: DomainKnowledgePermissionMember[];
  loading: boolean;
  saving: boolean;
  memberName: (member: DomainKnowledgePermissionMember) => string;
  onRoleChange: (userId: string, role: 'viewer' | 'editor') => void;
  onRemove: (userId: string) => void;
  onSave: () => void;
  /** 添加成员（用户搜索选中后回调；新成员默认 viewer） */
  onAdd: (user: PlatformUser) => void;
  /** 只读态：该 KB 的权限管理权（空间 owner/manager 或租户管理员） */
  canManage?: boolean;
}

export default function PermissionTab({
  members,
  loading,
  saving,
  memberName,
  onRoleChange,
  onRemove,
  onSave,
  onAdd,
  canManage = false,
}: PermissionTabProps) {
  const { t } = useTranslation();
  const [userSelectOpen, setUserSelectOpen] = useState(false);
  const [userSearchText, setUserSearchText] = useState('');
  const [availableUsers, setAvailableUsers] = useState<PlatformUser[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const userSelectRef = useRef<HTMLDivElement>(null);

  // 点击外部关闭用户选择器
  useEffect(() => {
    if (!userSelectOpen) return;
    const handler = (e: MouseEvent) => {
      if (userSelectRef.current && !userSelectRef.current.contains(e.target as Node)) {
        setUserSelectOpen(false);
        setUserSearchText('');
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [userSelectOpen]);

  const loadAvailableUsers = async () => {
    setUsersLoading(true);
    try {
      const result = await listUsers(1, 500);
      setAvailableUsers(result.items);
    } catch {
      // 无 user:read 时降级为空（与空间权限 modal 同口径）
      setAvailableUsers([]);
    } finally {
      setUsersLoading(false);
    }
  };

  const addedUserIds = new Set(members.map((m) => m.userId));
  const filteredAvailableUsers = availableUsers.filter((u) => {
    if (addedUserIds.has(String(u.id))) return false;
    if (!userSearchText) return true;
    const q = userSearchText.toLowerCase();
    return (
      (u.display_name || '').toLowerCase().includes(q) ||
      u.username.toLowerCase().includes(q) ||
      (u.email || '').toLowerCase().includes(q)
    );
  });

  return (
    <div className="config-section yx-kb-section-card">
      <div className="yx-kb-flex-header">
        <h3 className="yx-kb-section-title">
          <TeamOutlined className="yx-kb-icon-blue" /> {t('domainKnowledge.permissionSettings')}
        </h3>
        {/* 添加成员（可管理权限者可见）：搜索本租户用户，选中即加入列表（默认 viewer） */}
        <div ref={userSelectRef} style={{ position: 'relative' }}>
          <Button
            className="yx-kb-section-add-btn"
            icon={<PlusOutlined />}
            disabled={!canManage}
            title={canManage ? undefined : t('domainSpace.noManagePermission')}
            onClick={() => {
              const willOpen = !userSelectOpen;
              setUserSelectOpen(willOpen);
              setUserSearchText('');
              if (willOpen && availableUsers.length === 0) loadAvailableUsers();
            }}
          >
            {t('domainKnowledge.addMember')}
          </Button>
          {userSelectOpen && (
            <div
              style={{
                position: 'absolute',
                top: 38,
                right: 0,
                zIndex: 10,
                width: 300,
                background: '#fff',
                borderRadius: 8,
                boxShadow: '0 4px 20px rgba(0,0,0,.12)',
                border: '1px solid #e2e8f0',
                overflow: 'hidden',
              }}
            >
              <div style={{ padding: '8px 12px', borderBottom: '1px solid #e2e8f0' }}>
                <Input
                  size="small"
                  placeholder={t('domainSpace.searchUserPlaceholder')}
                  prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
                  value={userSearchText}
                  onChange={(e) => setUserSearchText(e.target.value)}
                  allowClear
                />
              </div>
              <div style={{ maxHeight: 220, overflowY: 'auto' }}>
                {usersLoading ? (
                  <div style={{ textAlign: 'center', padding: 20, color: '#94a3b8', fontSize: 13 }}>
                    {t('common.loading')}
                  </div>
                ) : filteredAvailableUsers.length === 0 ? (
                  <div style={{ textAlign: 'center', padding: 20, color: '#94a3b8', fontSize: 13 }}>
                    {userSearchText ? t('domainSpace.noMatchUser') : t('domainSpace.noAvailableUser')}
                  </div>
                ) : (
                  filteredAvailableUsers.slice(0, 30).map((user) => (
                    <div
                      key={user.id}
                      style={{ cursor: 'pointer', padding: '8px 12px', borderBottom: '1px solid #f1f5f9' }}
                      onClick={() => {
                        onAdd(user);
                        setUserSearchText('');
                        setUserSelectOpen(false);
                      }}
                    >
                      <div style={{ fontWeight: 500, color: '#0b2b5c' }}>
                        {user.display_name || user.username}
                      </div>
                      <div style={{ fontSize: 12, color: '#94a3b8' }}>{user.email || user.username}</div>
                    </div>
                  ))
                )}
              </div>
            </div>
          )}
        </div>
      </div>
      <p className="yx-kb-section-desc">{t('domainKnowledge.permissionDesc')}</p>
      <Table
        columns={[
          {
            title: t('domainKnowledge.user'),
            dataIndex: 'userId',
            key: 'user',
            width: 200,
            render: (_: unknown, record: DomainKnowledgePermissionMember) => (
              <div style={{ fontWeight: 500, color: '#0b2b5c' }}>{memberName(record)}</div>
            ),
          },
          {
            title: t('domainKnowledge.role'),
            dataIndex: 'role',
            key: 'role',
            width: 160,
            render: (role: string, record: DomainKnowledgePermissionMember) => (
              <Select
                value={role}
                disabled={!canManage}
                onChange={(value) => onRoleChange(record.userId, value as 'viewer' | 'editor')}
                style={{ width: 120 }}
                options={[
                  { value: 'editor', label: t('kbPermission.editor') },
                  { value: 'viewer', label: t('kbPermission.viewer') },
                ]}
              />
            ),
          },
          {
            title: t('common.actions'),
            key: 'actions',
            width: 100,
            render: (_: unknown, record: DomainKnowledgePermissionMember) =>
              canManage ? (
                <a className="yx-table-action" style={{ cursor: 'pointer' }} onClick={() => onRemove(record.userId)}>
                  {t('domainKnowledge.remove')}
                </a>
              ) : (
                <span style={{ color: '#cbd5e1' }}>—</span>
              ),
          },
        ]}
        dataSource={members}
        rowKey="userId"
        pagination={false}
        size="middle"
        loading={loading}
        locale={{ emptyText: t('domainKnowledge.noMembers') }}
      />
      <div className="yx-kb-save-bar">
        <Button
          type="primary"
          loading={saving}
          disabled={!canManage}
          title={canManage ? undefined : t('domainSpace.noManagePermission')}
          onClick={onSave}
        >
          {t('domainKnowledge.savePermission')}
        </Button>
      </div>
    </div>
  );
}
