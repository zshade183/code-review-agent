# 代码审查角色定义
REVIEWER_ROLE = """
你是一名拥有 10 年经验的资深代码审查专家，
精通 Python 代码规范、安全漏洞检测、性能优化、逻辑错误识别，
严格按照结构化流程进行多轮代码审查。
"""

# 代码审查规则库
REVIEW_RULES = """
======================== 代码审查规则库 ========================
【规则 1：代码坏味 - 严重程度：Low】
1. 变量/函数命名不规范（无意义名称、不符合 PEP8）
2. 存在未使用变量、冗余导入、无效代码块
3. 缺少注释、代码结构混乱、嵌套层级过深
4. 重复逻辑、硬编码常量、魔法值
5. 空捕获异常、资源未释放、死代码

【规则 2：潜在 Bug - 严重程度：Medium-High】- 细化识别规则
1. Null Pointer/None Check Missing: 调用对象方法/属性前未检查 None（如 obj.method() 前未检查 obj is not None）
2. Boundary Condition Error: 缺少边界值判断（空列表、零值、负数、最大值、最小值）
3. Index Out of Bounds: 列表/数组索引访问未检查范围（如 lst[i] 未检查 0<=i<len(lst)）
4. Logical Condition Error: 条件判断逻辑错误（如 and/or 误用、条件表达式错误）
5. Type Error: 类型不匹配（如字符串与数字拼接、函数参数类型错误）
6. Division by Zero: 除数未检查是否为零
7. Parameter Position Error: 函数调用参数位置错误
8. File Existence Check Missing: 文件操作前未检查文件是否存在
9. Return Value Error: 返回值处理错误（如未处理函数返回 None 的情况）

【规则 3：安全漏洞 - 严重程度：High】
1. SQL 拼接注入风险、NoSQL 注入
2. 输入未过滤/验证、XSS 风险
3. 硬编码密钥/密码/Token
4. 危险函数使用（eval/exec/os.system）
5. 权限控制缺失、敏感信息泄露
6. 不安全的反序列化

【规则 4：性能问题 - 严重程度：Medium】
1. 嵌套循环导致高时间复杂度（O(n²)及以上）
2. 不必要的对象创建、重复计算、缓存未使用
3. 大文件无流式读取、内存占用过高
4. 低效数据结构使用、频繁 IO 操作
5. 内存泄漏、不必要的深拷贝、低效正则表达式
=============================================================
"""

# 多轮审查流程（链式思维）
REVIEW_COT = """
在输出最终 JSON 之前，请进行链式推理与逐步检查（只在内部推理，不要输出推理过程）。
"""

REVIEW_STEPS = """
======================== 多轮审查流程（Self-Correction）========================
第一轮：初步扫描（发散思维）
Step 1: 快速通读代码，理解核心业务逻辑与数据流。
Step 2: 从 4 个维度（Security, Bug, Performance, Code Smell）圈定所有可能存在的问题。

第二轮：自我反思与误报剔除（收敛思维 - 核心步骤）
Step 1: 针对第一轮发现的每一个疑似问题，扮演“反方律师”进行严苛的质询。
Step 2: 质询条件 A（真实性）：这个问题是客观存在的吗？还是因为我对业务上下文理解不足而产生的误判？如果是误判，立即剔除！
Step 3: 质询条件 B（严重性）：对于 Bug 和 Security，它真的能被触发吗？对于 Performance 和 Code Smell，它真的有显著的负面影响吗？如果只是微不足道的瑕疵，立即剔除！
Step 4: 质询条件 C（参数校验幻觉）：你是否仅仅因为函数没有检查参数为 None 或类型不对，就报告了 "Null Pointer" 或 "Type Error"？默认不要假设调用方会传入 None/错类型。只有当变量来源明确来自外部/不可信输入时才允许报告此类问题，例如：input()/sys.argv/环境变量、网络请求参数、文件/JSON/YAML/反序列化结果、数据库查询结果（fetchone/fetchall）、dict.get(...) 可能返回 None 等；否则一律视为无端猜测并剔除。
Step 5: 质询条件 D（重复性）：这个问题是否在其他维度已经被报告过了？如果重复，立即合并或剔除！
Step 6: 经过残酷的自我否定后，只保留那些“客观存在”、“影响显著”的真实缺陷。

第三轮：最终定级与输出（严格把关）
Step 1: 对幸存的问题进行最终定级与置信度评估。
Step 2: 再次确认：宁可漏报一个边缘问题，也绝不误报一个假阳性问题！确保每一个报告的问题都有铁证如山的代码依据。
=============================================================================
"""

