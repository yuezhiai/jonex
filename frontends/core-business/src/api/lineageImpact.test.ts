/**
 * `lineageImpact` 取数模块 —— 响应格式校验。
 *
 * 断言钉住：读边失败 / 响应缺失必须抛错，不能被伪装成「没有边」的空图或 0% 覆盖率；
 * 合法响应原样返回。删掉任一校验，对应用例会红。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getData } from './request';
import { getLineageCoverage, getLineageImpact } from './lineageImpact';

vi.mock('./request', () => ({
  request: { get: vi.fn() },
  getData: vi.fn(),
}));

const getDataMock = vi.mocked(getData);

beforeEach(() => {
  vi.resetAllMocks();
});

const IMPACT_QUERY = { table: 'dbo.orders' };

const IMPACT_OK = {
  anchor: { table: 'dbo.orders', column: 'id' },
  direction: 'downstream',
  hops: 3,
  levels: [] as unknown[],
  affected: { tables: [] as string[], columns: [] as string[] },
  total_edges: 0,
  truncated: false,
  unknown: false,
};

const COVERAGE_OK = {
  data_source_id: 'ds1',
  tables: { total: 2, covered: 1, ratio: 0.5 },
  columns: { total: 4, covered: 2, ratio: 0.5 },
  by_origin: { manual: 1 },
  orphan_edges: 0,
  total_edges: 1,
  per_table: [],
  truncated: false,
  unknown: false,
};

describe('getLineageImpact 响应校验', () => {
  it('getData 返回 undefined 时抛错，而不是返回空图', async () => {
    getDataMock.mockResolvedValue(undefined as never);
    await expect(getLineageImpact('kb1', 'ds1', IMPACT_QUERY))
      .rejects.toThrow('血缘影响分析响应格式不正确');
  });

  it('levels 不是数组时抛错', async () => {
    getDataMock.mockResolvedValue({ ...IMPACT_OK, levels: null } as never);
    await expect(getLineageImpact('kb1', 'ds1', IMPACT_QUERY))
      .rejects.toThrow('血缘影响分析响应格式不正确');
  });

  it('unknown 不是布尔时抛错', async () => {
    getDataMock.mockResolvedValue({ ...IMPACT_OK, unknown: 'false' } as never);
    await expect(getLineageImpact('kb1', 'ds1', IMPACT_QUERY))
      .rejects.toThrow('血缘影响分析响应格式不正确');
  });

  it('合法响应（levels=[] + unknown=false）原样返回', async () => {
    getDataMock.mockResolvedValue(IMPACT_OK as never);
    await expect(getLineageImpact('kb1', 'ds1', IMPACT_QUERY))
      .resolves.toEqual(IMPACT_OK);
  });
});

describe('getLineageCoverage 响应校验', () => {
  it('getData 返回 undefined 时抛错，而不是读成 0%', async () => {
    getDataMock.mockResolvedValue(undefined as never);
    await expect(getLineageCoverage('kb1', 'ds1'))
      .rejects.toThrow('血缘覆盖率响应格式不正确');
  });

  it('tables 缺失时抛错', async () => {
    const { tables: _tables, ...rest } = COVERAGE_OK;
    getDataMock.mockResolvedValue(rest as never);
    await expect(getLineageCoverage('kb1', 'ds1'))
      .rejects.toThrow('血缘覆盖率响应格式不正确');
  });

  it('columns 缺失时抛错', async () => {
    const { columns: _columns, ...rest } = COVERAGE_OK;
    getDataMock.mockResolvedValue(rest as never);
    await expect(getLineageCoverage('kb1', 'ds1'))
      .rejects.toThrow('血缘覆盖率响应格式不正确');
  });

  it('unknown 不是布尔时抛错', async () => {
    getDataMock.mockResolvedValue({ ...COVERAGE_OK, unknown: null } as never);
    await expect(getLineageCoverage('kb1', 'ds1'))
      .rejects.toThrow('血缘覆盖率响应格式不正确');
  });

  it('合法响应原样返回', async () => {
    getDataMock.mockResolvedValue(COVERAGE_OK as never);
    await expect(getLineageCoverage('kb1', 'ds1'))
      .resolves.toEqual(COVERAGE_OK);
  });
});
