import { AutoComplete } from 'antd';
import type { PmSelectOption } from '../identifierOptions';
import { resolveTargetInput, targetDisplayText } from '../identifierOptions';

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
 * 两个容易踩反的地方：
 *  ① 回显用选项 label（「本体编译」），不是原始能力 ID（`atomic.ontology_compile.v1`）；
 *  ② 已选项的 label 同时是输入框文本 —— 若照它过滤，下拉只剩当前项，用户必须先点清空
 *    才能改选。文本仍等于当前展示值时视为「没在搜索」，列出全部候选。
 *
 * 下发给接口的始终是选项 value；用户手输模型名 / MCP 工具名时按自由输入原样下发。
 */
export default function TargetObjectFilter({ value, options, placeholder, onChange }: TargetObjectFilterProps) {
  const selectedText = targetDisplayText(options, value);

  return (
    <AutoComplete
      allowClear
      style={{ width: '100%' }}
      value={selectedText}
      options={[...options]}
      placeholder={placeholder}
      filterOption={(input, option) => {
        if (input.trim() === selectedText) return true;
        const needle = input.trim().toLowerCase();
        if (!needle) return true;
        return (
          String(option?.label ?? '')
            .toLowerCase()
            .includes(needle) ||
          String(option?.value ?? '')
            .toLowerCase()
            .includes(needle)
        );
      }}
      // 选中给 option.value；手输选项展示名也收敛回原始标识，其余按自由输入原样下发
      onChange={(text) => onChange(resolveTargetInput(options, text ?? ''))}
    />
  );
}
