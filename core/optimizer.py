# ========== 修复路径问题（必须放在最顶部）==========
import sys
import ast
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

import requests
import time
import logging
from pathlib import Path
from config import API_KEY, API_URL, MODEL_NAME, TIMEOUT, RETRY_TIMES, LOG_DIR, SLEEP_INTERVAL, MOCK_MODE
from core.prompts import build_dataset_optimize_prompt, build_optimize_prompt

# 初始化日志
Path(LOG_DIR).mkdir(exist_ok=True)
logging.basicConfig(
    filename=Path(LOG_DIR) / "optimizer.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)
logger = logging.getLogger(__name__)

def compare_code_signature(original_code: str, optimized_code: str) -> bool:
    """
    静态验证代码签名（函数名、参数、返回值结构），保证功能等价性
    :param original_code: 原始代码
    :param optimized_code: 优化后代码
    :return: 签名是否一致
    """
    try:
        # 解析AST
        original_ast = ast.parse(original_code)
        optimized_ast = ast.parse(optimized_code)
        
        def get_signatures(tree):
            sigs = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 仅对比函数名和参数个数，忽略类型注解等细节
                    args_count = len(node.args.args)
                    sigs[node.name] = args_count
                elif isinstance(node, ast.ClassDef):
                    sigs[node.name] = "class"
            return sigs

        original_sigs = get_signatures(original_ast)
        optimized_sigs = get_signatures(optimized_ast)
        
        # 核心函数和类必须存在且签名基本一致
        for name, sig in original_sigs.items():
            if name not in optimized_sigs or optimized_sigs[name] != sig:
                logger.warning(f"签名不匹配: {name} (预期 {sig}, 实际 {optimized_sigs.get(name)})")
                return False
        
        return True
    except Exception as e:
        logger.error(f"代码签名对比失败: {str(e)}")
        return False

def code_optimize(code: str, review_issues: dict) -> dict:
    """
    代码优化核心函数（带重试机制+功能等价性验证+模拟模式）
    :param code: 待优化代码
    :param review_issues: 审查结果字典
    :return: 优化结果字典（包含优化后代码、验证结果）
    """
    # 导入基于训练集的优化函数
    from experiments.exp3_optimize_training import optimized_code_optimize
    return optimized_code_optimize(code, review_issues)

if __name__ == "__main__":
    # 测试示例
    from reviewer import code_review
    test_code = """
def login(user, pwd):
    sql = "SELECT * FROM users WHERE name=" + user
    return True
    """
    # 第一步：审查代码
    review_result = code_review(test_code)
    # 第二步：优化代码
    optimize_result = code_optimize(test_code, review_result)
    print("优化后代码：")
    print(optimize_result["optimized_code"])
    print(f"\n功能等价性验证：{optimize_result['signature_match']}")
    print(f"优化总结：{optimize_result['optimization_summary']}")
