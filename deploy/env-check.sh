#!/usr/bin/env bash
# ============================================================
# deploy/env-check.sh — .env 实例与模板基线的差异校验
# ============================================================
# 治的问题：各人的 .env.local / deploy/.env 与模板基线长期漂移 ——
# 残留几个月前的调试开关、漏掉新增的配置项 —— 且没有任何机制发现。
#
# 两个入口都转调本脚本，保证两端输出逐字一致：
#   make env-check            Linux / macOS
#   .\jonex.ps1 env-check     Windows（经 Resolve-GitBash 定位的 Git Bash）
#
# 三类检查：
#   缺失   模板激活了、实例没设     → 会用代码默认值跑，可能是问题
#   多余   实例设了、模板从未提及   → 废弃键或本地调试残留
#   未登记 config.py 有、模板未提及 → 新增开关忘了写进基线
#
# ⚠️ 「未登记」噪音大且**不算失败**：实测 188 个 Settings 字段里有 50+ 个
#    带合理默认值、本就不该写进 .env（APP_NAME / DB_ECHO / BCRYPT_ROUNDS …），
#    故只限量提示、不影响退出码 —— 否则挂上 pre-commit 会被噪音逼成 `--no-verify`。
#
# 退出码：0 = 无「缺失/多余」；1 = 有（便于将来挂 pre-commit / CI）
# ============================================================

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 自身绝对路径：--self-test 会以子进程方式重跑本脚本，不能依赖调用方的 CWD
SELF="$SCRIPT_DIR/$(basename "$0")"
# ⚠️ 可被环境变量覆盖（--self-test 用）；正则解析 config.py 时依赖它
ROOT="${JONEX_ENV_CHECK_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

CONFIG_PY="$ROOT/jonex_core/common/config.py"
# 「未登记」清单默认只显示前 N 条，--all 看全量
UNREGISTERED_PREVIEW=10
# 字段数骤降的告警线（见 extract_code_keys 注释）：低于它认为正则失配。
# 实测基数 188，取 100 是「少了一半」的量级；--self-test 会调到 1（样本只有 3 个字段）。
MIN_FIELDS="${JONEX_ENV_CHECK_MIN_FIELDS:-100}"

if [ -t 1 ]; then
    RED='\033[31m'; YELLOW='\033[33m'; GREEN='\033[32m'; DIM='\033[2m'; BOLD='\033[1m'; NC='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; DIM=''; BOLD=''; NC=''
fi

TOTAL_MISSING=0
TOTAL_EXTRA=0

# ------------------------------------------------------------
# 键提取
# ------------------------------------------------------------
# ⚠️ 三处都 `tr -d '\r'`：本仓 example 是 **CRLF** 行尾（Windows 编辑产物），
#    不剥掉的话 `\r` 会混进键名，`comm` 比对会全量误判为「多余」。
#    实例文件的行尾不可控（有人 LF 有人 CRLF），更要无条件剥。

# 激活键：行首的 `KEY=`（不含注释行）
extract_active_keys() {
    grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' "$1" 2>/dev/null \
        | tr -d '\r' | sed 's/=$//' | sort -u
}

# 全部提及的键：激活行 + 注释行（模板里注释掉的项算「提过」，不算多余）
extract_known_keys() {
    grep -oE '^[[:space:]]*#?[[:space:]]*[A-Za-z_][A-Za-z0-9_]*=' "$1" 2>/dev/null \
        | tr -d '\r' | sed -E 's/^[[:space:]]*#?[[:space:]]*//; s/=$//' | sort -u
}

