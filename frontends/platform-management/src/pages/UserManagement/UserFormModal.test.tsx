// @vitest-environment jsdom
/**
 * 新建用户弹窗 · 字段格式校验。
 *
 * 只测格式规则是否真的挂在表单上：输入非法字符点保存，必须出现对应文案且不发请求。
 * 租户 / 角色是必填，这里不去操作下拉，因此错误列表里同时会有它们的必填提示，忽略即可。
 */
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { UserEvent } from '@testing-library/user-event';
import React from 'react';

import UserFormModal, { type UserFormModalHandle } from './UserFormModal';
import { createUser } from '../../api/users';

vi.mock('../../api/users', () => ({
  createUser: vi.fn().mockResolvedValue(undefined),
  updateUser: vi.fn().mockResolvedValue(undefined),
  getUserRoles: vi.fn().mockResolvedValue([]),
  setUserRoles: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../../api/roles', () => ({
  listRoles: vi.fn().mockResolvedValue({ items: [], total: 0 }),
}));

vi.mock('@jonex/shell-sdk', () => ({
  readCachedUser: () => null,
  isPlatformAdmin: () => true,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

async function openCreateModal(): Promise<UserEvent> {
  const ref = React.createRef<UserFormModalHandle>();
  render(
    <UserFormModal
      ref={ref}
      tenants={[{ id: 'tenant_a', name: 'Tenant A', userCount: 0 } as never]}
      onSaved={async () => {}}
    />,
  );
  act(() => ref.current!.open());
  return userEvent.setup();
}

async function submit(user: UserEvent) {
  await user.click(screen.getByRole('button', { name: 'common.save' }));
}

describe('UserFormModal 新建用户校验', () => {
  it('用户名只允许字母、数字、点、下划线', async () => {
    const user = await openCreateModal();
    await user.type(screen.getByLabelText('userManagement.username'), 'john-doe');

    await submit(user);

    expect(await screen.findByText('userManagement.invalidUsername')).toBeTruthy();
    await waitFor(() => expect(createUser).not.toHaveBeenCalled());
  });

  it('密码只允许字母、数字、点、下划线', async () => {
    const user = await openCreateModal();
    await user.type(screen.getByLabelText('userManagement.username'), 'john.doe_1');
    await user.type(screen.getByLabelText('auth.password'), 'Passw0rd!');

    await submit(user);

    expect(await screen.findByText('userManagement.invalidPassword')).toBeTruthy();
    await waitFor(() => expect(createUser).not.toHaveBeenCalled());
  });

  it('邮箱必须是合法格式', async () => {
    const user = await openCreateModal();
    await user.type(screen.getByLabelText('userManagement.email'), 'not-an-email');

    await submit(user);

    expect(await screen.findByText('userManagement.invalidEmail')).toBeTruthy();
    await waitFor(() => expect(createUser).not.toHaveBeenCalled());
  });
});
