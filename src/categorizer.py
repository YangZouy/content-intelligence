"""基于标签的文章分类规则引擎。

根据 LLM（summary_meta 节点）已抽取的 tags，将文章映射到
Hexo 博客已有的分类。纯规则、零 LLM 成本、确定性强——
符合项目"规则优先、定点 LLM"的设计哲学。

为什么用规则而不用 LLM 选分类？
  - 分类是个"有限候选集 + 明确映射"问题，规则足够且更可靠；
  - tags 已经是 LLM 的产物，再用规则做一次确定性映射，等于
    "LLM 负责语义抽取 + 规则负责结构决策"的典型分工；
  - 零额外 token 成本，零额外延迟，结果可预测、可测试。

分类候选（来自 config.yaml brand 注释，须与 Hexo 仓库实际类别一致）：
  AI与RAG、前端开发、后端与数据库、工程与运维、
  算法与数据结构、随笔·生活、项目实战

使用方式：
    from src.categorizer import categorize
    category = categorize(["langchain", "rag", "agent"])  # -> "AI与RAG"
"""

from __future__ import annotations

from typing import List, Optional

# ---------------------------------------------------------------------------
# 分类规则：tag 关键词 → 分类
# ---------------------------------------------------------------------------
# 顺序即优先级：当多个分类得分相同时，靠前的胜出（保证确定性）。
# 因此越"具体/专属"的分类越靠前，越"宽泛/兜底"的越靠后。
# 匹配方式：tag 小写后"包含"关键词即命中（substring match），
# 例如 tag="langchain" 命中关键词 "langchain"；tag="react-hooks" 命中 "react"。
# ---------------------------------------------------------------------------

CATEGORY_RULES: List[dict] = [
    {
        "category": "AI与RAG",
        "keywords": [
            "langchain", "langgraph", "langsmith", "llm", "rag", "agent",
            "智能体", "embedding", "向量", "prompt", "提示词", "结构化输出",
            "gpt", "chatgpt", "openai", "deepseek", "大模型", "ai",
            "机器学习", "深度学习", "nlp", "自然语言", "tool", "工具调用",
            "memory", "记忆", "middleware", "中间件", "rerank", "重排",
            "transformer", "微调", "fine-tune", "向量数据库", "qdrant",
            "wenyan", "文颜",
        ],
    },
    {
        "category": "算法与数据结构",
        "keywords": [
            "算法", "algorithm", "数据结构", "leetcode", "力扣",
            "动态规划", "dp", "排序", "查找", "树", "图", "递归",
            "复杂度", "贪心", "回溯", "双指针", "滑动窗口",
        ],
    },
    {
        "category": "前端开发",
        "keywords": [
            "react", "vue", "前端", "frontend", "css", "html", "javascript",
            "typescript", "webpack", "vite", "node", "nodejs", "浏览器",
            "dom", "组件", "redux", "nextjs", "nuxt", "hexo", "前端工程",
        ],
    },
    {
        "category": "后端与数据库",
        "keywords": [
            "后端", "backend", "数据库", "database", "mysql", "postgres",
            "postgresql", "redis", "mongodb", "sql", "nosql", "api", "rest",
            "graphql", "微服务", "服务端", "server", "orm", "django",
            "flask", "fastapi", "spring",
        ],
    },
    {
        "category": "工程与运维",
        "keywords": [
            "docker", "k8s", "kubernetes", "ci/cd", "devops", "运维",
            "部署", "deploy", "nginx", "linux", "shell", "监控", "日志",
            "github actions", "vercel", "oss", "云原生", "容器",
        ],
    },
    {
        "category": "项目实战",
        "keywords": [
            "项目", "实战", "project", "全栈", "fullstack", "落地",
            "架构设计", "系统设计", "面试", "简历",
        ],
    },
    {
        "category": "随笔·生活",
        "keywords": [
            "随笔", "生活", "感悟", "日记", "日常", "读书", "思考",
            "杂谈", "随想",
        ],
    },
]

# 与 config.yaml brand.default_categories 保持一致的最后兜底
DEFAULT_CATEGORY = "随笔·生活"


def categorize(tags: List[str], default: str = DEFAULT_CATEGORY) -> str:
    """根据 tags 选出最匹配的博客分类。

    计分制：遍历每条规则，统计该分类被多少个 tag 命中（命中即 +1），
    取得分最高的分类。得分相同时，规则顺序靠前的胜出（确定性）。
    全部不命中则返回 default。

    参数：
        tags: LLM 抽取的文章标签列表（如 ["langchain", "rag", "python"]）。
        default: 全部不命中时的兜底分类。

    返回：
        单个分类名（如 "AI与RAG"），保证是 Hexo 已有分类之一。
    """
    if not tags:
        return default

    tags_lower = [t.lower() for t in tags]

    best_category: Optional[str] = None
    best_score = 0

    for rule in CATEGORY_RULES:
        score = 0
        for keyword in rule["keywords"]:
            kw = keyword.lower()
            for tag in tags_lower:
                if kw in tag:
                    score += 1
        # 严格大于才覆盖 → 平分时保留靠前的规则（确定性）
        if score > best_score:
            best_score = score
            best_category = rule["category"]

    return best_category if best_category is not None else default


def list_categories() -> List[str]:
    """返回所有可用分类（按规则优先级顺序），供校验/展示用。"""
    return [rule["category"] for rule in CATEGORY_RULES]
