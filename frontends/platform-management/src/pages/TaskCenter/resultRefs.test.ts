import { describe, expect, it } from 'vitest';
import type { SubtaskRef } from '../../api/taskCenter';
import { artifactPath, describeRef, inputFileName } from './resultRefs';

const KB = 'kb-1';
const DOC = 'doc-1';

describe('artifactPath —— kind 到 core-business 页面的映射', () => {
  it('切片跳到文档解析结果页', () => {
    expect(artifactPath('knowledge_base.chunks', { knowledge_base_id: KB, document_id: DOC })).toBe(
      `/apps/core-business/domain-knowledge/${KB}/documents/${DOC}/result`,
    );
  });

  it('Wiki 与切片同一落点（编译结果都在解析结果页里）', () => {
    expect(artifactPath('knowledge_base.wiki', { knowledge_base_id: KB, document_id: DOC })).toBe(
      `/apps/core-business/domain-knowledge/${KB}/documents/${DOC}/result`,
    );
  });

  it('本体跳到库的知识图谱页，不需要 document_id', () => {
    expect(artifactPath('knowledge_base.ontology', { knowledge_base_id: KB })).toBe(
      `/apps/core-business/domain-knowledge/${KB}/graph`,
    );
  });

  it('未知运行方的 kind 不给落点', () => {
    expect(artifactPath('other_runner.chunks', { knowledge_base_id: KB, document_id: DOC })).toBeNull();
  });

  it('缺库 id 一律不给落点（拼出来只会是死链）', () => {
    expect(artifactPath('knowledge_base.chunks', { document_id: DOC })).toBeNull();
  });

  it('切片 / Wiki 缺 document_id 时退化成无落点，不拼半截 URL', () => {
    expect(artifactPath('knowledge_base.chunks', { knowledge_base_id: KB })).toBeNull();
    expect(artifactPath('knowledge_base.wiki', { knowledge_base_id: KB })).toBeNull();
  });

  it('id 里的特殊字符被转义', () => {
    expect(artifactPath('knowledge_base.ontology', { knowledge_base_id: 'kb/1 2' })).toBe(
      '/apps/core-business/domain-knowledge/kb%2F1%202/graph',
    );
  });
});

describe('describeRef —— 引用整理成页面形状', () => {
  it('输入侧文档引用：有中文名、没有产物标题', () => {
    const view = describeRef(
      {
        kind: 'knowledge_base.document',
        params: { knowledge_base_id: KB, document_id: DOC, document_name: '合规手册.pdf' },
      },
      'input',
    );
    expect(view.labelKey).toBe('taskCenter.execution.kind.document');
    expect(view.params.document_name).toBe('合规手册.pdf');
    expect(view.artifact).toBeNull();
    expect(view.path).toBeNull();
  });

  it('输出侧切片引用带产物标题与查看动作', () => {
    const view = describeRef({
      kind: 'knowledge_base.chunks',
      params: { knowledge_base_id: KB, document_id: DOC },
    });
    expect(view.artifact).toEqual({
      title: 'taskCenter.execution.artifact.chunks',
      action: 'taskCenter.execution.viewChunks',
    });
    expect(view.path).toBe(`/apps/core-business/domain-knowledge/${KB}/documents/${DOC}/result`);
  });

  it('未知 kind 原样保留，不假装认识它', () => {
    const view = describeRef({ kind: 'other_runner.thing', params: { x: 'y' } });
    expect(view.labelKey).toBeNull();
    expect(view.artifact).toBeNull();
    expect(view.path).toBeNull();
  });

  it('非标量参数被丢掉，不会影响落点判断', () => {
    const view = describeRef({
      kind: 'knowledge_base.chunks',
      params: { knowledge_base_id: KB, document_id: [DOC], n: 42 },
    });
    expect(view.params).toEqual({ knowledge_base_id: KB, n: '42' });
    expect(view.path).toBeNull();
  });

  it('缺 params 时不炸', () => {
    // 协议上 params 必填，但老报文 / 手填数据不保证 —— 这条守的是「别把页面炸了」
    const view = describeRef({ kind: 'knowledge_base.document' } as SubtaskRef);
    expect(view.params).toEqual({});
    expect(inputFileName(view)).toBeNull();
  });
});

describe('inputFileName', () => {
  it('优先取 document_name', () => {
    const view = describeRef(
      { kind: 'knowledge_base.document', params: { document_name: 'a.pdf', document_id: DOC } },
      'input',
    );
    expect(inputFileName(view)).toBe('a.pdf');
  });

  it('名字缺失时回落成 document_id（后端也是这么回的）', () => {
    const view = describeRef({ kind: 'knowledge_base.document', params: { document_id: DOC } }, 'input');
    expect(inputFileName(view)).toBe(DOC);
  });

  it('非文档引用不给文件名', () => {
    const view = describeRef({ kind: 'knowledge_base.chunks', params: { document_id: DOC } }, 'input');
    expect(inputFileName(view)).toBeNull();
  });
});
