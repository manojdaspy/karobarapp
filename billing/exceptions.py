class InsufficientStock(Exception):
    def __init__(self, sku, available, requested):
        self.sku = sku
        self.available = available
        self.requested = requested
        super().__init__(f"{sku}: {available} available, {requested} requested")


class ReturnNotAllowed(Exception):
    pass


class ReturnQuantityExceeded(Exception):
    def __init__(self, sku, returnable, requested):
        self.sku = sku
        self.returnable = returnable
        self.requested = requested
        super().__init__(
            f"Cannot return {requested} of {sku}; only {returnable} available for return."
        )


class InvalidPaymentAmount(Exception):
    pass


class InvoiceCancellationNotAllowed(Exception):
    pass
