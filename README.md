# Colab-Manager (`cm` / `colabm`)

> 单机多账号管理与全量算力监控 CLI 工具，专为 Google 官方 [`google-colab-cli`](https://github.com/googlecolab/google-colab-cli) 打造。
>
> Multi-account manager, concurrent compute-unit dashboard, and transparent proxy CLI for Google Colab CLI (`colab`).

---

## 目录

- [为什么需要 Colab-Manager？](#为什么需要-colab-manager)
- [核心特性](#核心特性)
- [安装](#安装)
- [快速上手](#快速上手)
- [全部命令与参数完整参考](#全部命令与参数完整参考)
  - [1. 账号与凭证管理（`auth` / 快捷命令）](#1-账号与凭证管理auth--快捷命令)
  - [2. 算力余额、使用率与监控（`usage`）](#2-算力余额使用率与监控usage)
  - [3. 跨账号实例查询与关停（`sessions` / `stop`）](#3-跨账号实例查询与关停sessions--stop)
  - [4. 智能选号（`pick`）](#4-智能选号pick)
  - [5. 透传执行原生 `colab` 命令（`@<别名>` / `run`）](#5-透传执行原生-colab-命令别名--run)
  - [6. 终端环境绑定与版本（`env` / `version`）](#6-终端环境绑定与版本env--version)
- [AI Agent / 自动化调用指南](#ai-agent--自动化调用指南)
  - [Agent 推荐标准工作流](#agent-推荐标准工作流)
  - [JSON 输出结构（`--json`）](#json-输出结构--json)
  - [环境变量与退出码约定](#环境变量与退出码约定)
- [数据存储与安全](#数据存储与安全)
- [开发与测试](#开发与测试)

---

## 为什么需要 Colab-Manager？

Google 官方 `google-colab-cli`（`colab`）默认采用单账号设计：
- 默认 `--auth=oauth2` 将唯一的 OAuth Token 写入系统钥匙串固定键位（`google-colab-cli / oauth_token`），无法同时管理多个 Google 账号；
- 会话状态默认写入同一个 `~/.config/colab-cli/sessions.json`；
- 原生 `colab usage` 一次只能查询单个账号，且忽略了后端返回的可用 GPU/TPU 列表（`eligibleGpus` / `eligibleTpus`）。

**Colab-Manager** 完全基于官方原生公开参数（`--auth=adc` + `GOOGLE_APPLICATION_CREDENTIALS` + `--config <独立 sessions.json>`）实现彻底隔离，**无需修改官方 CLI 源码**，即可在一台电脑上管理任意数量的 Google Colab 账号，并支持**多终端 / 多 Agent 真正并发运行、零串号、零竞态冲突**。

---

## 核心特性

- **多账号零冲突真并发**：每个账号拥有独立的 `authorized_user` ADC 凭证（`0600` 权限）与独立的 `sessions.json`，可同时在多个进程中并发操作不同账号。
- **全账号并发算力看板 (`cm usage`)**：一条命令并发拉取所有账号的余额（CU）、每小时消耗速率（CU/时）、预计续航、运行中实例及卡型、可用 GPU/TPU 列表（`H100`/`A100`/`G4`/`L4`/`T4`/`V6E1`/`V5E1`），自动汇总总余额与总消耗速率。
- **跨账号实例总览与一键止损 (`cm sessions` / `cm stop`)**：统一查看所有账号正在跑的 VM（含网页端开启的实例），支持按会话/节点或 `--all` 一键关停防烧卡。
- **智能选号 (`cm pick` / `cm @best`)**：自动按 GPU/TPU 支持情况、空闲状态、最低余额选出最优账号。
- **无缝透明透传 (`cm @<别名> ...` / `cm run <别名> ...`)**：支持全部原生 `colab` 子命令（`new`/`ssh`/`exec`/`upload`/`download`/`repl`/`drivemount`/`url`/`stop`/`install`/`ls`/`rm`/`run` 等），完整保留 TTY。
- **全中文界面 + 结构化 `--json` 输出**：人工看表格清爽直观，AI Agent 和脚本通过 `--json` 零解析成本接入。

### 看板效果预览

```text
                             Colab 多账号算力总览
╭────────────────────┬──────────┬─────────┬────────┬──────────┬──────────────────┬──────╮
│ 账号               │ 余额(CU) │ 消耗速率 │ 预计续航 │ 运行实例 │ 可用 GPU / TPU   │ 状态 │
├────────────────────┼──────────┼─────────┼────────┼──────────┼──────────────────┼──────┤
│ * main [Pro 主力]  │ 2,489.00 │ 2.30/时 │ 约45.1天│ 2 (2xT4) │ H100 A100 G4 L4  │ 正常 │
│   user1@gmail.com  │          │         │        │          │ T4 / TPU: V6E1   │      │
│ acc2 [备用号]      │ 2,347.24 │ 0.00/时 │   空闲 │ 0        │ H100 A100 G4 L4  │ 正常 │
│   user2@gmail.com  │          │         │        │          │ T4 / TPU: V6E1   │      │
├────────────────────┼──────────┼─────────┼────────┼──────────┼──────────────────┼──────┤
│ 合计 (2/2 正常)    │ 4,836.24 │ 2.30/时 │ 约87.6天│ 2        │                  │ 正常 │
╰────────────────────┴──────────┴─────────┴────────┴──────────┴──────────────────┴──────╯
```

---

## 安装

环境要求：**Python >= 3.12**（与官方 `google-colab-cli >= 0.8.0` 一致），推荐使用 [`uv`](https://docs.astral.sh/uv/)。

```bash
# 方式一：直接从 GitHub 安装全局 CLI（推荐）
uv tool install git+https://github.com/shukeCyp/Colab-Manager.git

# 方式二：克隆本地可编辑安装
git clone https://github.com/shukeCyp/Colab-Manager.git
cd Colab-Manager
uv tool install --editable .
```

安装完成后全局注册两个完全等效的命令：`cm` 与 `colabm`（下文示例统一使用 `cm`）。

---

## 快速上手

```bash
# 1. 一键导入当前本机已登录的原生 colab 账号
cm import main --note "主力 Pro+"

# 2. 添加更多 Google 账号（浏览器打开链接选择目标账号登录，粘贴授权码回车）
cm add acc2 --note "备用 Pro"
cm add acc3 --note "备用号 3"

# 3. 并发查看所有账号余额、消耗率、实例和可用 GPU/TPU
cm usage

# 4. 用指定账号开卡并执行命令
cm @acc2 new --gpu t4 -s train
cm @acc2 exec -s train "nvidia-smi"
cm @acc2 stop -s train

# 5. 自动挑余额最高且支持目标 GPU 的账号开卡
cm @best new --gpu a100 --high-mem -s bigjob
```

---

## 全部命令与参数完整参考

> 所有命令均支持 `-h` / `--help` 查看中文帮助。
> 账号别名规则：1–32 位字母、数字、`.`、`_`、`-`（以字母或数字开头）；保留字 `default`、`all`、`best`、`auto` 不可作为自定义别名。

### 1. 账号与凭证管理（`auth` / 快捷命令）

#### 新增账号：`cm add` / `cm auth add`
```bash
cm add <别名> [选项]
cm auth add <别名> [选项]
```
| 参数 / 选项 | 说明 |
| --- | --- |
| `<别名>` | 必填，账号简短别名（如 `acc2`、`pro`、`work`） |
| `-n, --note <文本>` | 可选备注标签（如 `"主力 Pro+"`） |
| `-f, --from-file <路径>` | 非交互式导入已有 ADC / `authorized_user` JSON 凭证文件 |
| `--open` | 自动在默认浏览器中打开 OAuth 授权链接 |
| `-d, --default` | 添加后设为默认账号 |
| `-c, --client-oauth-config <路径>` | 仅 `auth add`：指定自定义 OAuth 客户端配置 JSON |

```bash
cm add acc2 --note "主力号"
cm add acc3 --open --default
cm add bot --from-file /path/to/authorized_user.json
```

#### 导入本机原生登录：`cm import` / `cm auth import`
```bash
cm import [别名] [选项]
cm auth import [别名] [选项]
```
| 参数 / 选项 | 说明 |
| --- | --- |
| `[别名]` | 可选，省略则按邮箱前缀自动生成 |
| `-n, --note <文本>` | 可选备注标签 |
| `--copy-sessions / --no-copy-sessions` | 是否同步复制 `~/.config/colab-cli/sessions.json`（默认开启） |
| `-d, --default` | 设为默认账号 |

```bash
cm import main --note "native colab"
cm import backup --no-copy-sessions
```

#### 列出已管理账号：`cm ls` / `cm auth ls`
```bash
cm ls
cm auth ls
```

#### 并发检查令牌健康度：`cm auth check`
```bash
cm auth check [别名...] [--json]
```
| 参数 / 选项 | 说明 |
| --- | --- |
| `[别名...]` | 可选，指定一个或多个账号别名；省略则并发检查全部账号 |
| `--json` | 输出 JSON 数组（包含 `ok`、`live_email`、`expires_in_seconds`、`has_refresh_token`、`missing_scopes`、`scopes`） |

> 若任意账号检查失败，命令返回退出码 `1`。

#### 重新授权刷新凭证：`cm auth login`
```bash
cm auth login <别名> [--open] [-c <client_oauth_config>]
```
原地刷新指定账号的 OAuth 凭证，保留原备注与本地 `sessions.json`。

#### 修改备注 / 重命名 / 删除 / 切换默认账号
```bash
# 设置或清空备注（传空字符串 "" 清空）
cm auth note <别名> "<备注内容>"

# 重命名账号别名（自动同步目录与默认账号指针）
cm auth rename <旧别名> <新别名>

# 删除账号及本地凭证（加 -y/--yes 跳过确认，适合非交互脚本）
cm auth rm <别名> [-y|--yes]

# 切换默认账号（加 --sync-keyring 可同时写回原生 colab 系统钥匙串）
cm use <别名> [--sync-keyring]
```

---

### 2. 算力余额、使用率与监控（`usage`）

```bash
cm usage [别名...] [选项]
```
| 参数 / 选项 | 说明 |
| --- | --- |
| `[别名...]` | 可选，指定一个或多个账号；省略则并发查询全部账号 |
| `-s, --sort <字段>` | 排序字段：`balance`（余额，默认）/ `rate`（消耗速率）/ `active`（实例数）/ `name`（别名） |
| `--asc` | 改为升序排列（默认降序） |
| `--only-active` | 仅显示当前有运行中实例的账号 |
| `--min-balance <数值>` | 仅显示余额 `>=` 指定 CU 值的健康账号 |
| `--gpu <型号>` | 筛选支持指定 GPU 的账号（如 `H100`、`A100`、`G4`、`L4`、`T4`，不区分大小写） |
| `--tpu <型号>` | 筛选支持指定 TPU 的账号（如 `V6E1`、`V5E1`，不区分大小写） |
| `--no-eligible` | 隐藏“可用 GPU / TPU”列，适配极窄终端 |
| `-w, --watch <秒>` | 每隔 N 秒（最小 3 秒）自动刷新实时监控面板（`Ctrl+C` 退出） |
| `--json` | 输出含 `summary` 汇总与 `accounts` 明细的 JSON |

```bash
cm usage                                    # 查询全部账号
cm usage main acc2                          # 仅查询指定账号
cm usage --watch 30                         # 每 30 秒刷新实时看板
cm usage --gpu A100 --min-balance 100       # 筛选支持 A100 且余额 >= 100 的号
cm usage --only-active --sort rate          # 仅看在跑实例的号，按烧卡速率降序
cm usage --json                             # 输出 JSON 供 Agent 解析
```

---

### 3. 跨账号实例查询与关停（`sessions` / `stop`）

#### 查看所有活跃远端实例：`cm sessions`
```bash
cm sessions [别名...] [--json]
```
并发查询所有（或指定）账号的远端真实运行实例（`/tun/m/assignments`），自动匹配各账号本地 `sessions.json` 中的会话名；网页端直接开启的实例会标注为 `(网页端/未托管)`。

#### 关停 / 释放远端实例：`cm stop`
```bash
cm stop <别名|all> [会话名或节点ID] [--all] [-y|--yes]
```
| 参数 / 选项 | 说明 |
| --- | --- |
| `<别名\|all>` | 账号别名；传 `all` 时必须配合 `--all` |
| `[会话名或节点ID]` | 停止单个实例时传入本地 session 名或远端 `endpoint` |
| `--all` | 关停该账号（或全部账号）下的全部远端活跃实例（含网页端实例） |
| `-y, --yes` | 跳过确认提示（Agent / 脚本非交互必加） |

```bash
cm stop main train              # 停止 main 下名为 train 的会话
cm stop main gpu-t4-s-xxx       # 按远端 endpoint ID 精准关停
cm stop acc2 --all --yes        # 非交互关停 acc2 下所有运行中实例
cm stop all --all --yes         # 紧急止损：非交互关停全部账号下所有实例
```

---

### 4. 智能选号（`pick`）

```bash
cm pick [选项]
```
| 选项 | 说明 |
| --- | --- |
| `--gpu <型号>` | 要求账号支持该 GPU（如 `H100`、`A100`、`L4`、`T4`） |
| `--tpu <型号>` | 要求账号支持该 TPU（如 `V6E1`、`V5E1`） |
| `--idle` | 要求当前无活跃运行实例（`assignments_count == 0`） |
| `--min-balance <数值>` | 要求最低余额（CU） |
| `--json` | 输出被选中账号的完整 JSON 对象（省略时 stdout 仅输出纯别名字符串，方便 shell 变量捕获） |

```bash
# 纯文本输出最优账号别名（可直接嵌套进命令）
cm pick --gpu A100 --idle --min-balance 50

# 输出完整 JSON 供 Agent 读取余额和邮箱
cm pick --gpu T4 --idle --json
```

> 若无符合条件的账号，退出码为 `1`。

---

### 5. 透传执行原生 `colab` 命令（`@<别名>` / `run`）

以下两种写法完全等效，都会自动为子进程注入该账号独立的 `GOOGLE_APPLICATION_CREDENTIALS`、`--auth=adc` 和 `--config <sessions.json>`，并完整保留 TTY 与子进程退出码：

```bash
# 方式 A：极简 @ 语法糖（支持 @<别名>、@default、@best）
cm @<别名> <原生 colab 子命令及参数...>

# 方式 B：显式 run 子命令（支持 <别名>、default、best）
cm run <别名> <原生 colab 子命令及参数...>
```

> **`@best` / `run best` 自动选号规则**：会自动检测透传参数中是否包含 `--gpu <型号>` 或 `--tpu <型号>`，据此筛选支持该加速器且余额最高的健康账号执行，并在 stderr 打印选中日志（不污染 stdout）。

常用原生 `colab` 透传命令清单：
```bash
# 创建 GPU / TPU / CPU 运行时（支持 --gpu t4|l4|g4|a100|h100、--tpu v5e1|v6e1、--high-mem）
cm @acc2 new --gpu t4 -s train
cm @acc2 new --gpu a100 --high-mem -s train
cm @best new --gpu l4 -s auto-job

# 查看该账号会话列表 / 单个会话状态
cm @acc2 sessions
cm @acc2 status -s train

# 远程执行命令或 Python 代码
cm @acc2 exec -s train "nvidia-smi"
cm @acc2 exec -s train "python3 -c 'import torch; print(torch.cuda.is_available())'"

# 一次性脚本执行（自动开临时 VM 运行并释放）
cm @acc2 run ./script.py --gpu t4

# 上传 / 下载 / 列出 / 删除远端文件
cm @acc2 upload ./local_file.tar.gz /content/
cm @acc2 download /content/result.zip ./
cm @acc2 ls -s train /content
cm @acc2 rm -s train /content/tmp.log

# 安装依赖包 / 挂载 Google Drive / 重启内核
cm @acc2 install -s train torch transformers
cm @acc2 drivemount -s train /content/drive
cm @acc2 restart-kernel -s train

# 交互式 SSH / REPL / 获取浏览器直连 URL
cm @acc2 ssh -s train
cm @acc2 repl -s train
cm @acc2 url -s train

# 停止该账号下的会话 / 查看当前账号原生鉴权信息
cm @acc2 stop -s train
cm @acc2 whoami
cm @acc2 usage
```

---

### 6. 终端环境绑定与版本（`env` / `version`）

```bash
# 将当前终端的 `colab` 命令临时固定到指定账号（支持 --shell posix 或 fish）
eval "$(cm env acc2)"
eval "$(cm env acc2 --shell fish)"

# 查看版本号与配置目录路径
cm version
```

---

## AI Agent / 自动化调用指南

本工具在设计上原生适配 AI Agent（如 Antigravity、Claude Code、Codex 等）非交互调用。**推荐 Agent 遵守以下最佳实践：**

1. **优先使用 `--json` 与非交互标志**：查询用 `cm usage --json`、`cm sessions --json`、`cm auth check --json`、`cm pick --json`；破坏性操作必须显式带 `-y` / `--yes` 避免阻塞在交互确认提示上。
2. **绝不直接修改原生钥匙串**：不要在并发 Agent 任务中使用 `cm use <alias> --sync-keyring` 切换全局钥匙串；始终使用 `cm @<alias> ...` 或 `cm run <alias> ...`，每个进程天然隔离，支持多个 Agent 并行操作不同账号。
3. **固定会话名 `-s <session_name>`**：同一账号下并发跑多个任务时，始终显式传入 `-s <唯一会话名>`（例如 `-s job-01`），并在任务结束后调用 `cm @<alias> stop -s <会话名>` 释放 GPU 防止持续扣费。
4. **先选号再开卡，不要重复对同一任务多次 `cm @best`**：先用 `ACCT=$(cm pick --gpu A100 --idle)` 锁定一个账号别名，后续该任务的 `new` / `upload` / `exec` / `download` / `stop` 全部使用同一个 `cm @"$ACCT"`，保证落到同一台 VM 上。

### Agent 推荐标准工作流

```bash
# Step 1: 检查所有账号状态与可用余额（JSON 解析）
cm usage --json

# Step 2: 挑选一个空闲、支持 T4 且余额 >= 20 的账号并锁定别名
ACCT=$(cm pick --gpu T4 --idle --min-balance 20)

# Step 3: 在该账号下开启带固定名称的会话
cm @"$ACCT" new --gpu t4 -s agent-task-1

# Step 4: 上传脚本/数据、执行任务、拉回结果
cm @"$ACCT" upload ./run.py /content/run.py -s agent-task-1
cm @"$ACCT" exec -s agent-task-1 "python3 /content/run.py"
cm @"$ACCT" download /content/output.json ./output.json -s agent-task-1

# Step 5: 释放实例并确认无残留活跃卡
cm @"$ACCT" stop -s agent-task-1
cm sessions --json
```

### JSON 输出结构（`--json`）

#### `cm usage --json`
```json
{
  "summary": {
    "accounts_total": 3,
    "accounts_ok": 3,
    "total_balance": 5833.7,
    "total_rate_hourly": 3.09,
    "total_active_assignments": 2
  },
  "accounts": [
    {
      "alias": "main",
      "email": "user1@example.com",
      "note": "主力 Pro+",
      "is_default": true,
      "ok": true,
      "error": null,
      "balance": 2486.7,
      "rate_hourly": 3.09,
      "assignments_count": 2,
      "eligible_gpus": ["H100", "G4", "A100", "L4", "T4"],
      "eligible_tpus": ["V6E1", "V5E1"],
      "assignments": [
        {
          "alias": "main",
          "email": "user1@example.com",
          "session_name": "train",
          "endpoint": "gpu-l4-s-xxxx",
          "accelerator": "L4",
          "machine_shape": "Standard",
          "variant": "GPU",
          "token_expires_in_seconds": 3600
        }
      ],
      "fetched_at": "2026-11-24T09:38:50Z"
    }
  ]
}
```

#### `cm sessions --json`
返回 `AssignmentDetail` 数组，元素结构与上方 `assignments[]` 一致（若网页端开启，则 `session_name` 为 `null`）。

#### `cm auth check --json`
```json
[
  {
    "alias": "main",
    "recorded_email": "user1@example.com",
    "live_email": "user1@example.com",
    "is_default": true,
    "ok": true,
    "error": null,
    "expires_in_seconds": 3420,
    "has_refresh_token": true,
    "missing_scopes": [],
    "scopes": [
      "https://www.googleapis.com/auth/cloud-platform",
      "https://www.googleapis.com/auth/colaboratory",
      "https://www.googleapis.com/auth/drive.file",
      "https://www.googleapis.com/auth/userinfo.email",
      "https://www.googleapis.com/auth/userinfo.profile",
      "openid"
    ]
  }
]
```

### 环境变量与退出码约定

- **`COLAB_MANAGER_HOME`**：可选，覆盖默认配置目录 `~/.config/colab-manager/`（适合 CI 或多环境隔离）。
- **退出码**：
  - `0`：成功；
  - `1`：参数错误、无匹配账号（`cm pick`）、认证失效（`cm auth check`）或透传命令失败；
  - `cm @<alias>` / `cm run <alias>`：原样返回底层原生 `colab` 子进程的退出码。

---

## 数据存储与安全

- 所有配置文件默认保存在 `~/.config/colab-manager/`：
  ```text
  ~/.config/colab-manager/
  ├── accounts.json                   # 账号注册表（权限 0600）
  └── accounts/
      ├── main/
      │   ├── credentials.json        # 标准 ADC authorized_user 凭证（权限 0600）
      │   └── sessions.json           # 该账号独立的 colab 会话状态（权限 0600）
      └── acc2/
          ├── credentials.json
          └── sessions.json
  ```
- 凭证文件与目录严格设为 `0600` / `0700` 仅当前用户可读；所有 API 请求均直连 Google 官方端点，**不经过任何第三方服务器**。
- `cm add` 默认发送 `prompt=select_account consent`，每次新增账号都会强制弹出 Google 账号选择器，防止浏览器误选默认账号。

---

## 开发与测试

```bash
# 运行全部单元测试（15 项覆盖存储、并发 API、CLI 路由与透传）
uv run --extra dev pytest -v
```

## License

[MIT](./LICENSE)
