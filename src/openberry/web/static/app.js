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

  function openDetails(id) {
    const details = document.getElementById(id);
    if (!details || details.tagName !== "DETAILS") return false;
    details.open = true;
    details.scrollIntoView({ behavior: "smooth", block: "start" });
    const field = $("input, select, textarea", details);
    if (field) field.focus({ preventScroll: true });
    return true;
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
      return boxes.filter((b) => b.checked).map((b) => b.closest("label").textContent.trim()).join(", ");
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
    initNav();
    initAutosubmit();
    initConfirm();
    initDismiss();
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
