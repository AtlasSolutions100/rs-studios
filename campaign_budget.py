"""The planner's budget as a number with a currency and a period (UX-6, roadmap M5.4).

The brief used to take free text ("e.g. $300 for the first two weeks") and the plan printed it bare ("Budget 600").
Now the form takes an amount, a currency, whether it is the total for the test or per day, and the test length in
days. The brief keeps the structured fields (budget_amount, budget_currency, budget_period, budget_days) plus the
same "budget" text it always had, written from them, so the planner prompt and older code read it unchanged. Plans
made before this keep their free text and show it as typed.
"""

CURRENCIES = {"USD": "$", "CAD": "CA$", "AUD": "A$", "EUR": "€", "GBP": "£"}
PERIODS = {"total": "total for the test", "day": "per day"}
DEFAULT_DAYS = 14
MAX_AMOUNT = 10_000_000
MAX_DAYS = 365


def money(amount: float, currency: str = "USD") -> str:
    """$1,200 or $12.50: thousands separators, cents only when there are any."""
    sym = CURRENCIES.get(currency, "$")
    amount = float(amount)
    if abs(amount - round(amount)) < 0.005:
        return f"{sym}{int(round(amount)):,}"
    return f"{sym}{amount:,.2f}"


def parse(form) -> tuple[dict, str | None]:
    """The budget fields from the planner form: ({fields for the brief}, error or None). An empty amount is no
    budget (the planner assumes a small first test, as before)."""
    raw = (form.get("budget_amount") or "").strip().replace(",", "").lstrip("$€£")
    if not raw:
        return {}, None
    try:
        amount = float(raw)
    except ValueError:
        return {}, "Budget: enter a number, like 300."
    if not 0 < amount <= MAX_AMOUNT or amount != amount:
        return {}, "Budget: enter an amount above 0."
    currency = form.get("budget_currency") if form.get("budget_currency") in CURRENCIES else "USD"
    period = form.get("budget_period") if form.get("budget_period") in PERIODS else "total"
    try:
        days = int((form.get("budget_days") or "").strip() or DEFAULT_DAYS)
    except ValueError:
        return {}, "Test length: enter a number of days, like 14."
    if not 1 <= days <= MAX_DAYS:
        return {}, f"Test length: 1 to {MAX_DAYS} days."
    amount = round(amount, 2)
    fields = {"budget_amount": int(amount) if amount == int(amount) else amount, "budget_currency": currency,
              "budget_period": period, "budget_days": days}
    fields["budget"] = text(fields)
    return fields, None


def structured(brief: dict) -> bool:
    return isinstance(brief.get("budget_amount"), (int, float)) and brief.get("budget_amount") > 0


def total(brief: dict) -> float | None:
    """The whole test's budget, or None when the brief has no structured budget."""
    if not structured(brief):
        return None
    a, days = float(brief["budget_amount"]), int(brief.get("budget_days") or DEFAULT_DAYS)
    return a * days if brief.get("budget_period") == "day" else a


def text(brief: dict) -> str:
    """"$600 total over 14 days" or "$40 a day for 14 days ($560 total)"; a legacy brief's text as typed."""
    if not structured(brief):
        return (brief.get("budget") or "").strip()
    cur = brief.get("budget_currency") or "USD"
    days = int(brief.get("budget_days") or DEFAULT_DAYS)
    dword = f"{days} day" + ("" if days == 1 else "s")
    if brief.get("budget_period") == "day":
        return f"{money(brief['budget_amount'], cur)} a day for {dword} ({money(total(brief), cur)} total)"
    return f"{money(brief['budget_amount'], cur)} total over {dword}"


def share(brief: dict, percent) -> str | None:
    """One platform's part of the budget in money: "$360" (or "$24 a day"), or None without a structured budget."""
    if not structured(brief):
        return None
    try:
        p = float(percent)
    except (TypeError, ValueError):
        return None
    cur = brief.get("budget_currency") or "USD"
    if brief.get("budget_period") == "day":
        return f"{money(float(brief['budget_amount']) * p / 100, cur)} a day"
    return money(float(brief["budget_amount"]) * p / 100, cur)


GOAL_PLATFORMS = {
    # UX-7: the starting platform chips by goal (north star: product path Meta then Google, service path Google
    # Search plus Meta). TikTok stays one click away.
    "sales": ("meta", "google"),
    "leads": ("google", "meta"),
    "traffic": ("meta", "google"),
    "awareness": ("meta", "youtube"),
    "engagement": ("meta", "tiktok"),
    "installs": ("meta", "google"),
}


def platforms_for(goal: str) -> tuple:
    return GOAL_PLATFORMS.get((goal or "sales").lower(), GOAL_PLATFORMS["sales"])
