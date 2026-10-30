"""Deterministic synthetic benchmark v2: 24 single, 12 multi, 14 clean cases."""
import ast
import hashlib
import json
from collections import Counter
from pathlib import Path

OUT = Path(__file__).with_name('eval.json')
# category, subtype, requirement, buggy source, fixed source, anchor, explanation
CASES = [
('security','SQL Injection','name 来自 HTTP 请求；cursor 是 sqlite3 游标。', 'def find_user(cursor, name):\n    return cursor.execute("SELECT id FROM users WHERE name = \'" + name + "\'").fetchall()', 'def find_user(cursor, name):\n    return cursor.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchall()', 'return cursor', '不可信字符串被拼接进 SQL。'),
('security','Command Injection','host 来自请求；调用系统 nslookup。', 'import subprocess\ndef lookup(host):\n    return subprocess.run("nslookup " + host, shell=True, capture_output=True)', 'import subprocess\ndef lookup(host):\n    return subprocess.run(["nslookup", host], capture_output=True)', 'return subprocess', 'shell 会解释用户输入中的命令分隔符。'),
('security','Insecure Deserialization','payload 是客户端上传的 UTF-8 JSON 字节，应解析为对象。', 'import pickle\ndef decode(payload):\n    return pickle.loads(payload)', 'import json\ndef decode(payload):\n    return json.loads(payload.decode("utf-8"))', 'return pickle', '不可信 pickle 可以触发任意代码执行。'),
('security','Dangerous Function Abuse','expression 来自请求，协议只允许 JSON 数值或数组。', 'def parse_expression(expression):\n    return eval(expression)', 'import json\ndef parse_expression(expression):\n    return json.loads(expression)', 'return eval', 'eval 会执行输入中的 Python 表达式。'),
('security','Cross-Site Scripting (XSS)','name 来自请求，返回值直接作为 HTML 响应正文。', 'def greet(name):\n    return "<h1>" + name + "</h1>"', 'import html\ndef greet(name):\n    return "<h1>" + html.escape(name) + "</h1>"', 'return ', '未转义输入可注入 HTML 与脚本。'),
('security','Hardcoded Sensitive Information','客户端需要生产服务的认证令牌；应从部署环境读取。', 'def service_headers():\n    token = "prod-token-example-123456"\n    return {"Authorization": "Bearer " + token}', 'import os\ndef service_headers():\n    return {"Authorization": "Bearer " + os.environ["SERVICE_TOKEN"]}', 'token =', '认证令牌硬编码进源码，无法安全部署和轮换。'),
('bug','Division by Zero','values 为数值列表；空列表平均值约定返回 0。', 'def average(values):\n    return sum(values) / len(values)', 'def average(values):\n    return sum(values) / len(values) if values else 0', 'return ', '空列表会导致除零。'),
('bug','Index Out of Bounds','items 为非空列表，返回最后一项。', 'def last(items):\n    return items[len(items)]', 'def last(items):\n    return items[-1]', 'return ', '长度不是合法下标。'),
('bug','Null Pointer/None Check Missing','profile 是字典或 None；没有 profile 时返回 None。', 'def display_name(profile):\n    return profile.get("name")', 'def display_name(profile):\n    return (profile or {}).get("name")', 'return ', 'None 没有 get 方法。'),
('bug','Boundary Condition Error','18 岁及以上为成年人；age 为非负整数。', 'def is_adult(age):\n    return age > 18', 'def is_adult(age):\n    return age >= 18', 'return ', '18 岁被错误排除。'),
('bug','Return Value Error','返回摄氏温度对应的华氏温度，允许浮点值。', 'def fahrenheit(celsius):\n    return celsius * 9 / 5 - 32', 'def fahrenheit(celsius):\n    return celsius * 9 / 5 + 32', 'return ', '转换公式常数符号错误。'),
('bug','Logical Condition Error','仅管理员或资源所有者可以访问；参数均为布尔值。', 'def may_read(is_admin, is_owner):\n    return is_admin and is_owner', 'def may_read(is_admin, is_owner):\n    return is_admin or is_owner', 'return ', '错误要求两个授权条件同时成立。'),
('smell','Unused Variable/Import','计算圆面积，radius 为非负数；无导入副作用需求。', 'import math\nimport statistics\ndef area(radius):\n    return math.pi * radius ** 2', 'import math\ndef area(radius):\n    return math.pi * radius ** 2', 'import statistics', 'statistics 从未使用。'),
('smell','Improper Exception Handling','operation 无参数；仅 ValueError 应返回 None，其余错误必须传播。', 'def attempt(operation):\n    try:\n        return operation()\n    except Exception:\n        return None', 'def attempt(operation):\n    try:\n        return operation()\n    except ValueError:\n        return None', 'except Exception', '过宽捕获掩盖应传播的程序错误。'),
('smell','Redundant Code/Dead Code','value 为字符串，去掉首尾空白。', 'def normalize(value):\n    return value.strip()\n    value = "unused"', 'def normalize(value):\n    return value.strip()', 'value =', 'return 后的语句永远不可达。'),
('smell','Unreleased Resource','path 指向存在的 UTF-8 文件；读取首行，每次调用必须关闭句柄。', 'def first_line(path):\n    stream = open(path, encoding="utf-8")\n    return stream.readline()', 'def first_line(path):\n    with open(path, encoding="utf-8") as stream:\n        return stream.readline()', 'stream =', '文件句柄未可靠关闭。'),
('smell','Too Many Parameters','项目约定配置函数最多 5 个位置参数；将独立配置项封装为映射。', 'def settings(host, port, user, password, database, timeout, retries):\n    return dict(host=host, port=port, user=user, password=password, database=database, timeout=timeout, retries=retries)', 'def settings(options):\n    return dict(options)', 'def settings', '七个位置参数违反项目明确接口约定。'),
('smell','Duplicate Code','两个公开函数都需把字符串列表去空白转小写；项目要求共享同一实现。', 'def clean_names(names):\n    return [name.strip().lower() for name in names]\ndef clean_tags(tags):\n    return [tag.strip().lower() for tag in tags]', 'def clean_names(names):\n    return [name.strip().lower() for name in names]\ndef clean_tags(tags):\n    return clean_names(tags)', 'return [tag', '两处相同变换独立维护，违反共享实现约定。'),
('performance','Data Structure Misuse','items 为大量可哈希整数，保序去重；目标平均 O(n)。', 'def unique(items):\n    result = []\n    for item in items:\n        if item not in result:\n            result.append(item)\n    return result', 'def unique(items):\n    return list(dict.fromkeys(items))', 'if item', '列表反复查重在全部不同元素时为 O(n²)。'),
('performance','Redundant Calculation','values 是大型整数列表；返回每项乘总和的列表。', 'def weighted(values):\n    return [value * sum(values) for value in values]', 'def weighted(values):\n    total = sum(values)\n    return [value * total for value in values]', 'return [', '每个元素都重新计算相同总和。'),
('performance','Redundant I/O/Database Query','ids 可含重复整数；fetch 是无副作用的昂贵查询，结果在调用期间稳定。', 'def load_many(ids, fetch):\n    return [fetch(key) for key in ids]', 'def load_many(ids, fetch):\n    cache = {key: fetch(key) for key in dict.fromkeys(ids)}\n    return [cache[key] for key in ids]', 'return [', '重复 ID 导致重复昂贵查询。'),
('performance','Unnecessary Deep Copy','records 为大型嵌套 JSON 列表；只读且只需获得记录条数。', 'import copy\ndef count_records(records):\n    snapshot = copy.deepcopy(records)\n    return len(snapshot)', 'def count_records(records):\n    return len(records)', 'snapshot =', '只读取长度却深拷贝全部嵌套对象。'),
('performance','Inefficient Loop','items 为大型可排序整数列表；仅返回最大值，空列表返回 None；目标 O(n)。', 'def largest(items):\n    return sorted(items)[-1] if items else None', 'def largest(items):\n    return max(items) if items else None', 'return ', '仅求最大值却排序，复杂度 O(n log n)。'),
('performance','Redundant Calculation','values 为大型浮点列表，转换器 transform 是纯函数且计算昂贵；保留转换后正值。', 'def positive_transforms(values, transform):\n    return [transform(value) for value in values if transform(value) > 0]', 'def positive_transforms(values, transform):\n    converted = map(transform, values)\n    return [value for value in converted if value > 0]', 'return [', '通过过滤的元素被重复转换。'),
]
CATEGORIES = dict(security='Security Vulnerability', bug='Potential Bug', smell='Code Smell', performance='Performance Issue')


