from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class CampaignStats:
    total: int
    sent: int
    pending: int
    failed: int
    blocked: int
    invalid: int
    clicked: int
    ai_cost: float

    @property
    def sent_percent(self) -> float:
        return round(self.sent / self.total * 100, 2) if self.total else 0.0


def aggregate_campaign_stats(rows: list[dict]) -> CampaignStats:
    counts = Counter(row.get("status") for row in rows)
    return CampaignStats(
        total=len(rows),
        sent=counts["sent"],
        pending=counts["pending"] + counts["processing"] + counts["rescheduled"],
        failed=counts["failed"],
        blocked=sum(1 for row in rows if row.get("user_status") == "blocked"),
        invalid=sum(1 for row in rows if row.get("user_status") == "invalid"),
        clicked=sum(1 for row in rows if row.get("clicked")),
        ai_cost=float(sum(row.get("ai_cost") or 0 for row in rows)),
    )
