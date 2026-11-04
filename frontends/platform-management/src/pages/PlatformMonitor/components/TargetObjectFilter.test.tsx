// @vitest-environment jsdom
/**
 * 「目标对象」选择器的两条交互契约（监控调用记录页）：
 *  ① 选中能力后用选项 label 回显（「本体编译」），输入框不能露出原始能力 ID；
 *  ② 已选中时展开下拉仍列出全部候选 —— 用户不必先点清空才能改选。
 * 同时钉住下发值：选中 / 手输展示名都收敛回原始标识，自由输入原样下发。
 */
import { useState } from 'react';
import { fireEvent, render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import TargetObjectFilter from './TargetObjectFilter';
import type { PmSelectOption } from '../identifierOptions';

vi.stubGlobal(
  'ResizeObserver',
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
);

const OPTIONS: PmSelectOption[] = [
  { value: '', label: '全部对象' },
  { value: 'atomic.ontology_compile.v1', label: '本体编译' },
  { value: 'business.knowledge_base.v1', label: '知识库' },
  { value: 'search_ontology', label: 'search_ontology' },
];

/** 受控宿主：模拟页面用 ctx.set 回写筛选值后的重渲染 */
function Host({ initial, onChange }: { initial: string; onChange: (value: string) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <TargetObjectFilter
      value={value}
      options={OPTIONS}
      placeholder="全部对象"
      onChange={(next) => {
        onChange(next);
        setValue(next);
      }}
    />
  );
}

function renderFilter(value: string, onChange = vi.fn()) {
  const view = render(<Host initial={value} onChange={onChange} />);
  const input = view.container.querySelector('input') as HTMLInputElement;
  return { ...view, input, onChange };
}

/** 展开下拉并返回当前可见候选的文本 */
function openOptions(container: HTMLElement): string[] {
  fireEvent.mouseDown(container.querySelector('.ant-select-content') ?? container);
  return [...container.ownerDocument.querySelectorAll('.ant-select-item-option')].map((el) => el.textContent ?? '');
}

describe('目标对象选择器', () => {
  it('选中能力后回显选项 label，而不是原始能力 ID', () => {
    const { input } = renderFilter('atomic.ontology_compile.v1');

    expect(input.value).toBe('本体编译');
  });

  it('已选中时展开下拉仍列出全部候选（不必先清空）', () => {
    const { container } = renderFilter('atomic.ontology_compile.v1');

    expect(openOptions(container)).toEqual(['全部对象', '本体编译', '知识库', 'search_ontology']);
  });

  it('未选中时展开下拉同样列出全部候选', () => {
    const { container } = renderFilter('');

    expect(openOptions(container)).toEqual(['全部对象', '本体编译', '知识库', 'search_ontology']);
  });

  it('改选其它选项下发原始标识，并回显新选项 label', () => {
    const onChange = vi.fn();
    const { container, input } = renderFilter('atomic.ontology_compile.v1', onChange);

    openOptions(container);
    const target = [...container.ownerDocument.querySelectorAll('.ant-select-item-option')].find(
      (el) => el.textContent === '知识库',
    );
    fireEvent.click(target as Element);

    expect(onChange).toHaveBeenLastCalledWith('business.knowledge_base.v1');
    expect(input.value).toBe('知识库');
  });

  it('手输选项展示名收敛为原始标识，自由输入原样下发', () => {
    const onChange = vi.fn();
    const { input } = renderFilter('', onChange);

    fireEvent.change(input, { target: { value: '本体编译' } });
    expect(onChange).toHaveBeenLastCalledWith('atomic.ontology_compile.v1');

    fireEvent.change(input, { target: { value: 'custom_tool' } });
    expect(onChange).toHaveBeenLastCalledWith('custom_tool');
  });
});
