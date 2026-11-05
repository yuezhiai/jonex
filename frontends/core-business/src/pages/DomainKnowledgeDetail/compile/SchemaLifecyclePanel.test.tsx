/**
 * KB 级 schema 生命周期面板的组件级回归（jsdom + RTL）。
 *
 * 钉住容易静默坏掉的三点：
 * - 新鲜度徽标与影响面必须显式（outdated / manual_edited 不能静默）；
 * - 「重新编译 / 确认重新编译」一律显式 `force: true`（默认 false 的短路会把
 *   outdated 标记留着不动），成功后刷新自身状态并回调 `onChanged`；
 * - `canWrite=false` 时两个动作都不可点。
 *
 * 断言按 i18n key（`react-i18next` mock 成直出键名），不锁中文文案。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { message } from 'antd';

import SchemaLifecyclePanel from './SchemaLifecyclePanel';

const { statusMock, previewMock, recompileMock, reseedMock, tMock } = vi.hoisted(() => ({
  statusMock: vi.fn(),
  previewMock: vi.fn(),
  recompileMock: vi.fn(),
  reseedMock: vi.fn(),
  tMock: (key: string, params?: Record<string, unknown>) =>
    params ? `${key}(${JSON.stringify(params)})` : key,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: tMock }),
}));

vi.mock('@/api/domainKnowledge', () => ({
  getCompiledSchemaStatus: (...a: unknown[]) => statusMock(...a),
  previewRecompileCompiledSchema: (...a: unknown[]) => previewMock(...a),
  recompileCompiledSchema: (...a: unknown[]) => recompileMock(...a),
  reseedOntologyCompiledSchema: (...a: unknown[]) => reseedMock(...a),
}));

const STATUS = {
  schema: null,
  freshness: 'outdated',
  source_type: 'business_template',
  source_version: 1,
  source_hash: 'h0',
  expected_source_hash: 'h1',
  schema_version: 3,
  template_domain_id: 'd1',
  template_scenario_id: 's1',
  stale_doc_types: ['Gone'],
  alignment_impact: { active_alignments: 2, stale_doc_types: 1, projected_maps_to: 2 },
};

const PREVIEW = {
  changed: true,
  source_changed: true,
  manual_edited: false,
  added_entity_types: ['New'],
  removed_entity_types: ['Old'],
  renamed_entity_types: [],
  affected_alignments: 1,
  stale_after: ['Old'],
  affected_maps_to: 1,
};

beforeEach(() => {
  statusMock.mockReset().mockResolvedValue(STATUS);
  previewMock.mockReset();
  recompileMock.mockReset();
  reseedMock.mockReset();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('SchemaLifecyclePanel', () => {
  it('outdated：显示待复核徽标、过期实体类型与预览动作', async () => {
    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);

    expect(await screen.findByText('compile.lifecycle.outdated')).toBeTruthy();
    expect(screen.getByText(/Gone/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'compile.lifecycle.preview' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'compile.lifecycle.recompile' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'compile.lifecycle.reseed' })).toBeTruthy();
  });

  it('预览后展示差异，确认重编译以 force=true 调用 API 并回调 onChanged', async () => {
    previewMock.mockResolvedValue(PREVIEW);
    recompileMock.mockResolvedValue({
      schema_saved: true,
      schema_version: 4,
      unchanged: false,
      documents_matched: 0,
      documents_queued: 0,
    });
    const onChanged = vi.fn();

    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={onChanged} />);

    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.preview' }));
    expect(await screen.findByText('compile.lifecycle.added')).toBeTruthy();
    expect(await screen.findByText('New')).toBeTruthy();
    // 'Old' 同时出现在 removed_entity_types 与 stale_after，用 findAll 避免多元素歧义
    expect((await screen.findAllByText('Old')).length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: 'compile.lifecycle.confirmRecompile' }));

    await waitFor(() =>
      expect(recompileMock).toHaveBeenCalledWith('kb1', { force: true, applyToDocuments: false }),
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it('预览无内容差异：弹窗内提示 noContentChange', async () => {
    previewMock.mockResolvedValue({ ...PREVIEW, changed: false });
    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);

    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.preview' }));

    expect(await screen.findByText('compile.lifecycle.noContentChange')).toBeTruthy();
  });

  it('重编译成功后回调 onChanged 并用新状态重新拉取', async () => {
    recompileMock.mockResolvedValue({
      schema_saved: true,
      schema_version: 4,
      unchanged: false,
      documents_matched: 0,
      documents_queued: 0,
    });
    statusMock
      .mockResolvedValueOnce(STATUS)
      .mockResolvedValueOnce({ ...STATUS, freshness: 'seeded', stale_doc_types: [] });
    const onChanged = vi.fn();

    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={onChanged} />);
    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.recompile' }));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(await screen.findByText('compile.lifecycle.seeded')).toBeTruthy();
  });

  it('工具栏「从模板重置」先二次确认，确认后才调用 reseed', async () => {
    reseedMock.mockResolvedValue({});
    const onChanged = vi.fn();

    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={onChanged} />);
    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.reseed' }));

    // 一击不得直接执行（会丢弃人工编辑）。
    expect(reseedMock).not.toHaveBeenCalled();
    expect(await screen.findByText('compile.lifecycle.reseedConfirmContent')).toBeTruthy();

    fireEvent.click(await screen.findByRole('button', { name: 'common.confirm' }));

    await waitFor(() =>
      expect(reseedMock).toHaveBeenCalledWith('kb1', 's1', 'd1', false),
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it('勾选「重编译后重置文档」后，预览弹窗内重置透传 apply_to_documents=true', async () => {
    previewMock.mockResolvedValue(PREVIEW);
    reseedMock.mockResolvedValue({});

    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);
    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.preview' }));
    await screen.findByText('compile.lifecycle.added');

    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'compile.lifecycle.confirmReseed' }));

    await waitFor(() =>
      expect(reseedMock).toHaveBeenCalledWith('kb1', 's1', 'd1', true),
    );
  });

  it('勾选「重编译后重置文档」后，重编译透传 applyToDocuments=true', async () => {
    previewMock.mockResolvedValue(PREVIEW);
    recompileMock.mockResolvedValue({
      schema_saved: true,
      schema_version: 4,
      unchanged: false,
      documents_matched: 0,
      documents_queued: 0,
    });

    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);
    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.preview' }));
    await screen.findByText('compile.lifecycle.added');

    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'compile.lifecycle.confirmRecompile' }));

    await waitFor(() =>
      expect(recompileMock).toHaveBeenCalledWith('kb1', {
        force: true,
        applyToDocuments: true,
      }),
    );
  });

  it('canWrite=false：动作按钮禁用', async () => {
    render(<SchemaLifecyclePanel kbId="kb1" canWrite={false} onChanged={() => {}} />);

    const preview = await screen.findByRole('button', { name: 'compile.lifecycle.preview' });
    const recompile = screen.getByRole('button', { name: 'compile.lifecycle.recompile' });
    const reseed = screen.getByRole('button', { name: 'compile.lifecycle.reseed' });

    expect((preview as HTMLButtonElement).disabled).toBe(true);
    expect((recompile as HTMLButtonElement).disabled).toBe(true);
    expect((reseed as HTMLButtonElement).disabled).toBe(true);
    // 禁用原因常显（title），不是只靠灰色按钮让人猜
    expect(preview.getAttribute('title')).toBe('domainSpace.noManagePermission');
    expect(recompile.getAttribute('title')).toBe('domainSpace.noManagePermission');
  });

  it('manual_edited：显示人工编辑提示且禁用重新编译', async () => {
    statusMock.mockResolvedValue({ ...STATUS, freshness: 'manual_edited', stale_doc_types: [] });
    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);

    expect(await screen.findByText('compile.lifecycle.manualEdited')).toBeTruthy();
    expect(screen.getByText('compile.lifecycle.manualHint')).toBeTruthy();
    const recompile = screen.getByRole('button', { name: 'compile.lifecycle.recompile' });
    expect((recompile as HTMLButtonElement).disabled).toBe(true);
    expect(recompile.getAttribute('title')).toBe('compile.lifecycle.manualHint');
  });

  it('refreshKey 变化重新拉取状态（页内编辑后的跟随刷新）', async () => {
    const { rerender } = render(
      <SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} refreshKey={0} />,
    );
    expect(await screen.findByText('compile.lifecycle.outdated')).toBeTruthy();
    expect(statusMock).toHaveBeenCalledTimes(1);

    rerender(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} refreshKey={1} />);

    await waitFor(() => expect(statusMock).toHaveBeenCalledTimes(2));
  });

  it('切换 kbId：立即清空旧状态，不拿旧模板去重置新 KB', async () => {
    statusMock
      .mockResolvedValueOnce(STATUS)
      .mockReturnValueOnce(new Promise(() => {}));

    const { rerender } = render(
      <SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />,
    );
    expect(await screen.findByText('compile.lifecycle.outdated')).toBeTruthy();

    rerender(<SchemaLifecyclePanel kbId="kb2" canWrite onChanged={() => {}} />);

    await waitFor(() => expect(statusMock).toHaveBeenCalledWith('kb2'));
    expect(screen.queryByText('compile.lifecycle.outdated')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('旧 KB 的响应晚到不覆盖新 KB 的状态（请求序号守卫）', async () => {
    let resolveOld!: (v: unknown) => void;
    statusMock
      .mockReturnValueOnce(new Promise((resolve) => {
        resolveOld = resolve;
      }))
      .mockResolvedValueOnce({
        ...STATUS,
        freshness: 'seeded',
        template_scenario_id: null,
        stale_doc_types: [],
      });

    const { rerender } = render(
      <SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />,
    );
    await waitFor(() => expect(statusMock).toHaveBeenCalledWith('kb1'));

    rerender(<SchemaLifecyclePanel kbId="kb2" canWrite onChanged={() => {}} />);
    expect(await screen.findByText('compile.lifecycle.seeded')).toBeTruthy();

    await act(async () => {
      resolveOld(STATUS);
    });

    expect(screen.queryByText('compile.lifecycle.outdated')).toBeNull();
    expect(screen.getByText('compile.lifecycle.seeded')).toBeTruthy();
  });

  it('预览响应晚到且 kbId 已切换：丢弃，不弹旧 KB 的预览（M-6）', async () => {
    let resolvePreview!: (v: unknown) => void;
    previewMock.mockReturnValue(new Promise((resolve) => {
      resolvePreview = resolve;
    }));
    statusMock
      .mockResolvedValueOnce(STATUS)
      .mockResolvedValueOnce({
        ...STATUS,
        freshness: 'seeded',
        template_scenario_id: null,
        stale_doc_types: [],
      });

    const { rerender } = render(
      <SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.preview' }));

    rerender(<SchemaLifecyclePanel kbId="kb2" canWrite onChanged={() => {}} />);
    expect(await screen.findByText('compile.lifecycle.seeded')).toBeTruthy();

    await act(async () => {
      resolvePreview(PREVIEW);
    });

    expect(screen.queryByText('compile.lifecycle.added')).toBeNull();
    // P1-1：旧响应被丢弃后不能把 loading 永久留在新 KB 上。
    const previewBtn = screen.getByRole('button', { name: 'compile.lifecycle.preview' });
    await waitFor(() => expect((previewBtn as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(previewBtn);
    await waitFor(() => expect(previewMock).toHaveBeenCalledWith('kb2'));
  });

  it('勾选后取消预览：工具栏重编译实参 applyToDocuments=false（P1-2）', async () => {
    previewMock.mockResolvedValue(PREVIEW);
    recompileMock.mockResolvedValue({
      schema_saved: true,
      schema_version: 4,
      unchanged: false,
      documents_matched: 0,
      documents_queued: 0,
    });

    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);
    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.preview' }));
    await screen.findByText('compile.lifecycle.added');

    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'common.cancel' }));

    // antd Modal 关闭后保留 DOM，不能用 queryByText 判断已关；以工具栏实参断言
    // 勾选状态确实已随关闭复位。
    fireEvent.click(screen.getByRole('button', { name: 'compile.lifecycle.recompile' }));

    await waitFor(() =>
      expect(recompileMock).toHaveBeenCalledWith('kb1', {
        force: true,
        applyToDocuments: false,
      }),
    );
  });

  it('勾选后取消预览：工具栏重置实参 apply_to_documents=false（P1-2）', async () => {
    previewMock.mockResolvedValue(PREVIEW);
    reseedMock.mockResolvedValue({});

    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);
    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.preview' }));
    await screen.findByText('compile.lifecycle.added');

    // 勾选 → 点弹窗 footer 的「取消」→ 再走工具栏重置。
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'common.cancel' }));

    fireEvent.click(screen.getByRole('button', { name: 'compile.lifecycle.reseed' }));
    expect(reseedMock).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole('button', { name: 'common.confirm' }));

    await waitFor(() =>
      expect(reseedMock).toHaveBeenCalledWith('kb1', 's1', 'd1', false),
    );
  });

  it('勾选并在弹窗内确认重置：apply_to_documents=true（P1-2 对照）', async () => {
    previewMock.mockResolvedValue(PREVIEW);
    reseedMock.mockResolvedValue({});

    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);
    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.preview' }));
    await screen.findByText('compile.lifecycle.added');

    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'compile.lifecycle.confirmReseed' }));

    await waitFor(() =>
      expect(reseedMock).toHaveBeenCalledWith('kb1', 's1', 'd1', true),
    );
  });

  it('勾选并在弹窗内确认重编译：applyToDocuments=true（P1-2 对照）', async () => {
    previewMock.mockResolvedValue(PREVIEW);
    recompileMock.mockResolvedValue({
      schema_saved: true,
      schema_version: 4,
      unchanged: false,
      documents_matched: 0,
      documents_queued: 0,
    });

    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);
    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.preview' }));
    await screen.findByText('compile.lifecycle.added');

    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'compile.lifecycle.confirmRecompile' }));

    await waitFor(() =>
      expect(recompileMock).toHaveBeenCalledWith('kb1', {
        force: true,
        applyToDocuments: true,
      }),
    );
  });

  it('reseed 进行中只有重置键 loading，重编译键不转圈', async () => {
    let resolveReseed!: (v: unknown) => void;
    reseedMock.mockImplementation(
      () => new Promise((resolve) => {
        resolveReseed = resolve;
      }),
    );
    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);

    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.reseed' }));
    fireEvent.click(await screen.findByRole('button', { name: 'common.confirm' }));

    const reseedBtn = () => screen.getByText('compile.lifecycle.reseed').closest('button')!;
    const recompileBtn = () =>
      screen.getByText('compile.lifecycle.recompile').closest('button')!;
    await waitFor(() => expect(reseedBtn().className).toContain('ant-btn-loading'));
    expect(recompileBtn().className).not.toContain('ant-btn-loading');

    await act(async () => {
      resolveReseed({});
    });
  });

  it('状态接口 reject：组件不崩（无状态渲染）且留 console.warn 痕迹', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    statusMock.mockRejectedValue(new Error('boom'));

    const { container } = render(
      <SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />,
    );

    await waitFor(() => expect(warn).toHaveBeenCalled());
    expect(container.querySelector('button')).toBeNull();
  });

  it('running 期间重复点击只发一次重编译请求', async () => {
    let resolveRecompile!: (v: unknown) => void;
    recompileMock.mockImplementation(
      () => new Promise((resolve) => {
        resolveRecompile = resolve;
      }),
    );
    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);

    const btn = await screen.findByRole('button', { name: 'compile.lifecycle.recompile' });
    fireEvent.click(btn);
    fireEvent.click(btn);

    await waitFor(() => expect(recompileMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      resolveRecompile({
        schema_saved: true,
        schema_version: 4,
        unchanged: false,
        documents_matched: 0,
        documents_queued: 0,
      });
    });
    await waitFor(() => expect(statusMock).toHaveBeenCalledTimes(2));
  });

  it('动作失败透出后端 message（不吞成通用文案）', async () => {
    const errorSpy = vi.spyOn(message, 'error').mockImplementation((() => undefined) as never);
    recompileMock.mockRejectedValue(new Error('后端拒绝：版本冲突'));

    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);
    fireEvent.click(await screen.findByRole('button', { name: 'compile.lifecycle.recompile' }));

    await waitFor(() => expect(errorSpy).toHaveBeenCalledWith('后端拒绝：版本冲突'));
  });

  it('未知 freshness：中性灰 Tag + 原值显示，不回落成「已生成」', async () => {
    statusMock.mockResolvedValue({ ...STATUS, freshness: 'brand_new' });
    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);

    expect(await screen.findByText('compile.lifecycle.unknown: brand_new')).toBeTruthy();
    expect(screen.queryByText('compile.lifecycle.seeded')).toBeNull();
  });

  it('无模板场景：重置按钮禁用并常显原因', async () => {
    statusMock.mockResolvedValue({ ...STATUS, template_scenario_id: null });
    render(<SchemaLifecyclePanel kbId="kb1" canWrite onChanged={() => {}} />);

    const reseed = await screen.findByRole('button', { name: 'compile.lifecycle.reseed' });
    expect((reseed as HTMLButtonElement).disabled).toBe(true);
    expect(reseed.getAttribute('title')).toBe('compile.lifecycle.templateMissing');
  });
});
