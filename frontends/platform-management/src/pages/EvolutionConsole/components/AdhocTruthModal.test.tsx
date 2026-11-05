// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import AdhocTruthModal from './AdhocTruthModal';
import type { AdhocCase, AdhocTruth } from '../../../api/evolution';

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

const cases: AdhocCase[] = [{ case_id: 'c1', question: '问题一' }];

const submitButton = () =>
  screen.getByRole('button', { name: 'evolutionConsole.adhocSubmit' }) as HTMLButtonElement;

describe('AdhocTruthModal', () => {
  it('expect=answer 且 control 为空 → 提交禁用', () => {
    render(
      <AdhocTruthModal
        open cases={cases} value={{}} onCancel={() => {}} onSubmit={() => {}}
      />,
    );

    expect(submitButton().disabled).toBe(true);
  });

  it('expect=answer 填了 control → 提交可用', () => {
    render(
      <AdhocTruthModal
        open cases={cases} value={{}} onCancel={() => {}} onSubmit={() => {}}
      />,
    );

    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'SELECT 1' },
    });

    expect(submitButton().disabled).toBe(false);
  });

  it('expect=refuse 允许 control 为空', () => {
    const value: Record<string, AdhocTruth> = {
      c1: { case_id: 'c1', control: [], expect: 'refuse' },
    };
    render(
      <AdhocTruthModal
        open cases={cases} value={value} onCancel={() => {}} onSubmit={() => {}}
      />,
    );

    expect(submitButton().disabled).toBe(false);
  });
});