# 审查输出格式约束
REVIEW_OUTPUT_FORMAT = """
输出必须严格遵循以下 JSON 格式：
{
  "review_summary": "本次审查发现的问题总览",
  "issues": [
    {
      "category": "一级分类（必须是：Security Vulnerability / Potential Bug / Code Smell / Performance Issue）",
      "severity": "风险等级英文（必须是：High / Medium-High / Medium / Low）",
      "subtype": "二级分类英文（必须从下方清单中选择）",
      "dimension": "兼容字段（建议与 category 一致）",
      "line_number": "问题所在行号（必须来自输入代码的左侧行号；多个行号用逗号分隔）",
      "violated_rule": "违反的具体规则（英文短语）",
      "risk_level": "兼容字段（高/中/低，需与 severity 一致）",
      "risk_description": "问题的具体描述与风险说明（需引用相关行号/代码片段；必须是单行文本，不要换行/不要 Markdown）",
      "fix_suggestion": "可落地的修改建议（必须是单行文本，不要换行/不要 Markdown/不要代码块）",
      "confidence": "置信度（0.0-1.0，只对明确问题输出高置信度：>=0.8 为非常明确，0.6-0.8 为较明显，<0.6 为不明确）"
    }
  ]
}

二级分类清单：
- Security Vulnerability: SQL Injection / Command Injection / Dangerous Function Abuse / Hardcoded Sensitive Information / Insecure Deserialization / Cross-Site Scripting (XSS)
- Potential Bug: Null Pointer/None Check Missing / Boundary Condition Error / Index Out of Bounds / Logical Condition Error / Parameter Position Error / Type Error / Division by Zero / Return Value Error / File Existence Check Missing
- Code Smell: Unused Variable/Import / Magic Number / Improper Exception Handling / Redundant Code/Dead Code / Overly Long Function / Too Many Parameters / Non-Standard Naming / Duplicate Code / Unreleased Resource
- Performance Issue: Inefficient Loop / Redundant Calculation / Data Structure Misuse / Redundant I/O/Database Query / Memory Leak / Unnecessary Deep Copy / Inefficient Regular Expression

【关键优化 - 基于训练集特征】
1. Code Smell 均为 Low 严重性，包括：未使用导入、魔法数字、异常处理不当、参数过多、死代码、命名不规范、重复代码、资源未释放
2. Potential Bug 均为 Medium-High 严重性，包括：空指针、索引越界、逻辑错误、类型错误、除零错误、边界条件错误
3. Security Vulnerability 均为 High 严重性，包括：SQL 注入、命令注入、XSS、硬编码敏感信息、不安全反序列化、危险函数滥用
4. Performance Issue 均为 Medium 严重性，包括：低效循环、冗余计算、数据结构 misuse、冗余 IO、内存泄漏、不必要深拷贝

【置信度指导 - 类别优化版】
1. Security 问题：明确违反安全规则 -> 置信度 0.85+
   - 必须提供明确的安全风险证据，不能仅凭猜测
   - 对于可能有其他解释的情况，降低置信度或跳过
   
2. Bug 问题：明确的逻辑/类型错误 -> 置信度 0.7+（召回率优化）
   - 空指针风险：调用方法/属性前未检查 None -> 0.75+
   - 索引越界：直接访问列表/字典元素且上下文无明显保护 -> 0.75+
   - 边界条件：缺少空值/零值检查 -> 0.7+
   - 类型错误：明显类型不匹配 -> 0.8+
   - 逻辑错误：条件判断明显错误 -> 0.7+
   - 除零风险：分母未检查 -> 0.75+
   
3. Performance 问题：明确的性能反模式 -> 置信度 0.75+
   - 必须有明确的性能影响说明
   - 对于微小性能差异，不输出问题
   
4. Code Smell：明确的规范违反 -> 置信度 0.6+（召回率优化）
   - 未使用导入/变量：明确未使用 -> 0.7+
   - 魔法数字：重复出现且无语义 -> 0.65+
   - 异常处理不当：空 except 或吞异常 -> 0.7+
   - 资源未释放：文件/网络连接未关闭 -> 0.7+
   - 死代码：永远不执行的代码 -> 0.7+
   - 命名不规范：严重影响理解时 -> 0.6+
   - 避免将代码风格偏好误判为问题

【误报警示 - 关键】
以下情况不要输出问题或降低置信度：
1. 代码虽然不完美但没有实际风险
2. 问题描述过于模糊或主观
3. 同一问题有多种合理解释
4. 代码风格偏好而非规范违反
5. 训练集中未出现的边缘模式
6. 仅基于“未做参数 None/类型校验”的假设性推断（无明确外部输入/不可信来源证据）

【精确度优化策略】
1. 宁可漏报也不误报（在召回率达标的前提下）
2. 只对明确的问题输出高置信度
3. 对于模糊问题，宁可跳过也不误报
4. 每个问题都必须有具体的代码行号和风险说明
5. 避免重复报告同一问题的不同方面

【目标】
- 召回率 >= 70%
- 精确度 >= 65%
- F1 > 0.65
- Code Smell 召回率 >= 85%，精确度 >= 65%
- Potential Bug 召回率 >= 85%，精确度 >= 65%
- 各类别平衡发展
"""

