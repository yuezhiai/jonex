import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dropdown, Modal, Result, Space, Tag, Tooltip, message, theme } from 'antd';
import type { MenuProps, TableColumnsType } from 'antd';
import { DownOutlined, PlusOutlined, ReloadOutlined, ScheduleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { CommonTable, PageContent, PageHeader, usePermission } from '@jonex/shared-lib';
import type { CommonTableRef, FilterConfig, TableQueryParams } from '@jonex/shared-lib';
import { TASK_CENTER_READ_PERMISSION, TASK_CENTER_WRITE_PERMISSION } from '../../api/taskCenter';
import {
  copyDefinition,
  deleteDefinition,
  disableDefinition,
  listDefinitions,
  publishDefinition,
} from '../../api/taskCenterDefinitions';
import type {
  DefinitionStatus,
  TaskDefinitionItem,
} from '../../api/taskCenterDefinitions';
import useCurrentSpace from '../../hooks/useCurrentSpace';
import { fmtDateTime } from './format';
import TaskBasicInfoModal from './TaskBasicInfoModal';
import TaskCreateModal from './TaskCreateModal';
import RunTaskModal from './RunTaskModal';
import { taskEditorPath, taskExecutionsPath } from './paths';

/**
 * 任务中心的**一级页**：任务定义列表。
 *
 * 这里的「任务」是任务定义（一份编排 + 它的调度配置），点进去才是某任务的执行记录
 * （二级页 `./TaskExecutionList`）。两层的权限码相同：能看列表就能看执行记录，
 * 写操作统一要 `task_center:write`；定义本身写不了的时候后端还会判空间管理员。
 *
 * 系统级（`scope === 'system'`）定义对所有租户可见但**只读**：平台预置的编排，
 * 页面上不给执行 / 编辑 / 停用 / 删除入口。
 */

/** 状态标签的配色与文案 key。标签的圆点用 currentColor，跟 Tag 的色走。 */
const STATUS_META: Record<DefinitionStatus, { color: string; labelKey: string }> = {
  draft: { color: 'gold', labelKey: 'taskCenter.definition.status.draft' },
  published: { color: 'success', labelKey: 'taskCenter.definition.status.published' },
  disabled: { color: 'default', labelKey: 'taskCenter.definition.status.disabled' },
};

/**
 * 执行结果的配色。`last_execution_result` 是**最近一条任务的状态，不过滤终态** ——
 * 正在跑的定义也会显示 `pending` / `running`，所以五档都要有；从未执行是 `null`。
 */
const RESULT_META: Record<string, { color: string; labelKey: string }> = {
  pending: { color: 'default', labelKey: 'taskCenter.definition.result.pending' },
  running: { color: 'processing', labelKey: 'taskCenter.definition.result.running' },
  completed: { color: 'success', labelKey: 'taskCenter.definition.result.completed' },
  failed: { color: 'error', labelKey: 'taskCenter.definition.result.failed' },
  stopped: { color: 'default', labelKey: 'taskCenter.definition.result.stopped' },
};

/** 状态点。截图里的标签是「圆点 + 文字」，Tag 自带图标位，塞一个色块即可。 */
function StatusDot() {
  return (
    <span
      style={{
        display: 'inline-block',
        width: 6,
        height: 6,
        borderRadius: '50%',
        background: 'currentColor',
        verticalAlign: 'middle',
      }}
    />
  );
}

const errorText = (e: unknown, fallback: string): string =>
  e instanceof Error && e.message ? e.message : fallback;

export default function TaskCenter() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { hasPerm } = usePermission();
  const canRead = hasPerm(TASK_CENTER_READ_PERMISSION);
  const canWrite = hasPerm(TASK_CENTER_WRITE_PERMISSION);
  // 取 token 而不是写死色值：深浅色主题下图标要跟主色一起变
  const { token } = theme.useToken();
  const tableRef = useRef<CommonTableRef>(null);
  /** 当前空间来自主应用的空间切换器；null = 全部可见空间。 */
  const spaceId = useCurrentSpace();
  /** 「编辑基础信息」弹窗当前操作的行。null = 弹窗关着 */
  const [editing, setEditing] = useState<TaskDefinitionItem | null>(null);
  const [creating, setCreating] = useState(false);
  /** 正在准备执行的定义；null = 执行弹窗关着 */
  const [running, setRunning] = useState<TaskDefinitionItem | null>(null);

  /**
   * 空间变了要重取一次。跳过首次：挂载时 CommonTable 自己已经在取第一份数，
   * 不跳过就会连发两次请求。
   */
  const spaceReadyRef = useRef(false);
  useEffect(() => {
    if (!spaceReadyRef.current) {
      spaceReadyRef.current = true;
      return;
    }
    tableRef.current?.refresh();
  }, [spaceId]);

  const filters = useMemo<FilterConfig[]>(
    () => [
      {
        key: 'keyword',
        label: t('taskCenter.definition.column.name'),
        type: 'input',
        placeholder: t('taskCenter.definition.searchPlaceholder'),
      },
      {
        key: 'status',
        label: t('common.status'),
        type: 'select',
        placeholder: t('taskCenter.definition.statusPlaceholder'),
        width: 160,
        options: (Object.keys(STATUS_META) as DefinitionStatus[]).map((key) => ({
          label: t(STATUS_META[key].labelKey),
          value: key,
        })),
      },
    ],
    [t],
  );

  const fetchRows = useCallback(
    async (params: TableQueryParams<{ keyword?: string; status?: DefinitionStatus }>) => {
      const res = await listDefinitions({
        page: params.page,
        page_size: params.pageSize,
        keyword: params.keyword || undefined,
        status: params.status || undefined,
        // 不带空间 = 该用户可见的全部空间（后端按可见集合收敛，不是全平台）
        space_id: spaceId || undefined,
      });
      return { list: res.items ?? [], total: res.total ?? 0 };
    },
    [spaceId],
  );

  /** 二级页。任务名走 state 带过去，省得二级页为了一个标题再查一次。 */
  const openExecutions = useCallback(
    (row: TaskDefinitionItem) => {
      navigate(taskExecutionsPath(row.id), { state: { definitionName: row.name } });
    },
    [navigate],
  );

  const runRow = useCallback((row: TaskDefinitionItem) => {
    setRunning(row);
  }, []);

  /** 启用 / 停用都走各自的接口：停用只对已发布，启用只对草稿 / 已停用且有已发布版本。 */
  const switchStatus = useCallback(
    async (row: TaskDefinitionItem, next: 'published' | 'disabled') => {
      try {
        if (next === 'published') await publishDefinition(row.id);
        else await disableDefinition(row.id);
        message.success(t('taskCenter.definition.action.statusSuccess'));
      } catch (e: unknown) {
        message.error(errorText(e, t('common.operationFailed')));
      } finally {
        // 失败也刷新：拒绝往往意味着页面上的数据已经旧了
        tableRef.current?.refresh();
      }
    },
    [t],
  );

  const copyRow = useCallback(
    async (row: TaskDefinitionItem) => {
      try {
        const copied = await copyDefinition(row.id);
        message.success(t('taskCenter.definition.action.copySuccess'));
        // 复制出来的是一份新草稿：直接进编辑器是最顺的下一步
        navigate(taskEditorPath(copied.definition.id));
      } catch (e: unknown) {
        message.error(errorText(e, t('common.operationFailed')));
      } finally {
        tableRef.current?.refresh();
      }
    },
    [navigate, t],
  );

  const removeRow = useCallback(
    (row: TaskDefinitionItem) => {
      Modal.confirm({
        title: t('taskCenter.definition.action.deleteConfirm', { name: row.name }),
        okText: t('common.confirm'),
        cancelText: t('common.cancel'),
        okButtonProps: { danger: true },
        // onOk 里必须自己吞异常，抛出去会让确认框卡在 loading 不关
        onOk: async () => {
          try {
            await deleteDefinition(row.id);
            message.success(t('taskCenter.definition.action.deleteSuccess'));
          } catch (e: unknown) {
            message.error(errorText(e, t('taskCenter.definition.action.deleteFailed')));
          } finally {
            tableRef.current?.refresh();
          }
        },
      });
    },
    [t],
  );

  const moreMenu = useCallback(
    (row: TaskDefinitionItem): MenuProps['items'] => {
      const items: MenuProps['items'] = [];
      // 与后端同一条门禁：只有已发布能停用；启用（发布）要求已有已发布版本，
      // 草稿从来没有发过版，启用必然被拒 —— 不给这个入口，免得用户点了才知道
      if (row.status === 'published') {
        items.push({ key: 'disable', label: t('taskCenter.definition.action.disable') });
      } else if (row.status === 'disabled') {
        items.push({ key: 'enable', label: t('taskCenter.definition.action.enable') });
      }
      items.push({ key: 'copy', label: t('taskCenter.definition.action.copy') });
      // 已发布（可能还有在途实例）的定义不给删，先停用再删
      if (row.status !== 'published') {
        items.push({ key: 'delete', label: t('taskCenter.definition.action.delete'), danger: true });
      }
      return items;
    },
    [t],
  );

  const onMoreClick = useCallback(
    (row: TaskDefinitionItem, key: string) => {
      if (key === 'enable') void switchStatus(row, 'published');
      else if (key === 'disable') void switchStatus(row, 'disabled');
      else if (key === 'copy') void copyRow(row);
      else if (key === 'delete') removeRow(row);
    },
    [switchStatus, copyRow, removeRow],
  );

  const columns = useMemo<TableColumnsType<TaskDefinitionItem>>(
    () => [
      {
        title: t('taskCenter.definition.column.name'),
        key: 'name',
        render: (_, row) => (
          <Space size={8}>
            {/* 截图里任务名前有个小图标，用侧栏同一个 ScheduleOutlined 保持同一套语义 */}
            <ScheduleOutlined style={{ color: token.colorPrimary }} />
            {/* 行本身也绑了跳转，链接要止住冒泡，否则一次点击 push 两条历史 */}
            <a
              onClick={(e) => {
                e.stopPropagation();
                openExecutions(row);
              }}
            >
              {row.name}
            </a>
            {row.scope === 'system' && (
              <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                {t('taskCenter.definition.scope.system')}
              </Tag>
            )}
          </Space>
        ),
      },
      {
        title: t('common.status'),
        key: 'status',
        width: 120,
        render: (_, row) => {
          const meta = STATUS_META[row.status];
          return meta ? (
            <Tag color={meta.color} icon={<StatusDot />}>
              {t(meta.labelKey)}
            </Tag>
          ) : (
            row.status
          );
        },
      },
      {
        title: t('taskCenter.definition.column.executionCount'),
        key: 'executionCount',
        width: 120,
        render: (_, row) => row.execution_count,
      },
      {
        title: t('taskCenter.definition.column.lastExecutedAt'),
        key: 'lastExecutedAt',
        width: 190,
        render: (_, row) => fmtDateTime(row.last_executed_at),
      },
      {
        title: t('taskCenter.definition.column.lastResult'),
        key: 'lastResult',
        width: 140,
        render: (_, row) => {
          const meta = row.last_execution_result ? RESULT_META[row.last_execution_result] : undefined;
          if (!row.last_execution_result) return '-';
          return meta ? <Tag color={meta.color}>{t(meta.labelKey)}</Tag> : row.last_execution_result;
        },
      },
      {
        title: t('taskCenter.definition.column.createdBy'),
        key: 'createdBy',
        width: 120,
        // 展示名优先，取不到才回落到 user_id（与后端 `creator_name` 同口径）
        render: (_, row) => row.creator_name || row.created_by || '-',
      },
      {
        title: t('common.actions'),
        key: 'actions',
        width: 200,
        // 行点击会跳二级页，操作区的点击必须止住冒泡，否则点「执行」顺带跳走
        render: (_, row) => {
          // 系统级定义只读：连「更多」都不给，避免点进去才发现全是拒绝
          if (row.scope === 'system') return '-';
          return (
            <span onClick={(e) => e.stopPropagation()}>
              <Space size={8}>
                {/* 只有已发布的空间级定义能执行（后端 `def.run` 同一道门禁） */}
                {canWrite && row.status === 'published' && (
                  <a onClick={() => runRow(row)}>{t('taskCenter.definition.action.run')}</a>
                )}
                {/* 编辑只覆盖基础信息（名称 / 描述）；编排在「编辑任务」页 */}
                {canWrite && <a onClick={() => setEditing(row)}>{t('taskCenter.definition.action.edit')}</a>}
                {canWrite && (
                  <Dropdown
                    trigger={['click']}
                    menu={{
                      items: moreMenu(row),
                      onClick: ({ key, domEvent }) => {
                        // 下拉浮层挂在 body 上，但触发器的 click 仍会冒到行；点击菜单项本身也要拦住
                        domEvent.stopPropagation();
                        onMoreClick(row, key);
                      },
                    }}
                  >
                    <a>
                      {t('taskCenter.definition.action.more')} <DownOutlined />
                    </a>
                  </Dropdown>
                )}
              </Space>
            </span>
          );
        },
      },
    ],
    [t, token, canWrite, openExecutions, runRow, moreMenu, onMoreClick],
  );

  // 路由守卫在两个壳里不等价（standalone 的 basename 带尾斜杠，拦不住），页面自己兜一道
  if (!canRead) return <Result status="403" title="403" subTitle={t('error.403')} />;

  const createButton = (
    <Button
      type="primary"
      icon={<PlusOutlined />}
      disabled={!canWrite}
      onClick={() => {
        // 定义必须落在某个空间里：没选空间时给出明确指引，而不是弹一个填不完的表单
        if (!spaceId) {
          message.warning(t('taskCenter.definition.create.spaceRequired'));
          return;
        }
        setCreating(true);
      }}
    >
      {t('taskCenter.definition.action.create')}
    </Button>
  );

  return (
    <>
      <PageHeader title={t('taskCenter.title')} subtitle={t('taskCenter.definition.subtitle')} />
      <PageContent>
        <CommonTable<TaskDefinitionItem, { keyword?: string; status?: DefinitionStatus }>
          ref={tableRef}
          rowKey="id"
          columns={columns}
          filters={filters}
          // 截图里的「重置」是文字按钮且带刷新图标，内置那枚是描边按钮 —— 关掉自己摆
          actions={{ refresh: false, reset: false }}
          toolbar={
            <Space>
              <Button type="text" icon={<ReloadOutlined />} onClick={() => tableRef.current?.reset()}>
                {t('common.reset')}
              </Button>
              {canWrite ? (
                createButton
              ) : (
                <Tooltip title={t('error.403')}>
                  {/* 无写权限时按钮置灰，但要说明原因 —— 直接消失会让人以为功能没做 */}
                  <span>{createButton}</span>
                </Tooltip>
              )}
            </Space>
          }
          fetch={fetchRows}
          // 接了 fetch 就必须接 onError：取数失败时表格只会变成空态，不报警就与「确实没有任务」分不开
          onError={(e) => message.error(errorText(e, t('common.loadFailed')))}
          defaultPageSize={10}
          size="middle"
          onRow={(row) => ({ onClick: () => openExecutions(row), style: { cursor: 'pointer' } })}
        />
      </PageContent>
      <TaskBasicInfoModal
        open={Boolean(editing)}
        task={editing}
        onCancel={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          // 名称可能变了，刷新列表让整行（含搜索命中的关键词）重算
          tableRef.current?.refresh();
        }}
      />
      <TaskCreateModal
        open={creating}
        spaceId={spaceId}
        onCancel={() => setCreating(false)}
        onCreated={(created) => {
          setCreating(false);
          tableRef.current?.refresh();
          // 新建出来的是一份空草稿：直接进编辑器编排，比停在列表上再找一次更顺
          navigate(taskEditorPath(created));
        }}
      />
      <RunTaskModal
        open={Boolean(running)}
        definitionId={running?.id ?? null}
        onCancel={() => setRunning(null)}
        onDone={() => {
          setRunning(null);
          tableRef.current?.refresh();
        }}
      />
    </>
  );
}
