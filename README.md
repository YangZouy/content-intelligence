# Content Intelligence Dispatcher（内容智能分发助手）

**可恢复、可审计、可控制发布副作用的 AI 内容工作流**

一个面向多平台内容发布的可靠 AI 工作流。系统通过显式状态、确定性质量门禁、有限修复和发布隔离，控制模型输出与 GitHub Pages、微信公众号等外部发布副作用。内容处理步骤相对固定，因此采用确定性工作流，而非多 Agent 对话或开放式 ReAct 循环。

> **零低代码平台依赖。零检索增强。纯代码实现。**
>
> 项目经历三轮演进：`Coze Agent` → `Dify Workflow + Node.js CLI` → 当前 `Python + LangGraph` 完全重写，摆脱低代码平台在可调试性、版本管理与链路稳定性上的限制。

## 核心特性

- **多源导入**：支持本地 Markdown 文件、带附件的 Obsidian 仓库（图片自动解析 `assets/` 同级目录）、远程图片 URL
- **基于规则的格式化**：长段落拆分、列表转换、术语加粗 —— 默认 **零 LLM Token 消耗**；可选 `llm` 模式叠加一次语义润色（带安全回退）
- **AI 元数据生成**：默认仅调用一次大模型（DeepSeek，可切智谱 / OpenAI）提取标题 / 摘要 / 标签（约 ¥0.01 / 篇）
- **OSS 图床托管**：自动上传至阿里云 OSS，并以拼音生成全 ASCII 英文路径（避免 CDN 编码问题）
- **装饰性封面**：Unsplash 主源 + Picsum 兜底，自动落 OSS，同文同封面（重跑不漂移），失败不阻断流水线
- **双平台发布**：GitHub Pages（Hexo）与微信公众号（通过 wenyan 服务 HTTP 接口）各自独立发布
- **发布前质量门禁**：确定性检查元数据、Front Matter、Markdown 结构、代码、公式和 OSS 图片替换
- **有限质量修复**：首次不通过时携带问题重新生成一次；二次不通过立即终止，不进入发布
- **人工发布审批**：质量通过后暂停，展示标题、摘要、标签、封面和平台；支持确认、拒绝、修改后重检
- **跨进程恢复**：审批状态持久化到本地 SQLite，可通过运行 ID 在新进程中继续
- **文章级幂等发布**：使用 `article_id + content_version` 和 SQLite 发布账本，跨全新运行复用已成功结果
- **故障隔离**：博客与微信各自独立重试，单平台失败绝不阻塞另一平台
- **全链路可观测**：`trace_id` 贯穿每次运行，节点耗时、Token 消耗、发布结果持久化为 `runs/<timestamp>.json`
- **结构化重试轨迹**：OSS 与平台发布重试统一记录操作、次数、异常、退避时间和时间戳
- **离线评测基线**：20 篇固定 Markdown 样本覆盖正常内容、图片、代码公式表格和异常输入

## 系统架构

```
CLI (Typer + Rich)
  │
  ▼
ingest → format_optimize → summary_meta → image_process
                              ▲                 │
                              │                 ▼
                       quality_repair      cover_image
                         （最多一次）            │
                              ▲                 ▼
                              │           content_adapt
                              │                 │
                              └── 未通过 ─ quality_check
                                                │ 通过
                                                ▼
                                             approval
                                          /     |      \
                                      拒绝    修改     确认
                                       END   重新检查    │
                                                        ▼
                                                     publish
                                            /       \
                                       GitHub       微信
  │                    │              │
  ▼                    ▼              ▼
 runs/*.json        Hexo 仓库     wenyan server
                                (HTTP 固定 IP:8080)
```

状态在节点间以 `TypedDict`（`AgentState`）传递，每个节点只写自己阶段的字段；元数据节点产出 `Pydantic` 结构化对象，作为节点间的数据契约。

## 快速开始

### 1. 安装依赖

