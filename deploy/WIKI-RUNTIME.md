# Wiki Agent 服务器部署

## 日常更新

以下命令适用于已完成平台基础配置、并已合入本部署改动的 Linux 服务器：

```bash
git pull --ff-only origin dev
make build && make up
make wiki-check
```

`make up` 会自动查找并准备 Wiki 运行配置。首次生成后，递归重新读取 Makefile，确保同一次启动就加载新配置。`make wiki-check` 检查运行中的 Agent 沙箱、来源只读和 Wiki 解析分流；它不是完整业务验收。

`make up-server`、`make up-gpu` 和 `make up-jonex-wiki` 使用同一初始化入口。`make build` 仅构建镜像，不修改主机或数据库。

## 第一次在服务器上使用

先配置平台已有的环境文件、数据库、模型网关、解析与图片服务，确保 Docker 在当前 Linux 宿主机运行。Wiki 专属配置使用 `deploy/.env.jonex_wiki`，不存在时按 `deploy/.env.jonex_wiki.example` 准备。Wiki 内部密钥缺失或为已知占位值时会自动生成；不会自动生成模型或解析服务凭据。

随后执行 `make build && make up`。Wiki 本机配置和内部密钥放在 `deploy/.wiki-runtime/`，不加入 Git。Wiki 解析、编译和阻塞线程池的默认并发为 15，Bundle 默认保留当前版和一个历史版。写作额度在启动前按新版模板与既有有效设置取较大值，默认单次请求输入 2 MiB、每批总时长 900 秒、无响应时长 300 秒和 200 次调用，单轮输出 16,384 token。有效额度记录在 `compile-limits.yml`。

整库 Agent 搜索、阅读和常规写作的累计输入预算默认 600 万 token，由 `deploy/.env.jonex_wiki` 的 `JONEX_WIKI_AGENT_COMPILE_MAX_INPUT_TOKENS` 控制。最终综合的累计输入预算独立设置为 24 万 token，每个待补批次最多请求一次。可在同一文件调整 `JONEX_WIKI_AGENT_COMPILE_FINAL_INPUT_TOKENS`。短时收尾使用封存的证据摘录，失败时采用来源综述。发布结果仍包含完整原文、表格和图片。

如果用户命名空间上限小于 64，`make up` 会提示由管理员先运行 `make wiki-runtime-setup`。管理员入口可以通过 `sudo` 写入 `/etc/sysctl.d/90-jonex-wiki.conf` 并应用设置。该设置对整个宿主机生效。已有内容不同的同名文件交由管理员处理。以部署用户运行命令即可，脚本只在设置主机参数时调用 sudo。

平台 SQL 迁移不由 `make up` 自动执行。已有数据库升级前运行 `make db-migrate-dry`，有待执行迁移时按平台数据库部署说明备份和处理。Wiki 容器自身迁移仍遵循其容器启动入口。全新数据库的准备也不能由 seccomp 初始化代替。

## seccomp 策略的选择顺序

1. 已有 `deploy/.wiki-runtime/compose.yml` 时，优先保留并使用该目录内原有策略。
2. 没有运行配置时，可通过 `WIKI_SECCOMP_PROFILE=/path/to/profile.json` 指定自定义策略；否则尝试查找旧 Wiki 容器的部署配置。
3. 没有旧策略时，Linux x86_64 自动使用项目内的 `deploy/wiki-seccomp-x86_64.json`。其他架构需要提供适用的策略。

策略基于测试服务器使用的配置，限制默认未列出的系统调用，禁止旧式 `socketcall`，`socket` 只放行编号小于 `AF_ALG`（38）的地址族，因此 `AF_ALG`、`AF_VSOCK`（40）均被拒绝。旧版策略写成对同一参数的两个"不等于"条件，runc 会把它拆成两条独立的放行规则，实际什么都没拦住；脚本检查已有策略时按 runc 的这一行为判断。它与 Wiki 沙箱所需的用户命名空间设置、`cap_drop: ALL`（只加回 `SETFCAP`）、`no-new-privileges` 一起使用。已有自定义策略不会被自动替换；如包含旧式网络规则，脚本会提示。不同内核和 Docker 环境仍需要执行 `make wiki-check`，这些规则不是对所有机器的安全认证。

检测到非本机 Docker 连接时，脚本要求在目标 Docker 宿主机执行，以免错误修改客户端机器的命名空间配置。macOS 上保留已有开发环境，不自动调整 Linux 虚拟机设置。

## 其他检查

