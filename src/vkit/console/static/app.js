/* The page is a view. Every fact it renders comes from one API call, and a
   refusal is rendered with the core's own wording rather than a paraphrase. */
"use strict";

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* Text goes in as textContent, never innerHTML. The values include check
   commands, log tails and the core's refusal messages, and a run writes all
   three, so a log line containing markup must not become markup. */

async function api(route, params = {}) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined),
  ).toString();
  const response = await fetch(`/api/${route}${query ? `?${query}` : ""}`);
  const document_ = await response.json();
  if (!response.ok) {
    const error = new Error(document_.error || `request failed (${response.status})`);
    error.document = document_;
    error.status = response.status;
    throw error;
  }
  return document_;
}

let toastTimer = null;
function toast(message, bad) {
  const box = $("toast");
  box.textContent = message;
  box.classList.toggle("bad", Boolean(bad));
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 6000);
}

function verdict(result) {
  if (!result) return el("span", { class: "missing", text: "—" });
  return el("span", { class: `verdict ${result}`, text: result });
}

function panel(...children) {
  return el("div", { class: "panel" }, ...children);
}

function table(head, rows) {
  return el("table", {},
    el("thead", {}, el("tr", {}, ...head.map((h) => el("th", { text: h })))),
    el("tbody", {}, ...rows),
  );
}

function fill(target, ...nodes) {
  const body = $(target);
  body.replaceChildren(...nodes.flat().filter(Boolean));
}

function refusal(error) {
  return el("div", { class: "refusal" },
    el("div", { class: "why", text: error.status === 501 ? "not implemented in this build" : "refused" }),
    el("div", { class: "core", text: error.message }),
  );
}

/* ------------------------------------------------------------------ views */

async function showProject() {
  try {
    const data = await api("project");
    const source = data.source;
    fill("project-body", panel(el("dl", { class: "kv" },
      el("dt", { text: "root" }), el("dd", { class: "mono", text: data.root }),
      el("dt", { text: "git common dir" }), el("dd", { class: "mono", text: data.git_common_dir }),
      el("dt", { text: "state root" }), el("dd", { class: "mono", text: data.state_root }),
      el("dt", { text: "manifest" }), el("dd", { class: "mono", text: data.manifest_path }),
      el("dt", { text: "HEAD" }), el("dd", { class: "mono", text: source ? `${source.head.slice(0, 12)}  dirty=${source.dirty}` : data.source_error || "unknown" }),
      el("dt", { text: "tracked files" }), el("dd", { text: source ? String(source.tracked_files) : "—" }),
    )));
    if (data.manifest_error) {
      fill("project-body", $("project-body").firstChild,
        panel(el("div", { class: "refusal" },
          el("div", { class: "why", text: "the manifest could not be read" }),
          el("div", { class: "core", text: data.manifest_error }))));
    }
  } catch (error) {
    fill("project-body", refusal(error));
  }
}

async function showReadiness() {
  try {
    const data = await api("readiness");
    const rows = data.findings.map((f) => el("tr", {},
      el("td", { class: "mono", text: f.prerequisite ? `${f.check} / ${f.prerequisite}` : f.check }),
      el("td", {}, verdict(f.ok ? "PASS" : "FAIL")),
      el("td", { class: "mono", text: f.detail }),
    ));
    fill("readiness-body",
      panel(el("div", { class: "row" },
        el("strong", { text: data.ok ? "ready" : "not ready" }),
        el("span", { class: "note", text: `state: ${data.state_detail}` }))),
      rows.length
        ? panel(table(["finding", "result", "detail"], rows))
        : panel(el("p", { class: "note", text: "no findings: every prerequisite is on PATH" })),
    );
  } catch (error) {
    fill("readiness-body", refusal(error));
  }
}

