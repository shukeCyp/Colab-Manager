# Colab-Manager (`cm` / `colabm`)

> 单机多账号管理与全量算力监控 CLI 工具，专为 Google 官方 [`google-colab-cli`](https://github.com/googlecolab/google-colab-cli) 打造。
>
> Multi-account manager, concurrent compute-unit dashboard, and transparent proxy CLI for Google Colab CLI (`colab`).

---

## 为什么需要 Colab-Manager？

Google 官方 `google-colab-cli`（`colab`）默认采用单账号设计：
- 默认 `--auth=oauth2` 将唯一的 OAuth Token 写入系统钥匙串固定键位（`google-colab-cli / oauth_token`），无法同时管理多个 Google 账号；
- 会话状态默认写入同一个 `~/.config/colab-cli/sessions.json`；
- 原生 `colab usage` 一次只能查询单个账号，且忽略了后端返回的可用 GPU/TPU 列表（`eligibleGpus` / `eligibleTpus`）。

**Colab-Manager** 完全基于官方原生公开参数（`--auth=adc` + `GOOGLE_APPLICATION_CREDENTIALS` + `--config <独立 sessions.json>`）实现彻底隔离，**无需修改官方 CLI 源码**，即可在一台电脑上管理任意数量的 Google Colab 账号，并支持**多终端真正并发运行、零串号、零竞态冲突**。

---

## 核心特性

- **多账号零冲突真并发**：每个账号拥有独立的 `authorized_user` ADC 凭证（`0600` 权限）与独立的 `sessions.json`，可同时在多个终端用不同账号开 GPU、SSH、跑脚本。
- **全账号并发算力看板 (`cm usage`)**：一条命令多线程并发汇总全部账号的：
  - **剩余算力余额（Balance CU）** 与全局汇总 Total
  - **每小时消耗速率（Rate CU/hr）** 与全局汇总速率
  - **预计剩余续航时长（Est.）**（`balance / rate`）
  - **当前运行实例与具体卡型（Active）**（自动识别 `T4`、`L4`、`A100`、`H100`、`High-RAM` 及网页端实例）
  - **账号可用加速器列表（Eligible GPUs / TPUs）**（`H100`、`A100`、`G4`、`L4`、`T4`、`V6E1`、`V5E1`）
  - 支持 `--watch <秒>` 实时监控、`--gpu` / `--min-balance` / `--only-active` 过滤、`--sort` 排序、`--json` 结构化输出。
- **跨账号实例总览与一键止损 (`cm sessions` / `cm stop`)**：统一查看所有账号正在跑的 VM（含浏览器打开的 untracked 实例），支持单账号或全账号 `--all` 一键关停防烧卡。
- **智能选号 (`cm pick` / `cm @best`)**：自动筛选支持目标 GPU/TPU 且余额最高（或空闲）的健康账号。
- **无缝透明透传 (`cm @<alias> ...`)**：完整保留 TTY 交互体验，原生 `colab` 的 `new`、`ssh`、`exec`、`upload`、`download`、`repl`、`drivemount`、`url` 等命令全部原样支持。
- **一键导入现有登录 (`cm import`)**：直接读取本机原生 `colab` 系统钥匙串中的已有登录态并迁入管理，免去重复授权。

### 看板效果预览

```text
                             Colab 多账号算力总览
╭────────────────────┬──────────┬─────────┬────────┬──────────┬──────────────────┬──────╮
│ 账号               │ 余额(CU) │ 消耗速率 │ 预计续航 │ 运行实例 │ 可用 GPU / TPU   │ 状态 │
├────────────────────┼──────────┼─────────┼────────┼──────────┼──────────────────┼──────┤
│ * main [Pro 主力]  │ 2,489.00 │ 2.30/时 │ 约45.1天│ 2 (2xT4) │ H100 A100 G4 L4  │ 正常 │
│   user1@gmail.com  │          │         │        │          │ T4 / TPU: V6E1   │      │
│ acc2 [备用号]      │   100.00 │ 0.00/时 │   空闲 │ 0        │ L4 T4            │ 正常 │
│   user2@gmail.com  │          │         │        │          │                  │      │
├────────────────────┼──────────┼─────────┼────────┼──────────┼──────────────────┼──────┤
│ 合计 (2/2 正常)    │ 2,589.00 │ 2.30/时 │ 约46.9天│ 2        │                  │ 正常 │
╰────────────────────┴──────────┴─────────┴────────┴──────────┴──────────────────┴──────╯
```

---

## 安装

环境要求：**Python >= 3.12**（与官方 `google-colab-cli >= 0.8.0` 保持一致），推荐使用 [`uv`](https://docs.astral.sh/uv/)。

### 方式一：直接通过 `uv tool` 从 GitHub 安装（推荐）

```bash
uv tool install git+https://github.com/shukeCyp/Colab-Manager.git
```

### 方式二：克隆到本地可编辑安装

```bash
git clone https://github.com/shukeCyp/Colab-Manager.git
cd Colab-Manager
uv tool install --editable .
```

安装完成后，全局将注册两个等效命令：`cm`（推荐短命令）和 `colabm`。

---

## 快速上手

### 1. 导入或添加账号

```bash
# 一键导入当前本机已登录的原生 colab 账号（自动同步 sessions.json，不改动原钥匙串）
cm import main --note "主力 Pro+"

# 添加第二个 / 更多 Google 账号
# （终端会打印授权链接，建议在浏览器无痕窗口或不同 Chrome Profile 中打开并粘贴授权码）
cm add acc2 --note "备用 Pro"
cm add acc3 --note "免费号"

# 也可以直接导入已有的 Google ADC / authorized_user JSON 文件
cm add ci --from-file /path/to/adc.json
```

### 2. 查询所有账号余额、消耗率与实例

```bash
# 并发查询所有账号余额、消耗率、预计续航、运行中实例和可用 GPU/TPU
cm usage

# 每 30 秒自动刷新实时监控面板（Ctrl+C 退出）
cm usage --watch 30

# 过滤支持 H100 且余额 >= 50 的账号，按余额排序
cm usage --gpu H100 --min-balance 50 --sort balance

# 仅查看当前有运行中实例的账号
cm usage --only-active

# 输出 JSON（适合脚本/告警集成）
cm usage --json

# 跨账号查看所有活跃实例详情
cm sessions

# 并发检查所有账号 Token 健康度、过期时间与 Scope 完整度
cm auth check
```

### 3. 用指定账号运行任意原生 `colab` 命令

```bash
# 快捷语法：cm @<别名> <原生 colab 命令及参数>
cm @acc2 new --gpu t4 -s train
cm @acc2 ssh -s train
cm @acc2 exec -s train "nvidia-smi"
cm @acc2 upload ./dataset.zip /content/
cm @acc2 download /content/output.pt ./
cm @acc2 url -s train
cm @acc2 stop -s train

# @best 自动根据目标 --gpu/--tpu 挑选余额最高的可用账号执行！
cm @best new --gpu a100 --high-mem -s bigjob

# 也可以配合 cm pick 自由编排脚本
BEST=$(cm pick --gpu A100 --idle)
cm run "$BEST" new --gpu a100 -s worker1
```

### 4. 在当前终端固定账号 / 切换默认账号 / 一键关停实例

```bash
# 将当前终端的 `colab` 命令临时绑定到指定账号（支持 --shell posix / fish）
eval "$(cm env acc2)"
colab usage
colab sessions

# 切换默认账号（加上 --sync-keyring 可同时写回原生 colab 钥匙串）
cm use acc2
cm use acc2 --sync-keyring

# 一键关停指定账号或全部账号的所有运行中远端实例（包含浏览器端开启的实例）
cm stop acc2 --all
cm stop all --all
```

---

## 命令速查表

| 命令 | 说明 |
| --- | --- |
| `cm import [alias]` | 导入本机原生 `colab` 当前登录账号 |
| `cm add <alias>` | 通过浏览器 OAuth 授权或 `--from-file` 新增账号 |
| `cm ls` / `cm auth ls` | 列出所有已管理账号、备注、本地会话数与凭证状态 |
| `cm auth check [alias...]` | 并发检测 Token 有效性、刷新能力、TTL 与 Scopes |
| `cm auth login <alias>` | 原地重新授权刷新某个账号凭证（保留备注与会话） |
| `cm auth note <alias> <note>` | 更新账号备注标签 |
| `cm auth rename <old> <new>` | 重命名账号别名 |
| `cm auth rm <alias>` | 删除账号及本地凭证目录 |
| `cm use <alias> [--sync-keyring]` | 切换默认账号（可选同步回原生钥匙串） |
| `cm usage [alias...]` | 并发查看全账号余额、消耗率、续航、活跃实例与可用 GPU/TPU |
| `cm sessions [alias...]` | 跨账号列出全部活跃远端实例 |
| `cm pick [--gpu ...] [--idle]` | 自动挑选最优账号别名 |
| `cm @<alias> <colab args...>` | 透明透传执行原生 `colab` 命令（支持 `@default`、`@best`） |
| `cm run <alias> <colab args...>` | 同上，显式子命令形式 |
| `cm stop <alias\|all> [target] [--all]` | 释放指定实例或一键清空远端运行中实例 |
| `cm env [alias]` | 输出 shell `export` 与 `alias` 片段供 `eval` 使用 |

---

## 数据存储与安全

- 所有配置文件默认保存在 `~/.config/colab-manager/`（可通过环境变量 `COLAB_MANAGER_HOME` 自定义）：
  ```text
  ~/.config/colab-manager/
  ├── accounts.json                   # 账号注册表（0600）
  └── accounts/
      ├── main/
      │   ├── credentials.json        # 标准 ADC authorized_user 凭证（0600）
      │   └── sessions.json           # 该账号独立的 colab 会话状态（0600）
      └── acc2/
          ├── credentials.json
          └── sessions.json
  ```
- 凭证文件与目录严格设置 `0600` / `0700` 仅当前用户可读权限；所有 API 请求均直连 Google 官方端点，**不经过任何第三方服务器**。

---

## 开发与测试

```bash
# 运行全部单元测试
uv run --extra dev pytest -v
```

## License

[MIT](./LICENSE)
