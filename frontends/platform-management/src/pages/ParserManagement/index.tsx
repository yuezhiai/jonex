import {
  SearchOutlined,
  VideoCameraOutlined,
  AudioOutlined,
  FileImageOutlined,
  FileTextOutlined,
  GlobalOutlined,
  PartitionOutlined,
} from '@ant-design/icons';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Empty, Input, Spin } from 'antd';
import dayjs from 'dayjs';
import { PageContent, PageHeader } from '@jonex/shared-lib';
import { listParsers, type ParserItem } from '@/api/parsers';
import './index.css';

interface DisplayField {
  label: string;
  value: string;
}

interface TypeMeta {
  icon: React.ReactNode;
  gradient: string;
  order: number;
}

/** 解析器类型 → 图标与配色；顺序即卡片默认排序 */
const TYPE_META: Record<string, TypeMeta> = {
  video: {
    icon: <VideoCameraOutlined />,
    gradient: 'linear-gradient(135deg, #f87171, #dc2626)',
    order: 0,
  },
  audio: {
    icon: <AudioOutlined />,
    gradient: 'linear-gradient(135deg, #a78bfa, #7c3aed)',
    order: 1,
  },
  image: {
    icon: <FileImageOutlined />,
    gradient: 'linear-gradient(135deg, #fbbf24, #d97706)',
    order: 2,
  },
  document: {
    icon: <FileTextOutlined />,
    gradient: 'linear-gradient(135deg, #60a5fa, #1d4ed8)',
    order: 3,
  },
  txt: {
    icon: <FileTextOutlined />,
    gradient: 'linear-gradient(135deg, #60a5fa, #1d4ed8)',
    order: 4,
  },
  web: {
    icon: <GlobalOutlined />,
    gradient: 'linear-gradient(135deg, #94a3b8, #64748b)',
    order: 5,
  },
  cad: {
    icon: <PartitionOutlined />,
    gradient: 'linear-gradient(135deg, #94a3b8, #64748b)',
    order: 6,
  },
};

const FALLBACK_META: TypeMeta = {
  icon: <FileTextOutlined />,
  gradient: 'linear-gradient(135deg, #94a3b8, #64748b)',
  order: 99,
};

/** 内置解析器 id → i18n 名称键；库里没有的（租户自建）直接显示 name */
const BUILT_IN_NAME_KEYS: Record<string, string> = {
  video_full_pipeline: 'nameVideoVlm',
  video_mps: 'nameVideoMps',
  audio_transcribe: 'nameAudio',
  image_parse: 'nameImage',
  document_parse: 'nameDocument',
  text_parse: 'nameText',
  parser_demo_web: 'nameWeb',
  parser_demo_cad: 'nameCad',
};

const MAX_CHIPS = 5;

const readConfig = (item: ParserItem) => {
  const config = (item.config_json ?? {}) as Record<string, unknown>;
  const fields = Array.isArray(config.display_fields)
    ? (config.display_fields as DisplayField[]).filter((field) => field && field.label)
    : [];
  const version = typeof config.version === 'string' && config.version ? config.version : '--';
  const processCount = Number(config.process_count);
  return {
    fields,
    version,
    processCount: Number.isFinite(processCount) ? processCount : 0,
  };
};

const ParserManagement: React.FC = () => {
  const { t } = useTranslation();
  const [search, setSearch] = useState('');
  const [parsers, setParsers] = useState<ParserItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadParsers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await listParsers(0, 100);
      setParsers(resp.items || []);
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : t('parserManagement.requestFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadParsers();
  }, [loadParsers]);

  /** 只展示已启用的解析器：未上线（status !== 'active'）的直接不出现 */
  const visibleParsers = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    return parsers
      .filter((item) => item.status === 'active')
      .filter((item) => {
        if (!keyword) return true;
        const haystack = [item.name, item.parser_type, ...(item.file_types ?? [])]
          .join(' ')
          .toLowerCase();
        return haystack.includes(keyword);
      })
      .sort((a, b) => {
        const orderA = (TYPE_META[a.parser_type] ?? FALLBACK_META).order;
        const orderB = (TYPE_META[b.parser_type] ?? FALLBACK_META).order;
        return orderA - orderB || a.name.localeCompare(b.name);
      });
  }, [parsers, search]);

  const renderCard = (item: ParserItem) => {
    const meta = TYPE_META[item.parser_type] ?? FALLBACK_META;
    const { fields, version, processCount } = readConfig(item);
    const formats = item.file_types ?? [];
    const shown = formats.slice(0, MAX_CHIPS);
    const hiddenCount = formats.length - shown.length;
    const nameKey = BUILT_IN_NAME_KEYS[item.id];
    const displayName = nameKey ? t(`parserManagement.${nameKey}`) : item.name;
    const updatedText = item.updated_at ? dayjs(item.updated_at).format('MM-DD HH:mm') : '--';

    return (
      <div className="parser-card" key={item.id}>
        <div className="parser-card-head">
          <span className="parser-card-ico" style={{ background: meta.gradient }}>
            {meta.icon}
          </span>
          <div className="parser-card-title">
            <b title={displayName}>{displayName}</b>
            <small>
              {t('parserManagement.formatsCount', { count: formats.length })} · {version}
            </small>
          </div>
        </div>

        {shown.length > 0 && (
          <div className="parser-chips">
            {shown.map((format) => (
              <span className="parser-chip" key={format}>
                {format}
              </span>
            ))}
            {hiddenCount > 0 && <span className="parser-chip is-more">+{hiddenCount}</span>}
          </div>
        )}

        {fields.length > 0 && (
          <dl className="parser-fields">
            {fields.map((field) => (
              <React.Fragment key={field.label}>
                <dt>{field.label}</dt>
                <dd title={field.value}>{field.value}</dd>
              </React.Fragment>
            ))}
          </dl>
        )}

        <div className="parser-card-foot">
          <span>{t('parserManagement.processedTimes', { count: processCount.toLocaleString() })}</span>
          <span className="parser-card-sep">·</span>
          <span>{t('parserManagement.updatedAt', { time: updatedText })}</span>
        </div>
      </div>
    );
  };

  return (
    <>
      <PageHeader title={t('parserManagement.pageTitle')} />
      <PageContent showCard={false}>
        <div className="parser-toolbar">
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder={t('parserManagement.searchPlaceholder')}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            style={{ width: 264 }}
          />
          <span className="parser-toolbar-count">
            {t('parserManagement.totalCount', { count: visibleParsers.length })}
          </span>
        </div>

        {error ? (
          <div className="parser-error">
            <span>{error}</span>
            <Button size="small" onClick={() => void loadParsers()}>
              {t('common.retry')}
            </Button>
          </div>
        ) : loading ? (
          <div className="parser-placeholder">
            <Spin />
          </div>
        ) : visibleParsers.length === 0 ? (
          <div className="parser-placeholder">
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={search ? t('parserManagement.noMatch') : t('parserManagement.empty')}
            />
          </div>
        ) : (
          <div className="parser-grid">{visibleParsers.map(renderCard)}</div>
        )}
      </PageContent>
    </>
  );
};

export default ParserManagement;
