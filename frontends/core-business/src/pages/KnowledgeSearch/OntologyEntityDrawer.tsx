import { useTranslation } from 'react-i18next';
import { Drawer, Typography, Descriptions, Spin, Empty, Table } from 'antd';
import type { OntologyInstanceRow } from '@/types/domainKnowledge';

const { Title, Text } = Typography;

interface OntologyEntityDrawerProps {
  open: boolean;
  loading: boolean;
  entity: OntologyInstanceRow | null;
  attributeEntries: { key: string; value: unknown }[];
  onClose: () => void;
}

function SectionHeader({ title }: { title: string }) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        marginBottom: 16,
      }}
    >
      <div
        style={{
          width: 4,
          height: 16,
          background: '#3b82f6',
          borderRadius: 2,
        }}
      />
      <Title level={5} style={{ margin: 0, fontWeight: 600 }}>
        {title}
      </Title>
    </div>
  );
}

export default function OntologyEntityDrawer({
  open,
  loading,
  entity,
  attributeEntries,
  onClose,
}: OntologyEntityDrawerProps) {
  const { t } = useTranslation();

  const columns = [
    {
      title: t('knowledgeSearch.attributeName'),
      dataIndex: 'attrKey',
      key: 'attrKey',
      width: '40%',
      render: (text: string) => <Text style={{ fontWeight: 500 }}>{text}</Text>,
    },
    {
      title: t('knowledgeSearch.attributeValue'),
      dataIndex: 'value',
      key: 'value',
      render: (value: unknown) => {
        const display =
          value == null
            ? '—'
            : typeof value === 'object'
            ? JSON.stringify(value)
            : String(value);
        return <Text>{display}</Text>;
      },
    },
  ];

  const dataSource = attributeEntries.map(({ key, value }, index) => ({
    key: `${key}-${index}`,
    attrKey: key,
    value,
  }));

  return (
    <Drawer
      title={t('knowledgeSearch.ontologyEntityDetail')}
      size="large"
      open={open}
      onClose={onClose}
      destroyOnHidden
    >
      <Spin spinning={loading}>
        {entity ? (
          <>
            <section style={{ marginBottom: 24 }}>
              <SectionHeader title={t('knowledgeSearch.basicInfo')} />
              <Descriptions
                column={1}
                layout="vertical"
                size="small"
                colon={false}
                styles={{ label: { fontWeight: 600 } }}
              >
                <Descriptions.Item label={t('knowledgeSearch.entityName')}>
                  {entity.name || '—'}
                </Descriptions.Item>
                <Descriptions.Item label={t('knowledgeSearch.entityDescription')}>
                  {entity.description || '—'}
                </Descriptions.Item>
              </Descriptions>
            </section>

            <section>
              <SectionHeader title={t('knowledgeSearch.attributes')} />
              {attributeEntries.length > 0 ? (
                <Table
                  columns={columns}
                  dataSource={dataSource}
                  rowKey="key"
                  pagination={false}
                  size="middle"
                  bordered
                 
                />
              ) : (
                <Empty description={t('knowledgeSearch.noAttributes')} />
              )}
            </section>
          </>
        ) : (
          <Empty description={t('knowledgeSearch.entityDetailEmpty')} />
        )}
      </Spin>
    </Drawer>
  );
}
