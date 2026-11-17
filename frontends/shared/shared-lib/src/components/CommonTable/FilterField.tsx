import { useEffect, useRef, useState } from 'react';
import { Input, Select } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import type { FilterConfig, FilterContext } from './types';

const DEFAULT_DEBOUNCE = 300;
/** 栅格已经定好每项的宽度，控件默认铺满所在的 Col；config.width 仅在需要固定宽度时才传 */
const DEFAULT_WIDTH = '100%';

interface FilterFieldProps<F extends Record<string, unknown>> {
  config: FilterConfig;
  ctx: FilterContext<F>;
}

/** 按配置渲染单个筛选控件。select 直接受控于 ctx；input 走内部防抖，见 DebouncedInput。 */
export default function FilterField<F extends Record<string, unknown>>({
  config,
  ctx,
}: FilterFieldProps<F>) {
  if (config.type === 'select') {
    return (
      <Select
        mode={config.mode}
        maxTagCount={config.maxTagCount}
        allowClear
        placeholder={config.placeholder}
        style={{ width: config.width ?? DEFAULT_WIDTH }}
        value={ctx.get(config.key as keyof F) as string | string[] | undefined}
        options={config.options}
        onChange={(value) => ctx.set(config.key as keyof F, value as F[keyof F])}
      />
    );
  }
  return <DebouncedInput config={config} ctx={ctx} />;
}

/**
 * input 类型需要「本地显示值」与「已生效查询值」分离：输入过程中不能每次按键都发请求，
 * 但又必须让用户看到自己刚敲的字。这与存量页面输入值 / keyword 两个 state 的做法同构，
 * 只是不必每页重写。
 */
function DebouncedInput<F extends Record<string, unknown>>({ config, ctx }: FilterFieldProps<F>) {
  const applied = ctx.get(config.key as keyof F) as string | undefined;
  const [text, setText] = useState(applied ?? '');
  const timerRef = useRef<ReturnType<typeof setTimeout>>();

  // 查询值被外部改变时（防抖落地、或将来接入「清空筛选」）把输入框拉回一致
  useEffect(() => {
    setText(applied ?? '');
  }, [applied]);

  useEffect(() => () => clearTimeout(timerRef.current), []);

  const handleChange = (next: string) => {
    setText(next);
    const delay = config.type === 'input' ? (config.debounce ?? DEFAULT_DEBOUNCE) : DEFAULT_DEBOUNCE;
    clearTimeout(timerRef.current);
    if (delay <= 0) {
      ctx.set(config.key as keyof F, next as F[keyof F]);
      return;
    }
    timerRef.current = setTimeout(() => {
      ctx.set(config.key as keyof F, next as F[keyof F]);
    }, delay);
  };

  return (
    <Input
      allowClear
      // 存量 17 个列表页的筛选输入框清一色是关键词搜索，统一带图标
      prefix={<SearchOutlined style={{ color: 'var(--yx-text-muted)' }} />}
      placeholder={config.placeholder}
      style={{ width: config.width ?? DEFAULT_WIDTH }}
      value={text}
      onChange={(e) => handleChange(e.target.value)}
    />
  );
}
