import json
import sys

from agent.llm import LLMClient
from agent.pipelines import FixPipeline, ReviewPipeline
from agent.runtime import AgentConfig

CODE = '''import os
import pickle


def get_user(cursor, uid):
    sql = "SELECT * FROM users WHERE id = " + uid
    cursor.execute(sql)
    return pickle.loads(open("data.bin", "rb").read())


def run(cmd):
    os.system("echo " + cmd)
'''

pipeline = ReviewPipeline(client=LLMClient(mock=True), persist_traces=False)
for strategy in ("fast", "auto", "full"):
    result = pipeline.review(CODE, file_path="demo.py", strategy=strategy)
    print("=" * 70)
    print("strategy:", strategy, "| roles:", result.stats["plan"]["reviewer_roles"])
    print("summary:", result.report.review_summary)
    print("coverage:", json.dumps(result.coverage.to_dict(), ensure_ascii=False))
    print("adjudication:", result.stats["adjudication"])
    print("issues:", [(i.category.value, i.subtype, i.line_start, i.adjudication.value) for i in result.report.issues])
    print("verifier:", result.stats["verifier_status"], "critic:", result.stats["critic"])

fixer = FixPipeline(client=LLMClient(mock=True))
plan = fixer.plan(CODE, result.report)
print("=" * 70)
print("fix plan:", [i.subtype for i in plan.issues], "excluded:", plan.excluded)
fixed, run = fixer.fix(CODE, plan)
print("fix summary:", fixed.summary[:120])
print("verification passed:", fixed.verification.passed)
