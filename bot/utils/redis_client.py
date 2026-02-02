#!/usr/bin/env python3
"""
Redis 客户端工具 - 用于日志持久化、缓存等
"""

import os
import json
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

# 尝试导入 redis
try:
    import redis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False
    logger.warning("redis 模块未安装，日志持久化将不可用")


class RedisClient:
    """Redis 客户端封装"""
    
    def __init__(self, url: str = None):
        self.url = url or os.environ.get('REDIS_URL', '')
        self._client: Optional[redis.Redis] = None
        self._connected = False
        
        if self.url and HAS_REDIS:
            self._connect()
    
    def _connect(self):
        """连接 Redis"""
        try:
            self._client = redis.from_url(self.url, decode_responses=True)
            self._client.ping()
            self._connected = True
            logger.info(f"Redis 连接成功: {self.url}")
        except Exception as e:
            logger.warning(f"Redis 连接失败: {e}")
            self._connected = False
    
    @property
    def connected(self) -> bool:
        return self._connected and self._client is not None
    
    # ========== 日志存储 ==========
    
    def add_log(self, level: str, message: str, module: str = ""):
        """添加日志条目"""
        if not self.connected:
            return
        
        try:
            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "level": level,
                "module": module,
                "message": message
            }
            # 使用 list 存储日志，最新的在前面
            self._client.rpush("bot:logs", json.dumps(log_entry, ensure_ascii=False))
            # 保留最近 1000 条
            self._client.ltrim("bot:logs", 0, 999)
        except Exception as e:
            logger.debug(f"添加日志到 Redis 失败: {e}")
    
    def get_logs(self, limit: int = 100, level: str = None) -> List[Dict]:
        """获取日志"""
        if not self.connected:
            return []
        
        try:
            logs_raw = self._client.lrange("bot:logs", -limit, -1)
            logs = [json.loads(log) for log in logs_raw]
            
            if level:
                logs = [log for log in logs if log.get("level", "").upper() == level.upper()]
            
            return logs
        except Exception as e:
            logger.debug(f"从 Redis 获取日志失败: {e}")
            return []
    
    def clear_logs(self):
        """清空日志"""
        if not self.connected:
            return
        
        try:
            self._client.delete("bot:logs")
        except:
            pass
    
    # ========== 缓存 ==========
    
    def cache_set(self, key: str, value: Any, expire: int = 300):
        """设置缓存"""
        if not self.connected:
            return
        
        try:
            self._client.setex(f"cache:{key}", expire, json.dumps(value, ensure_ascii=False))
        except:
            pass
    
    def cache_get(self, key: str) -> Optional[Any]:
        """获取缓存"""
        if not self.connected:
            return None
        
        try:
            data = self._client.get(f"cache:{key}")
            return json.loads(data) if data else None
        except:
            return None
    
    # ========== 统计计数 ==========
    
    def incr_stat(self, name: str, amount: int = 1):
        """增加统计计数"""
        if not self.connected:
            return
        
        try:
            self._client.incrby(f"stat:{name}", amount)
        except:
            pass
    
    def get_stat(self, name: str) -> int:
        """获取统计计数"""
        if not self.connected:
            return 0
        
        try:
            val = self._client.get(f"stat:{name}")
            return int(val) if val else 0
        except:
            return 0


# 全局实例
_redis_client: Optional[RedisClient] = None


def get_redis() -> RedisClient:
    """获取 Redis 客户端实例"""
    global _redis_client
    if _redis_client is None:
        _redis_client = RedisClient()
    return _redis_client


class RedisLogHandler(logging.Handler):
    """将日志发送到 Redis 的 Handler"""
    
    def emit(self, record):
        try:
            client = get_redis()
            if client.connected:
                msg = self.format(record)
                client.add_log(
                    level=record.levelname,
                    message=msg,
                    module=record.name
                )
        except:
            pass
