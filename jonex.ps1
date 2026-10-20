<#
============================================================
 Jonex Platform 开发/部署脚本 (jonex.ps1)
 用法: .\jonex.ps1 <命令> [服务名]     完整命令: .\jonex.ps1 help
 执行策略拦截时: powershell -ExecutionPolicy Bypass -File .\jonex.ps1 help
============================================================

【1. 依赖安装】
  # Python（根目录，装进 .venv）
  uv sync                          # 或: pip install -e ".[dev]"
  # 前端（frontends/ 的 pnpm workspace）
  .\jonex.ps1 frontends-install    # 等价: cd frontends; pnpm install
  .\jonex.ps1 frontends-env        # 初始化各前端子应用 .env（从 .env.example 复制）

【2. 本地开发（后端用 VSCode Debug 断点调试，推荐）】
  说明: 后端断点调试走 .vscode/launch.json（本脚本不再提供本机后端启动命令）。
        中间件地址集中在根级 .env.local 一处改（本地=127.0.0.1 / 线上=服务器 IP）。
  步骤:
    a) 需要本地中间件时才起（连服务器中间件则跳过此步）:
         .\jonex.ps1 dev-infra-up         # postgres/redis/etcd/minio/milvus
    b) VSCode 调试面板选 compound 启动后端:
         "Jonex: Full Stack (无 RAG)"       # 6 个业务后端 + 前端 + 浏览器
         "Backend: All (无 RAG)"          # 仅 6 个业务后端
       （LightRAG/Atomic RAG 需 .venv-atomic-rag；没建就用"无 RAG"的 compound，
         RAG 走服务器，由 .env.local 的 ATOMIC_RAG_URL 指向）
    c) 前端（统一入口需先起 Dev Gateway，也可直接用 VSCode 的 Frontend 配置）:
         .\jonex.ps1 dev-gateway          # Dev Gateway 统一入口 http://localhost:8080（前端前提）
         .\jonex.ps1 dev-frontend         # shell + core/platform/ecosystem
         .\jonex.ps1 dev-frontend-shell   # 仅 shell (http://localhost:5173)

【3. 本地容器构建 / 启动（整套跑在 Docker 里）】
  .\jonex.ps1 init            # 首次: 生成 deploy/.env、.env.rag、.env.mcp、.env.openkb、.env.jonex_wiki
  .\jonex.ps1 build           # 构建镜像（自动先建 jonex/python-base:local）
  .\jonex.ps1 up              # 启动整套（中间件+后端+前端+RAG）
  .\jonex.ps1 ps              # 状态
  .\jonex.ps1 logs            # 跟随日志
  .\jonex.ps1 down            # 停止并删除
  # 单服务重建: .\jonex.ps1 rebuild <服务名>   例: gateway / core-business-frontend
  # 也可用 local- 前缀形式: local-build / local-up / local-ps / local-logs / local-down

【3.1 数据库增量迁移（存量库迭代，不重建数据）】
  前提: jonex-postgres 容器在跑（.\jonex.ps1 dev-infra-up 或 up）
  .\jonex.ps1 db-migrate-dry    # 预览待应用迁移（不执行）
  .\jonex.ps1 db-migrate        # 应用待应用迁移（列清单 + 确认）
  .\jonex.ps1 db-migrate-yes    # 跳过确认直接执行（CI / 自动化）
  # 状态记在 public.schema_migrations：已应用的跳过、失败的登记不上，重复执行安全

【3.2 配置基线校验（.env 实例 vs 模板）】
  .\jonex.ps1 env-check         # 比对差异（缺失/多余/未登记）
  .\jonex.ps1 env-check-all     # 同上，但列出全部键名（默认按前缀聚合）

 更多命令（GPU / 单服务 / 性能日志等）: .\jonex.ps1 help
============================================================
#>


# Jonex Platform PowerShell Script

