"""Readiness of the ChatGPT subscription sign-in behind cloud-gpt-sol (ADR 0005).

LiteLLM's chatgpt provider falls back to an interactive device-code login when
it has no token or a refresh fails. Inside the gateway that would hold the
request for minutes and print the code to the journal, so the gateway refuses
instead and the login stays an explicit operator step (agentiac chatgpt login).
"""

NOT_SIGNED_IN = 'ChatGPT is not signed in; run agentiac chatgpt login'
EXPIRED = 'ChatGPT sign-in expired; run agentiac chatgpt login'


def signin_problem(auth, refresh=True):
    """None when AUTH (a LiteLLM chatgpt Authenticator) has a usable token, else a short reason.

    With REFRESH an expired access token is refreshed here; without it a stored
    refresh token is enough. The reason never contains token text.
    """
    data = auth._read_auth_file()
    if not data or not data.get('refresh_token'):
        return NOT_SIGNED_IN
    if not refresh or not auth._is_token_expired(data, data.get('access_token') or ''):
        return None
    try:
        auth._refresh_tokens(data['refresh_token'])
    except Exception:
        return EXPIRED
    return None
