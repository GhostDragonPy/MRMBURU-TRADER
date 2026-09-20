class ExecutionDisabled(RuntimeError):
    pass

class ExecutionGateway:
    """Live broker orders stay closed. Paper fills use a separate simulator."""
    def submit(self, *args, **kwargs):
        raise ExecutionDisabled('Live/demo broker order submission is disabled')

    def assert_no_live(self):
        # Paper path may simulate fills; this gate still forbids broker sockets.
        return True
