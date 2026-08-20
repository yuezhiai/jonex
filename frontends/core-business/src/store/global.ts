import { create } from 'zustand';
import { getItem, setItem } from '@/utils/storage';
import { listSpaces } from '@/api/domainSpace';
import type { DomainSpace } from '@/types/domainSpace';
import { persistSpaceId, readPersistedSpaceId } from '@jonex/shell-sdk';

interface SetCurrentSpaceIdOpts {
  persist?: boolean;
  broadcast?: boolean;
}

interface GlobalState {
  userInfo: Record<string, unknown> | null;

  // ── 领域空间状态 ──
  spaces: DomainSpace[];
  spacesLoaded: boolean;
  spacesLoading: boolean;
  currentSpaceId: string | null;
  /** 派生：当前空间对象（由 actions 同步更新） */
  currentSpace?: DomainSpace;

  setUserInfo: (data: Record<string, unknown> | null) => void;
  setCurrentSpaceId: (id: string | null, opts?: SetCurrentSpaceIdOpts) => void;
  setSpaces: (list: DomainSpace[]) => void;
  loadSpaces: () => Promise<void>;
  refreshSpaces: () => Promise<void>;
}

export const useGlobalStore = create<GlobalState>((set, get) => ({
  userInfo: getItem<Record<string, unknown>>('jonex_user') || null,

  // ── 领域空间状态 ──
  spaces: [],
  spacesLoaded: false,
  spacesLoading: false,
  currentSpaceId: null,
  currentSpace: undefined,

  setUserInfo: (data) => {
    set({ userInfo: data });
    setItem('jonex_user', data);
  },

  setCurrentSpaceId: (id, opts = {}) => {
    const { persist = true, broadcast = false } = opts;
    set({ currentSpaceId: id, currentSpace: get().spaces.find((s) => s.id === id) });
    if (persist) {
      persistSpaceId(id);
    }
    if (broadcast) {
      // 动态 import 避免循环依赖（emitSpaceChanged 来自 shell-sdk）
      import('@jonex/shell-sdk').then(({ emitSpaceChanged }) => emitSpaceChanged(id));
    }
  },

  setSpaces: (list) => {
    set((state) => {
      // 当前空间不在新列表中则回落
      let currentSpaceId = state.currentSpaceId;
      if (currentSpaceId && !list.find((s) => s.id === currentSpaceId)) {
        currentSpaceId = list[0]?.id ?? null;
      }
      return {
        spaces: list,
        currentSpaceId,
        currentSpace: list.find((s) => s.id === currentSpaceId),
        spacesLoaded: true,
      };
    });
  },

  /** 幂等加载空间列表（首次初始化） */
  loadSpaces: async () => {
    const state = get();
    if (state.spacesLoading || state.spacesLoaded) return;
    set({ spacesLoading: true });
    try {
      const result = await listSpaces(0, 100);
      const data = result.items;
      // 初始化：localStorage > 首个
      const { currentSpaceId } = get();
      if (!currentSpaceId) {
        const persisted = readPersistedSpaceId();
        const valid = persisted && data.find((s) => s.id === persisted);
        const nextId = valid ? persisted : (data[0]?.id ?? null);
        persistSpaceId(nextId);
        set({ currentSpaceId: nextId });
      }
      get().setSpaces(data);
    } finally {
      set({ spacesLoading: false });
    }
  },

  /** 强制重拉空间列表（CRUD 后或收到失效事件） */
  refreshSpaces: async () => {
    set({ spacesLoading: true });
    try {
      const result = await listSpaces(0, 100);
      const data = result.items;
      get().setSpaces(data);
    } finally {
      set({ spacesLoading: false, spacesLoaded: true });
    }
  },
}));
