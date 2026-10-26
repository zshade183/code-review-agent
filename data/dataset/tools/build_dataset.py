import json
import sys
from pathlib import Path
from typing import Dict, List
from collections import Counter
import logging

sys.path.append(str(Path(__file__).parent.parent))
from core.data_cleaner import DataCleaner

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class DatasetBuilder:
    """数据集构建器 - 批量处理和构建标准化数据集"""
    
    def __init__(self):
        self.cleaner = DataCleaner()
        self.processed_samples: List[Dict] = []
        self.category_stats = Counter()
        self.project_stats = Counter()
    
    def load_raw_data(self, input_path: Path) -> List[Dict]:
        """加载原始数据"""
        logger.info(f"加载数据: {input_path}")
        
        try:
            with open(input_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and 'samples' in data:
                return data['samples']
            else:
                logger.error(f"未知的数据格式")
                return []
        except Exception as e:
            logger.error(f"加载数据失败: {e}")
            return []
    
    def process_samples(self, samples: List[Dict]) -> List[Dict]:
        """批量处理样本"""
        logger.info(f"开始处理 {len(samples)} 个样本")
        
        processed = []
        for sample in samples:
            result = self.cleaner.process_sample(sample)
            if result:
                processed.append(result)
                category = result.get('issue', {}).get('category', '')
                self.category_stats[category] += 1
                self.project_stats[result['project']] += 1
        
        self.processed_samples = processed
        return processed
    
    def save_dataset(self, output_path: Path, samples: List[Dict] = None):
        """保存处理后的数据集"""
        if samples is None:
            samples = self.processed_samples
        
        logger.info(f"保存数据集: {output_path} ({len(samples)} 个样本)")
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(samples, f, ensure_ascii=False, indent=2)
        
        logger.info(f"数据集已保存")
    
    def save_jsonl(self, output_path: Path, samples: List[Dict] = None):
        """保存为JSONL格式（每行一个JSON对象）"""
        if samples is None:
            samples = self.processed_samples
        
        logger.info(f"保存JSONL数据集: {output_path} ({len(samples)} 个样本)")
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            for sample in samples:
                f.write(json.dumps(sample, ensure_ascii=False) + '\n')
        
        logger.info(f"JSONL数据集已保存")
    
    def split_dataset(self, samples: List[Dict] = None, 
                   train_ratio: float = 0.7, 
                   val_ratio: float = 0.2,
                   test_ratio: float = 0.1,
                   seed: int = 42) -> Dict[str, List[Dict]]:
        """划分数据集为训练/验证/测试集"""
        if samples is None:
            samples = self.processed_samples
        
        import random
        random.seed(seed)
        shuffled = samples.copy()
        random.shuffle(shuffled)
        
        total = len(shuffled)
        train_end = int(total * train_ratio)
        val_end = train_end + int(total * val_ratio)
        
        splits = {
            'train': shuffled[:train_end],
            'val': shuffled[train_end:val_end],
            'test': shuffled[val_end:]
        }
        
        logger.info(f"数据集划分: train={len(splits['train'])}, val={len(splits['val'])}, test={len(splits['test'])}")
        
        return splits
    
    def generate_stats(self, samples: List[Dict] = None) -> Dict:
        """生成数据集统计信息"""
        if samples is None:
            samples = self.processed_samples
        
        stats = {
            'total': len(samples),
            'by_category': dict(self.category_stats),
            'by_project': dict(self.project_stats),
            'avg_code_length': 0,
            'max_code_length': 0,
            'min_code_length': float('inf')
        }
        
        code_lengths = []
        for s in samples:
            code = s.get('code', {}).get('buggy', '')
            if code:
                code_lengths.append(len(code))
        
        if code_lengths:
            stats['avg_code_length'] = sum(code_lengths) / len(code_lengths)
            stats['max_code_length'] = max(code_lengths)
            stats['min_code_length'] = min(code_lengths)
        
        return stats
    
    def save_stats(self, output_path: Path, stats: Dict = None):
        """保存统计信息"""
        if stats is None:
            stats = self.generate_stats()
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        
        logger.info(f"统计信息已保存: {output_path}")
    
    def print_summary(self):
        """打印处理摘要"""
        self.cleaner.print_stats()
        
        print("\n" + "=" * 50)
        print("数据集统计")
        print("=" * 50)
        print(f"总样本数: {len(self.processed_samples)}")
        print(f"类别分布:")
        for category, count in self.category_stats.most_common():
            print(f"  {category}: {count}")
        print(f"项目分布 (Top 10):")
        for project, count in self.project_stats.most_common(10):
            print(f"  {project}: {count}")
        print("=" * 50 + "\n")


def build_dataset(input_path: Path, output_dir: Path):
    """构建完整数据集"""
    builder = DatasetBuilder()
    
    raw_samples = builder.load_raw_data(input_path)
    processed_samples = builder.process_samples(raw_samples)
    
    if not processed_samples:
        logger.error("没有成功处理的样本")
        return
    
    builder.print_summary()
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    builder.save_dataset(output_dir / 'dataset.json')
    builder.save_jsonl(output_dir / 'dataset.jsonl')
    
    splits = builder.split_dataset()
    builder.save_jsonl(output_dir / 'splits' / 'train.jsonl', splits['train'])
    builder.save_jsonl(output_dir / 'splits' / 'val.jsonl', splits['val'])
    builder.save_jsonl(output_dir / 'splits' / 'test.jsonl', splits['test'])
    
    stats = builder.generate_stats()
    builder.save_stats(output_dir / 'stats.json', stats)


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='构建代码审查基准数据集')
    parser.add_argument('--input', '-i', type=Path, required=True, help='输入数据文件路径')
    parser.add_argument('--output', '-o', type=Path, default=Path('data/processed'), help='输出目录')
    parser.add_argument('--stats-only', action='store_true', help='仅生成统计信息')
    
    args = parser.parse_args()
    
    if args.stats_only:
        builder = DatasetBuilder()
        samples = builder.load_raw_data(args.input)
        processed = builder.process_samples(samples)
        builder.print_summary()
        stats = builder.generate_stats(processed)
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        build_dataset(args.input, args.output)


if __name__ == '__main__':
    main()