param(
    [Parameter(Position=0)]
    [string]$Command = "help",

    [Parameter(Position=1)]
    [string]$Service = "",

    [Parameter(Position=2)]
    [string]$N = "1"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSCommandPath
$DeployDir = Join-Path $RepoRoot "deploy"
$FrontendsDir = Join-Path $RepoRoot "frontends"

$MiddlewareServices = @("postgres", "redis", "etcd", "minio", "milvus")
$RagServices = @("lightrag", "atomic-rag")
$BackendServices = @("gateway", "sidecar", "knowledge-base-service", "business-domain-service", "task-center-service", "platform-service", "mcp-server")
$FrontendServices = @("frontend-gateway", "shell-frontend", "core-business-frontend", "platform-management-frontend", "ecosystem-management-frontend")

$ShellApp = "@jonex/shell"
$CoreApp = "@jonex/core-business"
$PlatformApp = "@jonex/platform-management"
$EcosystemApp = "@jonex/ecosystem-management"

# 短名 → docker-compose 服务名映射（供 rebuild/rebuild-service 等空格命令使用）
$ServiceNameMap = @{
    "knowledge-base" = "knowledge-base-service"
    "business-domain" = "business-domain-service"
    "platform" = "platform-service"
    "lightrag" = "lightrag"
    "atomic-rag" = "atomic-rag"
    "frontend-gateway" = "frontend-gateway"
    "shell-frontend" = "shell-frontend"
    "core-business-frontend" = "core-business-frontend"
    "platform-management-frontend" = "platform-management-frontend"
    "ecosystem-management-frontend" = "ecosystem-management-frontend"
}

$MainFrontendFilters = @("--filter", $ShellApp, "--filter", $CoreApp, "--filter", $PlatformApp, "--filter", $EcosystemApp)

if ($Service -match "^SERVICE=(.+)$") {
    $Service = $Matches[1]
}
if ($N -match "^N=(.+)$") {
    $N = $Matches[1]
}

$ComposeBaseFiles = @("-f", "docker-compose.yml")
$ComposeGpuFiles = $ComposeBaseFiles + @("-f", "docker-compose.gpu.yml")
$ComposeLocalFiles = $ComposeBaseFiles
$ComposeDevFiles = $ComposeLocalFiles + @("-f", "docker-compose.override.yml")
$ComposeGpuDevFiles = $ComposeDevFiles + @("-f", "docker-compose.gpu.yml")

function Invoke-Compose {
    param(
        [ValidateSet("Base", "Dev", "Gpu", "GpuDev")]
        [string]$Mode,
        [string[]]$ComposeArgs
    )

    $files = switch ($Mode) {
        "Base" { $ComposeLocalFiles }
        "Dev" { $ComposeDevFiles }
        "Gpu" { $ComposeGpuFiles }
        "GpuDev" { $ComposeGpuDevFiles }
    }

    Push-Location $DeployDir
    try {
        & docker compose @files @ComposeArgs
    }
    finally {
        Pop-Location
    }
}

function Enable-ComposeBake {
    # 让 docker compose build 委托 buildx bake（并行 + 缓存），并跳过 fs 授权交互
    $env:DOCKER_BUILDKIT = "1"
    $env:COMPOSE_BAKE = "1"
    if (-not $env:BUILDX_BAKE_ENTITLEMENTS_FS) { $env:BUILDX_BAKE_ENTITLEMENTS_FS = "0" }
}

function Build-PythonBase {
    # 构建共享基础镜像并 --load 进本地镜像库；7 个后端服务通过
    # docker-compose.yml 的 additional_contexts(docker-image://jonex/python-base:local) 复用它。
    # 不预先构建会导致 compose 把它当作 registry 镜像去拉取而报 403/not found。
    # 镜像已存在时跳过构建，如需强制重建使用 build-base 命令。
    $existing = & docker images -q jonex/python-base:local 2>$null
    if ($existing) {
        Write-Host "=== 共享基础镜像 jonex/python-base:local 已存在，跳过构建 ===" -ForegroundColor Green
        return
    }
    Write-Host "=== 构建共享基础镜像 jonex/python-base:local ===" -ForegroundColor Cyan
    Push-Location $RepoRoot
    try {
        & docker buildx build --load -t jonex/python-base:local -f deploy/docker/python-base.Dockerfile .
        if ($LASTEXITCODE -ne 0) { throw "python-base 镜像构建失败（退出码 $LASTEXITCODE）" }
    }
    finally {
        Pop-Location
    }
}

function Invoke-Pnpm {
    param([string[]]$PnpmArgs)

    Push-Location $FrontendsDir
    try {
        & pnpm @PnpmArgs
    }
    finally {
        Pop-Location
    }
}

function Resolve-GitBash {
    # 显式定位 Git for Windows 的 bash，**不靠 PATH 解析**。
    #
    # ⚠️ 为什么不能用 `Get-Command bash`：Windows 上这个名字有**三个**来源，
    # 而 `Get-Command` 返回哪个取决于调用方 PATH 的顺序 ——
    #   ① Git for Windows 的 bash（我们要的：有 mktemp/shop 等，且继承 Windows PATH
    #      因而能用 docker.exe）；
    #   ② `C:\Windows\System32\bash.exe` —— WSL 的启动器；
    #   ③ `%LOCALAPPDATA%\Microsoft\WindowsApps\bash.exe` —— WSL 的 App Execution Alias。
    # ②③ 在没装发行版时会直接炸（`execvpe /bin/bash failed 2`），而且**即使装了发行版**
    # 那个环境里也没有宿主机的 docker —— 拿它跑 apply.sh 必然失败。
    # 实测：同一条命令在 `-NoProfile` 下解析到 ①，在用户带 profile 的会话里解析到 ②，
    # 于是"在我这儿能跑"。所以这里绕开 PATH，按已知安装位置找，找不到就明确报错。
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:ProgramFiles) {
        $candidates.Add((Join-Path $env:ProgramFiles 'Git\bin\bash.exe'))
        $candidates.Add((Join-Path $env:ProgramFiles 'Git\usr\bin\bash.exe'))
    }
    $pf86 = ${env:ProgramFiles(x86)}
    if ($pf86) {
        $candidates.Add((Join-Path $pf86 'Git\bin\bash.exe'))
    }
    if ($env:LOCALAPPDATA) {
        $candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe'))
    }
    # 兜底：git.exe 的路径能反推 Git 安装根（...\Git\mingw64\bin\git.exe → ...\Git）
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($git -and $git.Source) {
        $gitRoot = Split-Path -Parent (Split-Path -Parent $git.Source)
        if ($gitRoot) {
            $candidates.Add((Join-Path $gitRoot 'bin\bash.exe'))
            $candidates.Add((Join-Path $gitRoot 'usr\bin\bash.exe'))
        }
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

function Invoke-DbMigrate {
    # 增量 DDL：跑 deploy/postgres/update/apply.sh（与 Makefile 的 make db-migrate 同一份脚本，
    # 行为一致：只应用 public.schema_migrations 里没登记过的文件、成功才登记、失败不登记）。
    #
    # ⚠️ 为什么交互确认放在 PowerShell 这边、而不是留给 apply.sh：
    #    apply.sh 里有一句「stdin 非 TTY（或 CI 环境）时自动视为 --yes」，本意是防 CI 卡死。
    #    但从 PowerShell 调 bash 时 stdin 算不算 TTY 并不可靠 —— 一旦判成非 TTY，
    #    `db-migrate` 会在用户**没看到任何提示**的情况下把待应用迁移全部执行掉。
    #    所以这里自己问、问完显式传 --yes：提示一定看得见，脚本一侧的行为也就不再依赖 TTY 判定。
    param(
        [switch]$DryRun,
        [switch]$Yes
    )

    $scriptPath = Join-Path $RepoRoot "deploy\postgres\update\apply.sh"
    if (-not (Test-Path $scriptPath)) {
        Write-Host "找不到迁移脚本: $scriptPath" -ForegroundColor Red
        exit 1
    }

    # apply.sh 是 bash 脚本（从宿主机读文件、经 stdin 送进容器执行）。
    # 用显式定位的 Git Bash，**不用 PATH 里的 `bash`** —— 理由见 Resolve-GitBash 的注释。
    $bashPath = Resolve-GitBash
    if (-not $bashPath) {
        Write-Host "未找到 Git Bash —— apply.sh 需要它（装 Git for Windows 即可）。" -ForegroundColor Red
        Write-Host "⚠️ 注意：PATH 里的 `bash` 可能是 WSL 的启动器（System32 或 WindowsApps 下），" -ForegroundColor DarkGray
        Write-Host "   那个跑不了本脚本（WSL 里没有宿主机的 docker）。" -ForegroundColor DarkGray
        Write-Host "可手工执行：" -ForegroundColor Yellow
        Write-Host '  & "C:\Program Files\Git\bin\bash.exe" deploy/postgres/update/apply.sh --dry-run   # 预览' -ForegroundColor Yellow
        Write-Host '  & "C:\Program Files\Git\bin\bash.exe" deploy/postgres/update/apply.sh             # 执行（会问一次）' -ForegroundColor Yellow
        exit 1
    }

    if (-not $DryRun -and -not $Yes) {
        # ⚠️ **失败即拒绝**：stdin 被重定向时 `Read-Host` 的行为不可靠 ——
        # 它可能读到管道里的值、可能立刻返回空、也可能在无控制台时直接抛。
        # 实测踩过一次：`... | & .\jonex.ps1 db-migrate` 这种调用下确认被跳过、
        # 迁移**直接应用了**（那正是这条护栏存在的理由）。
        # 与其赌它会问，不如问不了就不做 —— 要无人值守请显式用 db-migrate-yes。
        if ([Console]::IsInputRedirected) {
            Write-Host "标准输入被重定向，无法交互确认（Read-Host 在此环境下不可靠）。" -ForegroundColor Red
            Write-Host "本命令**不会**在没有确认的情况下执行迁移。请改用：" -ForegroundColor Yellow
            Write-Host "  .\jonex.ps1 db-migrate-dry   # 先预览待应用清单" -ForegroundColor Yellow
            Write-Host "  .\jonex.ps1 db-migrate-yes   # 确认无误后跳过询问直接执行" -ForegroundColor Yellow
            exit 1
        }

        Write-Host "即将把 deploy/postgres/update/ 里**尚未应用**的迁移应用到 jonex-postgres。" -ForegroundColor Yellow
        Write-Host "（迁移状态记在 public.schema_migrations：已应用的跳过、失败的不登记，重复执行安全）" -ForegroundColor DarkGray
        Write-Host "建议先预览: .\jonex.ps1 db-migrate-dry" -ForegroundColor Yellow
        $answer = Read-Host "确认执行？[y/N]"
        if ($answer -notmatch '^(y|yes)$') {
            Write-Host "已取消，未执行任何迁移。"
            exit 0
        }
    }

    # ⚠️ 用 Push-Location 到仓库根再调：apply.sh 自己会定位目录，但相对路径参数与
    # 出错信息的可读性都以仓库根为基准最自然。
    # ⚠️ `exit` **不放在 try 里** —— 避免依赖「exit 会不会触发 finally」这种细节，
    # 出口统一在函数末尾。
    # 两处**必须在调用原生命令时**做的处理，缺一个输出就不能看：
    #
    # ① `[Console]::OutputEncoding = UTF8`：apply.sh 输出的是 UTF-8（脚本里全是中文），
    #    而 PowerShell 5.1 默认按控制台 OEM 码页（中文 Windows 上是 GBK/936）去解 ——
    #    结果是中文乱码，而且部分字节序列会**吃掉换行**，几十行挤成一坨。
    #    用完恢复，别把调用方的会话编码改掉。
    #
    # ② `2>&1 | ForEach-Object { Write-Host "$_" }`：psql 会把
    #    `NOTICE: relation ... already exists` 这类**正常提示**写到 stderr，
    #    PowerShell 会把它包成 NativeCommandError、打一整块红字。
    #    命令其实成功了，看起来却像失败。转成字符串再 Write-Host 就只剩 message 本身
    #    （注意必须是 `"$_"` 而不是 `$_` —— 后者会打印 ErrorRecord 的格式化块）。
    #
    # `$LASTEXITCODE` 不受这两处影响，仍是 apply.sh 的真实退出码。
    # ③ `$ErrorActionPreference` 必须临时降到 Continue：本脚本头部设的是 **Stop**
    #    （`jonex.ps1:66`），而 Stop 下「原生命令写了 stderr」会被当成**终止性**错误 ——
    #    上面那个 `2>&1 | ForEach-Object` 根本来不及跑，整条管道直接中断并打出一大块
    #    NativeCommandError。而 psql 的 NOTICE（"relation ... already exists, skipping"）
    #    是**正常提示**，不是失败 —— 命令确实成功了。
    #    真实的成败仍由 `$LASTEXITCODE` 承载，末尾原样透传出去。
    $exitCode = 0
    $oldEncoding = [Console]::OutputEncoding
    $oldEap = $ErrorActionPreference
    Push-Location $RepoRoot
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $ErrorActionPreference = 'Continue'
        if ($DryRun) {
            & $bashPath "deploy/postgres/update/apply.sh" --dry-run 2>&1 | ForEach-Object { Write-Host "$_" }
        }
        else {
            & $bashPath "deploy/postgres/update/apply.sh" --yes 2>&1 | ForEach-Object { Write-Host "$_" }
        }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
        [Console]::OutputEncoding = $oldEncoding
        Pop-Location
    }
    exit $exitCode
}

function Invoke-EnvCheck {
    # .env 基线校验：跑 deploy/env-check.sh（与 Makefile 的 make env-check 同一份脚本，
    # 行为一致：比对每个 .env 实例与它的 .example 模板，报「缺失/多余/未登记」三类差异）。
    #
    # ⚠️ 与 db-migrate 同源的两个坑（完整理由见 Invoke-DbMigrate 的注释）：
    #   ① 脚本输出的是 UTF-8 中文，PowerShell 5.1 默认按控制台 OEM 码页（中文 Windows
    #      上是 GBK/936）去解 → 中文乱码，且部分字节序列会**吃掉换行**、几十行挤成一坨。
    #   ② 本脚本头部设了 $ErrorActionPreference = "Stop"，Stop 下「原生命令写了 stderr」
    #      会被当成终止性错误。env-check.sh 本身不写 stderr，但保持与 db-migrate 同一形状
    #      （临时降到 Continue + 2>&1 转字符串），免得日后脚本一改 stderr 就炸。
    param([switch]$All)

    $scriptPath = Join-Path $RepoRoot "deploy\env-check.sh"
    if (-not (Test-Path $scriptPath)) {
        Write-Host "找不到校验脚本: $scriptPath" -ForegroundColor Red
        exit 1
    }

    # env-check.sh 是与 Makefile 共用的 bash 脚本（同 apply.sh）：必须用显式定位的
    # Git Bash，**不用 PATH 里的 bash** —— 理由见 Resolve-GitBash 的注释。
    $bashPath = Resolve-GitBash
    if (-not $bashPath) {
        Write-Host "未找到 Git Bash —— env-check.sh 需要它（装 Git for Windows 即可）。" -ForegroundColor Red
        Write-Host "可手工执行：" -ForegroundColor Yellow
        Write-Host '  & "C:\Program Files\Git\bin\bash.exe" deploy/env-check.sh' -ForegroundColor Yellow
        exit 1
    }

    $exitCode = 0
    $oldEncoding = [Console]::OutputEncoding
    $oldEap = $ErrorActionPreference
    Push-Location $RepoRoot
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $ErrorActionPreference = 'Continue'
        if ($All) {
            & $bashPath "deploy/env-check.sh" --all 2>&1 | ForEach-Object { Write-Host "$_" }
        }
        else {
            & $bashPath "deploy/env-check.sh" 2>&1 | ForEach-Object { Write-Host "$_" }
        }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldEap
        [Console]::OutputEncoding = $oldEncoding
        Pop-Location
    }
    # ⚠️ 有「缺失/多余」时 env-check.sh **故意**返回 1（给 pre-commit / CI 留的挂载点），
    #    不是 PowerShell 出错 —— 原样透传，语义与 make env-check 保持一致。
    exit $exitCode
}

