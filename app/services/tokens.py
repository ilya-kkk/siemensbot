import secrets


def make_link_token() -> str:
    return secrets.token_urlsafe(24)
