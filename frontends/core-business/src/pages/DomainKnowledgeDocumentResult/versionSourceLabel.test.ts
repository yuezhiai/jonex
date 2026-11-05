import { describe, expect, it } from 'vitest';
import type { TFunction } from 'i18next';
import { versionSourceLabel } from './versionSourceLabel';

// t 桩：返回 key 本身，便于断言映射到了哪个文案键。
const t = ((key: string) => key) as TFunction;

describe('versionSourceLabel', () => {
  it('映射四种已知来源', () => {
    expect(versionSourceLabel('backfill', t)).toBe('domainKnowledge.versions.sourceBackfill');
    expect(versionSourceLabel('rollback', t)).toBe('domainKnowledge.versions.sourceRollback');
    expect(versionSourceLabel('merge', t)).toBe('domainKnowledge.versions.sourceMerge');
    expect(versionSourceLabel('upload', t)).toBe('domainKnowledge.versions.sourceUpload');
  });

  it('未知来源保留原值，空值回退为上传', () => {
    expect(versionSourceLabel('mystery', t)).toBe('mystery');
    expect(versionSourceLabel('', t)).toBe('domainKnowledge.versions.sourceUpload');
  });
});