function Invoke-PerfLog {
    # 查询某服务的历史日志并按关键字过滤（性能耗时埋点，详见 docs/ingestion-timing-metrics-design.md §10）
    # $Service 位置参数可传一个数字覆盖默认 tail 行数，例如: .\jonex.ps1 perf-ingest 5000
    param(
        [string]$ServiceName,
        [string]$Pattern
    )

    $tail = "1000"
    if ($Service -match '^\d+$') { $tail = $Service }

    Push-Location $DeployDir
    try {
        & docker compose @ComposeDevFiles logs "--tail=$tail" $ServiceName | Select-String $Pattern
    }
    finally {
        Pop-Location
    }
}

function Require-Service {
    if ([string]::IsNullOrWhiteSpace($Service)) {
        Write-Host "请指定服务名，例如:" -ForegroundColor Yellow
        Write-Host "  .\jonex.ps1 rebuild gateway"
        Write-Host "  .\jonex.ps1 rebuild core-business-frontend"
        Write-Host "  .\jonex.ps1 restart-service gateway"
        Write-Host "  .\jonex.ps1 logs-service SERVICE=gateway"
        exit 1
    }
}

function Import-DotEnv {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    foreach ($rawLine in Get-Content $Path) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) {
            continue
        }
        $idx = $line.IndexOf("=")
        if ($idx -le 0) {
            continue
        }
        $key = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
}

