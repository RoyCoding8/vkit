"""The landing page answers the operator's questions, and asserts no verdict it cannot support.

**The page is exercised by running it, not by reading it.** Whether a value from
the API becomes text or markup is decided at runtime by which DOM method the
page calls, so the hostile-value cases execute `app.js` against a DOM with a real
`fetch` pointed at a real console, then read back the rendered text and the
element tree. Reading the source could only ever prove a string is absent from
it. The DOM below is small and written out here for the same reason: a
substitute that a reader can check is worth more than a browser stack this
checkpoint is not allowed to install, and its `innerHTML` setter deliberately
parses markup, so the unsafe path is observable rather than absent.

**Node is a host capability, not a dependency.** Where `node` is absent the two
DOM cases skip and say so, because a skip naming the missing facility is an
honest result and a fabricated pass is not. Everything else here runs anywhere.

**Every expected value is a literal,** written out rather than read back from
the API the page called, because an assertion whose expected value comes from the
code under test cannot fail for a defect.

**No process is launched directly.** Every subprocess this file starts goes
through `tests/subproc.py`, which carries the Windows window-suppression
keywords. `tests/test_subprocess_windows.py` walks this file's AST and fails on
a direct `subprocess` call, so the route is structural rather than a convention.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import urllib.request
from pathlib import Path

import pytest

import subproc

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC = REPO_ROOT / "src" / "vkit" / "console" / "static"
STYLESHEET = STATIC / "style.css"
EXAMPLE = REPO_ROOT / "examples" / "python-cli"

#: The id the example manifest registers. Written out rather than read from the
#: manifest, so a manifest that stopped registering it fails here instead of
#: making the assertion agree with itself.
CHECK_ID = "totals-behavior"

#: Every view the page must keep reachable, in the order the nav presents them.
#: Checkpoint 12.2 adds five; nothing in this list may disappear, because each is
#: a capability that exists nowhere else in this build. The order is asserted
#: against the served nav, so it is the real reading order rather than a list.
REQUIRED_VIEWS = (
    "home", "overview", "checks", "evidence", "tasks", "cleanup", "integrations",
    "project", "setup", "runs", "recovery", "operations",
)

#: Where each view renders. Home answers four questions and so has four
#: containers rather than the one body every other view has; naming them here is
#: what keeps a landing panel from being added without somewhere to draw.
VIEW_BODIES = {
    "home": ("home-project", "home-available", "home-setup", "home-next"),
    "overview": ("overview-body",),
    "checks": ("checks-body",),
    "evidence": ("evidence-body",),
    "tasks": ("tasks-body",),
    "cleanup": ("cleanup-body",),
    "integrations": ("integrations-body",),
    "project": ("project-body",),
    "setup": ("setup-body",),
    "runs": ("runs-body", "run-detail"),
    "recovery": ("recovery-body",),
    "operations": ("operations-body",),
}

#: The two values the acceptance table requires to be inert, in the two forms
#: that matter. A script element and an event handler on an image create
#: different things, and a page that neutralised only the first would pass a
#: script assertion and fire on the second.
HOSTILE_SCRIPT = "<script>alert(1)</script>"
HOSTILE_IMAGE = '<img src=x onerror="alert(1)">'


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8")


@pytest.fixture(scope="module")
def console(tmp_path_factory: pytest.TempPathFactory):
    """One live console over a real repository, serving the real static files.

    Port 0 so a suite cannot collide with a console the operator already has
    open. The repository is a copy of the example, git-initialised, because
    `open_project` resolves the root through git and a project with no commit
    has no HEAD for the page to show.
    """
    from vkit.console import operations, server

    root = tmp_path_factory.mktemp("home-view") / "space repo"
    shutil.copytree(EXAMPLE, root)
    for args in (["git", "init", "-q"], ["git", "add", "-A"]):
        subproc.run(args, cwd=root, check=True, capture_output=True)
    subproc.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "example"],
        cwd=root, check=True, capture_output=True,
    )

    console_ = server.serve(operations.open_context(root), port=0)
    thread = threading.Thread(target=console_.serve_forever, name="vkit-home-view", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{console_.server_address[1]}"
    finally:
        console_.shutdown()
        console_.server_close()
        thread.join(timeout=10)


# --------------------------------------------------------------- the page


def test_the_landing_page_answers_the_operator_questions_in_order(console: str) -> None:
    """The first screen is Home, and its four headings are the four questions.

    A landing page that opens on a detail view makes the operator hunt for the
    answer to their first question, which is the behaviour this rewrite exists
    to remove. The order is asserted as well as the presence, because the
    sequence is the design: a reader who reads only the first heading learns
    which project they are in.
    """
    page = _get_text(f"{console}/")

    assert "__VKIT_TOKEN__" not in page, (
        "the served page still carries the unsubstituted placeholder, so nothing "
        "on it could hold a session token and every mutation would be refused"
    )
    assert '<section id="home" class="view on">' in page, (
        "the landing page does not open on the home view, so the operator lands on "
        "a detail view and has to hunt for the answer to the first question"
    )
    assert '<button data-view="home" class="on" aria-current="page">' in page, (
        "the nav does not mark Home as the current view, so the page shows its "
        "landing section with no nav item saying where the reader is"
    )

    headings = re.findall(r"<h3>(.*?)</h3>", page, flags=re.DOTALL)
    assert headings == [
        "Which project is open",
        "What is available",
        "What needs setup",
        "What to do next",
    ], f"the four questions are presented as {headings}"

    positions = [page.index(f"<h3>{heading}</h3>") for heading in headings]
    assert positions == sorted(positions), "the four questions are not in the order asked"


def test_every_view_is_still_reachable_from_the_nav(console: str) -> None:
    """No capability is deleted, and no nav button is a dead control.

    A button pointing at a section with no handler renders and then does
    nothing, which is the one failure a nav test that only counts buttons
    misses, so the handler is checked by name as well.
    """
    page = _get_text(f"{console}/")
    script = _get_text(f"{console}/app.js")

    assert re.findall(r'<button data-view="([^"]+)"', page) == list(REQUIRED_VIEWS), (
        "the nav does not carry every view; a removed button deletes a capability "
        "that exists nowhere else in this build"
    )
    for view in REQUIRED_VIEWS:
        assert f'<section id="{view}" class="view' in page, f"no section for the {view} view"
        assert re.search(rf"^\s+{view}: show[A-Z]", script, flags=re.MULTILINE), (
            f"the nav offers {view} but no view function renders it, so that button "
            f"is a control that can never do anything"
        )
        for body_id in VIEW_BODIES[view]:
            assert f'id="{body_id}"' in page, f"the {view} view has nowhere to render {body_id}"


def test_every_control_is_labelled_and_reachable_by_keyboard(console: str) -> None:
    """Every control has a name a screen reader can announce, and a visible focus ring.

    `aria-label` on the nav region and a real `<label for>` on the runs input are
    the two places this page has a control whose name is not its own text, so
    those are what the assertions name. The order follows the operator's path.
    """
    page = _get_text(f"{console}/")
    css = STYLESHEET.read_text(encoding="utf-8")

    assert '<nav aria-label="Views">' in page, (
        "the nav is an unlabelled landmark, so a screen reader announces a bare "
        "list of buttons with nothing saying what they navigate"
    )
    assert '<label for="limit">' in page, "the runs limit input has no associated label"
    assert 'aria-describedby="limit-help"' in page, (
        "the runs limit input has help text nothing tells the reader about"
    )
    assert 'aria-live="polite"' in page, (
        "no view announces its own load, so a slow read is silent"
    )
    assert ":focus-visible" in css, (
        "no visible focus ring, so a keyboard user cannot tell where they are"
    )
    # The nav buttons are real buttons, so a tab reaches them without a tabindex
    # or a key handler. A click-only control is the way that breaks, and the
    # only listener the page attaches to a nav button is a click.
    assert 'nav button.addEventListener' not in _get_text(f"{console}/app.js")


def test_the_page_is_usable_in_a_narrow_window(console: str) -> None:
    """The stylesheet carries a narrow-viewport rule, and the content can reflow.

    The seven nav buttons are the first thing to overflow, so the assertion is
    that the nav wraps rather than forcing the document sideways, and that long
    unbroken paths wrap inside their own column instead of widening the page.
    """
    css = STYLESHEET.read_text(encoding="utf-8")
    assert "@media (max-width: 720px)" in css, (
        "no narrow-viewport rule, so a 720px window is the only width this page "
        "was ever designed for"
    )
    assert re.search(r"nav\s*\{[^}]*flex-wrap:\s*wrap", css), (
        "the nav does not wrap, so seven buttons push the document sideways and "
        "the header scrolls out from under the reader"
    )
    assert ".kv dd" in css and "overflow-wrap" in css, (
        "a long project path does not wrap, so a deep checkout widens the page"
    )


# ------------------------------------------- installation vs task readiness


def test_installation_health_and_task_readiness_are_not_the_same_finding(console: str) -> None:
    """The page has no label that a doctor fact and a verification verdict share.

    The old page rendered "Readiness" and "not ready" above a table of
    prerequisite findings, on a project where nothing had been verified: every
    tool on PATH produced a green verdict that read exactly like a passed task.

    The premise is measured rather than assumed, and it is the whole reason this
    test exists: on this project `/api/readiness` reports `ok: true` while no task
    has been verified. If the page presented both through one label, that
    contradiction is what the operator would see. So the test compares the two
    API facts to the two labels on the page, and then checks where the
    verification sentence is built from.
    """
    page = _get_text(f"{console}/")
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    readiness = _get(f"{console}/api/readiness")
    runs = _get(f"{console}/api/runs?limit=5")
    assert readiness["ok"] is True, (
        "this fixture no longer proves the confusion it was built to prove: the "
        "host-setup probe passes, so the page has a green installation fact "
        "available to mislabel as a task verdict"
    )
    assert runs["runs"] == [], "this fixture is meant to be a project with no runs"

    assert 'data-view="readiness"' not in page, (
        "the nav still offers a view called readiness. The name is what the "
        "operator reads, and it is the word the old page used for doctor facts"
    )
    assert "not ready" not in page, (
        "the page still renders the words 'not ready' beside the host-setup probe. "
        "That phrase is a verification verdict and this data is not one, which is "
        "the exact confusion this rewrite removes"
    )
    assert '"ready"' not in script and "text: \"ready\"" not in script, (
        "app.js still renders a bare 'ready' verdict from the readiness document"
    )

    # The verification sentence is derived from the runs table alone. If the
    # function building it started reading the readiness document, the two would
    # be welded back into one claim and this fails.
    standing = re.search(r"function taskStanding\(([^)]*)\) \{(.*?)\n\}", script, flags=re.DOTALL)
    assert standing, "the page has no separate rendering for what a run has proven"
    assert standing.group(1).strip() == "runs", (
        f"the task-readiness function takes {standing.group(1)!r} rather than the "
        f"runs table, so its verdict is sourced from something other than evidence"
    )
    body = standing.group(2)
    assert "api(" not in body, (
        "the task-readiness panel fetches something of its own, so installation "
        "health and task readiness can be welded back into a single claim"
    )
    assert "no evidence" in body, (
        "with no runs recorded the page must say there is no evidence rather than "
        "leaving the verification question unanswered, which is the gap an "
        "installation fact used to fill"
    )


def test_the_setup_view_says_it_is_not_a_verification_result(console: str) -> None:
    """The view carrying the doctor facts states the limit of what it measures.

    The relabel moves the wrong word out; this states the right one in, because
    an operator who arrives from a link has not read the nav. The sentence is
    asserted in the served page, which is where the operator meets it.
    """
    page = _get_text(f"{console}/")
    assert "This is not a verification result" in page, (
        "the setup view does not say that host setup is not a verification "
        "result, so a reader arriving from a link is left to infer the distinction"
    )
    assert '<h2>Setup</h2>' in page and '<section id="setup" class="view' in page


# ------------------------------------------------------------ hostile values

#: The DOM harness. Small enough to read, and strict enough to fail: `textContent`
#: is a text node, `setAttribute` is an attribute, and the `innerHTML` setter
#: parses markup into element names, so a value that reached it is observable
#: rather than silently dropped. The parse is a tag scanner rather than a real
#: HTML parser because the assertions are about what the page *builds*, and the
#: page's own ids and handlers come from the served file.
DRIVER = r"""
const fs = require("fs");
const http = require("http");
const path = require("path");

