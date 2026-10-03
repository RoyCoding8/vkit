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

/* GET reads and POST writes. The split is the server's, not this page's: a
   mutation carries the token the server put in this page, and a request without
   it is refused there whether this page sent it or not.

   `body` is sent as JSON on a mutation only. It exists because the configuration
   save carries a whole policy document and a digest, which is neither an
   identifier nor something that belongs in a URL, and because the server refuses
   a body it has not bounded. */
const SESSION_TOKEN =
  document.querySelector('meta[name="vkit-token"]').content;

async function api(route, params = {}, mutate = false, body = null) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined),
  ).toString();
  const init = mutate
    ? {
        method: "POST",
        headers: body
          ? { "X-Vkit-Token": SESSION_TOKEN, "Content-Type": "application/json" }
          : { "X-Vkit-Token": SESSION_TOKEN },
        ...(body ? { body: JSON.stringify(body) } : {}),
      }
    : {};
  const response = await fetch(`/api/${route}${query ? `?${query}` : ""}`, init);
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

/* What kind of check a verdict came from, next to the verdict itself.

   A green tick means four different things in this product, and the same tick
   covers a test that proved one input and a property check that sampled a
   hundred. A reader who sees only the tick cannot tell which they have, so the
   category travels with the verdict everywhere a verdict is shown: the runs
   table, the run detail, the landing page's verification panel, and the toast a
   run produces. One helper rather than four renderings, because four would be
   four vocabularies and the fourth would be the one nobody keeps in step.

   The category is read from what the run recorded, never from what the check is
   declared to be. A run that recorded none predates the field, and is shown as
   "no category recorded" rather than being given a default that would read as
   measured. */
function categoryPill(evidenceKind) {
  if (!evidenceKind) return el("span", { class: "note mono", text: "no category recorded" });
  return el("span", { class: "pill note", text: evidenceKind });
}

function panel(...children) {
  return el("div", { class: "panel" }, ...children);
}

/* A table that outgrows its column scrolls inside its own panel. Letting it
   widen the document pushes the header and nav out from under the reader, so
   the overflow is contained here rather than left to the page.

   `hideAt` tags a column that the stylesheet drops on a narrow viewport, so a
   wide table keeps its actions on screen instead of hiding them behind a
   horizontal scroll nobody discovers. */
function table(head, rows) {
  return el("div", { class: "tablewrap" }, el("table", {},
    el("thead", {}, el("tr", {}, ...head.map((h) => el("th", { class: h.hideAt || "", text: h.label || h })))),
    el("tbody", {}, ...rows),
  ));
}

function fill(target, ...nodes) {
  const body = $(target);
  body.replaceChildren(...nodes.flat().filter(Boolean));
}

function refusal(error) {
  const isMissing = error.status === 501;
  return el("div", { class: `refusal${isMissing ? " gap" : ""}` },
    el("div", { class: "why", text: isMissing ? "not implemented in this build" : "refused" }),
    el("div", { class: "core", text: error.message }),
  );
}

/* A control starts in a pending state and stays visibly busy while the request
   is in flight. The page is the only feedback channel: without this a click on
   Run looks identical to a click that did nothing, which is the failure a
   confirmation gate must not have. */
function busy(button, label, work) {
  return (async () => {
    const original = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = label;
    try {
      await work();
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = original;
    }
  })();
}

/* ------------------------------------------------------------------ views

   Two of these are about different things and are built from different routes
   on purpose. `showSetup` reads `/api/readiness`, which is what `vkit doctor`
   reports: a manifest that parses, a state store that can be written, tools on
   PATH. None of that is a verification result. `taskStanding` reads
   `/api/runs`, which is the only place a run's published evidence exists. The
   page never derives one from the other, because a green doctor on a project
   that has never been verified reads exactly like a project that has, and that
   is the confusion this split exists to remove. */

async function showHome() {
  /* Each question is answered on its own. One refused route must not blank the
     other three, because an operator who cannot see which project is open has
     lost the page, and a blank panel is indistinguishable from a slow one. */
  const answers = await Promise.all([
    api("project").then((value) => [value, null], (error) => [null, error]),
    api("readiness").then((value) => [value, null], (error) => [null, error]),
    api("checks").then((value) => [value, null], (error) => [null, error]),
    api("runs", { limit: 5 }).then((value) => [value, null], (error) => [null, error]),
  ]);
  const [project, projectError] = answers[0];
  const [readiness, readinessError] = answers[1];
  const [checks, checksError] = answers[2];
  const [runs, runsError] = answers[3];

  /* `either` is here rather than a `cond ? ...spread(...) : ...` at each call
     site: a spread inside a conditional needs parentheses on both arms, and
     four of those is a syntax error waiting to be written slightly wrong. */
  const either = (error, panels) => (error ? [refusal(error)] : panels());

  fill("home-project", either(projectError, () => openProjectPanels(project)));
  fill("home-available", either(checksError, () => availablePanels(checks)));

  /* The setup block needs two routes, so it is the one that can be partly
     built. What it needs both for is the next-action sentence, which is why a
     refusal on either side is stated rather than silently producing a shorter
     answer that would claim a next step nothing has established. */
  fill("home-setup",
    either(readinessError, () => setupGapsPanels(readiness)),
    checksError ? null : installationPanelOnHome(checks),
    either(runsError, () => taskStanding(runs)));

  /* The next action is a claim about all three of project, setup and checks, so
     it is the one answer that cannot be made from a subset of them. */
  fill("home-next", checksError || readinessError || projectError
    ? panel(el("p", { class: "note", text: "The next action needs the project, its host setup and its checks. The panel above reports which of those the console could not read." }))
    : nextActionPanels(project, readiness, checks));
}

function openProjectPanels(project) {
  const source = project.source;
  const panels = [panel(el("dl", { class: "kv" },
    el("dt", { text: "root" }), el("dd", { class: "mono", text: project.root }),
    el("dt", { text: "HEAD" }), el("dd", { class: "mono", text: source ? source.head.slice(0, 12) : project.source_error || "unknown" }),
    el("dt", { text: "working tree" }), el("dd", { text: source ? (source.dirty ? `dirty, ${source.tracked_files} tracked` : `clean, ${source.tracked_files} tracked`) : "—" }),
    el("dt", { text: "manifest" }), el("dd", { class: "mono", text: project.manifest_path }),
  ))];
  if (project.manifest_error) {
    panels.push(panel(el("div", { class: "refusal" },
      el("div", { class: "why", text: "the manifest could not be read" }),
      el("div", { class: "core", text: project.manifest_error }))));
  }
  return panels;
}

/* What the project can run. The check id is what every other surface calls the
   check, so the operator reads one name rather than two. */