function Set-LocalBackendServiceUrls {
    $env:ENV = "dev"
    $env:SIDECAR_URL = "http://127.0.0.1:8001"
    $env:KNOWLEDGE_BASE_URL = "http://127.0.0.1:8003"
    $env:BUSINESS_DOMAIN_URL = "http://127.0.0.1:8005"
    $env:ATOMIC_RAG_URL = "http://127.0.0.1:8004"
    $env:PLATFORM_URL = "http://127.0.0.1:8006"
}

function Set-LocalBackendEnv {
    Import-DotEnv (Join-Path $DeployDir ".env")

    if (-not $env:DB_PORT) { $env:DB_PORT = "5432" }
    if (-not $env:DB_USERNAME) { $env:DB_USERNAME = "jonex" }
    if (-not $env:DB_PASSWORD) { $env:DB_PASSWORD = "change-me" }
    if (-not $env:DB_NAME) { $env:DB_NAME = "jonex" }

    $env:DB_HOST = "127.0.0.1"
    $env:REDIS_URL = "redis://127.0.0.1:6379/0"
    $env:MILVUS_HOST = "127.0.0.1"
    $env:MILVUS_PORT = "19530"
    Set-LocalBackendServiceUrls
}

$BuildServiceCommands = @{
    "build-sidecar" = "sidecar"
    "build-knowledge-base" = "knowledge-base-service"
    "build-business-domain" = "business-domain-service"
    "build-platform" = "platform-service"
}

$UpServiceCommands = @{
    "up-sidecar" = "sidecar"
    "up-knowledge-base" = "knowledge-base-service"
    "up-business-domain" = "business-domain-service"
    "up-platform" = "platform-service"
    "up-postgres" = "postgres"
    "up-redis" = "redis"
    "up-lightrag" = "lightrag"
    "up-keycloak" = "keycloak"
}

$DownServiceCommands = @{
    "down-gateway" = "gateway"
    "down-sidecar" = "sidecar"
    "down-knowledge-base" = "knowledge-base-service"
    "down-business-domain" = "business-domain-service"
    "down-platform" = "platform-service"
}

$LogsServiceCommands = @{
    "logs-gateway" = "gateway"
    "logs-sidecar" = "sidecar"
    "logs-knowledge-base" = "knowledge-base-service"
    "logs-business-domain" = "business-domain-service"
    "logs-platform" = "platform-service"
    "logs-lightrag" = "lightrag"
    "logs-postgres" = "postgres"
    "logs-redis" = "redis"
    "logs-milvus" = "milvus"
    "logs-etcd" = "etcd"
    "logs-minio" = "minio"
    "logs-keycloak" = "keycloak"
    "logs-mcp-server" = "mcp-server"
}

$RebuildServiceCommands = @{
    "rebuild-gateway" = "gateway"
    "rebuild-sidecar" = "sidecar"
    "rebuild-knowledge-base" = "knowledge-base-service"
    "rebuild-business-domain" = "business-domain-service"
    "rebuild-platform" = "platform-service"
    "rebuild-frontend-gateway" = "frontend-gateway"
    "rebuild-shell-frontend" = "shell-frontend"
    "rebuild-core-business-frontend" = "core-business-frontend"
    "rebuild-platform-management-frontend" = "platform-management-frontend"
    "rebuild-ecosystem-management-frontend" = "ecosystem-management-frontend"
}

$RestartServiceCommands = @{
    "restart-gateway" = "gateway"
    "restart-sidecar" = "sidecar"
    "restart-knowledge-base" = "knowledge-base-service"
    "restart-business-domain" = "business-domain-service"
    "restart-platform" = "platform-service"
    "restart-frontend-gateway" = "frontend-gateway"
    "restart-shell-frontend" = "shell-frontend"
    "restart-core-business-frontend" = "core-business-frontend"
    "restart-platform-management-frontend" = "platform-management-frontend"
    "restart-ecosystem-management-frontend" = "ecosystem-management-frontend"
}

$ExecServiceCommands = @{
    "exec-gateway" = @("gateway", "bash")
    "exec-sidecar" = @("sidecar", "bash")
    "exec-knowledge-base" = @("knowledge-base-service", "bash")
    "exec-business-domain" = @("business-domain-service", "bash")
    "exec-platform" = @("platform-service", "bash")
    "exec-lightrag" = @("lightrag", "bash")
    "exec-shell-frontend" = @("shell-frontend", "sh")
    "shell-gateway" = @("gateway", "bash")
    "shell-sidecar" = @("sidecar", "bash")
    "shell-knowledge-base" = @("knowledge-base-service", "bash")
    "shell-business-domain" = @("business-domain-service", "bash")
    "shell-platform" = @("platform-service", "bash")
    "shell-lightrag" = @("lightrag", "bash")
    "shell-frontend" = @("shell-frontend", "sh")
}

if ($BuildServiceCommands.ContainsKey($Command)) {
    Build-PythonBase
    Enable-ComposeBake
    Invoke-Compose -Mode Base -ComposeArgs @("build", $BuildServiceCommands[$Command])
    exit $LASTEXITCODE
}
if ($UpServiceCommands.ContainsKey($Command)) {
    Invoke-Compose -Mode Dev -ComposeArgs @("up", "-d", $UpServiceCommands[$Command])
    exit $LASTEXITCODE
}
if ($DownServiceCommands.ContainsKey($Command)) {
    Invoke-Compose -Mode Dev -ComposeArgs @("stop", $DownServiceCommands[$Command])
    Invoke-Compose -Mode Dev -ComposeArgs @("rm", "-f", $DownServiceCommands[$Command])
    exit $LASTEXITCODE
}
if ($LogsServiceCommands.ContainsKey($Command)) {
    Invoke-Compose -Mode Dev -ComposeArgs @("logs", "-f", "--tail=100", $LogsServiceCommands[$Command])
    exit $LASTEXITCODE
}
if ($RebuildServiceCommands.ContainsKey($Command)) {
    Build-PythonBase
    Enable-ComposeBake
    Invoke-Compose -Mode Dev -ComposeArgs @("build", $RebuildServiceCommands[$Command])
    Invoke-Compose -Mode Dev -ComposeArgs @("up", "-d", $RebuildServiceCommands[$Command])
    exit $LASTEXITCODE
}
if ($RestartServiceCommands.ContainsKey($Command)) {
    Invoke-Compose -Mode Dev -ComposeArgs @("restart", $RestartServiceCommands[$Command])
    exit $LASTEXITCODE
}
if ($ExecServiceCommands.ContainsKey($Command)) {
    $execArgs = $ExecServiceCommands[$Command]
    Invoke-Compose -Mode Dev -ComposeArgs (@("exec", $execArgs[0]) + $execArgs[1])
    exit $LASTEXITCODE
}