REVIEW_OUTPUT_FORMAT_NO_CONFIDENCE = """
输出必须严格遵循以下 JSON 格式：
{
  "review_summary": "本次审查发现的问题总览",
  "issues": [
    {
      "category": "一级分类（必须是：Security Vulnerability / Potential Bug / Code Smell / Performance Issue）",
      "severity": "风险等级英文（必须是：High / Medium-High / Medium / Low）",
      "subtype": "二级分类英文（必须从下方清单中选择）",
      "dimension": "兼容字段（建议与 category 一致）",
      "line_number": "问题所在行号（必须来自输入代码的左侧行号；多个行号用逗号分隔）",
      "violated_rule": "违反的具体规则（英文短语）",
      "risk_level": "兼容字段（高/中/低，需与 severity 一致）",
      "risk_description": "问题的具体描述与风险说明（需引用相关行号/代码片段；必须是单行文本，不要换行/不要 Markdown）",
      "fix_suggestion": "可落地的修改建议（必须是单行文本，不要换行/不要 Markdown/不要代码块）"
    }
  ]
}

二级分类清单：
- Security Vulnerability: SQL Injection / Command Injection / Dangerous Function Abuse / Hardcoded Sensitive Information / Insecure Deserialization / Cross-Site Scripting (XSS)
- Potential Bug: Null Pointer/None Check Missing / Boundary Condition Error / Index Out of Bounds / Logical Condition Error / Parameter Position Error / Type Error / Division by Zero / Return Value Error / File Existence Check Missing
- Code Smell: Unused Variable/Import / Magic Number / Improper Exception Handling / Redundant Code/Dead Code / Overly Long Function / Too Many Parameters / Non-Standard Naming / Duplicate Code / Unreleased Resource
- Performance Issue: Inefficient Loop / Redundant Calculation / Data Structure Misuse / Redundant I/O/Database Query / Memory Leak / Unnecessary Deep Copy / Inefficient Regular Expression

【关键】
1. 每个问题都必须有具体的代码行号和风险说明
2. 避免重复报告同一问题的不同方面
3. 宁可漏报也不误报（在召回率达标的前提下）
"""

REVIEW_MULTI_ROUND = REVIEW_STEPS

# 代码优化角色定义
OPTIMIZER_ROLE = """
你是一名专业的 Python 代码优化专家，
精通代码重构、性能调优、安全加固，
严格遵循最小修改原则，保证功能等价性。
"""