const staticDir = process.argv[1];
const baseUrl = process.argv[2];
const writes = JSON.parse(process.argv[3]);
const report = { panels: {}, innerHTMLWrites: 0 };

class Element {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.parent = null;
    this.attrs = {};
    this.text = "";
  }

  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    return this.text;
  }
  get content() { return this.attrs.content || ""; }
  get value() { return this.attrs.value || ""; }

  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return key in this.attrs ? this.attrs[key] : null; }
  removeAttribute(key) { delete this.attrs[key]; }

  set className(value) { this.attrs.class = String(value); }
  get className() { return this.attrs.class || ""; }
  get classList() {
    const self = this;
    return {
      toggle(name, on) {
        const has = (" " + (self.attrs.class || "") + " ").indexOf(" " + name + " ") >= 0;
        const want = on === undefined ? !has : Boolean(on);
        const set = (self.attrs.class || "").split(/\s+/).filter(Boolean);
        self.attrs.class = want
          ? set.concat([name]).join(" ")
          : set.filter((c) => c !== name).join(" ");
        return want;
      },
      contains(name) {
        return (" " + (self.attrs.class || "") + " ").indexOf(" " + name + " ") >= 0;
      },
    };
  }
  get dataset() {
    const self = this;
    return new Proxy({}, {
      get(_t, key) {
        const name = "data-" + String(key).replace(/[A-Z]/g, (m) => "-" + m.toLowerCase());
        return self.attrs[name];
      },
    });
  }
  get hidden() { return "hidden" in this.attrs; }
  set hidden(value) { if (value) this.attrs.hidden = ""; else delete this.attrs.hidden; }

  append(...kids) {
    for (const kid of kids) {
      if (kid === null || kid === undefined) continue;
      kid.parent = this;
      this.children.push(kid);
    }
  }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  addEventListener() {}

  set innerHTML(value) {
    report.innerHTMLWrites += 1;
    this.parsed = String(value).match(/<([a-zA-Z][a-zA-Z0-9]*)\b/g) || [];
  }
  get innerHTML() { return ""; }

  walk(visit) {
    for (const kid of this.children) {
      if (kid.nodeType !== 1) continue;
      visit(kid);
      kid.walk(visit);
    }
  }
  tags() { const found = []; this.walk((n) => found.push(n.tagName)); return found; }
  descendants() { const found = []; this.walk((n) => found.push(n)); return found; }
  matches(selector) {
    const trimmed = selector.trim();
    if (trimmed.startsWith(".")) return this.classList.contains(trimmed.slice(1));
    const attribute = /^\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(trimmed);
    if (attribute) {
      if (!(attribute[1] in this.attrs)) return false;
      return attribute[2] === undefined || this.attrs[attribute[1]] === attribute[2];
    }
    const tag = /^([a-zA-Z][a-zA-Z0-9]*)/.exec(trimmed);
    return tag ? this.tagName === tag[1].toUpperCase() : false;
  }
}

