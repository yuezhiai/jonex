import React from 'react';
import { Modal, Button, Input } from 'antd';
import { SettingOutlined, CloseOutlined, SearchOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { DomainKnowledgeItem, DomainKnowledgePermissionMember } from '@/types/domainKnowledge';

interface PermissionModalProps {
  open: boolean;
  currentKb: DomainKnowledgeItem | null;
  members: DomainKnowledgePermissionMember[];
  keyword: string;
  loading: boolean;
  saving: boolean;
  onKeywordChange: (val: string) => void;
  onRoleChange: (userId: string, role: 'viewer' | 'editor') => void;
  onSave: () => void;
  onCancel: () => void;
}

export default function PermissionModal({
  open,
  currentKb,
  members,
  keyword,
  loading,
  saving,
  onKeywordChange,
  onRoleChange,
  onSave,
  onCancel,
}: PermissionModalProps) {
  const { t } = useTranslation();

  return (
    <Modal open={open} onCancel={onCancel} footer={null} width={600} closable={false} styles={{ body: { padding: 0 } }}>
      <div className="yx-modal-header">
        <h2>
          <SettingOutlined style={{ color: '#3b82f6' }} /> {t('domainKnowledge.permissionSettingsTitle')}
        </h2>
        <Button type="text" className="yx-modal-close-btn" onClick={onCancel}>
          <CloseOutlined />
        </Button>
      </div>
      <div style={{ padding: '20px 24px' }}>
        <p style={{ fontSize: 14, color: '#475569', marginBottom: 16 }}>
          {t('domainKnowledge.permissionDescription', { name: currentKb?.name })}
        </p>
        <Input
          prefix={<SearchOutlined style={{ color: '#94a3b8', fontSize: 14 }} />}
          placeholder={t('domainKnowledge.searchUserOrRole')}
          value={keyword}
          onChange={(e) => onKeywordChange(e.target.value)}
          style={{ width: '100%', marginBottom: 12 }}
        />
        {loading ? (
          <div style={{ textAlign: 'center', padding: 40, color: '#94a3b8' }}>{t('common.loading')}</div>
        ) : (
          members.map((u, i) => {
            const checked = u.role;
            return (
              <div className="yx-perm-user-row" key={u.userId}>
                <div className="yx-perm-avatar" style={{ background: u.avatarColor }}>
                  {u.avatarText}
                </div>
                <div className="yx-perm-user-info">
                  <div className="yx-perm-user-name">{u.name}</div>
                  <div className="yx-perm-user-dept">{u.dept}</div>
                </div>
                <div className="yx-perm-radio">
                  {(['viewer', 'editor'] as const).map((role) => {
                    const isActive = checked === role;
                    const label = role === 'viewer' ? t('permission.view') : t('permission.manage');
                    return (
                      <label key={role} className={isActive ? 'is-checked' : ''}>
                        <input
                          type="radio"
                          name={`perm-${i}`}
                          checked={isActive}
                          onChange={() => onRoleChange(u.userId, role)}
                        />
                        {label}
                      </label>
                    );
                  })}
                </div>
              </div>
            );
          })
        )}
      </div>
      <div className="yx-modal-footer">
        <Button onClick={onCancel}>{t('common.cancel')}</Button>
        <Button type="primary" loading={saving} onClick={onSave}>
          {t('domainKnowledge.savePermission')}
        </Button>
      </div>
    </Modal>
  );
}