function availablePanels(checks) {
  if (!checks.checks.length) {
    return [panel(el("p", { class: "note", text: "this project registers no checks" }))];
  }
  return [panel(table(["check", "what it verifies", "category", "cases required"],
    checks.checks.map((check) => el("tr", {},
      el("td", { class: "mono" }, el("strong", { text: check.id })),
      el("td", { text: check.description }),
      el("td", {}, categoryPill(check.evidence_kind)),
      el("td", { class: "mono", text: String(check.obligations.length) }),
    ))))];
}

/* What is not set up on this host. The heading scopes it: a list of missing
   tools under a word like "readiness" reads as a verdict, and it is not one.
   The answer to "has anything been verified" is a different panel, built from
   the runs table. */
function setupGapsPanels(readiness) {
  const missing = readiness.findings.filter((finding) => !finding.ok);
  const panels = [];
  if (!readiness.state_writable) {
    panels.push(panel(el("div", { class: "refusal" },
      el("div", { class: "why", text: "the state store cannot be written" }),
      el("div", { class: "core", text: readiness.state_detail }))));
  }
  panels.push(missing.length
    ? panel(el("p", { class: "note", text: "these tools are not where the manifest expects them:" }),
        el("ul", { class: "policy" }, ...missing.map((finding) => el("li", {},
          el("span", { class: "mono strong", text: `${finding.check} / ${finding.prerequisite}` }),
          el("div", { class: "mono note", text: finding.detail })))))
    : panel(el("p", { class: "note", text: "every prerequisite the manifest names is on PATH" })));
  return panels;
}

/* The host's integration state, kept beside the missing tools because both are
   answers to "what needs setup" and neither is an answer to "is this verified".
   The word installed describes the plugin, not the project. */
function installationPanelOnHome(checks) {
  const installation = checks.installed;
  return [panel(el("div", { class: "row" },
    el("strong", { text: "host integration" }),
    el("span", { class: `pill ${installation.installed ? "yes" : "no"}`, text: installation.installed ? "installed" : "not installed" }),
    el("span", { class: "note", text: installation.installed
      ? `${installation.plugin} at ${installation.install_path || "an unrecorded path"}`
      : "the host has no record of this plugin; Operations can install it" })))];
}

/* The only sentence on this page that is about verification, and it is built
   from the runs table rather than from any installation fact. With no runs it
   says so in the negative, which is the honest answer for a project nobody has
   verified yet. */
function taskStanding(runs) {
  if (!runs.runs.length) {
    return panel(
      el("div", { class: "row" },
        el("strong", { text: "Task verification" }),
        el("span", { class: "verdict missing", text: "no evidence" })),
      el("p", { class: "note", text: "No run has been recorded for this project, so no task has been verified. Start one on Checks." }));
  }
  const latest = runs.runs[0];
  const result = latest.result || (latest.lifecycle === "terminal" ? "BLOCKED" : latest.lifecycle.toUpperCase());
  return panel(
    el("div", { class: "row" },
      el("strong", { text: "Task verification" }),
      verdict(result),
      categoryPill(latest.evidence_kind),
      el("span", { class: "mono note", text: latest.check_id })),
    el("p", { class: "note", text: "From the most recent recorded run. This is what a run published, not what the installation reports. Runs lists them all." }));
}

/* One action, chosen in the order a blocked step blocks the next. Each branch
   names a real cause from the API, and the only button here starts a run, which
   is the one action that produces the evidence this page is asking about. The
   rest point at the view that carries the control, rather than putting a second
   copy of a mutation on the landing page. */
function nextActionPanels(project, readiness, checks) {
  if (project.manifest_error) {
    return [nextPanel("The manifest could not be parsed, so no check can be started. Project shows the file and the parser's own message.")];
  }
  if (!readiness.state_writable) {
    return [nextPanel(`The state store cannot be written, so a run could not be recorded. ${readiness.state_detail}`)];
  }
  const missing = readiness.findings.filter((finding) => !finding.ok);
  if (missing.length) {
    const tools = [...new Set(missing.map((finding) => finding.prerequisite))];
    return [nextPanel(`Put ${tools.join(", ")} on PATH. A check whose prerequisite is missing is refused before it starts, so running one now would not produce evidence.`)];
  }
  if (!checks.checks.length) {
    return [nextPanel("This project registers no checks, so there is nothing to run. Add one to verification/manifest.json and reload.")];
  }
  return [panel(
    el("div", { class: "row" }, el("strong", { text: "Next" })),
    el("p", { class: "note", text: `Run ${checks.checks[0].id} to produce the first piece of evidence for this project.` }),
    runControls(checks.checks[0].id),
  )];
}

/* The heading and the sentence travel together, so a reader who lands on the
   panel has the label that scopes what follows rather than meeting a bare
   sentence. */
function nextPanel(sentence) {
  return panel(
    el("div", { class: "row" }, el("strong", { text: "Next" })),
    el("p", { class: "note", text: sentence }),
  );
}

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

/* Installation health, under a name that cannot be read as a verification
   verdict. This route was called "readiness" and showed a green PASS beside
   the word, on a project that had never been verified by anything: every
   prerequisite on PATH is a statement about the machine, not about the code.
   The findings are still the same doctor facts and the same API route; what
   changed is the label, so the page has nowhere to read a verdict from. */
async function showSetup() {
  try {
    const data = await api("readiness");
    const rows = data.findings.map((f) => el("tr", {},
      el("td", { class: "mono", text: f.prerequisite ? `${f.check} / ${f.prerequisite}` : f.check }),
      el("td", {}, el("span", { class: `verdict ${f.ok ? "PASS" : "FAIL"}`, text: f.ok ? "found" : "missing" })),
      el("td", { class: "mono", text: f.detail }),
    ));
    fill("setup-body",
      panel(el("div", { class: "row" },
        el("strong", { text: "Host setup" }),
        el("span", { class: "note", text: `state store: ${data.state_detail}` }))),
      rows.length
        ? panel(table(["what the manifest expects", "on this host", "where"], rows))
        : panel(el("p", { class: "note", text: "no findings: every prerequisite is on PATH" })),
    );
  } catch (error) {
    fill("setup-body", refusal(error));
  }
}

async function showChecks() {
  try {
    const data = await api("checks");
    fill("checks-body",
      installationPanel(data.installed),
      ...(data.checks.length
        ? data.checks.map((check) => panel(
            el("div", { class: "row" },
              el("strong", { class: "mono", text: check.id }),
              el("span", { class: "note", text: check.description })),
            el("dl", { class: "kv" },
              el("dt", { text: "command" }), el("dd", { class: "mono", text: check.command.join(" ") }),
              el("dt", { text: "cwd" }), el("dd", { class: "mono", text: check.cwd }),
              el("dt", { text: "timeout" }), el("dd", { text: `${check.timeout_seconds}s` }),
              el("dt", { text: "evidence category" }), el("dd", {}, categoryPill(check.evidence_kind)),
              el("dt", { text: "required cases" }), el("dd", { class: "mono", text: check.obligations.join(", ") }),
              el("dt", { text: "artifact" }), el("dd", { class: "mono", text: check.artifact }),
            ),
            runControls(check.id),
          ))
        : [panel(el("p", { class: "note", text: data.note }))]),
    );
  } catch (error) {
    fill("checks-body", refusal(error));
  }
}