class TextNode {
  constructor(value) { this.nodeType = 3; this.text = String(value); }
  get textContent() { return this.text; }
}

/* The served page, scanned into a tree. Void elements and text nodes are not
   needed: the assertions read ids, classes, and what the script builds. */
function build(html) {
  const root = new Element("root");
  const stack = [root];
  const open = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^<>]*?)?)(\/?)>/g;
  let match;
  while ((match = open.exec(html)) !== null) {
    const [, closing, rawTag, rawAttrs, selfClosing] = match;
    if (closing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const node = new Element(rawTag);
    for (const attr of rawAttrs.matchAll(/([a-zA-Z-]+)="([^"]*)"/g)) {
      node.attrs[attr[1]] = attr[2];
    }
    if (rawTag === "meta" && node.attrs.name === "vkit-token") {
      /* The page reads its session token from here at load. Substituted with a
         stand-in: this harness never issues a mutation, and a real credential
         in a test's argv would be one. */
      node.attrs.content = "test-token-not-a-credential";
    }
    stack[stack.length - 1].append(node);
    if (!selfClosing && rawTag !== "meta") stack.push(node);
  }
  return root;
}

function main() {
  const port = new URL(baseUrl).port;
  const root = build(
    fs.readFileSync(path.join(staticDir, "index.html"), "utf8")
      .replace(/__VKIT_TOKEN__/g, "test-token-not-a-credential"),
  );
  const byId = new Map();
  root.walk((node) => { if (node.attrs.id) byId.set(node.attrs.id, node); });

  const document = {
    documentElement: root,
    body: root,
    getElementById: (id) => byId.get(id) || null,
    querySelector: (selector) => root.descendants().find((n) => n.matches(selector)) || null,
    querySelectorAll: (selector) => {
      if (selector === "nav button") {
        return root.descendants().filter((n) => {
          let node = n;
          while (node) {
            if (node.tagName === "NAV") return n.tagName === "BUTTON";
            node = node.parent;
          }
          return false;
        });
      }
      return root.descendants().filter((n) => n.matches(selector));
    },
    createElement: (tag) => new Element(tag),
    createTextNode: (value) => new TextNode(value),
  };

  /* Write a poisoned value at a stated path. The path is data rather than a
     function so no source crosses into the driver, and a typo names a field
     that is not there instead of quietly passing the body through. */
  const setAt = (body, dotted, value) => {
    const steps = dotted.split(".");
    let node = body;
    for (const step of steps.slice(0, -1)) {
      if (node === null || typeof node !== "object") {
        throw new Error("no path " + dotted + ": " + step + " is not there");
      }
      node = node[step];
    }
    const last = steps[steps.length - 1];
    if (node === null || typeof node !== "object") throw new Error("no path " + dotted);
    node[last] = value;
  };

  const load = (target) => new Promise((resolve, reject) => {
    const [path, query] = target.split("?");
    const route = path.replace(/^\/api\//, "");
    const request = http.request(
      { host: "127.0.0.1", port, path: target, method: "GET" },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          const raw = Buffer.concat(chunks).toString("utf8");
          const body = raw ? JSON.parse(raw) : {};
          for (const [field, value] of Object.entries(writes)) {
            if (field.split(":")[0] !== route) continue;
            setAt(body, field.slice(field.indexOf(":") + 1), value);
          }
          resolve({ status: response.statusCode, body });
        });
      },
    );
    request.on("error", reject);
    request.end();
    void query;
  });

  let inflight = 0;
  const pageFetch = (url) => {
    inflight += 1;
    return load(new URL(url, baseUrl).pathname + new URL(url, baseUrl).search)
      .then((answer) => ({
        ok: answer.status === 200, status: answer.status, json: async () => answer.body,
      }))
      .finally(() => { inflight -= 1; });
  };

  const appSource = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
  /* `new Function` gives the script its own scope, so nothing the page declares
     escapes it and the harness cannot reach the router. The app already treats
     `window` as a global it may attach to, so the last line of the wrapped
     source hands the harness the two entry points it needs and nothing else. */
  const reachable = appSource + ";window.__vkit = { open: open, api: api };";
  const run = new Function(
    "document", "window", "fetch", "setTimeout", "clearTimeout", "URL", "URLSearchParams", reachable,
  );
  const pageWindow = { addEventListener() {} };
  run(document, pageWindow, pageFetch, () => 0, () => {}, URL, URLSearchParams);
  const openView = pageWindow.__vkit.open;

  /* Waiting for the sockets is not enough, and neither is spinning. Each route
     resolves, then the page reads `response.json()`, then it awaits four
     promises before it fills a panel, so the last byte arriving is several
     microtasks before the tree is built. And a loop of `setImmediate` burns
     through its turns in well under a millisecond, which never gives the event
     loop the chance to do the socket reads the page is actually waiting on, so
     it observes four requests in flight forever.

     So this waits on real timers until no request is open, then keeps turning
     for a further stretch so the page's own `await` chains run out. Real time
     passes, and the page's rendering is not raced. */
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const drain = async () => {
    for (let turn = 0; turn < 600 && inflight > 0; turn += 1) await pause(10);
    for (let turn = 0; turn < 20; turn += 1) await pause(10);
  };

  /* Every view is opened, not just the one the entry call lands on. A panel
     that reads empty because nothing ever navigated to it proves nothing, and
     an operator reaches each view from the nav, so the harness does the same.
     Home is opened first and settled before the rest, so the landing page is
     measured as an operator would meet it rather than after it has been left.

     The list is the page's own nav, read out of the served page rather than
     written out here. A hardcoded copy would go stale the moment a view was
     added, and the harness would then open the old set and report every new
     panel as missing -- a failure that reads as a page defect and is not one. */
  const VIEWS = root.descendants()
    .filter((n) => n.tagName === "BUTTON" && (n.attrs["data-view"] || ""))
    .map((n) => n.attrs["data-view"]);
  const opened = VIEWS.reduce(
    (chain, view) => chain.then(() => openView(view)).then(drain),
    Promise.resolve(),
  );

  return opened.then(drain).then(() => {
    for (const id of [
      "home-project", "home-available", "home-setup", "home-next",
      "project-body", "setup-body", "checks-body", "runs-body", "run-detail",
      "recovery-body", "operations-body",
      "overview-body", "evidence-body", "tasks-body", "cleanup-body",
      "integrations-body",
    ]) {
      const node = byId.get(id);
      report.panels[id] = node
        ? { missing: false, text: node.textContent, tags: node.tags() }
        : { missing: true, text: "", tags: [] };
    }
    report.rendered = root.descendants().length;
    process.stdout.write(JSON.stringify(report));
  });
}

