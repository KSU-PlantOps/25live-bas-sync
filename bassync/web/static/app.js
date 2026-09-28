// 25Live -> BAS sync web UI. Progressive enhancement only: every page works
// without it. Served from this origin; the Content-Security-Policy forbids
// inline script.
(function () {
  "use strict";
  // Confirm before submitting forms that change something big.
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    var msg = form.getAttribute("data-confirm");
    var forced = form.querySelector("[data-confirm-checked]:checked");
    if (forced && !window.confirm(forced.getAttribute("data-confirm-checked"))) {
      ev.preventDefault(); return;
    }
    if (msg && !window.confirm(msg)) { ev.preventDefault(); }
  });

  // Filter a table's rows as you type, and by campus where there is a picker.
  document.querySelectorAll("input[data-filter]").forEach(function (box) {
    var id = box.getAttribute("data-filter");
    var table = document.getElementById(id);
    if (!table) return;
    var campus = document.querySelector('select[data-campus-filter="' + id + '"]');
    var apply = function () {
      var q = box.value.trim().toLowerCase();
      var c = campus ? campus.value : "";
      table.querySelectorAll("tbody tr").forEach(function (tr) {
        var text = !q || tr.textContent.toLowerCase().indexOf(q) !== -1;
        var place = !c || tr.getAttribute("data-campus") === c;
        tr.hidden = !(text && place);
      });
    };
    box.addEventListener("input", apply);
    if (campus) campus.addEventListener("change", apply);
  });

  // Sort by a column: numbers numerically, everything else as text.
  document.querySelectorAll("th[data-sort]").forEach(function (th) {
    th.addEventListener("click", function () {
      var table = th.closest("table"), body = table.tBodies[0];
      var col = Array.prototype.indexOf.call(th.parentNode.children, th);
      var asc = th.getAttribute("aria-sort") !== "ascending";
      table.querySelectorAll("th[aria-sort]").forEach(function (h) { h.removeAttribute("aria-sort"); });
      th.setAttribute("aria-sort", asc ? "ascending" : "descending");
      var rows = Array.prototype.slice.call(body.rows);
      rows.sort(function (a, b) {
        var x = (a.cells[col] || {}).textContent || "", y = (b.cells[col] || {}).textContent || "";
        x = x.trim(); y = y.trim();
        var nx = parseFloat(x), ny = parseFloat(y);
        var r = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : x.localeCompare(y, undefined, {numeric: true});
        return asc ? r : -r;
      });
      rows.forEach(function (r) { body.appendChild(r); });
    });
  });

  // Picking another driver shows its settings (nothing is saved yet).
  document.querySelectorAll("select[data-driver-switch]").forEach(function (sel) {
    sel.addEventListener("change", function () {
      window.location = sel.getAttribute("data-driver-switch") + "&driver=" + encodeURIComponent(sel.value);
    });
  });

  // Follow a running job's output.
  var out = document.getElementById("output");
  if (out && out.getAttribute("data-running")) {
    var next = parseInt(out.getAttribute("data-next") || "0", 10);
    var stick = true;
    out.scrollTop = out.scrollHeight;
    out.addEventListener("scroll", function () {
      stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 8;
    });
    var poll = function () {
      fetch(out.getAttribute("data-url") + "?since=" + next, {credentials: "same-origin"})
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (data) {
          if (data.lines.length) {
            out.appendChild(document.createTextNode((out.textContent ? "\n" : "") + data.lines.join("\n")));
            if (stick) out.scrollTop = out.scrollHeight;
          }
          next = data.next;
          if (data.running) { setTimeout(poll, 1000); } else { window.location.reload(); }
        })
        .catch(function () { setTimeout(poll, 5000); });
    };
    setTimeout(poll, 800);
  }
  // Radio groups that show one set of fields or another (the booking form's
  // one day / every week). Without script every field shows, which still works.
  document.querySelectorAll("input[data-toggles]").forEach(function (radio) {
    var name = radio.getAttribute("data-toggles");
    var apply = function () {
      var picked = document.querySelector("input[data-toggles='" + name + "']:checked");
      document.querySelectorAll("[data-show-for^='" + name + "=']").forEach(function (el) {
        el.hidden = !picked || el.getAttribute("data-show-for") !== name + "=" + picked.value;
      });
    };
    radio.addEventListener("change", apply);
    apply();
  });
  // The restart page: wait for the service to come back as a new process
  // (its boot id changes), then return.
  var restarting = document.getElementById("restarting");
  if (restarting) {
    var oldBoot = restarting.getAttribute("data-boot");
    var began = Date.now();
    var check = function () {
      fetch(restarting.getAttribute("data-health"), {cache: "no-store", credentials: "same-origin"})
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (data) {
          if (data.boot && data.boot !== oldBoot) {
            window.location.href = restarting.getAttribute("data-next");
          } else { setTimeout(check, 1000); }
        })
        .catch(function () { setTimeout(check, 1000); });
      if (Date.now() - began > 45000) {
        var slow = restarting.querySelector("[data-restart-slow]");
        if (slow) { slow.hidden = false; }
      }
    };
    setTimeout(check, 1500);
  }
  document.querySelectorAll("pre[data-scroll-end]").forEach(function (p) { p.scrollTop = p.scrollHeight; });

  // The setup guide: tick or untick every room the filter leaves showing.
  document.querySelectorAll("button[data-check-all], button[data-check-none]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var on = btn.hasAttribute("data-check-all");
      var table = document.getElementById(btn.getAttribute(on ? "data-check-all" : "data-check-none"));
      if (!table) return;
      table.querySelectorAll("tbody tr").forEach(function (tr) {
        if (tr.hidden) return;
        tr.querySelectorAll("input[type=checkbox]").forEach(function (box) { box.checked = on; });
      });
    });
  });
  // On a phone the steps are one row that scrolls: start it at this step.
  var here = document.querySelector(".stepper [aria-current]");
  if (here) {
    var row = here.closest("ol");
    if (row && row.scrollWidth > row.clientWidth) row.scrollLeft = here.offsetLeft - row.offsetLeft - 16;
  }
  // ...and suggest this browser's timezone when none is chosen yet.
  document.querySelectorAll("select[data-browser-zone]").forEach(function (sel) {
    if (sel.value) return;
    var zone = "";
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { return; }
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === zone) { sel.value = zone; return; }
    }
  });

  // The status page refreshes itself when a job starts or finishes.
  var status = document.querySelector("[data-status-url]");
  if (status) {
    var shown = status.getAttribute("data-job") || "";
    setInterval(function () {
      if (document.hidden) return;
      var active = document.activeElement;
      if (active && /INPUT|SELECT|TEXTAREA/.test(active.tagName)) return;
      fetch(status.getAttribute("data-status-url"), {credentials: "same-origin"})
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) return;
          var now = data.job ? data.job.id : "";
          if (now !== shown) window.location.reload();
        })
        .catch(function () {});
    }, 5000);
  }
})();
