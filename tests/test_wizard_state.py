from __future__ import annotations

from sn_reg_bot.handlers.wizard_state import active_document_flow, active_text_flow, clear_other_wizards


class _FakeContext:
    def __init__(self) -> None:
        self.user_data: dict = {}


def test_stale_addorder_does_not_block_importwallet_text():
    ctx = _FakeContext()
    ctx.user_data["addorder"] = {"step": "wallet", "netuid": 15}
    ctx.user_data["importwallet"] = {"step": "name", "method": "file"}

    assert active_text_flow(ctx.user_data) == "importwallet"


def test_stale_addorder_hotkey_step_does_not_block_importwallet():
    ctx = _FakeContext()
    ctx.user_data["addorder"] = {"step": "hotkey", "netuid": 15, "coldkey": "mine"}
    ctx.user_data["importwallet"] = {"step": "mnemonic", "method": "mnemonic", "name": "mine"}

    assert active_text_flow(ctx.user_data) == "importwallet"


def test_clear_other_wizards_preserves_active_flow():
    ctx = _FakeContext()
    ctx.user_data["addorder"] = {"step": "wallet"}
    ctx.user_data["importwallet"] = {"step": "name"}

    clear_other_wizards(ctx, "importwallet")

    assert "addorder" not in ctx.user_data
    assert ctx.user_data["importwallet"]["step"] == "name"


def test_document_flow_only_when_await_file():
    ud = {"importwallet": {"step": "name", "method": "file"}}
    assert active_document_flow(ud) is None

    ud["importwallet"]["step"] = "await_file"
    assert active_document_flow(ud) == "importwallet"


def test_editorder_price_step_routes_text():
    ud = {"editorder": {"step": "price", "order_id": "abc"}}
    assert active_text_flow(ud) == "editorder"