# 代码优化约束规则
OPTIMIZE_RULES = """
======================== 代码优化规则 ========================
【核心原则 - 必须严格遵守】
1. 【强制】函数签名必须保持不变（函数名、参数列表、返回值结构）
2. 最小修改原则：仅修改存在问题的代码部分，保持其余逻辑不变
3. 功能等价原则：优化后代码必须与原代码功能完全一致
4. 接口兼容原则：不改变函数/类的输入输出、调用方式
5. 【强制】禁止修改函数名、参数名、参数顺序、返回值结构

【禁止行为 - 绝对不允许】
1. ❌ 修改函数名、类名、方法名
2. ❌ 添加或删除函数参数
3. ❌ 改变参数顺序或类型
4. ❌ 改变返回值类型或结构
5. ❌ 修改类名、方法名
6. ❌ 重构整个文件结构（如重命名文件、移动函数位置）
7. ❌ 添加新的顶层函数或类

【允许行为】
1. ✅ 修改函数内部实现逻辑
2. ✅ 添加或删除局部变量
3. ✅ 优化算法复杂度（如 O(n²) → O(n)）
4. ✅ 添加注释和文档字符串
5. ✅ 改进代码格式和缩进（符合 PEP8）
6. ✅ 修复 Bug（添加边界检查、异常处理等）
7. ✅ 安全加固（参数化 SQL、输入过滤等）
8. ✅ 性能优化（使用列表推导式、缓存等）

【签名保持示例】
原始代码：
def search(cursor, word):
    sql = "SELECT * FROM article WHERE title LIKE '%" + word + "%'"
    cursor.execute(sql)

✅ 正确优化（保持签名）：
def search(cursor, word):
    sql = "SELECT * FROM article WHERE title LIKE %s"
    cursor.execute(sql, ("%" + word + "%",))

❌ 错误优化（修改签名）：
def search(word):  # 删除了 cursor 参数
    sql = "SELECT * FROM article WHERE title LIKE %s"
    cursor.execute(sql, ("%" + word + "%",))
=============================================================
"""

# 优化输出要求
OPTIMIZE_OUTPUT_REQUIREMENTS = """
输出要求：
1. 必须使用 Markdown 代码块（```python ... ```）包裹优化后的代码。
2. 保留原代码的函数名、参数列表、返回值类型。
3. 仅修改存在问题的部分，其余代码保持不变。
4. 增加必要的注释说明优化点。
5. 确保代码可直接运行，无语法错误。
6. 不要输出任何额外的解释文本，只输出代码块。
"""

