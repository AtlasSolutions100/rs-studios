"""The presenter roster: who can say a script, where they can be, and what holds them the same from video to video (m15).

Every presenter is an invented, AI-generated person, described here and nowhere copied from: no real person,
no likeness, no celebrity. A presenter is

    id, name            a slug and a first name
    age_band, gender    for the picker's filters
    look                one line: what the video model is told today (the eight older presenters keep theirs word
                        for word, so their videos are what they were)
    appearance          the detailed description the still is drawn from, written so it fits nobody in particular
    voice               ONE voice: a descriptor the talking-video prompt can say today, plus a voice_ref slot (a short
                        recording in static/presenters/<id>/voice.mp3) for the audio reference
    situations          three or four places they can be (SITUATIONS), each with its own wardrobe and a still

Identity (the owner's paid test, Nov 2026): a still of a presenter given to the video model as its reference image
held the face in a new scene, and lip-sync from that still was good. So a presenter's identity is one approved still
per situation, plus one voice. The stills are made by a site admin through the app's own pipeline (library.py),
reviewed, exported and committed under static/presenters/ (tools/import_library_assets.py):

    static/presenters/<id>/portrait.webp          the picker's card
    static/presenters/<id>/<situation>.webp       the approved still of that situation (9:16)
    static/presenters/<id>/voice.mp3              a few seconds of their voice
    static/presenters/<id>/angle-left.webp        the look-lock pictures (ANGLES): the same person turned to their
    static/presenters/<id>/angle-right.webp       left, to their right and in profile, on a plain wall. They are never
    static/presenters/<id>/angle-profile.webp     shown to members; they only help the video model hold the face

roster() and get() return a url for each only when the file is there, so the front end shows what exists.

Three switches, all off by default (off = exactly what a talking video sent before):

    PRESENTER_STILLS      when a presenter + situation has an approved still, the worker passes it to the video model
                          as the first reference image and the prompt says to keep that person
    PRESENTER_VOICE_REF   when the presenter has a voice.mp3, the worker passes it as an audio reference (the vendor's
                          reference-to-video takes audio_urls) and the prompt says to speak in that voice. What the
                          vendor does with an audio reference is not documented: it needs a paid test before it is
                          switched on (README, "m15 library").
    PRESENTER_ANGLES      with PRESENTER_STILLS on and a still in use: the presenter's look-lock pictures that exist go
                          to the video model after the still, and the prompt says they show the same person from
                          other sides. It needs a paid test before it is switched on (README, "m15 library").
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static", "presenters")
STATIC_URL = "/static/presenters"
AGE_BANDS = ("18-24", "25-34", "35-44", "45-54", "55-64", "65+")
FICTIONAL = ("This is an invented, AI-generated person who does not resemble any real or famous individual.")


def _flag(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in ("1", "on", "true", "yes")


def stills_on() -> bool:
    return _flag("PRESENTER_STILLS")


def voice_ref_on() -> bool:
    return _flag("PRESENTER_VOICE_REF")


def angles_on() -> bool:
    return _flag("PRESENTER_ANGLES")


ANGLES = ("angle-left", "angle-right", "angle-profile")     # the look-lock pictures, in the order they are sent


# ---------------------------------------------------------------- where a presenter can be
# place:   said after "in" in the video prompt ("A woman ..., in <place>, sits with ...")
# scene:   the room of the still, in the plain-phone style (nothing branded, nothing staged)
# pose:    how they are in it
# surface: where the product will stand; the still keeps a clear space there
SITUATIONS = {
    "kitchen": {
        "label": "Kitchen",
        "place": "an ordinary, lived-in home kitchen",
        "scene": "an ordinary home kitchen: a laminate counter, a kettle and a dish rack behind them, a fridge with "
                 "a couple of plain magnets, daylight from a window on one side and a ceiling light on",
        "pose": "standing at the counter, leaning on it slightly",
        "surface": "the kitchen counter beside them"},
    "car": {
        "label": "Car",
        "place": "the driver's seat of a parked everyday car",
        "scene": "the driver's seat of a parked, ordinary car: cloth seats, the headrest and a rear side window behind "
                 "them, flat daylight through the windscreen, the phone propped on the dashboard",
        "pose": "sitting back in the seat, seatbelt off, hands in their lap",
        "surface": "the top of the dashboard at the bottom of the frame"},
    "outdoor": {
        "label": "Outdoors",
        "place": "a quiet residential street on an overcast day",
        "scene": "a quiet residential street on an overcast day: a low brick wall, a hedge and parked cars softly out "
                 "of focus behind them, even grey daylight, a little wind in their hair",
        "pose": "standing by the low wall, the phone held at arm's length",
        "surface": "the top of the low wall beside them"},
    "desk": {
        "label": "Desk",
        "place": "a plain home desk by a window",
        "scene": "a plain home desk: a closed laptop pushed to one side, a desk lamp, a jar of pens and a pinboard "
                 "with blank notes behind them, daylight from a window to the left",
        "pose": "sitting at the desk, forearms resting on it",
        "surface": "the desk in front of them"},
    "sofa": {
        "label": "Sofa",
        "place": "a living-room sofa with a coffee table in front",
        "scene": "an ordinary living room: a fabric sofa with a creased throw blanket, a floor lamp and a shelf of "
                 "books turned spine-in behind them, soft daylight from the side",
        "pose": "sitting forward on the sofa, elbows on their knees",
        "surface": "the coffee table in front of them"},
    "bathroom-mirror": {
        "label": "Bathroom mirror",
        "place": "a small home bathroom, filmed with the phone propped by the mirror",
        "scene": "a small home bathroom: white wall tiles, a towel on a hook and a frosted window behind them, the "
                 "flat light of a ceiling fitting mixed with daylight, the phone propped beside the mirror",
        "pose": "standing at the basin, facing the phone",
        "surface": "the edge of the basin shelf at the bottom of the frame",
        # Said in every still of this place, whoever it is: the first run drew low-cut tops here. Ads run on
        # platforms that reject bare shoulders and chests, so the wardrobe is ordinary daytime clothes (said as what
        # to draw, never as a list of what not to).
        "dress": "They are fully dressed, as for an ordinary morning before work: the top has a high, round "
                 "neckline and sleeves, and covers the shoulders, the chest and the midriff."},
    "gym": {
        "label": "Gym",
        "place": "a corner of an ordinary gym",
        "scene": "a corner of an ordinary gym: black rubber floor tiles, a rack of plain dumbbells and a wall mirror "
                 "behind them, overhead strip lights, a little flushed as if between sets",
        "pose": "sitting on the end of a flat bench, a towel over one shoulder",
        "surface": "the flat bench beside them"},
    "shop-floor": {
        "label": "Shop floor",
        "place": "the floor of a small independent shop, behind its wooden counter",
        "scene": "a small independent shop: a worn wooden counter, shelves of plain unlabelled boxes and jars behind "
                 "them, a string of bare bulbs and daylight from the shop window",
        "pose": "standing behind the counter, hands resting on it",
        "surface": "the wooden counter in front of them"},
}
SITUATION_IDS = tuple(SITUATIONS)

# ---------------------------------------------------------------- voices
# The four presenter voices that existed before (catalog.VOICES) keep their ids; the newer presenters each get a
# descriptor of their own, so a voice fits the person who has it. `desc` is said in the prompt, as before.
VOICES = [
    {"id": "steady_m60", "name": "Steady, older", "who": "male",
     "desc": "a steady, slightly gravelly older male voice, unhurried, natural American accent"},
    {"id": "smooth_f", "name": "Smooth & sure", "who": "female",
     "desc": "a smooth, self-assured female voice in a lower register, natural American accent"},
    {"id": "even_m", "name": "Even & precise", "who": "male",
     "desc": "an even, precise male voice, mid-pitched and friendly, natural American accent"},
    {"id": "bright_f40", "name": "Bright & neighbourly", "who": "female",
     "desc": "a bright, neighbourly female voice in her forties with a quick laugh in it, natural American accent"},
    {"id": "gruff_m", "name": "Gruff & warm", "who": "male",
     "desc": "a deep, gruff but warm male voice, plain-spoken, natural American accent"},
    {"id": "soft_f", "name": "Soft & light", "who": "female",
     "desc": "a soft, light young female voice, a little quiet, natural American accent"},
    {"id": "lively_m", "name": "Lively", "who": "male",
     "desc": "a lively, quick young male voice with easy energy, natural American accent"},
    {"id": "gentle_f60", "name": "Gentle, older", "who": "female",
     "desc": "a gentle, slightly husky older female voice, patient and kind, natural American accent"},
    {"id": "measured_m50", "name": "Measured", "who": "male",
     "desc": "a measured, resonant male voice in his fifties, dry and reassuring, natural American accent"},
    {"id": "brisk_f", "name": "Brisk", "who": "female",
     "desc": "a brisk, clear female voice with a coach's directness, natural American accent"},
    {"id": "mellow_m", "name": "Mellow", "who": "male",
     "desc": "a mellow, rounded male voice, relaxed and good-humoured, natural American accent"},
    {"id": "clear_f40", "name": "Clear & direct", "who": "female",
     "desc": "a clear, direct female voice in her forties, level and confident, natural American accent"},
]
VOICE_BY_ID = {v["id"]: v for v in VOICES}
# Labels of the four older presenter voices (catalog.VOICES holds their descriptors).
_CLASSIC_VOICE_NAMES = {"warm_f": "Warm & friendly", "upbeat_f": "Upbeat", "calm_m": "Calm & assured",
                        "chatty_m": "Casual & chatty"}


def _p(id, name, age_band, gender, look, vibe, setting, hue, voice, appearance, situations, classic=False):
    return {"id": id, "name": name, "age_band": age_band, "gender": gender, "look": look, "vibe": vibe,
            "setting": setting, "hue": hue, "voice": voice, "appearance": appearance,
            "situations": [{"id": s, "wardrobe": w} for s, w in situations], "classic": classic}


# ---------------------------------------------------------------- the roster
# The first eight are the presenters Campaignify has always had: same ids, names, looks, settings and voices, so old
# jobs, saved batches and the planner's picks resolve to the same words. The twelve after them are new in m15.
_ROSTER = [
    _p("maya", "Maya", "25-34", "female",
       "woman in her late 20s, curly dark hair, warm smile, casual knit sweater", "Warm, casual",
       "a bright, lived-in kitchen", 28, "warm_f",
       "A woman of about 28 with a medium-brown complexion, a round face with full cheeks and a small gap between "
       "her front teeth, dark brown eyes, and shoulder-length dark curls with a slight frizz, parted off-centre. "
       "Average height, soft build. A faint scar through her left eyebrow and unplucked brows.",
       [("kitchen", "an oatmeal-coloured knit sweater with the sleeves pushed up"),
        ("sofa", "a loose grey sweatshirt and black leggings"),
        ("car", "a denim jacket over a plain white tee")], classic=True),
    _p("jordan", "Jordan", "25-34", "male",
       "man in his early 30s, short beard, plain t-shirt, relaxed posture", "Easygoing",
       "a sunny living room with plants", 200, "chatty_m",
       "A man of about 32 with light olive skin, a long face, hazel eyes set a little close, short mid-brown hair "
       "receding slightly at the temples, and a short, slightly uneven brown beard. Lean build, sloping shoulders, "
       "a relaxed slouch. A small mole on his right cheek.",
       [("sofa", "a plain heather-grey t-shirt and jeans"),
        ("kitchen", "a faded navy t-shirt with a dish towel over one shoulder"),
        ("outdoor", "an olive overshirt, unbuttoned, over a white tee")], classic=True),
    _p("priya", "Priya", "35-44", "female",
       "woman in her mid 30s, long straight black hair, minimal makeup, linen shirt", "Calm, expert",
       "a tidy home office with soft daylight", 300, "warm_f",
       "A woman of about 36 with warm brown skin, an oval face, dark eyes with faint shadows under them, a straight "
       "nose, and long straight black hair worn loose behind her shoulders. "
       "Slim, upright posture. Almost no makeup; a few fine lines at the corners of her eyes.",
       [("desk", "a cream linen shirt with the top button open"),
        ("kitchen", "a rust-coloured cotton blouse with rolled sleeves"),
        ("bathroom-mirror", "a plain white waffle robe closed high at the neck over a white crew-neck tee, hair "
                            "held back with a clip")], classic=True),
    _p("sam", "Sam", "25-34", "nonbinary",
       "person in their mid 20s, short bleached hair, oversized hoodie, energetic", "Upbeat, Gen Z",
       "a bedroom with fairy lights", 140, "upbeat_f",
       "A person of about 25 with fair skin that flushes easily, a heart-shaped face, grey-green eyes, thick dark "
       "eyebrows, and a short choppy crop of bleached hair with dark roots showing. Slight build, narrow shoulders. "
       "A scatter of small blemishes along the jaw and a thin silver ring in one ear.",
       [("desk", "an oversized sage-green hoodie"),
        ("sofa", "a baggy striped long-sleeve tee"),
        ("outdoor", "a black puffer vest over a grey hoodie")], classic=True),
    _p("elena", "Elena", "55-64", "female",
       "woman in her 50s, silver bob haircut, glasses, cardigan", "Trusted, grounded",
       "a cozy reading nook", 15, "warm_f",
       "A woman of about 57 with fair, lightly lined skin, a square jaw softened by age, pale blue eyes behind "
       "thin oval wire-rimmed glasses, and a chin-length silver-grey bob tucked behind one ear. Medium build. Clear "
       "laugh lines, a little redness on the cheeks, no visible makeup beyond a tinted lip balm.",
       [("sofa", "a moss-green cardigan over a cream top"),
        ("kitchen", "a navy striped apron over a pale blue shirt"),
        ("outdoor", "a quilted beige jacket and a thin scarf")], classic=True),
    _p("marcus", "Marcus", "45-54", "male",
       "man in his 40s, close-cropped hair, button-up shirt with sleeves rolled", "Straight-talking",
       "a modern kitchen island", 220, "calm_m",
       "A man of about 46 with dark brown skin, a broad face, deep-set dark eyes, close-cropped black hair going "
       "grey above the ears and a clean-shaven jaw with a faint shadow. Solid, broad-shouldered build. A crease "
       "between his brows and a slightly crooked front tooth when he smiles.",
       [("kitchen", "a light blue button-up shirt with the sleeves rolled to the elbow"),
        ("desk", "a charcoal quarter-zip pullover"),
        ("car", "a plain navy polo shirt")], classic=True),
    _p("lena", "Lena", "18-24", "female",
       "woman in her early 20s, freckles, messy bun, gym clothes", "Sporty, quick",
       "a bright home gym corner", 90, "upbeat_f",
       "A woman of about 22 with light skin covered in freckles across the nose and cheeks, a narrow face, green "
       "eyes, sandy-blonde hair pulled into a loose, messy bun with strands escaping, and straight unshaped brows. "
       "Athletic, wiry build. Slightly sunburnt nose, no makeup.",
       [("gym", "a faded teal training top and black leggings"),
        ("outdoor", "a light running jacket, zipped halfway"),
        ("bathroom-mirror", "an oversized white crew-neck tee, hair still in its bun")], classic=True),
    _p("theo", "Theo", "25-34", "male",
       "man in his late 20s, wavy hair, denim jacket, friendly grin", "Friendly, chatty",
       "a cafe table by the window", 260, "chatty_m",
       "A man of about 29 with a light tan complexion, a wide friendly mouth, brown eyes, thick wavy dark-brown hair "
       "that falls over his forehead, and two days of patchy stubble. Tall and lanky, long neck. One ear sits "
       "slightly lower than the other and his grin is a little lopsided.",
       [("outdoor", "a worn denim jacket over a grey tee"),
        ("desk", "a mustard corduroy shirt, open over a white tee"),
        ("car", "a dark green hoodie")], classic=True),

    _p("walt", "Walt", "65+", "male",
       "man in his mid 60s, grey crew cut, trimmed white beard, plaid flannel shirt", "Plain-spoken, practical",
       "an ordinary, lived-in home kitchen", 35, "steady_m60",
       "A man of about 66 with weathered, ruddy fair skin, a wide face with deep creases around the mouth, pale grey "
       "eyes under heavy lids, a grey crew cut thinning on top and a neatly trimmed white beard. Stocky, thick "
       "forearms, a slight stoop. Sun spots on his temples and reading glasses pushed up onto his head.",
       [("kitchen", "a red-and-navy plaid flannel shirt"),
        ("shop-floor", "a canvas work apron over a grey henley"),
        ("car", "a tan field jacket over a plaid shirt"),
        ("outdoor", "a navy fleece vest over a flannel shirt")]),
    _p("nia", "Nia", "25-34", "female",
       "woman in her early 30s, shoulder-length twists, small gold hoops, fitted tee", "Confident, clear",
       "a plain home desk by a window", 320, "smooth_f",
       "A woman of about 31 with deep brown skin, high rounded cheekbones, wide-set dark eyes, full brows and "
       "shoulder-length two-strand twists pushed back off her face. Medium height, curvy build. Small gold hoop "
       "earrings, a little shine on her forehead, and a single dimple on the left when she smiles.",
       [("desk", "a fitted rust-orange tee"),
        ("kitchen", "a loose white button-down with the sleeves rolled"),
        ("sofa", "a soft lilac lounge set"),
        ("bathroom-mirror", "a plain grey crew-neck tee, twists tied up in a scarf")]),
    _p("dev", "Dev", "25-34", "male",
       "man in his early 30s, short black hair, neat stubble, rectangular glasses", "Thoughtful, precise",
       "a plain home desk by a window", 190, "even_m",
       "A man of about 33 with light brown skin, a narrow face, dark eyes behind rectangular black-framed glasses, "
       "short thick black hair combed to the side and neat, even stubble. Slim, slightly hunched at the shoulders. "
       "A faint chickenpox mark on his forehead and prominent Adam's apple.",
       [("desk", "a navy crew-neck jumper over a collared shirt"),
        ("sofa", "a plain charcoal tee"),
        ("car", "a light grey zip-up jacket")]),
    _p("rosa", "Rosa", "45-54", "female",
       "woman in her late 40s, dark wavy bob with some grey, laugh lines, bright blouse", "Lively, neighbourly",
       "the floor of a small independent shop, behind its wooden counter", 350, "bright_f40",
       "A woman of about 48 with olive skin, a round face with deep laugh lines, dark brown eyes, strong arched "
       "brows and a dark wavy bob streaked with grey at the front. Short, sturdy build. Small pearl studs, lipstick "
       "slightly worn off, and hands that move when she talks.",
       [("shop-floor", "a coral blouse under a dark apron"),
        ("kitchen", "a teal wrap top"),
        ("outdoor", "a camel wool coat with a patterned scarf"),
        ("car", "a cream cardigan over a floral top")]),
    _p("gus", "Gus", "45-54", "male",
       "man in his mid 40s, shaved head, full ginger beard, work shirt", "Dry, down-to-earth",
       "the floor of a small independent shop, behind its wooden counter", 20, "gruff_m",
       "A man of about 45 with pink, fair skin, a shaved head with a visible stubble line, small blue eyes, a broad "
       "nose and a full ginger beard flecked with white. Heavyset, barrel-chested, thick neck. Freckled forearms "
       "and a pale tan line across his forehead as if from a cap.",
       [("shop-floor", "a faded khaki work shirt with the sleeves rolled"),
        ("car", "a black zip fleece"),
        ("outdoor", "a waxed olive jacket"),
        ("gym", "a grey cut-off training tee")]),
    _p("hana", "Hana", "18-24", "female",
       "woman in her mid 20s, straight black hair with a fringe, soft cardigan", "Gentle, thoughtful",
       "a small home bathroom, filmed with the phone propped by the mirror", 285, "soft_f",
       "A woman of about 24 with light skin with a cool undertone, a small oval face, dark almond-shaped eyes, a "
       "short straight nose, and collarbone-length straight black hair with a full fringe that needs a trim. "
       "Petite, narrow build. A couple of small moles on her neck and slightly chapped lips.",
       [("bathroom-mirror", "a pale pink towelling headband and a plain white crew-neck tee"),
        ("desk", "a cream cable-knit cardigan"),
        ("sofa", "an oversized navy sweatshirt"),
        ("outdoor", "a beige trench coat, open")]),
    _p("otis", "Otis", "18-24", "male",
       "man in his mid 20s, short coily hair with a fade, thin moustache, sports top", "Upbeat, quick",
       "a corner of an ordinary gym", 110, "lively_m",
       "A man of about 24 with dark brown skin, a long oval face, bright dark eyes, short coily black hair with a "
       "low fade and a thin moustache that doesn't quite meet a chin-strap of stubble. Tall, athletic, long arms. "
       "A small scar on his chin and one slightly chipped front tooth.",
       [("gym", "a sleeveless black training top"),
        ("car", "a white hoodie with the hood down"),
        ("outdoor", "a red windbreaker"),
        ("sofa", "a loose yellow tee")]),
    _p("june", "June", "65+", "female",
       "woman in her late 60s, soft white curls, round tortoiseshell glasses, cardigan", "Kind, unhurried",
       "an ordinary, lived-in home kitchen", 45, "gentle_f60",
       "A woman of about 68 with fair, finely wrinkled skin, a soft round face, light brown eyes behind round "
       "tortoiseshell glasses, and short, soft white curls. Small and slightly plump, rounded shoulders. Age spots "
       "on her hands, a thin gold chain, and powder that has settled a little into the lines of her cheeks.",
       [("kitchen", "a lavender cardigan over a white blouse"),
        ("sofa", "a pale blue jumper with a brooch"),
        ("outdoor", "a sage gardening gilet over a striped top")]),
    _p("cal", "Cal", "45-54", "male",
       "man in his early 50s, salt-and-pepper swept-back hair, clean shaven, open-collar shirt", "Measured, reassuring",
       "the driver's seat of a parked everyday car", 210, "measured_m50",
       "A man of about 52 with tanned, slightly leathery skin, a long rectangular face, grey eyes with crow's feet, "
       "a prominent chin and thick salt-and-pepper hair swept back from a high forehead. Lean, upright. Clean "
       "shaven with a small shaving nick near the jaw and a watch tan on his wrist.",
       [("car", "a light blue open-collar shirt"),
        ("desk", "a grey merino jumper over a white shirt"),
        ("outdoor", "a navy quilted jacket"),
        ("shop-floor", "a rolled-sleeve chambray shirt")]),
    _p("bex", "Bex", "35-44", "female",
       "woman in her mid 30s, copper-red hair in a low ponytail, freckled, training jacket", "Direct, energetic",
       "a corner of an ordinary gym", 5, "brisk_f",
       "A woman of about 35 with pale, heavily freckled skin, a strong jaw, light blue eyes, fair lashes and "
       "copper-red hair pulled into a low ponytail with baby hairs loose at the temples. Athletic, square "
       "shoulders. A flush across her chest and cheeks, and a faded friendship bracelet on one wrist.",
       [("gym", "a charcoal training jacket, half zipped"),
        ("kitchen", "a white long-sleeve tee"),
        ("outdoor", "a bright blue running top"),
        ("bathroom-mirror", "a grey zip hoodie zipped up to the collarbone over a plain black crew-neck tee")]),
    _p("mateo", "Mateo", "35-44", "male",
       "man in his late 30s, dark curly hair, short beard, rolled-sleeve shirt", "Warm, good-humoured",
       "an ordinary, lived-in home kitchen", 25, "mellow_m",
       "A man of about 38 with tan skin, a broad smile, dark brown eyes with heavy lashes, thick dark curls cut "
       "short at the sides, and a short full beard with a few grey hairs at the chin. Medium height, stocky, "
       "slight belly. Hairy forearms and a small burn scar on the back of one hand.",
       [("kitchen", "a white shirt with the sleeves rolled and a striped apron"),
        ("shop-floor", "a dark green polo shirt"),
        ("sofa", "a maroon henley"),
        ("desk", "a plain black tee")]),
    _p("ivy", "Ivy", "35-44", "female",
       "woman in her early 40s, long braids tied back, wide smile, smart-casual blazer", "Assured, friendly",
       "a plain home desk by a window", 160, "clear_f40",
       "A woman of about 42 with medium-brown skin, a square face with a wide mouth, dark eyes, neatly shaped "
       "brows and long thin braids tied back at the nape. Tall, straight-backed, medium build. Fine lines across "
       "her forehead, small silver studs, and a beauty mark above her lip on the right.",
       [("desk", "a mustard blazer over a white tee"),
        ("car", "a black turtleneck"),
        ("sofa", "a soft grey wrap cardigan"),
        ("shop-floor", "a denim shirt with the sleeves rolled")]),
]
BY_ID = {p["id"]: p for p in _ROSTER}
assert len(BY_ID) == len(_ROSTER), "presenter ids must be unique"
CLASSIC_IDS = tuple(p["id"] for p in _ROSTER if p["classic"])
IDS = tuple(BY_ID)


def all_presenters() -> list[dict]:
    """The raw records (prompts, the admin's tools). The front end reads roster()."""
    return list(_ROSTER)


def record(pid) -> dict | None:
    return BY_ID.get(str(pid or ""))


def classic(p: dict) -> dict:
    """A presenter in the dict shape catalog.PRESENTERS has always had."""
    return {"id": p["id"], "name": p["name"], "look": p["look"], "vibe": p["vibe"], "setting": p["setting"],
            "hue": p["hue"]}


def classic_presenters() -> list[dict]:
    """The eight older presenters, as they were (catalog.PRESENTERS)."""
    return [classic(p) for p in _ROSTER if p["classic"]]


def voice_of(pid) -> str:
    p = record(pid)
    return p["voice"] if p else ""


def voice_label(voice_id: str) -> str:
    v = VOICE_BY_ID.get(voice_id)
    return v["name"] if v else _CLASSIC_VOICE_NAMES.get(voice_id, voice_id)


# ---------------------------------------------------------------- situations

def situation_ids(pid) -> list[str]:
    p = record(pid)
    return [s["id"] for s in p["situations"]] if p else []


def situation(pid, sid) -> dict | None:
    """{id, label, place, scene, pose, surface, wardrobe, own} for a presenter in a situation. Any presenter can
    be put in any of the eight places by description; `own` says whether it is one of theirs (the ones that get a
    still and a wardrobe)."""
    p, base = record(pid), SITUATIONS.get(str(sid or ""))
    if p is None or base is None:
        return None
    mine = next((s for s in p["situations"] if s["id"] == sid), None)
    return dict(base, id=sid, wardrobe=mine["wardrobe"] if mine else "", own=mine is not None)


def default_situation(pid) -> str:
    ids = situation_ids(pid)
    return ids[0] if ids else ""


# ---------------------------------------------------------------- files

def _file(pid: str, name: str) -> str | None:
    if pid not in BY_ID or "/" in name or "\\" in name or ".." in name:
        return None
    path = os.path.join(STATIC_DIR, pid, name)
    return path if os.path.isfile(path) else None


def _url(pid: str, name: str) -> str | None:
    return f"{STATIC_URL}/{pid}/{name}" if _file(pid, name) else None


def still_path(pid, sid) -> str | None:
    """The approved still of a presenter in a situation, when it has been imported."""
    return _file(str(pid or ""), f"{sid}.webp") if sid in SITUATIONS else None


def portrait_path(pid) -> str | None:
    return _file(str(pid or ""), "portrait.webp")


def voice_path(pid) -> str | None:
    """The presenter's voice sample (the voice_ref), when it has been imported."""
    return _file(str(pid or ""), "voice.mp3")


def angle_paths(pid) -> list:
    """The presenter's look-lock pictures that have been imported, in ANGLES order (none, some or all three)."""
    return [p for p in (_file(str(pid or ""), f"{a}.webp") for a in ANGLES) if p]


def _public(p: dict) -> dict:
    return {"id": p["id"], "name": p["name"], "age_band": p["age_band"], "gender": p["gender"], "look": p["look"],
            "vibe": p["vibe"],
            "voice": {"id": p["voice"], "label": voice_label(p["voice"])},
            "portrait": _url(p["id"], "portrait.webp"), "voice_sample": _url(p["id"], "voice.mp3"),
            "situations": [{"id": s["id"], "label": SITUATIONS[s["id"]]["label"],
                            "still": _url(p["id"], s["id"] + ".webp")} for s in p["situations"]]}


def roster() -> list[dict]:
    """Every presenter for the picker: id, name, age_band, gender, look, voice {id, label}, portrait, voice_sample
    and situations [{id, label, still}]. A url is there only when its file is (else None)."""
    return [_public(p) for p in _ROSTER]


def get(pid) -> dict | None:
    """One presenter in roster()'s shape, or None."""
    p = record(pid)
    return _public(p) if p else None


# ---------------------------------------------------------------- the stills' prompts (library.py makes them)
REAL_PHONE = ("Real-phone look: natural skin texture with visible pores and small imperfections, no smoothing or beauty "
              "filter, slight softness rather than razor-sharp focus, ordinary mixed light from the room, no studio "
              "lighting, no rim light, no portrait-mode blur, no colour grading. Nothing in the picture carries a "
              "readable brand, logo or text. One person only. No captions, no watermark, no border.")
SAME_PERSON = ("The reference image shows this person. Make a new photo of exactly the same person: the same face, bone "
               "structure, skin, hair, age and build, unchanged. The place, the clothes and the pose are new, as "
               "described here; do not copy the reference's room, clothes or pose.")
# The softer second try of a "same person, somewhere new" still the vendor's content filter refused (worker._soften):
# the reference already shows who it is, so the person isn't described again, the clothes are named plainly with
# no adjectives, and the place is one short phrase.
SOFT_LOOK = ("Natural skin, ordinary light from the room, no filter and no studio lighting. No readable text or logos. "
             "One person only, fully dressed in ordinary daytime clothes.")
_GARMENTS = ("sweatshirt", "sweater", "cardigan", "overshirt", "hoodie", "jacket", "blouse", "shirt", "robe", "polo",
             "jumper", "fleece", "apron", "tee", "top", "t-shirt", "pullover", "button-down", "coat", "windbreaker",
             "henley", "turtleneck")


def plain_wardrobe(wardrobe: str) -> str:
    """A wardrobe line with its adjectives dropped: "a faded teal training top and black leggings" -> "a plain
    top". The first garment it names that covers the upper body, else plain everyday clothes."""
    words = [w.strip(",.").lower() for w in (wardrobe or "").split()]
    found = next((g for w in words for g in _GARMENTS if w == g), "")
    return f"a plain {'t-shirt' if found == 'tee' else found}" if found else "plain everyday clothes"


def still_prompt(pid, sid, same_person: bool = False, soft: bool = False) -> str:
    """The prompt for one situation still: an ordinary room, phone framing, natural skin, no studio light, the mouth
    relaxed, and a clear space on a surface where a product will stand. With `same_person` it is the second path:
    the first still is given as a reference and only the place and the clothes change. `soft` (with `same_person`)
    is that path's second try after the vendor's content filter refused the first: a shorter description with no
    wardrobe adjectives."""
    p, s = record(pid), situation(pid, sid)
    if p is None or s is None:
        raise KeyError(f"{pid}:{sid}")
    if soft and same_person:
        return "\n".join([
            SAME_PERSON,
            "Vertical photo taken with the front camera of an ordinary phone, as the first frame of a selfie video.",
            f"The same person as in the reference image, wearing {plain_wardrobe(s['wardrobe'])}, in {s['place']}.",
            "They look into the lens with a relaxed, neutral expression, the mouth closed. Framed from the chest up "
            "with the whole face clear; their hands hold nothing.",
            f"In the lower third of the frame, {s['surface']} is in view with a clear, empty space where a product "
            "will stand later.",
            SOFT_LOOK, FICTIONAL])
    wear = f" Wearing {s['wardrobe']}." if s["wardrobe"] else ""
    dress = f" {s['dress']}" if s.get("dress") else ""
    lines = [SAME_PERSON] if same_person else []
    lines += [
        "Vertical photo taken with the front camera of an ordinary phone, a little above eye level, as the first "
        "frame of a selfie video.",
        f"{p['appearance']}{wear}{dress}",
        f"The place is {s['scene']}. They are {s['pose']}.",
        "They look into the lens with a relaxed, friendly, neutral expression, the mouth closed and relaxed, as if "
        "about to start talking. Framed from the chest up with the whole face clear and unobstructed and both "
        "shoulders in the frame; their hands hold nothing.",
        f"In the lower third of the frame, {s['surface']} is in view with a clear, empty space about the size of a "
        "mug where a product will stand later. Nothing is placed in that space.",
        REAL_PHONE, FICTIONAL]
    return "\n".join(lines)


def portrait_prompt(pid, same_person: bool = False, soft: bool = False) -> str:
    """The prompt for the picker's portrait: head and shoulders, in their first situation's room. `soft`: the
    shorter second try after a content-filter refusal (see still_prompt)."""
    p = record(pid)
    if p is None:
        raise KeyError(pid)
    s = situation(pid, default_situation(pid))
    if soft and same_person:
        return "\n".join([
            SAME_PERSON, "Head-and-shoulders photo taken with an ordinary phone at eye level.",
            f"The same person as in the reference image, wearing {plain_wardrobe(s['wardrobe'] if s else '')}, in "
            f"{s['place'] if s else 'an ordinary room'}.",
            "They look into the lens with a relaxed, friendly expression, the mouth closed. The face is centred and "
            "fully visible.", SOFT_LOOK, FICTIONAL])
    wear = f" Wearing {s['wardrobe']}." if s and s["wardrobe"] else ""
    lines = [SAME_PERSON] if same_person else []
    lines += ["Head-and-shoulders photo taken with an ordinary phone at eye level.",
              f"{p['appearance']}{wear}",
              f"Behind them, softly out of focus: {s['scene'] if s else 'an ordinary room'}.",
              "They look into the lens with a relaxed, friendly expression, the mouth closed. The face is centred, "
              "fully visible, evenly lit by daylight from a window.",
              REAL_PHONE, FICTIONAL]
    return "\n".join(lines)


VOICE_LINE = "Hi, I'm {name}. Here's a quick look at what this is and who it's for."
VOICE_LINE_SHORT = "Hi, I'm {name}. Here's a quick look at this."      # a sample under six seconds


def voice_line(pid, seconds: int = 6) -> str:
    """What a presenter says in their voice sample: a neutral line, nothing about any product. A sample shorter than
    six seconds gets the shorter line, so it is said at a natural pace instead of rushed."""
    p = record(pid)
    if not p:
        return ""
    return (VOICE_LINE if int(seconds or 6) >= 6 else VOICE_LINE_SHORT).format(name=p["name"])