# 代码侧权威字段：config.py 里 `    KEY: type = default` 形式的类字段。
# ⚠️ 不用 Python 枚举 pydantic 字段（`Settings.__fields__`）是**有意的**：
#    本工程 config.py 的字段声明格式高度规整，正则实测抓到 188 个，
#    与 pydantic 的 188 个**逐个一致**。用正则可让本脚本零 Python 依赖，
#    从而被 make / jonex.ps1 直接调用。
# ⚠️ 若将来 config.py 改用动态生成字段（元类 / 循环 setattr），本函数会静默变少 ——
#    故 check_all 末尾有一致性自检（比对注释里记录的历史基数）。
extract_code_keys() {
    grep -oE '^    [A-Z][A-Z0-9_]*:' "$CONFIG_PY" 2>/dev/null \
        | tr -d ' :' | sort -u
}

# ------------------------------------------------------------
# 键清单渲染
# ------------------------------------------------------------
# 默认按前缀聚合成 `PREFIX_*  N 项`，--all 才逐条列。
#
# ⚠️ 为什么必须聚合：实测真实 `.env.local` 缺 59 项，其中 `TASK_CENTER_*` 就占 25 项
#    —— 它们全是**带代码默认值的调优旋钮**，模板里激活只是「登记，让人知道有这开关」，
#    不设完全正常。逐条刷 59 行只会让人一眼略过，工具的告警价值归零。
#    聚合后一眼看出「是那几组参数没设」，要看具体键再 `--all`。
render_keys() {
    local keys="$1" show_all="$2"
    if [ "$show_all" = "1" ]; then
        printf '%s\n' "$keys" | sed 's/^/    /'
        return 0
    fi
    # 组内只有 1 项时不折叠成 `PREFIX_*` —— 那样反而看不出是哪个键
    printf '%s\n' "$keys" | awk -F'_' '
        {
            if (NF > 1) { grp = $1 "_*"; full[grp] = $0 }
            else { grp = $0 }
            cnt[grp]++
        }
        END {
            for (g in cnt) {
                if (cnt[g] == 1 && (g in full)) printf "%5d  %s\n", cnt[g], full[g]
                else printf "%5d  %s\n", cnt[g], g
            }
        }
    ' | sort -rn | sed 's/^/    /'
}

# ------------------------------------------------------------
# 单个文件对的检查
# ------------------------------------------------------------
# 输出三块清单；把「缺失/多余」累加进全局计数（决定退出码）。
check_one() {
    local inst="$1" tmpl="$2" label="$3" show_all="$4"

    if [ ! -f "$tmpl" ]; then
        return 0
    fi

    if [ ! -f "$inst" ]; then
        printf '%s%s%s  %s未创建（可跑 make init）%s\n' "$BOLD" "$label" "$NC" "$DIM" "$NC"
        return 0
    fi

    local tmpl_active inst_active tmpl_known
    tmpl_active="$(extract_active_keys "$tmpl")"
    inst_active="$(extract_active_keys "$inst")"
    tmpl_known="$(extract_known_keys "$tmpl")"

    # 缺失：模板激活了、实例没有
    local missing
    missing="$(comm -23 <(printf '%s\n' "$tmpl_active") <(printf '%s\n' "$inst_active"))"
    # 多余：实例激活了、模板连注释都没提过
    local extra
    extra="$(comm -13 <(printf '%s\n' "$tmpl_known") <(printf '%s\n' "$inst_active"))"

    printf '%s%s%s\n' "$BOLD" "$label" "$NC"

    if [ -z "$missing" ] && [ -z "$extra" ]; then
        printf '  %s✓ 无差异%s\n' "$GREEN" "$NC"
        return 0
    fi

    local n
    if [ -n "$missing" ]; then
        n="$(printf '%s\n' "$missing" | wc -l | tr -d ' ')"
        TOTAL_MISSING=$((TOTAL_MISSING + n))
        printf '  %s缺失 %s 项%s（模板有、你没有 → 会用代码默认值）\n' "$RED" "$n" "$NC"
        render_keys "$missing" "$show_all"
    fi

    if [ -n "$extra" ]; then
        n="$(printf '%s\n' "$extra" | wc -l | tr -d ' ')"
        TOTAL_EXTRA=$((TOTAL_EXTRA + n))
        printf '  %s多余 %s 项%s（你有、模板从未提及 → 废弃键或本地残留）\n' "$YELLOW" "$n" "$NC"
        render_keys "$extra" "$show_all"
    fi
}

