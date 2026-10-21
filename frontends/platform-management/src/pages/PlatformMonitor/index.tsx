import { useCallback, useMemo, useState } from 'react';
import { Button, DatePicker, Select, Tabs, message } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import dayjs, { type Dayjs } from 'dayjs';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import { PageContent } from '@jonex/shared-lib';
import {
  PM_DEFAULT_WINDOW,
  PM_MAX_CUSTOM_DAYS,
  PM_WINDOW_OPTIONS,
  toPmTabKey,
  toPmWindowChoice,
  type PmWindowChoice,
} from '../../constants/platformMonitor';
import { PM_TIME_PATTERN, PM_TIMEZONE, fmtPmInstant, fmtPmTime } from './time';
import type { PmTabKey, PmWindow } from './types';

const { RangePicker } = DatePicker;

type CustomRange = [Dayjs, Dayjs];

/**
 * 日期控件给的是**浏览器本地时区**的墙钟时间，页面展示却统一是 `Asia/Shanghai`。
 * 不换算的话，非东八区浏览器上「选 10:00」拿到的会是本地 10:00 那一刻，
 * 与页面上看到的北京时间对不上。这里把选中的墙钟时间按平台时区重新解释。
 */
const toPlatformTime = (value: Dayjs): Dayjs => dayjs.tz(value.format('YYYY-MM-DDTHH:mm:ss'), PM_TIMEZONE);

/** 首次点开「自定义」时的默认区间：近 7 天（与 Token 默认档一致），按平台时区算 */
const defaultCustomRange = (): CustomRange => {
  const now = dayjs().tz(PM_TIMEZONE);
  return [now.subtract(6, 'day').startOf('day'), now];
};
import TaskMonitor from './TaskMonitor';
import CallMonitor from './CallMonitor';
import TokenMonitor from './TokenMonitor';
import './index.scss';

/**
 * 平台监控：一个页面、三个页签（任务 / 调用 / Token），调用监控下再分三个二级页签。
 *
 * 页签落在 `?tab=`（二级落 `?sub=`），链接可直达、刷新不丢。
 *
 * 页面**没有**大标题：设计稿里页签本身就是页面的一级导航，再加一行标题会多出一块
 * 与下方 tabs 重复的视觉重量。文档标题走路由的 `title`（见 useDocumentTitle）。
 */
