/**
 * 文档列表的「铺满剩余高度」回归测试。
 *
 * 真实故障（反馈）：在领域知识库的文档列表里把「每页条数」调大后，页面没有滚动条，
 * 看不见下面的行，分页条也被挤出视口——翻页和再次改页尺寸都点不到。
 *
 * 测的是几何：表体必须自己滚（scrollHeight > clientHeight），分页条必须留在视口内，
 * 宿主容器不该被内容撑破。这三点只有真浏览器能量，单测里 jsdom 没有布局。
 */
import { test, expect, type Page } from '@playwright/test';

const KB_ID = 'kb-doc-list';

const ROW_COUNT = 30;

function docItem(i: number) {
  return {
    id: `doc-${i}`,
    knowledge_base_id: KB_ID,
    file_name: `测试文档-${String(i).padStart(2, '0')}.pdf`,
    file_type: 'pdf',
    file_size: 1024 * (i + 1),
    status: 'ready',
    ontology_status: 'done',
    created_at: '2026-11-01T10:00:00',
    updated_at: '2026-11-01T10:00:00',
    folder_id: null,
    data_source_type: 'file',
    mime_type: 'application/pdf',
    metadata: { uploader: 'e2e' },
  };
}

/** 统一响应信封 */
// 网关成功信封是 code: 0 —— shared-lib 的响应拦截器把 code !== 0 一律当失败
const ok = (data: unknown) => ({
  success: true,
  code: 0,
  message: 'ok',
  data,
});

test.describe('文档列表铺满剩余高度', () => {
  test.beforeEach(async ({ page }) => {
    // 直接进有登录态的 standalone：省掉跳登录页
    await page.addInitScript(() => {
      localStorage.setItem('jonex_access_token', 'e2e-token');
    });

    // 兜底：没显式 mock 的接口一律回空，避免整页报错
    await page.route('**/api/v1/**', async (route) => {
      const url = route.request().url();
      // eslint-disable-next-line no-console
      console.log('[e2e] unmocked:', route.request().method(), url);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(ok({ items: [], total: 0 })),
      });
    });

    await page.route('**/api/v1/auth/me', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          ok({
            user: {
              id: 'u-e2e',
              username: 'e2e',
              display_name: 'E2E',
              tenant_id: 'tenant_e2e',
              tenant_name: 'E2E 租户',
              roles: ['admin'],
            },
          }),
        ),
      });
    });

    await page.route(`**/api/v1/knowledge-base/knowledge-info/${KB_ID}**`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          ok({
            id: KB_ID,
            name: '文档列表布局用例库',
            space_id: 'space-1',
            space_name: '默认空间',
            document_count: ROW_COUNT,
            entity_count: 0,
            relation_count: 0,
            status: 'synced',
            kb_type: 'lightrag',
            structured_enabled: false,
            updated_at: '2026-11-01T10:00:00',
            can_write: true,
            can_manage_permissions: true,
          }),
        ),
      });
    });

    await page.route('**/api/v1/knowledge-base/documents/stats**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          ok({
            total: ROW_COUNT,
            processing: 0,
            completed: ROW_COUNT,
            compile_failed: 0,
            parse_failed: 0,
          }),
        ),
      });
    });

    await page.route('**/api/v1/knowledge-base/documents/quality-reports**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(ok({ items: [] })),
      });
    });

    // 列表本体：按后端口径给一页数据，total 固定，翻页/改页尺寸都由前端重取
    await page.route('**/api/v1/knowledge-base/documents?**', async (route) => {
      const url = new URL(route.request().url());
      const page = Number(url.searchParams.get('page') || '1');
      const pageSize = Number(url.searchParams.get('page_size') || '10');
      const start = (page - 1) * pageSize;
      const items = Array.from({ length: Math.max(0, Math.min(pageSize, ROW_COUNT - start)) }, (_, i) =>
        docItem(start + i + 1),
      );
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(ok({ items, total: ROW_COUNT, page, page_size: pageSize })),
      });
    });

    await page.route('**/api/v1/knowledge-base/folders**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(ok({ items: [] })),
      });
    });

    await page.route('**/api/v1/ecosystem/data-access-methods**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(ok({ items: [] })),
      });
    });
  });

  /** 每页 50 条后量几何：表体自己滚、分页条留在视口内、宿主不被撑破 */
  async function assertFillLayout(page: Page) {
    const rows = page.locator('.doc-library-table .ant-table-tbody > tr.ant-table-row');
    await expect(rows).toHaveCount(ROW_COUNT);

    const metrics = await page.evaluate(() => {
      const body = document.querySelector('.doc-library-table .ant-table-body') as HTMLElement | null;
      const pagination = document.querySelector('.doc-library-table .ant-pagination') as HTMLElement | null;
      const host = document.querySelector('.doc-library-table-host') as HTMLElement | null;
      return {
        viewportHeight: window.innerHeight,
        bodyClientHeight: body?.clientHeight ?? -1,
        bodyScrollHeight: body?.scrollHeight ?? -1,
        paginationBottom: pagination?.getBoundingClientRect().bottom ?? -1,
        hostClientHeight: host?.clientHeight ?? -1,
        hostScrollHeight: host?.scrollHeight ?? -1,
      };
    });

    // eslint-disable-next-line no-console
    console.log('[geometry]', metrics);

    // 表体必须有内部滚动条：数据比可视区多
    expect(metrics.bodyScrollHeight).toBeGreaterThan(metrics.bodyClientHeight);
    // 分页条不能被挤出视口（否则「翻页」「改页尺寸」都点不到）
    expect(metrics.paginationBottom).toBeLessThanOrEqual(metrics.viewportHeight);
    // 宿主容器不该被内容撑破（撑破 = 溢出 .doc-library-main 的 overflow:hidden 被裁掉）
    expect(metrics.hostScrollHeight).toBeLessThanOrEqual(metrics.hostClientHeight + 1);
  }

  /** 把每页条数改成 50：30 行全铺出来，远超一屏 */
  async function switchToFifty(page: Page) {
    await page.locator('.ant-pagination-options .ant-select').click();
    await page
      .locator('.ant-select-dropdown .ant-select-item-option')
      .filter({ hasText: /^50/ })
      .click();
  }

  test('每页条数放大后，表体内部滚动、分页条仍在视口内', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 720 });
    await page.goto(`/core-business/domain-knowledge/${KB_ID}`);

    // 默认 10 条先落定
    const rows = page.locator('.doc-library-table .ant-table-tbody > tr.ant-table-row');
    await expect(rows.first()).toBeVisible();
    await expect(rows).toHaveCount(10);

    // 默认页尺寸下翻页要能用（故障时整条分页都被裁在视口外）
    await page.locator('.ant-pagination-item-2').click();
    await expect(rows.first()).toContainText('测试文档-11');

    await switchToFifty(page);
    await assertFillLayout(page);
  });

  test('从情况追踪页签切进文档列表，首屏不卡加载、布局同样撑住', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 720 });
    // 文档库面板原本没挂载：切页签那一刻才首次挂载，量高与取数同时发生
    await page.goto(`/core-business/domain-knowledge/${KB_ID}?tab=tracking`);
    await page.locator('.ant-tabs-tab[data-node-key="documents"]').click();

    const rows = page.locator('.doc-library-table .ant-table-tbody > tr.ant-table-row');
    await expect(rows.first()).toBeVisible();
    // 遮罩必须收掉（历史故障：轮询的静默重取顶掉了首屏取数，loading 永久为 true）
    await expect(page.locator('.doc-library-table .ant-spin-spinning')).toHaveCount(0);

    await switchToFifty(page);
    await assertFillLayout(page);
  });
});
