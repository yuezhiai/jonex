/**
 * 用户密级配置（任务 7）取数模块 —— 纯映射 + 两个接口封装。
 *
 * 断言钉住三件事：
 *   ① snake_case → camelCase，`max_level_code=null` 原样保留（null ≠ 未设置默认）；
 *   ② `display_name` 为空/缺失时回落到 `username`，页面不会出现空白用户列；
 *   ③ 读/写路径与 body 口径：GET 列表取 `items`，PUT 带 `{max_level_code}` 且 userId 被 URL 编码。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import apiClient from './request';
import {
  listUserClearances,
  mapUserClearance,
  setUserClearance,
} from './dataGovernance';

vi.mock('./request', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}));

type UserClearanceRow = Parameters<typeof mapUserClearance>[0];

function makeRow(overrides: Partial<UserClearanceRow> = {}): UserClearanceRow {
  return {
    user_id: 'user-1',
    username: 'zhangsan',
    display_name: '张三',
    max_level_code: 'confidential',
    is_active: true,
    ...overrides,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe('mapUserClearance', () => {
  it('把后端 snake_case 映射为页面 camelCase', () => {
    expect(mapUserClearance(makeRow())).toEqual({
      userId: 'user-1',
      username: 'zhangsan',
      displayName: '张三',
      maxLevelCode: 'confidential',
      isActive: true,
    });
  });

  it('max_level_code 为 null 时原样保留（表示未设置，不是空串）', () => {
    const mapped = mapUserClearance(makeRow({ max_level_code: null }));

    expect(mapped.maxLevelCode).toBeNull();
  });

  it('display_name 为空时回落到 username', () => {
    expect(mapUserClearance(makeRow({ display_name: '' })).displayName).toBe(
      'zhangsan',
    );
  });
});

describe('listUserClearances', () => {
  it('GET 分类用户列表并从 items 映射', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      items: [makeRow(), makeRow({ user_id: 'user-2', max_level_code: null })],
    });

    const rows = await listUserClearances();

    expect(apiClient.get).toHaveBeenCalledWith('/data-governance/classification-users');
    expect(rows).toEqual([
      {
        userId: 'user-1',
        username: 'zhangsan',
        displayName: '张三',
        maxLevelCode: 'confidential',
        isActive: true,
      },
      {
        userId: 'user-2',
        username: 'zhangsan',
        displayName: '张三',
        maxLevelCode: null,
        isActive: true,
      },
    ]);
  });
});

describe('setUserClearance', () => {
  it('PUT 到对应用户路径并发送 {max_level_code}', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(undefined);

    await setUserClearance('user/1', 'internal');

    expect(apiClient.put).toHaveBeenCalledWith(
      '/data-governance/classification-users/user%2F1',
      { max_level_code: 'internal' },
    );
  });

  it('清除密级时显式发送 null', async () => {
    vi.mocked(apiClient.put).mockResolvedValue(undefined);

    await setUserClearance('user-1', null);

    expect(apiClient.put).toHaveBeenCalledWith(
      '/data-governance/classification-users/user-1',
      { max_level_code: null },
    );
  });
});