switch ($Command) {
    "help" {
        Write-Host "=== Jonex Platform Commands ===" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "本地开发:"
        Write-Host "  .\jonex.ps1 dev                         启动中间件 + RAG，并提示下一步"
        Write-Host "  .\jonex.ps1 dev-deps-up                 启动 postgres/redis/etcd/minio/milvus/lightrag/atomic-rag"
        Write-Host "  .\jonex.ps1 dev-infra-up                只启动 postgres/redis/etcd/minio/milvus"
        Write-Host "  .\jonex.ps1 up-keycloak                 启动 Keycloak（SSO，本地 dev/test）"
        Write-Host "  .\jonex.ps1 keycloak-db-init            幂等建 keycloak 独立库（存量库）"
        Write-Host "  .\jonex.ps1 dev-rag-up                  只启动 lightrag/atomic-rag"
        Write-Host "  .\jonex.ps1 dev-deps-down               停止本地开发依赖"
        Write-Host "  .\jonex.ps1 dev-deps-logs               查看本地开发依赖日志"
        Write-Host ""
        Write-Host "本机后端: 用 VSCode Debug 断点调试（.vscode/launch.json），中间件地址在 .env.local 里改。"
        Write-Host "          常用 compound: 'Jonex: Full Stack (无 RAG)' / 'Backend: All (无 RAG)'"
        Write-Host ""
        Write-Host "本机前端:"
        Write-Host "  .\jonex.ps1 frontends-install           安装/同步 frontends workspace 依赖"
        Write-Host "  .\jonex.ps1 frontends-env               初始化各前端子应用 .env（从 .env.example 复制）"
        Write-Host "  .\jonex.ps1 dev-gateway                 Dev Gateway 统一入口: http://localhost:8080（前端前提）"
        Write-Host "  .\jonex.ps1 dev-frontend                启动 shell + core/platform/ecosystem"
        Write-Host "  .\jonex.ps1 dev-frontend-shell          Shell: http://localhost:5173"
        Write-Host "  .\jonex.ps1 dev-frontend-core           Core Business: http://localhost:5175"
        Write-Host "  .\jonex.ps1 dev-frontend-ecosystem      Ecosystem Management: http://localhost:5176"
        Write-Host "  .\jonex.ps1 dev-frontend-platform       Platform Management: http://localhost:5177"
        Write-Host ""
        Write-Host "本地 Docker 联调:"
        Write-Host "  .\jonex.ps1 init                        初始化 deploy/.env、deploy/.env.rag、deploy/.env.mcp、deploy/.env.openkb、deploy/.env.jonex_wiki 与前端 .env"
        Write-Host "  .\jonex.ps1 build-base                  强制重建 jonex/python-base 基础镜像"
        Write-Host "  .\jonex.ps1 build                       构建镜像"
        Write-Host "  .\jonex.ps1 up                          启动整套服务"
        Write-Host "  .\jonex.ps1 down                        停止并删除整套服务"
        Write-Host "  .\jonex.ps1 ps                          查看状态"
        Write-Host "  .\jonex.ps1 logs                        查看日志"
        Write-Host "  .\jonex.ps1 restart                     重启整套服务"
        Write-Host "  # 也可用 local- 前缀形式: local-build / local-up / local-ps / local-logs / local-down / local-restart"
        Write-Host ""
        Write-Host "数据库增量迁移（存量库迭代，不重建数据；前提: jonex-postgres 在跑）:"
        Write-Host "  .\jonex.ps1 db-migrate-dry              预览待应用迁移（不执行）"
        Write-Host "  .\jonex.ps1 db-migrate                  应用待应用迁移（列清单 + 确认）"
        Write-Host "  .\jonex.ps1 db-migrate-yes              跳过确认直接执行（CI/自动化）"
        Write-Host "  # 状态记在 public.schema_migrations：已应用的跳过、失败的登记不上，重复执行安全"
        Write-Host ""
        Write-Host "配置基线校验（.env 实例 vs 模板；与 make env-check 同一份脚本）:"
        Write-Host "  .\jonex.ps1 env-check                   比对差异（缺失/多余/未登记）"
        Write-Host "  .\jonex.ps1 env-check-all               同上，但列出全部键名（默认按前缀聚合）"
        Write-Host "  # 有「缺失/多余」时退出码为 1（给 pre-commit / CI 留的挂载点）"
        Write-Host ""
        Write-Host "GPU/服务器 Docker:"
        Write-Host "  .\jonex.ps1 gpu-build                    构建 GPU 镜像"
        Write-Host "  .\jonex.ps1 gpu-up                       启动 GPU 部署"
        Write-Host "  .\jonex.ps1 gpu-down                     停止 GPU 部署"
        Write-Host "  .\jonex.ps1 gpu-ps                       查看 GPU 部署状态"
        Write-Host "  .\jonex.ps1 gpu-logs                     查看 GPU 部署日志"
        Write-Host ""
        Write-Host "单服务操作:"
        Write-Host "  .\jonex.ps1 rebuild gateway             删除旧镜像→--no-cache 构建→启动（源码调试）"
        Write-Host "  .\jonex.ps1 local-restart-service gateway"
        Write-Host "  .\jonex.ps1 local-logs-service knowledge-base-service"
        Write-Host ""
        Write-Host "性能耗时日志（摄入链路埋点，详见 docs/ingestion-timing-metrics-design.md）:"
        Write-Host "  .\jonex.ps1 perf                        汇总查看 ingest_timing + reconcile_timing"
        Write-Host "  .\jonex.ps1 perf-ingest                 worker 分阶段耗时 ingest_timing（atomic-rag）"
        Write-Host "  .\jonex.ps1 perf-reconcile              对账入图库耗时 reconcile_timing（knowledge-base-service）"
        Write-Host "  .\jonex.ps1 perf-chunk                  LightRAG 内部 chunk_timing（lightrag，拆 extract/merge/persist）"
        Write-Host "  .\jonex.ps1 perf-thinking               关思考注入 thinking.disabled（llm-gateway）"
        Write-Host "  .\jonex.ps1 perf-extract                抽取场景调用 lightrag_extract（llm-gateway，看 latency/token）"
        Write-Host "  .\jonex.ps1 perf-search                 本体检索 RAG 线路耗时 ontology_search_timing（多库检索/融合）"
        Write-Host "  .\jonex.ps1 perf-audit                  审计表耗时 audit_logs.duration_ms（postgres）"
        Write-Host "  # 默认查最近 1000 行历史，可传行数覆盖: .\jonex.ps1 perf-ingest 5000"
    }

    "init" {
        Write-Host "=== 初始化环境 ===" -ForegroundColor Cyan
        $envFile = Join-Path $DeployDir ".env"
        $envExample = Join-Path $DeployDir ".env.example"
        if (-not (Test-Path $envFile)) {
            Copy-Item $envExample $envFile
            Write-Host "已创建平台配置: deploy\.env" -ForegroundColor Green
        } else {
            Write-Host "平台配置已存在: deploy\.env" -ForegroundColor Yellow
        }

        $envRagFile = Join-Path $DeployDir ".env.rag"
        $envRagExample = Join-Path $DeployDir ".env.rag.example"
        if (-not (Test-Path $envRagFile)) {
            Copy-Item $envRagExample $envRagFile
            Write-Host "已创建 RAG 配置: deploy\.env.rag" -ForegroundColor Green
        } else {
            Write-Host "RAG 配置已存在: deploy\.env.rag" -ForegroundColor Yellow
        }

        $envMcpFile = Join-Path $DeployDir ".env.mcp"
        $envMcpExample = Join-Path $DeployDir ".env.mcp.example"
        if (-not (Test-Path $envMcpFile)) {
            Copy-Item $envMcpExample $envMcpFile
            Write-Host "已创建 MCP 配置: deploy\.env.mcp" -ForegroundColor Green
        } else {
            Write-Host "MCP 配置已存在: deploy\.env.mcp" -ForegroundColor Yellow
        }

        $envOpenkbFile = Join-Path $DeployDir ".env.openkb"
        $envOpenkbExample = Join-Path $DeployDir ".env.openkb.example"
        if (-not (Test-Path $envOpenkbFile)) {
            Copy-Item $envOpenkbExample $envOpenkbFile
            Write-Host "已创建 OpenKB 配置: deploy\.env.openkb" -ForegroundColor Green
        } else {
            Write-Host "OpenKB 配置已存在: deploy\.env.openkb" -ForegroundColor Yellow
        }

        $envWikiFile = Join-Path $DeployDir ".env.jonex_wiki"
        $envWikiExample = Join-Path $DeployDir ".env.jonex_wiki.example"
        if (-not (Test-Path $envWikiFile)) {
            Copy-Item $envWikiExample $envWikiFile
            Write-Host "已创建 Jonex Wiki 配置: deploy\.env.jonex_wiki" -ForegroundColor Green
        } else {
            Write-Host "Jonex Wiki 配置已存在: deploy\.env.jonex_wiki" -ForegroundColor Yellow
        }
        Write-Host "下一步: 按需修改 deploy\.env、deploy\.env.rag、deploy\.env.mcp、deploy\.env.openkb 和 deploy\.env.jonex_wiki" -ForegroundColor Yellow

        # 同时初始化前端 .env
        & $PSCommandPath "frontends-env"
    }

    "version" {
        & docker --version
        Push-Location $DeployDir
        try {
            & docker compose version
        }
        finally {
            Pop-Location
        }
    }

    { $_ -in "build", "build-local", "local-build", "build-prod", "build-server" } {
        Build-PythonBase
        Enable-ComposeBake
        Invoke-Compose -Mode Base -ComposeArgs @("build")
    }

    { $_ -in "build-gpu", "gpu-build" } { Build-PythonBase; Invoke-Compose -Mode Base -ComposeArgs @("build") }

    "build-base" {
        Write-Host "=== 强制重建共享基础镜像 jonex/python-base:local ===" -ForegroundColor Cyan
        Push-Location $RepoRoot
        try {
            & docker buildx build --load --no-cache -t jonex/python-base:local -f deploy/docker/python-base.Dockerfile .
            if ($LASTEXITCODE -ne 0) { throw "python-base 镜像构建失败（退出码 $LASTEXITCODE）" }
        }
        finally {
            Pop-Location
        }
    }
    "build-infra" { Invoke-Compose -Mode Base -ComposeArgs (@("build") + $MiddlewareServices) }
    "build-rag" { Enable-ComposeBake; Invoke-Compose -Mode Base -ComposeArgs (@("build") + $RagServices) }
    "build-backend" { Build-PythonBase; Enable-ComposeBake; Invoke-Compose -Mode Base -ComposeArgs (@("build") + $BackendServices) }
    "build-frontend" { Enable-ComposeBake; Invoke-Compose -Mode Base -ComposeArgs (@("build") + $FrontendServices) }

    "build-service" {
        Require-Service
        Build-PythonBase
        Enable-ComposeBake
        Invoke-Compose -Mode Base -ComposeArgs @("build", $Service)
    }

    { $_ -in "up", "up-local", "local-up", "up-detached" } {
        Write-Host "=== 本地 Docker 部署 ===" -ForegroundColor Cyan
        Invoke-Compose -Mode Dev -ComposeArgs @("up", "-d")
        Write-Host "服务启动中: .\jonex.ps1 local-ps / .\jonex.ps1 local-logs"
    }

    { $_ -in "up-gpu", "gpu-up" } {
        Write-Host "=== GPU Docker 部署 ===" -ForegroundColor Cyan
        Invoke-Compose -Mode GpuDev -ComposeArgs @("up", "-d")
    }

    { $_ -in "up-server", "up-prod" } {
        Write-Host "=== 生产 Docker 部署 ===" -ForegroundColor Cyan
        Invoke-Compose -Mode Base -ComposeArgs @("up", "-d")
    }

    "dev-infra-up" {
        Invoke-Compose -Mode Dev -ComposeArgs (@("up", "-d") + $MiddlewareServices)
    }

    "dev-rag-up" {
        Invoke-Compose -Mode Dev -ComposeArgs (@("up", "-d") + $RagServices)
    }

    "up-backend" {
        Invoke-Compose -Mode Dev -ComposeArgs (@("up", "-d") + $BackendServices)
    }

    "up-frontend" {
        Invoke-Compose -Mode Dev -ComposeArgs (@("up", "-d") + $FrontendServices)
    }

    { $_ -in "up-service", "local-up-service" } {
        Require-Service
        Invoke-Compose -Mode Dev -ComposeArgs @("up", "-d", $Service)
    }

    "gpu-up-service" {
        Require-Service
        Invoke-Compose -Mode GpuDev -ComposeArgs @("up", "-d", $Service)
    }

    "up-milvus" {
        Invoke-Compose -Mode Dev -ComposeArgs @("up", "-d", "etcd", "minio", "milvus")
    }

    { $_ -in "down", "down-local", "local-down" } {
        Invoke-Compose -Mode Dev -ComposeArgs @("down")
    }

    { $_ -in "down-gpu", "gpu-down" } {
        Invoke-Compose -Mode Gpu -ComposeArgs @("down")
    }

    { $_ -in "down-server", "down-prod" } {
        Invoke-Compose -Mode Base -ComposeArgs @("down")
    }

    "down-v" {
        Write-Host "警告: 这会删除数据库、Redis、MinIO、Milvus 等数据卷。" -ForegroundColor Red
        $confirm = Read-Host "确认继续? (y/N)"
        if ($confirm -ne "y") { exit 1 }
        Invoke-Compose -Mode Dev -ComposeArgs @("down", "-v")
    }

    "stop" {
        Invoke-Compose -Mode Dev -ComposeArgs @("stop")
    }

    "stop-service" {
        Require-Service
        Invoke-Compose -Mode Dev -ComposeArgs @("stop", $Service)
    }

    { $_ -in "down-service", "local-down-service" } {
        Require-Service
        Invoke-Compose -Mode Dev -ComposeArgs @("stop", $Service)
        Invoke-Compose -Mode Dev -ComposeArgs @("rm", "-f", $Service)
    }

    "gpu-down-service" {
        Require-Service
        Invoke-Compose -Mode Gpu -ComposeArgs @("stop", $Service)
        Invoke-Compose -Mode Gpu -ComposeArgs @("rm", "-f", $Service)
    }

    "down-frontend" {
        Invoke-Compose -Mode Dev -ComposeArgs (@("stop") + $FrontendServices)
        Invoke-Compose -Mode Dev -ComposeArgs (@("rm", "-f") + $FrontendServices)
    }

    { $_ -in "restart", "local-restart" } {
        Invoke-Compose -Mode Dev -ComposeArgs @("restart")
    }

    { $_ -in "restart-gpu", "gpu-restart" } {
        Invoke-Compose -Mode GpuDev -ComposeArgs @("restart")
    }

    { $_ -in "restart-server", "restart-prod" } {
        Invoke-Compose -Mode Base -ComposeArgs @("restart")
    }

    { $_ -in "restart-service", "local-restart-service" } {
        Require-Service
        Invoke-Compose -Mode Dev -ComposeArgs @("restart", $Service)
    }

    "gpu-restart-service" {
        Require-Service
        Invoke-Compose -Mode GpuDev -ComposeArgs @("restart", $Service)
    }

    { $_ -in "recreate-service", "local-recreate-service" } {
        Require-Service
        Invoke-Compose -Mode Dev -ComposeArgs @("up", "-d", "--force-recreate", $Service)
    }

    { $_ -in "rebuild", "rebuild-service", "local-rebuild-service" } {
        Require-Service
        if ($ServiceNameMap.ContainsKey($Service)) { $Service = $ServiceNameMap[$Service] }
        Build-PythonBase
        Enable-ComposeBake

        Push-Location $DeployDir
        try {
            # 1) 构建新镜像（--no-cache 确保源码更改不被旧缓存层掩盖）
            Write-Host "=== 构建新镜像: $Service ===" -ForegroundColor Cyan
            & docker compose $ComposeDevFiles build --no-cache $Service
            if ($LASTEXITCODE -ne 0) { throw "构建失败（退出码 $LASTEXITCODE）" }

            # 2) 删除旧镜像（释放 dangling 镜像）
            Write-Host "=== 清理旧镜像 ===" -ForegroundColor Cyan
            & docker image prune -f --filter "dangling=true" 2>&1 | Out-Null

            # 3) 启动新容器（--force-recreate 确保用新镜像重建）
            Write-Host "=== 启动服务: $Service ===" -ForegroundColor Cyan
            & docker compose $ComposeDevFiles up -d --force-recreate $Service
        }
        finally {
            Pop-Location
        }
    }

    "gpu-rebuild-service" {
        Require-Service
        Build-PythonBase
        Invoke-Compose -Mode Base -ComposeArgs @("build", $Service)
        Invoke-Compose -Mode GpuDev -ComposeArgs @("up", "-d", $Service)
    }

    { $_ -in "ps", "local-ps" } { Invoke-Compose -Mode Dev -ComposeArgs @("ps") }
    { $_ -in "ps-gpu", "gpu-ps" } { Invoke-Compose -Mode Gpu -ComposeArgs @("ps") }
    { $_ -in "ps-server", "ps-prod" } { Invoke-Compose -Mode Base -ComposeArgs @("ps") }

    { $_ -in "logs", "local-logs" } {
        Invoke-Compose -Mode Dev -ComposeArgs @("logs", "-f", "--tail=100")
    }

    { $_ -in "logs-gpu", "gpu-logs" } {
        Invoke-Compose -Mode Gpu -ComposeArgs @("logs", "-f", "--tail=100")
    }

    { $_ -in "logs-server", "logs-prod" } {
        Invoke-Compose -Mode Base -ComposeArgs @("logs", "-f", "--tail=100")
    }

    { $_ -in "logs-service", "local-logs-service" } {
        Require-Service
        Invoke-Compose -Mode Dev -ComposeArgs @("logs", "-f", "--tail=100", $Service)
    }

    "gpu-logs-service" {
        Require-Service
        Invoke-Compose -Mode Gpu -ComposeArgs @("logs", "-f", "--tail=100", $Service)
    }

    "logs-infra" {
        Invoke-Compose -Mode Dev -ComposeArgs (@("logs", "-f", "--tail=100") + $MiddlewareServices)
    }

    "logs-rag" {
        Invoke-Compose -Mode Dev -ComposeArgs (@("logs", "-f", "--tail=100") + $RagServices)
    }

    "logs-backend" {
        Invoke-Compose -Mode Dev -ComposeArgs (@("logs", "-f", "--tail=100") + $BackendServices)
    }

    "logs-frontend" {
        Invoke-Compose -Mode Dev -ComposeArgs (@("logs", "-f", "--tail=100") + $FrontendServices)
    }

    { $_ -in "dev", "dev-local" } {
        Invoke-Compose -Mode Dev -ComposeArgs (@("up", "-d") + $MiddlewareServices + $RagServices)
        Write-Host ""
        Write-Host "本地开发依赖已启动。"
        Write-Host "后端: VSCode Debug 选 'Backend: All (无 RAG)' 或 'Jonex: Full Stack (无 RAG)'"
        Write-Host "前端: .\jonex.ps1 dev-frontend"
    }

    { $_ -in "dev-deps-up" } {
        Invoke-Compose -Mode Dev -ComposeArgs (@("up", "-d") + $MiddlewareServices + $RagServices)
    }

    { $_ -in "dev-deps-down" } {
        Invoke-Compose -Mode Dev -ComposeArgs (@("stop") + $MiddlewareServices + $RagServices)
    }

    { $_ -in "dev-deps-logs" } {
        Invoke-Compose -Mode Dev -ComposeArgs (@("logs", "-f", "--tail=100") + $MiddlewareServices + $RagServices)
    }

    { $_ -in "frontends-install", "frontend-install" } {
        Write-Host "=== 安装/同步 frontends workspace 依赖 ===" -ForegroundColor Cyan
        Invoke-Pnpm -PnpmArgs @("install")
    }

    { $_ -in "frontends-env", "frontend-env" } {
        Write-Host "=== 初始化前端环境变量 ===" -ForegroundColor Cyan
        $apps = @("shell", "core-business", "ecosystem-management", "platform-management", "dev-gateway")
        $frontendsDir = Join-Path $RepoRoot "frontends"
        foreach ($app in $apps) {
            $envFile = Join-Path $frontendsDir "$app\.env"
            $envExample = Join-Path $frontendsDir "$app\.env.example"
            if (Test-Path -Path $envExample) {
                if (-not (Test-Path -Path $envFile)) {
                    Copy-Item -Path $envExample -Destination $envFile
                    Write-Host "已创建: frontends\$app\.env" -ForegroundColor Green
                } else {
                    Write-Host "已存在: frontends\$app\.env" -ForegroundColor Yellow
                }
            } else {
                Write-Host "模板缺失: frontends\$app\.env.example" -ForegroundColor Red
            }
        }
    }

    { $_ -in "dev-gateway", "frontend-gateway" } {
        Write-Host "=== 启动 Dev Gateway 统一入口: http://localhost:8080 ===" -ForegroundColor Cyan
        Invoke-Pnpm -PnpmArgs @("run", "dev:gateway")
    }

    { $_ -in "dev-frontend", "frontend", "dev-all" } {
        Write-Host "=== 启动 shell + core/platform/ecosystem ===" -ForegroundColor Cyan
        Invoke-Pnpm -PnpmArgs (@("-r", "--parallel") + $MainFrontendFilters + @("run", "dev"))
    }

    { $_ -in "dev-frontend-shell", "frontend-shell", "dev-shell" } {
        Write-Host "=== 启动 Shell Dev Server: http://localhost:5173 ===" -ForegroundColor Cyan
        Invoke-Pnpm -PnpmArgs @("--filter", $ShellApp, "run", "dev")
    }

    { $_ -in "dev-frontend-core", "frontend-core", "dev-core-business" } {
        Write-Host "=== 启动 Core Business Dev Server: http://localhost:5175 ===" -ForegroundColor Cyan
        Invoke-Pnpm -PnpmArgs @("--filter", $CoreApp, "run", "dev")
    }

    { $_ -in "dev-frontend-platform", "frontend-platform", "dev-platform-management" } {
        Write-Host "=== 启动 Platform Management Dev Server: http://localhost:5177 ===" -ForegroundColor Cyan
        Invoke-Pnpm -PnpmArgs @("--filter", $PlatformApp, "run", "dev")
    }

    { $_ -in "dev-frontend-ecosystem", "frontend-ecosystem", "dev-ecosystem-management" } {
        Write-Host "=== 启动 Ecosystem Management Dev Server: http://localhost:5176 ===" -ForegroundColor Cyan
        Invoke-Pnpm -PnpmArgs @("--filter", $EcosystemApp, "run", "dev")
    }

    { $_ -in "preview-core", "preview-core-business" } {
        Invoke-Pnpm -PnpmArgs @("--filter", $CoreApp, "run", "build")
        Invoke-Pnpm -PnpmArgs @("--filter", $CoreApp, "run", "preview")
    }

    { $_ -in "preview-platform", "preview-platform-management" } {
        Invoke-Pnpm -PnpmArgs @("--filter", $PlatformApp, "run", "build")
        Invoke-Pnpm -PnpmArgs @("--filter", $PlatformApp, "run", "preview")
    }

    { $_ -in "preview-ecosystem", "preview-ecosystem-management" } {
        Invoke-Pnpm -PnpmArgs @("--filter", $EcosystemApp, "run", "build")
        Invoke-Pnpm -PnpmArgs @("--filter", $EcosystemApp, "run", "preview")
    }

    "preview-all" {
        Invoke-Pnpm -PnpmArgs (@("-r") + $MainFrontendFilters + @("run", "build"))
        Invoke-Pnpm -PnpmArgs (@("-r", "--parallel") + $MainFrontendFilters + @("run", "preview"))
    }

    "restart-frontend" {
        Invoke-Compose -Mode Dev -ComposeArgs (@("restart") + $FrontendServices)
    }

    "scale-knowledge-base" {
        Invoke-Compose -Mode Dev -ComposeArgs @("up", "-d", "--scale", "knowledge-base-service=$N", "knowledge-base-service")
    }

    "pull-lightrag" {
        Invoke-Compose -Mode Gpu -ComposeArgs @("pull", "lightrag")
    }

    "init-db" {
        Set-LocalBackendEnv
        Invoke-Compose -Mode Dev -ComposeArgs @("exec", "postgres", "psql", "-U", $env:DB_USERNAME, "-d", $env:DB_NAME, "-f", "/docker-entrypoint-initdb.d/init.sql")
    }

    # ── 增量 DDL（存量库迭代，不重建数据）──
    # 与 Makefile 的 db-migrate / db-migrate-dry / db-migrate-yes 一一对应，
    # 都转调 deploy/postgres/update/apply.sh。前提：jonex-postgres 容器在跑。
    "db-migrate" {
        Invoke-DbMigrate
    }

    "db-migrate-dry" {
        Invoke-DbMigrate -DryRun
    }

    "db-migrate-yes" {
        # CI / 自动化用：跳过确认。⚠️ 这是「我知道自己在做什么」的开关，
        # 日常手工执行请用 db-migrate（它会先把待应用清单列出来问一次）。
        Invoke-DbMigrate -Yes
    }

    # ── .env 基线校验（与 Makefile 的 env-check / env-check-all 一一对应）──
    # 比对每个 .env 实例与它的 .example 模板：报「缺失/多余/未登记」。
    # 有「缺失/多余」时以退出码 1 结束（故意的，给 pre-commit / CI 留挂载点）。
    "env-check" {
        Invoke-EnvCheck
    }

    "env-check-all" {
        # 默认按前缀聚合（实测真实 .env.local 缺 59 项，逐条刷屏没人看）；
        # 这个开关列出全部键名。
        Invoke-EnvCheck -All
    }

    "keycloak-db-init" {
        Set-LocalBackendEnv
        Invoke-Compose -Mode Dev -ComposeArgs @("exec", "postgres", "createdb", "-U", $env:DB_USERNAME, "keycloak")
        Write-Host "若提示 keycloak 已存在可忽略（幂等）；新卷无需此步（migrations/001_schemas.sql 自动建）" -ForegroundColor Yellow
    }

    "exec-postgres" {
        Set-LocalBackendEnv
        Invoke-Compose -Mode Dev -ComposeArgs @("exec", "postgres", "psql", "-U", $env:DB_USERNAME, "-d", $env:DB_NAME)
    }

    "shell-postgres" {
        Set-LocalBackendEnv
        Invoke-Compose -Mode Dev -ComposeArgs @("exec", "postgres", "psql", "-U", $env:DB_USERNAME, "-d", $env:DB_NAME)
    }

    "test" {
        Invoke-Compose -Mode Dev -ComposeArgs @("ps")
        Write-Host ""
        Write-Host "检查 API Gateway..."
        try { Invoke-WebRequest -UseBasicParsing http://localhost:8000/health | Select-Object -ExpandProperty Content } catch { Write-Host "API Gateway 未就绪或未启动" -ForegroundColor Yellow }
        Write-Host ""
        Write-Host "检查 Sidecar..."
        try { Invoke-WebRequest -UseBasicParsing http://localhost:8001/health | Select-Object -ExpandProperty Content } catch { Write-Host "Sidecar 未就绪或未启动" -ForegroundColor Yellow }
    }

    "clean" {
        Write-Host "警告: 这会删除本项目容器、数据卷和相关镜像。" -ForegroundColor Red
        $confirm = Read-Host "确认继续? (y/N)"
        if ($confirm -ne "y") { exit 1 }
        Invoke-Compose -Mode Dev -ComposeArgs @("down", "-v", "--rmi", "all")
        & docker image prune -f
    }

    { $_ -in "perf", "perf-all" } {
        Write-Host "=== ingest_timing (atomic-rag, worker 分阶段耗时) ===" -ForegroundColor Cyan
        Invoke-PerfLog "atomic-rag" "ingest_timing"
        Write-Host ""
        Write-Host "=== reconcile_timing (knowledge-base-service, 入图库/端到端耗时) ===" -ForegroundColor Cyan
        Invoke-PerfLog "knowledge-base-service" "reconcile_timing"
    }

    "perf-ingest" { Invoke-PerfLog "atomic-rag" "ingest_timing" }
    "perf-reconcile" { Invoke-PerfLog "knowledge-base-service" "reconcile_timing" }
    "perf-chunk" { Invoke-PerfLog "lightrag" "chunk_timing" }
    "perf-thinking" { Invoke-PerfLog "llm-gateway" "thinking.disabled" }
    "perf-extract" { Invoke-PerfLog "llm-gateway" "lightrag_extract" }
    "perf-search" { Invoke-PerfLog "knowledge-base-service" "ontology_search_timing" }

    "perf-audit" {
        Set-LocalBackendEnv
        Invoke-Compose -Mode Dev -ComposeArgs @("exec", "postgres", "psql", "-U", $env:DB_USERNAME, "-d", $env:DB_NAME, "-c", "SELECT created_at, action, outcome, duration_ms, resource_id, request_params FROM platform.audit_logs WHERE action IN ('document.parse_done','document.parse_failed','document.parse_recover') ORDER BY created_at DESC LIMIT 20;")
    }

    default {
        Write-Host "Unknown command: $Command" -ForegroundColor Red
        Write-Host "Use: .\jonex.ps1 help"
        exit 1
    }
}

