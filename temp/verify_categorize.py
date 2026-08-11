"""快速验证 categorize 规则引擎的正确性（临时脚本）。"""
import sys
sys.path.insert(0, "D:/study/content_intelligence_dispatcher")

from src.categorizer import categorize, list_categories

cases = [
    # (描述, tags, 期望分类)
    ("LangChain简介", ["langchain", "llm", "python", "ai"], "AI与RAG"),
    ("RAG实战", ["rag", "embedding", "向量数据库", "qdrant"], "AI与RAG"),
    ("智能体", ["agent", "智能体", "tool", "langchain"], "AI与RAG"),
    ("结构化输出", ["结构化输出", "prompt", "llm"], "AI与RAG"),
    ("中间件", ["middleware", "中间件", "langchain"], "AI与RAG"),
    ("纯前端", ["react", "vue", "前端", "css"], "前端开发"),
    ("算法题", ["leetcode", "动态规划", "算法"], "算法与数据结构"),
    ("后端", ["mysql", "redis", "后端", "api"], "后端与数据库"),
    ("运维", ["docker", "k8s", "部署", "devops"], "工程与运维"),
    ("平分取靠前(AI优先于前端)", ["langchain", "react"], "AI与RAG"),
    ("空 tags 兜底", [], "随笔·生活"),
    ("全不命中兜底", ["美食", "旅游", "摄影"], "随笔·生活"),
    ("随笔类", ["随笔", "读书", "思考"], "随笔·生活"),
]

print("可用分类:", list_categories())
print("=" * 60)
all_pass = True
for desc, tags, expected in cases:
    got = categorize(tags)
    ok = "PASS" if got == expected else "FAIL"
    if got != expected:
        all_pass = False
    print(f"[{ok}] {desc:30s} tags={tags}")
    print(f"       期望={expected!r:20s} 实际={got!r}")
print("=" * 60)
print("全部通过" if all_pass else "存在失败用例，请检查规则")
