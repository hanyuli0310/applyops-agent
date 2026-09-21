# Applying for real: field notes from walking live postings

Written 2026-09-20, from actually driving the product against live LinkedIn
postings and the employer systems behind them. Everything here was observed, not
inferred; where something is still unknown it says so.

This is the document to read before pointing ApplyOps at a real posting, and the
one to add to when you find something new. It is deliberately blunt: the failure
modes below cost a day each.

---

## 1. Two shapes of application, and they are not alike

| | **Easy Apply** (on LinkedIn) | **Off-site** (employer's own system) |
|---|---|---|
| Where the form is | on LinkedIn, one click away at `/jobs/view/<id>/apply/` | on the employer's ATS (Greenhouse, Lever, Workday, `careers-page.com`, …) |
| Route recorded | `easy_apply` | `external` → `external_ats` once walked |
| How to reach it | follow the on-site apply control (`open_onsite_application`) | follow the apply control off-site (`follow_offsite_apply`) |
| Submission wording | in `evidence.SUCCESS_EVIDENCE["LinkedIn"]` | only for Greenhouse / Lever / Workday so far |
| Verified end-to-end | form fills; multi-step not yet handled | fills; several field-level gaps remain (§6) |

`is_drivable_route()` allows `demo`, `easy_apply` and `external_ats`. A plain
`external` posting is *not* drivable — it is the honest "we have no verified path
here" state, and it is where a posting parks until someone asks for the hop.

## 2. The off-site hop, step by step

1. **Detect.** `apply_target.offsite_apply_host()` reads the apply control and
   compares hosts. A LinkedIn posting is *always* `linkedin.com/jobs/view/<id>/`,
   so the route says `easy_apply` even when the application lives elsewhere —
   which is exactly why detection reads the page instead of trusting the URL.
2. **Follow.** `follow_offsite_apply()` navigates to the control's `href` after
   unwrapping the redirect. See §3 — clicking does not work.
3. **Land and check.** The landing page is inspected for a sign-in wall before
   anything else (§4). Then the normal fill runs, and the approval request is
   created **on the landing page**, because that page is what the grant binds to.
4. **Record.** Every step goes into `RouteKnowledge` (`open`, `click`, `hop`,
   one `fill` per field with its source and selector, `upload`). A walk that dies
   records a blockage instead. This is the material a future replay would use.

The hop is **off by default** everywhere: `POST /api/applications/<id>/prepare`
takes `{"allow_offsite_hop": true}`, `AutoPolicy.allow_offsite_hop` carries it for
a pass, and the MCP `prepare_application` tool exposes it. Following an apply
control into someone else's system is a decision per posting, not a convenience.

## 3. Landmines on LinkedIn's own pages

- **A scripted click on "Apply" does nothing.** Verified: no navigation, no new
  tab, no modal. Going to the `href` it names opens the flow immediately. Rule:
  navigate an href; click only a control that has none.
- **The redirect wrapper is a dead end.** `https://www.linkedin.com/safety/go/?url=…`
  leads to LinkedIn's own "you are leaving" interstitial
  (`/safety/go/?_l=en_US`). Unwrap the `url` parameter and go direct.
- **The footer comes first.** LinkedIn's footer is full of "Apply"-ish links to
  `business.linkedin.com`, `safety.linkedin.com` and friends, and they appear
  *before* the posting's control in the DOM. Treating "a different host" as "the
  employer" walked straight into them. Use `is_employer_destination()`: the
  employer is a host that is neither the current one nor a LinkedIn subdomain.
- **Easy Apply is a link, not a modal.** A real posting has no form and three of
  LinkedIn's own search boxes. Deciding "is there a form here?" from a count of
  controls gets it wrong both ways; decide from what the fill actually achieved
  (nothing fillable + an apply control present ⇒ the form is behind it).

## 4. Employer systems

- **They redirect.** `boards.greenhouse.io/mongodb/jobs/7742875` ended up on
  `www.mongodb.com/careers/jobs/7742875`. Record the landing URL, not the one you
  aimed at.
- **Some want an account first.** When the landing page has a password field and
  sign-in wording, `sign_in_wall()` catches it and the application parks with the
  host and "sign in once in this browser, then retry". Do not poke at a login.
- **Unknown confirmation wording means no submission.** If
  `success_patterns_for(url)` is empty, `submission.execute_authorized_submission`
  refuses *before* clicking: the alternative is sending a real application and
  then being able to report only "unverified". Add a site's wording only from a
  real observation.
- **Forms can be long.** Greenhouse's MongoDB form asks 18 required answers
  including Country, Preferred Name and a full demographic survey (§6).
- **The form may be in a cross-origin iframe.** Verified on that same posting:
  the host page (`www.mongodb.com/careers/jobs/7742875`) has **2** controls, while
  `job-boards.greenhouse.io/embed/job_app?for=mongodb&validityToken=…` — an
  iframe — has **36**. Field discovery walks frames, so the fields are found and
  written; what broke was the *read-back* (§6).
- **A cookie-consent overlay can intercept every click.** On that page OneTrust's
  banner (`#onetrust-consent-sdk`) sits over the form, and Playwright retries the
  click until it times out:
  `<div id="onetrust-button-group">… intercepts pointer events`.
  Dismiss the banner before touching the form, or interactions will look like
  "the control is there but nothing happens".

## 5. Rules that must not be broken

These used to be enforced by the test suite; the suite was removed on
2026-09-20, so they are written here instead.

1. **Never answer for the user.** Anything about the user's own facts — work
   authorization, sponsorship, salary, notice period, gender, race, veteran or
   disability status — is theirs to state. Missing ⇒ `waiting_for_input`, naming
   the question.
2. **Demographic and EEO questions are never auto-filled, even though the form
   marks them required.** They are legally sensitive self-identification, and in
   the US "prefer not to say" is a legitimate answer that only the applicant may
   choose. Park and ask (§6).
3. **No self-minted grants.** An approval request is filed, a human approves it,
   and the grant is bound to one application, one page and one snapshot digest.
4. **`UNVERIFIED` is never retried automatically.** Only a provable "nothing was
   sent" (`failed` with `sent: false`) may be retried.
5. **Do not submit what cannot be verified** (§4).
6. **`AGENTS.md` §12 and `applyops/target_titles.py` must agree** — the pool is
   the written definition, the module is the copy the code reads.
7. **The console, MCP and the runner share one browser page.** Every path that
   touches it takes the page lock; the cross-process half is the profile lock.
8. **Real postings are real.** No test or experiment may write to the real
   `data/` profile or answers; use a temporary data directory and the local demo
   ATS (`applyops.demo_ats`, which has fixtures for off-site, on-site Easy Apply,
   a sign-in wall and a footer decoy).

## 6. Known gaps (as of 2026-09-20)

- **Read-back on employer forms.** On Greenhouse/MongoDB every core field
  (First Name, Last Name, Email, Phone, Location, Resume) came back
  `unverifiable` — written, but the value could not be confirmed. The cause is
  now known: **the form lives in a cross-origin iframe** (§4), on a host page
  whose own document has almost nothing in it, and with a consent overlay on top.
  On `careers-page.com` the same fields read back fine, so this is specific to
  how that page exposes its inputs. `[unverifiable]` is not "wrong": it means the
  product will not claim a value it could not confirm — and it is a *safety*
  detail, because an approval summary built from unreadable fields may not show
  what would actually be sent. Do not submit from a form whose core fields are
  unverifiable.
- **`careers-page.com` had its own four**: `phone` and `years of experience`
  (twice) came back `mismatch`, `Salary` was `unreadable`, and a repeated
  `full name` section was `unverifiable`.
- **Anonymous dropzone inputs can collide with a text field's label.** Ashby
  renders a file input whose nearest label is `Name` -- the same label as the
  name text field above it. Choosing "the first file control" produced the
  reference `label=Name`, which resolved to the *text* field, and
  `set_input_files` timed out on it while a real application shipped without a
  resume. Prefer a reference that names the control directly, and prove it
  resolves to a control of `type=file` before using it
  (`filling._file_input_ref`).
- **A `[value="on"]` clause can match nothing.** Ashby's consent checkbox uses
  the whole consent sentence (329 characters) as its `name` attribute, and its
  value exists as a property but not as an attribute; CSS attribute selectors
  match attributes only. The collector records whether a value *attribute* is
  present and `build_ref` appends the clause only then
  (`locator.py`, `valueAttribute`).
- **Consent checkboxes are a standing answer** (AGENTS.md §14): checked by
  default, reported with the source `default:consent (AGENTS.md §14)`.
- **Typeahead fields.** LinkedIn's `Location (city)` is
  `role=combobox` + `aria-autocomplete=list`: a typed value alone reports
  `mismatch: control reports a different option` because a suggestion has to be
  picked. Same class of field exists on employer forms (city, country).
- **Multi-step forms.** LinkedIn Easy Apply is a wizard (Contact info → Resume →
  questions → Review → Submit). The product fills the screen in front of it; it
  does not yet advance between screens.
- **The city question is unresolved.** The current rule answers a city question
  with the posting's location (`job:location`), which is right when the question
  means "which office is this for" and wrong when it means "where do you live".
  For a remote posting (`Austin, TX (Remote)`) it is almost certainly the latter.
  Awaiting a decision.
- **Demographic blocks.** A real Greenhouse form cannot be submitted until the
  applicant answers its required demographic survey; the product parks instead.

## 7. Standing applicant defaults

`AGENTS.md` §14 records the decisions the applicant made once, so forms stop
asking: consent and agreement checkboxes are checked by default; experience
questions are answered from his stated experience, with "none of the above"
as a last resort (and never chosen on his behalf when no option is truthful);
every location question takes the posting's location. Anything not covered
there still goes to the applicant.

## 8. Operating notes

- **Data directory.** `data/` holds the real profile (`profile.md`), resume,
  answers (`answers` store), ledger (`app.sqlite`, schema v2 with
  `applications.location`) and the browser profile. The last one is 1 GB.
- **The browser.** `BrowserController` attaches to a browser already up on the
  profile's own `DevToolsActivePort`, or launches one asking Chrome to choose the
  port. `tools/attach.py` deliberately starts a *detached* Chrome on port 9222;
  if a run dies hard, that Chrome outlives its flock and every later launch fails
  with "profile is already in use" — `applyops stop` cleans it up by command
  line. Do not launch a second instance on the same profile.
- **Rails.** Daily cap and a randomised gap between submissions (default
  45–180 s) live in `guard_state.json`; the gap is waited out inside
  `service.submit` and re-checked afterwards. `run_pass(budget=N)` always states
  how many a pass may send.
- **Recovery.** `applyops doctor` reports what is configured; `applyops serve`
  opens the console. The ledger's schema migrates in place on open.
