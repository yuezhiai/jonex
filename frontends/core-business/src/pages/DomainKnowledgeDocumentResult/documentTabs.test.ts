import { describe, expect, it } from 'vitest';
import { buildDocumentStageKeys } from './documentTabs';

describe('buildDocumentStageKeys', () => {
  it('本版本只返回解析与编译两个 Tab', () => {
    expect(buildDocumentStageKeys()).toEqual(['parse', 'compile']);
  });
});
