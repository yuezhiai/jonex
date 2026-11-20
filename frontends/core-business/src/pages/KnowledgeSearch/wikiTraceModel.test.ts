import { describe, expect, it } from 'vitest';
import { runningStep, traceFromEvents } from './wikiTraceModel';

describe('real Agent progress', () => {
  it('pairs concurrent operations by identity and KB, including nonzero completion', () => {
    const steps = traceFromEvents([
      { kb_id: 'a', seq: 1, type: 'model', operation_id: 'model-1', status: 'running' },
      { kb_id: 'a', seq: 2, type: 'tool', tool: 'agent_read', operation_id: 'command-1', status: 'running', details: { resource_kind: 'table' } },
      { kb_id: 'b', seq: 1, type: 'tool', tool: 'agent_read', operation_id: 'command-1', status: 'running' },
      { kb_id: 'a', seq: 3, type: 'model', operation_id: 'model-1', status: 'done', duration_ms: 100 },
      { kb_id: 'a', seq: 4, type: 'tool', tool: 'agent_read', operation_id: 'command-1', status: 'nonzero', duration_ms: 15 },
      { kb_id: 'b', seq: 2, type: 'tool', tool: 'agent_read', operation_id: 'command-1', status: 'done', duration_ms: 8 },
    ]);
    expect(steps).toHaveLength(3);
    expect(steps.every((step) => step.done)).toBe(true);
    expect(steps.find((step) => step.kbId === 'a' && step.tool === 'agent_read')).toMatchObject({
      lifecycle: 'nonzero', resourceKind: 'table', durationMs: 15,
    });
    expect(runningStep(steps)).toBeUndefined();
  });

  it('does not invent duration or reinterpret unknown commands as reads', () => {
    const steps = traceFromEvents([
      { seq: 3, type: 'tool', tool: 'agent_command', operation_id: 'command-3', status: 'ended' },
      { seq: 4, type: 'tool', tool: 'agent_search', title: '文件检索命令已结束' },
    ]);
    expect(steps[0]).toMatchObject({ kind: 'command', done: true, lifecycle: 'ended' });
    expect(steps[0].durationMs).toBeUndefined();
    expect(steps[1].done).toBe(true);
  });

  it('merges actual bounded tool input and output across live lifecycle events', () => {
    const steps = traceFromEvents([
      { seq: 1, type: 'tool', tool: 'agent_read', operation_id: 'c', status: 'running', details: { input: 'cat sources/a.md' } },
      { seq: 2, type: 'tool', tool: 'agent_read', operation_id: 'c', status: 'done', details: { output: '2024: 1080 MWh' } },
    ]);
    expect(steps).toHaveLength(1);
    expect(steps[0]).toMatchObject({ input: 'cat sources/a.md', output: '2024: 1080 MWh', done: true });
  });
});
