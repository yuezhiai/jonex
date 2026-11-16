import { useCallback, useEffect, useRef, useState } from 'react';
import { AutoComplete } from 'antd';
import type { PmSelectOption } from '../identifierOptions';
import {
  matchesTargetQuery,
  normalizeTargetText,
  resolveTargetInput,
  targetDisplayText,
} from '../identifierOptions';

export interface TargetObjectFilterProps {
  /** 当前筛选项（原始标识，接口认的值）；空串 = 全部对象 */
  value: string;
  options: readonly PmSelectOption[];
  placeholder: string;
  onChange: (value: string) => void;
}

/**
 * 调用记录的「目标对象」选择器。
 *
 * 有三处容易踩反的地方：
 *
 * ① antd v6 的 combobox 模式下，**输入框显示值就是 `value` prop**（不是 `searchValue`）。
 *    所以「用户正在敲的文本」和「已提交给页面的筛选值」必须是两份状态：
 *    `text` 只驱动输入框，`commit()` 才把文本收敛成原始标识交给页面。若照旧逐字回灌，
 *    `resolveTargetInput` 的 trim 会吞掉尾空格，选中态还会让下方的过滤判据恒真。
 * ② 回显用选项 label（「本体编译」），不是原始能力 ID（`atomic.ontology_compile.v1`）；
 *    已选项的 label 同时是输入框文本 —— 文本等于当前已选展示值时视为「没在搜索」，
 *    列出全部候选，用户不必先点清空才能改选。
 * ③ 提交时机是 Enter / 失焦 / 选中候选 / 清空；打字过程只过滤候选、不触发查询。
 *    Enter 在包装层**捕获**阶段拦下：下拉里的候选会被鼠标悬停设成 active，
 *    rc-select 默认就让 Enter 把 active 项选走，与「Enter 提交我输入的内容」相悖。
 *
 * 下发给接口的始终是选项 value；用户手输模型名 / MCP 工具名时按自由输入原样下发。
 */
export default function TargetObjectFilter({ value, options, placeholder, onChange }: TargetObjectFilterProps) {
  /** 已提交筛选值的展示文本（父组件 value 的「人话」形态） */
  const committedText = targetDisplayText(options, value);
  const [text, setText] = useState(committedText);
  const [open, setOpen] = useState(false);

  // keydown / blur 可能拿到旧闭包，用 ref 读最新值
  const textRef = useRef(text);
  textRef.current = text;
  /** 已交给页面的筛选值；只在父组件真的换了值时才回写，避免重复提交触发多余查询 */
  const valueRef = useRef(value);
  useEffect(() => {
    valueRef.current = value;
  }, [value]);
  /** 上一次同步给输入框的展示文本；只认「父组件真的换了值」，不覆盖用户正在敲的内容 */
  const committedRef = useRef(committedText);

  // 外部改值（URL 回放 / 重置 / 候选项晚到把原始标识翻译成人话）→ 输入框跟着换
  useEffect(() => {
    if (committedRef.current === committedText) return;
    committedRef.current = committedText;
    setText(committedText);
  }, [committedText]);

  const commit = useCallback(
    (raw: string) => {
      const next = resolveTargetInput(options, raw);
      const nextText = targetDisplayText(options, next);
      committedRef.current = nextText;
      setText(nextText);
      if (next !== valueRef.current) {
        // 提前记下：同一次交互里的重复提交（Enter 后又失焦）不必再写一遍页面 state
        valueRef.current = next;
        onChange(next);
      }
    },
    [options, onChange],
  );

  /** 文本与已选展示文本一致 ⇒ 用户没在搜索，展开就列全部候选 */
  const searching = normalizeTargetText(text) !== normalizeTargetText(committedText);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Enter') return;
    // 输入法选字用的是 Enter，keyCode 229 是部分浏览器的组合态标记
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    // 拦在 rc-select 之前：否则 Enter 会选中下拉里被悬停（active）的那一项
    event.preventDefault();
    event.stopPropagation();
    commit(textRef.current);
    setOpen(false);
  };

  const handleBlur = (event: React.FocusEvent<HTMLDivElement>) => {
    // 焦点还在控件内部（输入框 ↔ 包装层）时不算失焦；点候选项也不会走到这里
    if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
    commit(textRef.current);
  };

  return (
    // 包装层只为抢在 rc-select 之前截获 Enter（捕获阶段）与失焦，不承担交互语义
    <div style={{ width: '100%' }} onKeyDownCapture={handleKeyDown} onBlur={handleBlur}>
      <AutoComplete
        allowClear
        style={{ width: '100%' }}
        value={text}
        options={[...options]}
        placeholder={placeholder}
        open={open}
        onOpenChange={setOpen}
        // 逐字输入只更新文本，不提交（提交时机见组件注释 ③）
        onSearch={(next) => setText(next ?? '')}
        // 点选候选 / 清空才是确定的提交，直接拿选项原始值
        onSelect={(next) => commit(String(next ?? ''))}
        onClear={() => commit('')}
        filterOption={(input, option) =>
          !searching ||
          matchesTargetQuery({ value: String(option?.value ?? ''), label: String(option?.label ?? '') }, input)
        }
      />
    </div>
  );
}