```bash
cd content_intelligence_dispatcher
pip install -r requirements.txt
# 可选：以可编辑模式安装，获得 content-dispatcher 命令行入口
pip install -e .
```

### 2. 配置环境变量

```bash
# 复制模板文件
cp .env.example .env

# 编辑文件，填入你的实际配置信息
# 至少需要：DEEPSEEK_API_KEY（或 ZHIPU_API_KEY / OPENAI_API_KEY）用于生成摘要
```

### 3. 准备 wenyan 服务（微信公众号发布依赖）

微信发布通过 `wenyan` 工具的 server 模式实现。本地调试或云端部署任选其一：

```bash
# 安装 wenyan CLI（含 server 功能）
npm install -g @wenyan-md/cli

# 配置微信公众号凭据（仅首次，写入 wenyan 配置）
wenyan config set WECHAT_APP_ID your-app-id
wenyan config set WECHAT_APP_SECRET your-app-secret

# 本地启动 server（建议用 pm2 常驻）
wenyan serve --port 3000 --api-key your-secret-key
```

确认 server 正常：

```bash
curl http://localhost:3000/health
```

> 公众号 API 对调用 IP 有白名单限制，且个人电脑公网 IP 随时变动。生产环境请将 wenyan server 部署到固定 IP 的云服务器（详见下文「微信发布架构 → 固定 IP 方案」），并将 `.env` 中的 `WECHAT_SERVER_URL` 指向 `http://<服务器IP>:8080`。

### 4. 运行

```bash
# 显式子命令（推荐，永远可用）
python -m src.cli publish ./my-article.md

# 指定平台
python -m src.cli publish ./my-article.md --platforms wechat
python -m src.cli publish ./my-article.md -p blog,wechat

# 临时切换排版模式（覆盖 config.yaml 的 format_optimize.mode）
python -m src.cli publish ./my-article.md -fo llm

# 安装后可执行入口
content-dispatcher publish ./my-article.md
```

## 配置说明

### 环境变量 (.env)

| 变量名 | 是否必需 | 描述 |
|----------|----------|-------------|
| `DEEPSEEK_API_KEY` | 默认 provider 必需 | DeepSeek API 密钥（provider=deepseek 时） |
| `ZHIPU_API_KEY` | 视 provider 而定 | 智谱 GLM API 密钥（provider=zhipu 时） |
| `OPENAI_API_KEY` | 视 provider 而定 | OpenAI API 密钥（provider=openai 时） |
| `ALIYUN_ACCESS_KEY_ID` | 处理图片时需要 | 阿里云 OSS 访问密钥 ID |
| `ALIYUN_ACCESS_KEY_SECRET` | 处理图片时需要 | 阿里云 OSS 访问密钥 Secret |
| `ALIYUN_OSS_ENDPOINT` | 处理图片时需要 | OSS 终端节点 URL |
| `ALIYUN_BUCKET_NAME` | 处理图片时需要 | Bucket 名称 |
| `GITHUB_TOKEN` | 发布博客时需要 | GitHub 个人访问令牌 |
| `GITHUB_USERNAME` | 发布博客时需要 | GitHub 用户名 |
| `GITHUB_HEXO_REPO` | 发布博客时需要 | Hexo 仓库（格式：所有者/仓库名） |
| `WECHAT_SERVER_URL` | 发布微信时需要 | wenyan server 地址（本地默认 `http://localhost:3000`，远程部署填 `http://<IP>:8080`） |
| `WECHAT_SERVER_API_KEY` | 发布微信时需要 | 启动 wenyan server 时设置的 API Key |
| `UNSPLASH_ACCESS_KEY` | 封面获取（可选） | Unsplash API Key；留空则封面直接走 Picsum 兜底 |

> `WECHAT_APP_ID` / `WECHAT_APP_SECRET` 仅由 wenyan **server 端**使用（本客户端通过 `server_url + api_key` 调用，无需在客户端 `.env` 重复配置；`.env.example` 中保留仅作提示）。