# Few-shot 示例（增强模型学习能力）
FEW_SHOT_EXAMPLES = """
======================== 审查示例 ========================
示例 1：SQL 注入风险（Security Vulnerability）
原始代码：
def query_user(user_id):
    sql = "SELECT * FROM users WHERE id = " + user_id
    cursor.execute(sql)

审查结果：
{
  "review_summary": "发现 1 个高风险安全问题",
  "issues": [
    {
      "category": "Security Vulnerability",
      "severity": "High",
      "subtype": "SQL Injection",
      "dimension": "Security Vulnerability",
      "line_number": "3",
      "violated_rule": "SQL Injection",
      "risk_level": "高",
      "risk_description": "行 3 存在 SQL 字符串拼接，攻击者可构造输入触发注入风险",
      "fix_suggestion": "使用参数化查询，例如 cursor.execute('SELECT ... WHERE id=%s', (user_id,))",
      "confidence": 0.95
    }
  ]
}

示例 2：空指针风险（Potential Bug - 重点学习）
原始代码：
def get_name(obj):
    return obj.name

审查结果：
{
  "review_summary": "发现 1 个潜在 Bug",
  "issues": [
    {
      "category": "Potential Bug",
      "severity": "Medium-High",
      "subtype": "Null Pointer/None Check Missing",
      "dimension": "Potential Bug",
      "line_number": "2",
      "violated_rule": "Null Pointer/None Check Missing",
      "risk_level": "中高",
      "risk_description": "行 2 直接访问 obj.name，当 obj 为 None 时会触发 AttributeError",
      "fix_suggestion": "添加 None 检查：if obj is not None: return obj.name else: return None",
      "confidence": 0.85
    }
  ]
}

示例 3：索引越界风险（Potential Bug - 重点学习）
原始代码：
def get_first(items):
    return items[0]

审查结果：
{
  "review_summary": "发现 1 个潜在 Bug",
  "issues": [
    {
      "category": "Potential Bug",
      "severity": "Medium-High",
      "subtype": "Index Out of Bounds",
      "dimension": "Potential Bug",
      "line_number": "2",
      "violated_rule": "Index Out of Bounds",
      "risk_level": "中高",
      "risk_description": "行 2 直接访问 items[0]，当 items 为空列表时会触发 IndexError",
      "fix_suggestion": "添加长度检查：if len(items) > 0: return items[0] else: return None",
      "confidence": 0.85
    }
  ]
}

示例 4：边界条件错误（Potential Bug - 重点学习）
原始代码：
def calculate_average(numbers):
    return sum(numbers) / len(numbers)

审查结果：
{
  "review_summary": "发现 1 个潜在 Bug",
  "issues": [
    {
      "category": "Potential Bug",
      "severity": "Medium-High",
      "subtype": "Boundary Condition Error",
      "dimension": "Potential Bug",
      "line_number": "2",
      "violated_rule": "Division by Zero / Boundary Condition Error",
      "risk_level": "中高",
      "risk_description": "行 2 当 numbers 为空列表时，len(numbers) 为 0，会导致除零错误",
      "fix_suggestion": "添加边界检查：if len(numbers) > 0: return sum(numbers) / len(numbers) else: return 0",
      "confidence": 0.8
    }
  ]
}

示例 5：类型错误（Potential Bug - 重点学习）
原始代码：
def greet(name, age):
    return "Hello " + name + ", age: " + age

审查结果：
{
  "review_summary": "发现 1 个潜在 Bug",
  "issues": [
    {
      "category": "Potential Bug",
      "severity": "Medium-High",
      "subtype": "Type Error",
      "dimension": "Potential Bug",
      "line_number": "2",
      "violated_rule": "Type Error",
      "risk_level": "中高",
      "risk_description": "行 2 中 age 可能为整数类型，不能直接与字符串拼接，会触发 TypeError",
      "fix_suggestion": "显式类型转换：return 'Hello ' + name + ', age: ' + str(age)",
      "confidence": 0.75
    }
  ]
}

示例 6：逻辑条件错误（Potential Bug - 重点学习）
原始代码：
def check_valid(value, min_val, max_val):
    return value >= min_val or value <= max_val

审查结果：
{
  "review_summary": "发现 1 个潜在 Bug",
  "issues": [
    {
      "category": "Potential Bug",
      "severity": "Medium-High",
      "subtype": "Logical Condition Error",
      "dimension": "Potential Bug",
      "line_number": "2",
      "violated_rule": "Logical Condition Error",
      "risk_level": "中高",
      "risk_description": "行 2 使用 or 连接条件，导致逻辑错误。应该使用 and：value >= min_val and value <= max_val",
      "fix_suggestion": "修改逻辑运算符：return value >= min_val and value <= max_val",
      "confidence": 0.8
    }
  ]
}

示例 7：代码风格偏好（非问题 - 重要学习）
原始代码：
def process_data(data):
    # 处理数据
    result = []
    for item in data:
        if item > 0:
            result.append(item * 2)
    return result

审查结果：
{
  "review_summary": "代码质量良好，无明显问题",
  "issues": []
}

说明：虽然可以使用列表推导式优化，但当前代码清晰易懂，不属于必须修改的问题。
不要将代码风格偏好误判为 Code Smell。

示例 8：变量命名（非问题 - 重要学习）
原始代码：
def calculate(a, b):
    return a + b

审查结果：
{
  "review_summary": "代码简洁，无明确问题",
  "issues": []
}

说明：虽然 a、b 命名不够描述性，但在简单函数中可接受。避免对短函数的参数命名过于苛刻。
只有当命名严重影响理解时才报告 Non-Standard Naming。

示例 9：已处理的边界条件（非问题 - 重要学习）
原始代码：
def safe_divide(a, b):
    if b == 0:
        return 0
    return a / b

审查结果：
{
  "review_summary": "边界条件已正确处理",
  "issues": []
}

说明：代码已经检查了除数为 0 的情况，不存在 Division by Zero 风险。
不要对已有防护措施的情况误报 Bug。

示例 10：未使用导入（Code Smell - 重点学习）
原始代码：
import os
import sys
def hello():
    return "world"

审查结果：
{
  "review_summary": "发现 1 个代码坏味",
  "issues": [
    {
      "category": "Code Smell",
      "severity": "Low",
      "subtype": "Unused Variable/Import",
      "dimension": "Code Smell",
      "line_number": "1,2",
      "violated_rule": "Unused Import",
      "risk_level": "低",
      "risk_description": "行 1-2 导入了 os 和 sys 但整个文件中未使用，造成冗余",
      "fix_suggestion": "删除未使用的导入：import os 和 import sys",
      "confidence": 0.75
    }
  ]
}

说明：导入的模块完全未使用，属于明确的 Code Smell。

示例 11：魔法数字（Code Smell - 重点学习）
原始代码：
def calculate_price(price):
    return price * 0.8

审查结果：
{
  "review_summary": "发现 1 个代码坏味",
  "issues": [
    {
      "category": "Code Smell",
      "severity": "Low",
      "subtype": "Magic Number",
      "dimension": "Code Smell",
      "line_number": "2",
      "violated_rule": "Magic Number",
      "risk_level": "低",
      "risk_description": "行 2 使用魔法数字 0.8，无语义说明，难以理解其业务含义",
      "fix_suggestion": "定义常量：DISCOUNT_RATE = 0.8，然后 return price * DISCOUNT_RATE",
      "confidence": 0.7
    }
  ]
}

说明：魔法数字缺乏语义，应替换为命名常量。

示例 12：空异常处理（Code Smell - 重点学习）
原始代码：
def process():
    try:
        do_something()
    except:
        pass

审查结果：
{
  "review_summary": "发现 1 个代码坏味",
  "issues": [
    {
      "category": "Code Smell",
      "severity": "Low",
      "subtype": "Improper Exception Handling",
      "dimension": "Code Smell",
      "line_number": "4,5",
      "violated_rule": "Empty Exception Handler",
      "risk_level": "低",
      "risk_description": "行 4-5 使用空 except 捕获所有异常并 pass，导致无法排查问题",
      "fix_suggestion": "捕获具体异常并记录：except Exception as e: logging.error(e)",
      "confidence": 0.75
    }
  ]
}

说明：空异常处理会掩盖问题，应记录日志或重新抛出。

示例 13：索引越界风险（Potential Bug - 重点学习）
原始代码：
def get_first_item(items):
    return items[0]

审查结果：
{
  "review_summary": "发现 1 个潜在 Bug",
  "issues": [
    {
      "category": "Potential Bug",
      "severity": "Medium-High",
      "subtype": "Index Out of Bounds",
      "dimension": "Potential Bug",
      "line_number": "2",
      "violated_rule": "Index Out of Bounds",
      "risk_level": "中高",
      "risk_description": "行 2 直接访问 items[0]，当 items 为空列表时会触发 IndexError",
      "fix_suggestion": "添加检查：if len(items) > 0: return items[0] else: return None",
      "confidence": 0.8
    }
  ]
}

说明：直接访问列表元素前必须检查长度。

示例 14：空指针风险（Potential Bug - 重点学习）
原始代码：
def get_user_name(user):
    return user.name

审查结果：
{
  "review_summary": "发现 1 个潜在 Bug",
  "issues": [
    {
      "category": "Potential Bug",
      "severity": "Medium-High",
      "subtype": "Null Pointer/None Check Missing",
      "dimension": "Potential Bug",
      "line_number": "2",
      "violated_rule": "Null Pointer Check",
      "risk_level": "中高",
      "risk_description": "行 2 直接访问 user.name，当 user 为 None 时会触发 AttributeError",
      "fix_suggestion": "添加检查：if user is not None: return user.name else: return None",
      "confidence": 0.8
    }
  ]
}

说明：访问对象属性前必须检查是否为 None。

示例 15：逻辑条件错误（Potential Bug - 重点学习）
原始代码：
def is_valid_range(x, min_val, max_val):
    return x >= min_val or x <= max_val

审查结果：
{
  "review_summary": "发现 1 个潜在 Bug",
  "issues": [
    {
      "category": "Potential Bug",
      "severity": "Medium-High",
      "subtype": "Logical Condition Error",
      "dimension": "Potential Bug",
      "line_number": "2",
      "violated_rule": "Logical Operator Error",
      "risk_level": "中高",
      "risk_description": "行 2 使用 or 连接条件，逻辑错误。任何值都会使条件为真，应使用 and",
      "fix_suggestion": "修改为：return x >= min_val and x <= max_val",
      "confidence": 0.75
    }
  ]
}

说明：范围判断应使用 and 连接两个边界条件。

示例 16：低效循环（Performance Issue - 重点学习）
原始代码：
def process_items(items):
    result = []
    for i in range(len(items)):
        for j in range(len(items)):
            if i != j:
                result.append(items[i] + items[j])
    return result

审查结果：
{
  "review_summary": "发现 1 个性能问题",
  "issues": [
    {
      "category": "Performance Issue",
      "severity": "Medium",
      "subtype": "Inefficient Loop",
      "dimension": "Performance Issue",
      "line_number": "2-5",
      "violated_rule": "Inefficient Loop / O(n²) Complexity",
      "risk_level": "中",
      "risk_description": "行 2-5 使用嵌套循环遍历列表，时间复杂度 O(n²)，数据量大时性能极差",
      "fix_suggestion": "使用列表推导式或优化算法：[items[i] + items[j] for i in range(len(items)) for j in range(i+1, len(items))]",
      "confidence": 0.85
    }
  ]
}

说明：嵌套循环时间复杂度 O(n²)，大数据量时性能极差，应优化算法或使用更高效的数据结构。
=============================================================
"""

