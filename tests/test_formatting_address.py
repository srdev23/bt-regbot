from sn_reg_bot.formatting import fmt_explorer_link, fmt_ss58


def test_fmt_ss58_copyable():
    assert fmt_ss58(None) == "Address: unavailable"
    assert (
        fmt_ss58("5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty")
        == "Address: <code>5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty</code>"
    )


def test_fmt_explorer_link():
    assert fmt_explorer_link(None) is None
    link = fmt_explorer_link("5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty")
    assert "taostats.io/account/5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty" in link
