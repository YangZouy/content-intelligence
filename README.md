# Content Intelligence Dispatcher（内容智能派发系统）

**个人知识内容智能分发系统**

这是一个纯 Python + LangGraph 构建的流水线系统，可导入你撰写的各类内容（Obsidian / 带外链的Markdown），优化排版格式，生成 AI 元数据，将图片上传至 OSS，并针对不同平台（Hexo 博客 / 微信公众号）进行内容适配，最终实现并行发布。

> **零 Dify 依赖。零检索增强。纯代码实现。**

## 核心特性

- **多源导入**：支持本地 Markdown 文件、带附件的 Obsidian 仓库
- **基于规则的格式化**：自动段落拆分、列表转换、术语加粗 — 零 LLM Token 消耗
- **AI 元数据生成**：仅调用一次大模型（默认 DeepSeek，可切换智谱/OpenAI）即可提取标题/摘要/标签（约 ¥0.01/篇）
- **OSS 图床托管**：自动上传至阿里云 OSS，并以拼音生成英文路径
- **双平台发布**：支持 GitHub Pages（Hexo）与微信公众号（通过 wenyan server HTTP 接口）
- **独立重试机制**：博客和微信各自独立发布 — 一个平台失败不会阻塞另一个
- **交互式命令行界面**：提供含最近文件列表的向导模式，或直接执行 `publish ./article.md` 命令

## 系统架构

```
CLI (Typer + Rich)
  │
  ▼
┌─────────────────────────────────────────────┐
│           LangGraph StateGraph               │
│                                             │
│  ingest → format_optimize → summary_meta   │
│    ↓           ↓              ↓            │
│  [原始]    [格式化后]      [元数据]          │
│    ↓                            ↓           │
│  image_process → content_adapt → publish   │
│       ↓              ↓           ↓         │
│   [OSS 图片]     [平台适配]   [并行发布]     │
│                              /      \       │
│                        GitHub    微信       │
└─────────────────────────────────────────────┘
  │                    │              │
  ▼                    ▼              ▼
 runs/*.json        Hexo 仓库     wenyan server
                                (HTTP localhost:3000)
```

## 快速开始

### 1. 安装依赖

```bash
cd content_intelligence_dispatcher
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
# 复制模板文件
cp .env.example .env

# 编辑文件，填入你的实际配置信息
# 至少需要：DEEPSEEK_API_KEY（或 ZHIPU_API_KEY / OPENAI_API_KEY）用于生成摘要
```

### 3. 启动 wenyan server

微信发布依赖本地运行的 wenyan server，需提前全局安装并启动：

```bash
# 安装 wenyan CLI（含 server 功能）
npm install -g @wenyan-md/cli

# 配置微信公众号凭据（仅首次需要）
wenyan config set WECHAT_APP_ID your-app-id
wenyan config set WECHAT_APP_SECRET your-app-secret

# 启动 server（建议用 pm2 常驻）
wenyan serve --port 3000 --api-key your-secret-key
```

确认 server 正常运行：

```bash
curl http://localhost:3000/health
```

### 4. 运行

```bash
# 直接发布：app.command只有一个子命令publish时 命令行必须省略publish
python -m src.cli ./my-article.md

```

## 配置说明

### 环境变量 (.env)

| 变量名 | 是否必需 | 描述 |
|----------|----------|-------------|
| `DEEPSEEK_API_KEY` | 是（默认） | DeepSeek API 密钥，用于摘要/元数据生成（provider=deepseek 时） |
| `ZHIPU_API_KEY` | 视 provider 而定 | 智谱 GLM API 密钥（provider=zhipu 时） |
| `OPENAI_API_KEY` | 视 provider 而定 | OpenAI API 密钥（provider=openai 时） |
| `ALIYUN_ACCESS_KEY_ID` | 处理图片时需要 | 阿里云 OSS 访问密钥 ID |
| `ALIYUN_ACCESS_KEY_SECRET` | 处理图片时需要 | 阿里云 OSS 访问密钥 Secret |
| `ALIYUN_OSS_ENDPOINT` | 处理图片时需要 | OSS 终端节点 URL |
| `ALIYUN_BUCKET_NAME` | 处理图片时需要 | Bucket 名称 |
| `GITHUB_TOKEN` | 发布博客时需要 | GitHub 个人访问令牌 |
| `GITHUB_USERNAME` | 发布博客时需要 | GitHub 用户名 |
| `GITHUB_HEXO_REPO` | 发布博客时需要 | Hexo 仓库（格式：所有者/仓库名） |
| `WECHAT_SERVER_URL` | 发布微信时需要 | wenyan server 地址（默认 `http://localhost:3000`） |
| `WECHAT_SERVER_API_KEY` | 发布微信时需要 | 启动 wenyan server 时设置的 API Key |

### config.yaml

编辑 `config/config.yaml` 可自定义以下内容：
- OSS 设置（终端节点、Bucket、最大图片数量）
- GitHub 仓库配置
- 品牌默认值（名称、受众、语气）
- LLM 模型选择（provider：openai / deepseek / zhipu / custom）
- 默认发布平台选择

## 项目结构

