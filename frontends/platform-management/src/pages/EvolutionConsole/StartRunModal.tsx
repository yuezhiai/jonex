import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Button, Checkbox, Collapse, Form, Input, InputNumber, Modal, Radio, Select, Space, Switch, Table, Tag, message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useTranslation } from 'react-i18next';
import {
  createCorpusCase,
  getAbstractSchema,
  getCompiledConstraints,
  getCompiledSchema,
  listCorpusCases,
  listEvolutionToolOptions,
  startEvolutionRun,
  type AbstractEntity,
  type CompiledConstraint,
  type CompiledRelationType,
  type CompiledSchemaEntityType,
  type ConstraintEntry,
  type CorpusCase,
  type EvolutionKind,
  type EvolutionToolOption,
  type MappingEntry,
  type MappingSlot,
  type RelationEntry,
  type StartRunBody,
  type ToolSchemaEntry,
} from '../../api/evolution';
import {
  EMPTY_OPTIONS,
  fetchOptimizableOptions,
  listRunnableSources,
  type DsOption,
  type OptimizableOptions,
} from '../../api/nl2sqlKbOptions';
import { relationEdgeOf } from './skipReasons';

/** 目标引导：用户选「想解决什么」，内部映射到 kind（不让人直接面对 6 个术语）。 */
const GOALS: Array<{ kind: EvolutionKind; titleKey: string; descKey: string }> = [
  { kind: 'alias', titleKey: 'evolutionConsole.goal.alias', descKey: 'evolutionConsole.goal.aliasDesc' },
  { kind: 'synonym', titleKey: 'evolutionConsole.goal.synonym', descKey: 'evolutionConsole.goal.synonymDesc' },
  { kind: 'query_rule', titleKey: 'evolutionConsole.goal.query_rule', descKey: 'evolutionConsole.goal.queryRuleDesc' },
  { kind: 'relation', titleKey: 'evolutionConsole.goal.relation', descKey: 'evolutionConsole.goal.relationDesc' },
  { kind: 'constraint', titleKey: 'evolutionConsole.goal.constraint', descKey: 'evolutionConsole.goal.constraintDesc' },
  { kind: 'mapping', titleKey: 'evolutionConsole.goal.mapping', descKey: 'evolutionConsole.goal.mappingDesc' },
  { kind: 'tool_schema', titleKey: 'evolutionConsole.goal.tool_schema', descKey: 'evolutionConsole.goal.toolSchemaDesc' },
];

/** 默认值（与 spec 6.2 一致；dry-run 收进「高级选项」，默认关）。 */
export const DEFAULT_KB = 'kb_assets_r2';
export const DEFAULT_JINPIN_DS = '4cdb29e4-268c-4daf-8abe-7b879fa6abf0';

/** 生成提交幂等键（优先 crypto.randomUUID，兼容非安全上下文——先例 KnowledgeSearch）。 */
function genUuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

/** constraint 行的操作：保持不变 / 停用 / 启用 / 改口径（改口径 = update）。 */
type ConstraintOp = 'keep' | 'disable' | 'enable' | 'update';

interface ConstraintDraft {
  op: ConstraintOp;
  /** 改口径时的草稿表达式（初值取当前 expression）。 */
  expression: string;
}

/** 按行当前 enabled 状态给出合法操作：已启用只能停用、已停用只能启用（避免 no-op 条目）。 */
const constraintOpsFor = (row: CompiledConstraint): ConstraintOp[] =>
  (row.enabled === false
    ? ['keep', 'enable', 'update']
    : ['keep', 'disable', 'update']);

// ── mapping（P1-5）：抽象落点的语义槽编辑 ──

/** 落点的四个可改槽（`enabled` 由 disable/enable 单独维护）。 */
const MAPPING_SLOTS: MappingSlot[] = ['role', 'filter', 'aggregation', 'grain'];

type MappingOp = 'keep' | 'disable' | 'enable' | 'update';

interface MappingDraft {
  op: MappingOp;
  slots: Record<MappingSlot, string>;
}

/** 抽象 schema 的一行落点（当前 ds 过滤后）。 */
interface MappingRow {
  /** 复合身份键 = `target_code|ds|entity|column`，与后端 `entry_key(...,"mapping")` 同口径。 */
  key: string;
  target_code: string;
  data_source_id: string;
  entity_name: string;
  attribute_name: string;
  /** 物理 `Entity.column`：行内输入框的 aria-label 用它定位（同一抽象属性可有多个落点）。 */
  sourceCode: string;
  enabled: boolean;
  slots: Record<MappingSlot, string>;
}

interface NewMappingDraft {
  target_code: string;
  entity_name: string;
  attribute_name: string;
  slots: Record<MappingSlot, string>;
}

const emptySlots = (): Record<MappingSlot, string> => ({
  role: '', filter: '', aggregation: '', grain: '',
});

const emptyNewMapping = (): NewMappingDraft => ({
  target_code: '', entity_name: '', attribute_name: '', slots: emptySlots(),
});

/** 落点的合法操作：已停用只能启用，反之只能停用（与 constraint 同姿态，避免 no-op）。 */
const mappingOpsFor = (enabled: boolean): MappingOp[] =>
  (enabled ? ['keep', 'disable', 'update'] : ['keep', 'enable', 'update']);

// ── tool_schema（Tool-Schema 线）：工具描述与启停编辑 ──

/** 工具草稿：`description` 初值取当前有效描述；`disabled` = 停用勾选。 */
interface ToolDraft {
  description: string;
  disabled: boolean;
}

interface FormValues {
  kind: EvolutionKind;
  kb?: string;
  ds?: string;
  repeat?: number;
  dry_run: boolean;
  skip_probe?: boolean;
  target_cases?: string[];
  window_days?: number;
  picks?: string;
}

interface StartRunModalProps {
  open: boolean;
  onClose: () => void;
  onStarted: (runId: string) => void;
  /** 提交失败（含 409 活跃冲突）后回调：让容器刷新 status 横幅。 */
  onFailed: () => void;
}

