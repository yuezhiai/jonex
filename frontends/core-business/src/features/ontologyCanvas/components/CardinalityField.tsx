import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import type { Cardinality } from '../domain/index.js';

/**
 * 关系基数选择器:基数本身就是一个**形状**(ER 图里画的就是箭头两端的 1/N),
 * 所以这里不摆五个近形词,而是四个图元磁贴 —— 读字不如看图。
 * 点选即设,再点一次清除(未设置 = TBox 不声明基数,与知识库侧缺省语义一致)。
 */
const SHAPES: { value: Cardinality; left: string; right: string }[] = [
  { value: 'one_to_one', left: '1', right: '1' },
  { value: 'one_to_many', left: '1', right: 'N' },
  { value: 'many_to_many', left: 'N', right: 'N' },
  { value: 'many_to_one', left: 'N', right: '1' },
];

export function CardinalityField({ value, onChange }: { value: Cardinality | undefined; onChange: (next: Cardinality | undefined) => void }): ReactNode {
  const { t } = useTranslation();
  const custom = value !== undefined && !SHAPES.some((shape) => shape.value === value);
  return (
    <div className="cardinality-grid" data-field="cardinality" role="radiogroup" aria-label={t('relation.cardinality')}>
      {SHAPES.map((shape) => {
        const active = value === shape.value;
        const label = t(`cardinality.${shape.value}`);
        return (
          <button
            key={shape.value}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={label}
            title={label}
            className={`cardinality-tile${active ? ' is-active' : ''}`}
            onClick={() => onChange(active ? undefined : shape.value)}
          >
            <span className="cardinality-glyph" aria-hidden="true"><b>{shape.left}</b><i>→</i><b>{shape.right}</b></span>
          </button>
        );
      })}
      {custom ? (
        <button type="button" role="radio" aria-checked={true} className="cardinality-tile is-active is-wide" title={t('cardinality.custom')} onClick={() => onChange(undefined)}>
          <span className="cardinality-glyph" aria-hidden="true"><b>{t('cardinality.custom')}</b></span>
        </button>
      ) : null}
    </div>
  );
}
