const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let history = [],
  report = null,
  engine = 0,
  busy = false,
  cli = "";
async function api(path, body) {
  const r = await fetch(
    path,
    body
      ? {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : {},
  );
  const data = await r.json();
  if (!r.ok) throw Error(data.error || "Request failed");
  return data;
}
function status(text, kind = "") {
  $("status").textContent = text;
  $("status").className = "status " + kind;
}
async function operation(button, fn) {
  if (busy) return;
  busy = true;
  button.setAttribute("aria-busy", "true");
  const old = button.textContent;
  button.textContent = "Working…";
  document.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    await fn();
  } catch (e) {
    status(e.message, "error");
  } finally {
    busy = false;
    button.textContent = old;
    button.removeAttribute("aria-busy");
    document.querySelectorAll("button").forEach((b) => (b.disabled = false));
    $("copy").disabled = !cli;
  }
}
function label(s) {
  return `#${s.id} · ${s.mode === "live" ? "LIVE" : "TEST"} · ${s.fetched_at.slice(11, 19)} UTC${s.complete ? "" : " · incomplete"}`;
}
async function refresh() {
  const data = await api("/api/history");
  history = data.snapshots;
  $("usage").textContent = `${data.usage} / ${data.limit}`;
  $("budget").value = data.usage;
  for (const id of ["before", "after"]) {
    const selected = $(id).value;
    $(id).innerHTML =
      '<option value="">Choose snapshot</option>' +
      history
        .filter((s) => s.complete)
        .map((s) => `<option value="${s.id}">${esc(label(s))}</option>`)
        .join("");
    $(id).value = selected;
  }
  $("history").innerHTML = history.length
    ? history
        .map(
          (s) =>
            `<button data-snapshot="${s.id}"><strong>${esc(label(s))}</strong><span class="history-meta">${esc(s.watch.query.slice(0, 58))}</span></button>`,
        )
        .join("")
    : "<p>Capture a live observation to create your first baseline.</p>";
  $("history")
    .querySelectorAll("button")
    .forEach(
      (b) =>
        (b.onclick = () => {
          $("after").value = b.dataset.snapshot;
          status(
            "After snapshot selected. Choose a compatible before snapshot, then compare.",
          );
        }),
    );
}
function selectPair(a, b) {
  $("before").value = a;
  $("after").value = b;
}
function getPolicy() {
  return {
    fuzzy: $("fuzzy").checked,
    max_content: Number($("max-content").value),
    max_removed: Number($("max-removed").value),
    min_rbo: Number($("min-rbo").value),
  };
}
async function compareSelected() {
  if (!$("before").value || !$("after").value)
    throw Error("Choose both snapshots before comparing.");
  report = await api("/api/compare", {
    before: Number($("before").value),
    after: Number($("after").value),
    ...getPolicy(),
  });
  engine = 0;
  render();
  status(
    `Compared #${report.before_id} → #${report.after_id}. ${report.provenance.before === "live" ? "Live observations." : "CONTROLLED REPLAY: test injections, not historical events."}`,
    report.provenance.before === "live" ? "" : "test",
  );
}
function render() {
  if (!report) return;
  const totals = { content: 0, added: 0, removed: 0, rank_only: 0 };
  report.engines.forEach((e) =>
    Object.keys(totals).forEach((k) => (totals[k] += e.counts[k])),
  );
  const e = report.engines[engine];
  const metrics = [
    ["content", "Snippet changes"],
    ["added", "New sources"],
    ["removed", "Removed sources"],
    ["rank_only", "Rank-only moves"],
  ]
    .map(
      ([key, name]) =>
        `<div class="metric"><strong>${totals[key]}</strong><span>${name} · both engines</span></div>`,
    )
    .join("");
  const a = e.alignment;
  const height = 240;
  const y = (i, n) => 35 + (i * 180) / Math.max(n - 1, 1);
  const changed = new Set(
    e.changes.filter((c) => c.match).map((c) => c.match.before),
  );
  const flow = `<svg class="rank-svg" viewBox="0 0 340 ${height}" role="img" aria-label="Matched source ranks before and after"><text x="35" y="16">BEFORE</text><text x="235" y="16">AFTER</text>${a.matches.map((m) => `<line ${changed.has(m.before) ? 'class="changed"' : ""} x1="75" y1="${y(m.before, e.before_count)}" x2="265" y2="${y(m.after, e.after_count)}"><title>${esc(m.method)}: rank ${m.before + 1} → ${m.after + 1}</title></line>`).join("")}${Array.from({ length: e.before_count }, (_, i) => `<circle cx="75" cy="${y(i, e.before_count)}" r="3"/><text x="45" y="${y(i, e.before_count) + 4}">${i + 1}</text>`).join("")}${Array.from({ length: e.after_count }, (_, i) => `<circle cx="265" cy="${y(i, e.after_count)}" r="3"/><text x="282" y="${y(i, e.after_count) + 4}">${i + 1}</text>`).join("")}</svg>`;
  const changes = e.changes
    .map((c) => {
      const source = c.after || c.before;
      return `<article class="change"><div class="change-title"><span class="kind">${esc(c.kind.replace("_", " "))}</span><h3>${esc(source.title)}</h3></div><a class="source-link" href="${esc(source.url)}" target="_blank" rel="noreferrer">${esc(source.host)} ↗</a>${c.match ? `<p class="helper">Rank ${c.before.rank} → ${c.after.rank} · ${esc(c.match.method)} · score ${c.match.score.toFixed(3)}</p>` : ""}<div class="diff">${c.before ? `<section><h4>Before · rank ${c.before.rank}</h4><p>${esc(c.before.snippet || "(No returned snippet)")}</p></section>` : ""}${c.after ? `<section><h4>After · rank ${c.after.rank}</h4><p>${esc(c.after.snippet || "(No returned snippet)")}</p></section>` : ""}</div></article>`;
    })
    .join("");
  const provenance = ["before", "after"]
    .map(
      (phase) =>
        `<h4>${phase === "before" ? "Before" : "After"} provider trace</h4>${(report.provenance[phase + "_trace"] || []).map((t) => `<p class="helper">${esc(t.engine)} · ${esc(t.elapsed_ms)} ms<br>SerpApi search ID: <code>${esc(t.search_id)}</code><br>Provider processed: ${esc(t.provider_processed_at)} · bypass: ${Boolean(t.force_provider_fresh)}</p>`).join("") || '<p class="helper">Controlled copy; no provider request.</p>'}`,
    )
    .join("");
  $("report").innerHTML =
    `<div class="metrics">${metrics}</div><div class="engine-nav" aria-label="Engine results">${report.engines.map((x, i) => `<button data-engine="${i}" aria-pressed="${i === engine}">${x.engine === "google" ? "Google Search" : "Google News"}</button>`).join("")}</div><div class="charts"><section class="chart"><h3>Source alignment</h3><p class="helper">${e.before_count} → ${e.after_count} sources · canonical + optional hypotheses</p>${flow}</section><section class="chart"><h3>Rank-biased overlap <strong>${e.rbo.value === null ? "—" : e.rbo.value.toFixed(3)}</strong></h3><p class="helper">p = ${e.rbo.p} · shared depth ${e.rbo.depth} · stability, not relevance</p><div class="agreement-bars">${e.rbo.agreements.map((v, i) => `<div class="bar-column"><span>${Math.round(v * 100)}%</span><i style="height:${v * 110}px"></i><span>${i + 1}</span></div>`).join("")}</div><p class="helper">Prefix overlap at each depth. Extrapolated tail weight: ${e.rbo.unexamined_tail_weight ?? "—"}.</p></section></div><div class="evidence-heading"><h2>Evidence diff</h2><span class="helper">${e.changes.length} changes · ${e.counts.unchanged} unchanged</span></div>${changes || '<p class="helper">No normalized source or snippet changes in this pair. An unchanged result is a valid finding.</p>'}<details><summary>Inspect matching algorithm and domain flow</summary><div class="assignment"><p>${esc(a.assignment.objective)}. Same host, title Jaccard ≥ ${a.assignment.threshold}. Exact dynamic programming; unmatched sources allowed.</p><code>DP states: ${a.assignment.states} · objective: ${a.assignment.score}</code><code>Candidate matrix: ${esc(JSON.stringify(a.assignment.matrix))}</code><p>${e.domain_flows.map((d) => `${esc(d.domain)}: ${d.before} → ${d.after}`).join("<br>")}</p><p>${esc(e.rbo.reason)}</p></div></details><details><summary>Observation provenance</summary><p>#${report.before_id}: ${esc(report.before_at)} · ${esc(report.provenance.before)}<br>#${report.after_id}: ${esc(report.after_at)} · ${esc(report.provenance.after)}</p><p>Query: ${esc(report.watch.query)} · ${esc(report.watch.gl)} / ${esc(report.watch.hl)}</p>${provenance}<p>${esc(e.scope)}</p><p>${esc(report.warning)}</p></details>`;
  $("report")
    .querySelectorAll("[data-engine]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          engine = Number(b.dataset.engine);
          render();
        }),
    );
  renderGate();
}
function renderGate() {
  const g = report.gate;
  $("gate").innerHTML =
    `<strong>${g.passed ? "PASS · exit 0" : "FAIL · exit 1"}</strong>${g.failures.length ? "<ul>" + g.failures.map((f) => `<li>${esc(f)}</li>`).join("") + "</ul>" : "<p>All configured thresholds passed.</p>"}`;
  const p = getPolicy();
  cli = `evidencedelta compare ${report.before_id} ${report.after_id} --gate${p.fuzzy ? "" : " --strict"} --max-content ${p.max_content} --max-removed ${p.max_removed} --min-rbo ${p.min_rbo}`;
  $("cli").textContent = cli;
  $("copy").disabled = false;
}
$("capture-form").onsubmit = (ev) => {
  ev.preventDefault();
  operation($("capture"), async () => {
    const query = $("query").value.trim();
    if (!query) {
      $("query").setAttribute("aria-invalid", "true");
      throw Error("Add a query before capturing.");
    }
    $("query").removeAttribute("aria-invalid");
    status("Requesting Google Search and Google News from SerpApi…");
    const result = await api("/api/capture", {
      query,
      force: $("force").checked,
    });
    await refresh();
    $("after").value = result.snapshot.id;
    if (result.comparison) {
      selectPair(result.comparison.before_id, result.comparison.after_id);
      await compareSelected();
    } else
      status(
        `${result.message}. Snapshot #${result.snapshot.id} · ${result.new_calls} reserved API calls. ${result.snapshot.trace.map((t) => `${t.engine}: ${t.elapsed_ms} ms · search ${t.search_id}`).join(" | ")}${result.errors?.length ? " · " + result.errors.join("; ") : ""}`,
      );
  });
};
$("replay").onclick = () =>
  operation($("replay"), async () => {
    status(
      "Creating a controlled mutation pair from the latest live snapshot…",
    );
    const result = await api("/api/replay", {});
    await refresh();
    selectPair(result.before.id, result.after.id);
    await compareSelected();
    status(
      `CONTROLLED REPLAY · live source #${result.source_live_id} · zero API calls. ${result.disclosure}`,
      "test",
    );
  });
