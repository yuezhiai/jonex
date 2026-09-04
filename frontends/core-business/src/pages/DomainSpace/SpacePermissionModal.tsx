import React, { useState, useCallback, forwardRef, useImperativeHandle, useRef, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Modal, Input, Button, Spin, message } from 'antd';
import { TeamOutlined, SearchOutlined, UserAddOutlined, CloseOutlined } from '@ant-design/icons';
import { getSpacePermissions, updateSpacePermissions, getSpacePermissionCandidates, type SpaceOwnerInfo } from '../../api/domainSpace';
import { candidateToPermMember, type MemberCandidate, type PermMember, type SpaceRole } from '../../types/domainService';
import type { DomainSpace } from '../../types/domainSpace';
import { getShellContext } from '@jonex/shell-sdk';

export interface SpacePermissionModalHandle {
  open: (space: DomainSpace) => void;
}

interface SpacePermissionModalProps {
  onSaved: () => void;
}

const SpacePermissionModal = forwardRef<SpacePermissionModalHandle, SpacePermissionModalProps>(
  function SpacePermissionModal({ onSaved }, ref) {
    const { t } = useTranslation();

    const [open, setOpen] = useState(false);
    const [permSpace, setPermSpace] = useState<DomainSpace | null>(null);
    const [permMembers, setPermMembers] = useState<PermMember[]>([]);
    // 空间创建者（纯展示：owner_id 不参与判定；创建人本身以 space_manager 出现在成员列表里）
    const [permOwner, setPermOwner] = useState<SpaceOwnerInfo | null>(null);
    const [permSearch, setPermSearch] = useState('');
    const [permLoading, setPermLoading] = useState(false);
    const [permSaving, setPermSaving] = useState(false);

    const [userSelectOpen, setUserSelectOpen] = useState(false);
    const [userSearchText, setUserSearchText] = useState('');
    const [availableUsers, setAvailableUsers] = useState<MemberCandidate[]>([]);
    const [usersLoading, setUsersLoading] = useState(false);
    const userSelectRef = useRef<HTMLDivElement>(null);

    useImperativeHandle(
      ref,
      () => ({
        open: async (space: DomainSpace) => {
          setPermSpace(space);
          setPermSearch('');
          setOpen(true);
          setPermLoading(true);
          setUserSelectOpen(false);
          setUserSearchText('');
          try {
            const { permissions: perms, owner } = await getSpacePermissions(space.id);
            setPermOwner(owner);
            // 成员展示名直接来自后端 join 的 display_name（viewer/知识编辑者无 user:read，
            // 前端拉 /users 会 403）；不再拉全量用户拼名字
            const members: PermMember[] = perms.map((p) => {
              const uid = String(p.user_id);
              const name = p.display_name || t('domainSpace.userPrefix', { id: uid.slice(0, 8) });
              return {
                id: uid,
                name,
                department: '',
                avatar: name.charAt(0).toUpperCase(),
                avatarColor: '#94a3b8',
                // [jonex] B2（D2）：取值 space_manager / member；显式映射，勿用三元兜底
                role: (p.role === 'space_manager' ? 'space_manager' : 'member') as SpaceRole,
              };
            });
            setPermMembers(members);
          } catch (err: any) {
            message.error(err?.message || t('common.loadFailed'));
            setPermMembers([]);
          } finally {
            setPermLoading(false);
          }
        },
      }),
      [t],
    );

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

    // 只读态：owner 或 service:write 持有者才可编辑（后端最终校验）
    const canManage = permSpace?.can_manage_permissions ?? false;

    const handleClose = () => {
      setOpen(false);
      setUserSelectOpen(false);
    };

    const handlePermSave = async () => {
      if (!permSpace || !canManage) return;
      setPermSaving(true);
      try {
        await updateSpacePermissions(
          permSpace.id,
          permMembers.map((m) => ({ user_id: m.id, role: m.role })),
        );
        message.success(t('common.saveSuccess'));
        setOpen(false);
        onSaved();
      } catch (err: any) {
        message.error(err instanceof Error ? err.message : t('common.saveFailed'));
      } finally {
        setPermSaving(false);
      }
    };

    const loadAvailableUsers = useCallback(async () => {
      if (!permSpace) return;
      setUsersLoading(true);
      try {
        const result = await getSpacePermissionCandidates(permSpace.id);
        setAvailableUsers(result);
      } catch {
        setAvailableUsers([]);
      } finally {
        setUsersLoading(false);
      }
    }, [permSpace]);

    const addPermMember = (user: MemberCandidate) => {
      setPermMembers((prev) => {
        if (prev.some((m) => m.id === user.user_id)) return prev;
        return [...prev, candidateToPermMember(user, 'member')];
      });
    };

    const removePermMember = (userId: string) => {
      setPermMembers((prev) => prev.filter((m) => m.id !== userId));
    };

    const filteredPermMembers = permMembers.filter((m) => {
      if (!permSearch) return true;
      return m.name.includes(permSearch) || m.department.includes(permSearch);
    });

    const currentUserId = getShellContext()?.getCurrentUser()?.id ?? '';
    const addedUserIds = new Set(permMembers.map((m) => m.id));
    // [jonex] B2（E3）：**撤掉了 owner 的候选排除**。
    // D2 之后创建人就是普通的 space_manager，可被调整甚至移除（由后端
    // 「空间至少保留一名管理者」兜底）。继续排除他会让「转交空间」做不了，
    // 而且后端已不再拦截，前后端口径会不一致。
    // 仍排除当前登录用户自己 —— 那是防「自己添加自己」的既有行为，与 owner 无关。
    const excludedIds = new Set([currentUserId].filter(Boolean).map(String));
    const filteredAvailableUsers = availableUsers.filter((u) => {
      const uid = u.user_id;
      if (addedUserIds.has(uid) || excludedIds.has(uid)) return false;
      if (!userSearchText) return true;
      const q = userSearchText.toLowerCase();
      return (
        (u.display_name || '').toLowerCase().includes(q) ||
        (u.username || '').toLowerCase().includes(q) ||
        (u.email || '').toLowerCase().includes(q)
      );
    });

    return (
      <Modal
        wrapClassName="yx-domain-space-modal"
        title={
          <span>
            <TeamOutlined style={{ color: '#3b82f6', marginRight: 8 }} />
            {t('domainSpace.permissionSettings')}
          </span>
        }
        open={open}
        onCancel={handleClose}
        onOk={handlePermSave}
        confirmLoading={permSaving}
        okText={t('domainSpace.savePermission')}
        cancelText={t('common.cancel')}
        width={600}
        okButtonProps={{ disabled: !canManage }}
      >
        <p style={{ fontSize: 14, color: '#475569', marginBottom: 12 }}>
          {t('domainSpace.setMemberPermission', { name: permSpace?.name || '' })}
        </p>

        {/* 添加成员区域（仅可管理权限者可见） */}
        {canManage && (
        <div ref={userSelectRef} style={{ position: 'relative', marginBottom: 12 }}>
          <Button
            icon={<UserAddOutlined />}
            onClick={() => {
              const willOpen = !userSelectOpen;
              setUserSelectOpen(willOpen);
              setUserSearchText('');
              if (willOpen && availableUsers.length === 0) loadAvailableUsers();
            }}
            style={{ marginBottom: userSelectOpen ? 8 : 0 }}
          >
            {t('domainSpace.addMember')}
          </Button>
          {userSelectOpen && (
            <div
              style={{
                position: 'absolute',
                top: 38,
                left: 0,
                zIndex: 10,
                width: 320,
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
                  <div style={{ textAlign: 'center', padding: 20 }}>
                    <Spin size="small" />
                  </div>
                ) : filteredAvailableUsers.length === 0 ? (
                  <div style={{ textAlign: 'center', padding: 20, color: '#94a3b8', fontSize: 13 }}>
                    {userSearchText ? t('domainSpace.noMatchUser') : t('domainSpace.noAvailableUser')}
                  </div>
                ) : (
                  filteredAvailableUsers.slice(0, 30).map((user) => (
                    <div
                      key={user.user_id}
                      className="yx-perm-user-row"
                      style={{ cursor: 'pointer', padding: '8px 12px' }}
                      onClick={() => {
                        addPermMember(user);
                        setUserSearchText('');
                      }}
                    >
                      <div className="yx-perm-avatar" style={{ background: candidateToPermMember(user).avatarColor }}>
                        {candidateToPermMember(user).avatar}
                      </div>
                      <div className="yx-perm-user-info" style={{ flex: 1 }}>
                        <div className="yx-perm-user-name">{user.display_name || user.username || user.user_id}</div>
                        <div className="yx-perm-user-dept">{user.email || user.username || ''}</div>
                      </div>
                      <span style={{ fontSize: 20, color: '#3b82f6', lineHeight: 1 }}>+</span>
                    </div>
                  ))
                )}
              </div>
            </div>
          )}
        </div>
        )}

        {/* 已有成员搜索 */}
        <Input
          prefix={<SearchOutlined style={{ color: '#94a3b8', fontSize: 14 }} />}
          placeholder={t('domainSpace.searchMemberPlaceholder')}
          value={permSearch}
          onChange={(e) => setPermSearch(e.target.value)}
          style={{ width: '100%', marginBottom: 12 }}
        />

        {/* 成员列表 */}
        <div style={{ maxHeight: 280, overflowY: 'auto' }}>
          {permLoading ? (
            <div style={{ textAlign: 'center', padding: 24 }}>
              <Spin />
            </div>
          ) : filteredPermMembers.length === 0 && !permOwner ? (
            <div style={{ textAlign: 'center', padding: 24, color: '#94a3b8', fontSize: 13 }}>
              {permSearch ? t('domainSpace.noMatchMember') : t('domainSpace.noMembers')}
            </div>
          ) : (
            <>
            {/* 创建者（owner）：只读展示，不落 space_permissions、不可删/改角色 */}
            {permOwner && (
              <div className="yx-perm-user-row">
                <div className="yx-perm-avatar" style={{ background: '#3b82f6' }}>
                  {(permOwner.display_name || permOwner.user_id).charAt(0).toUpperCase()}
                </div>
                <div className="yx-perm-user-info" style={{ flex: 1 }}>
                  <div className="yx-perm-user-name">
                    {permOwner.display_name || `ID: ${permOwner.user_id.slice(0, 8)}`}
                  </div>
                  <div className="yx-perm-user-dept">{t('domainSpace.ownerLabel')}</div>
                </div>
                <span
                  style={{
                    fontSize: 12,
                    color: '#3b82f6',
                    background: '#eff6ff',
                    padding: '2px 10px',
                    borderRadius: 10,
                  }}
                >
                  {t('domainSpace.ownerLabel')}
                </span>
              </div>
            )}
            {filteredPermMembers.map((member) => (
              <div key={member.id} className="yx-perm-user-row">
                <div className="yx-perm-avatar" style={{ background: member.avatarColor }}>
                  {member.avatar}
                </div>
                <div className="yx-perm-user-info" style={{ flex: 1 }}>
                  <div className="yx-perm-user-name">{member.name}</div>
                  <div className="yx-perm-user-dept">{member.department || `ID: ${member.id.slice(0, 8)}`}</div>
                </div>
                <div className="yx-perm-radio">
                  <label className={`yx-perm-radio-label${member.role === 'member' ? ' is-checked' : ''}`}>
                    <input
                      type="radio"
                      name={`perm-${member.id}`}
                      value="member"
                      checked={member.role === 'member'}
                      disabled={!canManage}
                      onChange={() => {
                        setPermMembers((prev) =>
                          prev.map((m) => (m.id === member.id ? { ...m, role: 'member' as const } : m)),
                        );
                      }}
                    />
                    {t('permission.spaceMember')}
                  </label>
                  <label className={`yx-perm-radio-label${member.role === 'space_manager' ? ' is-checked' : ''}`}>
                    <input
                      type="radio"
                      name={`perm-${member.id}`}
                      value="space_manager"
                      checked={member.role === 'space_manager'}
                      disabled={!canManage}
                      onChange={() => {
                        setPermMembers((prev) =>
                          prev.map((m) => (m.id === member.id ? { ...m, role: 'space_manager' as const } : m)),
                        );
                      }}
                    />
                    {t('permission.spaceManager')}
                  </label>
                </div>
                {canManage && (
                <Button
                  type="text"
                  className="yx-perm-remove-btn"
                  onClick={() => removePermMember(member.id)}
                  title={t('domainSpace.removeMember')}
                  style={{
                    color: '#94a3b8',
                    fontSize: 16,
                    padding: '0 0 0 8px',
                    lineHeight: 1,
                  }}
                >
                  <CloseOutlined />
                </Button>
                )}
              </div>
            ))
            }
            </>
          )}
        </div>
      </Modal>
    );
  },
);

export default SpacePermissionModal;
