from datetime import UTC, datetime

from app.services.leads import LEADS_CSV_COLUMNS, render_leads_csv


def test_render_leads_csv_uses_expected_columns() -> None:
    csv_bytes = render_leads_csv(
        [
            {
                "id": 7,
                "created_at": datetime(2026, 7, 1, 12, 30, tzinfo=UTC),
                "telegram_id": 123,
                "chat_id": 456,
                "username": "client",
                "name": "Анна",
                "niche": "услуги",
                "main_problem": None,
                "average_check": "15000",
                "revenue_estimate": "300000",
                "sales_volume": "20",
            }
        ]
    )

    content = csv_bytes.decode("utf-8-sig")
    lines = content.splitlines()

    assert lines[0] == ",".join(header for _, header in LEADS_CSV_COLUMNS)
    assert "id" not in lines[0]
    assert "created_at" not in lines[0]
    assert "telegram_id" not in lines[0]
    assert "chat_id" not in lines[0]
    assert "Юзернейм,Имя,Имя в Telegram" in lines[0]
    assert "Анна" in lines[1]
    assert ",," in lines[1]
