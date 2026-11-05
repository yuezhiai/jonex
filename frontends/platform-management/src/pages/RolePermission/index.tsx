import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, Col, Empty, Row, Result, Spin, Tag, Typography } from 'antd';
import { EditOutlined, TeamOutlined, PlusOutlined } from '@ant-design/icons';
import { listRoles, listPermissions, type RoleItem, type PermissionItem } from '../../api/roles';
import { readCachedUser, isPlatformAdmin, type ShellUser } from '@jonex/shell-sdk';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import PermissionEditModal, { type PermissionEditModalRef } from './PermissionEditModal';
import RoleUserAssignModal, { type RoleUserAssignModalRef } from './RoleUserAssignModal';
import NewRoleModal, { type NewRoleModalRef } from './NewRoleModal';

const BUILT_IN_ROLE_KEYS: Record<string, string> = {
  admin: 'systemAdmin',
  user: 'user',
  平台管理员: 'platformAdmin',
  租户管理员: 'systemAdmin',
  领域服务管理员: 'domainServiceAdmin',
  知识编辑者: 'knowledgeEditor',
  观察者: 'observer',
};

function roleCopy(role: RoleItem, t: (key: string) => string) {
  const key = BUILT_IN_ROLE_KEYS[role.name];
  return key
    ? {
        name: t(`rolePermission.builtInRoles.${key}.name`),
        description: t(`rolePermission.builtInRoles.${key}.description`),
      }
    : { name: role.name, description: role.description || '--' };
}

export default function RolePermission() {
  const { t } = useTranslation();
  const cachedUser = readCachedUser<ShellUser>();
  const isPlatformAdminFlag = isPlatformAdmin(cachedUser);
  const [roles, setRoles] = useState<RoleItem[]>([]);
  const [perms, setPerms] = useState<PermissionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const permModalRef = useRef<PermissionEditModalRef>(null);
  const userAssignModalRef = useRef<RoleUserAssignModalRef>(null);
  const newRoleModalRef = useRef<NewRoleModalRef>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [r, p] = await Promise.all([listRoles(), listPermissions()]);
      setRoles(r.items);
      setPerms(p.items);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t('rolePermission.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
  }, [load]);

  if (error)
    return (
      <Result
        status="error"
        title={t('rolePermission.loadFailed')}
        subTitle={error}
        extra={
          <Button type="primary" onClick={load}>
            {t('common.retry')}
          </Button>
        }
      />
    );

  // 非平台管理员不可见系统角色（平台管理员）
  const visibleRoles = roles.filter((r) => isPlatformAdminFlag || r.is_system !== 1);

  return (
    <>
      <PageHeader
        title={t('rolePermission.pageTitle')}
        extra={
          // marginLeft:auto 把这一个按钮推到页头行尾（PageHeader 的 extra 默认紧挨标题）
          <Button
            type="primary"
            icon={<PlusOutlined />}
            style={{ marginLeft: 'auto' }}
            onClick={() => newRoleModalRef.current?.open()}
          >
            {t('rolePermission.newRole')}
          </Button>
        }
      />
      <PageContent showCard={false}>
        <Spin spinning={loading}>
          {visibleRoles.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('common.noData')} />
          ) : (
            <Row gutter={[16, 16]}>
              {visibleRoles.map((r) => {
                const isAdmin = r.is_system === 1;
                const display = roleCopy(r, t);
                return (
                  <Col key={r.id} xs={24} md={12} xl={8}>
                    {/* 同排等高：Row 默认 stretch，Card 撑满 Col 的高度 */}
                    <Card
                      // 角色卡信息量小（名字 + 一句说明 + 两个动作），默认内边距把它撑得过大
                      size="small"
                      // 同排等高靠 height:100% 撑，但撑出来的余量默认堆在操作栏**下面**；
                      // 让卡片纵向 flex、正文吃掉余量，操作栏才能贴住卡片底边
                      style={{ height: '100%', display: 'flex', flexDirection: 'column' }}
                      styles={{ body: { flex: 1 } }}
                      title={
                        <span>
                          {display.name}
                          {isAdmin ? <Typography.Text type="secondary">{t('rolePermission.system')}</Typography.Text> : null}
                        </span>
                      }
                      actions={[
                        <Button
                          key="users"
                          type="link"
                          size="small"
                          icon={<TeamOutlined />}
                          onClick={() => userAssignModalRef.current?.open(r)}
                        >
                          {t('rolePermission.assignUsers')}
                        </Button>,
                        <Button
                          key="perms"
                          type="link"
                          size="small"
                          icon={<EditOutlined />}
                          onClick={() => permModalRef.current?.open(r)}
                        >
                          {t('rolePermission.editPerms')}
                        </Button>,
                      ]}
                    >
                      {/* 说明固定两行高：否则长短不一的描述会把同排卡片的内文挤成不齐 */}
                      <Typography.Paragraph
                        type="secondary"
                        ellipsis={{ rows: 2, tooltip: display.description }}
                        style={{ marginBottom: isAdmin ? 8 : 0, minHeight: 40 }}
                      >
                        {display.description}
                      </Typography.Paragraph>
                      {isAdmin ? <Tag color="blue">{t('rolePermission.allPermissions')}</Tag> : null}
                    </Card>
                  </Col>
                );
              })}
            </Row>
          )}
        </Spin>

        <PermissionEditModal ref={permModalRef} perms={perms} onSaved={load} />
        <RoleUserAssignModal ref={userAssignModalRef} onSaved={load} />
        <NewRoleModal ref={newRoleModalRef} onCreated={load} />
      </PageContent>
    </>
  );
}
