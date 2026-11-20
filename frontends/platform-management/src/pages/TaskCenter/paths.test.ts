/**
 * 任务中心两级路由的路径常量。
 *
 * 钉住两件事：一级是 `/task-center`（侧栏高亮按前缀匹配，二级路径也落在它下面）；
 * 任务 id 进 URL 必须编码 —— 否则 id 里的 `/` 会把动态段拆成两段，二级页直接 404。
 */
import { describe, expect, it } from 'vitest';
import { TASK_CENTER_LIST_PATH, TASK_EXECUTIONS_ALL_PATH, taskEditorPath, taskExecutionsPath } from './paths';

describe('任务中心路径', () => {
  it('一级路径是 /task-center', () => {
    expect(TASK_CENTER_LIST_PATH).toBe('/task-center');
  });

  it('全部执行记录是静态段，且在一级路径之下', () => {
    expect(TASK_EXECUTIONS_ALL_PATH).toBe('/task-center/executions');
    expect(TASK_EXECUTIONS_ALL_PATH.startsWith(`${TASK_CENTER_LIST_PATH}/`)).toBe(true);
  });

  it('任务编辑路径挂在执行记录之下，且 id 一样要编码', () => {
    expect(taskEditorPath('def_kb_monthly_compile')).toBe('/task-center/def_kb_monthly_compile/edit');
    expect(taskEditorPath('a/b')).toBe('/task-center/a%2Fb/edit');
  });

  it('任务执行记录路径拼接并编码 id', () => {
    expect(taskExecutionsPath('def_kb_monthly_compile')).toBe('/task-center/def_kb_monthly_compile');
    // 含斜杠的 id 必须被编码成一个路径段
    expect(taskExecutionsPath('a/b')).toBe('/task-center/a%2Fb');
  });
});
