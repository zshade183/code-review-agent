import json
import sys
from pathlib import Path
from typing import Dict, List
from collections import Counter

def convert_to_eval_format(input_path: Path, output_path: Path):
    """
    将processed数据集转换为eval_dataset兼容格式
    
    Args:
        input_path: 输入文件路径（JSONL格式）
        output_path: 输出文件路径
    """
    print(f"读取数据集: {input_path}")
    
    samples = []
    category_stats = Counter()
    
    with open(input_path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            
            try:
                sample = json.loads(line)
                
                converted = {
                    'id': sample.get('code_id', ''),
                    'language': sample.get('language', 'python'),
                    'code': sample.get('code', {}).get('buggy', ''),
                    'fixed_code': sample.get('code', {}).get('fixed', ''),
                    'issues': [
                        {
                            'category': sample.get('issue', {}).get('category', ''),
                            'subtype': sample.get('issue', {}).get('sub_category', ''),
                            'severity': sample.get('issue', {}).get('severity', 'medium'),
                            'description': sample.get('issue', {}).get('description', ''),
                            'location': {
                                'line_start': 1,
                                'line_end': 1
                            }
                        }
                    ]
                }
                
                samples.append(converted)
                category_stats[converted['issues'][0]['category']] += 1
                
            except Exception as e:
                print(f"转换样本失败: {e}")
                continue
    
    print(f"成功转换 {len(samples)} 个样本")
    print(f"类别分布: {dict(category_stats)}")
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(samples, f, ensure_ascii=False, indent=2)
    
    print(f"已保存到: {output_path}")

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='转换数据集格式')
    parser.add_argument('--input', '-i', type=Path, required=True, help='输入文件路径')
    parser.add_argument('--output', '-o', type=Path, default=Path('data/eval_dataset.json'), help='输出文件路径')
    
    args = parser.parse_args()
    
    convert_to_eval_format(args.input, args.output)

if __name__ == '__main__':
    main()
