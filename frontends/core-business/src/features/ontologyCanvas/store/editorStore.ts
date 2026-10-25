/**
 * 画布编辑状态（zustand + immer patches）。
 *
 * 移植自 nexonto 的同名 store，砍掉了 `addedMark` 那一套——那是**抽取/AI 合入
 * 时给新元素打绿色高亮**用的，本移植不带这两个功能。
 *
 * 保留 undo/redo 的机制不变：用 immer 的 patch 记录每步的逆操作，
 * 而不是深拷贝整份文档做快照。schema 有几百个实体时后者会很吃内存。
 */

import { applyPatches, enablePatches, produceWithPatches, type Patch } from 'immer';
import { create } from 'zustand';

import { applyCommand, type DomainCommand, type DomainDocument } from '../domain/index.js';

enablePatches();

export type Selection = { kind: 'entity' | 'relation' | 'constraint'; id: string } | null;

interface HistoryEntry {
  redo: Patch[];
  undo: Patch[];
}

/** 撤销栈上限。与 nexonto 一致——足够覆盖正常编辑节奏，又不至于攒住整份文档历史。 */
const HISTORY_LIMIT = 50;

interface EditorState {
  savedDocument: DomainDocument | undefined;
  workingDocument: DomainDocument | undefined;
  selection: Selection;
  past: HistoryEntry[];
  future: HistoryEntry[];
  dirty: boolean;

  initialize: (document: DomainDocument) => void;
  execute: (command: DomainCommand, selection?: Selection) => void;
  executeAll: (commands: DomainCommand[], selection?: Selection) => void;
  undo: () => void;
  redo: () => void;
  select: (selection: Selection) => void;
  /** 保存成功后把当前工作文档记为已保存基线（清 dirty） */
  markSaved: (revision: number, savedAt: string) => void;
  reset: () => void;
}

export const useEditorStore = create<EditorState>((set, get) => ({
  savedDocument: undefined,
  workingDocument: undefined,
  selection: null,
  past: [],
  future: [],
  dirty: false,

  initialize: (document) =>
    set({
      savedDocument: structuredClone(document),
      workingDocument: structuredClone(document),
      selection: null,
      past: [],
      future: [],
      dirty: false,
    }),

  execute: (command, selection) => {
    const current = get().workingDocument;
    if (!current) return;
    const [next, redo, undo] = produceWithPatches(current, () => applyCommand(current, command));
    // 命令被拒时 patches 为空（applyCommand 返回原文档）——不要记一条空历史
    if (!redo.length) {
      if (selection !== undefined) set({ selection });
      return;
    }
    set((state) => ({
      workingDocument: next,
      selection: selection === undefined ? state.selection : selection,
      past: [...state.past, { redo, undo }].slice(-HISTORY_LIMIT),
      future: [],
      dirty: true,
    }));
  },

  executeAll: (commands, selection) => {
    const current = get().workingDocument;
    if (!current || !commands.length) return;
    const [next, redo, undo] = produceWithPatches(current, () =>
      commands.reduce((doc, command) => applyCommand(doc, command), current),
    );
    if (!redo.length) {
      if (selection !== undefined) set({ selection });
      return;
    }
    set((state) => ({
      workingDocument: next,
      selection: selection === undefined ? state.selection : selection,
      past: [...state.past, { redo, undo }].slice(-HISTORY_LIMIT),
      future: [],
      dirty: true,
    }));
  },

  undo: () => {
    const state = get();
    // 不用 `Array.at()`——Jonex 的 tsconfig lib 早于 es2022，编译不过
    const entry = state.past[state.past.length - 1];
    if (!entry || !state.workingDocument) return;
    set({
      workingDocument: applyPatches(state.workingDocument, entry.undo),
      past: state.past.slice(0, -1),
      future: [entry, ...state.future].slice(0, HISTORY_LIMIT),
      dirty: true,
      // 撤销可能删掉了当前选中的对象 → 清选择，避免详情面板对着不存在的 id 取值
      selection: null,
    });
  },

  redo: () => {
    const state = get();
    const entry = state.future[0];
    if (!entry || !state.workingDocument) return;
    set({
      workingDocument: applyPatches(state.workingDocument, entry.redo),
      past: [...state.past, entry].slice(-HISTORY_LIMIT),
      future: state.future.slice(1),
      dirty: true,
      selection: null,
    });
  },

  select: (selection) => set({ selection }),

  markSaved: (revision, savedAt) => {
    const current = get().workingDocument;
    if (!current) return;
    const document = structuredClone(current);
    document.revision = revision;
    document.savedAt = savedAt;
    set({ workingDocument: document, savedDocument: structuredClone(document), dirty: false });
  },

  reset: () =>
    set({ savedDocument: undefined, workingDocument: undefined, selection: null, past: [], future: [], dirty: false }),
}));
