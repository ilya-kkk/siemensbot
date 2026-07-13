from types import SimpleNamespace

import pytest

from app import main


class _RedirectRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def record_offer_click(self, token: str) -> bool:
        self.calls.append(("click", token))
        return True

    async def get_app_config(self, default_offer_url: str) -> dict[str, str]:
        self.calls.append(("config", default_offer_url))
        return {"offer_url": "https://current.example/form"}


@pytest.mark.asyncio
async def test_redirect_records_click_then_reads_current_global_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RedirectRepository()
    monkeypatch.setattr(main, "AppRepository", lambda _session: repository)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(test_drive_url="https://legacy.example/form"),
    )

    response = await main.redirect_link("tracked-token", session=object())  # type: ignore[arg-type]

    assert response.status_code == 302
    assert response.headers["location"] == "https://current.example/form"
    assert repository.calls == [
        ("click", "tracked-token"),
        ("config", "https://legacy.example/form"),
    ]
