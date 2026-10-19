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


def test_get_user_pages_exchanges_long_lived_token_first():
    for cls in (FacebookProvider, InstagramProvider):
        provider = cls({"client_id": "a", "client_secret": "b"})
        exchange = MagicMock()
        exchange.json.return_value = {"access_token": "largo", "expires_in": 5184000}
        pages = MagicMock()
        pages.json.return_value = {"data": []}
        with patch.object(provider, "_request", side_effect=[exchange, pages]) as req:
            provider.get_user_pages("corto")
        first, second = req.call_args_list
        assert first.kwargs["params"]["grant_type"] == "fb_exchange_token"
        assert first.kwargs["params"]["fb_exchange_token"] == "corto"
        assert second.args[1].endswith("/me/accounts")
        assert second.kwargs["access_token"] == "largo"