/* Installation as the host reports it. This reads the host's own record; the
   console keeps no separate notion of what it installed. */
function installationPanel(installed) {
  if (!installed) {
    return panel(el("p", { class: "note", text: "the host reports no installation record" }));
  }
  return panel(
    el("div", { class: "row" },
      el("strong", { text: "Installation" }),
      el("span", { class: `pill ${installed.installed ? "yes" : "no"}`, text: installed.installed ? "installed" : "not installed" })),
    el("dl", { class: "kv" },
      el("dt", { text: "plugin" }), el("dd", { class: "mono", text: installed.plugin }),
      el("dt", { text: "version" }), el("dd", { class: "mono", text: installed.version || "—" }),
      el("dt", { text: "scope" }), el("dd", { text: installed.scope || "—" }),
      el("dt", { text: "marketplace" }), el("dd", { text: installed.marketplace_registered ? "registered" : "not registered" }),
      el("dt", { text: "package" }), el("dd", { class: "mono", text: installed.source || "not found beside this build" }),
      el("dt", { text: "installed at" }), el("dd", { class: "mono", text: installed.install_path || "—" }),
    ),
  );
}

/* A run is started from the check's own row, so the check id never has to be
   typed and a typo can never be what a refused run means.

   The task the run belongs to is shown by name afterwards. It is a real task,
   admitted through the core's own path, so an operator can read the same verdict
   from `vkit task finalize`; naming it here is what makes that discoverable
   rather than a coincidence. */
function runControls(checkId) {
  const button = el("button", { class: "act", type: "button" }, `Run ${checkId}`);
  const change = el("div", { class: "note" });
  button.addEventListener("click", () => busy(button, "running…", async () => {
    change.textContent = "computing the change set…";
    try {
      const planned = await api("plan", { operation: "run_check" });
      change.textContent = `${planned.changes.length} change(s): ` +
        planned.changes.map((c) => c.target).join(", ");
      const result = await api("run_check", { check_id: checkId }, true);
      toast(`run ${result.run_id}: ${result.outcome.result}`);
      change.textContent = `task ${result.task_id} generation ${result.generation}`;
      await Promise.all([showChecks(), showRuns(), showEvidence()]);
    } catch (error) {
      change.replaceChildren(refusal(error));
      toast(error.message, true);
    }
  }));
  return el("div", { class: "row" }, button, change);
}