async function showChecks() {
  try {
    const data = await api("checks");
    if (!data.checks.length) {
      fill("checks-body", panel(el("p", { class: "note", text: data.note })));
      return;
    }
    const nodes = data.checks.map((check) => panel(
      el("div", { class: "row" },
        el("strong", { class: "mono", text: check.id }),
        el("span", { class: "note", text: check.description })),
      el("dl", { class: "kv" },
        el("dt", { text: "command" }), el("dd", { class: "mono", text: check.command.join(" ") }),
        el("dt", { text: "cwd" }), el("dd", { class: "mono", text: check.cwd }),
        el("dt", { text: "timeout" }), el("dd", { text: `${check.timeout_seconds}s` }),
        el("dt", { text: "required scenarios" }), el("dd", { class: "mono", text: check.required_scenarios.join(", ") }),
        el("dt", { text: "artifact" }), el("dd", { class: "mono", text: check.artifact }),
      ),
      runControls(check.id),
    ));
    fill("checks-body", ...nodes, panel(el("p", { class: "note", text: data.note })));
  } catch (error) {
    fill("checks-body", refusal(error));
  }
}

/* A run is started from the check's own row, so the check id never has to be
   typed and a typo can never be what a refused run means. */
function runControls(checkId) {
  const button = el("button", { class: "act", type: "button" }, `Run ${checkId}`);
  const change = el("div", { class: "note" });
  button.addEventListener("click", async () => {
    button.disabled = true;
    change.textContent = "computing the change set…";
    try {
      const planned = await api("plan", { operation: "run_check" });
      change.textContent = `${planned.changes.length} change(s): ` +
        planned.changes.map((c) => c.target).join(", ");
      const result = await api("run_check", { check_id: checkId });
      toast(`run ${result.run_id}: ${result.outcome.result}`);
      change.textContent = "";
      await Promise.all([showChecks(), showRuns()]);
    } catch (error) {
      change.replaceChildren(refusal(error));
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  return el("div", { class: "row" }, button, change);
}

async function showRuns() {
  const limit = $("limit").value;
  try {
    const data = await api("runs", { limit });
    if (!data.runs.length) {
      fill("runs-body", panel(el("p", { class: "note", text: "no runs recorded yet" })));
      return;
    }
    const rows = data.runs.map((run) => {
      const open = el("button", { class: "act", type: "button" }, "Open");
      open.addEventListener("click", () => showRun(run.run_id));
      const cancel = el("button", { class: "act", type: "button" }, "Cancel");
      cancel.addEventListener("click", () => cancelRun(run.run_id));
      return el("tr", {},
        el("td", { class: "mono" }, el("a", { href: "#", onclick: (e) => { e.preventDefault(); showRun(run.run_id); }, text: run.run_id.slice(0, 12) })),
        el("td", { class: "mono", text: run.check_id }),
        el("td", {}, verdict(run.result || (run.lifecycle === "terminal" ? "BLOCKED" : run.lifecycle.toUpperCase()))),
        el("td", { class: "mono", text: run.reason || "" }),
        el("td", { class: "mono", text: (run.ended_at || run.registered_at || "").slice(0, 19).replace("T", " ") }),
        el("td", {}, open, " ", cancel),
      );
    });
    fill("runs-body", panel(table(["run", "check", "result", "reason", "ended", ""], rows)));
  } catch (error) {
    fill("runs-body", refusal(error));
  }
}

async function showRun(runId) {
  try {
    const data = await api("run", { run_id: runId });
    const report = data.report;
    const outcome = report.outcome;
    const scenarios = outcome.scenarios || [];
    fill("run-detail",
      panel(
        el("div", { class: "row" },
          el("strong", { class: "mono", text: runId }),
          verdict(outcome.result),
          outcome.reason ? el("span", { class: "note mono", text: outcome.reason }) : null,
        ),
        outcome.detail ? el("p", { class: "note", text: outcome.detail }) : null,
        el("dl", { class: "kv" },
          el("dt", { text: "check" }), el("dd", { class: "mono", text: report.check_id }),
          el("dt", { text: "command" }), el("dd", { class: "mono", text: report.command.argv.join(" ") }),
          el("dt", { text: "configuration digest" }), el("dd", { class: "mono", text: report.configuration_digest }),
          el("dt", { text: "started" }), el("dd", { class: "mono", text: (report.started_at || "").slice(0, 19).replace("T", " ") }),
          el("dt", { text: "ended" }), el("dd", { class: "mono", text: (report.ended_at || "").slice(0, 19).replace("T", " ") }),
        ),
        scenarios.length
          ? table(["scenario", "result", "observation"], scenarios.map((s) => el("tr", {},
              el("td", { class: "mono", text: s.id }),
              el("td", {}, verdict(s.result)),
              el("td", { text: s.observation }))))
          : null,
      ));
    for (const stream of ["stdout", "stderr"]) {
      if (!data.logs.includes(stream)) continue;
      const log = await api("log", { run_id: runId, stream });
      $("run-detail").append(panel(
        el("div", { class: "row" }, el("strong", { text: stream })),
        el("pre", { class: "log", text: log.text || "(empty)" }),
        log.truncated
          ? el("p", { class: "trunc", text: `tail of ${log.bytes} bytes; the file is ${log.size} bytes` })
          : null,
      ));
    }
  } catch (error) {
    fill("run-detail", refusal(error));
  }
}

async function cancelRun(runId) {
  try {
    const result = await api("cancel_run", { run_id: runId });
    const outcome = result.outcome;
    toast(`cancel ${runId.slice(0, 12)}: ${outcome.result}${outcome.reason ? ` / ${outcome.reason}` : ""}`);
    if (outcome.result === "BLOCKED") {
      // The core declined, and its own reason is what the operator is told.
      fill("run-detail", refusal(Object.assign(new Error(outcome.detail || "the core refused to cancel"), { status: 409 })));
    }
    await showRuns();
  } catch (error) {
    toast(error.message, true);
    fill("run-detail", refusal(error));
  }
}

async function showRecovery() {
  try {
    const data = await api("recovery");
    if (!data.findings.length) {
      fill("recovery-body", panel(el("p", { class: "note", text: "nothing to reconcile" })));
      return;
    }
    const rows = data.findings.map((f) => el("tr", {},
      el("td", { class: "mono", text: f.kind }),
      el("td", { class: "mono", text: f.target }),
      el("td", {}, verdict(f.actionable ? "PASS" : "BLOCKED"), f.actionable ? " actionable" : " read-only"),
      el("td", { text: f.detail }),
    ));
    fill("recovery-body",
      panel(table(["kind", "target", "state", "detail"], rows)),
      panel(el("p", { class: "note", text: data.note })));
  } catch (error) {
    fill("recovery-body", refusal(error));
  }
}

async function showOperations() {
  try {
    const data = await api("operations");
    const rows = data.operations.map((op) => el("tr", {},
      el("td", { class: "mono", text: op.name }),
      el("td", { text: op.effect }),
      el("td", {}, op.implemented ? el("span", { class: "PASS", text: "implemented" }) : el("span", { class: "BLOCKED", text: "not implemented" })),
      el("td", { class: "mono", text: op.writes.join(", ") }),
    ));
    fill("operations-body",
      panel(table(["operation", "effect", "status", "may write"], rows)),
      panel(el("p", { class: "note", text: "verification/manifest.json is committed policy and is not writable from here. Changing it means making a commit." })));
  } catch (error) {
    fill("operations-body", refusal(error));
  }
}

const VIEWS = {
  project: showProject,
  readiness: showReadiness,
  checks: showChecks,
  runs: showRuns,
  recovery: showRecovery,
  operations: showOperations,
};

function open(view) {
  for (const button of document.querySelectorAll("nav button")) {
    button.classList.toggle("on", button.dataset.view === view);
  }
  for (const section of document.querySelectorAll(".view")) {
    section.classList.toggle("on", section.id === view);
  }
  return VIEWS[view]();
}

for (const button of document.querySelectorAll("nav button")) {
  button.addEventListener("click", () => open(button.dataset.view));
}
$("refresh-runs").addEventListener("click", showRuns);

open("project");