```
content_intelligence_dispatcher/
├── config/
│   ├── config.yaml          # 主配置文件
│   └── user_prefs.yaml      # 用户偏好设置（自动管理）
├── src/
│   ├── cli.py               # 基于 Typer 的交互式命令行界面
│   ├── graph.py             # LangGraph StateGraph 编排逻辑
│   ├── state.py             # TypedDict 代理状态定义
│   ├── schema.py            # Pydantic 模型（LLM 输出、Front-Matter）
│   ├── config_loader.py     # 支持优先级链的 YAML + .env 加载器
│   ├── llm.py               # ChatOpenAI 工厂函数 (gpt-4o-mini)
│   ├── oss_client.py        # 阿里云 OSS 上传客户端
│   ├── errors.py            # 异常层级结构 + 重试装饰器
│   ├── observability.py     # loguru 追踪 + 运行日志持久化
│   ├── nodes/
│   │   ├── ingest.py        # 多源内容导入
│   │   ├── format_optimize.py  # 基于规则的格式化引擎
│   │   ├── summary_meta.py  # LLM 摘要/元数据提取
│   │   ├── image_process.py # OSS 上传 + 链接替换
│   │   ├── content_adapt.py # 特定平台内容适配
│   │   └── publish.py       # 并行发布编排器
│   └── publishers/
│       ├── base.py          # 发布器接口协议
│       ├── github_pages.py  # GitHub Pages 发布器
│       └── wechat.py        # 微信公众号 (wenyan server HTTP) 发布器
├── tests/                   # 测试套件
├── runs/                    # 运行日志（自动创建）
└── logs/                    # 应用日志（自动创建）
```

## 流水线节点详解

### 1. IngestNode（`ingest` 导入节点）
- 检测内容来源类型（Obsidian / 飞书 / Markdown 链接）
- 读取文件内容
- 提取所有图片引用（Markdown 和 HTML 语法）
- 扫描本地目录以匹配 Obsidian 风格的附件

### 2. FormatOptimizeNode（`format_optimize` 格式化优化节点）
- **纯规则引擎 — 零 LLM Token 消耗**
- 长段落拆分（在超过200字符的句子边界处拆分）
- 并列项 → 无序列表 (`- `)
- 顺序步骤 → 有序列表 (`1. `)
- 关键术语加粗 (`**术语**`)
- 警告/提示 → 引用块 (`> ⚠️` / `> 💡`)
- 代码块语言标注
- 长文章自动添加分节分隔符

**硬性约束**：绝不修改知识性内容（论点、数据、结论）

### 3. SummaryMetaNode（`summary_meta` 摘要元数据节点）
- **整个系统中唯一的 LLM 调用点**
- 使用可配置的大模型（默认 DeepSeek `deepseek-chat`，可切智谱 `glm-4-flash` 或 OpenAI `gpt-4o-mini`），输出结构化的 Pydantic 格式
- 提取：标题、摘要（≤200字）、标签（3-6个）、字数统计
- 保留原文中已有的 H1 标题
- Token 预算：约 ¥0.01 每篇文章

### 4. ImageProcessNode（`image_process` 图片处理节点）
- 将所有图片上传至阿里云 OSS
- 通过 pypinyin 生成英文路径（例如：`images/ShenDuXueXiRu/`）
- 将内容中的所有图片引用替换为 OSS 链接
- 选取第一张图片作为封面图
- 每篇文章最多处理10张图片（超出部分截断并发出警告）

### 5. ContentAdaptNode（`content_adapt` 内容适配节点）
- **Hexo 文档**：生成 YAML Front-Matter + 正文（标题、日期、标签、分类、封面图、描述）
- **微信草稿**：生成不含 Front-Matter 的纯 Markdown 正文，图片使用 OSS 链接

### 6. PublishNode（`publish` 发布节点）
- 每个平台独立并行发布
- 各平台拥有独立的重试逻辑（指数退避，最多重试2次）
- **一个平台发布失败，绝不会阻塞另一个平台的发布**
- 结果汇总为 `PublishResultItem[]` 列表

## 微信发布架构

微信发布通过本地运行的 wenyan server（HTTP 模式）实现，彻底替代原有的 stdio MCP 子进程方案：

```
Python 项目
    │
    │  HTTP POST /upload  （上传 Markdown）
    │  HTTP POST /publish （触发渲染+发布）
    ▼
wenyan server (localhost:3000)
    │
    │  微信公众号 API
    ▼
微信草稿箱
```

**优势**：无需管理子进程生命周期、握手超时和管道通信，发布逻辑简化为两次 HTTP 请求。

### 固定 IP 问题解决方案（服务器部署wenyan server）

服务器配置与本机PC版本一致的wenyan-cli，设置api-key，微信需要的密钥等，服务器上启动服务，默认端口为3000，修改成了8080，防火墙中放开8080端口，本机PC命令行中添加 --server表示与服务器通信

## 设计决策

| 决策 | 理由 |
|----------|-----------|
| 选择 LangGraph 而非自定义 DAG | 久经考验的状态管理方案，可视化，可扩展 |
| 状态定义采用 TypedDict 而非 dataclass | 原生兼容 LangGraph，清晰的阶段归属 |
| 格式化采用规则引擎而非 LLM | 零成本，确定性结果，保护知识内容 |
| 仅保留单点 LLM 调用 | 最小化 Token 成本（预算 ≤ ¥5/月），行为可预测 |
| 模型走 OpenAI 兼容协议 | 切换国产模型（DeepSeek / 智谱）零新依赖，仅改 config |
| OSS 路径使用 pypinyin | 全 ASCII 安全路径，避免 CDN 编码问题 |
| 发布器独立重试 | 博客和微信的故障完全解耦 |
| 采用 Typer + Rich + questionary | 符合现代 Python CLI 最佳实践 |
