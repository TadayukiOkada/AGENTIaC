"""Management API errors: code, message, request id, retryable and allowlisted details.

No traceback, argv, env or raw secret value is returned.
"""
class ApiError(Exception):
    def __init__(self, status, code, message, retryable=False, details=None):
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = list(details or [])

    def to_dict(self, request_id):
        return {'code': self.code, 'message': self.message, 'request_id': request_id,
                'retryable': self.retryable, 'details': self.details}
