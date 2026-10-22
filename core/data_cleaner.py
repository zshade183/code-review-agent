import ast
import re
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class DataCleaner:
    """数据清洗工具类 - 统一代码格式、移除无关内容、确保代码可独立运行"""
    
    def __init__(self):
        self.stats = {
            "total_processed": 0,
            "successful": 0,
            "failed": 0,
            "syntax_errors": 0,
            "empty_code": 0,
            "too_long": 0
        }
    
    def infer_category(self, sample: Dict) -> Tuple[str, str]:
        """
        从样本推断bug类别和子类别
        
        Args:
            sample: 原始样本
            
        Returns:
            (category, sub_category) 元组
        """
        description = sample.get('bug_description', '').lower()
        patch = sample.get('patch', '').lower()
        
        category = "Potential Bug"
        sub_category = "General Bug"
        
        if any(k in description or k in patch for k in ['sql', 'injection', 'xss', 'csrf', 'security', 'vulnerability', 'escape']):
            category = "Security Vulnerability"
            if 'sql' in patch:
                sub_category = "SQL Injection"
            elif 'xss' in description or 'escape' in patch:
                sub_category = "Cross-Site Scripting (XSS)"
            elif 'command' in patch:
                sub_category = "Command Injection"
            elif 'eval' in patch or 'exec' in patch:
                sub_category = "Dangerous Function Abuse"
            elif 'pickle' in patch or 'yaml.load' in patch:
                sub_category = "Insecure Deserialization"
            else:
                sub_category = "Security Vulnerability"
        elif any(k in description or k in patch for k in ['performance', 'optimize', 'slow', 'memory', 'leak', 'cache', 'complexity']):
            category = "Performance Issue"
            if 'loop' in patch:
                sub_category = "Inefficient Loop"
            elif 'cache' in patch:
                sub_category = "Redundant Calculation"
            elif 'memory' in description:
                sub_category = "Memory Leak"
            else:
                sub_category = "Performance Issue"
        elif any(k in description or k in patch for k in ['style', 'naming', 'unused', 'import', 'format', 'pep8', 'docstring']):
            category = "Code Smell"
            if 'unused' in patch or 'import' in patch:
                sub_category = "Unused Variable/Import"
            elif 'naming' in description:
                sub_category = "Non-Standard Naming"
            elif 'docstring' in patch:
                sub_category = "Missing Documentation"
            else:
                sub_category = "Code Smell"
        else:
            if 'index' in description or 'bounds' in description or 'out of' in description:
                sub_category = "Index Out of Bounds"
            elif 'null' in description or 'none' in description or 'check' in description:
                sub_category = "Null Pointer/None Check Missing"
            elif 'type' in description or 'typeerror' in description:
                sub_category = "Type Error"
            elif 'boundary' in description or 'condition' in description:
                sub_category = "Boundary Condition Error"
            elif 'logic' in description:
                sub_category = "Logical Condition Error"
            else:
                sub_category = "Potential Bug"
        
        return category, sub_category
    
    def extract_code_from_diff(self, diff_text: str) -> Tuple[Optional[str], Optional[str]]:
        """
        从git diff中提取buggy和fixed代码
        
        Args:
            diff_text: git diff格式的文本
            
        Returns:
            (buggy_code, fixed_code) 元组，提取失败返回 (None, None)
        """
        if not diff_text or not isinstance(diff_text, str):
            return None, None
        
        lines = diff_text.split('\n')
        buggy_lines = []
        fixed_lines = []
        in_hunk = False
        
        for line in lines:
            if line.startswith('diff --git'):
                in_hunk = False
                continue
            elif line.startswith('index '):
                continue
            elif line.startswith('--- '):
                continue
            elif line.startswith('+++ '):
                continue
            elif line.startswith('@@ '):
                in_hunk = True
                continue
            
            if in_hunk:
                if line.startswith('- '):
                    buggy_lines.append(line[2:])
                elif line.startswith('+ '):
                    fixed_lines.append(line[2:])
                elif line.startswith('-') and not line.startswith('--'):
                    buggy_lines.append(line[1:])
                elif line.startswith('+') and not line.startswith('++'):
                    fixed_lines.append(line[1:])
                elif line.startswith(' '):
                    buggy_lines.append(line[1:])
                    fixed_lines.append(line[1:])
                else:
                    buggy_lines.append(line)
                    fixed_lines.append(line)
        
        buggy_code = '\n'.join(buggy_lines).strip() if buggy_lines else None
        fixed_code = '\n'.join(fixed_lines).strip() if fixed_lines else None
        
        return buggy_code, fixed_code
    
    def clean_code(self, code: str) -> Optional[str]:
        """
        清理代码：移除无关内容、统一格式
        
        Args:
            code: 原始代码
            
        Returns:
            清理后的代码，如果代码无效返回None
        """
        if not code or not isinstance(code, str):
            return None
        
        code = code.strip()
        
        if not code or len(code) < 10:
            self.stats["empty_code"] += 1
            return None
        
        if len(code) > 50000:
            self.stats["too_long"] += 1
            return None
        
        code = self._remove_diff_markers(code)
        code = self._normalize_whitespace(code)
        code = self._remove_trailing_whitespace(code)
        
        return code
    
    def _remove_diff_markers(self, code: str) -> str:
        """移除git diff标记"""
        lines = code.split('\n')
        cleaned_lines = []
        
        for line in lines:
            if line.startswith('@@ ') or line.startswith('diff --git'):
                continue
            if line.startswith('--- ') or line.startswith('+++ '):
                continue
            if line.startswith('index '):
                continue
            cleaned_lines.append(line)
        
        return '\n'.join(cleaned_lines)
    
    def _normalize_whitespace(self, code: str) -> str:
        """规范化空白字符"""
        lines = code.split('\n')
        normalized_lines = []
        
        for line in lines:
            normalized_line = '\t'.join(line.split('\t'))
            normalized_line = normalized_line.rstrip()
            normalized_lines.append(normalized_line)
        
        return '\n'.join(normalized_lines)
    
    def _remove_trailing_whitespace(self, code: str) -> str:
        """移除行尾空白"""
        lines = code.split('\n')
        cleaned_lines = [line.rstrip() for line in lines]
        return '\n'.join(cleaned_lines)
    
    def validate_syntax(self, code: str, strict: bool = True) -> bool:
        """
        验证Python代码语法是否正确
        
        Args:
            code: Python代码
            strict: 是否严格验证（完整代码），False时允许代码片段
            
        Returns:
            语法是否正确
        """
        if not code:
            return False
        
        if not strict:
            return True
        
        try:
            ast.parse(code)
            return True
        except SyntaxError:
            self.stats["syntax_errors"] += 1
            return False
        except Exception:
            return False
    
    def extract_function_code(self, code: str, function_name: str) -> Optional[str]:
        """
        从代码中提取指定函数的代码
        
        Args:
            code: 完整代码
            function_name: 函数名
            
        Returns:
            函数代码，未找到返回None
        """
        try:
            tree = ast.parse(code)
            
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == function_name:
                    start_line = node.lineno
                    end_line = node.end_lineno if hasattr(node, 'end_lineno') else start_line
                    
                    lines = code.split('\n')
                    function_lines = lines[start_line - 1:end_line]
                    return '\n'.join(function_lines)
            
            return None
        except Exception:
            return None
    
    def make_code_standalone(self, code: str, required_imports: List[str] = None) -> str:
        """
        使代码可独立运行：添加必要的导入和辅助代码
        
        Args:
            code: 原始代码
            required_imports: 需要的导入列表
            
        Returns:
            可独立运行的代码
        """
        if not code:
            return code
        
        imports = []
        if required_imports:
            for imp in required_imports:
                if imp not in code:
                    imports.append(imp)
        
        if imports:
            code = '\n'.join(imports) + '\n\n' + code
        
        return code
    
    def process_sample(self, sample: Dict) -> Optional[Dict]:
        """
        处理单个样本：提取代码、清理、验证
        
        Args:
            sample: 原始样本数据
            
        Returns:
            处理后的样本，失败返回None
        """
        self.stats["total_processed"] += 1
        
        try:
            buggy_code = None
            fixed_code = None
            category = ""
            sub_category = ""
            
            if 'patch' in sample:
                buggy_code, fixed_code = self.extract_code_from_diff(sample['patch'])
                category, sub_category = self.infer_category(sample)
            elif 'buggy_code' in sample:
                buggy_code = self.clean_code(sample['buggy_code'])
                fixed_code = self.clean_code(sample.get('fixed_code', ''))
                category = sample.get('bug_category', '')
                sub_category = sample.get('bug_sub_category', '')
            elif 'code' in sample and isinstance(sample['code'], dict):
                code_dict = sample['code']
                if 'buggy' in code_dict:
                    buggy_code = self.clean_code(code_dict['buggy'])
                if 'fixed' in code_dict:
                    fixed_code = self.clean_code(code_dict['fixed'])
                category = sample.get('issue', {}).get('category', '')
                sub_category = sample.get('issue', {}).get('sub_category', '')
            
            if not buggy_code:
                self.stats["failed"] += 1
                return None
            
            is_from_patch = 'patch' in sample
            if not self.validate_syntax(buggy_code, strict=not is_from_patch):
                self.stats["failed"] += 1
                return None
            
            result = {
                'code_id': sample.get('code_id', sample.get('id', '')),
                'language': 'python',
                'source': sample.get('source', 'BugsInPy'),
                'project': sample.get('project', ''),
                'file_path': sample.get('file_path', ''),
                'issue': {
                    'type': 'bug',
                    'category': category,
                    'sub_category': sub_category,
                    'severity': 'medium',
                    'line': None,
                    'description': f"{category}: {sub_category}",
                    'cwe_id': None
                },
                'code': {
                    'buggy': buggy_code,
                    'fixed': fixed_code
                }
            }
            
            self.stats["successful"] += 1
            return result
            
        except Exception as e:
            logger.error(f"处理样本失败: {e}")
            self.stats["failed"] += 1
            return None
    
    def get_stats(self) -> Dict:
        """获取处理统计信息"""
        return self.stats.copy()
    
    def print_stats(self):
        """打印统计信息"""
        print("\n" + "=" * 50)
        print("数据清洗统计")
        print("=" * 50)
        print(f"总处理数: {self.stats['total_processed']}")
        print(f"成功: {self.stats['successful']}")
        print(f"失败: {self.stats['failed']}")
        print(f"语法错误: {self.stats['syntax_errors']}")
        print(f"空代码: {self.stats['empty_code']}")
        print(f"代码过长: {self.stats['too_long']}")
        if self.stats['total_processed'] > 0:
            success_rate = self.stats['successful'] / self.stats['total_processed'] * 100
            print(f"成功率: {success_rate:.2f}%")
        print("=" * 50 + "\n")