### config.yaml

编辑 `config/config.yaml` 可自定义：

- **oss**：终端节点、Bucket、允许扩展名、每篇最大图片数（`max_images_per_article`，默认 10）
- **github**：仓库、本地克隆路径、提交前缀
- **wechat**：server 地址、api_key、主题 `theme_id`、超时、重试次数
- **brand**：名称、受众、语气、默认分类 / 标签前缀、作者、原文地址
- **model**：`provider`（openai / deepseek / zhipu）、`summary_model`、`temperature`、`max_tokens`，均支持 `${ENV_VAR}` 占位
- **format_optimize**：`mode`（`rule` 默认 / `llm`）、`llm_max_tokens`、`safety_check`
- **cover**：是否启用、是否强制覆盖首图、尺寸、Unsplash key、`upload_to_oss`
- **default_options**：默认发布平台、是否自动发布、是否处理图片 / 排版优化

## 项目结构

```
content_intelligence_dispatcher/
├── config/
│   └── config.yaml          # 主配置文件（支持 ${ENV_VAR} 占位）
├── src/
│   ├── cli.py               # 基于 Typer + Rich 的命令行入口
│   ├── graph.py             # LangGraph StateGraph 编排 + 运行入口（可观测性）
│   ├── state.py             # TypedDict 代理状态（6 阶段字段划分）
│   ├── schema.py            # Pydantic 模型（LLM 输出、Front-Matter）
│   ├── config_loader.py     # 优先级链 YAML + .env 加载器
│   ├── llm.py               # provider 感知的 ChatOpenAI 工厂（OpenAI 兼容协议）
│   ├── oss_client.py        # 阿里云 OSS 上传客户端
│   ├── errors.py            # 异常层级 + 指数退避重试装饰器
│   ├── observability.py     # loguru 追踪 + 运行日志持久化（trace_id）
│   ├── publication_ledger.py # 文章身份、稳定资产和平台发布账本
│   ├── nodes/
│   │   ├── ingest.py        # 多源内容导入
│   │   ├── article_identity.py # article_id、首次发布日期和持久封面
│   │   ├── format_optimize.py  # 规则引擎（+可选 LLM 润色 / 安全护栏）
│   │   ├── summary_meta.py  # LLM 摘要 / 元数据提取
│   │   ├── image_process.py # OSS 上传 + 链接替换 + 首图选取
│   │   ├── cover.py         # 装饰性封面（Unsplash/Picsum，不走 LLM）
│   │   ├── content_adapt.py # 平台内容适配（Hexo / 微信 共用 front-matter）
│   │   ├── quality_check.py # 确定性发布前质量门禁
│   │   ├── quality_repair.py # 有限修复反馈（最多一次）
│   │   ├── approval.py      # 人工审批暂停、决策和修改路由
│   │   └── publish.py       # 平台发布编排器
│   └── publishers/
│       ├── base.py          # 发布器接口协议
│       ├── github_pages.py  # GitHub Pages 发布器（GitPython）
│       └── wechat.py        # 微信公众号发布器（wenyan server CLI）
├── tests/                   # pytest 自动化测试
├── eval/                    # 固定样本集、预期结果与离线评测报告
├── runs/                    # 运行日志 JSON（每次运行自动创建）
└── logs/                    # 应用日志（自动创建）
```

## 流水线节点详解

### 1. IngestNode（`ingest` 导入节点）
- 读取本地 Markdown 文件（`_detect_source_type` 当前固定返回 `obsidian`，即本地笔记 / 外链 Markdown 两类输入）
- 提取所有图片引用（Markdown `![](...)` 与 HTML `<img src>` 两种语法）
- 解析 Obsidian 附件：按 `assets/` 同级目录匹配真实文件，无法解析的裸文件名直接跳过（避免把无效路径传给 OSS）
- 远程图片 URL 原样保留，交由下游重新下载并上传