def make_row(index, specs, clean=False):
    requirements = '\n'.join(f'{i+1}. {s[2]}' for i, s in enumerate(specs))
    # Requirements travel with source so all three arms see identical context.
    header = '\n'.join('# ' + line for line in requirements.splitlines()) + '\n'
    code = header
    issues = []
    for spec in specs:
        category, subtype, _, buggy, fixed, anchor, explanation = spec
        block = fixed if clean else buggy
        offset = len(code.splitlines())
        if not clean:
            line = next(i for i, text in enumerate(block.splitlines(), 1) if anchor in text) + offset
            issues.append(dict(category=CATEGORIES[category], subtype=subtype,
                               severity='High' if category == 'security' else 'Medium',
                               description=explanation, location=dict(line_start=line, line_end=line),
                               fix_suggestion='参见 fixed_code 中对应函数的修复。'))
        code += block + '\n\n'
    return dict(id=f'EVAL-V2-{index:03d}', language='Python',
                sample_type='clean' if clean else 'single' if len(specs) == 1 else 'multi',
                provenance='synthetic-curated-v2', requirements=requirements,
                code=code.rstrip()+'\n',
                fixed_code=(header+'\n\n'.join(s[4] for s in specs)+'\n'), issues=issues)


def build():
    rows = [make_row(i+1, [s]) for i, s in enumerate(CASES)]
    pairs = [(i, 6+i) for i in range(6)] + [(12+i, 18+i) for i in range(6)]
    rows += [make_row(25+i, [CASES[a], CASES[b]]) for i, (a,b) in enumerate(pairs)]
    rows += [make_row(37+i, [CASES[j]], clean=True)
             for i,j in enumerate([0,1,2,3,4,6,7,8,9,13,15,18,20,23])]
    return rows


def audit(rows):
    assert len(rows) == 50
    assert len({r['id'] for r in rows}) == 50
    assert len({ast.dump(ast.parse(r['code'])) for r in rows}) == 50
    types = Counter(r['sample_type'] for r in rows)
    assert types == dict(single=24, multi=12, clean=14), types
    categories = Counter()
    for row in rows:
        assert row['requirements'] and '\ufffd' not in json.dumps(row, ensure_ascii=False)
        ast.parse(row['fixed_code'])
        for item in row['issues']:
            start, end = item['location'].values()
            assert 1 <= start <= end <= len(row['code'].splitlines())
            assert row['code'].splitlines()[start-1].strip()
            categories[item['category']] += 1
        assert (row['code'] == row['fixed_code']) == (row['sample_type'] == 'clean')
    assert set(categories.values()) == {12}, categories
    return dict(version=2, samples=50, sample_types=dict(types), issues=48,
                categories=dict(categories), sha256=hashlib.sha256(
                    json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest())


if __name__ == '__main__':
    rows = build()
    result = audit(rows)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    OUT.with_name('audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