async function showRuns() {
  const limit = $("limit").value;
  try {
    const data = await api("runs", { limit });
    if (!data.runs.length) {
      fill("runs-body", panel(el("p", { class: "note", text: "no runs recorded yet" })));
      $("run-detail").replaceChildren();
      return;
    }
    const rows = data.runs.map((run) => {
      const open = el("button", { class: "act", type: "button" }, "Open");
      open.addEventListener("click", () => busy(open, "opening…", () => showRun(run.run_id)));
      const cancel = el("button", { class: "act", type: "button" }, "Cancel");
      cancel.addEventListener("click", () => busy(cancel, "cancelling…", () => cancelRun(run.run_id)));
      return el("tr", {},
        el("td", { class: "mono" }, el("a", { href: "#", onclick: (e) => { e.preventDefault(); showRun(run.run_id); }, text: run.check_id })),
        el("td", {}, verdict(run.result || (run.lifecycle === "terminal" ? "BLOCKED" : run.lifecycle.toUpperCase()))),
        el("td", {}, categoryPill(run.evidence_kind)),
        el("td", { class: "mono wide-hide", text: run.reason || "" }),
        el("td", { class: "mono narrow-hide", text: (run.ended_at || run.registered_at || "").slice(0, 19).replace("T", " ") }),
        el("td", { class: "actions" }, open, " ", cancel),
      );
    });
    fill("runs-body", panel(table(
      ["check", "result", "category",
        { label: "reason", hideAt: "wide-hide" },
        { label: "ended", hideAt: "narrow-hide" }, ""],
      rows)));
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
          categoryPill(data.evidence_kind),
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
    const result = await api("cancel_run", { run_id: run_id }, true);
    // A cancellation that arrived before the run published an owner has no
    // outcome to show. It is pending, and the supervisor will honour it before
    // the check begins executing, so this is a state rather than a failure.
    if (result.pending) {
      toast(`cancel ${runId.slice(0, 12)}: pending, the run has no published owner yet`);
      await showRuns();
      return;
    }
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

/* One row per operation on the writable surface.

   The four setup operations get a button here. `run_check` and `cancel_run` do
   not: they are not setup actions, and `apply` cannot call them because a run
   needs a check id or a run id, not a name. Rendering a permanently disabled
   button for them would be a control that can never work; naming where each one
   is actually driven is honest and useful instead. */
async function showOperations() {
  try {
    const surface = await api("operations");
    const rows = surface.operations.map((op) => {
      const invokedFrom = { run_check: "the check's own Run button", cancel_run: "the Cancel button on a run" }[op.name];
      const cell = invokedFrom
        ? el("td", { class: "note" }, el("span", { class: "note", text: `driven from ${invokedFrom}` }))
        : el("td", { class: "actions" }, operationButton(op));
      return el("tr", {},
        el("td", { class: "mono" }, el("strong", { text: op.name })),
        el("td", { text: op.effect }),
        el("td", { class: "mono wide-hide", text: op.writes.join(", ") }),
        cell,
      );
    });
    fill("operations-body",
      panel(table(
        ["operation", "effect", { label: "may write", hideAt: "wide-hide" }, "apply from here"],
        rows)),
      panel(el("p", { class: "note", text: "verification/manifest.json is committed policy and is not writable from here. Changing it means making a commit." })));
  } catch (error) {
    fill("operations-body", refusal(error));
  }
}

function operationButton(op) {
  const outcome = el("div", { class: "outcome" });
  const button = el("button", { class: "act", type: "button" }, op.name);
  button.addEventListener("click", () => busy(button, "working…", async () => {
    outcome.replaceChildren();
    try {
      if (op.name === "enroll") return await runEnroll(outcome);
      const planned = await api("plan", { operation: op.name });
      if (!planned.implemented) {
        outcome.replaceChildren(refusal(Object.assign(
          new Error(planned.note || "not implemented in this build"), { status: 501 })));
        return;
      }
      const result = await api("apply", { operation: op.name }, true);
      const done = (result && result.result) || {};
      const said = done.host_output || done.note || `${op.name} applied`;
      toast(`${op.name}: ${said.split("\n")[0]}`);
      outcome.replaceChildren(el("p", { class: "note ok", text: said }));
      await Promise.all([showChecks(), showOperations()]);
    } catch (error) {
      outcome.replaceChildren(refusal(error));
      toast(error.message, true);
    }
  }));
  return el("div", {}, button, outcome);
}

/* Enroll is two steps because Plan 06 requires it: the policy is shown, and only
   an explicit acceptance enables execution. The accept button is rendered only
   after the policy has been fetched, so a user cannot accept a policy they were
   never shown. */
async function runEnroll(outcome) {
  // `apply` wraps the operation's answer as {operation, result}, so the policy
  // is under `result`. Reading it off the top level is how this rendered
  // "Cannot read properties of undefined" on a button that visibly did nothing.
  const envelope = await api("apply", { operation: "enroll" }, true);
  const policy = envelope.result && envelope.result.policy;
  if (!policy) {
    outcome.replaceChildren(refusal(Object.assign(
      new Error("enroll returned no policy to accept"), { status: 500 })));
    return;
  }
  const list = el("ul", { class: "policy" },
    ...policy.map((p) => el("li", {},
      el("span", { class: "mono strong", text: p.id }),
      el("div", { class: "mono note", text: p.command.join(" ") }))));

  const accept = el("button", { class: "act", type: "button" }, "Accept this policy");
  accept.addEventListener("click", () => busy(accept, "accepting…", async () => {
    try {
      const done = await api("apply", { operation: "enroll", accepted: "true" }, true);
      toast("enrolled: execution is enabled for this policy");
      outcome.replaceChildren(el("p", { class: "note ok", text: done.result.record }));
    } catch (error) {
      outcome.replaceChildren(refusal(error));
      toast(error.message, true);
    }
  }));

  outcome.replaceChildren(
    el("p", { class: "note", text: "this repository's executable policy:" }),
    list,
    el("div", { class: "row" }, accept),
  );
}

const VIEWS = {
  home: showHome,
  overview: showOverview,
  project: showProject,
  setup: showSetup,
  checks: showChecks,
  evidence: showEvidence,
  tasks: showTasks,
  cleanup: showCleanup,
  integrations: showIntegrations,
  runs: showRuns,
  recovery: showRecovery,
  operations: showOperations,
};

/* A section is a view whose data arrives inside another route's document.

   `api.py` owns the route table, so there is no `/api/cleanup` to call. Every
   section names the route it rides and the key it reads, and both are declared
   once in `plan.SECTIONS` so the page and the backend cannot disagree about
   which section lives on which route. */
async function sectionOf(route, key) {
  const document_ = await api(route);
  const parts = key.split(".");
  let node = document_;
  for (const part of parts) {
    node = node && node[part];
    if (node === undefined) return null;
  }
  return node;
}

/* An offered action or an explanation of what is missing.

   The rule this whole page is built on: never render a control that cannot work.
   So an action whose prerequisite is absent is not drawn as a button at all; it
   is drawn as the sentence naming that prerequisite. A greyed-out button would
   still look like something to press, and an operator pressing it gets a refusal
   instead of the explanation they needed. */
function actionOrReason(action, describe) {
  if (action.available === false) {
    return el("p", { class: "why-not", text: action.reason || describe });
  }
  return describe();
}

function kv(...pairs) {
  const nodes = [];
  for (const [term, value] of pairs.flat()) {
    if (value === null || value === undefined) continue;
    nodes.push(el("dt", { text: term }), el("dd", { class: "mono", text: String(value) }));
  }
  return el("dl", { class: "kv" }, ...nodes);
}

/* ---------------------------------------------------------- checks & evidence

   **The category is rendered beside every verdict, never inside it.** A green
   `PASS` from a property check and a green `PASS` from a scenario check are the
   same three characters and mean different things, so a reader who sees only the
   word cannot tell a sampled-agreement pass from a named-case pass. The category
   is therefore part of the same rendered string as the verdict, and the receipt's
   own statement of what it does NOT establish is shown directly beneath it. That
   second sentence is the one that stops a property pass being read as a
   scenario pass, because it says out loud what the green mark does not license. */
async function showEvidence() {
  const section = await sectionOf("checks", "sections.evidence");
  if (!section || !section.available) {
    return fill("evidence-body", panel(el("p", { class: "note", text: section && section.note ? section.note : "the manifest could not be read" })));
  }
  const rows = [];
  for (const check of section.checks) {
    const latest = check.latest;
    const boundary = latest.trust_boundary || {};
    rows.push(panel(
      el("div", { class: "row" },
        el("strong", { class: "mono", text: check.id }),
        /* One token, because the category decides how the word reads. Splitting
           it from the verdict is what the plan forbids. */
        el("span", { class: `verdict ${verdictClass(latest.verdict)}`, text: `${check.category} ${latest.verdict || "—"}` }),
        el("span", { class: "pill", text: check.claim_id })),
      el("p", { class: "note", text: check.description }),
      kv(
        ["establishes", check.establishes],
        ["does not establish", check.does_not_establish],
        ["obligations", check.scope.obligations.length ? check.scope.obligations.join(", ") : "none"],
        ["subject", check.scope.subject_paths.join(", ") || "none"],
        ["latest run", latest.run_id ? latest.run_id.slice(0, 12) : "never run"],
        ["run state", latest.state],
      ),
      latest.note ? el("p", { class: "why-not", text: latest.note }) : null,
      satisfiedPanel(latest),
      prerequisitesPanel(check),
      runAction(check),
    ));
  }
  return fill("evidence-body", ...rows);
}

function verdictClass(result) {
  if (!result) return "missing";
  return result;
}

/* The obligations a run actually discharged, from the receipt's `satisfied`.

   Shown because "PASS" alone does not say which of the required obligations were
   met. A run that satisfied none of them and still reports a PASS is a fact the
   operator must see, and this is where they see it. */
function satisfiedPanel(latest) {
  const entries = latest.satisfied || [];
  const counter = latest.counterexamples || [];
  if (!entries.length && !counter.length) return null;
  const items = entries.map((entry) => el("li", { class: "mono", text: describeObligation(entry) }));
  const counterItems = counter.map((entry) => el("li", { class: "mono fail", text: describeObligation(entry) }));
  return panel(
    el("div", { class: "row" }, el("strong", { text: "obligations" })),
    items.length ? el("ul", { class: "policy" }, ...items) : el("p", { class: "note", text: "no obligations recorded as satisfied" }),
    counterItems.length ? el("div", { class: "row" }, el("strong", { text: "counterexamples" })) : null,
    counterItems.length ? el("ul", { class: "policy" }, ...counterItems) : null,
  );
}

/* The receipt stores an obligation object under `obligation`, wrapped in a
   `case_satisfied` record. Read defensively: a receipt shape this page does not
   recognise must not blank the panel. */
function describeObligation(entry) {
  const obligation = (entry && entry.obligation) || {};
  const id = obligation.obligation || "(unnamed)";
  return `${obligation.kind || "obligation"} ${id}`;
}

/* Each prerequisite and whether it is on PATH. A missing one is named here and
   is also why the Run button below is not drawn. */
function prerequisitesPanel(check) {
  if (!check.prerequisites.length) return null;
  const items = check.prerequisites.map((need) => el("li", {},
    el("span", { class: need.found ? "mono" : "mono fail", text: need.name }),
    el("div", { class: "note mono", text: need.found ? "on PATH" : "not on PATH" })));
  return panel(
    el("div", { class: "row" }, el("strong", { text: "prerequisites" })),
    el("ul", { class: "policy" }, ...items));
}

/* The Run action, drawn only when it can work. */
function runAction(check) {
  return panel(actionOrReason(
    check.run_action,
    () => runControls(check.id),
  ));
}

/* ------------------------------------------------------------------- tasks

   The verdict shown is the one the core computed. The page does not decide it,
   does not re-derive it from the runs, and labels it as the core's reading so a
   reader can tell a computed readiness from a recorded one. */
async function showTasks() {
  const section = await sectionOf("project", "sections.tasks");
  if (!section) return fill("tasks-body", panel(el("p", { class: "note", text: "the runs table could not be read" })));
  if (!section.tasks.length) {
    return fill("tasks-body", panel(el("p", { class: "note", text: "no task has been admitted for this project. Running a check on Checks admits one." })));
  }
  const panels = section.tasks.map((task) => {
    if (!task.readable) {
      return panel(
        el("div", { class: "row" }, el("strong", { class: "mono", text: task.task_id }), el("span", { class: "verdict BLOCKED", text: "unreadable" })),
        el("div", { class: "refusal" }, el("div", { class: "why", text: "this task's contract cannot be read" }), el("div", { class: "core", text: task.error })));
    }
    const verdict = task.verdict || {};
    const readiness = verdict.readiness;
    return panel(
      el("div", { class: "row" },
        el("strong", { class: "mono", text: task.task_id }),
        el("span", { class: `verdict ${verdictClass(readiness)}`, text: readiness || "unknown" }),
        el("span", { class: "note mono", text: `generation ${task.generation}, status ${task.status}` })),
      verdict.recorded === false
        ? el("p", { class: "note", text: "Readiness above is the core's reading, recomputed from the frozen floor and the recorded identities. Viewing this page records nothing." })
        : null,
      verdict.error ? el("p", { class: "why-not", text: verdict.error }) : null,
      gapsPanel(verdict),
      kv(
        ["scope", task.contract.scope],
        ["policy digest", (task.policy_digest || "").slice(0, 12)],
        ["recorded readiness", task.recorded_readiness || "none recorded"],
      ),
      heldPanel(task),
      requiredClaims(task),
      taskRuns(task),
    );
  });
  return fill("tasks-body", ...panels);
}

/* Missing evidence, named. These are the core's own gap strings, carried
   verbatim: this page does not summarise a refusal into something friendlier. */
function gapsPanel(verdict) {
  if (!verdict.gaps || !verdict.gaps.length) return null;
  return panel(
    el("div", { class: "row" }, el("strong", { text: "missing evidence" })),
    el("ul", { class: "policy" }, ...verdict.gaps.map((gap) => el("li", { class: "note", text: gap }))));
}

function heldPanel(task) {
  const held = task.held_resources || [];
  const items = held.map((claim) => el("li", { class: "mono", text: `${claim.key} (${claim.kind}, ${claim.held} held at generation ${claim.generation})` }));
  return panel(
    el("div", { class: "row" }, el("strong", { text: "held resources" })),
    items.length ? el("ul", { class: "policy" }, ...items) : el("p", { class: "note", text: "this task holds no resources" }));
}

/* What the task must prove before it can be ready. */
function requiredClaims(task) {
  const required = (task.contract && task.contract.required_checks) || [];
  return panel(
    el("div", { class: "row" }, el("strong", { text: "required claims" })),
    required.length
      ? el("ul", { class: "policy" }, ...required.map((id) => el("li", { class: "mono", text: id })))
      : el("p", { class: "note", text: "no mandatory check floor recorded" }));
}

/* The runs belonging to this task, each with its verdict, cancellation control
   and bounded logs. Reached from the task so the operator sees one task's
   evidence together rather than hunting for it in the global list. */
function taskRuns(task) {
  const runs = task.runs || [];
  if (!runs.length) return panel(el("p", { class: "note", text: "this task has no recorded runs" }));
  const rows = runs.map((run) => {
    const cancel = el("button", { class: "act", type: "button" }, "Cancel");
    cancel.addEventListener("click", () => busy(cancel, "cancelling…", () => cancelRun(run.run_id).then(showTasks)));
    return el("tr", {},
      el("td", { class: "mono", text: run.check_id }),
      el("td", {}, verdict(run.result || (run.lifecycle === "terminal" ? "BLOCKED" : run.lifecycle.toUpperCase()))),
      el("td", { class: "mono wide-hide", text: run.evidence_kind || "—" }),
      el("td", { class: "actions" }, cancel));
  });
  return panel(table(["check", "result", { label: "category", hideAt: "wide-hide" }, ""], rows));
}

/* ----------------------------------------------------------------- cleanup

   **What cleanup did is real data.** The applied panel and the refusals panel
   read the durable record written by every apply outcome, so an empty list means
   this project has never cleaned anything, which is a true statement rather
   than a gap. A refusal is rendered with the checker's own reason, because the
   question an operator brings to this page is why a file was left alone. */
async function showCleanup() {
  const section = await sectionOf("checks", "sections.cleanup");
  if (!section) return fill("cleanup-body", panel(el("p", { class: "note", text: "the cleanup policy could not be read" })));

  if (!section.available || section.error) {
    return fill("cleanup-body", panel(
      el("div", { class: "refusal" },
        el("div", { class: "why", text: "the cleanup policy could not be read" }),
        el("div", { class: "core", text: section.error || section.problem || "no detail" }))));
  }

  const policy = section.policy || {};
  const outstanding = section.outstanding || [];
  const panels = [
    panel(
      el("div", { class: "row" },
        el("strong", { text: "cleanup mode" }),
        el("span", { class: `pill ${section.may_write ? "yes" : "no"}`, text: policy.mode || "unknown" })),
      kv(
        ["policy file", section.policy_path],
        ["policy digest", (section.policy_digest || "").slice(0, 12)],
        ["may write", section.may_write ? "yes" : "no"],
        ["python version", policy.python_version || "not pinned"],
      )),
    panel(
      el("div", { class: "row" }, el("strong", { text: "rules" })),
      el("ul", { class: "policy" },
        ...(section.registered_rules || []).map((rule) => {
          const enabled = (policy.enabled_rules || []).includes(rule);
          return el("li", { class: enabled ? "mono" : "mono missing", text: `${rule}${enabled ? "" : " (not enabled)"}` });
        }))),
    panel(
      el("div", { class: "row" }, el("strong", { text: "protected paths" })),
      el("ul", { class: "policy" },
        ...(section.protected_paths || []).map((p) => el("li", { class: "mono", text: p })),
        ...(policy.excluded_paths || []).map((p) => el("li", { class: "mono missing", text: `${p} (excluded by policy)` })))),
    panel(
      el("div", { class: "row" }, el("strong", { text: "pending cleanup" })),
      outstanding.length
        ? el("ul", { class: "policy" }, ...outstanding.map((item) => el("li", { class: "mono", text: `${item.path} — ${item.rule}` })))
        : el("p", { class: "note", text: "nothing is owed: every changed line satisfies the enabled rules" })),
    appliedPanel(section),
    refusalsPanel(section),
    panel(el("p", { class: "note", text: section.note })),
  ];
  return fill("cleanup-body", ...panels);
}

/* One applied cleanup: which file, which rule, what it changed, and the
   receipt that authorized it. Digests are truncated to 12 characters because a
   full sha256 in a table column is unreadable at any width, and a reader who
   wants the whole value has it in the section payload. */
function appliedPanel(section) {
  const applied = section.applied || [];
  const retries = section.already_applied_total || 0;
  const body = applied.length
    ? table(
        ["file", "rule", "before", "after", "receipt"],
        applied.map((record) => [
          el("td", { class: "mono", text: record.path }),
          el("td", { class: "mono", text: record.rule }),
          el("td", { class: "mono", text: shortDigest(record.beforeDigest) }),
          el("td", { class: "mono", text: shortDigest(record.afterDigest) }),
          el("td", { class: "mono", text: receiptSummary(record.receipt) }),
        ]))
    : el("p", { class: "note", text: "no cleanup has been applied in this project" });
  return panel(
    el("div", { class: "row" },
      el("strong", { text: "applied cleanup" }),
      el("span", { class: "pill yes", text: `${applied.length}` })),
    body,
    el("p", { class: "note", text: historyNote(section, retries) }));
}

/* A refusal row states the reason inline because `reason` alone does not tell an
   operator what to do; `detail` is the checker's own sentence and is preferred
   whenever it carries one. */
function refusalsPanel(section) {
  const refusals = section.refusals || [];
  const body = refusals.length
    ? el("ul", { class: "policy" }, ...refusals.map((record) => el("li", {},
        el("span", { class: "mono strong", text: `${record.path} — ${record.reason}` }),
        el("div", { class: "note", text: record.detail || "no detail was recorded" }))))
    : el("p", { class: "note", text: "nothing has been refused" });
  return panel(
    el("div", { class: "row" },
      el("strong", { text: "refused" }),
      el("span", { class: "pill no", text: `${refusals.length}` })),
    body);
}

function shortDigest(value) {
  return value ? String(value).slice(0, 12) : "—";
}

function receiptSummary(receipt) {
  if (!receipt || !receipt.result) return "no receipt";
  if (receipt.checker) return `${receipt.result} (${receipt.checker})`;
  return receipt.result;
}

/* `total` counts every recorded outcome and `truncated` says whether this page
   holds all of them, so the two sentences never disagree about what is on
   screen. `retries` is the AlreadyApplied count, which belongs in neither the
   applied nor the refused panel and would otherwise have no home on the page. */
function historyNote(section, retries) {
  if (section.history_error) {
    return `the cleanup record could not be read: ${section.history_error}`;
  }
  const total = section.total || 0;
  const retry = retries ? ` ${retries} repeated request(s) converged without a second edit.` : "";
  if (!section.truncated) return `Every recorded outcome is shown. ${total} recorded in all.${retry}`;
  return `Showing the newest ${section.limit || 0}. ${total} are recorded in all; the oldest have been dropped.${retry}`;
}

/* --------------------------------------------------------- settings & versions

   The settings section carries two things that must not be confused for each
   other: the components this host has installed, and the project configuration
   this console may edit. The configuration half is below.

   The editor is deliberately one path. A typed control and the JSON editor both
   produce the same document and both go through `preview` first, so the JSON
   editor is not a second, weaker door: it is the same validation with a different
   way of writing the input. */
function configurationEditor(block) {
  if (!block) return null;
  const outcome = el("div", { class: "outcome" });

  /* The local configuration and the approved integration policy, shown together.

     They are different authorities and a page that showed only the local one
     would let an operator read "saved" as "approved for a protected run". The
     approved policy is read-only here and this console cannot change it, so the
     panel says which one decides. */
  const approved = block.approved_integration || {};
  const mismatch = block.mismatch || {};
  const authorityPanel = panel(
    el("div", { class: "row" },
      el("strong", { text: "authority" }),
      el("span", { class: `pill ${approved.context ? "yes" : "no"}`, text: approved.context || "no approved policy" })),
    kv(
      ["local configuration", `local · ${(block.local && block.local.policy_id) || "—"}`],
      ["approved for protected", block.local && block.local.approved_for_protected ? "yes" : "no"],
      ["approved policy", approved.policy_id || "none"],
      ["pinned manifest revision", approved.manifest_revision || "none"],
    ),
    el("p", { class: "note", text: approved.detail || "" }),
    el("p", { class: "note", text: mismatch.detail || "" }),
  );

  const draft = { ...JSON.parse(JSON.stringify(block.current)) };
  draft.cleanup = { ...draft.cleanup };
  const editor = el("textarea", {
    class: "mono config-editor",
    rows: "18",
    "aria-label": "proposed configuration document",
  });
  /* The initial text goes in as a text child rather than through `value`, which
     a document-scanned element may not expose as a writable property. A textarea
     reads its content from its children, so this is both portable and the same
     textContent rule the rest of the page follows. */
  const rendered = JSON.stringify(draft, null, 2);
  editor.append(document.createTextNode(rendered));
  editor.addEventListener("input", () => {
    try {
      const parsed = JSON.parse(editor.value);
      draft.schema_version = parsed.schema_version;
      draft.description = parsed.description;
      draft.required_checks = parsed.required_checks;
      draft.connection = parsed.connection;
      draft.cleanup = parsed.cleanup;
      if (parsed.checks) draft.checks = parsed.checks;
    } catch (_error) {
      /* A half-typed document is not a proposal yet. Preview reports the parse. */
    }
  });

  /* The digest every write is guarded by, read when this view loaded. A second
     tab holding an older one is refused by the server rather than merged, so
     this is the value that makes the refusal happen instead of a silent
     overwrite. */
  const expectedDigest = block.digest;

  const previewButton = el("button", { class: "act", type: "button" }, "Preview this change");
  previewButton.addEventListener("click", () => busy(previewButton, "previewing…", async () => {
    outcome.replaceChildren();
    try {
      const envelope = await api("apply", { operation: "save_project_config", stage: "preview" }, true,
        { document: draft, expected_digest: expectedDigest });
      outcome.replaceChildren(previewReport(envelope.result, draft, expectedDigest, outcome));
      await showIntegrations();
    } catch (error) {
      outcome.replaceChildren(refusal(error));
    }
  }));

  const stages = el("div", { class: "row" }, previewButton);
  return panel(
    el("div", { class: "row" },
      el("strong", { text: "project configuration" }),
      el("span", { class: `pill ${block.editable ? "yes" : "no"}`, text: block.editable ? "editable" : "read-only" })),
    block.problem
      ? el("div", { class: "refusal" },
          el("div", { class: "why", text: "the stored configuration cannot be read" }),
          el("div", { class: "core", text: block.problem }))
      : null,
    kv(
      ["configuration digest", (block.digest || "").slice(0, 12)],
      ["writable documents", (block.paths || []).join(", ")],
      ["candidate revision", block.candidate ? block.candidate.revision : "none saved"],
      ["approved", block.approved ? `${block.approved.revision} (local)` : "not approved"],
      ["active", block.active ? block.active.revision : "not activated"],
    ),
    el("div", { class: "row" }, el("strong", { text: "document" })),
    editor,
    el("p", { class: "note", text: "Every field above is validated as a whole before anything is written, and the JSON editor is validated by the same code as the typed controls. argv is an argument list: one entry per argument, never a shell command." }),
    stages,
    outcome,
  );
}

/* What a change would do, shown before it is done.

   The removal list and the weakening flag are rendered as warnings rather than as
   fields, because they are the two answers that mean "this is not a routine
   settings change". A page that rendered them in the same table as a timeout
   edit would be making the safe answer as quiet as the unsafe one. */
function previewReport(result, draft, expectedDigest, outcome) {
  const children = [
    el("div", { class: "row" },
      el("strong", { text: result.routine ? "routine change" : "this change weakens the contract" }),
      el("span", { class: `pill ${result.routine ? "yes" : "warn"}`, text: result.routine ? "routine" : "not routine" })),
    el("p", { class: "note", text: result.note }),
  ];

  if ((result.removes_obligations || []).length) {
    children.push(el("div", { class: "refusal gap" },
      el("div", { class: "why", text: "removes required obligations" }),
      el("ul", { class: "policy" }, ...result.removes_obligations.map((entry) => el("li", {},
        el("span", { class: "mono strong", text: `${entry.check_id}: ${entry.obligation}` }),
        el("div", { class: "note", text: entry.detail }))))));
  }
  if (result.weakens_cleanup) {
    children.push(el("div", { class: "refusal gap" },
      el("div", { class: "why", text: "removes cleanup write authority" }),
      el("div", { class: "note", text: "the configuration in force authorizes verified cleanup writes and this proposal does not" })));
  }

  children.push(el("div", { class: "row" }, el("strong", { text: "changed" })));
  children.push(result.changed.length
    ? table(["document", "field", "now", "changed to"], result.changed.map((entry) => el("tr", {},
        el("td", { class: "mono wide-hide", text: entry.document }),
        el("td", { class: "mono", text: entry.field }),
        el("td", { class: "mono", text: JSON.stringify(entry.current) }),
        el("td", { class: "mono strong", text: JSON.stringify(entry.changed) }))))
    : el("p", { class: "note", text: "nothing in the document differs from what is in force" }));

  children.push(el("div", { class: "row" }, el("strong", { text: "affected checks" })));
  children.push((result.affected_checks || []).length
    ? el("ul", { class: "policy" }, ...result.affected_checks.map((id) => el("li", { class: "mono", text: id })))
    : el("p", { class: "note", text: "no check's obligations change" }));

  children.push(el("div", { class: "row" }, el("strong", { text: "evidence that becomes stale" })));
  children.push((result.stale_evidence || []).length
    ? table(["check", "run", "verdict"], result.stale_evidence.map((entry) => el("tr", {},
        el("td", { class: "mono", text: entry.check_id }),
        el("td", { class: "mono wide-hide", text: entry.run_id || "—" }),
        el("td", {}, verdict(entry.verdict)))))
    : el("p", { class: "note", text: "no recorded evidence was produced under the configuration in force" }));

  const revision = result.candidate.revision;
  const saveButton = el("button", { class: "act", type: "button" }, "Save this proposal");
  const approveButton = el("button", { class: "act", type: "button" }, "Approve this revision");
  const activateButton = el("button", { class: "act", type: "button" }, "Activate this revision");

  saveButton.addEventListener("click", () => busy(saveButton, "saving…", async () => {
    try {
      const envelope = await api("apply", { operation: "save_project_config", stage: "save" }, true,
        { document: draft, expected_digest: expectedDigest });
      toast(`saved candidate ${envelope.result.candidate.revision.slice(0, 12)}`);
      await showIntegrations();
    } catch (error) { toast(error.message, true); }
  }));

  const step = async (stage, button, done) => busy(button, `${stage}…`, async () => {
    try {
      const envelope = await api("apply", { operation: "save_project_config", stage }, true,
        { expected_digest: expectedDigest, revision });
      toast(`${stage}: ${(envelope.result.note || "").split("\n")[0]}`);
      if (done) done(envelope.result);
      await showIntegrations();
    } catch (error) { toast(error.message, true); }
  });

  approveButton.addEventListener("click", () => step("approve", approveButton));
  activateButton.addEventListener("click", () => step("activate", activateButton));

  children.push(el("div", { class: "row" }, saveButton));
  children.push(el("p", { class: "note", text: "Saving writes the proposal. Approving records that you accept it. Activating adopts it as the local policy a NEW task attempt is admitted under. None of the three releases a task that is already admitted, and none grants protected integration authority." }));
  children.push(el("div", { class: "row" }, approveButton, activateButton));
  return el("div", {}, ...children);
}

async function showIntegrations() {
  const section = await sectionOf("checks", "sections.integrations");
  if (!section) return fill("integrations-body", panel(el("p", { class: "note", text: "the integration record could not be read" })));

  const components = (section.components || []).map((component) => el("tr", {},
    el("td", { class: "mono" }, el("strong", { text: component.id })),
    el("td", { class: "note", text: component.kind }),
    el("td", { class: "mono", text: component.version || "—" }),
    el("td", {}, el("span", { class: `pill ${component.present ? "yes" : "no"}`, text: component.status })),
    el("td", { class: "mono wide-hide", text: component.detail || "" }),
  ));
  const actions = (section.actions || []).filter((action) => action.operation !== "save_project_config").map((action) => el("tr", {},
    el("td", { class: "mono" }, el("strong", { text: action.operation })),
    el("td", { text: action.effect }),
    el("td", { class: "actions" }, action.available
      ? operationButton({ name: action.operation })
      : el("span", { class: "why-not", text: action.reason })),
  ));
  return fill("integrations-body",
    panel(el("p", { class: "note", text: section.note })),
    panel(table(["component", "kind", "version", "status", { label: "detail", hideAt: "wide-hide" }], components)),
    panel(table(["operation", "effect", "apply from here"], actions)),
    configurationEditor(section.proposals),
    connectionPanels(section.connection));
}

/* --------------------------------------------------- the agent connection

   CONFIGURED and CONNECTED are two different facts and the panel never merges
   them. CONFIGURED is what the host's own file says, which the console did not
   write and can only read back. CONNECTED is what a real MCP handshake proved:
   a subprocess launched, an initialize frame answered, tools/list read off the
   wire. The snippet above is a third thing again -- generated configuration,
   pasted into a host that has not run it yet -- and it is labelled that way.

   There is no probe button. `api.py` owns the route table, so there is nowhere
   for a "probe now" request to arrive, and a panel that answered `not_probed`
   until a button was found had not answered the operator's question at all. The
   handshake runs when the host names both a command and a project root, which is
   the only case where it can mean anything. */
function connectionPanels(connection) {
  if (!connection) return null;

  const configured = connection.configured || {};
  const connected = connection.connected || {};
  const state = connected.state;
  /* Three states, three renderings. `not_probed` is not `not_connected`: the
     first says nothing was measured and the second says a measurement failed,
     and a reader who cannot tell them apart will stop reading this field. */
  const verdict = {
    connected: ["yes", "a real MCP handshake completed against this command"],
    not_connected: ["no", connected.reason || "the probe did not complete"],
    not_probed: ["unknown", "nothing is configured on this machine, so nothing was started"],
  }[state] || ["unknown", String(state)];

  const stateRows = [
    el("tr", {},
      el("td", {}, el("strong", { text: "configured" })),
      el("td", {}, el("span", {
        class: `pill ${configured.present ? "yes" : "no"}`,
        text: configured.present ? "yes" : "not configured",
      })),
      el("td", { class: "note", text: configured.present
        ? `${configured.host} names ${configured.command}` +
          (configured.command_exists ? "" : ", which is not a file on this machine")
        : `no ${configured.host} configuration was found on this machine` })),
    el("tr", {},
      el("td", {}, el("strong", { text: "connected" })),
      el("td", {}, el("span", {
        class: `pill ${state === "connected" ? "yes" : "no"}`,
        text: verdict[0],
      })),
      el("td", { class: "note", text: verdict[1] })),
  ];

  return panel(
    el("h3", { text: "Agent connection" }),
    el("p", { class: "note", text: (connection.snippet || {}).note || "" }),
    panel(table(["", "state", "what that means"], stateRows)),
    (connected.state === "connected"
      ? el("p", { class: "note", text:
          `the handshake returned ${connected.tool_names.length} tools over ` +
          `${connected.protocol_version}: ${connected.tool_names.join(", ")}` })
      : null),
    snippetPanel(connection.text),
    cataloguePanel(connection.catalogue),
    capabilityPanel(connection.capability_gaps),
  );
}

/* The snippet, in a pre the operator can select, with a copy button. It is
   never labelled as working: it is configuration, and nothing has run it. */
function snippetPanel(text) {
  if (!text) return null;
  const copy = el("button", { type: "button", text: "Copy" });
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast("configuration copied; it is not a verified connection");
    } catch (error) {
      toast("the browser refused clipboard access; select the text and copy it", true);
    }
  });
  return panel(
    el("div", { class: "row" }, el("strong", { text: "configuration to paste" }), copy),
    el("pre", { class: "mono snippet", text }),
    el("p", { class: "note", text: "Paste this into your host's MCP configuration. It has not been started." }),
  );
}