### 2. FormatOptimizeNode（`format_optimize` 格式化优化节点）
- **默认 `rule` 模式 —— 纯规则引擎，零 LLM Token 消耗、零内容损坏风险**
- 确定性规则：长段落拆分（>200 字句子边界）、并列项 → 无序列表、顺序步骤 → 有序列表、术语加粗、警告/提示 → 引用块（`> ⚠️` / `> 💡`）、代码块语言标注、长文自动添加 `---` 分节
- **可选 `llm` 模式**：规则底座之上叠加一次 LLM 语义润色（补过渡句 / 智能加粗 / 缺标题生成等），复用同一 LLM 工厂
- **安全护栏**（`_llm_output_safe`）：润色后比对图片 / 代码块 / 公式数量，任一被改坏即回退规则结果；LLM 调用异常同样回退
- **硬性约束**：绝不修改知识性内容（论点、数据、结论、代码块、图像、公式、表格）

### 3. SummaryMetaNode（`summary_meta` 摘要元数据节点）
- **默认（`rule` 模式）下全系统唯一的 LLM 调用点**
- 使用可配置模型（默认 DeepSeek `deepseek-chat`，可切智谱 `glm-4-flash` 或 OpenAI `gpt-4o-mini`），以 `function_calling` 方式输出 `Pydantic` 结构化结果（规避国产模型不支持 `json_schema` 响应格式）
- 提取：标题（保留原文 H1）、摘要（≤200 字）、标签（3–6 个）、字数、阅读时长
- Token 预算：约 ¥0.01 / 篇

### 4. ImageProcessNode（`image_process` 图片处理节点）
- 将所有图片上传至阿里云 OSS
- 通过 `pypinyin` 生成全 ASCII 英文路径（如 `images/ShenDuXueXiRu/`），避免 CDN 编码问题
- 替换正文所有图片引用为 OSS 直链
- 选取首图作为封面候选；每篇最多处理 10 张（超出截断并告警）

### 5. CoverImageNode（`cover_image` 封面节点）— 不走 LLM
- 装饰性博客封面（展示层素材，不占用 LLM 调用）
- 主源 Unsplash（按标签取主题图），失败 / 被墙时回退 Picsum（零 key、国内通常可达，解析最终 CDN 直链）
- 默认把封面上传 OSS，落在与正文图片**同一文件夹**（文件名固定 `cover.jpg`）
- **同文同封面（幂等）**：封面按 `article_id` 保存到 SQLite；以后即使是新的 `run_id` 也直接复用
- 任何异常返回 `{}`，**绝不阻断流水线**

### 6. ContentAdaptNode（`content_adapt` 内容适配节点）
- Hexo 与微信**共用同一份 YAML Front-Matter**（title / cover / author / source_url 等）
- **Hexo 文档**：Front-Matter + 正文，可直写 `source/_posts/<title>.md`
- **微信草稿**：同样带 Front-Matter（wenyan 要求每篇顶部至少含 `title`），正文使用 OSS 图片链接；wenyan 读取 title/cover/author/source_url，忽略 Hexo 专属字段

### 7. QualityCheckNode（`quality_check` 质量门禁）
- 检查元数据长度、Front Matter 完整性和平台格式
- 对照原文验证代码块、公式和图片未被破坏
- 验证图片均已替换为 OSS 映射地址
- 首次失败进入有限修复；修复后仍失败则终止，不触发发布

### 8. QualityRepairNode（`quality_repair` 有限修复）
- 将具体问题代码和原因反馈给元数据生成阶段
- 重新执行元数据、图片、封面、平台适配和质量检查
- `quality_repair_count` 硬限制为 1，不存在开放循环

