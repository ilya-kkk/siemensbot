from app.services.importer import parse_import_text


def test_parse_import_text_chat_id_and_username() -> None:
    users = parse_import_text("123456789,@SomeUser\n987654321 other_user")

    assert len(users) == 2
    assert users[0].chat_id == 123456789
    assert users[0].username == "SomeUser"
    assert users[0].status == "active"
    assert users[1].chat_id == 987654321


def test_parse_import_text_username_only_is_unresolved() -> None:
    users = parse_import_text("@legacy_user")

    assert len(users) == 1
    assert users[0].chat_id is None
    assert users[0].username == "legacy_user"
    assert users[0].status == "unresolved"


def test_parse_import_text_dedupes_by_chat_id() -> None:
    users = parse_import_text("123456789 @one_user\n123456789 @two_user")

    assert len(users) == 1
    assert users[0].username == "one_user"
