// @vitest-environment jsdom
/**
 * 「目标对象」选择器的交互契约（监控调用记录页）：
 *  ① 选中能力后用选项 label 回显（「本体编译」），输入框不能露出原始能力 ID；
 *  ② 已选中时展开下拉仍列出全部候选 —— 用户不必先点清空才能改选；
 *  ③ 输入只改输入框文本，提交（Enter / 失焦 / 选中 / 清空）才把筛选值交给页面；
 *  ④ 输入文本里的空格属于对象名本身（如模型名），不能被吞；
 *  ⑤ 候选按 label / value 包含匹配，大小写不敏感；
 *  ⑥ 鼠标悬停候选后按 Enter，提交的是输入框文本，而不是悬停的那一项。
 *
 * 这些行为都建立在 antd v6 combobox 的一个事实上：输入框显示值就是 `value` prop，
 * 所以「输入文本」与「已提交筛选值」必须是两份状态，不能靠 onChange 逐字回灌。
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

/** 模拟逐字输入（antd combobox 的输入框走原生 onChange → onSearch） */
function typeText(input: HTMLInputElement, value: string) {
  fireEvent.change(input, { target: { value } });
}

/** 当前已渲染的候选文本（下拉没开就是空数组） */
function visibleOptions(container: HTMLElement): string[] {
  return [...container.ownerDocument.querySelectorAll('.ant-select-item-option')].map((el) => el.textContent ?? '');
}

/** 展开下拉并返回当前可见候选的文本 */
function openOptions(container: HTMLElement): string[] {
  fireEvent.mouseDown(container.querySelector('.ant-select-content') ?? container);
  return visibleOptions(container);
}

function findOption(container: HTMLElement, text: string): Element {
  const target = [...container.ownerDocument.querySelectorAll('.ant-select-item-option')].find(
    (el) => el.textContent === text,
  );
  if (!target) throw new Error(`候选「${text}」不存在`);
  return target;
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
    fireEvent.click(findOption(container, '知识库'));

    expect(onChange).toHaveBeenLastCalledWith('business.knowledge_base.v1');
    expect(input.value).toBe('知识库');
  });

  it('输入文本里的空格原样保留，按 Enter 才提交', () => {
    const onChange = vi.fn();
    const { input } = renderFilter('', onChange);

    typeText(input, 'deepseek v4');
    expect(input.value).toBe('deepseek v4');
    // 打字阶段不触发筛选请求
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onChange).toHaveBeenLastCalledWith('deepseek v4');
  });

  it('输入部分文字只保留包含匹配的候选（label / value 双字段）', () => {
    const { container, input } = renderFilter('');

    typeText(input, 'v1');

    // 不必先点开：打字本身就要把下拉拉起来
    expect(visibleOptions(container)).toEqual(['本体编译', '知识库']);
    expect(openOptions(container)).toEqual(['本体编译', '知识库']);
  });

  it('候选匹配大小写不敏感', () => {
    const { container, input } = renderFilter('');

    typeText(input, 'KNOWLEDGE_BASE');

    expect(openOptions(container)).toEqual(['知识库']);
  });

  it('输入大小写不同的完整值，按 Enter 收敛为原始标识并回显 label', () => {
    const onChange = vi.fn();
    const { input } = renderFilter('', onChange);

    typeText(input, 'BUSINESS.KNOWLEDGE_BASE.V1');
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(onChange).toHaveBeenLastCalledWith('business.knowledge_base.v1');
    expect(input.value).toBe('知识库');
  });

  it('鼠标悬停候选项时按 Enter，提交输入文本而不是悬停项', () => {
    const onChange = vi.fn();
    const { container, input } = renderFilter('', onChange);

    openOptions(container);
    typeText(input, 'v1');
    const hovered = findOption(container, '本体编译');
    fireEvent.mouseMove(hovered);
    // 悬停确实把该项设成了 active —— 不然这个用例证明不了什么
    expect(hovered.className).toContain('ant-select-item-option-active');

    fireEvent.keyDown(input, { key: 'Enter' });

    expect(onChange).toHaveBeenLastCalledWith('v1');
    expect(onChange).not.toHaveBeenCalledWith('atomic.ontology_compile.v1');
  });

  it('输入法组合态（keyCode 229）的 Enter 不提交', () => {
    const onChange = vi.fn();
    const { input } = renderFilter('', onChange);

    typeText(input, '文档');
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 });

    expect(onChange).not.toHaveBeenCalled();
  });

  it('失焦即提交输入内容', () => {
    const onChange = vi.fn();
    const { input } = renderFilter('', onChange);

    typeText(input, '知识库');
    fireEvent.blur(input);

    expect(onChange).toHaveBeenLastCalledWith('business.knowledge_base.v1');
  });

  it('外部改了已选值时，输入框跟着换成新的展示名', () => {
    const view = render(<TargetObjectFilter value="" options={OPTIONS} placeholder="全部对象" onChange={vi.fn()} />);
    const input = view.container.querySelector('input') as HTMLInputElement;
    expect(input.value).toBe('');

    view.rerender(
      <TargetObjectFilter
        value="business.knowledge_base.v1"
        options={OPTIONS}
        placeholder="全部对象"
        onChange={vi.fn()}
      />,
    );

    expect(input.value).toBe('知识库');
  });
});
