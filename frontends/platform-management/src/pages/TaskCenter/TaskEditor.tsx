import { useCallback, useEffect, useMemo, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Divider,
  Input,
  InputNumber,
  Result,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  theme,
  message,
} from 'antd';
import { DeleteOutlined, MinusOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import { PageContent, PageHeader, usePermission } from '@jonex/shared-lib';
import type { ApiError } from '@jonex/shared-lib';
import {
  TASK_CENTER_READ_PERMISSION,
  TASK_CENTER_WRITE_PERMISSION,
} from '../../api/taskCenter';
import {
  getDefinition,
  getDefinitionVersion,
  listCatalogSteps,
  saveOrchestration,
} from '../../api/taskCenterDefinitions';
import type {
  FieldSpec,
  FlowConfig,
  FlowStep,
  OrchestrationSaveStatus,
  TaskCatalogStep,
  TaskDefinitionDetail,
} from '../../api/taskCenterDefinitions';
import { TASK_CENTER_LIST_PATH, taskExecutionsPath } from './paths';
import AddStepPopover from './AddStepPopover';
import WorkflowNodeIcon from './WorkflowNodeIcon';

/**
 * 「编辑任务」页：中间画布是纵向编排，右侧是选中节点的配置面板。
 *
 * 读写都对齐后端的编排契约（`task_definition_versions.config`）：
 * - 画布上的顺序就是 `steps[]`，串行链写成 `depends_on` / `next_steps` 的对偶；
 * - 每个步骤吃哪些字段由内置步骤清单（`GET /task-center/steps`）决定，只绑「来源」；
 * - 结束节点只**声明**产出变量，不引用上游 —— 引用写在步骤的 `bind_inputs` 里。
 *
 * 保存走 `PUT /orchestration`：存草稿或存并发布是一个接口的两种 `status`，
 * 后端按「最新版本是不是草稿」决定原地改还是新开一版。
 */

/** 编辑器里的一个流程入参。数组而不是对象：名字要能边输入边改，对象键做不到。 */
interface StartInput {
  name: string;
  spec: FieldSpec;
}

/** 编辑器里的一个结束节点产出声明（只声明，不引用上游）。 */
interface EndOutput {
  name: string;
  spec: FieldSpec;
}

/** 编辑器里的一个步骤。页面态，保存时再展开成 `FlowStep`。 */
interface StepState {
  nodeId: string;
  stepCode: string;
  timeoutSeconds: number;
  maxRetries: number;
  onRetryExhausted: 'terminate' | 'skip';
  /** catalog 字段名 → 绑定表达式（`@task.inputs.x` / `@node.outputs.y`）；空 = 未绑定 */
  bindings: Record<string, string>;
}

const TASK_INPUT_PREFIX = '@task.inputs.';

/**
 * 开始入参 / 结束产出的可选类型：契约允许的六种都给。
 *
 * `object` / `array` 走**抽象声明**（`abstract: true`）—— 编辑器没有子结构录入界面，
 * 而 N17 只对非抽象的 `object` / `array` 要求 `properties` / `items`，
 * 抽象形态在文档里就是 `Object` / `Array<Object>`。
 */
const DECLARABLE_TYPES = ['string', 'number', 'boolean', 'file', 'object', 'array'];

/**
 * 变量名的合法格式，与后端引用解析器同口径（`expressions.py` 的 `_SEGMENT_RE`）：
 * 以字母或下划线开头，之后只允许字母、数字、下划线、连字符。
 * 名字不合法时 `@task.inputs.<name>` 解析不出来，发版才会被拒 —— 提前在这里拦。
 */
const VARIABLE_NAME_RE = /^[A-Za-z_][A-Za-z0-9_-]*$/;

/** 抽象类型标记：不展开子字段的 object / array 必须显式写 `abstract: true`（N17 的豁免条件）。 */
const isAbstractType = (type: string): boolean => type === 'object' || type === 'array';

/** 切换类型时整份重建 spec：不把上一个类型的 `abstract` / `properties` / `items` 留下来。 */
const fieldSpecFor = (type: string, required = false): FieldSpec => ({
  type,
  ...(required ? { required: true } : {}),
  ...(isAbstractType(type) ? { abstract: true } : {}),
});

/** 类型的展示文案：文档口径把抽象形态写作 `Object` / `Array<Object>`。 */
const fieldTypeLabel = (spec: Pick<FieldSpec, 'type' | 'abstract'>): string => {
  if (spec.type === 'object' && spec.abstract) return 'Object';
  if (spec.type === 'array' && spec.abstract) return 'Array<Object>';
  return spec.type;
};

/**
 * N12 的类型分桶：`file` / 数组 / 其余都算标量。
 * 与后端 `_type_bucket` 同一套口径 —— 绑定下拉按桶过滤，减少「选得上但存不进」。
 */
const bucketOf = (type: string | undefined): 'file' | 'array' | 'scalar' =>
  type === 'file' ? 'file' : type === 'array' ? 'array' : 'scalar';

const isNotFound = (e: unknown): boolean => (e as { status?: number } | undefined)?.status === 404;

/** 保存被拒时后端把校验问题放在 `error_details.issues`，逐条展示比一句「保存失败」有用。 */
function saveErrorMessage(e: unknown, fallback: string): string {
  const issues = (e as ApiError).errorDetails?.issues;
  if (Array.isArray(issues) && issues.length > 0) return issues.join('；');
  return e instanceof Error && e.message ? e.message : fallback;
}

/**
 * 某个步骤最终生效的输入绑定。
 *
 * 知识编译的输入**不自由绑定**：`chunks` / `document_id` 固定取「文档解析」的产出，
 * `knowledge_id` 沿用解析那一步的来源（通常就是流程入参）。展示、校验、保存都用这份
 * 推导结果，解析节点改来源时编译自动跟着变，不会出现两处不一致。
 */
function bindingsFor(steps: StepState[], step: StepState): Record<string, string> {
  if (step.stepCode !== 'compile') return step.bindings;
  const parseStep = steps.find((item) => item.stepCode === 'parse');
  if (!parseStep) return step.bindings;
  return {
    chunks: `@${parseStep.nodeId}.outputs.chunks`,
    document_id: `@${parseStep.nodeId}.outputs.document_id`,
    // 旧契约里知识库入参叫 knowledge_base_id，运行时由 `_fill_inputs` 归一化；
    // 编译沿用解析那一步的来源，两种键名都认
    knowledge_id:
      parseStep.bindings.knowledge_id ?? parseStep.bindings.knowledge_base_id ?? '',
  };
}

/**
 * 页面态 → 后端契约。**不做校验**：脏检查与保存共用这一份序列化，
 * 才能保证「载入时的快照」和「此刻的配置」用同一把尺子比较。
 */
function serializeConfig(
  startInputs: StartInput[],
  steps: StepState[],
  endOutputs: EndOutput[],
): FlowConfig {
  const nodeIds = steps.map((step) => step.nodeId);
  const flowSteps: FlowStep[] = steps.map((step, index) => ({
    node_id: step.nodeId,
    step_code: step.stepCode,
    // 画布是单链：上一个节点是唯一依赖，下一个节点是唯一后继
    depends_on: index === 0 ? [] : [nodeIds[index - 1]],
    next_steps: index === nodeIds.length - 1 ? [] : [nodeIds[index + 1]],
    sort: index + 1,
    timeout_seconds: step.timeoutSeconds,
    max_retries: step.maxRetries,
    on_retry_exhausted: step.onRetryExhausted,
    bind_inputs: Object.fromEntries(
      Object.entries(bindingsFor(steps, step)).filter(([, value]) => value && value.trim()),
    ),
  }));

  return {
    start: {
      node_id: 'start',
      inputs: Object.fromEntries(
        startInputs.map((item) => [item.name.trim(), { ...item.spec, required: item.spec.required === true }]),
      ),
    },
    steps: flowSteps,
    end: {
      node_id: 'end',
      outputs: Object.fromEntries(
        endOutputs.map((item) => [item.name.trim(), { ...item.spec, required: item.spec.required === true }]),
      ),
    },
  };
}

export default function TaskEditor() {
  const { t } = useTranslation();
  const { definitionId } = useParams<{ definitionId?: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { hasPerm } = usePermission();
  const canRead = hasPerm(TASK_CENTER_READ_PERMISSION);
  const canWrite = hasPerm(TASK_CENTER_WRITE_PERMISSION);
  const { token } = theme.useToken();

  /** 路由 state 里的名字：接口回来前先用它，标题不闪 */
  const stateName = (location.state as { definitionName?: string } | null)?.definitionName;

  const [detail, setDetail] = useState<TaskDefinitionDetail | null>(null);
  const [catalog, setCatalog] = useState<TaskCatalogStep[]>([]);
  const [startInputs, setStartInputs] = useState<StartInput[]>([]);
  const [steps, setSteps] = useState<StepState[]>([]);
  const [endOutputs, setEndOutputs] = useState<EndOutput[]>([]);
  /** 当前载入的是哪一版的草稿/发布状态，用来决定「存草稿」还是「发新版」的提示口径 */
  const [versionStatus, setVersionStatus] = useState<'draft' | 'published' | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [saving, setSaving] = useState<OrchestrationSaveStatus | null>(null);
  /** 最近一次载入 / 保存成功时的配置快照（序列化后的字符串），用来判断有没有未保存的修改 */
  const [savedSnapshot, setSavedSnapshot] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string>('start');

  const definition = detail?.definition ?? null;
  const readOnly = definition?.scope === 'system';
  const locked = !canWrite || readOnly;

  /**
   * 拉取定义与编排。
   *
   * `silent` 用于保存后的回读：保存已经在按钮上转圈了，再把整页切成 Spin 会闪一下；
   * 静默模式也不重置选中节点与错误态，用户停在哪个节点就还在哪个节点。
   */
  const load = useCallback(async (options?: { silent?: boolean }): Promise<void> => {
    const silent = options?.silent === true;
    if (!definitionId) {
      setNotFound(true);
      setLoading(false);
      return;
    }
    if (!silent) {
      setLoading(true);
      setLoadError(null);
      setNotFound(false);
    }
    try {
      // 清单与定义各自独立，一次并发拿完：清单是静态的，晚到只会拖慢首屏
      const [loaded, loadedCatalog] = await Promise.all([getDefinition(definitionId), listCatalogSteps()]);
      let config: FlowConfig | null = null;
      let status: 'draft' | 'published' | null = null;
      // 优先草稿：有未发布的改动时，编辑器要接着那版改，而不是展示线上版本
      const versionNo = loaded.draft_version_no ?? loaded.definition.current_version_no;
      if (versionNo) {
        const version = await getDefinitionVersion(definitionId, versionNo);
        config = version.config;
        status = version.status;
      }
      const nextStart = Object.entries(config?.start?.inputs ?? {}).map(([name, spec]) => ({ name, spec }));
      const nextSteps: StepState[] = [...(config?.steps ?? [])]
        .sort((a, b) => (a.sort ?? 0) - (b.sort ?? 0))
        .map((step) => ({
          nodeId: step.node_id,
          stepCode: step.step_code,
          timeoutSeconds: step.timeout_seconds,
          maxRetries: step.max_retries ?? 0,
          onRetryExhausted: step.on_retry_exhausted ?? 'terminate',
          bindings: Object.fromEntries(
            Object.entries(step.bind_inputs ?? {}).filter(([, value]) => typeof value === 'string'),
          ) as Record<string, string>,
        }));
      const nextEnd = Object.entries(config?.end?.outputs ?? {}).map(([name, spec]) => ({ name, spec }));
      setDetail(loaded);
      setCatalog(loadedCatalog);
      setVersionStatus(status);
      setStartInputs(nextStart);
      setSteps(nextSteps);
      setEndOutputs(nextEnd);
      // 基线用同一份序列化算：载入态的「脏」从此为 false，改任何一个配置项才变 true
      setSavedSnapshot(JSON.stringify(serializeConfig(nextStart, nextSteps, nextEnd)));
      if (!silent) setSelectedKey('start');
    } catch (e: unknown) {
      // 静默回读失败不改界面：保存本身已经成功，页面上的数据仍是刚提交的那份
      if (silent) return;
      if (isNotFound(e)) setNotFound(true);
      // 只存原始信息、不在这里拼文案：把 t 放进 deps 会让 load 每次渲染都换引用，effect 自激
      else setLoadError(e instanceof Error ? e.message : '');
    } finally {
      if (!silent) setLoading(false);
    }
  }, [definitionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const catalogByCode = useMemo(
    () => Object.fromEntries(catalog.map((step) => [step.step_code, step])) as Record<string, TaskCatalogStep>,
    [catalog],
  );

  /** 步骤名的本地化：已知 step_code 用本地文案，未知的回落到清单里的名字 */
  const stepLabel = useCallback(
    (stepCode: string): string => {
      const key = `taskCenter.editor.node.${stepCode}`;
      const label = t(key);
      return label === key ? (catalogByCode[stepCode]?.name ?? stepCode) : label;
    },
    [t, catalogByCode],
  );

  const selectedStep = steps.find((step) => step.nodeId === selectedKey);
  const selectedIndex = steps.findIndex((step) => step.nodeId === selectedKey);

  /** 添加菜单的门禁，与真正插入时用的是同一个函数 */
  const blockedReason = useCallback(
    (stepCode: string): 'added' | 'needParse' | null => {
      if (steps.some((step) => step.stepCode === stepCode)) return 'added';
      // 编译读的是解析产物：没有解析节点时先加编译，编排必然跑不通
      if (stepCode !== 'parse' && !steps.some((step) => step.stepCode === 'parse')) return 'needParse';
      return null;
    },
    [steps],
  );

  /** 按字段名做一次「同名同桶」的默认绑定：照种子里那条链的形态，省掉大多数手工下拉 */
  const suggestBindings = useCallback(
    (stepCode: string, priorSteps: StepState[], inputs: StartInput[]): Record<string, string> => {
      const schema = catalogByCode[stepCode]?.input_schema ?? {};
      const bindings: Record<string, string> = {};
      for (const [field, spec] of Object.entries(schema)) {
        const matchedInput = inputs.find((item) => item.name === field);
        if (matchedInput && bucketOf(matchedInput.spec.type) === bucketOf(spec.type)) {
          bindings[field] = `${TASK_INPUT_PREFIX}${field}`;
          continue;
        }
        for (const prior of [...priorSteps].reverse()) {
          const output = catalogByCode[prior.stepCode]?.output_schema?.[field];
          if (output && bucketOf(output.type) === bucketOf(spec.type)) {
            bindings[field] = `@${prior.nodeId}.outputs.${field}`;
            break;
          }
        }
      }
      return bindings;
    },
    [catalogByCode],
  );

  const addStep = (stepCode: string): void => {
    if (blockedReason(stepCode)) return;
    const catalogStep = catalogByCode[stepCode];
    if (!catalogStep) return;
    let index = steps.filter((step) => step.stepCode === stepCode).length + 1;
    let nodeId = `${stepCode}_${index}`;
    const used = new Set(steps.map((step) => step.nodeId));
    while (used.has(nodeId)) nodeId = `${stepCode}_${++index}`;
    const node: StepState = {
      nodeId,
      stepCode,
      timeoutSeconds: catalogStep.timeout_seconds,
      maxRetries: 0,
      onRetryExhausted: 'terminate',
      bindings: suggestBindings(stepCode, steps, startInputs),
    };
    setSteps((prev) => [...prev, node]);
    setSelectedKey(nodeId);
  };

  const removeStep = (nodeId: string): void => {
    const target = steps.find((step) => step.nodeId === nodeId);
    if (!target) return;
    // 解析是编译的前提：删掉解析后，后面的步骤全部失去输入 —— 整条链一起清，别留下跑不通的编排
    const removedIds = new Set(
      target.stepCode === 'parse' ? steps.map((step) => step.nodeId) : [nodeId],
    );
    setSteps((prev) =>
      prev
        .filter((step) => !removedIds.has(step.nodeId))
        .map((step) => ({
          ...step,
          bindings: Object.fromEntries(
            Object.entries(step.bindings).filter(([, value]) => {
              // 绑定指向被删节点时一并清掉：留着就是一条永远解析不了的引用
              const match = /^@([^.]+)\.outputs\./.exec(value);
              return !match || !removedIds.has(match[1]);
            }),
          ),
        })),
    );
    setSelectedKey((current) => (removedIds.has(current) ? 'start' : current));
  };

  const updateStep = (nodeId: string, patch: Partial<StepState>): void => {
    setSteps((prev) => prev.map((step) => (step.nodeId === nodeId ? { ...step, ...patch } : step)));
  };

  /** 改开始入参名：步骤里的 `@task.inputs.old` 跟着改成新名，否则引用悬空 */
  const renameStartInput = (index: number, nextName: string): void => {
    const oldName = startInputs[index]?.name;
    if (oldName === undefined) return;
    setStartInputs((prev) => prev.map((item, i) => (i === index ? { ...item, name: nextName } : item)));
    if (oldName === nextName) return;
    const oldRef = `${TASK_INPUT_PREFIX}${oldName}`;
    const nextRef = `${TASK_INPUT_PREFIX}${nextName}`;
    setSteps((prev) =>
      prev.map((step) => ({
        ...step,
        bindings: Object.fromEntries(
          Object.entries(step.bindings).map(([field, value]) => [field, value === oldRef ? nextRef : value]),
        ),
      })),
    );
  };

  const updateStartSpec = (index: number, patch: Partial<FieldSpec>): void => {
    setStartInputs((prev) =>
      prev.map((item, i) => (i === index ? { ...item, spec: { ...item.spec, ...patch } } : item)),
    );
  };

  /** 改类型用整份替换：只在旧 spec 上合并会把上一个类型的 abstract 留下来 */
  const replaceStartSpec = (index: number, spec: FieldSpec): void => {
    setStartInputs((prev) => prev.map((item, i) => (i === index ? { ...item, spec } : item)));
  };

  const removeStartInput = (index: number): void => {
    const removed = startInputs[index];
    setStartInputs((prev) => prev.filter((_, i) => i !== index));
    if (!removed?.name) return;
    const removedRef = `${TASK_INPUT_PREFIX}${removed.name}`;
    setSteps((prev) =>
      prev.map((step) => ({
        ...step,
        bindings: Object.fromEntries(
          Object.entries(step.bindings).filter(([, value]) => value !== removedRef),
        ),
      })),
    );
  };

  /**
   * 改结束节点的一行。`spec` 给了就整份替换：类型切换必须丢掉上一个类型的
   * `abstract` / `properties` / `items`，合并写法会把它们留下来。
   */
  const updateEndOutput = (index: number, patch: { name?: string; spec?: FieldSpec }): void => {
    setEndOutputs((prev) =>
      prev.map((item, i) =>
        i === index
          ? {
              ...item,
              ...(patch.name !== undefined ? { name: patch.name } : {}),
              ...(patch.spec !== undefined ? { spec: patch.spec } : {}),
            }
          : item,
      ),
    );
  };

  /** 保存前的本地校验 + 组装成后端契约。返回 null 表示已拦下并提示过用户。 */
  const buildConfig = (): FlowConfig | null => {
    // 变量名三查：非空 → 格式（与后端 `_SEGMENT_RE` 同口径）→ 不重复。
    // 格式不合法时绑定表达式 `@task.inputs.<name>` 解析不出来，不能在发版时才暴露。
    const checkNames = (names: string[]): boolean => {
      if (names.some((name) => !name)) {
        message.error(t('taskCenter.editor.field.nameRequired'));
        return false;
      }
      if (names.some((name) => !VARIABLE_NAME_RE.test(name))) {
        message.error(t('taskCenter.editor.field.nameInvalid'));
        return false;
      }
      if (new Set(names).size !== names.length) {
        message.error(t('taskCenter.editor.field.nameDuplicate'));
        return false;
      }
      return true;
    };
    if (!checkNames(startInputs.map((item) => item.name.trim()))) return null;
    if (!checkNames(endOutputs.map((item) => item.name.trim()))) return null;
    if (steps.length === 0) {
      message.error(t('taskCenter.editor.step.required'));
      return null;
    }
    // 必填入参没绑来源，运行期会拿到空值：拦在保存前，别让它变成一条跑不通的版本
    for (const step of steps) {
      const schema = catalogByCode[step.stepCode]?.input_schema ?? {};
      const bindings = bindingsFor(steps, step);
      const missing = Object.entries(schema).find(
        ([field, spec]) => spec.required && !bindings[field]?.trim(),
      );
      if (missing) {
        // 提示用「步骤名 + 字段名」而不是 node_id：画布上看到的是步骤名，node_id 只存在于数据里；
        // 同时把右侧面板切到这一步，用户不用自己去找是哪张卡
        message.error(
          t('taskCenter.editor.field.bindRequired', {
            step: stepLabel(step.stepCode),
            field: missing[0],
          }),
        );
        setSelectedKey(step.nodeId);
        return null;
      }
    }

    return serializeConfig(startInputs, steps, endOutputs);
  };

  const submit = async (status: OrchestrationSaveStatus): Promise<void> => {
    if (!definitionId) return;
    const config = buildConfig();
    if (!config) return;
    setSaving(status);
    try {
      await saveOrchestration(definitionId, config, status);
      message.success(
        t(status === 'published' ? 'taskCenter.editor.action.publishSuccess' : 'taskCenter.editor.action.saveSuccess'),
      );
      // 保存会推进版本号（或原地改草稿），静默回读让页面态与库里一致 —— 不切整页 Spin，
      // 按钮自己的 loading 一直转到这一步结束
      await load({ silent: true });
    } catch (e: unknown) {
      message.error(
        saveErrorMessage(
          e,
          t(status === 'published' ? 'taskCenter.editor.action.publishFailed' : 'taskCenter.editor.action.saveFailed'),
        ),
      );
    } finally {
      setSaving(null);
    }
  };

  /** 某个步骤可选的来源：任务入参 + 它之前的步骤产出，按 N12 的类型桶过滤 */
  const bindingOptions = useCallback(
    (stepIndex: number, target: FieldSpec) => {
      const bucket = bucketOf(target.type);
      const fromInputs = startInputs
        .filter((item) => item.name && bucketOf(item.spec.type) === bucket)
        .map((item) => ({
          value: `${TASK_INPUT_PREFIX}${item.name}`,
          label: `${t('taskCenter.editor.section.input')} · ${item.name}`,
        }));
      const fromSteps = steps.slice(0, stepIndex).flatMap((prior) =>
        Object.entries(catalogByCode[prior.stepCode]?.output_schema ?? {})
          .filter(([, spec]) => bucketOf(spec.type) === bucket)
          .map(([field]) => ({
            value: `@${prior.nodeId}.outputs.${field}`,
            label: `${stepLabel(prior.stepCode)} · ${prior.nodeId}.${field}`,
          })),
      );
      return [...fromInputs, ...fromSteps];
    },
    [startInputs, steps, catalogByCode, stepLabel, t],
  );

  // 路由守卫在两个壳里不等价（standalone 的 basename 带尾斜杠，拦不住），页面自己兜一道
  if (!canRead) return <Result status="403" title="403" subTitle={t('error.403')} />;

  if (loading) {
    return (
      <>
        <PageHeader title={stateName ?? t('taskCenter.editor.title')} backTo={definitionId ? taskExecutionsPath(definitionId) : TASK_CENTER_LIST_PATH} />
        <PageContent>
          <Spin />
        </PageContent>
      </>
    );
  }

  if (notFound) {
    return (
      <Result
        status="404"
        title={t('taskCenter.editor.notFound')}
        extra={
          <Button type="primary" onClick={() => navigate(TASK_CENTER_LIST_PATH)}>
            {t('taskCenter.executions.backToList')}
          </Button>
        }
      />
    );
  }

  if (loadError) {
    return (
      <Result
        status="error"
        title={t('taskCenter.editor.loadFailed')}
        subTitle={loadError || t('taskCenter.editor.loadFailed')}
        extra={<Button onClick={() => void load()}>{t('common.retry')}</Button>}
      />
    );
  }

  const backTo = definitionId ? taskExecutionsPath(definitionId) : TASK_CENTER_LIST_PATH;

  /** 区块标题：黑色小标题，可挂一个灰色说明标签（如「只读」） */
  const renderSectionTitle = (title: string, tag?: string): ReactNode => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '2px 0 10px' }}>
      <Typography.Text strong style={{ fontSize: 13 }}>
        {title}
      </Typography.Text>
      {tag && (
        <Tag
          variant="filled"
          style={{
            marginInlineEnd: 0,
            fontSize: 11,
            background: token.colorFillTertiary,
            color: token.colorTextSecondary,
          }}
        >
          {tag}
        </Tag>
      )}
    </div>
  );

  const renderTableHeader = (labels: string[], template: string): ReactNode => (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: template,
        columnGap: 8,
        fontSize: 12,
        color: token.colorTextSecondary,
        paddingBottom: 6,
      }}
    >
      {labels.map((label, index) => (
        <span key={`${label}-${index}`} style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {label}
        </span>
      ))}
    </div>
  );

  /**
   * 类型一律用浅灰 Tag，和变量名区分开：名字是黑色正文，类型是元信息。
   * 抽象对象按文档口径显示 `Object` / `Array<Object>`，而不是裸的 `object` / `array`。
   */
  const renderTypeTag = (spec: Pick<FieldSpec, 'type' | 'abstract'>): ReactNode => (
    <Tag
      variant="filled"
      style={{
        marginInlineEnd: 0,
        background: token.colorFillTertiary,
        color: token.colorTextSecondary,
        fontSize: 11,
        lineHeight: '18px',
        justifyContent: 'center',
      }}
    >
      {fieldTypeLabel(spec)}
    </Tag>
  );

  /** 开始节点：流程入参，可增删改类型与必填 */
  const renderStartPanel = (): ReactNode => (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <WorkflowNodeIcon kind="start" size={20} />
        <Typography.Text strong style={{ fontSize: 13 }}>
          {t('taskCenter.editor.section.input')}
        </Typography.Text>
        <Button
          size="small"
          type="text"
          icon={<PlusOutlined />}
          aria-label={t('taskCenter.editor.action.addInput')}
          disabled={locked}
          onClick={() =>
            setStartInputs((prev) => [
              ...prev,
              { name: '', spec: { type: 'string', required: true } },
            ])
          }
          style={{ marginLeft: 'auto', color: token.colorPrimary, background: token.colorPrimaryBg }}
        />
      </div>

      {renderTableHeader(
        [
          t('taskCenter.editor.field.varName'),
          t('taskCenter.editor.field.varType'),
          t('taskCenter.editor.field.required'),
          '',
        ],
        '1fr 104px 40px 28px',
      )}

      {startInputs.length === 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {t('taskCenter.editor.empty')}
        </Typography.Text>
      )}

      {startInputs.map((item, index) => {
        const missing = item.name.trim() === '';
        const duplicated =
          !missing && startInputs.filter((other) => other.name.trim() === item.name.trim()).length > 1;
        const invalid = !missing && !VARIABLE_NAME_RE.test(item.name.trim());
        return (
          <div key={`start-${index}`} style={{ marginBottom: 10 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 104px 40px 28px', gap: 8, alignItems: 'center' }}>
              <Input
                size="small"
                disabled={locked}
                status={missing || duplicated || invalid ? 'error' : undefined}
                value={item.name}
                placeholder={t('taskCenter.editor.field.varNamePlaceholder')}
                onChange={(event) => renameStartInput(index, event.target.value)}
              />
              <Select
                size="small"
                disabled={locked}
                value={item.spec.type}
                options={DECLARABLE_TYPES.map((type) => ({ value: type, label: type }))}
                onChange={(value) => replaceStartSpec(index, fieldSpecFor(value, item.spec.required === true))}
              />
              <Checkbox
                disabled={locked}
                checked={item.spec.required === true}
                onChange={(event) => updateStartSpec(index, { required: event.target.checked })}
              />
              <Button
                size="small"
                type="text"
                disabled={locked}
                icon={<MinusOutlined />}
                aria-label={t('taskCenter.editor.action.removeInput')}
                onClick={() => removeStartInput(index)}
              />
            </div>
            {missing && (
              <Typography.Text type="danger" style={{ fontSize: 11 }}>
                {t('taskCenter.editor.field.nameRequired')}
              </Typography.Text>
            )}
            {duplicated && (
              <Typography.Text type="danger" style={{ fontSize: 11 }}>
                {t('taskCenter.editor.field.nameDuplicate')}
              </Typography.Text>
            )}
            {invalid && (
              <Typography.Text type="danger" style={{ fontSize: 11 }}>
                {t('taskCenter.editor.field.nameInvalid')}
              </Typography.Text>
            )}
          </div>
        );
      })}
    </div>
  );

  /** 结束节点：只声明产出变量。用谁的值由各个步骤的输入绑定决定，这里不重复表达。 */
  const renderEndPanel = (): ReactNode => (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <WorkflowNodeIcon kind="end" size={20} />
        <Typography.Text strong style={{ fontSize: 13 }}>
          {t('taskCenter.editor.section.outputShort')}
        </Typography.Text>
        <Button
          type="text"
          size="small"
          icon={<PlusOutlined />}
          disabled={locked}
          aria-label={t('taskCenter.editor.action.addOutput')}
          onClick={() => setEndOutputs((prev) => [...prev, { name: '', spec: { type: 'string' } }])}
          style={{ marginLeft: 'auto', color: token.colorPrimary, background: token.colorPrimaryBg }}
        />
      </div>

      {/* 与契约一致：结束节点只声明产出，不含取值路径 */}
      <Typography.Text type="secondary" style={{ fontSize: 11.5, display: 'block', marginBottom: 10 }}>
        {t('taskCenter.editor.end.hint')}
      </Typography.Text>

      {renderTableHeader(
        [
          t('taskCenter.editor.field.varName'),
          t('taskCenter.editor.field.varType'),
          t('taskCenter.editor.field.required'),
          '',
        ],
        '1fr 104px 40px 28px',
      )}

      {endOutputs.length === 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {t('taskCenter.editor.empty')}
        </Typography.Text>
      )}

      {endOutputs.map((item, index) => {
        const missing = item.name.trim() === '';
        const duplicated =
          !missing && endOutputs.filter((other) => other.name.trim() === item.name.trim()).length > 1;
        const invalid = !missing && !VARIABLE_NAME_RE.test(item.name.trim());
        return (
          <div key={`end-${index}`} style={{ marginBottom: 10 }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 104px 40px 28px', gap: 8, alignItems: 'center' }}>
              <Input
                size="small"
                disabled={locked}
                status={missing || duplicated || invalid ? 'error' : undefined}
                value={item.name}
                placeholder={t('taskCenter.editor.field.varNamePlaceholder')}
                onChange={(event) => updateEndOutput(index, { name: event.target.value })}
              />
              <Select
                size="small"
                disabled={locked}
                value={item.spec.type}
                options={DECLARABLE_TYPES.map((type) => ({ value: type, label: type }))}
                onChange={(value) =>
                  updateEndOutput(index, { spec: fieldSpecFor(value, item.spec.required === true) })
                }
              />
              <Checkbox
                disabled={locked}
                checked={item.spec.required === true}
                // 只改必填：其余键（abstract / description / default 等）原样保留
                onChange={(event) =>
                  updateEndOutput(index, { spec: { ...item.spec, required: event.target.checked } })
                }
              />
              <Button
                size="small"
                type="text"
                disabled={locked}
                icon={<MinusOutlined />}
                aria-label={t('taskCenter.editor.action.removeOutput')}
                onClick={() => setEndOutputs((prev) => prev.filter((_, i) => i !== index))}
              />
            </div>
            {missing && (
              <div style={{ color: token.colorError, fontSize: 12, marginTop: 4 }}>
                {t('taskCenter.editor.field.nameRequired')}
              </div>
            )}
            {duplicated && (
              <div style={{ color: token.colorError, fontSize: 12, marginTop: 4 }}>
                {t('taskCenter.editor.field.nameDuplicate')}
              </div>
            )}
            {invalid && (
              <div style={{ color: token.colorError, fontSize: 12, marginTop: 4 }}>
                {t('taskCenter.editor.field.nameInvalid')}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );

  /** 中间步骤的配置：输入（只绑来源）+ 输出（只读）+ 高级属性 */
  const renderStepPanel = (step: StepState, index: number): ReactNode => {
    const catalogStep = catalogByCode[step.stepCode];
    const inputSchema = catalogStep?.input_schema ?? {};
    const outputSchema = catalogStep?.output_schema ?? {};
    const inputTemplate = 'minmax(0, 1fr) 84px 148px';
    // 知识编译的输入由「文档解析」推导而来，只展示、不给改
    const derived = step.stepCode === 'compile';
    const bindings = bindingsFor(steps, step);
    const describeBinding = (value: string): string => {
      if (!value) return '—';
      if (value.startsWith(TASK_INPUT_PREFIX)) {
        return `${t('taskCenter.editor.section.input')} · ${value.slice(TASK_INPUT_PREFIX.length)}`;
      }
      const stepRef = /^@([^.]+)\.outputs\.(.+)$/.exec(value);
      if (stepRef) {
        const prior = steps.find((item) => item.nodeId === stepRef[1]);
        return `${stepLabel(prior?.stepCode ?? '')} · ${stepRef[1]}.${stepRef[2]}`;
      }
      return value;
    };
    return (
      <>
        {renderSectionTitle(
          t('taskCenter.editor.section.input'),
          derived ? t('taskCenter.editor.field.derived') : undefined,
        )}
        {renderTableHeader(
          [
            t('taskCenter.editor.field.varName'),
            t('taskCenter.editor.field.varType'),
            t('taskCenter.editor.field.upstream'),
          ],
          inputTemplate,
        )}
        {Object.keys(inputSchema).length === 0 ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t('taskCenter.editor.empty')}
          </Typography.Text>
        ) : (
          Object.entries(inputSchema).map(([field, spec]) => (
            <div
              key={`${step.nodeId}:${field}`}
              style={{ display: 'grid', gridTemplateColumns: inputTemplate, columnGap: 8, alignItems: 'center', marginBottom: 8 }}
            >
              <Typography.Text style={{ fontSize: 13 }} ellipsis title={field}>
                {field}
                {spec.required ? ' *' : ''}
              </Typography.Text>
              {renderTypeTag(spec)}
              {derived ? (
                // 编译的输入固定跟随解析：只展示来源，不给改
                <Typography.Text
                  type="secondary"
                  style={{ fontSize: 12 }}
                  ellipsis
                  title={bindings[field] ? describeBinding(bindings[field]) : undefined}
                >
                  {describeBinding(bindings[field] ?? '')}
                </Typography.Text>
              ) : (
                <Select
                  size="small"
                  allowClear
                  disabled={locked}
                  status={spec.required && !bindings[field] ? 'error' : undefined}
                  placeholder={t('taskCenter.editor.field.upstreamPlaceholder')}
                  // 空下拉直说原因：新草稿没有开始入参时这里本来就没有可选项，默认的 No data 看不出该干什么
                  notFoundContent={t('taskCenter.editor.field.noUpstream')}
                  value={bindings[field] || undefined}
                  onChange={(value) =>
                    updateStep(step.nodeId, {
                      bindings: { ...step.bindings, [field]: (value as string | undefined) ?? '' },
                    })
                  }
                  options={bindingOptions(index, spec)}
                />
              )}
            </div>
          ))
        )}

        <Divider style={{ margin: '14px 0' }} />

        {renderSectionTitle(
          t('taskCenter.editor.section.output'),
          t('taskCenter.editor.output.readonly'),
        )}
        {renderTableHeader(
          [t('taskCenter.editor.field.varName'), t('taskCenter.editor.field.dataType')],
          'minmax(0, 1fr) 96px',
        )}
        {Object.keys(outputSchema).length === 0 ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t('taskCenter.editor.empty')}
          </Typography.Text>
        ) : (
          Object.entries(outputSchema).map(([field, spec]) => (
            <div
              key={`${step.nodeId}:out:${field}`}
              style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 96px', columnGap: 8, alignItems: 'center', marginBottom: 8 }}
            >
              <Typography.Text style={{ fontSize: 13 }} ellipsis title={field}>
                {field}
              </Typography.Text>
              {renderTypeTag(spec)}
            </div>
          ))
        )}

        <Divider style={{ margin: '14px 0' }} />

        {renderSectionTitle(t('taskCenter.editor.section.advanced'))}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Typography.Text style={{ width: 104, fontSize: 13 }}>
              {t('taskCenter.editor.field.timeout')}
            </Typography.Text>
            <InputNumber
              size="small"
              min={1}
              disabled={locked}
              style={{ flex: 1 }}
              value={step.timeoutSeconds}
              // 清空输入框时收到 null：保持原值，不静默改回清单默认 —— 用户没重填之前，
              // 把超时悄悄改掉比留空更危险
              onChange={(value) => {
                if (typeof value === 'number') updateStep(step.nodeId, { timeoutSeconds: value });
              }}
            />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Typography.Text style={{ width: 104, fontSize: 13 }}>
              {t('taskCenter.editor.field.maxRuns')}
            </Typography.Text>
            <Select
              size="small"
              disabled={locked}
              style={{ flex: 1 }}
              value={String(step.maxRetries)}
              onChange={(value) => updateStep(step.nodeId, { maxRetries: Number(value) })}
              options={[
                { value: '0', label: t('taskCenter.editor.options.noRetry') },
                { value: '1', label: t('taskCenter.editor.options.retry1') },
                { value: '2', label: t('taskCenter.editor.options.retry2') },
                { value: '3', label: t('taskCenter.editor.options.retry3') },
              ]}
            />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Typography.Text style={{ width: 104, fontSize: 13 }}>
              {t('taskCenter.editor.field.retryInterval')}
            </Typography.Text>
            <Select
              size="small"
              disabled={locked}
              style={{ flex: 1 }}
              value={step.onRetryExhausted}
              onChange={(value) => updateStep(step.nodeId, { onRetryExhausted: value })}
              options={[
                { value: 'terminate', label: t('taskCenter.editor.options.stopFlow') },
                { value: 'skip', label: t('taskCenter.editor.options.continueFlow') },
              ]}
            />
          </div>
        </div>
      </>
    );
  };

  /** 导轨卡片的副标题：端点写变量，中间节点报「几步之外」的体量 */
  const railMeta = (key: string): string => {
    if (key === 'start') {
      return startInputs.length === 0
        ? t('taskCenter.editor.nodeStatus.startHint')
        : startInputs.map((item) => `${item.name || '?'} ${fieldTypeLabel(item.spec)}`).join(' · ');
    }
    if (key === 'end') {
      return endOutputs.length === 0
        ? t('taskCenter.editor.nodeStatus.endHint')
        : endOutputs.map((item) => `${item.name || '?'} ${fieldTypeLabel(item.spec)}`).join(' · ');
    }
    const step = steps.find((item) => item.nodeId === key);
    if (!step) return '';
    const catalogStep = catalogByCode[step.stepCode];
    return t('taskCenter.editor.nodeStatus.varCount', {
      inputs: Object.keys(catalogStep?.input_schema ?? {}).length,
      outputs: Object.keys(catalogStep?.output_schema ?? {}).length,
    });
  };

  const businessCount = steps.length;

  /** 选中节点的标题与图标 */
  const selectedTitle =
    selectedKey === 'start'
      ? t('taskCenter.editor.node.start')
      : selectedKey === 'end'
        ? t('taskCenter.editor.node.end')
        : stepLabel(selectedStep?.stepCode ?? '');
  const selectedKind = selectedKey === 'start' || selectedKey === 'end' ? selectedKey : (selectedStep?.stepCode ?? '');

  /**
   * 有没有未保存的修改：当前画布与最近一次载入/保存成功的快照逐字比较。
   * 没改过就不让存 —— 否则每点一次「保存」都会在库里多出一个空草稿版本。
   */
  const dirty = savedSnapshot !== null && JSON.stringify(serializeConfig(startInputs, steps, endOutputs)) !== savedSnapshot;
  /** 发布可用：有改动，或手上本身就有一版未发布的草稿（把它发出去本身就是有意义的动作） */
  const canPublish = dirty || versionStatus === 'draft';

  return (
    <>
      <PageHeader
        title={definition?.name ?? stateName ?? t('taskCenter.editor.title')}
        // 行高钉死：中文回退到 PingFang（ascent+descent ≈1.4em）、英文走 SF Pro（≈1.17em），
        // line-height:normal 会让标题盒高随语言变，和右侧按钮错开
        style={{ lineHeight: '32px' }}
        backTo={backTo}
        extra={
          // marginLeft:auto 把动作组推到页头行尾；alignSelf 覆盖页头行的 flex-end 底对齐
          <Space style={{ marginLeft: 'auto', alignSelf: 'center' }}>
            {!locked && (
              <Button
                onClick={() => void submit('draft')}
                loading={saving === 'draft'}
                // 一个在保存/发布时，另一个按钮禁用：两次提交并发会双双写版本
                disabled={saving === 'published' || !dirty}
                title={!dirty ? t('taskCenter.editor.action.noChanges') : undefined}
              >
                {t('common.save')}
              </Button>
            )}
            {!locked && (
              <Button
                type="primary"
                onClick={() => void submit('published')}
                loading={saving === 'published'}
                disabled={saving === 'draft' || !canPublish}
                title={!canPublish ? t('taskCenter.editor.action.noChanges') : undefined}
              >
                {t('taskCenter.editor.action.publish')}
              </Button>
            )}
          </Space>
        }
      />
      <PageContent>
        {readOnly && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            title={t('taskCenter.editor.systemReadOnly')}
          />
        )}
        {!readOnly && versionStatus === 'published' && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            title={t('taskCenter.editor.draftHint')}
          />
        )}
        <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
          {/* 左：画布。浅灰底 + 居中窄列，视觉上就是「一条纵向流程」 */}
          <div
            style={{
              flex: '1 1 0%',
              minWidth: 0,
              background: token.colorFillQuaternary,
              border: `1px solid ${token.colorBorderSecondary}`,
              borderRadius: token.borderRadiusLG,
              padding: '28px 24px 24px',
            }}
          >
            <div style={{ maxWidth: 460, margin: '0 auto' }} data-testid="workflow-canvas">
              <div style={{ position: 'relative', paddingLeft: 48 }}>
                {/* 贯穿全流程的导轨：流程是一条链，所以用一条连续的主线，而不是一叠独立卡片 */}
                <span
                  aria-hidden
                  style={{
                    position: 'absolute',
                    left: 23,
                    top: 16,
                    bottom: 28,
                    borderLeft: `1.5px solid ${token.colorBorderSecondary}`,
                  }}
                />
                {/* 开始节点 */}
                <div style={{ position: 'relative', marginBottom: 12 }}>
                  <span
                    style={{
                      position: 'absolute',
                      left: -40,
                      top: 0,
                      display: 'inline-flex',
                      borderRadius: '50%',
                      boxShadow: `0 0 0 3px ${token.colorFillQuaternary}`,
                    }}
                  >
                    <WorkflowNodeIcon kind="start" size={30} />
                  </span>
                  <div
                    data-node-id="start"
                    onClick={() => setSelectedKey('start')}
                    style={cardStyle(selectedKey === 'start', true)}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <Typography.Text strong style={{ fontSize: 14, lineHeight: 1.5 }}>
                          {t('taskCenter.editor.node.start')}
                        </Typography.Text>
                        <Typography.Text type="secondary" style={{ fontSize: 11.5, display: 'block', marginTop: 5 }} ellipsis>
                          {railMeta('start')}
                        </Typography.Text>
                      </div>
                    </div>
                  </div>
                </div>

                {/* 分组标签只出现在第一个业务节点之前 */}
                {businessCount > 0 && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '16px 0 12px' }}>
                    <span style={{ fontSize: 11.5, color: token.colorPrimary, letterSpacing: 0.3 }}>
                      {t('taskCenter.editor.section.nodes')} · {businessCount}
                    </span>
                    <span style={{ flex: 1, borderTop: `1px dashed ${token.colorPrimaryBorder}` }} />
                  </div>
                )}

                {steps.map((step) => (
                  <div key={step.nodeId} style={{ position: 'relative', marginBottom: 12 }}>
                    <span
                      style={{
                        position: 'absolute',
                        left: -40,
                        top: 0,
                        display: 'inline-flex',
                        borderRadius: '50%',
                        boxShadow: `0 0 0 3px ${token.colorFillQuaternary}`,
                      }}
                    >
                      <WorkflowNodeIcon kind={step.stepCode} size={30} />
                    </span>
                    <div
                      data-node-id={step.nodeId}
                      onClick={() => setSelectedKey(step.nodeId)}
                      style={cardStyle(selectedKey === step.nodeId, false)}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                            <Typography.Text strong style={{ fontSize: 13.5, lineHeight: 1.5 }}>
                              {stepLabel(step.stepCode)}
                            </Typography.Text>
                            <Tag
                              variant="filled"
                              color="success"
                              style={{ marginInlineEnd: 0, fontSize: 11, lineHeight: '18px', paddingInline: 7 }}
                            >
                              {t('taskCenter.editor.nodeStatus.configured')}
                            </Tag>
                          </div>
                          <Typography.Text type="secondary" style={{ fontSize: 11.5, display: 'block', marginTop: 5 }} ellipsis>
                            {railMeta(step.nodeId)}
                          </Typography.Text>
                        </div>
                        {!locked && (
                          <Button
                            type="text"
                            size="small"
                            aria-label={t('taskCenter.editor.action.deleteNode')}
                            icon={<DeleteOutlined style={{ fontSize: 14 }} />}
                            onClick={(event) => {
                              event.stopPropagation();
                              removeStep(step.nodeId);
                            }}
                            danger
                            style={{ marginLeft: 'auto', marginRight: -4 }}
                          />
                        )}
                      </div>
                    </div>
                  </div>
                ))}

                {/* 添加入口落在「结束」之前：只追加，顺序天然可控 */}
                {!locked && (
                  <div style={{ margin: '2px 0 16px' }}>
                    <AddStepPopover
                      steps={catalog}
                      onSelect={addStep}
                      blockedReason={blockedReason}
                      placement="bottom"
                    >
                      <Button
                        type="dashed"
                        block
                        icon={<PlusOutlined />}
                        data-testid="add-step-button"
                        style={{ height: 34, fontSize: 13 }}
                      >
                        {t('taskCenter.editor.action.addStep')}
                      </Button>
                    </AddStepPopover>
                  </div>
                )}

                {/* 结束节点 */}
                <div style={{ position: 'relative', marginBottom: 12 }}>
                  <span
                    style={{
                      position: 'absolute',
                      left: -40,
                      top: 0,
                      display: 'inline-flex',
                      borderRadius: '50%',
                      boxShadow: `0 0 0 3px ${token.colorFillQuaternary}`,
                    }}
                  >
                    <WorkflowNodeIcon kind="end" size={30} />
                  </span>
                  <div
                    data-node-id="end"
                    onClick={() => setSelectedKey('end')}
                    style={cardStyle(selectedKey === 'end', true)}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <Typography.Text strong style={{ fontSize: 14, lineHeight: 1.5 }}>
                          {t('taskCenter.editor.node.end')}
                        </Typography.Text>
                        <Typography.Text type="secondary" style={{ fontSize: 11.5, display: 'block', marginTop: 5 }} ellipsis>
                          {railMeta('end')}
                        </Typography.Text>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* 右：节点配置。key 挂选中节点：切节点时整块重建，下拉不会残留上一个节点的值 */}
          <div
            key={selectedKey}
            style={{
              width: 380,
              flex: '0 0 380px',
              background: token.colorBgContainer,
              border: `1px solid ${token.colorBorderSecondary}`,
              borderRadius: token.borderRadiusLG,
              padding: '8px 20px 20px',
              position: 'sticky',
              top: 0,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <WorkflowNodeIcon kind={selectedKind} size={22} />
              <Typography.Text strong style={{ fontSize: 15 }}>
                {selectedTitle}
              </Typography.Text>
              {selectedStep && !locked && (
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => removeStep(selectedStep.nodeId)}
                  aria-label={t('taskCenter.editor.action.remove')}
                  style={{ marginLeft: 'auto' }}
                />
              )}
            </div>

            <Divider style={{ margin: '14px 0' }} />

            {selectedKey === 'start'
              ? renderStartPanel()
              : selectedKey === 'end'
                ? renderEndPanel()
                : selectedStep
                  ? renderStepPanel(selectedStep, selectedIndex)
                  : null}
          </div>
        </div>
      </PageContent>
    </>
  );

  /** 画布卡片的选中/默认样式。抽出来只因为这处样式在两个渲染点（开始/结束/步骤）重复。 */
  function cardStyle(active: boolean, endpoint: boolean): CSSProperties {
    return {
      cursor: 'pointer',
      background: token.colorBgContainer,
      border: `1px solid ${active ? token.colorPrimary : token.colorBorderSecondary}`,
      borderRadius: token.borderRadius,
      boxShadow: active ? token.boxShadowTertiary : '0 1px 2px rgba(15, 23, 42, .04)',
      padding: endpoint ? '18px 16px' : '17px 15px',
    };
  }
}
