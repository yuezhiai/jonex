import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useNavigate } from 'react-router-dom';
import { Input, Button, Spin, Result, message } from 'antd';
import {
  SaveOutlined,
  TeamOutlined,
  UserAddOutlined,
  SearchOutlined,
  WarningOutlined,
  DeleteOutlined,
  CloseOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons';
import { emitSpacesInvalidated } from '@jonex/shell-sdk';
import {
  getSpace,
  updateSpace,
  getSpacePermissions,
  updateSpacePermissions,
  getSpacePermissionCandidates,
  type SpaceOwnerInfo,
} from '../../api/domainSpace';
import type { DomainSpace } from '../../types/domainSpace';
import { candidateToPermMember, type MemberCandidate, type PermMember, type SpaceRole } from '../../types/domainService';
import { useStore } from '../../store';
import DeleteSpaceModal from './DeleteSpaceModal';
import type { DeleteSpaceModalHandle } from './DeleteSpaceModal';

const DomainSpaceSettings = function DomainSpaceSettings() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { global } = useStore();

  const [space, setSpace] = useState<DomainSpace | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 空间管理权限（owner/租户管理员）：基本信息保存、成员管理、删除空间统一按此控制
  const canManage = space?.can_manage_permissions ?? false;

  // 基本信息
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [savingInfo, setSavingInfo] = useState(false);

  // 权限成员
  const [permMembers, setPermMembers] = useState<PermMember[]>([]);
  // 空间创建者（纯展示：owner_id 不参与判定；创建人本身以 space_manager 出现在成员列表里）
  const [permOwner, setPermOwner] = useState<SpaceOwnerInfo | null>(null);
  const [permLoading, setPermLoading] = useState(false);
  const [permSaving, setPermSaving] = useState(false);
  const [permSearch, setPermSearch] = useState('');

  // 用户选择器
  const [userSelectOpen, setUserSelectOpen] = useState(false);
  const [userSearchText, setUserSearchText] = useState('');
  const [availableUsers, setAvailableUsers] = useState<MemberCandidate[]>([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const userSelectRef = useRef<HTMLDivElement>(null);

  const deleteRef = useRef<DeleteSpaceModalHandle>(null);

  // ── 加载空间详情 ──
  const loadSpace = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const data = await getSpace(id);
      setSpace(data);
      setName(data.name);
      setDescription(data.description || '');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : t('common.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [id]);

  // ── 加载权限 ──
  const loadPermissions = useCallback(async () => {
    if (!id) return;
    setPermLoading(true);
    try {
      const { permissions: perms, owner } = await getSpacePermissions(id);
      setPermOwner(owner);
      // 成员展示名直接来自后端 join 的 display_name（viewer/知识编辑者无 user:read）
      setPermMembers(
        perms.map((p) => {
          const uid = String(p.user_id);
          const name = p.display_name || t('domainSpace.userPrefix', { id: uid.slice(0, 8) });
          // [jonex] B2（D2）：后端取值已改为 space_manager / member。
          // 原实现是「非 manager 一律当 viewer」的三元兜底 —— 改名后若只换字面量、
          // 不换兜底逻辑，未知取值会被静默当成 member（越权方向安全，但会掩盖迁移未跑）。
          // 这里改为显式映射，未知值保持 member 但可由后端白名单兜底拒绝。
          const role: SpaceRole = p.role === 'space_manager' ? 'space_manager' : 'member';
          return {
            id: uid,
            name,
            department: '',
            avatar: name.charAt(0).toUpperCase(),
            avatarColor: '#94a3b8',
            role,
          };
        }),
      );
    } catch {
      setPermMembers([]);
    } finally {
      setPermLoading(false);
    }
  }, [id, t]);

  useEffect(() => {
    loadSpace();
    loadPermissions();
  }, [loadSpace, loadPermissions]);

  // 在空间详情页时，切换全局空间 → 页面跟随跳转到对应空间的详情
  useEffect(() => {
    if (global.currentSpaceId && id && global.currentSpaceId !== id) {
      navigate(`/domain-space/${global.currentSpaceId}/settings`, { replace: true });
    }
  }, [global.currentSpaceId, id, navigate]);

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

  const loadAvailableUsers = useCallback(async () => {
    if (!id) return;
    setUsersLoading(true);
    try {
      const result = await getSpacePermissionCandidates(id);
      setAvailableUsers(result);
    } catch {
      setAvailableUsers([]);
    } finally {
      setUsersLoading(false);
    }
  }, [id]);

  // ── 基本信息保存 ──
  const handleSaveInfo = async () => {
    if (!id) return;
    if (!name.trim()) {
      message.warning(t('common.nameRequired'));
      return;
    }
    setSavingInfo(true);
    try {
      await updateSpace(id, { name: name.trim(), description });
      message.success(t('common.saveSuccess'));
      await global.refreshSpaces();
      emitSpacesInvalidated();
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.saveFailed'));
    } finally {
      setSavingInfo(false);
    }
  };

  // ── 权限成员 ──
  const addPermMember = (user: MemberCandidate) => {
    setPermMembers((prev) => {
      if (prev.some((m) => m.id === user.user_id)) return prev;
      return [...prev, candidateToPermMember(user, 'member')];
    });
  };

  const removePermMember = (userId: string) => {
    setPermMembers((prev) => prev.filter((m) => m.id !== userId));
  };

  const setMemberRole = (userId: string, role: SpaceRole) => {
    setPermMembers((prev) => prev.map((m) => (m.id === userId ? { ...m, role } : m)));
  };

  const handleSavePermissions = async () => {
    if (!id) return;
    setPermSaving(true);
    try {
      await updateSpacePermissions(
        id,
        permMembers.map((m) => ({ user_id: m.id, role: m.role })),
      );
      message.success(t('common.saveSuccess'));
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('common.saveFailed'));
    } finally {
      setPermSaving(false);
    }
  };

  const addedUserIds = new Set(permMembers.map((m) => m.id));
  // 当前登录用户（jonex_user 缓存为 ShellUser，id 兜底 user_id），添加成员时过滤掉自己
  const selfUserId = global.userInfo ? String(global.userInfo.id ?? global.userInfo.user_id ?? '') : '';
  // [jonex] B2（E3）：**撤掉了 owner 的候选排除**（原先这里算 ownerId 用于过滤）。
  // D2 之后创建人就是普通的 space_manager，可被调整甚至移除。
  const filteredAvailableUsers = availableUsers.filter((u) => {
    const uid = u.user_id;
    if (selfUserId && uid === selfUserId) return false; // 过滤自己的账号
    if (addedUserIds.has(uid)) return false;
    if (!userSearchText) return true;
    const q = userSearchText.toLowerCase();
    return (
      (u.display_name || '').toLowerCase().includes(q) ||
      (u.username || '').toLowerCase().includes(q) ||
      (u.email || '').toLowerCase().includes(q)
    );
  });
  const filteredPermMembers = permMembers.filter((m) => {
    if (!permSearch) return true;
    return m.name.includes(permSearch) || m.department.includes(permSearch);
  });

  const handleDeleted = () => {
    navigate('/domain-space');
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 300 }}>
        <Spin size="large" />
      </div>
    );
  }
  if (error || !space) {
    return (
      <Result
        status="error"
        title={t('common.loadFailed')}
        subTitle={error || t('domainSpace.notFound')}
        extra={
          <Button type="primary" onClick={() => navigate('/domain-space')}>
            {t('common.backToList')}
          </Button>
        }
      />
    );
  }

  return (
    <div className="yx-domain-space-page">
      {/* 页面标题 */}
      <div className="yx-page-header">
        <h1 className="yx-page-title" style={{ margin: 0 }}>
          {t('domainSpace.settingsTitle', { name: space.name })}
        </h1>
      </div>

      {/* 基本信息 */}
      <section style={{ marginBottom: 28 }}>
        <h2
          style={{
            fontSize: 17,
            fontWeight: 600,
            color: '#0b2b5c',
            marginBottom: 16,
            paddingBottom: 12,
            borderBottom: '1px solid #eef2f6',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <InfoCircleOutlined style={{ color: '#3b82f6' }} /> {t('domainSpace.sectionInfo')}
        </h2>
        <div className="yx-card" style={{ padding: '24px 28px' }}>
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 13, color: '#64748b', marginBottom: 6 }}>{t('domainSpace.name')}</div>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={128}
              placeholder={t('domainSpace.nameInputPlaceholder')}
            />
          </div>
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 13, color: '#64748b', marginBottom: 6 }}>{t('domainSpace.description')}</div>
            <Input.TextArea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              placeholder={t('domainSpace.descriptionPlaceholder')}
            />
          </div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12, paddingTop: 8 }}>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={savingInfo}
              disabled={!canManage}
              title={canManage ? undefined : t('domainSpace.noManagePermission')}
              onClick={handleSaveInfo}
            >
              {t('common.save')}
            </Button>
          </div>
        </div>
      </section>

      {/* 成员权限 */}
      <section style={{ marginBottom: 28 }}>
        <h2
          style={{
            fontSize: 17,
            fontWeight: 600,
            color: '#0b2b5c',
            marginBottom: 16,
            paddingBottom: 12,
            borderBottom: '1px solid #eef2f6',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <TeamOutlined style={{ color: '#3b82f6' }} /> {t('domainSpace.permissionSettings')}
        </h2>
        <div className="yx-card" style={{ padding: '24px 28px' }}>
          {/* 添加成员 */}
          <div ref={userSelectRef} style={{ position: 'relative', marginBottom: 12 }}>
            <Button
              icon={<UserAddOutlined />}
              disabled={!canManage}
              title={canManage ? undefined : t('domainSpace.noManagePermission')}
              onClick={() => {
                const willOpen = !userSelectOpen;
                setUserSelectOpen(willOpen);
                setUserSearchText('');
                if (willOpen && availableUsers.length === 0) loadAvailableUsers();
              }}
            >
              {t('domainSpace.addMember')}
            </Button>
            {userSelectOpen && (
              <div
                style={{
                  position: 'absolute',
                  top: 40,
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

          {/* 已有成员搜索 */}
          <Input
            prefix={<SearchOutlined style={{ color: '#94a3b8', fontSize: 14 }} />}
            placeholder={t('domainSpace.searchMemberPlaceholder')}
            value={permSearch}
            onChange={(e) => setPermSearch(e.target.value)}
            style={{ width: '100%', marginBottom: 12 }}
          />

          {/* 成员列表 */}
          <div style={{ maxHeight: 320, overflowY: 'auto', marginBottom: 16 }}>
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
                        onChange={() => setMemberRole(member.id, 'member')}
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
                        onChange={() => setMemberRole(member.id, 'space_manager')}
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
                    style={{ color: '#94a3b8', fontSize: 16, padding: '0 0 0 8px', lineHeight: 1 }}
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
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={permSaving}
              disabled={!canManage}
              title={canManage ? undefined : t('domainSpace.noManagePermission')}
              onClick={handleSavePermissions}
            >
              {t('domainSpace.savePermission')}
            </Button>
          </div>
        </div>
      </section>

      {/* 危险区 */}
      <section>
        <div style={{ border: '1px solid #fecaca', borderRadius: 14, overflow: 'hidden' }}>
          <div
            style={{
              background: '#fef2f2',
              padding: '16px 24px',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              fontWeight: 600,
              color: '#dc2626',
              fontSize: 15,
            }}
          >
            <WarningOutlined /> {t('domainSpace.dangerZone')}
          </div>
          <div
            style={{
              background: '#fff',
              padding: 24,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            <div>
              <p style={{ fontSize: 14, color: '#1e293b', fontWeight: 500, marginBottom: 4 }}>
                {t('domainSpace.deleteSpaceTitle')}
              </p>
              <p style={{ fontSize: 13, color: '#64748b' }}>{t('domainSpace.deleteWarning')}</p>
            </div>
            <Button
              danger
              type="primary"
              icon={<DeleteOutlined />}
              disabled={!canManage}
              title={canManage ? undefined : t('domainSpace.noManagePermission')}
              onClick={() => deleteRef.current?.open(space.name)}
            >
              {t('domainSpace.deleteSpaceBtn')}
            </Button>
          </div>
        </div>
      </section>

      <DeleteSpaceModal ref={deleteRef} spaceId={id || ''} onDeleted={handleDeleted} />
    </div>
  );
};

export default DomainSpaceSettings;
