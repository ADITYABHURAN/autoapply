"""Read an application form off the page and fill it.

Nothing here is specific to one ATS. `extract_fields` walks the DOM and returns every
control with its label, type, options and required flag; `fill_field` knows how to set
each kind of control. Per-ATS quirks live in adapters.py.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

EXTRACT_JS = r"""
(rootSelector) => {
  const root = (rootSelector && document.querySelector(rootSelector)) || document;
  window.__aa = window.__aa || 0;
  const tag = el => { if (!el.dataset.aaId) el.dataset.aaId = 'aa' + (window.__aa++); return el.dataset.aaId; };
  const vis = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 1 && r.height > 1;
  };
  const raw = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const clean = t => (t || '').replace(/✱/g, ' ').replace(/\s+/g, ' ').replace(/[\s*]+$/, '').replace(/\(required\)$/i, '').trim();
  let lastLabelEl = null;  // element the most recent label text came from (its class can mark "required")
  const LABELISH = 'label, legend, [class*="label" i], [class*="question" i], [class*="title" i]';
  const CONTROLS = 'input:not([type=hidden]), textarea, select, button';

  const nearbyLabel = (start, skip) => {
    let node = start;
    for (let i = 0; i < 6 && node && node !== document.body; i++, node = node.parentElement) {
      for (const cand of node.querySelectorAll(LABELISH)) {
        if (skip.some(s => cand === s || cand.contains(s) || s.contains(cand))) continue;
        if (cand.querySelector(CONTROLS)) continue;
        const t = raw(cand);
        if (t && t.length < 400) { lastLabelEl = cand; return t; }
      }
    }
    return '';
  };
  const labelOf = el => {
    lastLabelEl = (el.labels && el.labels[0]) || el.closest('label') || null;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(i => raw(document.getElementById(i))).join(' ').trim(); if (t) return t; }
    if (el.labels && el.labels.length) { const t = raw(el.labels[0]); if (t) return t; }
    const wrap = el.closest('label'); if (wrap && raw(wrap)) return raw(wrap);
    return nearbyLabel(el.parentElement, [el]) || el.getAttribute('aria-label') || el.placeholder || el.name || '';
  };
  const optionLabel = el => {
    if (el.labels && el.labels.length && raw(el.labels[0])) return raw(el.labels[0]);
    const wrap = el.closest('label'); if (wrap && raw(wrap)) return raw(wrap);
    return el.getAttribute('aria-label') || el.value || '';
  };
  const groupLabel = els => {
    const fs = els[0].closest('fieldset');
    if (fs && fs.querySelector('legend') && els.every(e => fs.contains(e))) return raw(fs.querySelector('legend'));
    let anc = els[0].parentElement;
    while (anc && !els.every(e => anc.contains(e))) anc = anc.parentElement;
    const skip = els.flatMap(e => [e, ...(e.labels ? [...e.labels] : []), e.closest('label')].filter(Boolean));
    return nearbyLabel(anc, skip) || (anc && anc.getAttribute('aria-label')) || els[0].name || '';
  };
  const reqLabel = labelRaw => /\*\s*$|✱|\(required\)/i.test(labelRaw) || !!(lastLabelEl && /required/i.test(lastLabelEl.getAttribute('class') || ''));
  const isReq = (el, labelRaw) => el.required || el.getAttribute('aria-required') === 'true' || reqLabel(labelRaw);
  // Upload widgets label the button ("Attach", "Upload File"), not the question; look at the enclosing group first.
  const fileLabel = el => {
    const grp = el.closest('[role="group"][aria-labelledby]');
    if (grp) { const t = raw(document.getElementById(grp.getAttribute('aria-labelledby'))); if (t) { lastLabelEl = null; return t; } }
    const l = labelOf(el);
    const m = l.match(/^(.*?)\s*✱?\s*(attach|upload)\b.*$/i);
    if (m && m[1]) return m[1] + (l.includes('✱') ? ' *' : '');
    if (/^(attach|upload|choose|browse)\b/i.test(l) || !l) return nearbyLabel(el.parentElement && el.parentElement.parentElement, [el]) || el.id || el.name || l;
    return l;
  };

  const out = [], groups = {};
  for (const el of root.querySelectorAll('input, textarea, select')) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (el.disabled || el.getAttribute('aria-hidden') === 'true') continue;
    if (type === 'file') {
      if (el.closest('[class*="autofill" i]')) continue;  // "autofill from resume" helpers are not form fields
      const l = fileLabel(el);
      out.push({id: tag(el), kind: 'file', label: clean(l), required: isReq(el, l), options: [], accept: el.accept || ''});
      continue;
    }
    if (type === 'radio' || type === 'checkbox') {
      const key = type + ':' + (el.name || tag(el));
      (groups[key] = groups[key] || []).push(el);
      continue;
    }
    if (!vis(el)) continue;
    const l = labelOf(el);
    if (el.tagName === 'SELECT') {
      const opts = [...el.options].filter(o => o.value !== '' && !o.disabled).map(o => raw(o));
      out.push({id: tag(el), kind: 'select', label: clean(l), required: isReq(el, l), options: opts, value: el.selectedIndex > 0 ? raw(el.selectedOptions[0]) : ''});
    } else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list' || el.getAttribute('aria-haspopup') === 'listbox') {
      out.push({id: tag(el), kind: 'combobox', label: clean(l), required: isReq(el, l), options: [], value: el.value || ''});
    } else {
      out.push({id: tag(el), kind: el.tagName === 'TEXTAREA' ? 'textarea' : 'text', input_type: type, label: clean(l), required: isReq(el, l), options: [], value: el.value || '', maxlength: el.maxLength > 0 ? el.maxLength : null});
    }
  }
  for (const [key, els] of Object.entries(groups)) {
    const shown = els.filter(e => vis(e) || (e.labels && e.labels.length && vis(e.labels[0])) || (e.closest('label') && vis(e.closest('label'))));
    if (!shown.length) continue;
    const isRadio = key.startsWith('radio:');
    if (!isRadio && shown.length === 1) {
      lastLabelEl = null;
      const l = optionLabel(shown[0]);
      out.push({id: tag(shown[0]), kind: 'checkbox', label: clean(l), required: isReq(shown[0], l), options: [], value: shown[0].checked ? 'true' : ''});
      continue;
    }
    lastLabelEl = null;
    const l = groupLabel(shown);
    out.push({
      id: tag(shown[0]), kind: isRadio ? 'radio' : 'checkbox_group', label: clean(l),
      required: reqLabel(l) || shown.some(e => e.required || e.getAttribute('aria-required') === 'true'), options: shown.map(e => clean(optionLabel(e))),
      option_ids: shown.map(tag), value: shown.filter(e => e.checked).map(e => clean(optionLabel(e))).join('; '),
    });
  }
  // Yes/No style button pairs (Ashby renders boolean questions this way).
  const seen = new Set();
  for (const btn of root.querySelectorAll('button')) {
    if (!/^(yes|no)$/i.test(raw(btn)) || !vis(btn)) continue;
    const box = btn.parentElement;
    if (!box || seen.has(box)) continue;
    const btns = [...box.querySelectorAll(':scope > button')].filter(vis);
    if (btns.length < 2 || btns.length > 4) continue;
    seen.add(box);
    lastLabelEl = null;
    const l = nearbyLabel(box.parentElement, btns);
    out.push({id: tag(box), kind: 'button_group', label: clean(l), required: reqLabel(l), options: btns.map(raw), option_ids: btns.map(tag), value: ''});
  }
  return out;
}
"""


@dataclass
class Field:
    id: str
    kind: str  # text | textarea | select | combobox | radio | checkbox | checkbox_group | button_group | file
    label: str
    required: bool = False
    options: list[str] = field(default_factory=list)
    option_ids: list[str] = field(default_factory=list)
    value: str = ""
    input_type: str = ""
    accept: str = ""
    maxlength: Optional[int] = None

    @property
    def key(self) -> str:
        return question_key(self.label) + "|" + self.kind

    @property
    def is_choice(self) -> bool:
        return self.kind in ("select", "radio", "checkbox_group", "button_group") or (self.kind == "combobox" and bool(self.options))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def question_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()[:300]


def _sel(aa_id: str) -> str:
    return f'[data-aa-id="{aa_id}"]'


def extract_fields(page, root_selector: str | None = None, probe_comboboxes: bool = True) -> list[Field]:
    raw = page.evaluate(EXTRACT_JS, root_selector)
    fields = [Field(**{k: v for k, v in f.items() if k in Field.__dataclass_fields__}) for f in raw]
    if probe_comboboxes:
        for f in fields:
            if f.kind == "combobox":
                f.options = _read_combobox_options(page, f)
    return fields


OPTION_SEL = '[role="option"]:visible'


def _read_combobox_options(page, f: Field) -> list[str]:
    """Open a custom dropdown to see its choices. Typeahead fields return [] until typed into."""
    loc = page.locator(_sel(f.id))
    try:
        loc.scroll_into_view_if_needed(timeout=3000)
        loc.click(timeout=3000)
        page.wait_for_timeout(300)
        opts = [o.strip() for o in page.locator(OPTION_SEL).all_inner_texts()[:300] if o.strip()]
        page.keyboard.press("Escape")
        return opts
    except Exception:
        return []


_YES = re.compile(r"^\s*(yes|true|i am|i do|i have|i will)\b", re.I)
_NO = re.compile(r"^\s*(no|false|i am not|i do not|i don'?t|i have not|i will not)\b", re.I)


def choose_option(options: list[str], desired: str) -> Optional[str]:
    """Map a desired answer onto one of a field's options. None when nothing is close."""
    if not options or desired is None:
        return None
    want = str(desired).strip().lower()
    if not want:
        return None
    lowered = [o.strip().lower() for o in options]
    if want in lowered:
        return options[lowered.index(want)]
    if want in ("yes", "true", "no", "false"):
        pattern = _YES if want in ("yes", "true") else _NO
        hits = [o for o in options if pattern.search(o)]
        if len(hits) == 1:
            return hits[0]
        return None
    starts = [o for o, l in zip(options, lowered) if l.startswith(want) or want.startswith(l)]
    if len(starts) == 1:
        return starts[0]
    if len(starts) > 1 and all(l.startswith(want) for o, l in zip(options, lowered) if o in starts):
        return min(starts, key=len)  # "United States" -> "United States +1", not "... Minor Outlying Islands"
    contains = [o for o, l in zip(options, lowered) if want in l]
    if len(contains) == 1:
        return contains[0]
    close = difflib.get_close_matches(want, lowered, n=1, cutoff=0.75)
    return options[lowered.index(close[0])] if close else None


