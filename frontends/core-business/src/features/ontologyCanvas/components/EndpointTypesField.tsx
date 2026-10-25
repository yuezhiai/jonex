import { useEffect, useRef, useState, type ReactNode } from 'react';
// 图标统一用 Jonex 已有的 @ant-design/icons，不引入第二套图标库（lucide-react）
import { CloseOutlined, LockOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import type { EntityType } from '../domain/index.js';

/**
 * 多态端点的类型集合编辑器(关系属性面板)。
 *
 * 关键语义:**声明的源/目标类型是「底线」**——知识库编译器要求集合必须包含它(不变量),
 * 所以它被渲染成锁定芯片(不可移除),额外允许的类型是白底可移除芯片。
 * 结构即信息:用户一眼看出「哪些是必须的、哪些是我加的」,而不必记住规则。
 *
 * @param single 底线的实体 id(源或目标)
 * @param value  集合(含 single);只含 single 时父级存 undefined(不写冗余字段)
 */
export function EndpointTypesField({ entities, single, value, onChange, field }: {
  entities: EntityType[];
  single: string;
  value: string[] | undefined;
  onChange: (ids: string[]) => void;
  /** 'sourceTypes' | 'targetTypes':写进 DOM(定位/断言用,同一面板会出现两次) */
  field: 'sourceTypes' | 'targetTypes';
}): ReactNode {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const extras = (value ?? []).filter((id) => id !== single);
  const entityOf = (id: string): EntityType | undefined => entities.find((entity) => entity.id === id);
  const addable = entities.filter((entity) => entity.id !== single && !extras.includes(entity.id));

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent): void => {
      if (boxRef.current !== null && !boxRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const commit = (ids: string[]): void => onChange([single, ...ids.filter((id) => id !== single)]);

  return (
    <div className={`type-chips type-chips--${field}`} ref={boxRef} data-field={field}>
      <span className="type-chip type-chip--core" title={t('relation.typeCoreTitle')}>
        <LockOutlined className="type-chip-lock" style={{ fontSize: 11 }} aria-hidden="true" />
        <span className="type-chip-name">{entityOf(single)?.displayName ?? entityOf(single)?.name ?? single}</span>
      </span>
      {extras.map((id) => (
        <span className="type-chip type-chip--extra" key={id}>
          <span className="type-chip-name">{entityOf(id)?.displayName ?? entityOf(id)?.name ?? id}</span>
          <button type="button" className="type-chip-remove" aria-label={t('relation.typeRemove', { name: entityOf(id)?.name ?? id })} title={t('relation.typeRemove', { name: entityOf(id)?.name ?? id })} onClick={() => commit(extras.filter((item) => item !== id))}>
            <CloseOutlined style={{ fontSize: 11 }} />
          </button>
        </span>
      ))}
      <button type="button" className="type-add" aria-expanded={open} aria-haspopup="listbox" onClick={() => setOpen((current) => !current)}>
        <PlusOutlined style={{ fontSize: 12 }} />
        <span>{t('relation.typeAdd')}</span>
      </button>
      {open ? (
        <div className="type-picker" role="listbox" aria-label={t('relation.typeAdd')}>
          {addable.length === 0
            ? <p className="type-picker-empty">{t('relation.typeAddEmpty')}</p>
            : addable.map((entity) => (
              <button key={entity.id} type="button" role="option" aria-selected={false} className="type-picker-item" onClick={() => { commit([...extras, entity.id]); setOpen(false); }}>
                <span className="type-picker-name">{entity.displayName ?? entity.name}</span>
                <span className="type-picker-code">{entity.name}</span>
              </button>
            ))}
        </div>
      ) : null}
    </div>
  );
}
