import { create } from 'zustand';
import type { KnowledgeSearchUiMode, SearchSession } from '@/types/knowledgeSearch';

/**
 * 正在进行的知识检索（跨页面路由保留）。
 *
 * 检索的请求与结果状态**不能挂在页面组件的生命周期上**：用户切走页面时组件卸载，
 * 组件内的 `useState` 随之销毁、cleanup 又会 abort 掉 fetch —— 回到页面只剩空白，
 * 后台明明还在跑的任务也白跑了。
 *
 * 这里把「正在跑的这一次检索」提升到模块级：页面卸载不中断请求，完成回调继续写回
 * 这份 session；用户回到页面直接接着看（同一标签页内的路由往返）。
 *
 * 终止只发生在显式动作上：
 * - 用户点「停止」/「清空」；
 * - 发起新的检索（旧任务继续跑只会白占资源）；
 * - 切换空间（换了上下文就该从干净状态开始，与 Wiki 会话的既有口径一致）。
 *
 * 刷新 / 关标签页是浏览器断连，这里的运行态不会保留：Wiki 链路有后端检索会话
 * （`store/knowledgeSearchSession.ts` 的 session_id）可以重新订阅，其余链路
 * 由后端把这次检索跑完并落检索历史。
 */

type SessionUpdater = SearchSession | null | ((prev: SearchSession | null) => SearchSession | null);

export interface KnowledgeSearchRuntimeState {
  /** 发起检索时的空间：空间换了就不再续接 */
  spaceId: string | null;
  /** 发起时的输入框文本（回到页面时回填） */
  query: string;
  /** 发起时的检索范围与档位（回到页面时还原面板） */
  domainId: string;
  mode: KnowledgeSearchUiMode | null;
  session: SearchSession | null;
  /** 本次运行的 AbortController：页面卸载后仍由 store 持有，便于回来时停止 */
  controller: AbortController | null;

  setSession: (next: SessionUpdater) => void;
  /** 发起一次检索：记录上下文与控制器，同时写入首帧会话状态 */
  beginRun: (run: {
    spaceId: string | null;
    query: string;
    domainId: string;
    mode: KnowledgeSearchUiMode | null;
    controller: AbortController;
    session: SearchSession;
  }) => void;
  /** 只记录上下文（历史回放 / 恢复订阅这类不新建 controller 的会话） */
  setContext: (context: {
    spaceId: string | null;
    query: string;
    domainId: string;
    mode: KnowledgeSearchUiMode | null;
  }) => void;
  setController: (controller: AbortController | null) => void;
  /** 本次运行结束时解绑控制器（仅当仍是它，避免旧任务的收尾把新任务释放掉） */
  releaseController: (controller: AbortController) => void;
  /** 中止当前运行（主动停止 / 清空 / 被新检索取代） */
  abortRun: () => void;
  /** 回到干净态：清空会话、上下文与控制器 */
  reset: () => void;
}

export const useKnowledgeSearchRuntimeStore = create<KnowledgeSearchRuntimeState>((set, get) => ({
  spaceId: null,
  query: '',
  domainId: '',
  mode: null,
  session: null,
  controller: null,

  setSession: (next) =>
    set((state) => ({ session: typeof next === 'function' ? next(state.session) : next })),

  beginRun: ({ spaceId, query, domainId, mode, controller, session }) =>
    set({ spaceId, query, domainId, mode, controller, session }),

  setContext: ({ spaceId, query, domainId, mode }) => set({ spaceId, query, domainId, mode }),

  setController: (controller) => set({ controller }),

  releaseController: (controller) => {
    if (get().controller === controller) set({ controller: null });
  },

  abortRun: () => {
    get().controller?.abort();
    set({ controller: null });
  },

  reset: () => set({ spaceId: null, query: '', domainId: '', mode: null, session: null, controller: null }),
}));