main().catch((error) => {
  process.stdout.write(JSON.stringify({ error: String((error && error.stack) || error) }));
  process.exitCode = 1;
});
"""


def _node() -> str | None:
    return shutil.which("node")


def _render(console: str, writes: dict[str, str]) -> dict:
    """Run `app.js` against the live console, with named fields poisoned.

    `writes` maps `"<route>:<dotted.path>"` to the value that field should carry,
    so the page takes its ordinary code path and the values it renders are the
    values the API returned with that one field replaced.
    """
    done = subproc.run(
        [_node(), "-e", DRIVER, str(STATIC), console, json.dumps(writes)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=False,
    )
    assert done.returncode == 0, f"the page driver failed:\n{done.stdout}\n{done.stderr}"
    assert done.stdout.strip(), f"the page driver printed nothing:\n{done.stderr}"
    result = json.loads(done.stdout)
    assert "error" not in result, f"the page did not render: {result.get('error')}"
    return result


@pytest.mark.skipif(_node() is None, reason="requires a node binary to render the page's DOM")
def test_a_project_name_containing_markup_is_rendered_as_text(console: str) -> None:
    """`<script>alert(1)</script>` in the project root reaches the operator as words.

    The payload is written into the real `project` route's `root` field, and both
    the landing page and the project view are rendered against it. Two halves,
    and the second is the one that matters: the payload is visible as literal
    text, and no `SCRIPT` element exists in the rendered tree. A page that
    showed it correctly as text and also left a parsed copy behind would pass the
    first half and fail the second.
    """
    result = _render(console, {"project:root": HOSTILE_SCRIPT})

    project = result["panels"]["project-body"]
    assert HOSTILE_SCRIPT in project["text"], (
        f"the project root is not shown as the text the API returned; the panel "
        f"reads {project['text'][:200]!r}"
    )
    assert "SCRIPT" not in project["tags"], (
        f"the project view built a SCRIPT element ({project['tags']}), so the value "
        f"was parsed as markup rather than shown as text"
    )

    home = result["panels"]["home-project"]
    assert HOSTILE_SCRIPT in home["text"], (
        f"the landing page does not show the project root it was given; it reads "
        f"{home['text'][:200]!r}"
    )
    assert "SCRIPT" not in home["tags"], (
        f"the landing page's project panel built a SCRIPT element ({home['tags']})"
    )
    assert result["innerHTMLWrites"] == 0, (
        f"app.js assigned innerHTML {result['innerHTMLWrites']} time(s) while "
        f"rendering. A value that reaches innerHTML is a value the browser parses "
        f"as markup, whatever the value happens to contain"
    )


@pytest.mark.skipif(_node() is None, reason="requires a node binary to render the page's DOM")
def test_a_check_description_containing_markup_is_rendered_as_text(console: str) -> None:
    """`<img src=x onerror=...>` in a check description reaches the operator as words.

    The second payload, and the one a script-tag check does not cover: an image
    carrying an event handler creates an element without creating a script, so a
    page that only escaped angle brackets would pass the assertion above and fire
    here. The `IMG` tag is checked as well as the visible text, so a payload that
    became any element at all fails.
    """
    result = _render(console, {"checks:checks.0.description": HOSTILE_IMAGE})

    available = result["panels"]["home-available"]
    assert HOSTILE_IMAGE in available["text"], (
        f"the check description is not shown as literal text; the panel reads "
        f"{available['text'][:300]!r}"
    )
    assert "IMG" not in available["tags"], (
        f"the payload became an IMG element ({available['tags']}), so the "
        f"description was parsed as markup rather than shown as text"
    )

    checks = result["panels"]["checks-body"]
    if not checks["missing"]:
        assert "IMG" not in checks["tags"], (
            f"the checks view built an IMG element from a description "
            f"({checks['tags']})"
        )
    assert result["innerHTMLWrites"] == 0, (
        f"app.js assigned innerHTML {result['innerHTMLWrites']} time(s) while "
        f"rendering a poisoned check description"
    )


@pytest.mark.skipif(_node() is None, reason="requires a node binary to render the page's DOM")
def test_the_landing_page_renders_this_projects_own_values(console: str) -> None:
    """With nothing poisoned, the landing page shows this project's real identity.

    The hostile cases prove the page does not execute what it is given. This one
    proves it renders something at all, and that the values on screen came from
    the API rather than from placeholders: the root, the head prefix and the
    check id are all read from the routes the page itself calls.
    """
    result = _render(console, {})

    project_api = _get(f"{console}/api/project")
    root_text = result["panels"]["home-project"]["text"]
    assert project_api["root"] in root_text, (
        f"the landing page does not name the open project; it reads {root_text[:200]!r}"
    )
    assert project_api["source"]["head"][:12] in root_text, (
        "the landing page does not show which HEAD the project is on, so an "
        "operator cannot tell whether they are looking at the code they think"
    )

    available = result["panels"]["home-available"]["text"]
    assert CHECK_ID in available, (
        f"the landing page does not list the check this project registers; it "
        f"reads {available[:300]!r}"
    )

    setup = result["panels"]["home-setup"]["text"]
    assert "no evidence" in setup, (
        f"with no runs recorded the landing page must say there is no evidence; it "
        f"reads {setup[:300]!r}"
    )
    assert result["innerHTMLWrites"] == 0, (
        "app.js assigned innerHTML while rendering this project's own values"
    )