/* The catalogue, named as the third kind of evidence. `vkit mcp serve --json`
   returns before the transport is touched, so it prints all six tools with the
   MCP SDK absent while the same command without --json exits 5. Showing it
   beside a handshake is what stops it reading as one. */
function cataloguePanel(catalogue) {
  if (!catalogue) return null;
  return panel(
    el("div", { class: "row" }, el("strong", { text: "tool catalogue" })),
    el("p", { class: "note", text: catalogue.note }),
    el("p", { class: "mono note", text: (catalogue.tools || []).map((t) => t.name).join(", ") }),
  );
}

/* What this host can and cannot do. The rows needing lifecycle hooks are the
   point: a standalone MCP host reaches a real verdict by calling task_finalize
   explicitly, and it does NOT inherit Claude's automatic Stop gate. A missing
   gate shown as a row is a fact; the same gate silently assumed is a wait that
   never ends. */
function capabilityPanel(gaps) {
  if (!gaps) return null;
  const rows = (gaps.rows || []).map((row) => el("tr", {},
    el("td", {}, el("span", { class: `pill ${row.supported ? "yes" : "no"}`, text: row.supported ? "yes" : "no" })),
    el("td", {}, el("strong", { text: row.capability })),
    el("td", { class: "note", text: row.requires }),
    el("td", { class: "note", text: row.detail }),
  ));
  return panel(
    el("div", { class: "row" }, el("strong", { text: `capability for this host (${gaps.host})` })),
    el("p", { class: "note", text: gaps.note }),
    table(["supported", "capability", "requires", "what it means"], rows),
  );

}

