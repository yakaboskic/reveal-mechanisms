/** A self-contained HTML snapshot of the investigation the user is looking at: the gap, the factors, the
 * feasibility assessment, and the investigation result, including the account graph as it is drawn. */

const reportStyle = `
:root { color-scheme:light; --base:#f6f5f2; --ink:#33363d; --blue:#2c5c97; --orange:#e07b39; --orange-dark:#c2662b; --orange-soft:#fbeee3; --muted:#6b6b6b; --line:#e6e1d6; --error:#b3261e; }
* { box-sizing:border-box; }
body { margin:0; background:var(--base); color:var(--ink); font-family:"Segoe UI",-apple-system,BlinkMacSystemFont,Arial,sans-serif; font-size:15px; line-height:1.6; }
main.report { width:100%; margin:0 auto; background:white; border:0; border-radius:0; padding:2rem 2.25rem 3rem; }
h3,h4,p { margin:0; }
h3 { font-size:20px; font-weight:650; letter-spacing:-.3px; line-height:1.35; }
h4 { font-size:15px; font-weight:650; line-height:1.4; }
p + p { margin-top:.75rem; }
ul { margin:.45rem 0 .2rem; padding-left:1.2rem; }
li { margin:.4rem 0; line-height:1.55; }
li::marker { color:var(--orange); }
a { color:var(--blue); text-underline-offset:3px; }
.report-fields { padding-bottom:.15rem; }
.report-fields p { margin:.55rem 0 0; font-size:16px; line-height:1.5; }
.report-fields p:first-child { margin-top:0; }
.report-dapper { margin:20px 0; border:1px solid var(--line); border-radius:8px; background:var(--base); }
.report-dapper summary { display:flex; align-items:center; gap:.7rem; padding:.8rem 1rem; cursor:pointer; color:var(--ink); font-size:14px; font-weight:650; list-style:none; }
.report-dapper summary::-webkit-details-marker { display:none; }
.report-dapper summary::before { content:""; flex:none; width:.42rem; height:.42rem; margin-top:-.15rem; border:solid var(--ink); border-width:0 1.6px 1.6px 0; transform:rotate(-45deg); }
.report-dapper[open] summary { border-bottom:1px solid var(--line); }
.report-dapper[open] summary::before { margin-top:.15rem; transform:rotate(45deg); }
.report-dapper p { margin:0; padding:.9rem 1rem 1rem; color:var(--muted); font-size:14px; }
.report-dapper pre { margin:0; max-height:32rem; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font-size:12px; line-height:1.5; background:white; padding:1rem 1.05rem 1.1rem; border-radius:0 0 8px 8px; }
.report-fields strong,.activity-status-row > strong { font-size:13px; font-weight:650; }
.report-block + .report-block { margin-top:1.6rem; padding-top:1.35rem; border-top:1px solid var(--line); }
.feasibility-report { display:grid; gap:.2rem; margin:0; }
.feasibility-report h3 { margin:0 0 .7rem; }
.feasibility-report h4 { margin:1.15rem 0 .2rem; font-size:14px; color:var(--orange-dark); }
.feasibility-report p { font-size:15px; line-height:1.65; }
.feasibility-report ul { margin:.2rem 0 .15rem; }
.activity-status-row { display:flex; align-items:center; flex-wrap:wrap; gap:.45rem .55rem; margin:.35rem 0 .7rem; font-weight:400; }
.activity-status-row p { margin:0; font-size:14px; font-weight:400; line-height:28px; }
.activity-status-row .status { display:inline-flex; align-items:center; box-sizing:border-box; height:28px; padding-top:0; padding-bottom:0; font-weight:400; line-height:1; }
.status { display:inline-block; border-radius:4px; padding:.3rem .55rem; font-size:13px; background:var(--base); color:var(--muted); font-weight:600; white-space:nowrap; }
.status.succeeded { color:#196254; background:#e4f2ec; }
.status.failed { color:var(--error); background:#fdf2f1; }
.status.running { color:var(--orange-dark); background:var(--orange-soft); }
.notice { background:var(--orange-soft); border:1px solid #f0d3bc; border-radius:4px; padding:.7rem .9rem; margin-bottom:1rem; font-size:14px; }
.muted { color:var(--muted); }
.results { padding-top:1.35rem; margin-top:1.35rem; border-top:1px solid var(--line); }
.results > h3 { margin-bottom:.85rem; }
.result-record h4 { margin-bottom:.7rem; font-size:16px; }
.result-record p,.research-text { font-size:15px; line-height:1.7; }
.outcome-section { margin-top:.85rem; font-size:14px; }
.scope-note { border-left:3px solid var(--orange); padding-left:.7rem; margin-top:1rem; color:#6b4e32; }
.paragraph-result { margin-top:1.35rem; }
.paragraph-result > h3 { margin-bottom:.7rem; }
.error-text { color:var(--error); }
.settings-note { margin-top:1rem; color:var(--muted); font-size:14px; }
.account-net-figure { margin:0 0 1.2rem; }
.account-net-legend { display:flex; align-items:center; gap:.4rem; margin:0 0 .7rem; color:var(--muted); font-size:12px; font-weight:650; }
.account-swatch { width:18px; height:6px; border-radius:999px; }
.account-swatch.support { background:#c5daf0; }
.account-swatch.dispute { background:#f3ccc8; }
.account-swatch.plain { background:#b7b9be; }
.account-swatch.cfde { background:repeating-linear-gradient(90deg,#8cc3a5 0 6px,transparent 6px 10px); }
.account-net-scroll { max-width:100%; overflow-x:auto; overflow-y:hidden; }
.account-net { position:relative; display:block; overflow:hidden; }
.account-net-col { position:relative; z-index:1; display:flex; flex-direction:column; justify-content:center; min-width:0; }
.account-net-col > h3 { margin:0 0 25px; color:var(--muted); font-size:12px; font-weight:650; line-height:1.3; text-align:center; }
.account-net-pad-right { padding-right:16px; }
.account-net-pad { padding-right:16px; padding-left:16px; }
.account-net-pad-left { padding-left:16px; }
.account-net-col > article + article { margin-top:.65rem; }
.account-node { position:relative; background:#efefef; border:1px solid #dddddd; border-radius:10px; padding:.55rem .7rem; }
.account-node span,.account-dataset-name { display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden; color:var(--ink); font-size:13px; font-weight:600; line-height:1.35; }
.account-dataset-name { text-decoration:none; }
.account-node small { display:block; margin-top:.25rem; color:var(--muted); font-size:12px; font-weight:400; line-height:1.35; }
.account-net-note { margin:-.3rem 0 .7rem; color:var(--muted); font-size:12px; }
.account-node.account-dataset { background:#fff; border-color:#a6cebb; }
.account-dataset-name i { color:var(--blue); font-style:normal; }
.account-dataset-orgs { display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; margin-top:.45rem; }
.account-org { display:inline-flex; line-height:0; }
.account-org-logo { display:block; width:26px; height:26px; object-fit:contain; }
.account-org-logo.is-wide { width:auto; max-width:150px; }
.account-org-initials { display:inline-grid; place-items:center; width:26px; height:26px; border-radius:50%; background:#e7f3ec; color:#1f7a4d; font-size:11px; font-weight:700; text-decoration:none; }
.factor-net-edges { position:absolute; inset:0; width:100%; height:100%; overflow:visible; pointer-events:none; }
.net-context-bubbles { display:flex; flex-wrap:wrap; gap:.4rem; margin:.7rem 0 0; padding:0; list-style:none; }
.net-context-bubbles li { max-width:100%; background:#e7f3ec; color:#1f7a4d; border:1px solid #a6cebb; border-radius:999px; padding:.2rem .7rem; font-size:12px; font-weight:600; line-height:1.4; }
.net-context-bubbles.account-cfde-bubble { margin-top:.5rem; }
.net-context-bubbles.account-cfde-bubble.is-unverified li { background:#f3f3f1; color:var(--muted); border-color:#dddddd; }
`;

