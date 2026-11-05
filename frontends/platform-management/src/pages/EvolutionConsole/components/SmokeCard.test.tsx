// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import React from 'react';
import SmokeCard from './SmokeCard';
import type { SmokeResult } from '../../../api/evolution';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

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

const failedSmoke: SmokeResult = {
  mode: 'warn',
  status: 'failed',
  cases: [],
  reasons: [],
  duration_ms: 12,
};

describe('SmokeCard warn 模式', () => {
  it('run.smoke_status=warn 时显示黄色 warn Tag（即使 smoke.status=failed）', () => {
    const { container } = render(
      <SmokeCard smoke={failedSmoke} runSmokeStatus="warn" />,
    );

    const warningTag = container.querySelector('.ant-tag-warning');
    expect(warningTag).toBeTruthy();
    expect(warningTag?.textContent).toContain('evolutionConsole.smokeStatus.warn');
    expect(container.querySelector('.ant-tag-error')).toBeNull();
  });

  it('非 warn 模式：failed 仍显示红色 Tag', () => {
    const { container } = render(
      <SmokeCard smoke={failedSmoke} runSmokeStatus="failed" />,
    );

    expect(container.querySelector('.ant-tag-error')).toBeTruthy();
    expect(container.querySelector('.ant-tag-warning')).toBeNull();
  });
});
