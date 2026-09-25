KEYWORD = 'dolcebot'


def eligible_rate_plans(rate_plans):
    """Rate plans the direct-booking modal may offer."""
    return [rp for rp in rate_plans if KEYWORD in rp['title'].lower()]
