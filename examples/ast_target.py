import ast
import hashlib


def get_ast_hash(code: str) -> str:
    """获取代码AST的哈希值，用于比较AST是否等价"""
    if not code:
        return ""
    try:
        tree = ast.parse(code)
        # 将AST转换为字符串表示并计算哈希
        ast_str = ast.dump(tree, annotate_fields=False)
        return hashlib.md5(ast_str.encode()).hexdigest()
    except Exception as e:
        # 增加错误处理，但不影响主流程
        return ""


def check_ast_match(code1: str, code2: str) -> bool:
    """检查两段代码的AST是否匹配"""
    if not code1 or not code2:
        return False
    hash1 = get_ast_hash(code1)
    hash2 = get_ast_hash(code2)
    if not hash1 or not hash2:
        return False
    return hash1 == hash2


# 测试用例
print("=== AST 功能测试 ===")

# 测试1: 空代码
print("\n测试1: 空代码")
empty_hash = get_ast_hash("")
print(f"空代码哈希: '{empty_hash}'")
print(f"空代码匹配: {check_ast_match("", "")}")

# 测试2: 简单代码
print("\n测试2: 简单代码")
code1 = "def add(a, b): return a + b"
code2 = "def add(a, b):\n    return a + b"
code3 = "def add(x, y): return x + y"

print(f"代码1哈希: {get_ast_hash(code1)}")
print(f"代码2哈希: {get_ast_hash(code2)}")
print(f"代码3哈希: {get_ast_hash(code3)}")
print(f"代码1和代码2匹配: {check_ast_match(code1, code2)}")
print(f"代码1和代码3匹配: {check_ast_match(code1, code3)}")

# 测试3: 语法错误
print("\n测试3: 语法错误")
error_code = "def add(a, b: return a + b"
error_hash = get_ast_hash(error_code)
print(f"错误代码哈希: '{error_hash}'")
print(f"错误代码匹配: {check_ast_match(error_code, code1)}")

print("\n=== 测试完成 ===")
