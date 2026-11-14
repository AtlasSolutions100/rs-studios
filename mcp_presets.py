"""Claude presets: ready-made asks for the Campaignify MCP (the /mcp page's gallery, the app's MCP page and the
server's prompts/list, so Claude shows each one as a slash command).

Each preset is one prompt a member can paste into Claude (or pick as /<name>): it names the product, the job and the
order of the tools, and leaves <angle brackets> where the member fills in their own words. Every preset that makes
something still goes through the tools' own rules: Claude prices first and waits for a yes before anything spends
credits; launching (where it's on) shows the approval card and creates everything paused.

    PRESETS      the catalogue, in gallery order
    TABS         the gallery's tabs (id, label)
    for_tab()    presets on one tab ("popular" = the marked ones)
    prompt_text  the text of one preset with the member's arguments filled in (prompts/get)

Example media are the site's own preset and showcase files (static/presets, static/showcase); a preset without one
shows its tab's colour block.
"""
from dataclasses import dataclass, field

TABS = [("popular", "Popular"), ("product", "Product showcase"), ("ugc", "UGC & creators"),
        ("apps", "Apps & SaaS"), ("variations", "Test variations"), ("launch", "Plan & launch"),
        ("research", "Research & strategy")]
TAB_LABEL = dict(TABS)


@dataclass(frozen=True)
class McpPreset:
    id: str                      # slash command name: lowercase, dashes
    title: str
    line: str                    # one line under the title
    prompt: str                  # what is pasted into Claude; <...> marks the member's own words
    tab: str
    tools: tuple = ()            # the MCP tools it uses, shown as tags
    example: str = ""            # /static/... image
    video: str = ""              # /static/... clip played on hover, if any
    popular: bool = False
    args: tuple = field(default_factory=tuple)   # (name, description, required) for prompts/get

    @property
    def cover(self) -> str:
        return self.example


_P = "/static/presets/"
_S = "/static/showcase/"
_PRODUCT = ("product", "Which product (name), if you have more than one", False)

