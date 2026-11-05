// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import React from 'react';
import VersionList, { canRollbackTo } from './VersionList';
import type { SemanticVersion } from '../../../api/evolution';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));
vi.mock('copy-to-clipboard', () => ({ default: () => true }));

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false, media: query, onchange: null,
      addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});
afterEach(cleanup);

const version = (id: string, status: string): SemanticVersion => ({
  version_id: id,
  scope: 'abstract',
  status,
});

describe('canRollbackTo', () => {
  it('只有 adopted run + active/superseded 版本可回滚', () => {
    expect(canRollbackTo('adopted', version('v1', 'active'))).toBe(true);
    expect(canRollbackTo('adopted', version('v2', 'superseded'))).toBe(true);
    expect(canRollbackTo('adopted', version('v3', 'candidate'))).toBe(false);
    expect(canRollbackTo('evaluated', version('v1', 'active'))).toBe(false);
    expect(canRollbackTo(undefined, version('v1', 'active'))).toBe(false);
  });
});

describe('VersionList 回滚按钮', () => {
  const items = [
    version('v-active', 'active'),
    version('v-super', 'superseded'),
    version('v-cand', 'candidate'),
  ];

  it('adopted run：仅 active/superseded 可点', () => {
    render(<VersionList items={items} runStatus="adopted" onRollback={() => {}} />);

    const buttons = screen.getAllByRole('button', { name: 'evolutionConsole.versionRollback' });
    expect(buttons).toHaveLength(3);
    expect((buttons[0] as HTMLButtonElement).disabled).toBe(false);
    expect((buttons[1] as HTMLButtonElement).disabled).toBe(false);
    expect((buttons[2] as HTMLButtonElement).disabled).toBe(true);
  });

  it('非 adopted run：全部禁用', () => {
    render(<VersionList items={items} runStatus="evaluated" onRollback={() => {}} />);

    const buttons = screen.getAllByRole('button', { name: 'evolutionConsole.versionRollback' });
    expect(buttons.every((b) => (b as HTMLButtonElement).disabled)).toBe(true);
  });
});
