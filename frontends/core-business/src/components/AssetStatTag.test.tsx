// @vitest-environment jsdom
/**
 * 页头统计标签：number / string 两种数值形态，以及 lightrag 库在 Neo4j 降级时
 * 本体实例 / 关系实例显示「—」而不是被后端置 0 的计数。
 */
import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import AssetStatTag, { buildAssetTags } from './AssetStatTag';
import type { DomainKnowledgeDetail } from '@/types/domainKnowledge';

// t 桩：直接返回 key，断言不依赖中文文案
const t = (key: string) => key;

const DETAIL: DomainKnowledgeDetail = {
  id: 'kb-1',
  name: '测试库',
  spaceId: 'space-1',
  spaceName: '空间',
  documentCount: 5,
  entityCount: 7,
  relationCount: 3,
  compileVersionCount: 0,
  status: 'synced',
  updatedAt: '—',
  kbType: 'lightrag',
  can_manage_permissions: false,
  can_write: false,
  structuredEnabled: true,
  similarityCheckEnabled: false,
  similarityCheckThreshold: null,
};

function renderTags(detail: DomainKnowledgeDetail) {
  const tags = buildAssetTags(t, detail, null);
  return render(
    <>
      {tags.map((tag) => (
        <AssetStatTag key={tag.label} {...tag} />
      ))}
    </>,
  );
}

afterEach(() => {
  cleanup();
});

describe('buildAssetTags', () => {
  it('lightrag 正常时输出真实计数', () => {
    const tags = buildAssetTags(t, DETAIL, null);
    expect(tags.map((tag) => tag.value)).toEqual([5, 7, 3]);
  });

  it('lightrag 降级时本体实例 / 关系实例传 —，文档总数不变', () => {
    const tags = buildAssetTags(t, { ...DETAIL, ontologyDegraded: true }, null);
    expect(tags.map((tag) => tag.value)).toEqual([5, '—', '—']);
  });
});

describe('AssetStatTag', () => {
  it('number 走千分位，string 原样输出', () => {
    render(
      <>
        <AssetStatTag label="count" value={1234} icon={null} />
        <AssetStatTag label="degraded" value="—" icon={null} />
      </>,
    );
    expect(screen.getByText('1,234')).not.toBeNull();
    expect(screen.getByText('—')).not.toBeNull();
  });

  it('lightrag 降级时页头两个标签显示 —，不出现 0', () => {
    renderTags({ ...DETAIL, ontologyDegraded: true });

    expect(screen.getAllByText('—')).toHaveLength(2);
    // 文档总数（来自 PG）不受影响
    expect(screen.getByText('5')).not.toBeNull();
    // 降级值 0 不得作为计数渲染
    expect(screen.queryByText('0')).toBeNull();
  });
});
