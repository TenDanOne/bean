from unittest.mock import MagicMock, patch

from providers.facebook import FacebookProvider
from providers.instagram import InstagramProvider


def test_get_user_id_reads_me_id():
    for cls in (FacebookProvider, InstagramProvider):
        provider = cls({"client_id": "a", "client_secret": "b"})
        resp = MagicMock()
        resp.json.return_value = {"id": 4242}
        with patch.object(provider, "_request", return_value=resp) as req:
            assert provider.get_user_id("tok") == "4242"
        assert req.call_args.args[1].endswith("/me")
