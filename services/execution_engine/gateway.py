class ExecutionDisabled(RuntimeError):
    pass

class ExecutionGateway:
    """Closed boundary until phase 9. No broker client, credentials or sockets."""
    def submit(self, *args, **kwargs):
        raise ExecutionDisabled('v0.2 supports research only; all order execution is disabled')
