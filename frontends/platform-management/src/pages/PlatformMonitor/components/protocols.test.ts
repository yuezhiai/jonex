/**
 * 调用监控协议下拉候选白名单。
 *
 * 两个页签（开放能力 / 调用记录）的协议下拉都有两层候选：
 *  1. 静态白名单 —— M1 元数据拿不到时的兜底；
 *  2. M1 对象清单 —— 后端放行 mcp 后自动出现。
 * 这里钉住第 1 层：MCP 调用必须能在下拉里选到，且既有 invoke / openai 不回归。
 *
 * 下拉文案走 `platformMonitor.callProtocol.<value>`；白名单里的每个取值都必须在
 * zh/en 两套语言里有对应键，否则下拉会出现裸 key。
 */
import { describe, expect, it } from 'vitest';
import platformZh from '../../../locales/zh.json';
import platformEn from '../../../locales/en.json';
import { PROTOCOLS as SCOPE_PROTOCOLS } from './CallScopeView';
import { PROTOCOLS as RECORD_PROTOCOLS } from '../CallRecords';

const PROTOCOL_WHITELISTS: [string, readonly string[]][] = [
  ['开放能力协议下拉兜底', SCOPE_PROTOCOLS],
  ['调用记录协议下拉', RECORD_PROTOCOLS],
];

function resolveKey(node: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, part) => {
    if (acc === null || typeof acc !== 'object' || Array.isArray(acc)) return undefined;
    return (acc as Record<string, unknown>)[part];
  }, node);
}

describe.each(PROTOCOL_WHITELISTS)('%s', (_label, protocols) => {
  it('包含 mcp，且既有 invoke / openai 不回归', () => {
    expect(protocols).toContain('mcp');
    expect(protocols).toContain('invoke');
    expect(protocols).toContain('openai');
    // 顺序即下拉顺序：invoke 在最前保持不变，mcp 紧随其后，openai 仍在末位
    expect(protocols).toEqual(['invoke', 'mcp', 'openai']);
  });

  it('每个协议在 zh / en 都有下拉文案', () => {
    for (const locale of [platformZh, platformEn]) {
      for (const protocol of protocols) {
        const label = resolveKey(locale, `platformMonitor.callProtocol.${protocol}`);
        expect(typeof label).toBe('string');
        expect(label).not.toBe('');
      }
    }
  });
});