class FillError(Exception):
    pass


def fill_field(page, f: Field, value: Any) -> None:
    """Set one control. Raises FillError when the value can't be applied."""
    loc = page.locator(_sel(f.id))
    if f.kind in ("text", "textarea"):
        text = str(value)
        if f.maxlength:
            text = text[: f.maxlength]
        loc.scroll_into_view_if_needed()
        loc.fill(text)
    elif f.kind == "select":
        choice = choose_option(f.options, str(value))
        if choice is None:
            raise FillError(f"no option like {value!r}")
        loc.select_option(label=choice)
    elif f.kind in ("radio", "button_group"):
        choice = choose_option(f.options, str(value))
        if choice is None:
            raise FillError(f"no option like {value!r}")
        target = page.locator(_sel(f.option_ids[f.options.index(choice)]))
        if f.kind == "radio":
            _check(page, target)
        else:
            target.click()
    elif f.kind == "checkbox":
        if _truthy(value):
            _check(page, loc)
    elif f.kind == "checkbox_group":
        wanted = value if isinstance(value, list) else [v.strip() for v in str(value).split(";") if v.strip()]
        picked = 0
        for w in wanted:
            choice = choose_option(f.options, w)
            if choice is not None:
                _check(page, page.locator(_sel(f.option_ids[f.options.index(choice)])))
                picked += 1
        if wanted and not picked:
            raise FillError(f"no option like {value!r}")
    elif f.kind == "file":
        loc.set_input_files(str(value))
    elif f.kind == "combobox":
        _fill_combobox(page, loc, f, str(value))
    else:
        raise FillError(f"unknown field kind {f.kind}")


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "yes", "1", "checked", "on")


