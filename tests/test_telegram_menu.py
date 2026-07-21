import re

from sn_reg_bot.telegram_menu import BOT_MENU_COMMANDS


def test_menu_commands_are_valid_for_telegram():
    seen: set[str] = set()
    for cmd in BOT_MENU_COMMANDS:
        assert cmd.command not in seen, f"duplicate command: {cmd.command}"
        seen.add(cmd.command)
        assert re.fullmatch(r"[a-z0-9_]{1,32}", cmd.command)
        assert 3 <= len(cmd.description) <= 256


def test_menu_includes_core_commands():
    names = {c.command for c in BOT_MENU_COMMANDS}
    assert "start" in names
    assert "addorder" in names
    assert "editorder" in names
    assert "wallets" in names
    assert "address" in names
    assert "removewallet" in names
    assert "importhotkey" in names