PRESETS = [
    McpPreset(
        "product-launch-kit", "Product launch kit", "One product in, a ready-to-test launch campaign out.",
        "Build a launch kit for <my product>: read its approved claims, write 5 Meta ad variants, price 4 image ads "
        "(square and vertical) and one 15-second presenter video, and plan a 2-week test at <$ a day>. Show me every "
        "price before you make anything.",
        "product", ("get_product", "generate_ad_copy", "quote_ad_batch", "start_campaign_plan"),
        _P + "hero.webp", popular=True, args=(_PRODUCT,)),
    McpPreset(
        "talking-ugc-review", "Talking UGC review", "An AI presenter holds up your product and says why it's good.",
        "Make a 15-second selfie-style presenter video for <my product>. Pick a presenter and a setting that fit my "
        "audience, hold the product, and write the script from its approved claims. No customer testimonial wording. "
        "Quote it first.",
        "ugc", ("list_ad_options", "quote_ad_batch", "generate_video_ads"),
        _P + "talk-to-camera.webp", _P + "talk-to-camera.mp4", popular=True, args=(_PRODUCT,)),
    McpPreset(
        "hook-multiplier", "Hook multiplier", "Five different first lines, same ad, ready to A/B test.",
        "Write 5 scroll-stopping hooks for <my product> on <TikTok or Meta>, each a different angle (problem, "
        "curiosity, result, objection, POV). Then price one 8-second presenter video per hook so I can test them.",
        "variations", ("generate_ad_copy", "quote_ad_batch"),
        _P + "street-interview.webp", popular=True, args=(_PRODUCT,)),
    McpPreset(
        "app-demo-video", "App demo on a real screen", "A presenter shows your app or site, with your own screens.",
        "Make a presenter video showing <my app> on a laptop or phone. Use my product's screen recording or "
        "screenshots on the screen (not generated ones), and a 20-second script: the problem, what the app does, "
        "one call to action. Quote it first.",
        "apps", ("get_product", "list_ad_options", "quote_ad_batch", "generate_video_ads"),
        _P + "web-app-demo.webp", _P + "web-app-demo.mp4", popular=True, args=(_PRODUCT,)),
    McpPreset(
        "competitor-snapshot", "Competitor snapshot", "What your rivals sell, charge and say, with sources.",
        "Run a competitor snapshot on <competitor 1>, <competitor 2>, <competitor 3>. Then tell me the gap I should "
        "attack in my ads and write 3 hooks for it.",
        "research", ("quote_ai_tasks", "start_competitor_snapshot", "get_competitor_snapshot", "generate_ad_copy"),
        _P + "compare.webp", popular=True),
    McpPreset(
        "mr-campaign", "Ask Mr. Campaign", "A marketing consultant finds what's holding sales back.",
        "/consultant I sell <what you sell> to <who>. My goal this month is <goal>. What's the one thing I should fix "
        "first, and what are my next 3 moves?",
        "research", ("mr_campaign",), _P + "founder-story.webp", popular=True),
    # ---- product showcase
    McpPreset(
        "image-ad-set", "Image ad set", "Six on-brand image ads in every size, words spelled right.",
        "Price 6 image ads for <my product> using 3 different templates that fit it, in 1:1, 4:5 and 9:16, with the "
        "headline: <headline>. Make them after I say yes.",
        "product", ("list_ad_options", "quote_ad_batch", "generate_image_ads"), _P + "callouts.webp", args=(_PRODUCT,)),
    McpPreset(
        "product-turntable", "Product turntable", "A clean 10-second spin of your product photo.",
        "Make a 10-second product video of <my product> turning slowly on a clean surface, from its best photo, "
        "with sound. Quote it first.",
        "product", ("quote_ad_batch", "generate_video_ads"), _P + "turn.webp", _P + "turn.mp4", args=(_PRODUCT,)),
    McpPreset(
        "unboxing-pov", "POV unboxing", "Hands open the box and reveal your product.",
        "Make a POV unboxing video of <my product>: the package arrives, hands open it, the product is revealed. "
        "Use the unboxing preset and a 10-second length. Quote it first.",
        "product", ("list_ad_options", "quote_ad_batch", "generate_video_ads"),
        _P + "pov-unboxing.webp", _P + "pov-unboxing.mp4", args=(_PRODUCT,)),
    McpPreset(
        "seasonal-promo", "Seasonal promo", "A holiday or season version of your best ad.",
        "Make <season or holiday> image ads for <my product> with the seasonal templates, in square and vertical. "
        "Only use a discount if I give you one: <offer or none>.",
        "product", ("list_ad_options", "quote_ad_batch", "generate_image_ads"),
        _P + "season-winter-gift.webp", args=(_PRODUCT,)),
    # ---- UGC & creators
    McpPreset(
        "fashion-try-on", "Fashion try-on", "A presenter tries on your piece and shows the fit.",
        "Make a try-on video for <my clothing item>: a presenter who fits my customer shows the fit, the fabric and "
        "how they'd style it, 15 seconds, vertical. Quote it first.",
        "ugc", ("list_ad_options", "quote_ad_batch", "generate_video_ads"),
        _P + "try-on.webp", _P + "try-on.mp4", args=(_PRODUCT,)),
    McpPreset(
        "three-presenters", "Three presenters, one script", "The same script said by three different people.",
        "Price one 15-second video of this script said by 3 different presenters (different ages and settings): "
        "<script>. Make them after I say yes.",
        "ugc", ("list_ad_options", "quote_ad_batch", "generate_video_ads"), _P + "street.webp"),
    McpPreset(
        "car-talk", "Car talk", "A casual 'I had to tell you' video from the driver's seat.",
        "Make a 15-second car-talk presenter video for <my product> in a parked car, casual and quick, with one clear "
        "call to action. Quote it first.",
        "ugc", ("list_ad_options", "quote_ad_batch", "generate_video_ads"), _P + "car-talk.webp", args=(_PRODUCT,)),
    McpPreset(
        "voiceover-product", "Voiceover product video", "Your product shots with a narrator over them.",
        "Make a 15-second voiceover video for <my product> from its photos, with a warm narrator and this message: "
        "<message>. Quote it first.",
        "product", ("list_ad_options", "quote_ad_batch", "generate_video_ads"),
        _P + "vo-product-shots.webp", _P + "vo-product-shots.mp4", args=(_PRODUCT,)),
    # ---- apps & SaaS
    McpPreset(
        "saas-explainer", "SaaS explainer", "Problem, product, proof in 20 seconds.",
        "Write a 20-second explainer script for <my software> (problem, how it works in one line, one result, call to "
        "action), then price it as a presenter video at a desk and as a voiceover over my screenshots.",
        "apps", ("generate_ad_copy", "quote_ad_batch"), _P + "how-it-works.webp", args=(_PRODUCT,)),
    McpPreset(
        "app-store-images", "App store and social stills", "Phone mockups with your real screens and a headline.",
        "Price 4 image ads for <my app> that show my own screenshots on a phone, with these headlines: <headline 1>, "
        "<headline 2>. Square and vertical.",
        "apps", ("get_product", "quote_ad_batch", "generate_image_ads"), _P + "app-screen.webp", args=(_PRODUCT,)),
    # ---- test variations
    McpPreset(
        "angle-test", "Three-angle test", "Three different reasons to buy, one ad each.",
        "Find 3 different angles for <my product> (a problem it solves, a moment it fits, a comparison), write a "
        "headline for each and price one image ad per angle in 4:5 and 9:16.",
        "variations", ("get_product", "generate_ad_copy", "quote_ad_batch"), _P + "three-reasons.webp", args=(_PRODUCT,)),
    McpPreset(
        "copy-pack", "Copy pack for every platform", "Meta, TikTok and Google copy sized to each.",
        "Write ad copy for <my product> for Meta (5 variants), TikTok captions (5) and Google Search headlines (5), "
        "each within that platform's limits.",
        "variations", ("generate_ad_copy",), _P + "clean-headline.webp", args=(_PRODUCT,)),
    # ---- plan & launch
    McpPreset(
        "two-week-test", "Two-week test plan", "Budget split, ads to make and a day-by-day plan.",
        "Plan a 2-week test for <my product> at <$ a day> on <Meta and TikTok>: angles, the ads to make, the budget "
        "split and what to check each day. Show me the cap before you start.",
        "launch", ("quote_ai_tasks", "start_campaign_plan", "get_campaign_plan"), _P + "how-to-steps.webp",
        args=(_PRODUCT,)),
    McpPreset(
        "launch-on-meta", "Launch on Meta, paused", "Your plan becomes a paused campaign in your ad account.",
        "Walk me through launching my latest campaign plan on Meta: load the Meta playbook, check my connected ad "
        "account, show me the approval card, and only create it (paused) after I approve.",
        "launch", ("get_ad_playbook", "list_ad_connections", "prepare_launch", "launch_campaign"),
        _P + "offer-promo.webp"),
    McpPreset(
        "google-search-setup", "Google Search setup", "Step-by-step setup with nothing missed.",
        "Walk me through setting up Google Search ads for <my product>, step by step, using the Google Search "
        "playbook. Write the headlines and descriptions for me too.",
        "launch", ("get_ad_playbook", "generate_ad_copy"), _P + "problem-answer.webp", args=(_PRODUCT,)),
    # ---- research & strategy
    McpPreset(
        "offer-fix", "Fix my offer", "Mr. Campaign tightens what you sell and how you say it.",
        "/consultant Look at my offer for <my product> and tell me how to make it an offer people feel stupid saying "
        "no to. Then write 3 headlines for the new offer.",
        "research", ("mr_campaign", "generate_ad_copy"), _P + "offer.webp", args=(_PRODUCT,)),
    McpPreset(
        "weekly-refresh", "Weekly creative refresh", "New ads for whatever's getting tired.",
        "List what I made this week, then suggest 4 new ads that are genuinely different (new hook, new format, new "
        "presenter) and price them.",
        "variations", ("list_assets", "list_ad_options", "quote_ad_batch"), _P + "swap.webp"),
]

BY_ID = {p.id: p for p in PRESETS}


def for_tab(tab: str) -> list:
    if tab == "popular":
        return [p for p in PRESETS if p.popular]
    return [p for p in PRESETS if p.tab == tab]


def tabs_in_use() -> list:
    return [(t, label) for t, label in TABS if for_tab(t)]


def prompt_text(preset: McpPreset, args: dict | None = None) -> str:
    """The preset's prompt for prompts/get. A filled `product` argument replaces "<my product>" (and the like)."""
    text = preset.prompt
    prod = ((args or {}).get("product") or "").strip()
    if prod:
        for ph in ("<my product>", "<my app>", "<my software>", "<my clothing item>"):
            text = text.replace(ph, prod)
    return text


def as_dicts() -> list:
    return [{"id": p.id, "title": p.title, "line": p.line, "prompt": p.prompt, "tab": p.tab,
             "tab_label": TAB_LABEL[p.tab], "tools": list(p.tools), "image": p.example, "video": p.video,
             "popular": p.popular} for p in PRESETS]