/** 发起 run 弹窗：字段逐项映射 POST /runs 的 body。 */
export default function StartRunModal({ open, onClose, onStarted, onFailed }: StartRunModalProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<FormValues>();
  const [submitting, setSubmitting] = useState(false);
  const [cases, setCases] = useState<CorpusCase[]>([]);
  const [newCaseOpen, setNewCaseOpen] = useState(false);
  const [caseCreating, setCaseCreating] = useState(false);
  const [caseDsOptions, setCaseDsOptions] = useState<DsOption[]>([]);
  const [caseDsLoading, setCaseDsLoading] = useState(false);
  const [newCaseForm] = Form.useForm<{
    question: string; control?: string; expect: 'answer' | 'refuse';
    tier?: string; ds?: string; name?: string;
  }>();
  const [options, setOptions] = useState<OptimizableOptions>(EMPTY_OPTIONS);
  const [optionsLoading, setOptionsLoading] = useState(false);
  const [optionsError, setOptionsError] = useState(false);
  // constraint：当前 compiled schema 的约束清单 + 逐行草稿；新增口径独立一份。
  const [constraints, setConstraints] = useState<CompiledConstraint[]>([]);
  const [constraintDrafts, setConstraintDrafts] = useState<Record<string, ConstraintDraft>>({});
  const [constraintLoading, setConstraintLoading] = useState(false);
  const [constraintError, setConstraintError] = useState(false);
  const [newConstraint, setNewConstraint] = useState({ target_code: '', expression: '' });
  const constraintSeqRef = useRef(0);
  // mapping：抽象 schema 全量（KB 级，一次拉取）+ 当前 ds 的编译 schema（add 区列白名单）。
  const [abstractEntities, setAbstractEntities] = useState<AbstractEntity[]>([]);
  const [mappingLoading, setMappingLoading] = useState(false);
  const [mappingError, setMappingError] = useState(false);
  const [mappingDrafts, setMappingDrafts] = useState<Record<string, MappingDraft>>({});
  const [newMapping, setNewMapping] = useState<NewMappingDraft>(emptyNewMapping);
  const [compiledEntities, setCompiledEntities] = useState<CompiledSchemaEntityType[]>([]);
  const [compiledLoading, setCompiledLoading] = useState(false);
  const mappingSeqRef = useRef(0);
  const compiledSeqRef = useRef(0);
  // relation：当前 kb × ds 的现有 join 边（勾选 = 按边名删除）；零勾选 = 发现器模式。
  const [relationEdges, setRelationEdges] = useState<CompiledRelationType[]>([]);
  const [relationSelected, setRelationSelected] = useState<string[]>([]);
  const [relationLoading, setRelationLoading] = useState(false);
  const [relationError, setRelationError] = useState(false);
  const relationSeqRef = useRef(0);
  // tool_schema：当前 kb 的工具名单（代码默认 ⊕ active 覆盖）+ 逐工具草稿。
  const [toolOptions, setToolOptions] = useState<EvolutionToolOption[]>([]);
  const [toolDrafts, setToolDrafts] = useState<Record<string, ToolDraft>>({});
  const [toolLoading, setToolLoading] = useState(false);
  const [toolError, setToolError] = useState(false);
  const toolSeqRef = useRef(0);
  const kind = Form.useWatch('kind', form) ?? 'alias';
  const kbSelected = Form.useWatch('kb', form);
  const dsSelected = Form.useWatch('ds', form);

  const loadSeqRef = useRef(0);
  const loadOptions = useCallback(() => {
    const seq = ++loadSeqRef.current; // T2-1 序号守卫：只有最新一发请求能写 state
    setOptionsLoading(true);
    setOptionsError(false);
    fetchOptimizableOptions()
      .then((res) => {
        if (seq === loadSeqRef.current) setOptions(res);
      })
      .catch(() => {
        // 不加守卫时，较早的失败最后落地会「粘住」optionsError（成功路径不清 error），
        // 即便新一轮已成功也仍显示失败——旧请求一律不得写 state。
        if (seq === loadSeqRef.current) {
          setOptions(EMPTY_OPTIONS);
          setOptionsError(true);
        }
      })
      .finally(() => {
        if (seq === loadSeqRef.current) setOptionsLoading(false);
      });
  }, []);

  /** constraint 底稿：按当前 kb × ds 读 compiled schema 的 constraints。
   * 序号守卫（同 loadOptions）：快速切 kb/ds 时旧响应不得覆盖新清单。
   * 刻意不依赖 `t`——测试里的 t 每次渲染都是新引用，进了 useCallback 会触发 effect 死循环。 */
  const loadConstraints = useCallback((kb: string, ds: string) => {
    const seq = ++constraintSeqRef.current;
    setConstraintLoading(true);
    setConstraintError(false);
    getCompiledConstraints(kb, ds)
      .then((res) => {
        if (seq !== constraintSeqRef.current) return;
        const rows = Array.isArray(res.constraints) ? res.constraints : [];
        setConstraints(rows);
        const drafts: Record<string, ConstraintDraft> = {};
        for (const row of rows) {
          if (!row?.name) continue;
          drafts[row.name] = { op: 'keep', expression: String(row.expression ?? '') };
        }
        setConstraintDrafts(drafts);
      })
      .catch(() => {
        if (seq !== constraintSeqRef.current) return;
        setConstraints([]);
        setConstraintDrafts({});
        setConstraintError(true);
      })
      .finally(() => {
        if (seq === constraintSeqRef.current) setConstraintLoading(false);
      });
  }, []);

  /** mapping 底稿：抽象 schema 是 KB 级数据，按 kb 拉一次（seq 守卫同 constraint）。 */
  const loadAbstractSchema = useCallback((kb: string) => {
    const seq = ++mappingSeqRef.current;
    setMappingLoading(true);
    setMappingError(false);
    getAbstractSchema(kb)
      .then((res) => {
        if (seq !== mappingSeqRef.current) return;
        const rows = res?.schema?.entities;
        setAbstractEntities(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (seq !== mappingSeqRef.current) return;
        setAbstractEntities([]);
        setMappingError(true);
      })
      .finally(() => {
        if (seq === mappingSeqRef.current) setMappingLoading(false);
      });
  }, []);

  /** relation 底稿：按当前 kb × ds 读 compiled schema 的 relation_types（有 seq 守卫）。 */
  const loadRelations = useCallback((kb: string, ds: string) => {
    const seq = ++relationSeqRef.current;
    setRelationLoading(true);
    setRelationError(false);
    getCompiledSchema(kb, ds)
      .then((res) => {
        if (seq !== relationSeqRef.current) return;
        const rows = res?.relation_types;
        setRelationEdges(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (seq !== relationSeqRef.current) return;
        setRelationEdges([]);
        setRelationError(true);
      })
      .finally(() => {
        if (seq === relationSeqRef.current) setRelationLoading(false);
      });
  }, []);

  /** tool_schema 底稿：按当前 kb 拉工具名单（代码默认 ⊕ active 覆盖），
   * 并重建逐工具草稿（切 kb 后旧输入不得带过去）。序号守卫同 constraint。 */
  const loadToolOptions = useCallback((kb: string) => {
    const seq = ++toolSeqRef.current;
    setToolLoading(true);
    setToolError(false);
    listEvolutionToolOptions(kb)
      .then((res) => {
        if (seq !== toolSeqRef.current) return;
        const rows = Array.isArray(res?.tools) ? res.tools : [];
        setToolOptions(rows);
        const drafts: Record<string, ToolDraft> = {};
        for (const row of rows) {
          if (!row?.name) continue;
          drafts[row.name] = {
            description: String(row.effective_description ?? row.description ?? ''),
            disabled: row.enabled === false,
          };
        }
        setToolDrafts(drafts);
      })
      .catch(() => {
        if (seq !== toolSeqRef.current) return;
        setToolOptions([]);
        setToolDrafts({});
        setToolError(true);
      })
      .finally(() => {
        if (seq === toolSeqRef.current) setToolLoading(false);
      });
  }, []);

  // T3-1 幂等键：每次「打开弹窗」生成一次；提交失败后的重试沿用同一个——
  // 桥接凭它识别重试、复看既定 run 而不再 spawn（一次点击两个 run 的现场：2026-10-09 run_10/run_11）。
  const requestIdRef = useRef('');
  useEffect(() => {
    if (!open) return;
    requestIdRef.current = genUuid();
    // 重开时回到 initialValues（dry-run 默认开），不残留上次的输入
    form.resetFields();
    setNewConstraint({ target_code: '', expression: '' });
    setNewMapping(emptyNewMapping());
    loadOptions();
  }, [open, form, loadOptions]);

  useEffect(() => {
    if (!open) return;
    listCorpusCases()
      .then((res) => setCases(res.cases))
      .catch(() => setCases([])); // 题目清单拉不到不阻断发起（目标题可空 = 全部）
  }, [open]);

  useEffect(() => {
    if (!newCaseOpen || !kbSelected) return;
    setCaseDsLoading(true);
    listRunnableSources(kbSelected)
      .then((list) => setCaseDsOptions(list))
      .catch(() => setCaseDsOptions([]))
      .finally(() => setCaseDsLoading(false));
  }, [newCaseOpen, kbSelected]);

  /** 按 kind 的 KB 选项池：alias / tool_schema 用抽象行那族（tool_schema 的覆盖写在
   * 抽象 schema 的 tools 块）；query_rule/relation/constraint 用 compiled 行那族
   * （后三者判据同源）。
   *
   * synonym 不需要抽象 schema（候选来自失败观测、验真走编译 schema），只有抽象
   * schema 的 KB 不够用：取两族的**去重并集** —— 有数据源/编译 schema 但没有抽象
   * schema 的 KB 也必须能选。 */
  const kbPool = useMemo(() => {
    if (kind === 'synonym') {
      const byId = new Map(options.aliasKbs.map((kb) => [kb.id, kb]));
      for (const item of options.queryRuleKbs) byId.set(item.kb.id, item.kb);
      return Array.from(byId.values());
    }
    return kind === 'alias' || kind === 'tool_schema'
      ? options.aliasKbs
      : options.queryRuleKbs.map((x) => x.kb);
  }, [kind, options]);

  /** 需要 ds 的 kind 的选项池（由已选 kb 决定；复用同一份已拉数据，零二次请求）。
   * alias / tool_schema / synonym 是 KB 级，没有 ds 概念。 */
  const dsPool = useMemo(() => {
    if (kind === 'alias' || kind === 'tool_schema' || kind === 'synonym' || !kbSelected) {
      return [];
    }
    return options.queryRuleKbs.find((x) => x.kb.id === kbSelected)?.sources ?? [];
  }, [kind, options, kbSelected]);

  // 值对账：选项就绪后被选值不在池里（切 kind / 切 kb / 默认值失效）→ 清空。
  // kb/ds 一起清（kb 失效时 ds 的归属也失效）；kind 切到 alias 时不动 ds——虽隐藏但保留，
  // 切回 query_rule / relation 时若仍有效自动带出。
  // 不变量（T2-2）：跑本 effect 时读到的 options 一定是「本轮的」——同一请求链内 setOptions 先于
  // setOptionsLoading(false) 入队，loading=false 的那次渲染必然携带新池；旧请求已被序号守卫挡在
  // state 外。故「池空 → 清空默认值」不可能由陈旧空池误触发（四条时序路径已逐一推演）。
  // 加载中/失败态不动：失败态靠禁用提交兜底，别把默认值误清。
  useEffect(() => {
    if (!open || optionsLoading || optionsError) return;
    if (kbSelected && !kbPool.some((k) => k.id === kbSelected)) {
      form.setFieldsValue({ kb: undefined, ds: undefined });
      return;
    }
    if (kind !== 'alias' && kind !== 'tool_schema' && kind !== 'synonym'
        && dsSelected && !dsPool.some((s) => s.id === dsSelected)) {
      form.setFieldsValue({ ds: undefined });
    }
  }, [open, kind, optionsLoading, optionsError, kbSelected, dsSelected, kbPool, dsPool, form]);

  // constraint 底稿按 (kb, ds) 拉取；切走 kind / 关窗时作废在途请求并清空，避免旧清单残留。
  useEffect(() => {
    if (!open || kind !== 'constraint' || !kbSelected || !dsSelected) {
      constraintSeqRef.current += 1;
      setConstraintLoading(false);
      setConstraintError(false);
      setConstraints([]);
      setConstraintDrafts({});
      setNewConstraint({ target_code: '', expression: '' });
      return;
    }
    // kb/ds 变了就是另一份底稿：新增口径不能带着上一份的输入过去。
    setNewConstraint({ target_code: '', expression: '' });
    loadConstraints(kbSelected, dsSelected);
  }, [open, kind, kbSelected, dsSelected, loadConstraints]);

  // 约束行状态变化（重载 / 切 kb）后，把不再合法的 op 归一回 keep。
  useEffect(() => {
    if (kind !== 'constraint') return;
    setConstraintDrafts((prev) => {
      let changed = false;
      const next: Record<string, ConstraintDraft> = {};
      for (const [name, draft] of Object.entries(prev)) {
        const row = constraints.find((item) => item.name === name);
        const allowed = row ? constraintOpsFor(row) : (['keep'] as ConstraintOp[]);
        if (!allowed.includes(draft.op)) {
          next[name] = { ...draft, op: 'keep' };
          changed = true;
        } else {
          next[name] = draft;
        }
      }
      return changed ? next : prev;
    });
  }, [kind, constraints]);

  const setDraft = (name: string, patch: Partial<ConstraintDraft>) => {
    setConstraintDrafts((prev) => {
      const base = prev[name];
      return {
        ...prev,
        [name]: {
          op: base?.op ?? 'keep',
          expression: base?.expression ?? '',
          ...patch,
        },
      };
    });
  };

  // ── mapping：当前 ds 的落点行（抽象 schema 只按 kb 拉一次，切 ds 只重算行）──
  const mappingRows = useMemo<MappingRow[]>(() => {
    if (kind !== 'mapping' || !dsSelected) return [];
    const rows: MappingRow[] = [];
    for (const entity of abstractEntities) {
      for (const attr of entity.attributes ?? []) {
        for (const landing of attr.sources ?? []) {
          if (landing.dataSourceId !== dsSelected) continue;
          const targetCode = `${entity.name}.${attr.name}`;
          rows.push({
            key: [
              targetCode, landing.dataSourceId, landing.entityName, landing.attributeName,
            ].join('|'),
            target_code: targetCode,
            data_source_id: landing.dataSourceId,
            entity_name: landing.entityName,
            attribute_name: landing.attributeName,
            sourceCode: `${landing.entityName}.${landing.attributeName}`,
            enabled: landing.enabled !== false,
            slots: {
              role: landing.role ?? '',
              filter: landing.filter ?? '',
              aggregation: landing.aggregation ?? '',
              grain: landing.grain ?? '',
            },
          });
        }
      }
    }
    return rows;
  }, [kind, dsSelected, abstractEntities]);

  useEffect(() => {
    if (kind !== 'mapping') {
      setMappingDrafts({});
      return;
    }
    const next: Record<string, MappingDraft> = {};
    for (const row of mappingRows) next[row.key] = { op: 'keep', slots: { ...row.slots } };
    setMappingDrafts(next);
  }, [kind, mappingRows]);

  // 底稿按 kb 拉取；切走 kind / 关窗时作废在途请求并清空（seq 守卫同 constraint）。
  useEffect(() => {
    if (!open || kind !== 'mapping' || !kbSelected) {
      mappingSeqRef.current += 1;
      setMappingLoading(false);
      setMappingError(false);
      setAbstractEntities([]);
      setMappingDrafts({});
      return;
    }
    loadAbstractSchema(kbSelected);
  }, [open, kind, kbSelected, loadAbstractSchema]);

  // add 区的列来源：当前 kb × ds 的编译 schema（只有 ds 变了才重拉）。
  useEffect(() => {
    if (!open || kind !== 'mapping' || !kbSelected || !dsSelected) {
      compiledSeqRef.current += 1;
      setCompiledLoading(false);
      setCompiledEntities([]);
      return;
    }
    const seq = ++compiledSeqRef.current;
    setCompiledLoading(true);
    getCompiledSchema(kbSelected, dsSelected)
      .then((res) => {
        if (seq !== compiledSeqRef.current) return;
        const rows = res?.entity_types;
        setCompiledEntities(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (seq !== compiledSeqRef.current) return;
        setCompiledEntities([]);
      })
      .finally(() => {
        if (seq === compiledSeqRef.current) setCompiledLoading(false);
      });
  }, [open, kind, kbSelected, dsSelected]);

  // kb / ds 变了就是另一份底稿：add 区的输入不能带过去。
  useEffect(() => {
    setNewMapping(emptyNewMapping());
  }, [open, kind, kbSelected, dsSelected]);

  // relation 现有边按 (kb, ds) 拉取；切走 kind / 关窗时作废在途请求并清空勾选。
  useEffect(() => {
    if (!open || kind !== 'relation' || !kbSelected || !dsSelected) {
      relationSeqRef.current += 1;
      setRelationLoading(false);
      setRelationError(false);
      setRelationEdges([]);
      setRelationSelected([]);
      return;
    }
    // kb/ds 变了就是另一份边清单：上一份的勾选不能带过去。
    setRelationSelected([]);
    loadRelations(kbSelected, dsSelected);
  }, [open, kind, kbSelected, dsSelected, loadRelations]);

  // tool_schema 工具名单按 kb 拉取；切走 kind / 关窗时作废在途请求并清空草稿。
  useEffect(() => {
    if (!open || kind !== 'tool_schema' || !kbSelected) {
      toolSeqRef.current += 1;
      setToolLoading(false);
      setToolError(false);
      setToolOptions([]);
      setToolDrafts({});
      return;
    }
    loadToolOptions(kbSelected);
  }, [open, kind, kbSelected, loadToolOptions]);

  const setMappingDraft = (key: string, patch: Partial<MappingDraft>) => {
    setMappingDrafts((prev) => {
      const base = prev[key];
      if (!base) return prev;
      return { ...prev, [key]: { op: base.op, slots: base.slots, ...patch } };
    });
  };

  /** 编辑任一槽即视为「改口径」：只提交与该落点原值不同的槽（空串 = 显式清空）。 */
  const setMappingSlot = (key: string, slot: MappingSlot, value: string) => {
    setMappingDrafts((prev) => {
      const base = prev[key];
      if (!base) return prev;
      return {
        ...prev,
        [key]: { op: 'update', slots: { ...base.slots, [slot]: value } },
      };
    });
  };

  /** add 区的抽象属性候选：只列在该源有实体级落点（`included is not False`）的实体
   *  —— 没有实体级落点，后端 `entity_not_landed` 必然跳过。 */
  const mappingAttributeOptions = useMemo(() => {
    if (kind !== 'mapping' || !dsSelected) return [];
    return abstractEntities
      .filter((entity) => (entity.sources ?? []).some(
        (src) => src.dataSourceId === dsSelected && src.included !== false,
      ))
      .flatMap((entity) => (entity.attributes ?? []).map((attr) => {
        const target = `${entity.name}.${attr.name}`;
        return { value: target, label: target };
      }));
  }, [kind, dsSelected, abstractEntities]);

  /** 选抽象属性 → 同时带出该源上的物理实体名（列选项依赖它）。 */
  const pickMappingTarget = (target: string) => {
    if (!dsSelected) return;
    const sep = target.indexOf('.');
    const entityName = sep >= 0 ? target.slice(0, sep) : target;
    const landing = (abstractEntities.find((entity) => entity.name === entityName)?.sources ?? [])
      .find((src) => src.dataSourceId === dsSelected && src.included !== false);
    setNewMapping((prev) => ({
      ...prev,
      target_code: target,
      entity_name: landing?.entityName ?? '',
      attribute_name: '',
    }));
  };

  const compiledEntity = compiledEntities.find(
    (entity) => entity.name.toLowerCase() === newMapping.entity_name.toLowerCase(),
  );
  /** 列 → 提交物理列名（`physical_name`），后端按 slug/物理名归一后写回规范列名。 */
  const mappingColumnOptions = (compiledEntity?.attributes ?? []).map((attr) => ({
    value: attr.physical_name || attr.name,
    label: attr.physical_name && attr.physical_name !== attr.name
      ? `${attr.name}（${attr.physical_name}）`
      : attr.name,
  }));

  /** 只把**发生变化的行**转成 entries（未改的行不发）。 */
  const buildMappingEntries = (): MappingEntry[] => {
    if (!dsSelected) return [];
    const out: MappingEntry[] = [];
    for (const row of mappingRows) {
      const draft = mappingDrafts[row.key];
      if (!draft) continue;
      const identity = {
        target_code: row.target_code,
        data_source_id: row.data_source_id,
        entity_name: row.entity_name,
        attribute_name: row.attribute_name,
      };
      if (draft.op === 'disable' || draft.op === 'enable') {
        if (mappingOpsFor(row.enabled).includes(draft.op)) {
          out.push({ op: draft.op, ...identity });
        }
        continue;
      }
      const changed: Partial<Record<MappingSlot, string>> = {};
      for (const slot of MAPPING_SLOTS) {
        const value = draft.slots[slot].trim();
        if (value !== row.slots[slot]) changed[slot] = value;
      }
      if (Object.keys(changed).length) {
        out.push({ op: 'update', ...identity, ...changed });
      }
    }
    if (newMapping.target_code.trim() && newMapping.entity_name.trim()
        && newMapping.attribute_name.trim()) {
      const slots: Partial<Record<MappingSlot, string>> = {};
      for (const slot of MAPPING_SLOTS) {
        const value = newMapping.slots[slot].trim();
        if (value) slots[slot] = value;
      }
      out.push({
        op: 'add',
        target_code: newMapping.target_code.trim(),
        data_source_id: dsSelected,
        entity_name: newMapping.entity_name.trim(),
        attribute_name: newMapping.attribute_name.trim(),
        ...slots,
      });
    }
    return out;
  };

  /** relation：勾选的现有边 → `{op:"remove", name}`；零勾选返回空（→ 不送 entries = 发现器模式）。 */
  const buildRelationEntries = (): RelationEntry[] =>
    relationSelected.map((name) => ({ op: 'remove', name }));

  const setToolDraft = (name: string, patch: Partial<ToolDraft>) => {
    setToolDrafts((prev) => {
      const base = prev[name];
      if (!base) return prev;
      return { ...prev, [name]: { ...base, ...patch } };
    });
  };

  /** tool_schema：只提交**有变化**的条目（未改的工具不发）：
   * - 描述（trim 后）与当前有效描述不同 → `update`；
   * - 停用勾选与当前启停不同 → `disable` / `enable`（同一工具两处都变时各发一条）。
   * 空描述不产生 update（后端要求非空，静默按未改处理）。 */
  const buildToolEntries = (): ToolSchemaEntry[] => {
    const out: ToolSchemaEntry[] = [];
    for (const tool of toolOptions) {
      const draft = toolDrafts[tool.name];
      if (!draft) continue;
      const description = draft.description.trim();
      const baseline = String(tool.effective_description ?? tool.description ?? '').trim();
      if (description && description !== baseline) {
        out.push({ op: 'update', name: tool.name, description });
      }
      const currentlyDisabled = tool.enabled === false;
      if (draft.disabled && !currentlyDisabled) {
        out.push({ op: 'disable', name: tool.name });
      } else if (!draft.disabled && currentlyDisabled) {
        out.push({ op: 'enable', name: tool.name });
      }
    }
    return out;
  };

  /** 只把**发生变化的行**转成 entries：未改的行不发（后端按 entries 逐条落库）。
   * 变化 = op 切到 disable/enable，或 update 行的 expression 与原文不同。
   * 底部的「新增口径」填全 target_code + expression 才生成 add 条目。 */
  const buildConstraintEntries = (): ConstraintEntry[] => {
    const out: ConstraintEntry[] = [];
    for (const row of constraints) {
      if (!row?.name) continue;
      const draft = constraintDrafts[row.name];
      if (!draft) continue;
      if (draft.op === 'disable' || draft.op === 'enable') {
        // 只发合法的状态切换（已停用的行不会发 disable，反之亦然）。
        if (constraintOpsFor(row).includes(draft.op)) {
          out.push({ op: draft.op, name: row.name });
        }
        continue;
      }
      if (draft.op === 'update') {
        const expression = draft.expression.trim();
        if (expression && expression !== String(row.expression ?? '').trim()) {
          out.push({ op: 'update', name: row.name, expression });
        }
      }
    }
    const targetCode = newConstraint.target_code.trim();
    const expression = newConstraint.expression.trim();
    if (targetCode && expression) {
      // 新增只允许 query_rule（unique_attribute 不走 add，见 plan R2）。
      out.push({ op: 'add', target_code: targetCode, constraint_type: 'query_rule', expression });
    }
    return out;
  };

  const submit = async () => {
    let values: FormValues;
    try {
      values = await form.validateFields();
    } catch (err) {
      // 校验失败时 antd 已在字段上显示红字；其余异常照抛
      if ((err as { errorFields?: unknown })?.errorFields) return;
      throw err;
    }
    // constraint / mapping 批必须带 entries（后端缺省 1001）：零变化不发请求，先给人话提示。
    const constraintEntries = values.kind === 'constraint' ? buildConstraintEntries() : [];
    if (values.kind === 'constraint' && constraintEntries.length === 0) {
      message.warning(t('evolutionConsole.constraintNeedChange'));
      return;
    }
    const mappingEntries = values.kind === 'mapping' ? buildMappingEntries() : [];
    if (values.kind === 'mapping' && mappingEntries.length === 0) {
      message.warning(t('evolutionConsole.mappingNeedChange'));
      return;
    }
    // tool_schema 同理：entries 必填（后端缺省 1001），零变化先给人话提示。
    const toolEntries = values.kind === 'tool_schema' ? buildToolEntries() : [];
    if (values.kind === 'tool_schema' && toolEntries.length === 0) {
      message.warning(t('evolutionConsole.toolSchemaNeedChange'));
      return;
    }
    // relation 是可选的：勾了现有边 = 按边名删除；零勾选 = 交给后端发现器（不送 entries）。
    const relationEntries = values.kind === 'relation' ? buildRelationEntries() : [];
    // relation / query_rule / constraint 需要 ds（都锚在某个库的 compiled 行上）；
    // alias / mapping / tool_schema / synonym 是 KB 级 run（后三者各有独立或共享的
    // KB 级版本链），后端明确拒 ds。
    const needsDs = values.kind !== 'alias'
      && values.kind !== 'mapping'
      && values.kind !== 'tool_schema'
      && values.kind !== 'synonym';
    const body: StartRunBody = {
      request_id: requestIdRef.current || undefined, // T3-1：重试沿用同一键，桥接幂等复用
      kind: values.kind,
      kb: values.kb,
      ds: needsDs ? values.ds : undefined,
      entries: values.kind === 'constraint'
        ? constraintEntries
        : values.kind === 'mapping'
          ? mappingEntries
          : values.kind === 'tool_schema'
            ? toolEntries
            : values.kind === 'relation' && relationEntries.length
              ? relationEntries
              : undefined,
      repeat: values.repeat,
      dry_run: !!values.dry_run,
      target_cases: values.target_cases?.length ? values.target_cases : undefined,
      // alias / synonym 都从「窗口内的失败观测」提候选（synonym 的 builder 读同一字段）。
      window_days: values.kind === 'alias' || values.kind === 'synonym'
        ? values.window_days
        : undefined,
      // picks 只属于 query_rule；relation 零勾选时才由后端发现器自动产出条目。
      picks:
        values.kind === 'query_rule' && values.picks
          ? values.picks.split(',').map((s) => s.trim()).filter(Boolean)
          : undefined,
    };
    setSubmitting(true);
    try {
      const res = await startEvolutionRun(body);
      message.success(t('evolutionConsole.startOk', { runId: res.run_id }));
      form.resetFields();
      onStarted(res.run_id);
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : t('common.error'));
      onFailed();
    } finally {
      setSubmitting(false);
    }
  };

  /** constraint 编辑表：目标 / 类型 / 表达式（改口径时内联编辑）/ 启用状态 / 操作。 */
  const constraintColumns: ColumnsType<CompiledConstraint> = [
    {
      title: t('evolutionConsole.constraintColTarget'),
      dataIndex: 'target_code',
      render: (v: string | null | undefined) => <code>{v || '-'}</code>,
    },
    {
      title: t('evolutionConsole.constraintColType'),
      dataIndex: 'constraint_type',
      width: 120,
      render: (v: string | null | undefined) => v || '-',
    },
    {
      title: t('evolutionConsole.constraintColExpression'),
      key: 'expression',
      render: (_, row) => {
        const draft = constraintDrafts[row.name];
        if (draft?.op === 'update') {
          return (
            <Input.TextArea
              aria-label={`constraint-expr-${row.name}`}
              rows={2}
              value={draft.expression}
              onChange={(e) => setDraft(row.name, { expression: e.target.value })}
            />
          );
        }
        return <span style={{ color: '#595959' }}>{row.expression || '-'}</span>;
      },
    },
    {
      title: t('evolutionConsole.constraintColEnabled'),
      dataIndex: 'enabled',
      width: 100,
      render: (v: boolean | null | undefined) => (v === false
        ? <Tag>{t('evolutionConsole.constraintDisabledTag')}</Tag>
        : <Tag color="success">{t('evolutionConsole.constraintEnabledTag')}</Tag>),
    },
    {
      title: t('evolutionConsole.constraintColOp'),
      key: 'op',
      width: 250,
      render: (_, row) => {
        const allowed = constraintOpsFor(row);
        const current = constraintDrafts[row.name]?.op;
        return (
          <Radio.Group
            size="small"
            optionType="button"
            // 行状态变了（如重载后 enabled 翻转）而草稿还停在非法 op 时，显示回 keep。
            value={current && allowed.includes(current) ? current : 'keep'}
            onChange={(e) => setDraft(row.name, { op: e.target.value as ConstraintOp })}
            options={allowed.map((op) => ({
              value: op,
              label: t(`evolutionConsole.constraintOp.${op}`),
            }))}
          />
        );
      },
    },
  ];

  const constraintEditor = (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        {t('evolutionConsole.constraintAddTitle')}
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
        <Input
          aria-label="constraint-add-target"
          style={{ width: 220 }}
          placeholder={t('evolutionConsole.constraintAddTarget')}
          value={newConstraint.target_code}
          onChange={(e) => setNewConstraint((prev) => ({ ...prev, target_code: e.target.value }))}
        />
        <Input.TextArea
          aria-label="constraint-add-expression"
          style={{ flex: 1 }}
          rows={2}
          placeholder={t('evolutionConsole.constraintAddExpression')}
          value={newConstraint.expression}
          onChange={(e) => setNewConstraint((prev) => ({ ...prev, expression: e.target.value }))}
        />
      </div>
    </div>
  );

  /** mapping 编辑表：目标 / 源实体 / 列 / 四个语义槽（内联编辑）/ 状态 / 操作。 */
  const mappingSlotColumns: ColumnsType<MappingRow> = MAPPING_SLOTS.map((slot) => ({
    title: t(`evolutionConsole.mappingSlot.${slot}`),
    key: slot,
    width: 150,
    render: (_, row) => (
      <Input
        size="small"
        aria-label={`mapping-${slot}-${row.sourceCode}`}
        value={mappingDrafts[row.key]?.slots[slot] ?? ''}
        onChange={(e) => setMappingSlot(row.key, slot, e.target.value)}
      />
    ),
  }));

  const mappingColumns: ColumnsType<MappingRow> = [
    {
      title: t('evolutionConsole.mappingColTarget'),
      dataIndex: 'target_code',
      width: 150,
      render: (v: string) => <code>{v}</code>,
    },
    { title: t('evolutionConsole.mappingColSource'), dataIndex: 'entity_name', width: 110 },
    {
      title: t('evolutionConsole.mappingColColumn'),
      dataIndex: 'attribute_name',
      width: 120,
      render: (v: string) => <code>{v}</code>,
    },
    ...mappingSlotColumns,
    {
      title: t('evolutionConsole.mappingColEnabled'),
      key: 'enabled',
      width: 100,
      render: (_, row) => (row.enabled
        ? <Tag color="success">{t('evolutionConsole.constraintEnabledTag')}</Tag>
        : <Tag>{t('evolutionConsole.constraintDisabledTag')}</Tag>),
    },
    {
      title: t('evolutionConsole.mappingColOp'),
      key: 'op',
      width: 250,
      render: (_, row) => {
        const allowed = mappingOpsFor(row.enabled);
        const current = mappingDrafts[row.key]?.op;
        return (
          <Radio.Group
            size="small"
            optionType="button"
            value={current && allowed.includes(current) ? current : 'keep'}
            onChange={(e) => setMappingDraft(row.key, { op: e.target.value as MappingOp })}
            options={allowed.map((op) => ({
              value: op,
              label: t(`evolutionConsole.mappingOp.${op}`),
            }))}
          />
        );
      },
    },
  ];

  const mappingEditor = (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        {t('evolutionConsole.mappingAddTitle')}
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        <Select
          aria-label="mapping-add-target"
          style={{ width: 220 }}
          showSearch
          optionFilterProp="label"
          placeholder={t('evolutionConsole.mappingAddTarget')}
          value={newMapping.target_code || undefined}
          options={mappingAttributeOptions}
          onChange={pickMappingTarget}
        />
        <Select
          aria-label="mapping-add-column"
          style={{ width: 220 }}
          showSearch
          optionFilterProp="label"
          placeholder={t('evolutionConsole.mappingAddColumn')}
          value={newMapping.attribute_name || undefined}
          options={mappingColumnOptions}
          loading={compiledLoading}
          disabled={!newMapping.entity_name}
          onChange={(value: string) => setNewMapping((prev) => ({ ...prev, attribute_name: value }))}
        />
        {MAPPING_SLOTS.map((slot) => (
          <Input
            key={slot}
            aria-label={`mapping-add-${slot}`}
            style={{ width: 160 }}
            placeholder={t(`evolutionConsole.mappingSlot.${slot}`)}
            value={newMapping.slots[slot]}
            onChange={(e) => setNewMapping((prev) => ({
              ...prev,
              slots: { ...prev.slots, [slot]: e.target.value },
            }))}
          />
        ))}
      </div>
    </div>
  );

  /** relation 现有边表：边名 / 两端 / origin / 基数；勾选 = 按边名删除。 */
  const relationColumns: ColumnsType<CompiledRelationType> = [
    {
      title: t('evolutionConsole.relationEdgeCol'),
      key: 'edge',
      width: 300,
      ellipsis: true,
      render: (_, row) => (
        <Checkbox
          aria-label={`relation-edge-${row.name}`}
          checked={relationSelected.includes(row.name)}
          onChange={(e) => setRelationSelected((prev) => (e.target.checked
            ? [...prev, row.name]
            : prev.filter((name) => name !== row.name)))}
        >
          <code>{row.name}</code>
        </Checkbox>
      ),
    },
    {
      title: t('evolutionConsole.relationEndpointsCol'),
      key: 'endpoints',
      render: (_, row) => relationEdgeOf(row) ?? '-',
    },
    {
      title: t('evolutionConsole.relationOriginCol'),
      dataIndex: 'origin',
      width: 110,
      render: (v: string | null | undefined) => v || '-',
    },
    {
      title: t('evolutionConsole.relationCardinalityCol'),
      dataIndex: 'cardinality',
      width: 120,
      render: (v: string | null | undefined) => v || '-',
    },
  ];

  const relationSection = (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        {t('evolutionConsole.relationExistingTitle')}
      </div>
      <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 8px' }}>
        {t('evolutionConsole.relationExistingHint')}
      </p>
      {relationError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message={t('evolutionConsole.relationLoadFailed')}
          action={(
            <Button
              size="small"
              onClick={() => {
                if (kbSelected && dsSelected) loadRelations(kbSelected, dsSelected);
              }}
            >
              {t('common.retry')}
            </Button>
          )}
        />
      )}
      <Table<CompiledRelationType>
        rowKey="name"
        size="small"
        pagination={false}
        loading={relationLoading}
        dataSource={relationEdges}
        locale={{ emptyText: t('evolutionConsole.relationEmpty') }}
        scroll={{ x: 820 }}
        columns={relationColumns}
      />
    </div>
  );

  /** tool_schema 工具表：工具名 / 当前有效描述 / 新描述 / 停用勾选。 */
  const toolColumns: ColumnsType<EvolutionToolOption> = [
    {
      title: t('evolutionConsole.toolSchemaColName'),
      dataIndex: 'name',
      width: 200,
      render: (name: string, row) => (
        <>
          <code>{name}</code>
          {row.overridden && (
            <Tag style={{ marginLeft: 8 }}>{t('evolutionConsole.toolSchemaOverriddenTag')}</Tag>
          )}
        </>
      ),
    },
    {
      title: t('evolutionConsole.toolSchemaColDescription'),
      key: 'effective_description',
      render: (_, row) => (
        <span style={{ color: '#595959' }}>
          {row.effective_description || row.description || '-'}
        </span>
      ),
    },
    {
      title: t('evolutionConsole.toolSchemaColNewDescription'),
      key: 'description',
      render: (_, row) => (
        <Input
          aria-label={`tool-desc-${row.name}`}
          value={toolDrafts[row.name]?.description ?? ''}
          placeholder={t('evolutionConsole.toolSchemaDescriptionHint')}
          maxLength={256}
          showCount
          onChange={(e) => setToolDraft(row.name, { description: e.target.value })}
        />
      ),
    },
    {
      title: t('evolutionConsole.toolSchemaColDisabled'),
      key: 'disabled',
      width: 90,
      render: (_, row) => (
        <Checkbox
          aria-label={`tool-disable-${row.name}`}
          checked={toolDrafts[row.name]?.disabled ?? row.enabled === false}
          onChange={(e) => setToolDraft(row.name, { disabled: e.target.checked })}
        />
      ),
    },
  ];

  const toolSection = (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>
        {t('evolutionConsole.toolSchemaTitle')}
      </div>
      <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 8px' }}>
        {t('evolutionConsole.toolSchemaHint')}
      </p>
      {toolError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message={t('evolutionConsole.toolSchemaLoadFailed')}
          action={(
            <Button
              size="small"
              onClick={() => {
                if (kbSelected) loadToolOptions(kbSelected);
              }}
            >
              {t('common.retry')}
            </Button>
          )}
        />
      )}
      <Table<EvolutionToolOption>
        rowKey="name"
        size="small"
        pagination={false}
        loading={toolLoading}
        dataSource={toolOptions}
        locale={{ emptyText: t('evolutionConsole.toolSchemaEmpty') }}
        scroll={{ x: 780 }}
        columns={toolColumns}
      />
    </div>
  );

  return (
    <Modal
      open={open}
      title={t('evolutionConsole.startRunTitle')}
      onCancel={onClose}
      onOk={submit}
      confirmLoading={submitting}
      okText={t('evolutionConsole.startRun')}
      okButtonProps={{
        disabled: optionsLoading || optionsError
          || (kind === 'constraint' && constraintLoading)
          || (kind === 'mapping' && mappingLoading)
          || (kind === 'tool_schema' && toolLoading)
          // 工具名单加载失败时提交只会送出空 entries（得到「零变化」提示），
          // 直接禁用提交，等重试把名单拿回来。
          || (kind === 'tool_schema' && toolError),
      }}
      width={kind === 'mapping' ? 1100 : kind === 'constraint' ? 960 : kind === 'relation' ? 900 : kind === 'tool_schema' ? 1000 : undefined}
      destroyOnClose
    >
      <Alert type="warning" showIcon style={{ marginBottom: 12 }}
             message={t('evolutionConsole.startRunHint')} />
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          kind: 'alias',
          kb: DEFAULT_KB,
          ds: DEFAULT_JINPIN_DS,
          repeat: 3,
          dry_run: false,
          window_days: 7,
        }}
      >
        <Form.Item name="kind" label={t('evolutionConsole.formGoal')}
                   extra={t('evolutionConsole.formGoalHint')}>
          <Radio.Group>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {GOALS.map((goal) => (
                <Radio key={goal.kind} value={goal.kind}>
                  <span style={{ fontWeight: 600 }}>{t(goal.titleKey)}</span>
                  <div style={{ color: '#8c8c8c', fontSize: 12, marginTop: 2 }}>
                    {t(goal.descKey)}
                  </div>
                </Radio>
              ))}
            </div>
          </Radio.Group>
        </Form.Item>
        <Form.Item
          name="kb"
          label={t('evolutionConsole.formKb')}
          rules={[{ required: true, message: t('evolutionConsole.formKbRequired') }]}
          extra={
            optionsError ? (
              <span>
                {t('evolutionConsole.formKbOptionsError')}{' '}
                <Button type="link" size="small" style={{ padding: 0 }} onClick={loadOptions}>
                  {t('common.retry')}
                </Button>
              </span>
            ) : (
              t('evolutionConsole.formKbHint')
            )
          }
        >
          <Select
            showSearch
            loading={optionsLoading}
            optionFilterProp="label"
            placeholder={t('evolutionConsole.formKb')}
            options={kbPool.map((k) => ({ value: k.id, label: `${k.name}（${k.id}）` }))}
          />
        </Form.Item>
        {kind !== 'alias' && kind !== 'tool_schema' && kind !== 'synonym' && (
          <Form.Item
            name="ds"
            label={t('evolutionConsole.formDs')}
            rules={[{ required: true, message: t('evolutionConsole.formDsRequired') }]}
            extra={t('evolutionConsole.formDsHint')}
          >
            <Select
              showSearch
              optionFilterProp="label"
              placeholder={t('evolutionConsole.formDs')}
              options={dsPool.map((s) => ({ value: s.id, label: `${s.name}（${s.id}）` }))}
            />
          </Form.Item>
        )}
        {kind === 'relation' && relationSection}
        {kind === 'tool_schema' && toolSection}
        {kind === 'constraint' && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>
              {t('evolutionConsole.constraintTitle')}
            </div>
            <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 8px' }}>
              {t('evolutionConsole.constraintHint')}
            </p>
            {constraintError && (
              <Alert
                type="error"
                showIcon
                style={{ marginBottom: 8 }}
                message={t('evolutionConsole.constraintLoadFailed')}
                action={(
                  <Button
                    size="small"
                    onClick={() => {
                      if (kbSelected && dsSelected) loadConstraints(kbSelected, dsSelected);
                    }}
                  >
                    {t('common.retry')}
                  </Button>
                )}
              />
            )}
            <Table<CompiledConstraint>
              rowKey="name"
              size="small"
              pagination={false}
              loading={constraintLoading}
              dataSource={constraints}
              locale={{ emptyText: t('evolutionConsole.constraintEmpty') }}
              columns={constraintColumns}
            />
            {constraintEditor}
          </div>
        )}
        {kind === 'mapping' && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>
              {t('evolutionConsole.mappingTitle')}
            </div>
            <p style={{ color: '#8c8c8c', fontSize: 12, margin: '0 0 8px' }}>
              {t('evolutionConsole.mappingHint')}
            </p>
            {mappingError && (
              <Alert
                type="error"
                showIcon
                style={{ marginBottom: 8 }}
                message={t('evolutionConsole.mappingLoadFailed')}
                action={(
                  <Button
                    size="small"
                    onClick={() => {
                      if (kbSelected) loadAbstractSchema(kbSelected);
                    }}
                  >
                    {t('common.retry')}
                  </Button>
                )}
              />
            )}
            <Table<MappingRow>
              rowKey="key"
              size="small"
              pagination={false}
              loading={mappingLoading}
              dataSource={mappingRows}
              locale={{ emptyText: t('evolutionConsole.mappingEmpty') }}
              scroll={{ x: 1260 }}
              columns={mappingColumns}
            />
            {mappingEditor}
          </div>
        )}
        <Form.Item name="repeat" label={t('evolutionConsole.formRepeat')} normalize={(v) => v ?? undefined}>
          <InputNumber min={1} max={10} />
        </Form.Item>
        <Collapse
          ghost
          style={{ marginBottom: 8 }}
          items={[{
            key: 'advanced',
            label: t('evolutionConsole.advanced'),
            children: (
              <Form.Item name="dry_run" label={t('evolutionConsole.formDryRun')}
                         valuePropName="checked" extra={t('evolutionConsole.formDryRunHint')}>
                <Switch />
              </Form.Item>
            ),
          }]}
        />
        <Form.Item
          name="target_cases"
          label={(
            <Space size={8}>
              <span>{t('evolutionConsole.formTargetCases')}</span>
              <Button size="small"
                      onClick={() => { newCaseForm.resetFields(); setNewCaseOpen(true); }}
                      disabled={!kbSelected}>
                {t('evolutionConsole.newCase')}
              </Button>
              <span style={{ color: '#8c8c8c', fontSize: 12 }}>
                {t('evolutionConsole.formTargetCasesCount', {
                  count: cases.filter((c) => !kbSelected || !c.kb_id || c.kb_id === kbSelected).length,
                })}
              </span>
            </Space>
          )}
          extra={t('evolutionConsole.formTargetCasesHint')}
        >
          <Select
            mode="multiple"
            allowClear
            optionFilterProp="label"
            options={cases
              .filter((c) => !kbSelected || !c.kb_id || c.kb_id === kbSelected)
              .map((c) => ({
                value: c.name,
                label: t('evolutionConsole.formTargetCaseOption', {
                  question: c.question,
                  name: c.name,
                  tier: t(`evolutionConsole.tier.${c.tier}`, { defaultValue: c.tier }),
                }),
              }))}
          />
        </Form.Item>
        {(kind === 'alias' || kind === 'synonym') && (
          <Form.Item name="window_days" label={t('evolutionConsole.formWindowDays')} normalize={(v) => v ?? undefined}>
            <InputNumber min={1} max={90} />
          </Form.Item>
        )}
        {kind === 'query_rule' && (
          <Form.Item name="picks" label={t('evolutionConsole.formPicks')}
                     extra={t('evolutionConsole.formPicksHint')}>
            <Input placeholder="SysRole.f_category, Jobs.f_status" />
          </Form.Item>
        )}
      </Form>

      <Modal
        open={newCaseOpen}
        title={t('evolutionConsole.newCaseTitle')}
        okText={t('common.ok')}
        cancelText={t('common.cancel')}
        confirmLoading={caseCreating}
        onCancel={() => setNewCaseOpen(false)}
        onOk={async () => {
          let v: { question: string; control?: string; expect: 'answer' | 'refuse';
            tier?: string; ds?: string; name?: string };
          try {
            v = await newCaseForm.validateFields();
          } catch (e) {
            if ((e as { errorFields?: unknown })?.errorFields) return;
            throw e;
          }
          if (!kbSelected || !v.ds) {
            message.warning(t('evolutionConsole.newCaseNeedDs'));
            return;
          }
          const control = (v.control || '')
            .split('\n').map((x) => x.trim()).filter(Boolean);
          if (v.expect === 'answer' && control.length === 0) {
            message.warning(t('evolutionConsole.newCaseNeedControl'));
            return;
          }
          setCaseCreating(true);
          try {
            const name = (v.name || '').trim()
              || `ui_${Date.now().toString(36)}`;
            await createCorpusCase({
              name,
              question: v.question.trim(),
              expect: v.expect,
              kb_id: kbSelected,
              ds_id: v.ds,
              tier: v.tier || 'core',
              control: v.expect === 'answer' ? control : null,
            });
            const res = await listCorpusCases();
            setCases(res.cases);
            const picked = new Set<string>(form.getFieldValue('target_cases') || []);
            picked.add(name);
            form.setFieldsValue({ target_cases: Array.from(picked) });
            message.success(t('evolutionConsole.newCaseCreated', { name }));
            setNewCaseOpen(false);
            newCaseForm.resetFields();
          } catch (e: unknown) {
            message.error(e instanceof Error ? e.message : t('common.error'));
          } finally {
            setCaseCreating(false);
          }
        }}
      >
        <Form form={newCaseForm} layout="vertical"
              initialValues={{ expect: 'answer', tier: 'core' }}>
          <Form.Item name="question" label={t('evolutionConsole.newCaseQuestion')}
                     rules={[{ required: true, message: t('evolutionConsole.newCaseQuestion') }]}>
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="expect" label={t('evolutionConsole.newCaseExpect')}>
            <Select options={[
              { value: 'answer', label: t('evolutionConsole.newCaseExpectAnswer') },
              { value: 'refuse', label: t('evolutionConsole.newCaseExpectRefuse') },
            ]} />
          </Form.Item>
          <Form.Item name="control" label={t('evolutionConsole.newCaseControl')}
                     extra={t('evolutionConsole.newCaseControlHint')}>
            <Input.TextArea rows={3} placeholder="SELECT ..." />
          </Form.Item>
          <Form.Item name="ds" label={t('evolutionConsole.newCaseDs')}
                     rules={[{ required: true, message: t('evolutionConsole.newCaseNeedDs') }]}>
            <Select
              showSearch
              optionFilterProp="label"
              loading={caseDsLoading}
              placeholder={t('evolutionConsole.newCaseDs')}
              options={caseDsOptions.map((s) => ({ value: s.id, label: `${s.name}（${s.id}）` }))}
            />
          </Form.Item>
          <Form.Item name="tier" label={t('evolutionConsole.newCaseTier')}>
            <Select options={['core', 'single_table', 'cross_table', 'filtered', 'ambiguous']
              .map((x) => ({ value: x, label: t(`evolutionConsole.tier.${x}`, { defaultValue: x }) }))} />
          </Form.Item>
          <Form.Item name="name" label={t('evolutionConsole.newCaseName')}
                     extra={t('evolutionConsole.newCaseNameHint')}>
            <Input placeholder="ui_xxxx" />
          </Form.Item>
        </Form>
      </Modal>
    </Modal>
  );
}