### 9. PublishNode（`publish` 发布节点）
- 博客和微信拆为两个独立 checkpoint 步骤，按目标平台依次执行
- 各自独立重试逻辑（指数退避；博客最多 3 次，微信最多 2 次）
- **单平台失败绝不阻塞另一平台**；结果汇总为 `PublishResultItem[]`
- `article_id` 默认由规范化源文件绝对路径生成；文件可能移动时可通过 `--slug` 提供稳定身份
- `content_version` 只计算标题、摘要、标签、正文和正文图片 URL
- 日期、封面、运行 ID 和临时路径不参与 `content_version`，不会制造虚假新版本
- SQLite 发布账本按 `article_id + platform + content_version` 保存外部 ID、成功状态和发布时间
- checkpoint 或发布账本已有相同成功版本时，该平台显示 `REUSED`，不会重复调用发布器
- 博客目标文件内容未变化时不创建空 commit、不执行 Git push
- 微信成功后的 `media_id` 保存在 checkpoint；恢复时直接复用

日期和封面规则：

- Front Matter 日期只写 `YYYY-MM-DD`
- 日期在文章第一次进入工作流生成 Front Matter 时确定，并立即保存到文章资料表
- 后续审批、跨天恢复、内容更新和重新发布始终复用该日期，不会静默改变已经预览的文档
- 封面首次生成后立即按 `article_id` 保存，后续运行不再随机获取

发布部分失败后可按运行 ID 重试：

```bash
content-dispatcher retry-publish <run-id>
# 或
python -m src.cli retry-publish <run-id>
```

恢复会重新经过博客和微信发布步骤，但成功平台会被幂等检查跳过，只有失败平台发生外部调用。
即使启动了全新的工作流、产生了新的 `run_id`，发布账本仍能识别同一文章的同一内容版本。
微信服务目前不接受客户端幂等键，因此如果服务端已创建草稿、但连接在返回 `Media ID` 前中断，客户端无法百分之百判断该次请求是否成功；正常收到 `Media ID` 后的恢复不会重复创建。

### 发布前人工审批

质量门禁通过后，工作流不会直接发布，而是展示：

- 标题、摘要、标签和封面
- 本次目标平台
- 可跨进程恢复的运行 ID

审批动作：

- **确认发布**：继续执行 Git push / 微信草稿创建
- **拒绝终止**：正常结束工作流，不执行任何发布操作
- **修改后重检**：修改标题、摘要、标签、封面或平台，重新适配并再次经过质量门禁；通过后再次审批

审批 checkpoint 保存于 `checkpoints/workflow.sqlite`。关闭终端后可恢复：

```bash
content-dispatcher resume <run-id>
# 或
python -m src.cli resume <run-id>
```

## 微信发布架构

本项目的 Python 端并不直接 HTTP 调用微信，而是调用 `wenyan` 工具：

```
Python WeChatPublisher
  │  subprocess: wenyan publish --server <url> --api-key <key> --theme <id>
  ▼
wenyan CLI（HTTP 客户端）
  │  HTTP POST（上传 Markdown + 触发渲染/发布）
  ▼
wenyan server（固定 IP:8080，云端部署）
  │  微信公众号 API
  ▼
微信草稿箱
```

**两个工程化决策**（均为踩坑后的真实修复）：

1. **成功判定以 `Media ID:` 为准，而非退出码**：Windows 下 `wenyan.cmd` 包装脚本会把退出码污染成 1，但 `Media ID` 只有服务端真正创建草稿后才会输出，是更可靠的成功信号。
2. **剥离代理环境变量**：subprocess 中移除 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`，避免本地代理转发「裸 IP:端口」请求不稳定导致的 `fetch failed`；服务器 IP 国内直连可达，无需代理。

### 固定 IP 方案（服务器部署 wenyan server）

将 wenyan server 部署到固定 IP 的云服务器，规避公众号 API 的 IP 白名单限制，并使本机 CLI 与 server 解耦。

```ini
# /etc/systemd/system/wenyan.service（已配置开机自启）
[Unit]
Description=Wenyan Server
After=network.target

