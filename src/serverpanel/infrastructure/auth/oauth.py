"""OAuth adapters — stubs for future Google/GitHub integration."""

# Will be implemented when OAuth dependencies are added.
# Architecture: each OAuth provider is a class with:
#   - get_authorization_url() -> str
#   - handle_callback(code: str) -> OAuthUserInfo
#   - OAuthUserInfo has: email, display_name, provider, provider_user_id
