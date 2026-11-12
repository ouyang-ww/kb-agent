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


class StudyDisabledError(KBError):
    """学习模块关闭。核心摄入、检索、问答不依赖该模块。"""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or (
                "学习模块已关闭（study.enabled=off）。"
                "摄入、检索和问答不受影响。开启：kb study enable"
            )
        )
