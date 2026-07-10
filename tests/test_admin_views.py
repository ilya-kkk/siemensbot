import pytest

from app.services.admin_views import parse_campaign_settings


def test_parse_campaign_settings_accepts_comma_and_spaces() -> None:
    assert parse_campaign_settings("100, 30") == (100, 30)
    assert parse_campaign_settings("100 30") == (100, 30)


def test_parse_campaign_settings_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        parse_campaign_settings("100")
    with pytest.raises(ValueError):
        parse_campaign_settings("0, 30")
