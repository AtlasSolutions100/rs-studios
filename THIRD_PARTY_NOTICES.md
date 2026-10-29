# Third-party notices

Campaignify's own code is not covered by this file. It lists outside material that parts of Campaignify are based on.

## Marketing Skills (Corey Haines)

Source: https://github.com/coreyhaines31/marketingskills (reference copy at commit 0cd9b3a).
Licence: MIT (full text below).

Campaignify uses these playbooks as reference only. Their guidance was condensed and rewritten in Campaignify's own
words for small sellers, and Campaignify's compliance rules (`compliance.py`, `copywriter.RULES`) override it
everywhere. No playbook text is copied: `tests/test_app.py` fails if any sentence longer than 12 words in the
playbooks appears word for word in the files below (it checks fingerprints in `tests/playbook_fingerprints.txt`,
or the playbooks themselves when `RS_PLAYBOOKS` points at a copy).

Files with material derived from the playbooks:

| File | What | Playbooks it draws on |
|---|---|---|
| `copywriter.py` | `EMAIL_GUIDE`, `LANDING_GUIDE` (email and landing-page structure, length and subject-line guidance) | emails, copywriting |
| `platforms.py` | `EMAIL_TYPES` (the Copy studio's five kinds of email) | emails |
| `planner.py` | the offer and lead-magnet parts of `SYSTEM`; `LEADS_ITEMS` | offers, lead-magnets |
| `flows.py` | `TEMPLATES` (starter flow timing), `draft_system()`, `INTERNAL` (Campaignify's own onboarding emails) | emails, sms, onboarding |
| `assistant.py` | the activation and "one goal per session" parts of `SYSTEM`; `nudge()` (stalled-user suggestions) | onboarding |
| `sms.py`, `forms_routes.py` | opt-out, HELP and consent wording | sms |

### Licence

```
MIT License

Copyright (c) 2025 Corey Haines

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
