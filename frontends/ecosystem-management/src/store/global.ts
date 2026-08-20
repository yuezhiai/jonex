import { create } from 'zustand';
import { getItem, setItem } from '@/utils/storage';

interface GlobalState {
  locale: string;
  userInfo: Record<string, unknown> | null;
  setLocale: (lang: string) => void;
  setUserInfo: (data: Record<string, unknown> | null) => void;
}

export const useGlobalStore = create<GlobalState>((set) => ({
  locale: getItem<string>('locale') || 'zh',
  userInfo: getItem<Record<string, unknown>>('jonex_user') || null,

  setLocale: (lang) => {
    set({ locale: lang });
    setItem('locale', lang);
  },

  setUserInfo: (data) => {
    set({ userInfo: data });
    setItem('jonex_user', data);
  },
}));