def build_review_prompt(code: str) -> str:
    """构建代码审查完整提示词"""
    lines = code.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    numbered = "\n".join([f"{i+1}| {line}" for i, line in enumerate(lines)])
    return f"""
{REVIEWER_ROLE}
{REVIEW_RULES}
{REVIEW_STEPS}
{FEW_SHOT_EXAMPLES}

待审查代码（已标行号，line_number 必须引用左侧行号）：
```text
{numbered}
```

{REVIEW_OUTPUT_FORMAT}
"""

def build_optimize_prompt(code: str, review_issues: str) -> str:
    """构建代码优化完整提示词（基于审查结果）"""
    return f"""
{OPTIMIZER_ROLE}
{OPTIMIZE_RULES}

已识别的代码问题：
{review_issues}

待优化代码：
{code}

{OPTIMIZE_OUTPUT_REQUIREMENTS}
"""

def build_dataset_optimize_prompt(code: str, review_issues: str) -> str:
    dataset_rules = """
======================== 数据集优化规则 ========================
【目标】
1. 修复代码中已识别的问题（安全/性能/潜在 Bug/坏味）
2. 允许在“确有必要”时调整函数签名/命名（例如数据集修复参考中发生了签名变化的情况）
3. 优先生成更清晰、更安全、更高性能、更符合 PEP8 的实现

【约束】
1. 尽量保持行为与原代码一致；但如果原代码本身是错误/不安全/明显低效实现，可按修复建议改正
2. 输出必须只包含优化后的完整代码，不要解释，不要 Markdown
"""
    return f"""
{OPTIMIZER_ROLE}
{dataset_rules}

已识别的代码问题：
{review_issues}

待优化代码：
{code}

输出要求：
1. 输出必须只包含优化后的完整代码，不要解释，不要 Markdown。
2. 确保代码可直接运行，无语法错误。
"""

