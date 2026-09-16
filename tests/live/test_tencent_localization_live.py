import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("TENCENT_LIVE_TEST") != "1",
    reason="set TENCENT_LIVE_TEST=1 only after explicit quota authorization",
)

def test_live_environment_is_explicit_and_credentialed():
    assert os.environ.get("TENCENTCLOUD_SECRET_ID")
    assert os.environ.get("TENCENTCLOUD_SECRET_KEY")