$("compare").onclick = () => operation($("compare"), compareSelected);
$("refresh").onclick = () => operation($("refresh"), refresh);
["max-content", "max-removed", "min-rbo", "fuzzy"].forEach(
  (id) =>
    ($(id).onchange = () => {
      if (report) operation($("compare"), compareSelected);
    }),
);
$("verify").onclick = () =>
  operation($("verify"), async () => {
    const v = await api("/api/verify");
    $("ledger").textContent =
      `${v.valid ? "VERIFIED" : "FAILED"} · ${v.entries} ledger entries · head ${v.head.slice(0, 16)}… ${v.limitation}`;
    status(
      `Ledger ${v.valid ? "verified" : "verification failed"}. ${v.entries} entries. ${v.limitation}`,
      v.valid ? "" : "error",
    );
  });
$("export").onclick = () =>
  operation($("export"), async () => {
    const bundle = await api("/api/export");
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = "evidence-delta-bundle.json";
    a.click();
    URL.revokeObjectURL(url);
    status(
      `Exported ${bundle.snapshots.length} snapshots with their hash-chain ledger. Verify offline: evidencedelta verify --bundle evidence-delta-bundle.json`,
    );
  });
$("copy").onclick = () =>
  operation($("copy"), async () => {
    await navigator.clipboard.writeText(cli);
    status("CLI command copied. Run it against the same local database.");
  });