# ------------------------------------------------------------
# 未登记检查（代码有、模板没提过）
# ------------------------------------------------------------
check_unregistered() {
    local show_all="$1"
    [ -f "$CONFIG_PY" ] || return 0

    local code_keys
    code_keys="$(extract_code_keys)"
    local count
    count="$(printf '%s\n' "$code_keys" | wc -l | tr -d ' ')"

    printf '%s代码侧字段登记情况%s\n' "$BOLD" "$NC"

    # 自检：字段数骤降说明 config.py 的声明格式变了、正则失配（见 extract_code_keys 注释）
    if [ "$count" -lt "$MIN_FIELDS" ]; then
        printf '  %s⚠ 只解析出 %s 个字段（历史基数 188）——%s\n' "$YELLOW" "$count" "$NC"
        printf '  %s  config.py 的字段声明格式可能已变，请检查本脚本的正则%s\n' "$YELLOW" "$NC"
        return 0
    fi
    printf '  %s已解析 %s 个字段%s\n' "$DIM" "$count" "$NC"

    local tmpl_all=""
    local tmpl
    while IFS= read -r tmpl; do
        [ -n "$tmpl" ] || continue
        tmpl_all="$(printf '%s\n%s\n' "$tmpl_all" "$(extract_known_keys "$tmpl")" | sort -u)"
    done < <(list_templates)

    local unregistered
    unregistered="$(comm -23 <(printf '%s\n' "$code_keys") <(printf '%s\n' "$tmpl_all"))"
    [ -n "$unregistered" ] || return 0

    local n
    n="$(printf '%s\n' "$unregistered" | wc -l | tr -d ' ')"
    printf '  %s未登记 %s 项%s（模板均未提及；多数有代码默认值，仅供参考）\n' "$DIM" "$n" "$NC"
    if [ "$show_all" = "1" ]; then
        printf '%s\n' "$unregistered" | sed 's/^/    /'
    else
        printf '%s\n' "$unregistered" | head -n "$UNREGISTERED_PREVIEW" | sed 's/^/    /'
        printf '    %s…另有 %s 项（--all 看全量）%s\n' "$DIM" "$((n - UNREGISTERED_PREVIEW))" "$NC"
    fi
}

# ------------------------------------------------------------
# 模板发现
# ------------------------------------------------------------
# 自动发现而非硬编码清单：新增 example 时零改动即被纳入。
# ⚠️ 只扫根目录与 deploy/ 下的 `.env*.example` —— 前端 `frontends/*/.env.example`
#    的键是 VITE_*，不属于后端 config.py 的范畴，掺进来全是误报。
list_templates() {
    local f
    for f in "$ROOT"/.env*.example "$ROOT"/deploy/.env*.example; do
        [ -f "$f" ] && printf '%s\n' "$f"
    done
}

check_all() {
    local show_all="$1"

    printf '=== .env 基线检查（根目录: %s）===\n\n' "$ROOT"

    local tmpl
    while IFS= read -r tmpl; do
        [ -n "$tmpl" ] || continue
        local inst="${tmpl%.example}"
        check_one "$inst" "$tmpl" "${inst#"$ROOT"/}" "$show_all"
    done < <(list_templates)

    printf '\n'
    check_unregistered "$show_all"

    printf '\n'
    if [ "$TOTAL_MISSING" -eq 0 ] && [ "$TOTAL_EXTRA" -eq 0 ]; then
        printf '%s✓ 全部实例与基线一致%s\n' "$GREEN" "$NC"
        return 0
    fi

    printf '%s✗ 共 %s 项缺失、%s 项多余%s\n' "$RED" "$TOTAL_MISSING" "$TOTAL_EXTRA" "$NC"
    printf '%s  缺失项会静默回落到代码默认值；多余项通常已无人读取。%s\n' "$DIM" "$NC"
    printf '%s  对齐方式：把缺失项补进实例，多余项确认无人用后删除。%s\n' "$DIM" "$NC"
    return 1
}