def _check(page, target) -> None:
    """Check a radio/checkbox; fall back to clicking its label when the input is visually hidden."""
    try:
        target.check(timeout=3000)
    except Exception:
        aa_id = target.get_attribute("data-aa-id")
        clicked = page.evaluate(
            """id => { const el = document.querySelector(`[data-aa-id="${id}"]`);
                 const lab = (el.labels && el.labels[0]) || el.closest('label');
                 if (el.checked) return true; (lab || el).click(); return el.checked; }""", aa_id)
        if not clicked:
            raise FillError("could not check option")


def _fill_combobox(page, loc, f: Field, value: str) -> None:
    loc.scroll_into_view_if_needed()
    known = choose_option(f.options, value) if f.options else None
    query = known or value
    loc.click()
    try:
        loc.fill(query[:60])
    except Exception:
        page.keyboard.type(query[:60])
    try:
        page.locator(OPTION_SEL).first.wait_for(timeout=4000)
    except Exception:
        page.keyboard.press("Escape")
        raise FillError(f"no dropdown options for {value!r}")
    shown = [o.strip() for o in page.locator(OPTION_SEL).all_inner_texts()[:100]]
    pick = choose_option(shown, query)
    if pick is None and not f.options and shown:
        pick = shown[0]  # typeahead (e.g. city search): take the top suggestion
    if pick is None:
        page.keyboard.press("Escape")
        raise FillError(f"no option like {value!r}")
    page.locator(OPTION_SEL).nth(shown.index(pick)).click()
