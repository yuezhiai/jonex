import { describe, expect, it } from 'vitest';
import type { DgDataContractDetail } from './types';
import {
  CONTRACT_STATUSES,
  formatContractContent,
  isConflictError,
  statusColor,
  statusLabelKey,
  summarizeScaffold,
  toDataContractItem,
  validateJsonContent,
} from './contractUtils';

describe('statusLabelKey / statusColor', () => {
  it('三种状态各自映射到 i18n 键与配色', () => {
    expect(CONTRACT_STATUSES).toEqual(['draft', 'published', 'retired']);
    expect(statusLabelKey('draft')).toBe(
      'dataGovernance.dataContracts.status.draft',
    );
    expect(statusLabelKey('published')).toBe(
      'dataGovernance.dataContracts.status.published',
    );
    expect(statusLabelKey('retired')).toBe(
      'dataGovernance.dataContracts.status.retired',
    );

    expect(statusColor('draft')).toBe('gold');
    expect(statusColor('published')).toBe('green');
    expect(statusColor('retired')).toBe('default');
  });

  it('未登记状态原样返回 / 回落 default（不抛错）', () => {
    expect(statusLabelKey('unknown')).toBe('unknown');
    expect(statusLabelKey('')).toBe('');
    expect(statusColor('unknown')).toBe('default');
  });
});

describe('toDataContractItem', () => {
  it('详情字段原样映射为列表行，payload 不透出', () => {
    const detail: DgDataContractDetail = {
      contractId: 'dc_1',
      dataSourceId: 'ds_1',
      dataSourceName: '财务库',
      name: '财务合同',
      version: 3,
      status: 'published',
      revision: 7,
      publishedVersion: 3,
      draftVersion: 4,
      hasDraft: true,
      updatedAt: '2026-10-19T08:00:00Z',
      updatedBy: 'user_1',
      payload: { apiVersion: 'v3.0.0' },
    };

    const item = toDataContractItem(detail);
    expect(item).toEqual({
      contractId: 'dc_1',
      dataSourceId: 'ds_1',
      dataSourceName: '财务库',
      name: '财务合同',
      version: 3,
      status: 'published',
      revision: 7,
      publishedVersion: 3,
      draftVersion: 4,
      hasDraft: true,
      updatedAt: '2026-10-19T08:00:00Z',
      updatedBy: 'user_1',
    });
    expect(item).not.toHaveProperty('payload');
  });

  it('可空字段为 null 时原样保留（不抛错、不补默认值）', () => {
    const detail: DgDataContractDetail = {
      contractId: 'dc_2',
      dataSourceId: 'ds_2',
      dataSourceName: null,
      name: '空合同',
      version: 0,
      status: 'draft',
      revision: 1,
      publishedVersion: null,
      draftVersion: null,
      hasDraft: false,
      updatedAt: null,
      updatedBy: null,
      payload: {},
    };

    expect(toDataContractItem(detail)).toEqual({
      contractId: 'dc_2',
      dataSourceId: 'ds_2',
      dataSourceName: null,
      name: '空合同',
      version: 0,
      status: 'draft',
      revision: 1,
      publishedVersion: null,
      draftVersion: null,
      hasDraft: false,
      updatedAt: null,
      updatedBy: null,
    });
  });
});

describe('isConflictError', () => {
  it('业务码 1003 或 HTTP 409 视为乐观锁冲突', () => {
    expect(isConflictError({ bizCode: 1003 })).toBe(true);
    expect(isConflictError({ status: 409 })).toBe(true);
    expect(isConflictError({ bizCode: 1003, status: 409 })).toBe(true);
  });

  it('其它错误码 / 非对象 / 字符串形态不算冲突', () => {
    expect(isConflictError({ bizCode: 1002 })).toBe(false);
    expect(isConflictError({ status: 500 })).toBe(false);
    expect(isConflictError(null)).toBe(false);
    expect(isConflictError('conflict')).toBe(false);
    expect(isConflictError({ status: '409' })).toBe(false);
  });
});

describe('formatContractContent', () => {
  it('JSON 两空格缩进序列化', () => {
    expect(
      formatContractContent(
        { apiVersion: 'v3.0.0', schema: [{ name: 'jobs' }] },
        'json',
      ),
    ).toBe(
      '{\n  "apiVersion": "v3.0.0",\n  "schema": [\n    {\n      "name": "jobs"\n    }\n  ]\n}',
    );
  });

  it('空 payload 序列化为 {}；YAML 前端不序列化（返回空串）', () => {
    expect(formatContractContent(null, 'json')).toBe('{}');
    expect(formatContractContent(undefined, 'json')).toBe('{}');
    expect(formatContractContent({ apiVersion: 'v3.0.0' }, 'yaml')).toBe('');
  });
});

describe('validateJsonContent', () => {
  it('合法对象通过', () => {
    expect(validateJsonContent('{"apiVersion":"v3.0.0"}')).toEqual({
      ok: true,
    });
    expect(validateJsonContent('  {}  ')).toEqual({ ok: true });
  });

  it('空文本 / 语法错误 / 非对象分别给出对应 i18n 错误键', () => {
    expect(validateJsonContent('')).toEqual({
      ok: false,
      error: 'dataGovernance.dataContracts.contentRequired',
    });
    expect(validateJsonContent('   ')).toEqual({
      ok: false,
      error: 'dataGovernance.dataContracts.contentRequired',
    });
    expect(validateJsonContent('{oops')).toEqual({
      ok: false,
      error: 'dataGovernance.dataContracts.contentInvalidJson',
    });
    expect(validateJsonContent('{"a":')).toEqual({
      ok: false,
      error: 'dataGovernance.dataContracts.contentInvalidJson',
    });
  });

  it('数组 / 标量 / null 不算合同对象', () => {
    for (const text of ['[]', '"x"', '42', 'null', 'true']) {
      expect(validateJsonContent(text)).toEqual({
        ok: false,
        error: 'dataGovernance.dataContracts.contentNotObject',
      });
    }
  });
});

describe('summarizeScaffold', () => {
  it('原样透传四个计数并给出 hasSkipped', () => {
    expect(
      summarizeScaffold({
        tables: 3,
        columns: 12,
        skippedTables: 1,
        skippedColumns: 2,
      }),
    ).toEqual({
      tables: 3,
      columns: 12,
      skippedTables: 1,
      skippedColumns: 2,
      hasSkipped: true,
    });
    expect(
      summarizeScaffold({
        tables: 3,
        columns: 12,
        skippedTables: 0,
        skippedColumns: 0,
      }),
    ).toEqual({
      tables: 3,
      columns: 12,
      skippedTables: 0,
      skippedColumns: 0,
      hasSkipped: false,
    });
  });

  it('缺失 / 非法计数归一为 0（负数、NaN、小数）', () => {
    expect(summarizeScaffold(undefined)).toEqual({
      tables: 0,
      columns: 0,
      skippedTables: 0,
      skippedColumns: 0,
      hasSkipped: false,
    });
    expect(
      summarizeScaffold({
        tables: -1,
        columns: Number.NaN,
        skippedTables: 2.9,
        skippedColumns: undefined,
      }),
    ).toEqual({
      tables: 0,
      columns: 0,
      skippedTables: 2,
      skippedColumns: 0,
      hasSkipped: true,
    });
  });
});
