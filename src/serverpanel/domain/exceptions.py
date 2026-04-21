"""Domain exceptions hierarchy."""


class ServerPanelError(Exception):
    """Base exception for all ServerPanel errors."""


class AuthError(ServerPanelError):
    """Authentication/authorization errors."""


class InvalidCredentialsError(AuthError):
    """Wrong email or password."""


class ProviderError(ServerPanelError):
    """Errors from server provider APIs."""

    def __init__(self, message: str, provider: str = "", status_code: int | None = None):
        self.provider = provider
        self.status_code = status_code
        super().__init__(message)


class ProviderAuthError(ProviderError):
    """Invalid provider credentials."""


class ProviderNotFoundError(ProviderError):
    """Resource not found at provider."""


class ProviderRateLimitError(ProviderError):
    """Rate limit exceeded."""


class SSHError(ServerPanelError):
    """SSH connection or command errors."""


class SSHConnectionError(SSHError):
    """Cannot connect via SSH."""


class SSHCommandError(SSHError):
    """Command execution failed."""

    def __init__(self, message: str, exit_code: int = -1, stderr: str = ""):
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(message)


class NotFoundError(ServerPanelError):
    """Resource not found in database."""


class EncryptionError(ServerPanelError):
    """Credential encryption/decryption errors."""
