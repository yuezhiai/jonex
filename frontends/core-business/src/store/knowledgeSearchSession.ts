import { create } from 'zustand';

/**
 * 未完成的 Wiki 检索会话（跨刷新 / 跨路由保留）。
 *
 * 连接只代表「谁在看」：离开页面或刷新只是断开订阅，后台任务在能力服务继续执行，
 * 凭 `session_id` 可以重新订阅（`POST /knowledge-base/search/jonex_wiki/sessions/{id}/stream`）。
 * 这里存的就是那把钥匙。
 *
 * 用 sessionStorage 而不是 localStorage：刷新保留、关标签页即清，不跨标签页串台。
 */
const STORAGE_KEY = 'jonex_knowledge_search_pending';

export interface PendingWikiSearchSession {
  sessionId: string;
  query: string;
  /** 发起时的空间：回来时空间已换 ⇒ 不再恢复（换了上下文就该从干净状态开始） */
  spaceId: string | null;
  domainId: string;
  startedAt: number;
}

function readStored(): PendingWikiSearchSession | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PendingWikiSearchSession> | null;
    if (!parsed || typeof parsed.sessionId !== 'string' || !/^[a-f0-9]{32}$/.test(parsed.sessionId)) {
      return null;
    }
    return {
      sessionId: parsed.sessionId,
      query: typeof parsed.query === 'string' ? parsed.query : '',
      spaceId: typeof parsed.spaceId === 'string' ? parsed.spaceId : null,
      domainId: typeof parsed.domainId === 'string' ? parsed.domainId : '',
      startedAt: typeof parsed.startedAt === 'number' ? parsed.startedAt : Date.now(),
    };
  } catch {
    // 隐私模式 / 存储被禁用：退化成「不跨刷新恢复」，不影响本次检索
    return null;
  }
}

function writeStored(value: PendingWikiSearchSession | null): void {
  try {
    if (value) window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    else window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // 同上：写不进去只是不能恢复，不是错误
  }
}

interface KnowledgeSearchSessionState {
  pending: PendingWikiSearchSession | null;
  save: (session: PendingWikiSearchSession) => void;
  /** 传 sessionId 时只清这一条，避免旧会话的收尾回调把新会话的记录抹掉 */
  clear: (sessionId?: string) => void;
}

export const useKnowledgeSearchSessionStore = create<KnowledgeSearchSessionState>((set, get) => ({
  pending: readStored(),
  save: (session) => {
    writeStored(session);
    set({ pending: session });
  },
  clear: (sessionId) => {
    const current = get().pending;
    if (sessionId && current && current.sessionId !== sessionId) return;
    writeStored(null);
    set({ pending: null });
  },
}));

/** 读取待恢复会话（store 首次初始化已从 sessionStorage 载入） */
export function readPendingWikiSearchSession(): PendingWikiSearchSession | null {
  return useKnowledgeSearchSessionStore.getState().pending ?? readStored();
}
