import React from 'react';
import { Button, Modal, message } from 'antd';
import { ApiOutlined, PlusOutlined, CloudOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { deleteDataSource } from '@/api/dataSource';
import { dataSourceIconMap } from './index';
import type { DataSourceConfig } from '@/types/domainKnowledge';

interface DataSourceTabProps {
  dataSources: DataSourceConfig[];
  dataSourcesLoading: boolean;
  /** KB 写权限（权限矩阵）：空间 owner/manager/租户管理员 或 授权 editor */
  canWrite?: boolean;
  onAdd: () => void;
  onEdit: (ds: DataSourceConfig) => void;
  onReload: () => void;
}

export default function DataSourceTab({
  dataSources,
  dataSourcesLoading,
  canWrite = false,
  onAdd,
  onEdit,
  onReload,
}: DataSourceTabProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  return (
    <div className="config-section yx-kb-section-card">
      <div className="yx-kb-flex-header">
        <h3 className="yx-kb-section-title">
          <ApiOutlined className="yx-kb-icon-blue" /> {t('domainKnowledge.configuredDataSources')}
        </h3>
        <Button
          type="primary"
          className="yx-kb-section-add-btn"
          icon={<PlusOutlined />}
          disabled={!canWrite}
          title={canWrite ? undefined : t('domainSpace.noManagePermission')}
          onClick={onAdd}
        >
          {t('domainKnowledge.addDataSource')}
        </Button>
      </div>
      {dataSourcesLoading ? (
        <div className="yx-kb-empty-state">{t('common.loading')}</div>
      ) : (
        dataSources.map((ds) => {
          const DsIcon = dataSourceIconMap[ds.iconType] || CloudOutlined;
          return (
            <div
              key={ds.id}
              onClick={() => navigate(ds.path)}
              className="yx-kb-ds-card"
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLElement).style.borderColor = '#dbe7f5';
                (e.currentTarget as HTMLElement).style.background = '#fafcff';
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.borderColor = '#eef2f6';
                (e.currentTarget as HTMLElement).style.background = '#fff';
              }}
            >
              <div
                style={{
                  width: 40,
                  height: 40,
                  borderRadius: 8,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: 18,
                  background: ds.iconBg,
                  color: ds.iconColor,
                  flexShrink: 0,
                }}
              >
                <DsIcon />
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 14, fontWeight: 500, color: '#1e293b' }}>
                  {ds.name} <span style={{ fontSize: 12, color: '#94a3b8', fontWeight: 400 }}>· {ds.type}</span>
                </div>
                <div style={{ fontSize: 12, color: '#94a3b8', marginTop: 2 }}>{ds.desc}</div>
              </div>
              <span
                className="yx-kb-ds-link-edit"
                style={canWrite ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
                onClick={(e) => {
                  e.stopPropagation();
                  if (!canWrite) return;
                  onEdit(ds);
                }}
              >
                {t('domainKnowledge.edit')}
              </span>
              <span
                className="yx-kb-ds-link-del"
                style={canWrite ? undefined : { opacity: 0.4, cursor: 'not-allowed' }}
                onClick={(e) => {
                  e.stopPropagation();
                  if (!canWrite) return;
                  Modal.confirm({
                    title: t('domainKnowledge.deleteDataSourceTitle'),
                    content: t('domainKnowledge.deleteDataSourceContent', { name: ds.name }),
                    okText: t('common.okText'),
                    cancelText: t('common.cancelText'),
                    okButtonProps: { danger: true },
                    onOk: async () => {
                      await deleteDataSource(ds.id);
                      message.success(t('common.deleteSuccess'));
                      onReload();
                    },
                  });
                }}
              >
                {t('domainKnowledge.delete')}
              </span>
            </div>
          );
        })
      )}
    </div>
  );
}
