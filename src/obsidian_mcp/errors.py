"""可向 agent 返回的错误，不包含凭证或原始 HTTP 响应。"""


class ObsidianError(Exception):
    """可恢复的业务、连接或兼容性错误。"""


class ConflictError(ObsidianError):
    """版本已变化，必须重新读取后决定如何修改。"""


class NotFoundError(ObsidianError):
    """请求的文件、目录或接口不存在。"""