`python3 deploy/wiki-runtime-ensure.py` 默认执行只读检查，包含数据库和解析服务配置，需要相关服务可访问。显式使用 `--repair` 可修复配置，并在备份后执行已登记的平台 SQL 迁移。先核对检查结果和数据库维护窗口。

检查通过不代表真实模型、图片展示、浏览器操作或所有业务功能已经完成端到端验收。

## Wiki Agent 沙箱的容器条件

Wiki Agent 的沙箱（bwrap）在容器内创建用户、挂载和网络命名空间。以下条件和报错均在
Ubuntu 26.04（内核 7.0）服务器上逐项对照实测：

| 条件 | 缺失时 bwrap 的报错 | 原因 |
|---|---|---|
| `cap_add: ["SETFCAP"]` | `setting up uid map: Operation not permitted` | 容器以 root 运行，bwrap 把 uid 0 映射为 uid 0；Linux 5.12 起映射父命名空间的 uid 0 要求创建者持有 `CAP_SETFCAP`，`cap_drop: ALL` 把它去掉了 |
| 宿主启用 AppArmor 时不用 `docker-default` | `Failed to make / slave: Permission denied` | Docker 的 `docker-default` 策略拒绝 `mount` |
| 内核有 `kernel.apparmor_restrict_unprivileged_userns` 开关时用 `apparmor=jonex-wiki-sandbox`，不用 `unconfined` | `loopback: Failed RTM_NEWADDR: Operation not permitted` | Ubuntu 23.10 起，unconfined 进程新建的用户命名空间会被切到 `unprivileged_userns` 策略，其中所有能力都被拒绝 |

AppArmor 一项按宿主机选择：

- 未启用 AppArmor（`/sys/module/apparmor/parameters/enabled` 不为 `Y`）：不加 AppArmor 选项。
- 启用 AppArmor，内核没有上述开关：`apparmor=unconfined`。
- 启用 AppArmor，内核有上述开关：`apparmor=jonex-wiki-sandbox`。不论开关当前是 0 还是 1 都这样配置，以免之后改为 1 时沙箱失效。
  这份策略在 `deploy/apparmor/jonex-wiki-sandbox`，内容是 `flags=(unconfined)` 外加 `userns` 放行，
  也就是 Ubuntu 给需要用户命名空间的程序的标准写法：容器的其余行为与 `unconfined` 相同，宿主机上其他进程仍受限制。
  `make wiki-runtime-setup` 用 sudo 把它装到 `/etc/apparmor.d/` 并加载，开机时由 AppArmor 自动加载；
  已有同名文件但内容不同时停止，不覆盖。`make up` 只检查，缺少时提示管理员先运行 `make wiki-runtime-setup`。
  撤销：`sudo apparmor_parser -R /etc/apparmor.d/jonex-wiki-sandbox && sudo rm -f /etc/apparmor.d/jonex-wiki-sandbox`。

seccomp 不需要放行 `unshare`：bwrap 通过 `clone` 创建命名空间，策略已放行带 `CLONE_NEWUSER` 的 `clone`。
seccomp 和 `no-new-privileges` 始终生效，除 `SETFCAP` 外不加回其他能力。

改动后旧的本机运行配置需要重建（`wiki-runtime-setup.sh` 不会覆盖已有配置）：

```bash
mv deploy/.wiki-runtime "deploy/.wiki-runtime.bak-$(date +%Y%m%d%H%M)"
make wiki-runtime-setup
make up-server
make wiki-check
```

排查顺序：

1. `docker exec jonex-wiki ls -l /usr/bin/bwrap` 应为字符设备。
2. `docker inspect jonex-wiki` 确认 `SecurityOpt` 带 seccomp 和上面选定的 AppArmor 选项，`CapAdd` 为 `CAP_SETFCAP`。
3. 看 bwrap 的真实报错，按上表对照。`make wiki-check` 不输出 stderr，只能看到 `AGENT_SANDBOX_PREFLIGHT_FAILED`，改用：

   ```bash
   docker exec -i jonex-wiki python -c '
   import subprocess, sys
   _run = subprocess.run
   def run(*a, **k):
       r = _run(*a, **k)
       if getattr(r, "stderr", None): sys.stderr.write(r.stderr)
       return r
   subprocess.run = run
   exec(compile(sys.stdin.read(), "preflight", "exec"))' < deploy/wiki-agent-preflight.py
   ```

4. 报错涉及能力被拒时，`sudo dmesg -T | grep apparmor= | tail` 会显示是哪个 AppArmor 策略拒绝了哪项能力。
