/**
 * [jonex] 推理步骤状态映射（2026-10-16 接入 Ant Design X）。
 *
 * 后端的 `ReasoningStep.status` 是四值 `running | done | skipped | failed`，
 * `ThoughtChain` 的 `status` 只有三值可落 `loading | success | error`（另有 `abort`，
 * 那是「用户主动中止」的语义，推理链里没有对应来源）。两边的取值集合不重合，
 * 所以映射必须显式写出来，不能直接把后端的值透传给组件。
 *
 * `skipped` 是唯一没有对应值的：本映射把它落 `success` **并置 `skipped: true`**，
 * 由调用方换成灰色跳过图标。另一个做法是把它排除在链路外单列一行，但那会让一条
 * 完整推理链出现断点 —— 步骤「消失」比步骤「被标成跳过」更难解释。
 *
 * 独立成 `.ts` 纯模块（而非写在 `ThoughtSteps.tsx` 里）是为了让单测不依赖 React 渲染。
 */

/** `ThoughtChain` 步骤支持的状态 */
export type ThoughtChainStatus = 'loading' | 'success' | 'error';

export interface ThoughtStepStatus {
  status: ThoughtChainStatus;
  /** 该步被跳过 —— `status` 仍为 `success`，靠图标与配色区分 */
  skipped: boolean;
}

export function toThoughtStatus(status: string): ThoughtStepStatus {
  switch (status) {
    case 'running':
      return { status: 'loading', skipped: false };
    case 'done':
      return { status: 'success', skipped: false };
    case 'skipped':
      return { status: 'success', skipped: true };
    case 'failed':
      return { status: 'error', skipped: false };
    default:
      // 后端将来新增状态时落到这里。按 error 处理而不是 success —— 不认识的状态
      // 意味着我们不能宣称这一步成功了，显示成成功会掩盖问题。
      return { status: 'error', skipped: false };
  }
}
