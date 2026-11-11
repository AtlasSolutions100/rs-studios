"""Lines of public copy with two wordings (public_site.py, public_pages.py): one while sign-up is free, one while
PLANS_ONLY is on (plan_gate.py: no free tier, a 3-day trial on Starter and Growth). Kept apart from both modules so
either can use it."""


def plans_only() -> bool:
    import plan_gate
    return plan_gate.on()


class P(str):
    """A whole line with two wordings, for the few that public_site.SWITCHED's phrases can't express (a page title,
    an h1, a description built on "no trial"): the first while sign-up is free, the second while PLANS_ONLY is on.
    It is the first wording wherever a str is used; format() chooses by the switch."""

    def __new__(cls, free, paid):
        o = super().__new__(cls, free)
        o.paid = paid
        return o

    def format(self, *a, **kw):
        return str.format(self.paid if plans_only() else self, *a, **kw)
