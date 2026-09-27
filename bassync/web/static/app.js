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
  document.querySelectorAll("pre[data-scroll-end]").forEach(function (p) { p.scrollTop = p.scrollHeight; });

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
