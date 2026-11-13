"""T056: the ledger's money arithmetic (research R7), as properties over every amount and fee.

All amounts are integer cents. platform = round_half_up(amount × fee_pct / 100); the seller's
share is the remainder, so the two always add up to what the buyer paid, with no rounding gap.

Contract first (Ticket 3.2): `app.ledger` arrives with T068 (Ticket 3.5). Until then each test is
a strict xfail; when T068 makes one pass, CI fails until its marker is removed.
"""

from decimal import ROUND_HALF_UP, Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

PENDING = pytest.mark.xfail(reason="T068 (Ticket 3.5): app.ledger not built yet", strict=True)

amounts = st.integers(min_value=100, max_value=1_000_000)
# fee_pct is numeric(5,2) in Orders (T061): 0 to 100 with two decimals.
fee_pcts = st.decimals(min_value=0, max_value=100, places=2, allow_nan=False, allow_infinity=False)


def expected_fee(amount: int, fee_pct: Decimal) -> int:
    return int((Decimal(amount) * fee_pct / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@PENDING
@given(amount=amounts, fee_pct=fee_pcts)
def test_platform_fee_rounds_half_up(amount, fee_pct):
    from app.ledger import platform_fee

    assert platform_fee(amount, fee_pct) == expected_fee(amount, fee_pct)


@PENDING
@given(amount=amounts, fee_pct=fee_pcts)
def test_the_two_shares_always_sum_to_the_amount(amount, fee_pct):
    from app.ledger import split

    platform, seller = split(amount, fee_pct)
    assert platform + seller == amount
    assert platform == expected_fee(amount, fee_pct)
    assert 0 <= platform <= amount and 0 <= seller <= amount


@PENDING
@pytest.mark.parametrize(
    ("amount", "fee_pct", "platform"),
    [
        (2500, Decimal("10.00"), 250),  # the Sprint 3 spike's charge
        (105, Decimal("10.00"), 11),  # 10.5 rounds half up, not to even
        (125, Decimal("10.00"), 13),  # 12.5
        (100, Decimal("0.00"), 0),
        (100, Decimal("100.00"), 100),
        (999, Decimal("12.50"), 125),  # 124.875
    ],
)
def test_known_values(amount, fee_pct, platform):
    from app.ledger import split

    assert split(amount, fee_pct) == (platform, amount - platform)


@PENDING
@given(amount=amounts, fee_pct=fee_pcts)
def test_a_refund_is_the_exact_negative_of_the_sale(amount, fee_pct):
    from app.ledger import refund_entries, sale_entries

    sale = sale_entries(amount, fee_pct)
    refund = refund_entries(amount, fee_pct)
    assert {(e.party, -e.amount_cents) for e in sale} == {(e.party, e.amount_cents) for e in refund}
    assert sum(e.amount_cents for e in sale) + sum(e.amount_cents for e in refund) == 0
