"""统一异常体系。"""


class KBError(Exception):
    """知识库基础异常。"""


class NotFoundError(KBError):
    """文档/资源不存在。"""


class IngestError(KBError):
    """入库失败（不支持类型、解析失败等）。"""


class LLMError(KBError):
    """LLM 调用失败。"""


class ConfigError(KBError):
    """配置错误。"""
