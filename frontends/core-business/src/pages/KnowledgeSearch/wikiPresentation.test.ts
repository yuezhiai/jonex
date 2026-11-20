import { describe, expect, it } from 'vitest';

import { chooseWikiTraceView } from './wikiPresentation';

describe('chooseWikiTraceView', () => {
  it('混合检索（source=mixed）即使引用带 wiki query_id 也不用 WikiTrace', () => {
    expect(
      chooseWikiTraceView({
        source: 'mixed',
        hasWikiEvidence: true,
        wikiTraceStepCount: 0,
      }),
    ).toBe(false);
  });

  it('流式 wiki 会话始终用 WikiTrace', () => {
    expect(
      chooseWikiTraceView({
        wikiSession: 'session-1',
        hasWikiEvidence: false,
        wikiTraceStepCount: 0,
      }),
    ).toBe(true);
  });

  it('后端来源 jonex_wiki 用 WikiTrace', () => {
    expect(
      chooseWikiTraceView({
        source: 'jonex_wiki',
        hasWikiEvidence: true,
        wikiTraceStepCount: 0,
      }),
    ).toBe(true);
  });

  it('历史回放（无 source）纯 wiki 会话：有 wiki 证据且有落库事件才用 WikiTrace', () => {
    expect(
      chooseWikiTraceView({
        source: null,
        hasWikiEvidence: true,
        wikiTraceStepCount: 3,
      }),
    ).toBe(true);
    expect(
      chooseWikiTraceView({
        source: null,
        hasWikiEvidence: true,
        wikiTraceStepCount: 0,
      }),
    ).toBe(false);
  });

  it('普通本体/问答检索不走 WikiTrace', () => {
    expect(
      chooseWikiTraceView({
        source: 'ontology',
        hasWikiEvidence: false,
        wikiTraceStepCount: 0,
      }),
    ).toBe(false);
  });
});