const commands = [
  [
    "Capture live",
    () => {
      $("palette").close();
      $("query").focus();
    },
  ],
  [
    "Run controlled replay",
    () => {
      $("palette").close();
      $("replay").click();
    },
  ],
  [
    "Compare snapshots",
    () => {
      $("palette").close();
      $("compare").click();
    },
  ],
  [
    "Verify ledger",
    () => {
      $("palette").close();
      $("verify").click();
    },
  ],
  [
    "Export bundle",
    () => {
      $("palette").close();
      $("export").click();
    },
  ],
];
let selection = 0;
function listCommands() {
  const found = commands.filter((c) =>
    c[0].toLowerCase().includes($("command-search").value.toLowerCase()),
  );
  selection = Math.min(selection, Math.max(0, found.length - 1));
  $("command-list").innerHTML =
    found
      .map(
        (c, i) =>
          `<button class="${i === selection ? "selected" : ""}">${esc(c[0])}</button>`,
      )
      .join("") || "<p>No matching commands.</p>";
  $("command-list")
    .querySelectorAll("button")
    .forEach((b, i) => (b.onclick = found[i][1]));
  return found;
}
function openPalette() {
  $("command-search").value = "";
  selection = 0;
  listCommands();
  $("palette").showModal();
  $("command-search").focus();
}
$("commands").onclick = openPalette;
$("close-palette").onclick = () => $("palette").close();
$("palette").onclick = (e) => {
  if (e.target === $("palette")) {
    const r = $("palette").getBoundingClientRect();
    if (
      e.clientX < r.left ||
      e.clientX > r.right ||
      e.clientY < r.top ||
      e.clientY > r.bottom
    )
      $("palette").close();
  }
};
$("command-search").oninput = () => {
  selection = 0;
  listCommands();
};
$("command-search").onkeydown = (e) => {
  const found = listCommands();
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    selection =
      (selection + (e.key === "ArrowDown" ? 1 : -1) + found.length) %
      Math.max(1, found.length);
    listCommands();
  }
  if (e.key === "Enter" && found[selection]) {
    e.preventDefault();
    found[selection][1]();
  }
};
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    $("palette").open ? $("palette").close() : openPalette();
  }
});
refresh()
  .then(async () => {
    const live = history.filter((s) => s.mode === "live" && s.complete);
    if (live.length > 1) {
      const prior = live
        .slice(1)
        .find((s) => JSON.stringify(s.watch) === JSON.stringify(live[0].watch));
      if (prior) {
        selectPair(prior.id, live[0].id);
        await compareSelected();
      }
    }
  })
  .catch((e) => status(e.message, "error"));