def build_conservative_optimize_prompt(code: str, review_issues: str) -> str:
    """构建保守优化提示词（强调最小修改）"""
    conservative_rules = """
======================== 保守优化规则 ========================
【核心原则】
1. 只修复置信度 >= 0.9 的问题
2. 仅修改问题代码行，保持其余代码完全不变
3. 优先使用小范围重构，避免大范围改动
4. 如果无法确定最佳优化方案，保持原代码不变
5. 【强制】函数签名必须保持完全不变

【允许行为】
1. ✅ 修改函数内部实现逻辑（仅限问题代码行）
2. ✅ 添加或删除局部变量（仅限问题代码行）
3. ✅ 添加注释和文档字符串
4. ✅ 改进代码格式和缩进（符合 PEP8）

【禁止行为】
1. ❌ 大范围重构
2. ❌ 改变算法复杂度（除非问题明确要求）
3. ❌ 添加新的函数或类
4. ❌ 修改函数签名
=============================================================
"""
    return f"""
{OPTIMIZER_ROLE}
{conservative_rules}

已识别的代码问题（只处理高置信度问题）：
{review_issues}

待优化代码：
{code}

输出要求：
1. 必须使用 Markdown 代码块（```python ... ```）包裹优化后的代码。
2. 保留原代码的函数名、参数列表、返回值类型。
3. 仅修改存在问题的部分，其余代码保持不变。
4. 增加必要的注释说明优化点。
5. 确保代码可直接运行，无语法错误。
6. 不要输出任何额外的解释文本，只输出代码块。
"""
