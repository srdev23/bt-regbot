from types import SimpleNamespace

from sn_reg_bot.handlers.edit_order import _order_button_label
from sn_reg_bot.models.orders import SubnetRegistrationParams


def _order(coldkey: str, hotkey: str, *, order_id: str = "abcdef12-xxxx"):
    params = SubnetRegistrationParams(15, coldkey, hotkey, 0.55)
    return SimpleNamespace(id=order_id, params=params)


def test_editorder_button_includes_wallet_and_hotkey():
    label = _order_button_label(_order("default", "hotkey27"))
    assert "default/hotkey27" in label
    assert "abcdef12" in label
    assert "SN15" in label
    assert "0.5500" in label
    assert len(label) <= 64


def test_editorder_button_truncates_long_wallet_names():
    label = _order_button_label(
        _order("very_long_coldkey_name_here", "very_long_hotkey_name_here")
    )
    assert "abcdef12" in label
    assert "SN15" in label
    assert len(label) <= 64
