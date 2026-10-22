import json
import os
import re
from typing import Dict, List

# Mapping of CodeFuse categories to current project categories
CATEGORY_MAPPING = {
    "安全漏洞": "Security Vulnerability",
    "功能bug": "Potential Bug",
    "代码坏味道": "Code Smell",
    "性能问题": "Performance Issue"
}

# Subcategory mappings
SUBCATEGORY_MAPPING = {
    # Security Vulnerability subcategories
    "SQL注入": "SQL Injection",
    "命令注入": "Command Injection",
    "XSS": "XSS",
    "硬编码敏感信息": "Hardcoded Sensitive Information",
    # Potential Bug subcategories
    "空指针": "Null Pointer",
    "边界错误": "Boundary Error",
    "索引越界": "Index Out of Bounds",
    "逻辑错误": "Logical Error",
    # Code Smell subcategories
    "未使用变量/导入": "Unused Variable/Import",
    "魔法数字": "Magic Number",
    "重复代码": "Duplicate Code",
    "过长函数": "Long Function",
    # Performance Issue subcategories
    "低效循环": "Inefficient Loop",
    "冗余计算": "Redundant Calculation",
    "数据结构误用": "Data Structure Misuse"
}

def transform_sample(sample: Dict) -> Dict:
    """Transform a CodeFuse-CR-Bench sample to the current dataset format"""
    # Extract category and subcategory
    category = sample.get("category", "")
    sub_category = sample.get("sub_category", "")
    
    # Map to current project categories
    mapped_category = CATEGORY_MAPPING.get(category, "Potential Bug")
    mapped_sub_category = SUBCATEGORY_MAPPING.get(sub_category, sub_category)
    
    # Extract code
    buggy_code = sample.get("buggy_code", "")
    fixed_code = sample.get("fixed_code", "")
    
    # Extract project information
    project = sample.get("project", "unknown")
    file_path = sample.get("file_path", "")
    
    # Generate unique ID
    sample_id = f"codefuse_{project}_{sample.get('id', 'unknown')}"
    
    return {
        "id": sample_id,
        "project": project,
        "bug_id": sample.get("id", "unknown"),
        "bug_category": mapped_category,
        "bug_sub_category": mapped_sub_category,
        "buggy_code": buggy_code,
        "fixed_code": fixed_code,
        "file_path": file_path,
        "source": "CodeFuse-CR-Bench"
    }

def transform_dataset(input_file: str, output_file: str) -> None:
    """Transform the CodeFuse-CR-Bench dataset"""
    # Read input file
    with open(input_file, 'r', encoding='utf-8') as f:
        samples = json.load(f)
    
    # Transform samples
    transformed_samples = []
    for sample in samples:
        # Filter by supported categories
        category = sample.get("category", "")
        if category in CATEGORY_MAPPING:
            transformed = transform_sample(sample)
            transformed_samples.append(transformed)
    
    # Save output file
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(transformed_samples, f, ensure_ascii=False, indent=2)
    
    print(f"Transformed {len(transformed_samples)} samples out of {len(samples)}")
    print(f"Saved to {output_file}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Transform CodeFuse-CR-Bench dataset")
    parser.add_argument("--input", required=True, help="Input JSON file")
    parser.add_argument("--output", required=True, help="Output JSON file")
    
    args = parser.parse_args()
    transform_dataset(args.input, args.output)
