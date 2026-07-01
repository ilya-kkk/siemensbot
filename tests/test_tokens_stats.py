from app.services.stats import aggregate_campaign_stats
from app.services.tokens import make_link_token


def test_link_tokens_are_unique() -> None:
    tokens = {make_link_token() for _ in range(100)}

    assert len(tokens) == 100


def test_aggregate_campaign_stats() -> None:
    stats = aggregate_campaign_stats(
        [
            {"status": "sent", "user_status": "active", "clicked": True, "ai_cost": 0.1},
            {"status": "pending", "user_status": "active", "clicked": False, "ai_cost": 0.2},
            {"status": "failed", "user_status": "blocked", "clicked": False, "ai_cost": 0},
            {"status": "failed", "user_status": "invalid", "clicked": False, "ai_cost": 0},
        ]
    )

    assert stats.total == 4
    assert stats.sent == 1
    assert stats.pending == 1
    assert stats.failed == 2
    assert stats.blocked == 1
    assert stats.invalid == 1
    assert stats.clicked == 1
    assert stats.sent_percent == 25.0
    assert round(stats.ai_cost, 2) == 0.3
