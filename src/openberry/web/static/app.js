/* OpenBerry dashboard enhancements. Every page works without this file; it adds the
   registration wizard, website auto-fill, copy buttons, scan polling and small conveniences.
   User data is only ever inserted with textContent. */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const csrfToken = () => {
    const meta = $('meta[name="csrf-token"]');
    return meta ? meta.content : "";
  };

  /* ---------------------------------------------------------------- mobile navigation */
  function initNav() {
    const toggle = $("[data-nav-toggle]");
    if (!toggle) return;
    const setOpen = (open) => {
      document.body.classList.toggle("nav-open", open);
      toggle.setAttribute("aria-expanded", String(open));
      if (open) {
        const first = $("#sidebar a, #sidebar select");
        if (first) first.focus();
      }
    };
    toggle.addEventListener("click", () => setOpen(!document.body.classList.contains("nav-open")));
    $$("[data-nav-close]").forEach((el) => el.addEventListener("click", () => { setOpen(false); toggle.focus(); }));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && document.body.classList.contains("nav-open")) { setOpen(false); toggle.focus(); }
    });
  }

  /* ---------------------------------------------------------------- small conveniences */
  function initAutosubmit() {
    $$("select[data-autosubmit]").forEach((sel) => {
      sel.addEventListener("change", () => { if (sel.form) sel.form.requestSubmit ? sel.form.requestSubmit() : sel.form.submit(); });
    });
  }

  function initConfirm() {
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-confirm]");
      if (btn && !window.confirm(btn.getAttribute("data-confirm"))) {
        e.preventDefault();
        e.stopImmediatePropagation();
      }
    }, true);
  }

  function initDismiss() {
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-dismiss]");
      if (btn && btn.parentElement) btn.parentElement.remove();
    });
  }

  // Success and info messages fade after a few seconds (not while hovered or focused);
  // errors and warnings stay until the user closes them.
  function initFlashes() {
    const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    document.querySelectorAll(".flashes .flash-success, .flashes .flash-info").forEach((el) => {
      let timer = null;
      const leave = () => {
        if (el.matches(":hover") || el.contains(document.activeElement)) { start(); return; }
        if (reduce) { el.remove(); return; }
        el.classList.add("is-leaving");
        setTimeout(() => el.remove(), 260);
      };
      const start = () => { clearTimeout(timer); timer = setTimeout(leave, 6000); };
      el.addEventListener("mouseenter", () => clearTimeout(timer));
      el.addEventListener("mouseleave", start);
      start();
    });
  }

  function openDetails(id) {
    const details = document.getElementById(id);
    if (!details || details.tagName !== "DETAILS") return false;
    details.open = true;
    details.scrollIntoView({ behavior: "smooth", block: "start" });
    const field = $("input, select, textarea", details);
    if (field) field.focus({ preventScroll: true });
    return true;
  }

  // Keep your place across a reload. Tabs (.tabs a) and links or forms marked data-keep-scroll load the
  // same page again; without this the browser starts at the top. Just before leaving, remember where the
  // list card acted on (else the page's [data-scroll-anchor], else the page itself) was on screen; if the next
  // page is the one the action was going to, put it back there. early.js hides the content until then (same
  // rule as keepScrollApplies), so the top of the page doesn't flash first.
  const KEEP_SCROLL_KEY = "openberry-keep-scroll";
  const KEEP_SCROLL_MS = 15000;

  function keepScrollApplies(state) {
    const age = state ? Date.now() - state.at : -1;
    return !!state && state.to === location.pathname + location.search && !location.hash
      && age >= 0 && age < KEEP_SCROLL_MS;
  }

  // The top of what the user can see: below the sticky top bar on a phone.
  function screenTop() {
    const bar = $(".topbar");
    return bar ? Math.max(0, bar.getBoundingClientRect().bottom) : 0;
  }

  function byKeyboard() {
    const el = document.activeElement;
    try { return !!el && el !== document.body && el.matches(":focus-visible"); } catch (err) { return false; }
  }

  function rememberScroll(to, focus, leaving, card) {
    const anchor = $("[data-scroll-anchor]");
    // List items the action takes away (the one acted on, the ticked drafts) leave a gap after the reload. The part
    // of it above the screen would pull items not read yet out of view, so the page comes back that much higher.
    const top = screenTop();
    let removed = 0;
    (leaving || []).forEach((li) => {
      const next = li.nextElementSibling;
      const end = next ? next.getBoundingClientRect().top : li.getBoundingClientRect().bottom;
      removed += Math.max(0, Math.min(end, top) - li.getBoundingClientRect().top);
    });
    // The card acted on keeps its place on screen (the next one takes it when it leaves), even if cards above it
    // changed meanwhile: auto-approve approves due drafts as the page loads.
    let pin = card || null;
    while (pin && (leaving || []).includes(pin)) pin = pin.nextElementSibling;
    const state = {
      to: to, at: Date.now(), focus: focus || null, y: window.scrollY - removed,
      top: anchor ? anchor.getBoundingClientRect().top + removed : null,
      pin: pin && pin.id ? pin.id : null, pinTop: card ? card.getBoundingClientRect().top + removed : null,
    };
    try { sessionStorage.setItem(KEEP_SCROLL_KEY, JSON.stringify(state)); } catch (err) { /* storage blocked */ }
  }

  function restoreScroll() {
    let state = null;
    try {
      const raw = sessionStorage.getItem(KEEP_SCROLL_KEY);
      sessionStorage.removeItem(KEEP_SCROLL_KEY);
      state = JSON.parse(raw || "null");
    } catch (err) { return; }
    // Another page, an old or abandoned click and a #section link (it decides where to land) start as usual,
    // and so does a form shown again with errors: they are listed at its top.
    if (!keepScrollApplies(state) || $(".form-alert")) return;
    const anchor = $("[data-scroll-anchor]");
    const pin = state.pin && typeof state.pinTop === "number" ? document.getElementById(state.pin) : null;
    let y = state.y;
    if (pin) y = pin.getBoundingClientRect().top + window.scrollY - state.pinTop;
    else if (anchor && typeof state.top === "number") {
      y = anchor.getBoundingClientRect().top + window.scrollY - state.top;
    }
    if (typeof y !== "number" || !isFinite(y)) return;
    y = Math.max(0, y);
    // A short page (an empty tab) would stop above y and move the tabs: give it room below instead.
    const main = $(".main");
    const short = y - (document.documentElement.scrollHeight - window.innerHeight);
    // Only to keep on screen what was on screen: tabs scrolled out of view would leave an empty screen.
    if (short > 0 && main && (pin || typeof state.top !== "number" || state.top >= 0)) {
      main.style.paddingBottom = (parseFloat(getComputedStyle(main).paddingBottom) || 0) + Math.ceil(short) + "px";
    }
    window.scrollTo(0, y);
    if (!state.focus) return;
    // From the keyboard, the next Tab goes on from where the user was, not from the top of the page.
    let target = null;
    if (state.focus === "tab") {
      target = $('.tabs a[aria-current="page"]');
    } else {
      // Hold and "Let it auto-approve" replace each other: the one now in its place takes the focus.
      const swap = { hold: "release", release: "hold" }[state.focus.value];
      const buttons = $$("form[data-keep-scroll]").filter((f) => f.getAttribute("action") === state.focus.action)
        .flatMap((f) => Array.from(f.elements).filter((b) => b.type === "submit"));
      target = buttons.find((b) => b.value === state.focus.value) || buttons.find((b) => swap && b.value === swap);
      if (!target) {
        const li = pin || $$(".queue > li").find((el) => el.getBoundingClientRect().bottom > screenTop());
        target = li && $("input:not([type=hidden]), a[href], button:not([disabled])", li);
      }
    }
    if (target) target.focus({ preventScroll: true });
  }

  // A phone shows only part of the tab strip: bring the current tab into it (scrollIntoView would move the page).
  function showCurrentTab() {
    const strip = $(".tabs");
    const current = $('.tabs a[aria-current="page"]');
    if (!strip || !current) return;
    const s = strip.getBoundingClientRect();
    const c = current.getBoundingClientRect();
    if (c.left < s.left || c.right > s.right) strip.scrollLeft += c.left - s.left - (s.width - c.width) / 2;
  }

  function initKeepScroll() {
    document.documentElement.classList.remove("keep-scroll");  // set by early.js; hidden things can't take focus
    showCurrentTab();
    restoreScroll();
    document.addEventListener("click", (e) => {
      const link = e.target.closest(".tabs a[href], a[data-keep-scroll]");
      if (!link || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      if (link.target && link.target !== "_self") return;
      // A tab opened from the keyboard (a click with detail 0) gets the focus back.
      rememberScroll(link.pathname + link.search, e.detail === 0 && link.matches(".tabs a") ? "tab" : null);
    });
    document.addEventListener("submit", (e) => {
      const form = e.target;
      if (e.defaultPrevented || !form.matches("form[data-keep-scroll]")) return;
      // Where the server sends the user back: the form's "next" (a #section there wins), else this page.
      const next = form.querySelector('input[name="next"]');
      let to = location.pathname + location.search;
      if (next && next.value) {
        try { const url = new URL(next.value, location.href); to = url.pathname + url.search; } catch (err) { /* keep */ }
      }
      // Cards the action takes off the list: the ticked drafts, or the card of a form marked data-keep-scroll="remove".
      let leaving = [];
      const card = form.closest(".queue > li");
      if (form.matches("[data-bulk]")) leaving = $$(".queue > li").filter((li) => $("[data-bulk-item]:checked", li));
      else if (card && form.getAttribute("data-keep-scroll") === "remove") leaving = [card];
      const submitter = e.submitter || form.querySelector("button[type=submit], button:not([type])");
      const focus = byKeyboard() ? { action: form.getAttribute("action"), value: submitter ? submitter.value : "" } : null;
      rememberScroll(to, focus, leaving, card);
    });
  }

  // Outreach Drafts tab: tick drafts, then approve or skip them together. Without this script the
  // checkboxes and buttons still work; it adds "Select all", the live count and the confirm text.
  function initBulk() {
    const form = $("form[data-bulk]");
    if (!form) return;
    const items = $$('input[data-bulk-item]').filter((el) => el.form === form);
    const all = $("[data-bulk-all]", form);
    const count = $("[data-bulk-count]", form);
    const buttons = $$("[data-bulk-action]", form);
    const drafts = (n) => n + (n === 1 ? " draft" : " drafts");
    const update = () => {
      const n = items.filter((el) => el.checked).length;
      if (all) {
        all.checked = n > 0 && n === items.length;
        all.indeterminate = n > 0 && n < items.length;
      }
      count.textContent = n ? drafts(n) + " selected" : "Tick drafts to approve or skip them together.";
      buttons.forEach((btn) => {
        const approve = btn.getAttribute("data-bulk-action") === "approve";
        btn.disabled = n === 0;
        $("[data-bulk-label]", btn).textContent = (approve ? "Approve" : "Skip") + " selected" + (n ? " (" + n + ")" : "");
        btn.setAttribute("data-confirm", approve
          ? "Approve " + drafts(n) + " exactly as written? Approved LinkedIn messages can be sent by your AI agent if it is on."
          : "Skip " + drafts(n) + "? They leave the queue and won't be sent.");
      });
    };
    if (all) {
      $$("[data-bulk-js]", form).forEach((el) => { el.hidden = false; });
      all.addEventListener("change", () => {
        items.forEach((el) => { el.checked = all.checked; });
        update();
      });
    }
    items.forEach((el) => el.addEventListener("change", update));
    // Against double submits; after the browser has read the clicked button's action (a disabled one isn't sent).
    form.addEventListener("submit", () => setTimeout(() => buttons.forEach((btn) => { btn.disabled = true; }), 0));
    // Restore after the browser's back button, which can bring back ticked boxes.
    window.addEventListener("pageshow", update);
    update();
  }

  function initDetailsLinks() {
    $$("[data-open-details]").forEach((link) => {
      link.addEventListener("click", (e) => {
        if (openDetails(link.getAttribute("data-open-details"))) e.preventDefault();
      });
    });
    if (location.hash.length > 1) {
      // A malformed hash ("#%E0%A4") makes decodeURIComponent throw, which would abort init().
      try { openDetails(decodeURIComponent(location.hash.slice(1))); } catch (_) { /* not a details id */ }
    }
  }

  /* ---------------------------------------------------------------- copy to clipboard */
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
      area.remove();
      return ok;
    }
  }

  function initCopy() {
    document.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-copy]");
      if (!btn) return;
      e.preventDefault();
      const target = document.querySelector(btn.getAttribute("data-copy"));
      if (!target) return;
      const text = ("value" in target && target.tagName !== "BUTTON" ? target.value : target.textContent).trim();
      const ok = await copyText(text);
      const label = $("[data-copy-label]", btn);
      const original = label ? label.textContent : "";
      btn.classList.toggle("is-done", ok);
      if (label) label.textContent = ok ? "Copied" : "Press Ctrl+C";
      setTimeout(() => {
        btn.classList.remove("is-done");
        if (label) label.textContent = original;
      }, 1600);
    });
  }

  /* ---------------------------------------------------------------- character counters */
  function initCounters() {
    $$("textarea[data-maxlen]").forEach((area) => {
      const out = document.getElementById(area.getAttribute("data-counter"));
      const max = parseInt(area.getAttribute("data-maxlen"), 10);
      if (!out || !max) return;
      const update = () => {
        const n = area.value.length;
        out.textContent = n + " / " + max + (n > max ? ": too long for a connection note" : "");
        out.classList.toggle("over", n > max);
      };
      area.addEventListener("input", update);
      update();
    });
  }

  /* ---------------------------------------------------------------- scan status polling */
  function initScanPoll() {
    const el = $("[data-scan-poll]");
    if (!el) return;
    const url = el.getAttribute("data-scan-poll");
    const tick = async () => {
      try {
        const res = await fetch(url, {
          headers: { Accept: "application/json", "X-CSRF-Token": csrfToken() },
          credentials: "same-origin",
        });
        if (res.ok) {
          const data = await res.json();
          if (!data.running) { location.reload(); return; }
        }
      } catch (_) { /* network blip: try again */ }
      setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
  }

  /* ---------------------------------------------------------------- website auto-fill */
  function fieldLabel(input) {
    const wrap = input.closest("[data-label]");
    return wrap ? wrap.getAttribute("data-label") : input.name;
  }

  function initAutofill() {
    $$("[data-autofill]").forEach((btn) => {
      const form = btn.form;
      const status = $("#autofill-status", form);
      const setStatus = (text, isError) => {
        if (!status) return;
        status.textContent = text;
        status.classList.toggle("is-error", Boolean(isError));
      };
      btn.addEventListener("click", async () => {
        const urlInput = form.elements.namedItem("website");
        const url = urlInput ? urlInput.value.trim() : "";
        if (!url) { setStatus("Enter your website address first.", true); if (urlInput) urlInput.focus(); return; }
        btn.disabled = true;
        setStatus("Reading " + url + " …");
        try {
          const res = await fetch(btn.getAttribute("data-autofill"), {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": csrfToken() },
            body: JSON.stringify({ url: url }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            throw new Error(typeof data.detail === "string" ? data.detail : "Could not read that website.");
          }
          const filled = [];
          Object.entries(data.suggestions || {}).forEach(([name, value]) => {
            const input = form.elements.namedItem(name);
            if (!input || !("value" in input) || !value || input.value.trim()) return;
            input.value = value;
            input.classList.add("autofilled");
            filled.push(fieldLabel(input));
          });
          setStatus(filled.length
            ? "Filled " + filled.join(", ") + ". Please check them."
            : "Nothing new to fill: those fields already have values.");
        } catch (err) {
          setStatus(err.message || "Could not read that website.", true);
        } finally {
          btn.disabled = false;
        }
      });
    });
  }

  /* ---------------------------------------------------------------- registration wizard */
  function controls(step) {
    return $$("input, select, textarea", step).filter((el) => !el.disabled && el.type !== "hidden");
  }

  function firstInvalid(step) {
    return controls(step).find((el) => !el.checkValidity());
  }

  function valueOf(field) {
    const boxes = $$('input[type="checkbox"]', field);
    if (boxes.length) {
      // A checkbox card (one yes/no option) shows its title, not its description.
      const text = (b) => {
        const label = b.closest("label");
        const title = label && $(".radio-title", label);
        return (title || label).textContent.trim();
      };
      return boxes.filter((b) => b.checked).map(text).join(", ");
    }
    const radios = $$('input[type="radio"]', field);
    if (radios.length) {
      const chosen = radios.find((r) => r.checked);
      const title = chosen && $(".radio-title", chosen.closest("label"));
      return title ? title.textContent.trim() : "";
    }
    const control = $("input, select, textarea", field);
    if (!control) return "";
    if (control.tagName === "SELECT") {
      return control.value ? control.options[control.selectedIndex].textContent.trim() : "";
    }
    return control.value.trim();
  }

  function buildReview(steps, out, goTo) {
    out.textContent = "";
    steps.filter((s) => s.getAttribute("data-step") !== "review").forEach((step, index) => {
      const section = document.createElement("section");
      section.className = "review-section";
      const head = document.createElement("h3");
      const legend = $("legend", step);
      const kicker = legend ? $(".step-kicker", legend) : null;
      head.textContent = legend ? legend.textContent.replace(kicker ? kicker.textContent : "", "").trim() : "";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "btn btn-sm btn-ghost";
      edit.textContent = "Edit";
      edit.addEventListener("click", () => goTo(index));
      head.appendChild(edit);
      section.appendChild(head);
      const list = document.createElement("dl");
      $$(".field[data-label]", step).forEach((field) => {
        const value = valueOf(field);
        if (!value) return;
        const dt = document.createElement("dt");
        dt.textContent = field.getAttribute("data-label");
        const dd = document.createElement("dd");
        dd.textContent = value;
        list.append(dt, dd);
      });
      if (!list.children.length) {
        const none = document.createElement("p");
        none.className = "muted small";
        none.textContent = "Nothing filled in.";
        section.appendChild(none);
      } else {
        section.appendChild(list);
      }
      out.appendChild(section);
    });
  }

  function initWizard(form) {
    const steps = $$("[data-step]", form);
    if (steps.length < 2) return;
    const keys = steps.map((s) => s.getAttribute("data-step"));
    const dots = $$("[data-step-to]", form);
    const back = $("[data-wizard-back]", form);
    const next = $("[data-wizard-next]", form);
    const submit = $("[data-wizard-submit]", form);
    const count = $("[data-wizard-count]", form);
    const review = $("[data-review]", form);
    const last = steps.length - 1;
    let current = Math.max(0, keys.indexOf(form.getAttribute("data-first-step") || keys[0]));

    form.noValidate = true;  // validated per step below; hidden steps would block native validation
    steps.forEach((s) => s.setAttribute("tabindex", "-1"));

    function show(index, focus) {
      current = Math.max(0, Math.min(last, index));
      steps.forEach((s, i) => { s.hidden = i !== current; });
      dots.forEach((dot, i) => {
        dot.classList.toggle("is-done", i < current);
        if (i === current) dot.setAttribute("aria-current", "step");
        else dot.removeAttribute("aria-current");
      });
      back.hidden = current === 0;
      next.hidden = current === last;
      submit.hidden = current !== last;
      if (count) count.textContent = "Step " + (current + 1) + " of " + steps.length;
      if (review && keys[current] === "review") buildReview(steps, review, (i) => show(i, true));
      if (focus) {
        steps[current].focus({ preventScroll: true });
        const top = form.getBoundingClientRect().top + window.scrollY - 16;
        if (window.scrollY > top) window.scrollTo({ top: top, behavior: "smooth" });
      }
    }

    function advanceTo(target) {
      for (let i = current; i < target; i++) {
        const bad = firstInvalid(steps[i]);
        if (bad) { show(i, false); bad.reportValidity(); bad.focus(); return; }
      }
      show(target, true);
    }

    // Error summary links point at fields on other, hidden steps: open that step, then the field.
    $$(".form-alert a[data-error-step]", form).forEach((link) => {
      let target = null;
      try { target = form.querySelector(link.getAttribute("href")); } catch (_) { target = null; }
      const wrap = target && target.closest("[data-label]");
      const where = $(".error-where", link);
      if (wrap && where) where.textContent = where.textContent.replace(/:$/, "") + " › " + wrap.getAttribute("data-label") + ":";
      link.addEventListener("click", (e) => {
        const index = target ? steps.findIndex((s) => s.contains(target)) : keys.indexOf(link.getAttribute("data-error-step"));
        if (index < 0) return;
        e.preventDefault();
        show(index, false);
        const field = target && (target.matches("input, select, textarea") ? target : $("input, select, textarea", target));
        if (field) {
          field.focus({ preventScroll: true });
          field.scrollIntoView({ behavior: "smooth", block: "center" });
        } else {
          steps[index].focus();
        }
      });
    });

    next.addEventListener("click", () => advanceTo(current + 1));
    back.addEventListener("click", () => show(current - 1, true));
    dots.forEach((dot, i) => dot.addEventListener("click", () => (i <= current ? show(i, true) : advanceTo(i))));

    form.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && current < last && e.target.tagName === "INPUT" && e.target.type !== "checkbox") {
        e.preventDefault();
        advanceTo(current + 1);
      }
    });

    form.addEventListener("submit", (e) => {
      for (let i = 0; i < steps.length; i++) {
        const bad = firstInvalid(steps[i]);
        if (bad) {
          e.preventDefault();
          show(i, false);
          bad.reportValidity();
          bad.focus();
          return;
        }
      }
      submit.disabled = true;
    });

    show(current, false);
  }

  function init() {
    initKeepScroll();
    initNav();
    initAutosubmit();
    initConfirm();
    initDismiss();
    initFlashes();
    initBulk();
    initDetailsLinks();
    initCopy();
    initCounters();
    initScanPoll();
    initAutofill();
    $$("form[data-wizard]").forEach(initWizard);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
