import concurrent.futures
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from tqdm import tqdm

from core.dataset_cache import DatasetCache


@dataclass
class ProcessResult:
    """处理结果"""
    sample_id: str
    success: bool
    result: Any
    error: Optional[str]
    processing_time: float


class DatasetProcessor:
    """数据集并发处理器"""
    
    def __init__(
        self,
        max_workers: int = 4,
        enable_cache: bool = True,
        cache_ttl: int = 7200
    ):
        """
        初始化处理器
        
        Args:
            max_workers: 最大并发工作线程数
            enable_cache: 是否启用缓存
            cache_ttl: 缓存过期时间（秒）
        """
        self.max_workers = max_workers
        self.enable_cache = enable_cache
        
        if enable_cache:
            self.cache = DatasetCache(ttl=cache_ttl)
        else:
            self.cache = None
        
        self.stats = {
            "total_samples": 0,
            "successful": 0,
            "failed": 0,
            "total_time": 0.0
        }
    
    def process_sample(
        self,
        sample: Any,
        process_func: Callable,
        task_type: str,
        params: Optional[Dict] = None
    ) -> ProcessResult:
        """
        处理单个样本
        
        Args:
            sample: 样本数据
            process_func: 处理函数
            task_type: 任务类型
            params: 额外参数
        
        Returns:
            处理结果
        """
        sample_id = getattr(sample, 'sample_id', 'unknown')
        code = getattr(sample, 'code', '')
        
        start_time = time.time()
        
        try:
            # 检查缓存
            if self.cache:
                cached_result = self.cache.get(code, task_type, params)
                if cached_result is not None:
                    return ProcessResult(
                        sample_id=sample_id,
                        success=True,
                        result=cached_result,
                        error=None,
                        processing_time=time.time() - start_time
                    )
            
            # 执行处理
            result = process_func(sample, params)
            
            # 存入缓存
            if self.cache:
                self.cache.set(code, task_type, result, params)
            
            return ProcessResult(
                sample_id=sample_id,
                success=True,
                result=result,
                error=None,
                processing_time=time.time() - start_time
            )
            
        except Exception as e:
            return ProcessResult(
                sample_id=sample_id,
                success=False,
                result=None,
                error=str(e),
                processing_time=time.time() - start_time
            )
    
    def process_batch(
        self,
        samples: List[Any],
        process_func: Callable,
        task_type: str,
        params: Optional[Dict] = None,
        show_progress: bool = True
    ) -> List[ProcessResult]:
        """
        批量处理样本
        
        Args:
            samples: 样本列表
            process_func: 处理函数
            task_type: 任务类型
            params: 额外参数
            show_progress: 是否显示进度条
        
        Returns:
            处理结果列表
        """
        self.stats["total_samples"] = len(samples)
        results = []
        
        # 创建进度条
        pbar = tqdm(total=len(samples), desc=f"处理 {task_type}") if show_progress else None
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务
            future_to_sample = {
                executor.submit(
                    self.process_sample,
                    sample,
                    process_func,
                    task_type,
                    params
                ): sample for sample in samples
            }
            
            # 收集结果
            for future in concurrent.futures.as_completed(future_to_sample):
                try:
                    result = future.result()
                    results.append(result)
                    
                    # 更新统计
                    if result.success:
                        self.stats["successful"] += 1
                    else:
                        self.stats["failed"] += 1
                    self.stats["total_time"] += result.processing_time
                    
                except Exception as e:
                    print(f"处理样本时发生错误: {e}")
                
                # 更新进度条
                if pbar:
                    pbar.update(1)
        
        if pbar:
            pbar.close()
        
        return results
    
    def process_batch_sequential(
        self,
        samples: List[Any],
        process_func: Callable,
        task_type: str,
        params: Optional[Dict] = None,
        show_progress: bool = True
    ) -> List[ProcessResult]:
        """
        顺序批量处理样本（用于调试或单线程场景）
        
        Args:
            samples: 样本列表
            process_func: 处理函数
            task_type: 任务类型
            params: 额外参数
            show_progress: 是否显示进度条
        
        Returns:
            处理结果列表
        """
        self.stats["total_samples"] = len(samples)
        results = []
        
        pbar = tqdm(total=len(samples), desc=f"处理 {task_type}") if show_progress else None
        
        for sample in samples:
            result = self.process_sample(sample, process_func, task_type, params)
            results.append(result)
            
            if result.success:
                self.stats["successful"] += 1
            else:
                self.stats["failed"] += 1
            self.stats["total_time"] += result.processing_time
            
            if pbar:
                pbar.update(1)
        
        if pbar:
            pbar.close()
        
        return results
    
    def get_stats(self) -> Dict[str, Any]:
        """获取处理统计信息"""
        total_samples = self.stats["total_samples"]
        success_rate = self.stats["successful"] / total_samples if total_samples > 0 else 0
        avg_time = self.stats["total_time"] / total_samples if total_samples > 0 else 0
        
        return {
            **self.stats,
            "success_rate": success_rate,
            "avg_processing_time": avg_time
        }
    
    def print_stats(self) -> None:
        """打印统计信息"""
        stats = self.get_stats()
        
        print("\n" + "=" * 50)
        print("处理统计信息")
        print("=" * 50)
        print(f"总样本数: {stats['total_samples']}")
        print(f"成功: {stats['successful']}")
        print(f"失败: {stats['failed']}")
        print(f"成功率: {stats['success_rate']:.2%}")
        print(f"总处理时间: {stats['total_time']:.2f}秒")
        print(f"平均处理时间: {stats['avg_processing_time']:.2f}秒")
        
        if self.cache:
            self.cache.print_stats()
        
        print("=" * 50 + "\n")
