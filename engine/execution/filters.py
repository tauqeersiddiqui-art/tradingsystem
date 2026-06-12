# execution/filters.py


def has_oi_wall(option_chain, atm_strike, direction):

    try:

        if not option_chain:
            return False

        nearby = sorted(
            option_chain,
            key=lambda x: abs(x.get("strike", 0) - atm_strike)
        )[:5]

        avg_ce = sum(s.get("ce_oi", 0) for s in nearby) / max(len(nearby), 1)
        avg_pe = sum(s.get("pe_oi", 0) for s in nearby) / max(len(nearby), 1)

        for s in nearby:

            strike = s.get("strike", 0)
            ce_oi = s.get("ce_oi", 0)
            pe_oi = s.get("pe_oi", 0)

            if direction == "CE":

                if strike > atm_strike and ce_oi > avg_ce * 2:
                    return True

            elif direction == "PE":

                if strike < atm_strike and pe_oi > avg_pe * 2:
                    return True

        return False

    except Exception:
        return False