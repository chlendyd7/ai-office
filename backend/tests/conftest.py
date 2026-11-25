import pytest


@pytest.fixture(autouse=True)
def _never_touch_the_real_gateway(monkeypatch):
    """Importing the app loads backend/.env into os.environ (real credentials on a dev machine). Tests must not depend
    on, or spend money with, whatever happens to be configured there: strip it for every test. Tests that need a
    gateway pass their own env / a mock transport."""
    for name in ("H_CHAT_API_KEY", "H_CHAT_BASE_URL", "H_CHAT_PROJECT_ID", "H_CHAT_AUTH_PREFIX"):
        monkeypatch.delenv(name, raising=False)
