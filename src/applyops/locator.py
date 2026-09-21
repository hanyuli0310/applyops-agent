"""Resolve form controls on a page, across shadow DOM and cross-origin iframes.

Why this exists
---------------
`browser.py` used to extract fields with an injected `document.querySelectorAll`.
That approach is now fundamentally broken for the sites we care about.

LinkedIn's current Easy Apply flow renders inside a **shadow root**
(`<div id="interop-outlet">`). `document.querySelectorAll` does not cross shadow
boundaries, so the old code did not merely mis-read those fields -- it could not
see that they existed. On a modal that visibly contained an email selector, a
country selector and a phone input, it would report zero fields. A harness would
then believe the form was complete and fail at submit time.

Playwright's locator engine *does* pierce shadow roots, and element handles can
read attributes across the boundary, so this module is built on those two
primitives rather than on injected DOM queries.

Addressing
----------
Controls are addressed by a short **reference string**, not a raw CSS selector,
so a reference survives a round trip through an MCP tool call::

    id=<id>                     exact element id
    idsuffix=<tail>             id ends with <tail>
    auto=<data-automation-id>   Workday and friends
    css=<selector>              explicit CSS (aria-label / placeholder / name)
    label=<text>                matched against the accessible label

`idsuffix` matters more than it looks. LinkedIn's form ids look like::

    single-line-text-form-component-formElement-urn-li-jobs-applyformcommon-
        easyApplyFormElement-4453247844-31371469220-phoneNumber-nationalNumber

The leading portion is generated noise, but the tail
(`phoneNumber-nationalNumber`) is semantic and stable. Anchoring on the tail
survives exactly the kind of churn that breaks an id-equality match.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from playwright.async_api import Frame, Locator, Page
from pydantic import BaseModel, Field

# Controls we care about. `:not([type=hidden])` because a hidden input is not a
# question, and a form that reports hidden fields as answerable is worse than one
# that reports nothing.
# A Yes/No question on Ashby is two <button aria-pressed> elements sitting over
# a `tabindex="-1"` input that cannot be clicked. A selector of inputs alone
# cannot see the question at all -- it is neither answered nor reported missing,
# and the application then fails at submit on a question nobody was asked. The
# button form is matched narrowly (it must carry `data-option` or a yes/no
# class) so an unrelated toggle button does not become a form field.
CONTROL_SELECTOR = (
    'input:not([type="hidden"]), textarea, select, '
    '[role="combobox"], [role="listbox"], [role="radio"], [role="checkbox"], '
    'button[aria-pressed][data-option], button[aria-pressed][class*=yesno], '
    '[contenteditable="true"]'
)

# Site chrome rather than form content. For *fields* this includes `footer`,
# because the page footer holds chrome controls such as LinkedIn's language
# picker -- a real <select> that is not an application question, and whose
# presence also breaks "wait until the form appears" polling, since the page
# would then never report zero fields.
#
# Buttons use a *narrower* filter on purpose: the apply modal keeps its own
# Next / Review / Submit buttons in a <footer>, so `browser._visible_buttons`
# excludes only `header, nav`. Fields and buttons are filtered differently
# because they are not distributed the same way across the page.
CHROME_ANCESTORS = "header, nav, footer"

# React's generated ids look like «r3» -- no semantic content, and they change
# between renders, so they are useless as a stable reference.
_REACT_ID_CHARS = "«»"

# Framework-generated ids end in an instance counter that changes on every
# render: Ember's `jobsDocumentCardToggle-ember244`, or a bare trailing `-17`.
# Anchoring on one of these would fill the selector memory with keys that are
# stale by the next page load, so they are treated as noise and the label is
# used instead.
_UNSTABLE_ID = re.compile(r"(?:^|[-_])ember\d+$|(?:^|[-_])\d{2,}$")


class FieldControl(BaseModel):
    """One answerable control found on the page."""

    ref: str  # reference string the caller hands back to act on this field
    label: str = ""
    label_source: str = ""  # label[for] | aria-label | aria-labelledby | placeholder | option-label | none
    group_label: str = ""  # the question this control answers, from its fieldset/ARIA group
    name: str = ""  # the form field name, which is what makes one radio option unique
    tag: str = ""
    field_type: str = "text"
    value: str = ""
    required: bool = False
    disabled: bool = False
    options: list[str] = Field(default_factory=list)
    checked: Optional[bool] = None
    element_id: str = ""
    frame_url: str = ""  # non-empty when the control lives in an iframe
    in_shadow_dom: bool = False

    @property
    def is_choice(self) -> bool:
        return self.field_type in ("radio", "checkbox")

    @property
    def is_dropdown(self) -> bool:
        return self.field_type in ("select", "combobox", "listbox")


def _is_noise_id(element_id: str) -> bool:
    """True for ids that carry no stable meaning across renders."""
    if not element_id:
        return True
    if any(ch in element_id for ch in _REACT_ID_CHARS):
        return True
    if _UNSTABLE_ID.search(element_id):
        return True
    return len(element_id) < 2


# The JS below runs on an element handle, so it executes inside whatever root
# the element lives in (light DOM or shadow). All lookups are scoped to that
# same root, because `label[for]` in the light DOM cannot see a shadow control.
_JS_DESCRIBE = """
(el) => {
  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
  const root = el.getRootNode();

  if (el.closest && el.closest('header, nav, footer')) return null;

  const attr = (a) => el.getAttribute ? el.getAttribute(a) : null;

  let label = '';
  let how = '';
  const id = el.id || '';
  if (id && root.querySelector) {
    const l = root.querySelector(`label[for="${CSS.escape(id)}"]`);
    if (l && norm(l.textContent)) { label = norm(l.textContent); how = 'label[for]'; }
  }
  if (!label && attr('aria-label')) { label = norm(attr('aria-label')); how = 'aria-label'; }
  if (!label && attr('aria-labelledby') && root.getElementById) {
    const l = root.getElementById(attr('aria-labelledby'));
    if (l) { label = norm(l.textContent); how = 'aria-labelledby'; }
  }
  if (!label) {
    // Climb a few levels looking for the form-element wrapper that carries the
    // question text. ATS forms nearly always wrap label + control together.
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++) {
      const cand = p.querySelector(
        'legend, .fb-dash-form-element__label, [data-test-form-element-label], label'
      );
      if (cand && norm(cand.textContent)) { label = norm(cand.textContent); how = 'ancestor'; break; }
      p = p.parentElement;
    }
  }
  if (!label && attr('placeholder')) { label = norm(attr('placeholder')); how = 'placeholder'; }

  // Which *question* does this control answer? For a bare "Yes"/"No" radio the
  // label is not the question, and answering by label is how one question's
  // answer ends up in another's box. The group is what binds them, and it is
  // taken from the nearest fieldset legend or ARIA group label -- inside this
  // control's own group only, never from the page as a whole.
  let group = '';
  let groupRequired = false;
  let node = el;
  for (let i = 0; i < 6 && node; i++) {
    const tag = (node.tagName || '').toLowerCase();
    if (tag === 'fieldset') {
      const legend = node.querySelector('legend');
      if (legend && norm(legend.textContent)) { group = norm(legend.textContent); }
      break;
    }
    const role = (node.getAttribute && (node.getAttribute('role') || '')) || '';
    if (role === 'group' || role === 'radiogroup') {
      const own = norm(node.getAttribute('aria-label') || '');
      const byId = (node.getAttribute('aria-labelledby') || '');
      const referenced = byId && root.getElementById ? root.getElementById(byId) : null;
      const text = own || (referenced ? norm(referenced.textContent) : '');
      if (text) { group = text; }
      break;
    }
    node = node.parentElement;
  }

  // Last resort, and the one Ashby needs: the question lives in a heading near
  // the options with no legend and no ARIA group label at all. Without it the
  // group was empty, which cost two things -- the required asterisk on the
  // *question* was never seen (so a required multi-select looked optional and
  // nothing reported it), and a named option could not be matched to its
  // question.
  {
    // Walk up for the question that owns this control, whether or not a group
    // was found above. Two things are read from it, and the second is why this
    // is no longer inside `if (!group)`: the required marker lives in the *class*
    // of the title ("_required_") with the asterisk as a separate element, so no
    // attribute and no text says "required". Reading it only when the group was
    // still empty made a required Yes/No question look optional -- it was never
    // filled and never reported, and the submission was rejected for a question
    // nobody knew had been asked.
    let up = el;
    for (let i = 0; i < 8 && up; i++) {
      up = up.parentElement;
      if (!up) break;
      const titled = up.querySelector(
        '.ashby-application-form-question-title, [class*=question-title], '
        + 'legend, h1, h2, h3, h4, h5, h6'
      );
      const text = titled ? norm(titled.textContent) : '';
      if (!text || text.length >= 300) { continue; }
      if (!group) { group = text; }
      const marks = ((titled.className || '') + ' '
        + ((titled.parentElement && titled.parentElement.className) || ''));
      if (/required/i.test(marks)) { groupRequired = true; }
      break;
    }
  }

  // The same doubling, applied to the *question*. LinkedIn renders the question
  // twice inside the group and appends its own "Required" marker, so the group
  // read "Bachelor's Degree?Have you completed ... : Bachelor's Degree? Required"
  // -- a key that matches no stored answer. The applicant had answered this
  // question many times and it still came back unanswered, and the report named
  // a question nobody could find in the answer store because of a stray space.
  if (group) {
    let g = group.trim();
    const trailingRequired = /^(.*?)\\s*Required\\s*$/i.exec(g);
    if (trailingRequired && trailingRequired[1].trim()) { g = trailingRequired[1].trim(); }
    const gHalf = g.length / 2;
    if (Number.isInteger(gHalf)) {
      if (g.slice(0, gHalf).trim() === g.slice(gHalf).trim()) { g = g.slice(0, gHalf).trim(); }
    } else if (Math.floor(gHalf) > 3) {
      const gk = Math.floor(gHalf);
      if (g.slice(0, gk).trim() === g.slice(gk).trim()) { g = g.slice(0, gk).trim(); }
    }
    group = g;
  }

  // A label rendered twice is noise, not a different question. LinkedIn does
  // this for screen readers: "Email addressEmail address", and also as two
  // copies separated by a space ("Do you speak fluent English? Do you speak
  // fluent English?"). The separator is what broke the previous check: it makes
  // the total length odd, and a check that only splits an even-length string in
  // half skipped the space-separated form, handing a doubled question to the
  // answer lookup -- which then missed, and a posting was skipped for a
  // question the user had already answered. Both forms are handled, and each
  // half is trimmed before comparing so the separator does not hide the match.
  if (label.length > 6) {
    const even = label.length / 2;
    if (Number.isInteger(even)) {
      if (label.slice(0, even).trim() === label.slice(even).trim()) {
        label = label.slice(0, even).trim();
      }
    } else if (Math.floor(even) > 3) {
      const k = Math.floor(even);
      if (label.slice(0, k).trim() === label.slice(k).trim()) {
        label = label.slice(0, k).trim();
      }
    }
  }

  let options = [];
  let type = (attr('type') || '').toLowerCase();
  const tag = el.tagName.toLowerCase();
  const role = (attr('role') || '').toLowerCase();
  if (tag === 'select') {
    options = Array.from(el.options).map(o => norm(o.text || o.value));
  } else if (role === 'combobox' || role === 'listbox') {
    // Options live in a popup that only exists once the control is opened, so
    // there is nothing to enumerate yet. Reported as unknown, not as empty.
    options = [];
  }

  let fieldType = type || 'text';
  if (tag === 'textarea') fieldType = 'textarea';
  else if (tag === 'select') fieldType = 'select';
  else if (tag === 'input' && type === 'file') fieldType = 'file';
  else if (tag === 'input' && !type) fieldType = 'text';
  else if (role === 'combobox') fieldType = 'combobox';
  else if (role === 'listbox') fieldType = 'listbox';
  else if (role === 'radio') fieldType = 'radio';
  else if (role === 'checkbox') fieldType = 'checkbox';
  else if (attr('contenteditable') === 'true') fieldType = 'contenteditable';
  else if (tag === 'input' && (type === 'radio' || type === 'checkbox')) fieldType = type;

  const pressedAttr = attr('aria-pressed');
  const isToggle = tag === 'button' && (pressedAttr === 'true' || pressedAttr === 'false');

  let checked = null;
  if (isToggle) {
    checked = pressedAttr === 'true';
    fieldType = 'radio';
    // The button's own text *is* the option ("Yes" / "No"); the question is the
    // group above it. Left as-is, the ancestor climb earlier finds the
    // question's <label> and both buttons report the question as their label --
    // they become one indistinguishable field and neither can be chosen.
    const own = norm(el.textContent || '');
    if (own) { label = own; how = 'button-text'; }
  } else if ('checked' in el && (type === 'radio' || type === 'checkbox' || role === 'radio' || role === 'checkbox')) {
    if (type === 'radio' || type === 'checkbox') checked = el.checked;
    else checked = attr('aria-checked') === 'true';
  }

  // Option text for a radio/checkbox is the wrapping label's text.
  let optionText = '';
  if (checked !== null && !isToggle) {
    const wrap = (el.closest && el.closest('label')) || el.parentElement;
    optionText = norm(wrap ? wrap.textContent : '');
    if (optionText && optionText.slice(0, 80) !== label.slice(0, 80)) {
      label = optionText.slice(0, 120);
      how = how || 'option-label';
    }
  }

  return {
    id: id,
    tag: tag,
    type: type,
    role: role,
    label: label.slice(0, 200),
    labelSource: how,
    groupLabel: group,
    fieldType: fieldType,
    name: attr('name') || '',
    value: (el.value === undefined || el.value === null) ? '' : String(el.value).slice(0, 200),
    // Three ways a form says "required", and the third is the one that bit us:
    // Ashby marks a question with a trailing asterisk in its label and sets no
    // `required` attribute at all, so a required multi-select looked optional --
    // nothing reported it as missing, and the submitted application was rejected
    // by a validation error nobody had been told about.
    required: !!el.required
      || attr('aria-required') === 'true'
      || String(label || '').trim().endsWith('*')
      || String(label || '').trim().endsWith('✱')
      || String(group || '').trim().endsWith('*')
      || String(group || '').trim().endsWith('✱')
      || groupRequired,
    disabled: !!el.disabled || attr('aria-disabled') === 'true',
    options: options.slice(0, 200),
    valueAttribute: attr('value') || '',
    checked: checked,
    automationId: attr('data-automation-id') || '',
    inShadow: root !== document,
  };
}
"""


def _id_tail(element_id: str) -> str:
    """The semantic tail of a generated form id, or '' if there is none.

    LinkedIn ids end in something like `...-phoneNumber-nationalNumber`; the
    last two dash-separated segments carry the meaning.
    """
    parts = [p for p in element_id.split("-") if p]
    if len(parts) < 3:
        return ""
    tail = "-".join(parts[-2:])
    # Require a mixed-case or long tail so we do not anchor on `-1-2`.
    if len(tail) < 8:
        return ""
    return tail


def build_ref(info: dict) -> str:
    """Choose the most stable reference string for a control.

    Ordered by how well each survives a site redesign: an automation id or a
    semantic id tail is stable, a positional selector is not.
    """
    automation = info.get("automationId") or ""
    if automation:
        return f"auto={automation}"

    field_type = (info.get("fieldType") or "").lower()
    name = info.get("name") or ""
    value = info.get("value")
    if field_type in {"radio", "checkbox"} and name:
        # `label=Yes` is ambiguous the moment a page has two Yes/No questions:
        # the reference for the first group's "Yes" also matches the second
        # group's, so one of them was silently dropped at collection time and
        # could never be answered. name+value addresses exactly one option.
        #
        # The value clause is appended only when a `value` *attribute* is
        # actually present. Ashby renders consent checkboxes whose `name` is
        # the whole consent sentence and whose value exists as a property but
        # not as an attribute; `[value="on"]` matches attributes only, so a
        # reference built from the property matched nothing at all and the
        # checkbox could never be touched.
        selector = f'[name="{_css_quote(name)}"]'
        if info.get("valueAttribute") and value not in (None, ""):
            selector += f'[value="{_css_quote(str(value))}"]'
            return f"css={selector}"
        # No value attribute: every sibling in the group shares this `name`, so
        # a name-only reference is ambiguous. On a live Ashby question it
        # ticked the *first* of four options -- an answer the applicant never
        # gave, and one the read-back confirmed as "verified" because it only
        # ever looked at the element it had landed on. The option's own text is
        # the only thing that tells them apart.
        option_label = info.get("label") or ""
        if option_label:
            return f"label={option_label}"
        return f"css={selector}"

    element_id = info.get("id") or ""
    if not _is_noise_id(element_id):
        tail = _id_tail(element_id)
        # Prefer the tail when the id looks generated, so a prefix change does
        # not invalidate the reference.
        if tail and len(element_id) > 40:
            return f"idsuffix={tail}"
        return f"id={element_id}"

    label = info.get("label") or ""
    source = info.get("labelSource") or ""
    if label and source == "aria-label":
        return f'css=[aria-label="{_css_quote(label)}"]'
    if label and source == "placeholder":
        return f'css=[placeholder="{_css_quote(label)}"]'
    if label:
        # Last stable option: what a human would call the field.
        return f"label={label}"

    name = info.get("name") or ""
    if name:
        return f'css=[name="{_css_quote(name)}"]'

    if element_id:
        # Unstable, but still more specific than nothing at all.
        return f"id={element_id}"
    return ""


def _css_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def parse_ref(ref: str) -> tuple[str, str]:
    """Split a reference string into (strategy, value)."""
    if not ref:
        return "", ""
    if "=" not in ref:
        # Tolerate a bare selector from a caller that passed one.
        return "css", ref
    strategy, _, value = ref.partition("=")
    return strategy.strip().lower(), value


def _css_for(strategy: str, value: str) -> str:
    if strategy == "id":
        return f'[id="{_css_quote(value)}"]'
    if strategy == "idsuffix":
        return f'[id$="{_css_quote(value)}"]'
    if strategy == "auto":
        return f'[data-automation-id="{_css_quote(value)}"]'
    if strategy == "css":
        return value
    return ""


# Frames that are advertising or telemetry plumbing rather than form content.
# LinkedIn, for instance, keeps a hidden `/preload/` iframe around whose inputs
# would otherwise be reported as application questions.
_NOISE_FRAME_HINTS = (
    "/preload/",
    "googleads",
    "doubleclick",
    "facebook.com/tr",
    "analytics",
    "/collect",
)

# An iframe smaller than this is decorative, not a form.
_MIN_FRAME_PX = 2


async def _frame_is_visible(frame: Frame) -> bool:
    """True when the iframe actually occupies space on the page."""
    try:
        # A detached or cross-origin iframe can leave Playwright waiting on a
        # CDP response indefinitely. Field discovery must remain bounded: a
        # decorative frame is never worth stalling the whole application.
        element = await asyncio.wait_for(frame.frame_element(), timeout=1.5)
        if element is None:
            return False
        if not await asyncio.wait_for(element.is_visible(), timeout=1.5):
            return False
        box = await asyncio.wait_for(element.bounding_box(), timeout=1.5)
        if not box:
            return False
        return box["width"] > _MIN_FRAME_PX and box["height"] > _MIN_FRAME_PX
    except Exception:
        # A frame that cannot be inspected is not safe to treat as form
        # content; keeping it here can hang every subsequent field scan.
        return False


async def _frames(page: Page) -> list[Frame]:
    """Main frame first, then child frames -- including cross-origin ones.

    Cross-origin frames are kept on purpose, because ATS forms are often
    embedded that way, but zero-size and tracker frames are dropped so the
    caller does not see advertising widgets as application questions.
    """
    frames: list[Frame] = [page.main_frame]
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        url = (frame.url or "").lower()
        if any(hint in url for hint in _NOISE_FRAME_HINTS):
            continue
        if not await _frame_is_visible(frame):
            continue
        frames.append(frame)
    return frames


async def _describe(locator: Locator, limit: int, frame_url: str = "") -> list[FieldControl]:
    found: list[FieldControl] = []
    count = await locator.count()
    for index in range(min(count, limit)):
        handle = locator.nth(index)
        try:
            if not await handle.is_visible():
                continue
            info = await handle.evaluate(_JS_DESCRIBE)
        except Exception:
            # A detached or restricted node (e.g. inside a cross-origin frame
            # that blocks evaluation) is skipped rather than failing the scan.
            continue
        if not info:
            continue
        ref = build_ref(info)
        if not ref:
            continue
        found.append(
            FieldControl(
                ref=ref,
                label=info.get("label", ""),
                label_source=info.get("labelSource", ""),
                group_label=info.get("groupLabel", ""),
                name=info.get("name", ""),
                tag=info.get("tag", ""),
                field_type=info.get("fieldType", "text"),
                value=info.get("value", ""),
                required=bool(info.get("required")),
                disabled=bool(info.get("disabled")),
                options=list(info.get("options") or []),
                checked=info.get("checked"),
                element_id=info.get("id", ""),
                frame_url=frame_url,
                in_shadow_dom=bool(info.get("inShadow")),
            )
        )
    return found


async def resolve_fields(page: Page, limit_per_frame: int = 120) -> list[FieldControl]:
    """Every answerable control on the page, across shadow roots and frames.

    Duplicates are collapsed on the reference string, so a control reachable
    through two frames is reported once.
    """
    results: list[FieldControl] = []
    seen: set[str] = set()

    for frame in await _frames(page):
        try:
            locator = frame.locator(CONTROL_SELECTOR)
            controls = await _describe(locator, limit_per_frame, frame_url=frame.url)
        except Exception:
            continue
        for control in controls:
            # The group is part of the identity: two Yes/No questions on one page
            # produce controls with the same label and (before the ref fix) the
            # same reference, and collapsing them meant only the first question
            # was ever seen, let alone answered.
            key = f"{control.ref}|{control.label}|{control.group_label}"
            if key in seen:
                continue
            seen.add(key)
            results.append(control)

    return results


async def resolve_ref(page: Page, ref: str) -> Optional[Locator]:
    """Resolve a reference string to a locator, searching frames in order.

    A shadow-root control is found by Playwright's own engine, so no special
    handling is needed here -- but a control inside an iframe is only reachable
    from its own frame, which is why frames are searched.
    """
    strategy, value = parse_ref(ref)
    if not strategy or not value:
        return None

    if strategy == "label":
        # Exact first, substring second -- and the order is the whole point.
        # A field labelled "No" (a yes/no radio) used to resolve to the field
        # labelled "Notice period", because substring matching found "No" inside
        # "Notice". That is not a cosmetic bug: the click landed on the wrong
        # control, and the read-back then "could not verify" a value that was
        # never typed where we thought.
        def factories(exact: bool):
            return (
                lambda f: f.get_by_label(value, exact=exact),
                lambda f: f.get_by_role("radio", name=value, exact=exact),
                lambda f: f.get_by_role("checkbox", name=value, exact=exact),
            )

        for exact in (True, False):
            for factory in factories(exact):
                for frame in await _frames(page):
                    try:
                        locator = factory(frame)
                        if await locator.count():
                            return locator.first
                    except Exception:  # noqa: BLE001 - try the next strategy
                        continue
        # Before falling back to the text, try the control the label *belongs*
        # to when the association is broken. Ashby renders "Where are you
        # currently based?" as a `<label for="_systemfield_location">` whose
        # target does not exist (a React-controlled input with no id), so
        # get_by_label finds nothing and the text fallback lands on the label --
        # and writing to a label reads back the question, which is a mismatch,
        # every time. The control is in the label's own container; find it there.
        for frame in await _frames(page):
            try:
                marker = await frame.evaluate(
                    """text => {
                      const wanted = String(text || '').trim().toLowerCase();
                      if (!wanted) return '';
                      for (const label of document.querySelectorAll('label')) {
                        const own = (label.innerText || '').trim().toLowerCase();
                        if (!own || !own.includes(wanted)) continue;
                        const direct = label.control
                          || (label.htmlFor ? document.getElementById(label.htmlFor) : null);
                        const host = label.parentElement || label.closest('div');
                        const control = direct
                          || (host ? host.querySelector('input,textarea,select') : null);
                        if (!control) continue;
                        const token = 'applyops-ref-' + Math.random().toString(36).slice(2, 9);
                        control.setAttribute('data-applyops-ref', token);
                        return token;
                      }
                      return '';
                    }""",
                    value,
                )
            except Exception:
                marker = ""
            if marker:
                candidate = frame.locator(f'[data-applyops-ref="{marker}"]').first
                try:
                    if await candidate.count():
                        return candidate
                except Exception:
                    pass

        # Nothing carried that accessible name. Fall back to the visible text
        # itself -- clicking a label toggles the control it belongs to, which is
        # enough for choosing a resume or agreeing to a term.
        for frame in await _frames(page):
            try:
                locator = frame.get_by_text(value, exact=False)
                count = await locator.count()
                for index in range(min(count, 6)):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        return candidate
            except Exception:
                continue
        return None

    css = _css_for(strategy, value)
    if not css:
        return None

    for frame in await _frames(page):
        try:
            locator = frame.locator(css)
            if await locator.count():
                return locator.first
        except Exception:
            continue
    return None


async def find_button(page: Page, name: str) -> Optional[Locator]:
    """Locate a button or button-like link by accessible name.

    Deliberately does not use the page's CSS classes: LinkedIn's class names are
    now hashed (`b486132d _50ad7bd2 ...`) and carry no meaning, and LinkedIn's
    Easy Apply control is an ``<a>`` rather than a ``<button>``.
    """
    for frame in await _frames(page):
        for factory in (
            lambda f: f.get_by_role("button", name=name, exact=False),
            lambda f: f.get_by_role("link", name=name, exact=False),
            lambda f: f.locator(f'[aria-label*="{_css_quote(name)}"]'),
        ):
            try:
                locator = factory(frame)
                count = await locator.count()
                for index in range(min(count, 8)):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        return candidate
            except Exception:
                continue
    return None