function escapeHtml(value: string) {
  return value.replace(/[&<>"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char] || char));
}

function reportFilename(title: string) {
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
  return `reveal-report-${slug || "investigation"}.html`;
}

function dataUrl(blob: Blob) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

/** Organization logos are same-origin images. Embedding them keeps the saved file faithful offline. */
async function inlineImages(root: ParentNode) {
  await Promise.all([...root.querySelectorAll("img")].map(async img => {
    const source = img.getAttribute("src");
    if (!source || source.startsWith("data:")) return;
    try {
      const response = await fetch(new URL(source, location.origin));
      if (!response.ok) return;
      img.setAttribute("src", await dataUrl(await response.blob()));
    } catch { /* The saved file keeps the original address. */ }
  }));
}

/** The saved graph keeps the on-screen geometry. Edges are already drawn in that box, so each node and
 * heading is pinned to the same rectangle and the box keeps that height. A later reflow would move the
 * cards off the lines and let them cover the account text. */
function pinBox(source: HTMLElement, target: HTMLElement, origin: DOMRect) {
  const box = source.getBoundingClientRect();
  target.style.position = "absolute";
  target.style.boxSizing = "border-box";
  target.style.margin = "0";
  target.style.zIndex = "1";
  target.style.left = `${box.left - origin.left}px`;
  target.style.top = `${box.top - origin.top}px`;
  target.style.width = `${box.width}px`;
  target.style.height = `${box.height}px`;
}
function freezeGraphs(live: ParentNode, copy: ParentNode) {
  const liveNets = [...live.querySelectorAll<HTMLElement>(".account-net")];
  const copyNets = [...copy.querySelectorAll<HTMLElement>(".account-net")];
  liveNets.forEach((net, index) => {
    const clone = copyNets[index];
    const origin = net.getBoundingClientRect();
    if (!clone || origin.width < 2 || origin.height < 2) return;
    clone.style.display = "block";
    clone.style.position = "relative";
    clone.style.overflow = "hidden";
    clone.style.width = `${origin.width}px`;
    clone.style.height = `${origin.height}px`;
    clone.style.minWidth = "0";
    clone.querySelectorAll<HTMLElement>(".account-net-col").forEach(column => {
      column.style.position = "static";
      column.style.display = "block";
      column.style.height = "0";
      column.style.minWidth = "0";
      column.style.padding = "0";
      column.style.zIndex = "auto";
    });
    const pin = (selector: string) => {
      const sources = [...net.querySelectorAll<HTMLElement>(selector)];
      const targets = [...clone.querySelectorAll<HTMLElement>(selector)];
      sources.forEach((source, item) => { const target = targets[item]; if (target) pinBox(source, target, origin); });
    };
    pin(".account-node");
    pin(".account-net-col > h3");
    const svg = clone.querySelector("svg");
    if (svg instanceof SVGElement) {
      svg.setAttribute("width", String(origin.width));
      svg.setAttribute("height", String(origin.height));
      svg.style.position = "absolute";
      svg.style.left = "0";
      svg.style.top = "0";
      svg.style.width = `${origin.width}px`;
      svg.style.height = `${origin.height}px`;
    }
  });
}

function prepareClone(live: HTMLElement) {
  const copy = live.cloneNode(true) as HTMLElement;
  copy.classList.remove("report-parked");
  copy.removeAttribute("id");
  copy.querySelectorAll(".account-node-pop, button, #report-dapper-data").forEach(node => node.remove());
  [copy, ...copy.querySelectorAll("[data-cursor-ref]")].forEach(node => node.removeAttribute("data-cursor-ref"));
  freezeGraphs(live, copy);
  return copy;
}

/** The result graph is drawn from the live layout. Wait until that drawing, or the text of a result with no graph, is present. */
async function whenReady(pane: HTMLElement) {
  const start = performance.now();
  let framesAfterNodes = 0;
  while (performance.now() - start < 2500) {
    const loading = (pane.textContent || "").includes("Loading saved result");
    const edges = pane.querySelector(".account-net svg path, .account-net svg line");
    const nodes = pane.querySelector(".account-node");
    const prose = pane.querySelector(".feasibility-report, .result-record, .error-text, .notice");
    if (!loading && (edges || (nodes && framesAfterNodes > 8) || (prose && !pane.querySelector(".account-net")))) return;
    if (!loading && nodes) framesAfterNodes += 1;
    await new Promise(resolve => requestAnimationFrame(resolve));
  }
}

function dapperAccordion() {
  const details = document.createElement("details");
  details.className = "report-dapper";
  const summary = document.createElement("summary");
  summary.textContent = "DAPPER object";
  details.append(summary);
  const source = document.getElementById("report-dapper-data");
  const ready = source?.dataset.ready === "true" && !!(source.textContent || "").trim();
  if (!ready) {
    const note = document.createElement("p");
    note.textContent = "DAPPER object not ready yet. Download report again later";
    details.append(note);
    return details;
  }
  const pre = document.createElement("pre");
  pre.textContent = source?.textContent || "";
  details.append(pre);
  return details;
}

let saving = false;

export async function downloadInvestigationReport(title: string) {
  if (saving) return;
  const fields = [...document.querySelectorAll<HTMLElement>(".investigation-heading .investigation-field")];
  const assessment = document.getElementById("report-assessment");
  const result = document.getElementById("report-result");
  const body = document.getElementById("report-body");
  const panes = [assessment, result, body].filter((pane): pane is HTMLElement => pane instanceof HTMLElement);
  if (!fields.length && !panes.length) return;
  saving = true;
  try {
    await Promise.all(panes.map(whenReady));
    const main = document.createElement("div");
    if (fields.length) {
      const section = document.createElement("section");
      section.className = "report-fields";
      for (const field of fields) section.append(prepareClone(field));
      main.append(section);
    }
    main.append(dapperAccordion());
    for (const pane of panes) {
      const section = document.createElement("section");
      section.className = "report-block";
      section.append(prepareClone(pane));
      main.append(section);
    }
    await inlineImages(main);
    const html = `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${escapeHtml(title || "Investigation")}</title><style>${reportStyle}</style></head><body><main class="report">${main.innerHTML}</main></body></html>`;
    const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = reportFilename(title || "investigation");
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } finally { saving = false; }
}
