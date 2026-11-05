// @vitest-environment jsdom
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import React from 'react';
import GateCard from './GateCard';
import type { GateDetail } from '../../../api/evolution';

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

const gate: GateDetail = {
  decision: 'accept',
  delta: 0,
  passes: [30, 30],
};

describe('GateCard', () => {
  it('渲染 effective / adhoc / override 三字段', () => {
    render(
      <GateCard
        gate={gate}
        gateEffective="accept"
        gateAdhoc="accept"
        override={{ used: true, by: 'u_admin', reason: '人工复核' }}
      />,
    );

    expect(screen.getByText('evolutionConsole.gateEffective')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.gateAdhoc')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.gateOverride')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.gateOverrideUsed')).toBeTruthy();
    expect(screen.getByText(/u_admin/)).toBeTruthy();
    // effective 与 adhoc 都是 accept
    expect(screen.getAllByText(/accept/).length).toBeGreaterThanOrEqual(2);
  });

  it('gate=null 不崩，仍展示信任闭环三字段', () => {
    render(<GateCard gate={null} gateEffective={null} gateAdhoc={null} override={null} />);

    expect(screen.getByText('evolutionConsole.gateNone')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.gateEffective')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.gateAdhoc')).toBeTruthy();
    expect(screen.getByText('evolutionConsole.gateOverride')).toBeTruthy();
  });
});
