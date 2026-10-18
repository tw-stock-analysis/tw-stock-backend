class ServiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class MarketDataUnavailable(ServiceError):
    def __init__(self):
        super().__init__(
            "MARKET_DATA_UNAVAILABLE",
            "Market data is not available. Run the official data import.",
            503,
        )
