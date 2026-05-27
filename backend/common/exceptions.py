class BusinessException(Exception):
    """Raise from routers to emit a standard failure envelope.

    Example:
        raise BusinessException("STORE_NOT_FOUND", "존재하지 않는 매장입니다.", status=404)
    """

    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)
