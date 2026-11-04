import { createServer } from 'node:http'
import pkg from 'http-proxy'
const { createProxyServer } = pkg

// ============================================================
// Environment (.env) — 按本文件位置加载，避免 --env-file 相对 cwd
// 解析在不同启动方式（pnpm --filter / 直接 node / VSCode）下不一致
// ============================================================
try {
  process.loadEnvFile(new URL('./.env', import.meta.url))
} catch {
  // .env 缺失时使用下方默认值，不阻塞启动
}

// ============================================================
// Configuration
// ============================================================
const PORT = parseInt(process.env.GATEWAY_PORT || '8080', 10)

const UPSTREAMS = {
  // Backend API gateway
  api: process.env.API_TARGET || 'http://localhost:8000',
  // Frontend Vite dev servers
  shell: process.env.SHELL_TARGET || 'http://localhost:5173',
  'expert-call': process.env.EXPERT_CALL_TARGET || 'http://localhost:5174',
  'core-business': process.env.CORE_BUSINESS_TARGET || 'http://localhost:5175',
  'ecosystem-management': process.env.ECOSYSTEM_MANAGEMENT_TARGET || 'http://localhost:5176',
  'platform-management': process.env.PLATFORM_MANAGEMENT_TARGET || 'http://localhost:5177',
}

const REMOTE_ENTRY_MODULES = {
  'expert-call': '__federation_remote_expertCall_entry',
  'core-business': '__federation_remote_coreBusiness_entry',
  'ecosystem-management': '__federation_remote_ecosystemManagement_entry',
  'platform-management': '__federation_remote_platformManagement_entry',
}

// ============================================================
// HTTP Proxy
// ============================================================
const proxy = createProxyServer({
  changeOrigin: true,
  ws: true,
  proxyTimeout: 60000,
  timeout: 60000,
})

proxy.on('error', (err, req, res) => {
  console.error(`[ERROR] Proxy: ${err.message}`)
  if (res?.writeHead) {
    res.writeHead(502, { 'Content-Type': 'text/plain' })
    res.end('Bad Gateway')
  }
})

proxy.on('proxyRes', (proxyRes, req) => {
  // Security headers (mirroring Nginx deploy/nginx/app-locations.conf)
  proxyRes.headers['X-Frame-Options'] = 'SAMEORIGIN'
  proxyRes.headers['X-XSS-Protection'] = '1; mode=block'
  proxyRes.headers['X-Content-Type-Options'] = 'nosniff'
  proxyRes.headers['Referrer-Policy'] = 'no-referrer-when-downgrade'
})

// ============================================================
// Route matching
// ============================================================
const SUB_APPS = ['expert-call', 'core-business', 'ecosystem-management', 'platform-management']

function findRoute(pathname) {
  // 1. Health check
  if (pathname === '/health') {
    return { handler: true }
  }

  // 2. 演化台（服务端）：试跑/调整/采纳要走真实 ②/LLM 链（正常 30-60s，上游降级时
  //    可能跑数分钟）——全局 60s 会贴线截断，浏览器只得 ERR_EMPTY_RESPONSE
  //    （2026-10-13 真机实测 58s 险过、超线即误报「网络异常」）。
  //    预算链（网关须为全长，先于任何上游放行）：试跑/调整 = 前端 TIMEOUT_REPLAY/ADJUST
  //    320s；采纳 = 前端 TIMEOUT_ADOPT 600s。故取 610s 覆盖两者（终审 M1：330s 仍会
  //    静默截断 600s 档的采纳）。
  if (pathname.startsWith('/api/v1/knowledge-base/evolution')) {
    return { target: UPSTREAMS.api, timeoutMs: 610_000 }
  }

  // 3. Backend API
  if (pathname.startsWith('/api/')) {
    return { target: UPSTREAMS.api }
  }

  // 4. Remote entry (Module Federation dev server virtual module)
  for (const app of SUB_APPS) {
    const entryPrefix = `/remotes/${app}/assets/remoteEntry.js`
    if (pathname === entryPrefix) {
      const moduleName = REMOTE_ENTRY_MODULES[app]
      if (moduleName) {
        return { target: UPSTREAMS[app], path: `/@id/__x00__virtual:${moduleName}` }
      }
    }
  }

  // 5. Remote assets (Module Federation) — strip /remotes prefix
  for (const app of SUB_APPS) {
    const prefix = `/remotes/${app}/`
    if (pathname.startsWith(prefix)) {
      const newPath = pathname.replace(`/remotes/${app}`, `/${app}`)
      return { target: UPSTREAMS[app], path: newPath }
    }
  }

  // 6. Standalone SPA routes
  for (const app of SUB_APPS) {
    if (pathname.startsWith(`/${app}/`) || pathname === `/${app}`) {
      return { target: UPSTREAMS[app] }
    }
  }

  // 7. Shell — catch-all
  return { target: UPSTREAMS.shell }
}

