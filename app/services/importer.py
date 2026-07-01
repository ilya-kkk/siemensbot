import io
import re
from csv import Error, Sniffer, reader
from dataclasses import dataclass

CHAT_ID_RE = re.compile(r"(?<!\d)-?\d{5,20}(?!\d)")
USERNAME_RE = re.compile(r"@?([A-Za-z][A-Za-z0-9_]{4,31})")


@dataclass(frozen=True)
class ImportedUser:
    chat_id: int | None
    username: str | None
    status: str
    source: str = "old_import"

    @property
    def username_normalized(self) -> str | None:
        if not self.username:
            return None
        return self.username.strip().lstrip("@").lower()


def _clean_username(value: str | None) -> str | None:
    if not value:
        return None
    match = USERNAME_RE.search(value.strip())
    if not match:
        return None
    return match.group(1)


def _parse_row(values: list[str]) -> ImportedUser | None:
    joined = " ".join(v.strip() for v in values if v and v.strip())
    if not joined:
        return None

    chat_id_match = CHAT_ID_RE.search(joined)
    username = _clean_username(joined)
    chat_id = int(chat_id_match.group(0)) if chat_id_match else None

    if chat_id is None and username is None:
        return None

    return ImportedUser(
        chat_id=chat_id,
        username=username,
        status="active" if chat_id is not None else "unresolved",
    )


def parse_import_text(content: str) -> list[ImportedUser]:
    rows: list[list[str]] = []
    sample = content[:2048]
    has_csv_shape = "," in sample or ";" in sample or "\t" in sample

    if has_csv_shape:
        try:
            dialect = Sniffer().sniff(sample, delimiters=",;\t")
            csv_reader = reader(io.StringIO(content), dialect)
            for row in csv_reader:
                lowered = {cell.strip().lower() for cell in row}
                if {"chat_id", "username"} & lowered:
                    continue
                rows.append(row)
        except Error:
            rows = [[line] for line in content.splitlines()]
    else:
        rows = [[line] for line in content.splitlines()]

    deduped: dict[str, ImportedUser] = {}
    for row in rows:
        parsed = _parse_row(row)
        if not parsed:
            continue
        key = f"chat:{parsed.chat_id}" if parsed.chat_id is not None else f"user:{parsed.username_normalized}"
        if key and key not in deduped:
            deduped[key] = parsed

    return list(deduped.values())
