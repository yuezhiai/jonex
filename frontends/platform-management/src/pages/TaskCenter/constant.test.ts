/**
 * 任务来源码表 —— 筛选与详情共用的那一份。
 *
 * 断言钉住三件事：码表与协议层那四个码逐字一致、未知码回落原码而不是漏出 i18n key、
 * 空值走「平台自建」。第三条尤其要钉：把未知码也当「平台自建」是最容易写错的一版实现，
 * 它会让「后端加了新运行方」这件事在界面上彻底看不出来。
 */
import { describe, expect, it } from 'vitest';
import en from '../../locales/en.json';
import zh from '../../locales/zh.json';
import {
  SUBTASK_STATUS_COLORS,
  SUBTASK_STATUS_DOT_COLORS,
  SUBTASK_STATUS_LABEL_KEY,
  TASK_SOURCE_LABEL_KEY,
  TASK_SOURCE_VALUES,
  TASK_STATUS_COLORS,
  TASK_STATUS_LABEL_KEY,
  TASK_STATUS_ORDER,
  isSubtaskTerminal,
  isTaskTerminal,
  subtaskAction,
  taskSourceLabel,
} from '../../constants/taskCenter';

/** i18next 的替身：找不到 key 时它就是把 key 原样回给你，这里照搬这个行为 */
const t = (key: string) => key;

describe('TASK_SOURCE_VALUES', () => {
  it('正好是协议层那四个码，顺序也钉死', () => {
    expect(TASK_SOURCE_VALUES).toEqual([
      'knowledge_base.file.manual_upload',
      'knowledge_base.file.api_pull',
      'knowledge_base.file.api_push',
      'knowledge_base.file.storage_upload',
    ]);
  });
});

describe('TASK_SOURCE_LABEL_KEY', () => {
  it('四个码都有对应文案 key，没有多余的项', () => {
    expect(Object.keys(TASK_SOURCE_LABEL_KEY).sort()).toEqual([...TASK_SOURCE_VALUES].sort());
  });
});

describe('taskSourceLabel', () => {
  it('已知码映射到数据源接入页那套文案', () => {
    expect(taskSourceLabel(t, 'knowledge_base.file.manual_upload')).toBe('dataSource.fileUploadAccess');
    expect(taskSourceLabel(t, 'knowledge_base.file.api_pull')).toBe('dataSource.apiAccess');
    expect(taskSourceLabel(t, 'knowledge_base.file.api_push')).toBe('dataSource.apiPushAccess');
    expect(taskSourceLabel(t, 'knowledge_base.file.storage_upload')).toBe('dataSource.storageAccess');
  });

  it('未知码回落原码本身，不拼 key', () => {
    // 后端将来加了别的运行方：界面该显示那个码，而不是漏出 taskCenter.source.xxx
    expect(taskSourceLabel(t, 'business_domain.file.manual_upload')).toBe('business_domain.file.manual_upload');
  });

  it('空值显示「平台自建」', () => {
    expect(taskSourceLabel(t, null)).toBe('taskCenter.source.platform');
  });
});

/**
 * 「已停止」状态的前端映射 —— 后端 2026-10-15 把它独立成终态（设计文档 §6.7），
 * 前端本轮补齐。断言钉住三件事：五张映射表都覆盖它（漏一张就有一种界面表现坏掉）、
 * 它算终态但**不等于失败**（Tag 色必须与 failed 分得开）、三处文案 key 齐全 ——
 * 最后一条最容易漏：少 `result.stopped` 时时间线上会漏出原始 i18n key。
 */
describe('「已停止」状态（stopped）', () => {
  it('任务状态五档齐全，stopped 排在末位', () => {
    expect(TASK_STATUS_ORDER).toEqual(['pending', 'running', 'completed', 'failed', 'stopped']);
  });

  it('四张映射表各覆盖 stopped', () => {
    expect(TASK_STATUS_LABEL_KEY.stopped).toBe('taskCenter.status.stopped');
    expect(SUBTASK_STATUS_LABEL_KEY.stopped).toBe('taskCenter.subtaskStatus.stopped');
    expect(SUBTASK_STATUS_COLORS.stopped).toBe('warning');
    expect(SUBTASK_STATUS_DOT_COLORS.stopped).toBe('gray');
  });

  it('Tag 色与 failed 分得开 —— 分不开的话用户点完停止看到的还是「失败」', () => {
    expect(TASK_STATUS_COLORS.stopped).toBe('warning');
    expect(TASK_STATUS_COLORS.failed).toBe('error');
  });

  it('Timeline 点色只用它认的那四个字面量，别的值会静默失效', () => {
    for (const color of Object.values(SUBTASK_STATUS_DOT_COLORS)) {
      expect(['gray', 'blue', 'green', 'red']).toContain(color);
    }
  });

  it('它是终态 —— 后端的重跑前置认它，前端不认就会把「重试」按钮藏掉', () => {
    expect(isTaskTerminal('stopped')).toBe(true);
    expect(isSubtaskTerminal('stopped')).toBe(true);
  });

  it('停了的那一步给「重试」；任务还在跑时什么都不给', () => {
    expect(subtaskAction('stopped', 'stopped')).toBe('retry');
    expect(subtaskAction('running', 'stopped')).toBe(null);
  });

  it('三处文案 key 在 zh / en 两份里都在', () => {
    for (const locale of [zh, en]) {
      expect(locale.taskCenter.status.stopped).toBeTruthy();
      expect(locale.taskCenter.subtaskStatus.stopped).toBeTruthy();
      expect(locale.taskCenter.result.stopped).toBeTruthy();
    }
  });
});