export default function PlatformMonitor() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = toPmTabKey(searchParams.get('tab'));

  /**
   * 时间窗也落在 URL 上（`?win=24h` / `?win=custom&from=&to=`）：监控页经常被截图、
   * 贴到群里讨论，链接得能还原成「他说的是哪个窗口」。
   *
   * 只在挂载时读一次。之后控件改了就直接回写 URL（见 `writeWindow`），不做双向同步 ——
   * 那样每次取数都要先过一轮 effect，切页签时还会互相覆盖。
   */
  const [initialWindow] = useState<{ choice: PmWindowChoice; range: CustomRange }>(() => {
    const choice = toPmWindowChoice(tab, searchParams.get('win')) ?? PM_DEFAULT_WINDOW[tab];
    const fromRaw = searchParams.get('from');
    const toRaw = searchParams.get('to');
    // dayjs(undefined) 是「现在」而不是「无效」，两个参数都拿到了才解析。
    // 转成平台时区再交给控件：回显要跟页面其余时间同一套口径
    if (fromRaw && toRaw) {
      const from = dayjs(fromRaw).tz(PM_TIMEZONE);
      const to = dayjs(toRaw).tz(PM_TIMEZONE);
      if (from.isValid() && to.isValid()) return { choice, range: [from, to] };
    }
    return { choice, range: defaultCustomRange() };
  });

  // 时间窗按页签各存一份：设计稿里任务/调用是「24小时」而 Token 是「近7天」，
  // 共用一个值会让切到 Token 时窗口被前一个页签带着走。
  const [choices, setChoices] = useState<Record<PmTabKey, PmWindowChoice>>(() => ({
    ...PM_DEFAULT_WINDOW,
    [tab]: initialWindow.choice,
  }));
  // 自定义区间同样按页签各存一份，切回来不会丢上次选的范围
  const [customRanges, setCustomRanges] = useState<Record<PmTabKey, CustomRange>>(() => ({
    task: defaultCustomRange(),
    call: defaultCustomRange(),
    token: defaultCustomRange(),
    [tab]: initialWindow.range,
  }));

  /**
   * 把「页签 + 窗口」写回 URL。切页签时一并带上新页签自己的窗口，链接才是完整的。
   *
   * **必须在现有参数上合并**：筛选条件（调用记录的 status / source 等）也存在 URL 上，
   * 从空参数重建会把它们直接抹掉。
   */
  const writeWindow = (nextTab: PmTabKey, nextChoice: PmWindowChoice, range: CustomRange) => {
    const next = new URLSearchParams(searchParams);
    next.set('tab', nextTab);
    next.set('win', nextChoice);
    if (nextChoice === 'custom') {
      next.set('from', range[0].toISOString());
      next.set('to', range[1].toISOString());
    } else {
      // 切回预设档位后 from/to 已无意义，留着会让链接看起来还停在自定义区间
      next.delete('from');
      next.delete('to');
    }
    // sub 只对调用页签有意义，切走时清掉，免得别的页签链接里挂着一个无效的二级页签
    if (nextTab !== 'call') next.delete('sub');
    setSearchParams(next);
  };

  // 刷新：递增一个计数进各页的取数依赖。不用时间戳 —— 那会让每次渲染都变、把取数打成死循环。
  const [refreshKey, setRefreshKey] = useState(0);
  /** 页签回传的后端落库时刻；空串/缺失时退回本地取数时刻 */
  const [asOf, setAsOf] = useState<string | null>(null);
  const [loadedAt, setLoadedAt] = useState(() => new Date());
  const handleLoaded = useCallback((nextAsOf?: string) => {
    setLoadedAt(new Date());
    // undefined = 这次取数没有 as-of 信息（如只刷了列表），保留上一份，免得被清成空
    if (nextAsOf !== undefined) setAsOf(nextAsOf.trim() ? nextAsOf : null);
  }, []);

  const choice = choices[tab];
  const customRange = customRanges[tab];

  /**
   * 传给页签的时间窗。自定义档要包成对象，且**必须 memo**：
   * 页签把它当 effect / useCallback 的依赖，每次渲染新建对象会把取数打成死循环。
   */
  const window = useMemo<PmWindow>(() => {
    if (choice !== 'custom') return choice;
    const [from, to] = customRange;
    return { preset: 'custom', from: from.toISOString(), to: to.toISOString() };
  }, [choice, customRange]);

  const handleCustomRange = (range: (Dayjs | null)[] | null) => {
    const [from, to] = range ?? [];
    if (!from || !to) return;
    // 控件值是浏览器本地墙钟，先按平台时区解释再参与跨度校验与下发
    const platformFrom = toPlatformTime(from);
    const platformTo = toPlatformTime(to);
    // 在控件的选择阶段就拦住，别让用户点出一个必然 4004 的请求
    if (platformTo.diff(platformFrom, 'hour', true) > PM_MAX_CUSTOM_DAYS * 24) {
      message.warning(t('platformMonitor.window.tooLong', { max: PM_MAX_CUSTOM_DAYS }));
      return;
    }
    setCustomRanges((prev) => ({ ...prev, [tab]: [platformFrom, platformTo] }));
    writeWindow(tab, 'custom', [platformFrom, platformTo]);
  };

  const panes = { window, refreshKey, onLoaded: handleLoaded };

  return (
    <PageContent showCard={false}>
      <div className="yx-pm-page">
        <Tabs
          className="yx-pm-tabs"
          activeKey={tab}
          // 切页签重置 sub —— 二级页签由 CallMonitor 自己维护，切走再回来回落到第一个；
          // 窗口带上新页签自己的那份，否则链接里的 win 会停在旧页签的档位
          onChange={(key) => {
            const nextTab = key as PmTabKey;
            writeWindow(nextTab, choices[nextTab], customRanges[nextTab]);
          }}
          // 切走即卸载：三个页签都在挂载时取数，留着不卸会让切回来的数字停在几分钟前
          destroyOnHidden
          tabBarExtraContent={
            <div className="yx-pm-toolbar">
              <Select
                size="small"
                className="yx-pm-window-select"
                value={choice}
                onChange={(value: PmWindowChoice) => {
                  setChoices((prev) => ({ ...prev, [tab]: value }));
                  writeWindow(tab, value, customRange);
                }}
                options={PM_WINDOW_OPTIONS[tab].map((option) => ({ value: option.value, label: t(option.labelKey) }))}
              />
              {choice === 'custom' ? (
                <RangePicker
                  size="small"
                  className="yx-pm-window-range"
                  allowClear={false}
                  showTime={{ format: 'HH:mm' }}
                  format="MM-DD HH:mm"
                  value={customRange}
                  onChange={handleCustomRange}
                  // 统计口径截至当前，选未来时间没有意义。日历按平台时区解释，
                  // 禁用判断也要用平台时区的「今天」，否则非东八区浏览器会多禁/少禁一天
                  disabledDate={(current) =>
                    current.format('YYYY-MM-DD') > dayjs().tz(PM_TIMEZONE).format('YYYY-MM-DD')
                  }
                  placeholder={[t('platformMonitor.window.start'), t('platformMonitor.window.end')]}
                />
              ) : null}
              <Button size="small" icon={<ReloadOutlined />} onClick={() => setRefreshKey((key) => key + 1)}>
                {t('platformMonitor.refresh')}
              </Button>
              <span className="yx-pm-updated-at">
                {/* 后端 as_of 才是「数据截止到何时」；降级空壳没有它，才退回本地取数时刻。
                    两者都按平台时区显示：任务族的时刻带 +08:00，浏览器在别的时区时会差一截 */}
                {t('platformMonitor.updatedAt', {
                  time: asOf
                    ? (fmtPmTime(asOf, PM_TIME_PATTERN.clock) ?? '-')
                    : fmtPmInstant(loadedAt, PM_TIME_PATTERN.clock),
                })}
              </span>
            </div>
          }
          items={[
            { key: 'task', label: t('platformMonitor.tabs.task'), children: <TaskMonitor {...panes} /> },
            { key: 'call', label: t('platformMonitor.tabs.call'), children: <CallMonitor {...panes} /> },
            { key: 'token', label: t('platformMonitor.tabs.token'), children: <TokenMonitor {...panes} /> },
          ]}
        />
      </div>
    </PageContent>
  );
}
