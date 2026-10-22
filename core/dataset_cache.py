import hashlib
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional


class DatasetCache:
    """数据集测试专用缓存系统"""
    
    def __init__(self, cache_dir: str = "cache", max_size: int = 10000, ttl: int = 7200):
        """
        初始化缓存系统
        
        Args:
            cache_dir: 缓存目录
            max_size: 内存缓存最大条目数
            ttl: 缓存过期时间（秒）
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        
        self.memory_cache = OrderedDict()
        self.max_size = max_size
        self.ttl = ttl
        
        self.stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0
        }
    
    def _get_key(self, code: str, task_type: str, params: Optional[Dict] = None) -> str:
        """生成缓存键"""
        content = f"{task_type}:{code}"
        if params:
            content += f":{json.dumps(params, sort_keys=True)}"
        return hashlib.sha256(content.encode()).hexdigest()
    
    def _get_cache_file(self, key: str) -> Path:
        """获取缓存文件路径"""
        return self.cache_dir / f"{key}.json"
    
    def get(self, code: str, task_type: str, params: Optional[Dict] = None) -> Optional[Any]:
        """
        从缓存获取结果
        
        Args:
            code: 代码内容
            task_type: 任务类型（review/optimize等）
            params: 额外参数（如提示词策略）
        
        Returns:
            缓存的结果，如果不存在或已过期则返回None
        """
        key = self._get_key(code, task_type, params)
        
        # 检查内存缓存
        if key in self.memory_cache:
            result, timestamp = self.memory_cache[key]
            if time.time() - timestamp < self.ttl:
                self.stats["hits"] += 1
                self.memory_cache.move_to_end(key)
                return result
            else:
                del self.memory_cache[key]
        
        # 检查磁盘缓存
        cache_file = self._get_cache_file(key)
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 检查是否过期
                if time.time() - data.get("timestamp", 0) < self.ttl:
                    self.stats["hits"] += 1
                    # 加载到内存缓存
                    self._set_memory(key, data["result"])
                    return data["result"]
                else:
                    # 过期则删除
                    cache_file.unlink()
            except Exception:
                pass
        
        self.stats["misses"] += 1
        return None
    
    def set(self, code: str, task_type: str, result: Any, params: Optional[Dict] = None) -> None:
        """
        将结果存入缓存
        
        Args:
            code: 代码内容
            task_type: 任务类型
            result: 处理结果
            params: 额外参数
        """
        key = self._get_key(code, task_type, params)
        
        # 存入内存缓存
        self._set_memory(key, result)
        
        # 存入磁盘缓存
        cache_file = self._get_cache_file(key)
        try:
            data = {
                "result": result,
                "timestamp": time.time(),
                "task_type": task_type
            }
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    
    def _set_memory(self, key: str, result: Any) -> None:
        """存入内存缓存"""
        if len(self.memory_cache) >= self.max_size:
            # 删除最旧的缓存
            self.memory_cache.popitem(last=False)
            self.stats["evictions"] += 1
        self.memory_cache[key] = (result, time.time())
    
    def clear(self) -> None:
        """清空所有缓存"""
        self.memory_cache.clear()
        for cache_file in self.cache_dir.glob("*.json"):
            cache_file.unlink()
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        total_requests = self.stats["hits"] + self.stats["misses"]
        hit_rate = self.stats["hits"] / total_requests if total_requests > 0 else 0
        
        return {
            **self.stats,
            "hit_rate": hit_rate,
            "memory_cache_size": len(self.memory_cache),
            "disk_cache_size": len(list(self.cache_dir.glob("*.json")))
        }
    
    def print_stats(self) -> None:
        """打印缓存统计信息"""
        stats = self.get_stats()
        print("\n" + "=" * 50)
        print("缓存统计信息")
        print("=" * 50)
        print(f"缓存命中: {stats['hits']}")
        print(f"缓存未命中: {stats['misses']}")
        print(f"缓存淘汰: {stats['evictions']}")
        print(f"命中率: {stats['hit_rate']:.2%}")
        print(f"内存缓存大小: {stats['memory_cache_size']}")
        print(f"磁盘缓存大小: {stats['disk_cache_size']}")
        print("=" * 50 + "\n")
