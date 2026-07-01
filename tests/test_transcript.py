from app.services.transcript import render_dialogue_html, split_telegram_html


def test_render_dialogue_html_escapes_user_text() -> None:
    html = render_dialogue_html(
        [
            {
                "created_at": "2026-07-01 12:00",
                "direction": "incoming",
                "text": "<script>alert(1)</script>",
            }
        ]
    )

    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_split_telegram_html_keeps_chunks_under_limit() -> None:
    text = "\n\n".join(f"block {i} " + "x" * 20 for i in range(20))
    chunks = split_telegram_html(text, limit=100)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
