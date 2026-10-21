import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { Input, Button, Result, Spin, message } from 'antd';
import { PlusCircleOutlined } from '@ant-design/icons';
import { emitSpacesInvalidated } from '@jonex/shell-sdk';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import { createSpace } from '../../api/domainSpace';
import type { DomainSpaceFormData } from '../../types/domainSpace';
import { useStore } from '../../store';

export default function DomainSpaceCreate() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { global } = useStore();
  const [form, setForm] = useState<DomainSpaceFormData>({ name: '', description: '' });
  const [submitting, setSubmitting] = useState(false);

  // [jonex] W2（2026-10-19）：本页原先无任何权限守卫，无创建权的用户
  // （既非租户管理员、也没有 space:write）能一路填完表单，提交时才吃 403。
  // 现在按后端 can_create_space 判（与 create 准入同源），直接拦在表单之前。
  // 复用全局 store 的列表请求（AppLayout 已 loadSpaces，幂等），不额外发请求。
  const { loadSpaces } = global;
  // 列表请求失败时不能把用户永久卡在 loading：fail-open 回落到表单，
  // 真正的准入仍由后端 can_create_space（create）兜底。
  const [loadFailed, setLoadFailed] = useState(false);
  useEffect(() => {
    if (global.spacesLoaded) return undefined;
    let cancelled = false;
    void loadSpaces().catch(() => {
      if (!cancelled) setLoadFailed(true);
    });
    return () => {
      cancelled = true;
    };
  }, [loadSpaces, global.spacesLoaded]);

  const handleCreate = async () => {
    if (!form.name.trim()) {
      message.warning(t('domainSpace.nameRequired'));
      return;
    }
    setSubmitting(true);
    try {
      const saved = await createSpace({ ...form, name: form.name.trim() });
      message.success(t('domainSpace.createSuccess'));
      await global.refreshSpaces();
      if (saved?.id) {
        global.setCurrentSpaceId(saved.id, { persist: true, broadcast: true });
        emitSpacesInvalidated();
        navigate(`/domain-space/${saved.id}/settings`, { replace: true });
        return;
      }
      emitSpacesInvalidated();
      navigate('/domain-space', { replace: true });
    } catch (err: unknown) {
      message.error(err instanceof Error ? err.message : t('domainSpace.createFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  // 列表还没回来时先给 loading：否则首屏 canCreateSpace 恒为 false 会误闪「无权限」。
  // loadFailed 时放行到表单（fail-open），准入仍由后端拦截。
  if (!global.spacesLoaded && !loadFailed) {
    return (
      <>
        <PageHeader title={t('domainSpace.create')} />
        <PageContent showCard={false}>
          <div style={{ display: 'flex', justifyContent: 'center', padding: '80px 0' }}>
            <Spin />
          </div>
        </PageContent>
      </>
    );
  }

  if (!global.canCreateSpace) {
    return (
      <>
        <PageHeader title={t('domainSpace.create')} />
        <PageContent showCard={false}>
          <Result
            status="403"
            title={t('domainSpace.noCreatePermission')}
            subTitle={t('domainSpace.noCreatePermissionHint')}
            extra={
              <Button onClick={() => navigate('/domain-space', { replace: true })}>
                {t('domainSpace.backToList')}
              </Button>
            }
          />
        </PageContent>
      </>
    );
  }

  return (
    <>
      <PageHeader title={t('domainSpace.create')} />
      <PageContent showCard={false}>
        <div className="yx-domain-space-page">
          <div className="yx-card" style={{ padding: '28px 32px', maxWidth: 640 }}>
            <div style={{ marginBottom: 20 }}>
              <div style={{ fontSize: 13, color: '#64748b', marginBottom: 6 }}>
                <span style={{ color: '#ef4444', marginRight: 4 }}>*</span>
                {t('domainSpace.name')}
              </div>
              <Input
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                placeholder={t('domainSpace.namePlaceholder')}
                maxLength={255}
                onPressEnter={handleCreate}
              />
            </div>
            <div style={{ marginBottom: 24 }}>
              <div style={{ fontSize: 13, color: '#64748b', marginBottom: 6 }}>{t('domainSpace.description')}</div>
              <Input.TextArea
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                placeholder={t('domainSpace.descriptionPlaceholder')}
                rows={4}
                maxLength={1024}
                showCount
              />
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
              <Button onClick={() => navigate('/domain-space')}>{t('common.cancel')}</Button>
              <Button type="primary" icon={<PlusCircleOutlined />} loading={submitting} onClick={handleCreate}>
                {t('common.create')}
              </Button>
            </div>
          </div>
        </div>
      </PageContent>
    </>
  );
}
