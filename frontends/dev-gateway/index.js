import { createServer } from 'node:http'
import pkg from 'http-proxy'
const { createProxyServer } = pkg

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

  // 2. Backend API
  if (pathname.startsWith('/api/')) {
    return { target: UPSTREAMS.api }
  }

  // 3. Remote entry (Module Federation dev server virtual module)
  for (const app of SUB_APPS) {
    const entryPrefix = `/remotes/${app}/assets/remoteEntry.js`
    if (pathname === entryPrefix) {
      const moduleName = REMOTE_ENTRY_MODULES[app]
      if (moduleName) {
        return { target: UPSTREAMS[app], path: `/@id/__x00__virtual:${moduleName}` }
      }
    }
  }

  // 4. Remote assets (Module Federation) — strip /remotes prefix
  for (const app of SUB_APPS) {
    const prefix = `/remotes/${app}/`
    if (pathname.startsWith(prefix)) {
      const newPath = pathname.replace(`/remotes/${app}`, `/${app}`)
      return { target: UPSTREAMS[app], path: newPath }
    }
  }

  // 5. Standalone SPA routes
  for (const app of SUB_APPS) {
    if (pathname.startsWith(`/${app}/`) || pathname === `/${app}`) {
      return { target: UPSTREAMS[app] }
    }
  }

  // 6. Shell — catch-all
  return { target: UPSTREAMS.shell }
}

// ============================================================
// HTTP server
// ============================================================
const server = createServer((req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`)
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

  proxy.web(req, res, { target: route.target }, (err) => {
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
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`)
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