[Service]
Type=simple
User=wenyan
WorkingDirectory=/var/lib/wenyan
EnvironmentFile=/etc/wenyan/env
ExecStart=/usr/bin/node /usr/lib/node_modules/@wenyan-md/cli/dist/cli.js serve --port 8080 --api-key-file /etc/wenyan/api-key
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

本机 `.env` 中将 `WECHAT_SERVER_URL` 设为 `http://<服务器IP>:8080`，`WECHAT_SERVER_API_KEY` 与服务器一致即可。

## 运行日志与可观测性

每次运行都会生成 `runs/<timestamp>.json`，记录：

- `trace_id`、时间戳、来源类型、文件名、请求平台
- 各节点耗时（`node_durations`）
- Token 消耗（`token_usage`）
- 发布结果列表（平台 / 成功 / 尝试次数 / URL 或错误）
- 重试事件列表（操作名 / 当前尝试 / 最大次数 / 异常类型 / 等待时间）
- 总耗时、OSS 上传清单

Token 统计直接读取模型响应中的 `usage_metadata`，并兼容 OpenAI 协议常见的 `prompt_tokens` / `completion_tokens` 字段。质量修复或可选 LLM 排版产生多次模型调用时会累加，而不是只记录最后一次调用。

平台发布只在工作流发布节点保留一层重试，避免发布器装饰器和节点循环叠加造成尝试次数膨胀。OSS 上传继续复用公共退避装饰器，两类重试都会进入同一份 `retry_events` 审计轨迹。

## 离线评测

`eval/samples/` 包含 20 篇固定 Markdown：普通技术文章、多图片文章、代码/公式/表格文章、缺失 Front Matter、异常链接和缺失附件，以及未闭合 Markdown 结构。每篇样本在 `eval/manifest.yaml` 声明预期的质量门禁结果。

执行离线评测：

```bash
python -m src.cli evaluate
# 或自定义报告路径
python -m src.cli evaluate --output eval/reports/baseline.json
```

评测不调用模型、OSS、GitHub 或微信服务，输出 JSON 报告并统计：格式结构保留率、元数据结构化成功率、质量检查首次通过率和预期结果匹配率。人工接受率、平均修复次数、平台发布成功率、故障恢复成功率、耗时和 Token 成本将在接入真实运行批次后使用 `runs/*.json` 聚合。

终端以 Rich 表格展示执行概览、各平台发布结果与 Token 用量，便于定位慢节点与失败环节。

## 设计决策

| 决策 | 理由 |
|----------|-----------|
| 选择 LangGraph 而非自定义 DAG | 成熟的状态管理方案，编译期校验节点拓扑，可扩展 |
| 状态用 TypedDict 而非 dataclass | 原生兼容 LangGraph，节点只写各自阶段字段，契约清晰 |
| 格式化默认用规则引擎 | 零成本、确定性结果，保护知识内容不被改写 |
| 默认单点 LLM 调用（rule 模式） | 最小化 Token 成本（约 ¥0.01 / 篇），行为可预测；`llm` 模式叠加润色并带安全回退 |
| 模型走 OpenAI 兼容协议 | 切换国产模型（DeepSeek / 智谱）零新依赖，仅改 config |
| 结构化输出用 function_calling | 规避国产模型不支持 `json_schema` 响应格式的问题 |
| OSS 路径用 pypinyin | 全 ASCII 安全路径，避免 CDN 编码问题 |
| 发布器独立重试 + 故障隔离 | 博客与微信的故障完全解耦，单平台失败不阻塞另一平台 |
| 封面不走 LLM | 展示层素材不占用宝贵 LLM 调用，主源+兜底保证可用性 |
| 采用 Typer + Rich | 符合现代 Python CLI 最佳实践，自动生成 `--help` 与类型校验 |

## 测试状态

`pyproject.toml` 已配置 `pytest`（含 `pytest-asyncio` 开发依赖与 `testpaths`），但 `tests/` 目录下的用例仍在规划中，尚未落地。纯规则节点（`format_optimize`）与配置加载是优先补测对象。