# ------------------------------------------------------------
# 自检（--self-test）：造样本文件验证三类判据，不碰真实 .env
# ------------------------------------------------------------
run_self_test() {
    local tmp
    tmp="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" EXIT

    mkdir -p "$tmp/jonex_core/common" "$tmp/deploy"
    cat > "$tmp/jonex_core/common/config.py" <<'EOF'
class SampleSettings:
    ALPHA_ONE: str = "x"
    BETA_TWO: int = 1
    DELTA_FOUR: bool = False
EOF
    cat > "$tmp/deploy/.env.example" <<'EOF'
ALPHA_ONE=placeholder
# BETA_TWO=2
EOF
    # 实例：漏 ALPHA_ONE（→缺失）、多 GAMMA_THREE（→多余）
    printf 'GAMMA_THREE=3\r\n' > "$tmp/deploy/.env"

    local out status
    out="$(JONEX_ENV_CHECK_ROOT="$tmp" JONEX_ENV_CHECK_MIN_FIELDS=1 bash "$SELF" 2>&1)"
    status=$?

    local failures=0
    assert_contains() {
        if printf '%s' "$out" | grep -q "$1"; then
            printf '  %s✓%s %s\n' "$GREEN" "$NC" "$2"
        else
            printf '  %s✗%s %s\n' "$RED" "$NC" "$2"
            printf '%s\n' "$out" | sed 's/^/      | /'
            failures=$((failures + 1))
        fi
    }

    printf '=== env-check 自检 ===\n'
    assert_contains '缺失 1 项' '检出「缺失」（模板有、实例无）'
    assert_contains 'ALPHA_ONE' '缺失项键名正确'
    assert_contains '多余 1 项' '检出「多余」（实例有、模板无）'
    assert_contains 'GAMMA_THREE' '多余项键名正确'
    # 未登记 = {ALPHA_ONE, BETA_TWO, DELTA_FOUR} − 模板提及的 {ALPHA_ONE, BETA_TWO} = {DELTA_FOUR}
    assert_contains '未登记 1 项' '检出「未登记」（代码有、模板无）'
    assert_contains 'DELTA_FOUR' '未登记项键名正确'
    # 注释行 BETA_TWO 算「已提及」，不得被判为多余
    if printf '%s' "$out" | grep -q 'BETA_TWO.*多余'; then
        printf '  %s✗%s 注释行被误判为多余\n' "$RED" "$NC"
        failures=$((failures + 1))
    else
        printf '  %s✓%s 模板注释项未被误判为多余\n' "$GREEN" "$NC"
    fi
    # CRLF 样本必须被正确解析（不剥 \r 时 GAMMA_THREE 会带 \r 而匹配不上）
    if [ "$status" -eq 1 ]; then
        printf '  %s✓%s 有差异时退出码为 1\n' "$GREEN" "$NC"
    else
        printf '  %s✗%s 有差异时退出码应为 1，实为 %s\n' "$RED" "$NC" "$status"
        failures=$((failures + 1))
    fi

    printf '\n'
    if [ "$failures" -eq 0 ]; then
        printf '%s✓ 自检通过%s\n' "$GREEN" "$NC"
        return 0
    fi
    printf '%s✗ 自检失败 %s 项%s\n' "$RED" "$failures" "$NC"
    return 1
}

# ------------------------------------------------------------
case "${1:-}" in
    --self-test) run_self_test ;;
    --all)       check_all 1 ;;
    -h|--help)
        sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    "")          check_all 0 ;;
    *)
        printf '用法: %s [--all|--self-test]\n' "$0" >&2
        exit 2
        ;;
esac
