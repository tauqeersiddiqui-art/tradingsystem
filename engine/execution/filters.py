# execution/filters.py


def has_oi_wall(option_chain, atm_strike, direction):
    """
    Avoid trading directly into heavy Open Interest (OI) walls.

    Parameters
    ----------
    option_chain : list[dict]
        Option chain rows with keys:
        strike, ce_oi, pe_oi

    atm_strike : float
        Current ATM strike

    direction : str
        "CE" or "PE"

    Returns
    -------
    bool
        True  → OI wall detected, block trade
        False → Safe to trade
    """

    try:

        if not option_chain:
            return False

        # Get closest strikes around ATM
        strikes = sorted(
            option_chain,
            key=lambda x: abs(x.get("strike", 0) - atm_strike)
        )[:5]

        for s in strikes:

            ce_oi = s.get("ce_oi", 0)
            pe_oi = s.get("pe_oi", 0)

            # Block CE if heavy CALL OI wall exists
            if direction == "CE" and ce_oi > 6_000_000:
                return True

            if direction == "PE" and pe_oi > 6_000_000:
                return True

        return False

    except Exception:
        return False