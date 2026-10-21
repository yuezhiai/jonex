const APP_PATH_PATTERN = /^\/apps\/([^/?#]+)/;

function appIdOf(path: string): string | null {
  const match = APP_PATH_PATTERN.exec(path);
  return match ? match[1] : null;
}

/**
 * 子应用回报的地址栏路径，shell 该不该采纳进自己的 router。
 *
 * 只认 `/apps/<appId>/...`（登录页等不归子应用），且只在**换了应用**时才采纳：
 * 同一应用内部的跳转由子应用自己写地址栏就够，采纳会让 AppHost 重算 iframe 的 src，
 * 把已经导航好的子应用整个重载一遍，白白丢掉它的 SPA 状态。
 */
export function shouldAdoptRoute(currentPath: string, incoming: unknown): incoming is string {
  if (typeof incoming !== 'string') return false;
  const incomingAppId = appIdOf(incoming);
  return incomingAppId !== null && incomingAppId !== appIdOf(currentPath);
}