// ============================================================
// HTTP server
// ============================================================
const server = createServer((req, res) => {
  let url
  try {
    url = new URL(req.url, `http://${req.headers.host || 'localhost'}`)
  } catch {
    // 畸形 URL（如 "//"）会让 new URL 抛 ERR_INVALID_URL，必须兜底，
    // 否则未捕获异常会直接杀死进程（历史崩溃根因）。
    res.writeHead(400, { 'Content-Type': 'text/plain' })
    res.end('Bad Request')
    return
  }
  const route = findRoute(url.pathname)

  // Health check
  if (route.handler) {
    res.writeHead(200, { 'Content-Type': 'text/plain' })
    res.end('ok')
    return
  }

  // Apply path rewrite for remote assets
  if (route.path != null) {
    req.url = route.path + url.search
  }

  console.log(`  → ${req.method} ${req.url} [${route.target}]`)

  // 每路由可覆盖全局 60s 超时（如演化台 610s）；不设则沿 createProxyServer 默认。
  const proxyOpts = { target: route.target }
  if (route.timeoutMs) {
    proxyOpts.proxyTimeout = route.timeoutMs
    proxyOpts.timeout = route.timeoutMs
  }

  proxy.web(req, res, proxyOpts, (err) => {
    console.error(`[ERROR] ${req.method} ${req.url} → ${err.message}`)
    if (!res.headersSent) {
      res.writeHead(502, { 'Content-Type': 'text/plain' })
      res.end('Bad Gateway')
    }
  })
})

// ============================================================
// WebSocket upgrade (Vite HMR)
// ============================================================
server.on('upgrade', (req, socket, head) => {
  let url
  try {
    url = new URL(req.url, `http://${req.headers.host || 'localhost'}`)
  } catch {
    socket.destroy()
    return
  }
  const route = findRoute(url.pathname)

  if (route.target) {
    proxy.ws(req, socket, head, { target: route.target })
  } else {
    socket.destroy()
  }
})

// ============================================================
// Start
// ============================================================
server.listen(PORT, () => {
  console.log('')
  console.log('╔══════════════════════════════════════════╗')
  console.log('║   Jonex Platform Dev Gateway            ║')
  console.log(`║   http://localhost:${String(PORT).padEnd(5)}                     ║`)
  console.log('╚══════════════════════════════════════════╝')
  console.log('')
  console.log('Upstreams:')
  console.log(`  API               → ${UPSTREAMS.api}`)
  console.log(`  Shell             → ${UPSTREAMS.shell}`)
  console.log(`  Expert Call       → ${UPSTREAMS['expert-call']}`)
  console.log(`  Core Business     → ${UPSTREAMS['core-business']}`)
  console.log(`  Eco Management    → ${UPSTREAMS['ecosystem-management']}`)
  console.log(`  Platform Mgmt     → ${UPSTREAMS['platform-management']}`)
  console.log('')
  console.log('Routing:')
  console.log('  /api/*                              → API')
  console.log('  /remotes/<app>/* (→ strip /remotes) → Sub-app Vite dev server')
  console.log('  /<app>/* (standalone SPA)           → Sub-app Vite dev server')
  console.log('  /* (catch-all)                       → Shell Vite dev server')
  console.log('')
})