/* ------------------------------------------------------------------ overview

   The five questions an operator opens the console with, answered in order on
   the page rather than only in the nav. Built from the runs table for any
   verdict, so a green installation fact can never stand in for one. */
async function showOverview() {
  const section = await sectionOf("project", "sections.overview");
  if (!section) return fill("overview-body", panel(el("p", { class: "note", text: "the project record could not be read" })));
  const enrollment = section.enrollment || {};
  const blockers = section.blockers || [];
  return fill("overview-body",
    panel(
      el("div", { class: "row" },
        el("strong", { text: "enrollment" }),
        el("span", { class: `pill ${enrollment.enrolled ? "yes" : "no"}`, text: enrollment.state })),
      kv(
        ["root", section.root],
        ["policy digest", (enrollment.policy_digest || "").slice(0, 12)],
        ["checks registered", String(section.checks_registered)],
        ["state store", section.state_writable ? "writable" : `not writable: ${section.state_detail}`],
      )),
    panel(
      el("div", { class: "row" }, el("strong", { text: "blockers" })),
      blockers.length
        ? el("ul", { class: "policy" }, ...blockers.map((blocker) => el("li", {},
            el("span", { class: "strong", text: blocker.what }),
            el("div", { class: "note", text: `${blocker.detail}. Clears when: ${blocker.clears_when}` }))))
        : el("p", { class: "note", text: "nothing is blocking work on this project" })));
}

function open(view) {
  for (const button of document.querySelectorAll("nav button")) {
    button.classList.toggle("on", button.dataset.view === view);
    button.setAttribute("aria-current", button.dataset.view === view ? "page" : "false");
  }
  for (const section of document.querySelectorAll(".view")) {
    section.classList.toggle("on", section.id === view);
    section.hidden = section.id !== view;
  }
  return VIEWS[view]();
}

for (const button of document.querySelectorAll("nav button")) {
  button.addEventListener("click", () => open(button.dataset.view));
}
$("refresh-runs").addEventListener("click", () => busy($("refresh-runs"), "loading…", showRuns));

/* A failed fetch is a state the page must show rather than swallow: without this
   the operator sees the last good values and cannot tell they are stale. */
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  if (reason && reason.status) {
    toast(reason.message, true);
    return;
  }
  toast(String(reason && reason.message ? reason.message : reason), true);
});

open("home");
