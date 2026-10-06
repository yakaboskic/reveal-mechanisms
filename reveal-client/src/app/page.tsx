"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { version as clientVersion } from "../../package.json";
import { api, ApiError, backend, errorMessage, request } from "../lib/api";
import { followJob, followWorkspace } from "../lib/events";
import { createMutationKeys } from "../lib/mutations";
import { emptyComposer, terminal, withFactors, type AnalysisInput, type Composer, type Draft, type Factor, type Gap, type Job, type JobEvent, type Me, type Schema } from "../lib/types";

type PendingSubmission = { body: AnalysisInput; key: string };
const readable = (value: string) => value.replaceAll("_", " ");
const date = (value: string) => new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const gapTitle = (gap: Gap) => gap.object.name || gap.object.text || "Knowledge gap";
const gapBody = (gap: Gap) => gap.object.gap_description || gap.object.text || gap.source.source_id;
function accountLevel(count: number, max: number) {
  if (max <= 0 || count <= 0) return 0;
  const ratio = count / max;
  return ratio >= 0.66 ? 2 : ratio >= 0.33 ? 1 : 0;
}
function GapOption({ gap, selected, disabled, maxAccounts, onSelect, onInspect }: { gap: Gap; selected: boolean; disabled: boolean; maxAccounts: number; onSelect: () => void; onInspect: () => void }) {
  const bodyRef = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const body = gapBody(gap);
  const accounts = gap.scientific_accounts?.count ?? 0;
  const upvotes = gap.votes?.upvotes ?? 0;
  const downvotes = gap.votes?.downvotes ?? 0;
  const voteTone = (gap.votes?.score ?? upvotes - downvotes) > 0 ? "up" : (gap.votes?.score ?? upvotes - downvotes) < 0 ? "down" : "even";
  useEffect(() => {
    const node = bodyRef.current;
    if (!node || open) return;
    const measure = () => setOverflows(node.scrollHeight > node.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [body, open]);
  return <div className={selected ? "gap-option selected" : "gap-option"} role="button" tabIndex={disabled ? -1 : 0} aria-pressed={selected} aria-disabled={disabled} onClick={() => { if (!disabled) onSelect(); }} onKeyDown={event => { if (event.target !== event.currentTarget || disabled) return; if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(); } }}>
    <strong>{gapTitle(gap)}</strong>
    <span ref={bodyRef} className={open ? "expanded" : undefined}>{body}</span>
    <div className="gap-meta">
      {(overflows || open) && <button type="button" className="read-more" aria-expanded={open} onClick={event => { event.stopPropagation(); setOpen(value => !value); }}>{open ? "Show less" : "Read more"}</button>}
      <div className="gap-bubbles" onClick={event => event.stopPropagation()}>
        <span className={"gap-bubble accounts level-" + accountLevel(accounts, maxAccounts)} aria-label={accounts + " connected accounts"}>{accounts}</span>
        <span className={"gap-bubble votes tone-" + voteTone} aria-label={upvotes + " upvotes, " + downvotes + " downvotes"}><span className="vote-up">{upvotes}</span><span className="vote-bar" aria-hidden="true">|</span><span className="vote-down">{downvotes}</span></span>
        <button type="button" className="gap-bubble inspect" onClick={onInspect}>Inspect gap</button>
      </div>
    </div>
  </div>;
}
const factorTitle = (factor: Factor) => factor.cfde_anchor.label || factor.object.name || factor.source_id;
function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function evidenceHref(reference: string): string | null {
  if (/^PMID:\d+$/i.test(reference)) return `https://pubmed.ncbi.nlm.nih.gov/${reference.split(":")[1]}/`;
  if (/^PMC(?:ID)?:PMC\d+$/i.test(reference)) return `https://pmc.ncbi.nlm.nih.gov/articles/${reference.split(":")[1]}/`;
  if (/^DOI:10\.\d{4,9}\/.+/i.test(reference)) return `https://doi.org/${reference.slice(4)}`;
  try {
    const url = new URL(reference);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch { return null; }
}
function textField(item: Record<string, unknown>, key: string) {
  const value = item[key];
  return typeof value === "string" && value.trim() ? value.trim() : "";
}
function gapSnippets(gap: Gap) {
  const raw = isRecord(gap.source_detail?.raw) ? gap.source_detail.raw : {};
  const evidence = Array.isArray(raw.evidence) ? raw.evidence : [];
  return evidence.flatMap(item => {
    if (!isRecord(item) || typeof item.snippet !== "string" || !item.snippet.trim()) return [];
    const reference = textField(item, "reference");
    return [{ text: item.snippet.trim(), explanation: textField(item, "explanation"), title: textField(item, "reference_title"), source: textField(item, "evidence_source"), href: reference ? evidenceHref(reference) : null, support: textField(item, "supports") }];
  });
}
function GapFacts({ gap }: { gap: Gap }) {
  const snippets = gapSnippets(gap);
  return <dl className="inspect-fields">
    {gap.object.gap_description && <div><dt>Rationale</dt><dd>{gap.object.gap_description}</dd></div>}
    {!!snippets.length && <div><dt>Snippets</dt><dd><div className="snippet-list">{snippets.map((item, index) => <article className="snippet-card" key={index}><p className="snippet-text">“{item.text}”</p>{item.explanation && <p className="snippet-note">{item.explanation}</p>}{(item.title || item.source) && <div className="snippet-cite">{item.title && <p className="snippet-title">{item.href ? <a href={item.href} target="_blank" rel="noreferrer">{item.title}</a> : item.title}</p>}{item.source && <p className="snippet-source">{item.source}</p>}</div>}{item.support && <p><span>Supports</span>{item.support}</p>}</article>)}</div></dd></div>}
  </dl>;
}
function diseaseBubble(gap: Gap) {
  const disease = gap.source.disease_label || "";
  const scope = gap.object.scope && gap.object.scope !== disease ? gap.object.scope : "";
  const diseaseScope = [disease, scope].filter(Boolean).join(" / ");
  if (!diseaseScope) return null;
  const entity = (gap.object.about_entities || []).find(value => /^https?:\/\//i.test(value));
  return entity ? <a className="disease-bubble" href={entity} target="_blank" rel="noreferrer">{diseaseScope}</a> : <span className="disease-bubble">{diseaseScope}</span>;
}
function InspectCard({ title, open, closeLabel, badge, onOpen, onClose, children }: { title: string; open: boolean; closeLabel: string; badge?: ReactNode; onOpen: () => void; onClose: () => void; children: ReactNode }) {
  return <section className={open ? "inspect-card open" : "inspect-card"}><div className="inspect-card-head"><button type="button" className="inspect-toggle" aria-expanded={open} onClick={onOpen}><span>{title}</span></button><button type="button" className="inspect-close" aria-label={closeLabel} onClick={onClose}>×</button>{open && badge && <div className="inspect-badge-row">{badge}</div>}</div>{open && <div className="inspect-body">{children}</div>}</section>;
}
function InspectColumn({ gap, factor, factorPending, focus, gapNote, factorNote, onFocus, onCloseGap, onCloseFactor }: {
  gap: Gap | null; factor: Factor | null; factorPending: boolean; focus: "gap" | "factor"; gapNote: string; factorNote: string; onFocus: (focus: "gap" | "factor") => void; onCloseGap: () => void; onCloseFactor: () => void;
}) {
  return <div className="inspect-frame">
    {gap && <InspectCard title={gap.object.text || gapTitle(gap)} open={focus === "gap"} closeLabel="Close gap inspection" badge={diseaseBubble(gap)} onOpen={() => onFocus("gap")} onClose={onCloseGap}><GapFacts gap={gap} />{gapNote && <p className="settings-note">{gapNote}</p>}</InspectCard>}
    {(factor || factorPending) && <InspectCard title={factor ? factorTitle(factor) : "Loading factor…"} open={focus === "factor"} closeLabel="Close factor inspection" onOpen={() => onFocus("factor")} onClose={onCloseFactor}>{factor ? <>{factor.cfde_anchor.subtitle && <p>{factor.cfde_anchor.subtitle}</p>}{factor.object.description && factor.object.description !== factor.cfde_anchor.subtitle && <p>{factor.object.description}</p>}{factor.kpn_trait?.name && <dl className="settings-facts"><dt>Trait</dt><dd>{factor.kpn_trait.name}</dd></dl>}</> : <p className="settings-note">Loading the factor…</p>}{factorNote && <p className="settings-note">{factorNote}</p>}</InspectCard>}
  </div>;
}
function setLocation(kind: "draft" | "job", id: string | null) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set(kind, id); else url.searchParams.delete(kind);
  window.history.replaceState(null, "", url);
}
const DRAFT_PAGE_SIZE = 10;
const API_VERSION = "0.2.0-draft";
const SETTINGS_KEY = "reveal-client-settings";
const LAST_DRAFT_KEY = "reveal-client-last-draft";
type SettingsTab = "settings" | "apis" | "information";
function readOpenLastDraft() {
  try { return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null")?.openLastDraft === true; }
  catch { return false; }
}
const productApis: { method: string; path: string; detail: string }[] = [
  { method: "GET", path: "/api/session", detail: "Check whether this browser already has a workspace session." },
  { method: "POST", path: "/api/session", detail: "Connect to the workspace." },
  { method: "DELETE", path: "/api/session", detail: "Disconnect the workspace. Saved drafts and running jobs stay on the server." },
  { method: "GET", path: "/v1/drafts", detail: "List saved drafts." },
  { method: "GET", path: "/v1/drafts/{id}", detail: "Open one saved draft." },
  { method: "POST", path: "/v1/drafts", detail: "Save a new draft." },
  { method: "PATCH", path: "/v1/drafts/{id}", detail: "Save updates to the open draft." },
  { method: "DELETE", path: "/v1/drafts/{id}", detail: "Delete a draft. A submitted investigation from that draft stays available." },
  { method: "GET", path: "/v1/knowledge-gaps", detail: "Browse knowledge gaps, ranked by how many investigations use them." },
  { method: "GET", path: "/v1/knowledge-gaps/search", detail: "Search knowledge gaps by a disease or research question." },
  { method: "GET", path: "/v1/knowledge-gaps/{id}", detail: "Load the knowledge gap selected for an investigation." },
  { method: "POST", path: "/v1/mechanisms/suggest", detail: "Suggest mechanism anchors for the selected gap." },
  { method: "GET", path: "/v1/mechanisms/{id}", detail: "Load a mechanism factor after it is selected." },
  { method: "GET", path: "/v1/jobs", detail: "List workspace jobs." },
  { method: "GET", path: "/v1/jobs/{id}", detail: "Open one job and its saved result." },
  { method: "POST", path: "/v1/jobs", detail: "Start an analysis from a saved draft." },
  { method: "POST", path: "/v1/jobs/{id}/cancel", detail: "Stop a running job." },
  { method: "POST", path: "/v1/jobs/{id}/retry-review", detail: "Retry a saved review that can be run again." },
  { method: "GET", path: "/v1/jobs/{id}/events", detail: "Follow a job’s live activity." },
  { method: "GET", path: "/v1/jobs/{id}/evidence-package", detail: "Open the frozen evidence package for a finished job." },
  { method: "GET", path: "/v1/artifacts/{sha256}", detail: "Open a captured artifact from a job event." },
  { method: "GET", path: "/v1/research-requests", detail: "Match saved drafts to the investigations they started." },
  { method: "GET", path: "/v1/me/workspace/events", detail: "Follow workspace changes while the session is connected." },
];
function SettingsPanel({ tab, openLastDraft, onTab, onOpenLastDraft, onClose }: {
  tab: SettingsTab; openLastDraft: boolean; onTab: (tab: SettingsTab) => void; onOpenLastDraft: (value: boolean) => void; onClose: () => void;
}) {
  return <div className="warning-stage" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="settings-panel" role="dialog" aria-modal="true" aria-labelledby="settings-heading">
      <button type="button" className="panel-back" aria-label="Back" onClick={onClose}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22.9 12H6M11 6l-6 6 6 6" /></svg></button>
      <h2 id="settings-heading" className="sr-only">Settings</h2>
      <div className="settings-tabs" role="tablist" aria-label="Settings sections">
        {([["settings", "Settings"], ["apis", "APIs"], ["information", "Information"]] as const).map(([id, label]) => <button type="button" key={id} role="tab" id={"settings-tab-" + id} aria-selected={tab === id} aria-controls={"settings-panel-" + id} onClick={() => onTab(id)}>{label}</button>)}
      </div>
      {tab === "settings" && <div className="settings-body" role="tabpanel" id="settings-panel-settings" aria-labelledby="settings-tab-settings">
        <label className="settings-option"><input type="checkbox" checked={openLastDraft} onChange={event => onOpenLastDraft(event.target.checked)} /><span><strong>Open last draft when connecting to the workspace</strong><span>Connecting opens the draft you last worked on. Leave this off to start from the welcome panel.</span></span></label>
      </div>}
      {tab === "apis" && <div className="settings-body" role="tabpanel" id="settings-panel-apis" aria-labelledby="settings-tab-apis">
        <ul className="settings-api-list">{productApis.map(item => <li key={item.method + item.path}><code>{item.method} {item.path}</code><p>{item.detail}</p></li>)}</ul>
      </div>}
      {tab === "information" && <div className="settings-body" role="tabpanel" id="settings-panel-information" aria-labelledby="settings-tab-information">
        <dl className="settings-facts"><dt>Product</dt><dd>REVEAL client</dd><dt>Client version</dt><dd>{clientVersion}</dd><dt>API</dt><dd>REVEAL Mechanisms API</dd><dt>API version</dt><dd>{API_VERSION}</dd><dt>Description</dt><dd>Scientific questions, evidence, and live research.</dd></dl>
        <p className="settings-note">The client reaches the API through the workspace gateway. Requests are signed on the server.</p>
      </div>}
    </section>
  </div>;
}
const evidenceName = (id: "biomarkerkg" | "prokn") => id === "biomarkerkg" ? "BiomarkerKG" : "ProKN";
function SavedDrafts({ drafts, jobs, requests, gapLabels, factorLabels, ready, requestsReady, page, deletingId, onPage, onOpen, onDelete, onClose }: {
  drafts: Draft[]; jobs: Job[]; requests: Schema<"ResearchRequest">[]; gapLabels: Record<string, string>; factorLabels: Record<string, string>;
  ready: boolean; requestsReady: boolean; page: number; deletingId: string; onPage: (page: number) => void; onOpen: (id: string) => void; onDelete: (draft: Draft) => void; onClose: () => void;
}) {
  const pages = Math.max(1, Math.ceil(drafts.length / DRAFT_PAGE_SIZE));
  const rows = drafts.slice(page * DRAFT_PAGE_SIZE, page * DRAFT_PAGE_SIZE + DRAFT_PAGE_SIZE);
  return <div className="warning-stage" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="draft-picker" role="dialog" aria-modal="true" aria-labelledby="draft-picker-heading">
      <button type="button" className="panel-back" aria-label="Back" onClick={onClose}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22.9 12H6M11 6l-6 6 6 6" /></svg></button>
      <div className="draft-picker-heading"><h2 id="draft-picker-heading">Saved drafts</h2></div>
      <div className="draft-table-wrap"><table className="draft-table">
        <thead><tr><th scope="col">Draft</th><th scope="col">Knowledge gap</th><th scope="col">Factors</th><th scope="col">Evidence</th><th scope="col">Investigation</th><th scope="col">Select</th><th scope="col">Delete</th></tr></thead>
        <tbody>{rows.length === 0 ? <tr><td colSpan={7}>No saved drafts.</td></tr> : rows.map(draft => {
          const gapId = draft.composer.source_gap?.id;
          const factorIds = draft.composer.eaggl_anchors.map(anchor => anchor.reference.source_id);
          const linked = requests.filter(request => request.source_draft_id === draft.id).sort((a, b) => b.submitted_at.localeCompare(a.submitted_at));
          const investigations = linked.flatMap(request => {
            const matches = jobs.filter(job => job.research_request_id === request.id);
            return matches.length ? matches.map(job => `${job.kind === "analysis" ? "Analysis" : "Research paragraph"} — ${readable(job.status)} — ${date(job.created_at)}`) : [`Analysis — ${date(request.submitted_at)}`];
          });
          return <tr key={draft.id}>
            <td>{draft.name || "Untitled draft"} ({date(draft.updated_at)})</td>
            <td>{gapId ? gapLabels[gapId] || (ready ? "—" : "Loading…") : "—"}</td>
            <td>{factorIds.length ? factorIds.map(id => factorLabels[id] || (ready ? "—" : "Loading…")).join(", ") : "—"}</td>
            <td>{draft.composer.selected_kgs.map(evidenceName).join(", ") || "—"}</td>
            <td>{!requestsReady ? "Loading…" : investigations.length ? <span className="draft-investigations">{investigations.map((line, index) => <span key={index}>{line}</span>)}</span> : "—"}</td>
            <td><button type="button" onClick={() => onOpen(draft.id)}>Open</button></td>
            <td><button type="button" className="draft-delete" aria-label={`Delete ${draft.name || "Untitled draft"}`} disabled={!!deletingId} onClick={() => onDelete(draft)}>{deletingId === draft.id ? "Deleting…" : "Delete"}</button></td>
          </tr>;
        })}</tbody>
      </table></div>
      {drafts.length > DRAFT_PAGE_SIZE && <nav className="draft-pages" aria-label="Saved draft pages"><button type="button" className="secondary" disabled={page === 0} onClick={() => onPage(page - 1)}>Previous</button>{Array.from({ length: pages }, (_, index) => <button type="button" className={index === page ? undefined : "secondary"} aria-current={index === page ? "page" : undefined} key={index} onClick={() => onPage(index)}>{index + 1}</button>)}<button type="button" className="secondary" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>Next</button></nav>}
    </section>
  </div>;
}
function storedIntent(user: string): PendingSubmission | null {
  try {
    const value = JSON.parse(sessionStorage.getItem("reveal-submit:" + user) || "null");
    return value?.body?.kind === "analysis" && typeof value.body.draft_id === "string" && Number.isInteger(value.body.draft_version) && typeof value.key === "string" ? value : null;
  } catch { return null; }
}

export default function Home() {
  const [principal, setPrincipal] = useState<Me | null>(null);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [moreRecords, setMoreRecords] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [name, setName] = useState("");
  const [composer, setComposer] = useState<Composer>(emptyComposer);
  const [gap, setGap] = useState<Gap | null>(null);
  const [query, setQuery] = useState("");
  const [gaps, setGaps] = useState<Gap[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [suggestion, setSuggestion] = useState<Schema<"Suggestions"> | null>(null);
  const [factors, setFactors] = useState<Record<string, Factor>>({});
  const [factorMisses, setFactorMisses] = useState<Record<string, true>>({});
  const [job, setJob] = useState<Job | null>(null);
  const [activity, setActivity] = useState<JobEvent[]>([]);
  const [streamState, setStreamState] = useState("");
  const [, setWorkspaceState] = useState("");
  const [streamAttempt, setStreamAttempt] = useState(0);
  const [pending, setPending] = useState<PendingSubmission | null>(null);
  const [welcomeOpen, setWelcomeOpen] = useState(false);
  const [panelView, setPanelView] = useState<"menu" | "draft" | "copy">("menu");
  const [step, setStep] = useState<"gap" | "anchors" | "investigate" | null>("gap");
  const [activityOpen, setActivityOpen] = useState(false);
  const [draftPicker, setDraftPicker] = useState(false);
  const [deletingId, setDeletingId] = useState("");
  const [pendingDelete, setPendingDelete] = useState<Draft | null>(null);
  const [inspectGap, setInspectGap] = useState<Gap | null>(null);
  const [inspectGapNote, setInspectGapNote] = useState("");
  const [inspectFactor, setInspectFactor] = useState<Factor | null>(null);
  const [inspectFactorId, setInspectFactorId] = useState<string | null>(null);
  const [inspectFactorNote, setInspectFactorNote] = useState("");
  const [inspectFocus, setInspectFocus] = useState<"gap" | "factor">("gap");
  const factorInspectId = useRef("");
  const [draftPage, setDraftPage] = useState(0);
  const [gapLabels, setGapLabels] = useState<Record<string, string>>({});
  const [factorLabels, setFactorLabels] = useState<Record<string, string>>({});
  const [draftRequests, setDraftRequests] = useState<Schema<"ResearchRequest">[]>([]);
  const [catalogReady, setCatalogReady] = useState(false);
  const [requestsReady, setRequestsReady] = useState(false);
  const [sessionOpen, setSessionOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("settings");
  const [openLastDraft, setOpenLastDraft] = useState(false);
  const [unsavedPrompt, setUnsavedPrompt] = useState(false);
  const [draftPrompt, setDraftPrompt] = useState<null | "save" | "start">(null);
  const [focusNonce, setFocusNonce] = useState(0);
  const editorGeneration = useRef(0), jobGeneration = useRef(0);
  const collections = useRef<{ drafts: boolean; jobs: boolean; flight: Promise<void> | null }>({ drafts: false, jobs: false, flight: null });
  const identity = useRef<string | null>(null);
  const searchAbort = useRef<AbortController | null>(null), suggestionAbort = useRef<AbortController | null>(null);
  const mutationKeys = useRef(createMutationKeys());
  const currentJob = useRef<Job | null>(null);
  const startFocus = useRef<string | null>(null);
  const sessionMenu = useRef<HTMLDivElement>(null);
  const helpMenu = useRef<HTMLDivElement>(null);
  const keptName = useRef("");
  const dirty = !draft || JSON.stringify(draft.composer) !== JSON.stringify(composer) || (draft.name || "") !== name;
  const mutable = Boolean(principal) && !busy;
  const gapChosen = Boolean(composer.source_gap);
  const anchorChosen = composer.eaggl_anchors.length > 0;
  const suggestedIds = suggestion?.automatic_anchors.map(anchor => anchor.factor.source_id) ?? [];
  const anchorIds = suggestedIds.length ? Array.from(new Set([...composer.eaggl_anchors.map(anchor => anchor.reference.source_id), ...suggestedIds])) : composer.eaggl_anchors.map(anchor => anchor.reference.source_id);
  const missingFactorKey = composer.eaggl_anchors.map(anchor => anchor.reference.source_id).filter(id => !factors[id] && !factorMisses[id]).join("\n");

  const refresh = useCallback((kinds: string[] = ["drafts", "jobs"]): Promise<void> => {
    if (!identity.current) return Promise.resolve();
    const state = collections.current;
    state.drafts ||= kinds.includes("drafts"); state.jobs ||= kinds.includes("jobs");
    if (state.flight) return state.flight;
    state.flight = (async () => {
      try {
        // Mutations during an in-flight read schedule one further read, never overlap it.
        while (identity.current && (state.drafts || state.jobs)) {
          const owner = identity.current, draftsNeeded = state.drafts, jobsNeeded = state.jobs;
          state.drafts = false; state.jobs = false;
          const [saved, recent] = await Promise.all([draftsNeeded ? api.drafts() : null, jobsNeeded ? api.jobs() : null]);
          if (identity.current !== owner) continue;
          if (saved) setDrafts(saved.items);
          if (recent) setJobs(recent.items);
          if (saved?.page.has_more || recent?.page.has_more) setMoreRecords(true);
        }
      } finally { state.flight = null; }
    })();
    return state.flight;
  }, []);
  const openJob = useCallback(async (id: string) => {
    const generation = ++jobGeneration.current, owner = identity.current;
    try {
      const value = await api.job(id);
      if (generation !== jobGeneration.current || owner !== identity.current) return;
      currentJob.current = value; setJob(value); setActivity([]); setLocation("job", id); setError("");
    } catch (error) { if (generation === jobGeneration.current && owner === identity.current) setError(errorMessage(error)); }
  }, []);
  const openDraft = useCallback(async (id: string) => {
    const generation = ++editorGeneration.current, owner = identity.current;
    suggestionAbort.current?.abort(); setSuggesting(false);
    try {
      const value = await api.draft(id);
      if (generation !== editorGeneration.current || owner !== identity.current) return;
      setDraft(value); setComposer(value.composer); setName(value.name || ""); setSuggestion(null); setGap(null);
      setLocation("draft", id); setNotice("Saved draft loaded."); setError(""); setWelcomeOpen(false); setStep(value.composer.source_gap ? "anchors" : "gap");
      if (value.composer.source_gap) {
        const selected = await api.gap(value.composer.source_gap.id);
        if (generation === editorGeneration.current) setGap(selected);
      }
    } catch (error) { if (generation === editorGeneration.current && owner === identity.current) setError(errorMessage(error)); }
  }, []);

  useEffect(() => {
    let active = true;
    api.session().then(value => { if (active) setPrincipal(value.principal); }).catch(error => { if (active) setError(errorMessage(error)); }).finally(() => { if (active) setChecking(false); });
    return () => { active = false; };
  }, []);
  useEffect(() => { setOpenLastDraft(readOpenLastDraft()); }, []);
  useEffect(() => { if (draft?.id) localStorage.setItem(LAST_DRAFT_KEY, draft.id); }, [draft?.id]);
  useEffect(() => {
    if (!settingsOpen) return;
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") setSettingsOpen(false); }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [settingsOpen]);
  useEffect(() => {
    identity.current = principal?.user_id || null;
    if (!principal) return;
    setPending(storedIntent(principal.user_id));
    void refresh().catch(error => setError(errorMessage(error)));
    const params = new URLSearchParams(window.location.search);
    if (params.get("draft")) void openDraft(params.get("draft")!);
    if (params.get("job")) { setActivityOpen(true); void openJob(params.get("job")!); }
    return () => { identity.current = null; ++editorGeneration.current; ++jobGeneration.current; };
  }, [principal?.user_id, refresh, openDraft, openJob]);
  useEffect(() => {
    if (!principal) return;
    const controller = new AbortController(); let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    const invalidated = new Set<string>();
    const scheduleRefresh = (kinds: string[]) => {
      for (const kind of kinds) invalidated.add(kind);
      if (refreshTimer) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => {
        const kinds = [...invalidated]; invalidated.clear();
        void refresh(kinds).catch(error => { if (!controller.signal.aborted) setError(errorMessage(error)); });
      }, 120);
    };
    void followWorkspace({ signal: controller.signal, onState: setWorkspaceState, onChange: change => {
      const kinds = change ? change.collections.filter(value => ["drafts", "jobs"].includes(value)) : ["drafts", "jobs"];
      if (kinds.length) scheduleRefresh(kinds);
    } }).catch(error => { if (!controller.signal.aborted) setWorkspaceState(errorMessage(error)); });
    return () => { controller.abort(); if (refreshTimer) clearTimeout(refreshTimer); };
  }, [principal?.user_id, refresh]);
  useEffect(() => {
    if (!principal || !job) return;
    const id = job.id, controller = new AbortController();
    setStreamState("Connecting to live activity…");
    async function readLatest() {
      const value = await api.job(id);
      if (!controller.signal.aborted && currentJob.current?.id === id) { currentJob.current = value; setJob(value); }
      return value;
    }
    void followJob(id, { signal: controller.signal, onState: setStreamState,
      onResync: async () => { const value = await readLatest(); setNotice("Older activity is unavailable. The current saved job was reloaded."); return { cursor: value.last_event_id, terminal: terminal(value.status) }; },
      onEvent: event => {
        if (controller.signal.aborted) return;
        setActivity(items => items.some(item => item.id === event.id) ? items : [...items, event].slice(-1000));
        setJob(value => value?.id === id ? { ...value, status: event.status, stage: event.stage, result: event.result || value.result,
          updated_at: event.occurred_at, last_event_id: event.id } : value);
        if (terminal(event.status)) { setStreamState("Activity complete"); void readLatest().then(() => refresh(["jobs"])).catch(error => setError(errorMessage(error))); }
      },
    }).then(() => { if (!controller.signal.aborted) setStreamState("Activity complete"); })
      .catch(error => { if (!controller.signal.aborted) setStreamState(errorMessage(error)); });
    return () => controller.abort();
  // The stream owns status changes; only selecting a different job reconnects it.
  }, [principal?.user_id, job?.id, streamAttempt, refresh]);
  useEffect(() => () => { searchAbort.current?.abort(); suggestionAbort.current?.abort(); }, []);
  useEffect(() => {
    if (welcomeOpen || !startFocus.current) return;
    const id = startFocus.current;
    if (id === "job-select" && !activityOpen) return;
    if (id === "gap-search" && step !== "gap") return;
    startFocus.current = null;
    const element = document.getElementById(id);
    element?.scrollIntoView({ block: "center" });
    element?.focus();
  }, [welcomeOpen, activityOpen, step, focusNonce]);
  useEffect(() => {
    if (step === "anchors" && !gapChosen) setStep("gap");
    else if (step === "investigate" && !(gapChosen && anchorChosen)) setStep(gapChosen ? "anchors" : "gap");
  }, [step, gapChosen, anchorChosen]);
  useEffect(() => {
    if (!sessionOpen && !helpOpen) return;
    function close(event: MouseEvent) {
      const target = event.target as Node;
      if (!sessionMenu.current?.contains(target)) setSessionOpen(false);
      if (!helpMenu.current?.contains(target)) setHelpOpen(false);
    }
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") { setSessionOpen(false); setHelpOpen(false); } }
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", onKey); };
  }, [sessionOpen, helpOpen]);
  useEffect(() => {
    if (!unsavedPrompt && !draftPrompt) return;
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") { setUnsavedPrompt(false); setDraftPrompt(null); } }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [unsavedPrompt, draftPrompt]);
  useEffect(() => {
    if (!draftPicker) return;
    function onKey(event: KeyboardEvent) { if (event.key === "Escape") { if (pendingDelete) setPendingDelete(null); else setDraftPicker(false); } }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [draftPicker, pendingDelete]);
  useEffect(() => {
    if (!draftPicker) return;
    const last = Math.max(0, Math.ceil(drafts.length / DRAFT_PAGE_SIZE) - 1);
    if (draftPage > last) setDraftPage(last);
  }, [drafts.length, draftPage, draftPicker]);
  useEffect(() => {
    if (!draftPicker) return;
    const controller = new AbortController();
    setCatalogReady(false); setRequestsReady(false);
    const gapIds = [...new Set(drafts.flatMap(item => item.composer.source_gap ? [item.composer.source_gap.id] : []))];
    const factorIds = [...new Set(drafts.flatMap(item => item.composer.eaggl_anchors.map(anchor => anchor.reference.source_id)))];
    async function labelsFor(ids: string[], read: (id: string) => Promise<string>, apply: (labels: Record<string, string>) => void) {
      const labels: Record<string, string> = {};
      for (let index = 0; index < ids.length; index += 8) {
        if (controller.signal.aborted) return;
        const batch = await Promise.all(ids.slice(index, index + 8).map(async id => {
          try { return [id, await read(id)] as const; }
          catch (error) { if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) throw error; return [id, "Unavailable"] as const; }
        }));
        for (const [id, label] of batch) if (label) labels[id] = label;
        if (!controller.signal.aborted) apply({ ...labels });
      }
    }
    void (async () => {
      try {
        await Promise.all([
          labelsFor(gapIds, async id => { const gap = await api.gap(id, controller.signal); return gap.object.text || gapTitle(gap); }, setGapLabels),
          labelsFor(factorIds, async id => factorTitle(await api.factor(id, controller.signal)), setFactorLabels),
          api.requests(controller.signal).then(page => { if (!controller.signal.aborted) setDraftRequests(page.items); }).catch(error => {
            if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) throw error;
          }).finally(() => { if (!controller.signal.aborted) setRequestsReady(true); }),
        ]);
        if (!controller.signal.aborted) setCatalogReady(true);
      } catch { /* A newer list replaced this lookup. */ }
    })();
    return () => controller.abort();
  }, [draftPicker, drafts]);
  useEffect(() => {
    if (!missingFactorKey) return;
    const ids = missingFactorKey.split("\n");
    const controller = new AbortController();
    void (async () => {
      const loaded: Record<string, Factor> = {};
      const missed: string[] = [];
      try {
        for (let index = 0; index < ids.length; index += 8) {
          if (controller.signal.aborted) return;
          const batch = await Promise.all(ids.slice(index, index + 8).map(async id => {
            try { return [id, await api.factor(id, controller.signal)] as const; }
            catch (error) { if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) throw error; return [id, null] as const; }
          }));
          for (const [id, factor] of batch) { if (factor) loaded[id] = factor; else missed.push(id); }
        }
        if (controller.signal.aborted) return;
        if (Object.keys(loaded).length) setFactors(current => ({ ...current, ...loaded }));
        if (missed.length) setFactorMisses(current => ({ ...current, ...Object.fromEntries(missed.map(id => [id, true])) }));
      } catch { /* A newer selection replaced this lookup. */ }
    })();
    return () => controller.abort();
  }, [missingFactorKey]);

  async function connect() {
    setBusy("connect"); setError("");
    try {
      setLocation("draft", null); setLocation("job", null);
      const value = await api.connect();
      let restore: string | null = null;
      if (readOpenLastDraft()) {
        const id = localStorage.getItem(LAST_DRAFT_KEY);
        if (id) {
          try { await api.draft(id); restore = id; }
          catch (error) { if (error instanceof ApiError && error.status === 404) localStorage.removeItem(LAST_DRAFT_KEY); }
        }
      }
      if (restore) setLocation("draft", restore);
      setPrincipal(value.principal); setNotice(""); setPanelView("menu"); setWelcomeOpen(!restore); setActivityOpen(false); setSettingsOpen(false);
      setDraft(null); setComposer(emptyComposer()); setName(""); setGap(null); setSuggestion(null); setStep("gap"); currentJob.current = null; setJob(null);
    }
    catch (error) { setError(errorMessage(error)); }
    finally { setBusy(""); }
  }
  async function disconnect() {
    setBusy("disconnect");
    try {
      await api.disconnect(); setSessionOpen(false); setDraftPicker(false); setHelpOpen(false); setSettingsOpen(false); setWelcomeOpen(false); setPanelView("menu"); setStep("gap"); setActivityOpen(false); setPrincipal(null); currentJob.current = null; setJob(null); setDraft(null); setJobs([]); setDrafts([]);
      setComposer(emptyComposer()); setName(""); setGap(null); setSuggestion(null); setActivity([]); setPending(null);
      setNotice("Disconnected. Saved work and running jobs remain on the server.");
    } catch (error) { setError(errorMessage(error)); }
    finally { setBusy(""); }
  }
  async function search() {
    searchAbort.current?.abort(); const controller = new AbortController(); searchAbort.current = controller;
    setSearching(true); setError("");
    try { const values = await api.gaps(query, controller.signal); if (!controller.signal.aborted) { setGaps(values); setSearched(true); } }
    catch (error) { if (!controller.signal.aborted) setError(errorMessage(error)); }
    finally { if (!controller.signal.aborted) setSearching(false); }
  }
  async function suggest(next: Composer, replaceSelection: boolean) {
    suggestionAbort.current?.abort(); const controller = new AbortController(); suggestionAbort.current = controller;
    const generation = editorGeneration.current; setSuggesting(true); setError("");
    try {
      const value = await api.suggest(next, controller.signal);
      if (controller.signal.aborted || generation !== editorGeneration.current) return;
      setSuggestion(value); setFactors(values => ({ ...values, ...Object.fromEntries(value.automatic_anchors.map(anchor => [anchor.factor.source_id, anchor.factor])) }));
      if (replaceSelection) setComposer(current => withFactors(current, value.automatic_anchors.slice(0, 1).map(anchor => anchor.factor), value.suggestion_id, true));
    } catch (error) { if (!controller.signal.aborted && generation === editorGeneration.current) setError(errorMessage(error)); }
    finally { if (!controller.signal.aborted) setSuggesting(false); }
  }
  function selectGap(value: Gap) {
    if (composer.source_gap?.source_id === value.source.source_id) return;
    ++editorGeneration.current;
    const next: Composer = { ...composer, source_gap: { id: value.object.id, source_id: value.source.source_id, source_revision: value.source.source_revision }, eaggl_anchors: [], dismissed_source_ids: [] };
    setGap(value); setComposer(next); setSuggestion(null); setNotice(""); setStep("anchors");
    void suggest(next, false);
  }
  function newDraft() {
    if (dirty && (composer.source_gap || name) && !window.confirm("Start a new draft? Unsaved changes will be discarded.")) return false;
    ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
    setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null); setLocation("draft", null); setNotice(""); setError(""); setStep("gap"); setActivityOpen(false);
    return true;
  }
  function beginWithoutDraft() {
    if (dirty && (draft || composer.source_gap || name) && !window.confirm("Leave this draft and choose a knowledge gap? Unsaved changes will be discarded.")) return;
    ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
    setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null);
    setQuery(""); setGaps([]); setSearched(false); setLocation("draft", null); setNotice(""); setError("");
    setStep("gap"); setActivityOpen(false); setWelcomeOpen(false); setPanelView("menu"); setUnsavedPrompt(false); setDraftPrompt(null);
    startFocus.current = "gap-search"; setFocusNonce(value => value + 1);
  }
  function startFrom(action: "draft" | "gap" | "jobs") {
    setSessionOpen(false);
    if (action === "draft") { if (newDraft()) { setPanelView("draft"); setWelcomeOpen(true); } return; }
    if (action === "gap") { beginWithoutDraft(); return; }
    if (panelView === "copy") setName(keptName.current);
    if (action === "jobs") { setActivityOpen(true); startFocus.current = "job-select"; }
    else { setStep("gap"); startFocus.current = "gap-search"; }
    setWelcomeOpen(false); setPanelView("menu");
  }
  function openCopy() {
    setSessionOpen(false);
    if (panelView !== "copy") keptName.current = name;
    setName(""); setPanelView("copy"); setWelcomeOpen(true);
  }
  async function browseTrending() {
    if (dirty && (draft || composer.source_gap || name) && !window.confirm("Leave this draft and browse trending knowledge gaps? Unsaved changes will be discarded.")) return;
    ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
    setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null);
    setQuery(""); setGaps([]); setSearched(false); setLocation("draft", null); setNotice(""); setError("");
    setStep("gap"); setActivityOpen(false); setWelcomeOpen(false); setPanelView("menu"); setUnsavedPrompt(false); setDraftPrompt(null);
    searchAbort.current?.abort(); const controller = new AbortController(); searchAbort.current = controller;
    setSearching(true);
    try { const values = await api.gaps("", controller.signal); if (!controller.signal.aborted) { setGaps(values); setSearched(true); } }
    catch (error) { if (!controller.signal.aborted) setError(errorMessage(error)); }
    finally { if (!controller.signal.aborted) setSearching(false); }
  }
  function showDrafts() {
    setSessionOpen(false); setHelpOpen(false); setDraftPage(0); setDraftPicker(true);
    void refresh(["drafts", "jobs"]);
  }
  function chooseDraft(id: string) {
    if (dirty && (composer.source_gap || name) && !window.confirm("Load the saved draft and discard unsaved changes?")) return;
    setDraftPicker(false); setWelcomeOpen(false); setPanelView("menu");
    void openDraft(id);
  }
  async function removeDraft(value: Draft) {
    setPendingDelete(null); setDeletingId(value.id); setError("");
    try {
      await mutationKeys.current.run(["delete", value.id, value.version], key => api.deleteDraft(value, key));
      if (draft?.id === value.id) {
        ++editorGeneration.current; suggestionAbort.current?.abort(); setSuggesting(false);
        setDraft(null); setName(""); setComposer(emptyComposer()); setGap(null); setSuggestion(null); setLocation("draft", null); setStep("gap"); setActivityOpen(false);
      }
      await refresh(["drafts"]);
    } catch (error) { setError(errorMessage(error)); }
    finally { setDeletingId(""); }
  }
  async function save(asNew = false): Promise<Draft | null> {
    const fromStart = welcomeOpen && panelView === "draft";
    const fromCopy = asNew || (welcomeOpen && panelView === "copy");
    setBusy("save"); setError("");
    try {
      const value = await mutationKeys.current.run(["save", fromCopy ? null : draft?.id, fromCopy ? null : draft?.version, composer, name], key => api.save(fromCopy ? null : draft, composer, name, key));
      setDraft(value); setComposer(value.composer); setName(value.name || ""); setLocation("draft", value.id);
      if (fromStart) { setGaps([]); setSearched(false); setNotice(""); setStep("gap"); setActivityOpen(false); setWelcomeOpen(false); setPanelView("menu"); }
      else if (fromCopy) { setNotice(""); setWelcomeOpen(false); setPanelView("menu"); setStep(value.composer.source_gap ? "anchors" : "gap"); }
      else { setUnsavedPrompt(false); setDraftPrompt(null); }
      await refresh();
      return value;
    } catch (error) { setError(error instanceof ApiError && error.status === 409 && error.code !== "REFERENCE_GENERATION_SUPERSEDED" ? "This draft changed on the server. Open the saved draft again to review it before saving. Your local selections remain visible." : errorMessage(error)); return null; }
    finally { setBusy(""); }
  }
  async function beginAnalysis(saved?: Draft) {
    if (!principal) return;
    const source = saved || draft;
    if (!pending && (!source || (!saved && dirty))) return;
    if (!pending && (!source?.composer.source_gap || !source.composer.eaggl_anchors.length)) return;
    const intent = pending || { body: { kind: "analysis" as const, draft_id: source!.id, draft_version: source!.version, budgets: { max_accounts: 1 } }, key: crypto.randomUUID() };
    setPending(intent); sessionStorage.setItem("reveal-submit:" + principal.user_id, JSON.stringify(intent));
    setBusy("submit"); setError("");
    try {
      const value = await api.submit(intent.body, intent.key);
      sessionStorage.removeItem("reveal-submit:" + principal.user_id); setPending(null);
      currentJob.current = value; setJob(value); setActivity([]); setLocation("job", value.id); setActivityOpen(true); setNotice("Analysis submitted. Live activity will appear alongside your draft."); await refresh();
    } catch (error) {
      // A later refusal cannot disprove an earlier committed request whose response was lost.
      setError(errorMessage(error));
    } finally { setBusy(""); }
  }
  function discardSubmission() {
    if (!principal || !window.confirm("The original request may already have created a job. Check Workspace jobs first. Discard this recovery key and allow a new submission?")) return;
    sessionStorage.removeItem("reveal-submit:" + principal.user_id); setPending(null);
  }
  async function cancel() {
    if (!job) return; const id = job.id; setBusy("cancel"); setError("");
    try { const value = await api.cancel(id); if (currentJob.current?.id === id) { currentJob.current = value; setJob(value); } setNotice("Stop requested. The service will preserve captured work and clean up the research sandbox."); await refresh(); }
    catch (error) { setError(errorMessage(error)); } finally { setBusy(""); }
  }
  async function retryReview() {
    if (!job) return; const id = job.id; setBusy("review"); setError("");
    try {
      const value = await mutationKeys.current.run(["review", job.id, job.last_event_id], key => api.retryReview(job, key)); if (currentJob.current?.id === id) { currentJob.current = value; setJob(value); setActivity([]); setStreamAttempt(value => value + 1); } await refresh();
    } catch (error) { setError(errorMessage(error)); } finally { setBusy(""); }
  }

  async function openInspect(value: Gap) {
    setInspectGap(value); setInspectGapNote(""); setInspectFocus("gap");
    try {
      const fresh = await api.gap(value.object.id);
      setInspectGap(current => current?.object.id === value.object.id ? fresh : current);
    } catch (error) { setInspectGapNote(errorMessage(error)); }
  }
  async function openFactorInspect(sourceId: string) {
    factorInspectId.current = sourceId; setInspectFactorId(sourceId); setInspectFocus("factor"); setInspectFactorNote("");
    setInspectFactor(factors[sourceId] ?? null);
    try {
      const fresh = await api.factor(sourceId);
      if (factorInspectId.current !== sourceId) return;
      setFactors(values => ({ ...values, [fresh.source_id]: fresh })); setInspectFactor(fresh);
    } catch (error) { if (factorInspectId.current === sourceId) setInspectFactorNote(errorMessage(error)); }
  }
  function closeGapInspect() {
    setInspectGap(null); setInspectGapNote("");
    if (inspectFactorId) setInspectFocus("factor");
  }
  function closeFactorInspect() {
    factorInspectId.current = ""; setInspectFactorId(null); setInspectFactor(null); setInspectFactorNote("");
    if (inspectGap) setInspectFocus("gap");
  }
  const maxGapAccounts = Math.max(0, ...gaps.map(item => item.scientific_accounts?.count ?? 0));
  const inWorkspace = Boolean(principal) && !welcomeOpen;
  const errorNotice = error ? <div className="notice error" role="alert"><span>{error}</span><button className="quiet" onClick={() => setError("")} aria-label="Dismiss error">Dismiss</button></div> : null;
  return <>
    <header className="site-header"><div className="brand"><div className="brand-logos"><img src="/brand/cfde-knowledge-center.svg" alt="CFDE Knowledge Center" /><span className="brand-rule" aria-hidden="true" /><img src="/brand/cfde-ecosystem.png" alt="Common Fund Data Ecosystem" /></div><span className="brand-rule" aria-hidden="true" /><div className="brand-copy"><h1><span className="brand-reveal">REVEAL</span><span className="brand-product">client</span></h1><p>Scientific questions, evidence, and live research</p></div></div>
      <div className="connection">
        {principal && <div className="session-menu" ref={sessionMenu}><button className="secondary" aria-expanded={sessionOpen} aria-haspopup="menu" aria-controls="session-menu" onClick={() => { setHelpOpen(false); setSessionOpen(open => !open); }}>Session</button>
          {sessionOpen && <div id="session-menu" className="session-menu-list" role="menu"><button role="menuitem" onClick={() => startFrom("draft")}>Start a new draft</button><button role="menuitem" onClick={openCopy} disabled={!draft}>Save as new draft</button><button role="menuitem" onClick={showDrafts} disabled={!drafts.length}>Open saved draft</button><button role="menuitem" onClick={() => startFrom("gap")}>Choose a knowledge gap</button><button role="menuitem" onClick={() => startFrom("jobs")}>Workspace jobs</button></div>}
        </div>}
        {principal && <div className="session-menu" ref={helpMenu}><button className="secondary" aria-expanded={helpOpen} aria-haspopup="menu" aria-controls="help-menu" onClick={() => { setSessionOpen(false); setHelpOpen(open => !open); }}>Help</button>
          {helpOpen && <div id="help-menu" className="session-menu-list" role="menu"><button role="menuitem" onClick={() => setHelpOpen(false)}>Learn REVEAL client</button><button role="menuitem" onClick={() => setHelpOpen(false)}>Quick start tutorial</button></div>}
        </div>}
        {principal ? <button onClick={disconnect} disabled={!!busy}>{busy === "disconnect" ? "Disconnecting…" : "Disconnect workspace"}</button> : <span className="connection-status">Disconnected</span>}
        <button type="button" className="settings-button" aria-label="Settings" aria-expanded={settingsOpen} onClick={() => { setSessionOpen(false); setHelpOpen(false); setSettingsTab("settings"); setSettingsOpen(true); }}><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.49.49 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.48.48 0 0 0-.48-.41h-3.84a.48.48 0 0 0-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 0 0-.59.22L2.74 8.87a.48.48 0 0 0 .12.61l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32a.49.49 0 0 0-.12-.61l-2.03-1.58zM12 15.6A3.6 3.6 0 1 1 12 8.4a3.6 3.6 0 0 1 0 7.2z" /></svg></button>
      </div></header>
    {inWorkspace && <div className="workspace-bar">{draft ? <div className="draft-tools">{dirty && <button type="button" className="save-updates" onClick={() => save()} disabled={!mutable || suggesting}>{busy === "save" ? "Saving…" : "Save updates"}</button>}{name.trim() && <p className="draft-badge">{name.trim()}</p>}</div> : <div className="draft-tools"><button type="button" className="save-updates" onClick={() => setDraftPrompt("save")} disabled={!mutable || suggesting}>Save draft</button></div>}{errorNotice}</div>}
    <main className={inWorkspace ? "workspace" : undefined}>
      {!inWorkspace && errorNotice}
      {!principal ? <section className="welcome"><div className="welcome-lead"><h2>Choose the gap. Ground the claim.</h2><svg className="welcome-mark" viewBox="0 0 132 18" aria-hidden="true"><line x1="16" y1="9" x2="116" y2="9" stroke="#a7a9ad" strokeWidth="1.7" /><circle cx="9" cy="9" r="6.1" fill="#fff" stroke="#e07b39" strokeWidth="1.8" /><circle cx="123" cy="9" r="7" fill="#e07b39" /></svg><button onClick={connect} disabled={!!busy || checking}>{checking ? "Checking workspace…" : busy === "connect" ? "Connecting…" : "Connect workspace"}</button></div><div className="welcome-cards"><a className="welcome-card" href="#learn-reveal-client" onClick={event => event.preventDefault()}><svg viewBox="0 0 72 56" aria-hidden="true"><path d="M36 12v32M14 16c7 5 15 5 22-2 7 7 15 7 22 2v28c-7 5-15 5-22-2-7 7-15 7-22 2V16z" /></svg><strong>Learn REVEAL client</strong><span>A guide to the workspace, from a knowledge gap to a grounded claim.</span></a><a className="welcome-card" href="#quick-start-demo" onClick={event => event.preventDefault()}><svg viewBox="0 0 72 56" aria-hidden="true"><rect x="14" y="12" width="44" height="32" rx="3" /><path className="card-icon-fill" d="M33 22l12 6-12 6z" /></svg><strong>Watch quick start demo</strong><span>A short walkthrough of connecting and starting an investigation.</span></a></div></section> : welcomeOpen ? (panelView === "draft" || panelView === "copy" ? <div className="welcome-panel-stage"><section className="welcome-panel" role="dialog" aria-modal="true" aria-labelledby="welcome-heading"><button type="button" className="panel-back" aria-label="Back to welcome" onClick={() => { if (panelView === "copy") setName(keptName.current); else { setName(""); setNotice(""); } setPanelView("menu"); }}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M22.9 12H6M11 6l-6 6 6 6" /></svg></button><div className="draft-start"><h2 id="welcome-heading">{panelView === "copy" ? "Save as a new draft" : "Start a draft"}</h2><label htmlFor="draft-name">Draft name</label><input id="draft-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} disabled={!mutable} placeholder="Name this investigation" autoFocus />{notice && <p className="notice" role="status">{notice}</p>}<div className="actions"><button onClick={() => save(panelView === "copy")} disabled={!mutable || !name.trim() || (panelView !== "copy" && !dirty)}>{busy === "save" ? "Saving…" : "Save draft"}</button></div></div></section></div> : <div className="welcome-panel-stage"><section className="welcome-panel" aria-labelledby="welcome-menu-heading"><h2 id="welcome-menu-heading">Welcome to your workspace</h2><div className="welcome-menu"><button type="button" onClick={() => startFrom("draft")}>Start a draft</button><button type="button" onClick={showDrafts} disabled={!drafts.length}>Open a draft</button><button type="button" onClick={() => startFrom("jobs")}>Workspace jobs</button><hr className="welcome-rule" /><button type="button" onClick={() => void browseTrending()} disabled={searching}>Browse trending knowledge gaps</button></div></section></div>) : <>
        <div className={(inspectGap || inspectFactorId) ? "workspace-frames" : "workspace-stage"}>
        <div className="steps">
          <section className={step === "gap" ? "step open" : "step"}>
            <button type="button" className="step-toggle" aria-expanded={step === "gap"} aria-describedby="step-gap-guide" onClick={() => setStep(current => current === "gap" ? null : "gap")}><span className="step-number">1</span>Choose a knowledge gap</button>
            {step !== "gap" && gap ? <p id="step-gap-guide" className="step-chosen">{gap.object.text || gapTitle(gap)}</p> : <p id="step-gap-guide" className="step-guide">Find an existing scientific question that evidence still leaves unexplained. The gap you select becomes the question this investigation will try to ground.</p>}
            {step === "gap" && <div className="step-body"><form className="search-control" autoComplete="off" onSubmit={event => { event.preventDefault(); void search(); }}><label className="sr-only" htmlFor="gap-search">Search knowledge gaps</label><input id="gap-search" name="reveal-gap-search" type="text" inputMode="search" autoComplete="off" autoCorrect="off" spellCheck={false} value={query} onChange={event => setQuery(event.target.value)} placeholder="Search a disease or research question" /><button type="submit" className="secondary" disabled={searching}>{searching ? "Searching…" : "Search"}</button></form>
              {gaps.length > 0 && <div className="gap-results"><p className="gap-guide">{query.trim() ? `${gaps.length} knowledge gap${gaps.length === 1 ? "" : "s"} found.` : `${gaps.length} trending knowledge gaps.`} Click one to select for the next step.</p><div className="gap-list" aria-label="Knowledge gap search results">{gaps.map(value => <GapOption key={value.source.source_id} gap={value} maxAccounts={maxGapAccounts} selected={composer.source_gap?.source_id === value.source.source_id} disabled={!mutable} onSelect={() => selectGap(value)} onInspect={() => void openInspect(value)} />)}</div><ul className="gap-legend"><li><span className="gap-swatch accounts" aria-hidden="true" />Accounts</li><li><span className="gap-swatch up" aria-hidden="true" />Upvotes</li><li><span className="gap-swatch down" aria-hidden="true" />Downvotes</li></ul></div>}
              {searched && !searching && !gaps.length && <p className="empty">No matching gaps. Try a broader disease name.</p>}
</div>}
          </section>
          <section className={step === "anchors" ? "step open" : gapChosen ? "step" : "step inactive"}>
            <button type="button" className="step-toggle" aria-expanded={step === "anchors"} aria-describedby="step-anchors-guide" disabled={!gapChosen} onClick={() => { if (gapChosen) setStep(current => current === "anchors" ? null : "anchors"); }}><span className="step-number">2</span>Select mechanism anchors</button>
            {step !== "anchors" && anchorChosen ? <p id="step-anchors-guide" className="step-chosen">{composer.eaggl_anchors.map(anchor => { const factor = factors[anchor.reference.source_id]; return factor ? factorTitle(factor) : anchor.reference.source_id; }).join(", ")}</p> : <p id="step-anchors-guide" className="step-guide">Choose genetic factors that may help explain the selected gap. At least one factor is required to start an investigation, and none is selected for you.{suggestion?.limitations.length ? ` ${suggestion.limitations.join(" ")}` : ""}</p>}
            {step === "anchors" && <div className="step-body">{!composer.source_gap ? <p className="empty">Select a question to find related genetic mechanisms.</p> : <>
                {!anchorChosen && <p className="gap-guide">At least one factor has to be selected to initiate investigation.</p>}
                <div className="anchor-list">{anchorIds.map(sourceId => {
                  const factor = factors[sourceId], selected = composer.eaggl_anchors.some(value => value.reference.source_id === sourceId);
                  const title = factor ? factorTitle(factor) : factorMisses[sourceId] ? sourceId : "Loading…";
                  return <label className={selected ? "anchor selected" : "anchor"} key={sourceId}><input type="checkbox" checked={selected} disabled={!mutable || (!selected && composer.eaggl_anchors.length >= 10)} onChange={() => setComposer(current => selected ? { ...current, eaggl_anchors: current.eaggl_anchors.filter(value => value.reference.source_id !== sourceId) } : factor && suggestion ? withFactors(current, [factor], suggestion.suggestion_id) : current)} /><span><strong>{title}</strong>{factor?.cfde_anchor.subtitle && <small>{factor.cfde_anchor.subtitle}</small>}</span><button type="button" className="gap-bubble inspect" onClick={event => { event.preventDefault(); event.stopPropagation(); void openFactorInspect(sourceId); }}>Inspect factor</button></label>;
                })}{!suggestedIds.length && (suggesting ? <p role="status" className="loading">Finding relevant mechanisms…</p> : <button type="button" className="quiet suggestion-refresh" disabled={!mutable} onClick={() => void suggest(composer, false)}>Show suggestions</button>)}</div>
                {anchorChosen && <button type="button" className="step-next" onClick={() => setStep("investigate")}>Select evidence and initiate investigation in step 3.</button>}
              </>}
</div>}
          </section>
          <section className={step === "investigate" ? "step open" : gapChosen && anchorChosen ? "step" : "step inactive"}>
            <button type="button" className="step-toggle" aria-expanded={step === "investigate"} aria-describedby="step-investigate-guide" disabled={!(gapChosen && anchorChosen)} onClick={() => { if (gapChosen && anchorChosen) setStep(current => current === "investigate" ? null : "investigate"); }}><span className="step-number">3</span>Investigate</button>
            <p id="step-investigate-guide" className="step-guide">Start an analysis of the gap and the factors you selected. Choose any extra knowledge-graph evidence, then begin the investigation.</p>
            {step === "investigate" && <div className="step-body"><p className="step-local">These options add knowledge-graph evidence the analysis can search along with your gap and factors. BiomarkerKG supplies literature-linked biomarkers and diseases; ProKN supplies genes, proteins, pathways, and diseases.</p><fieldset disabled={!mutable}><legend>Knowledge graph evidence</legend>{(["biomarkerkg", "prokn"] as const).map(value => <label className="check" key={value}><input type="checkbox" checked={composer.selected_kgs.includes(value)} onChange={event => setComposer(current => ({ ...current, selected_kgs: event.target.checked ? [...current.selected_kgs, value] : current.selected_kgs.filter(kg => kg !== value) }))} />{value === "biomarkerkg" ? "BiomarkerKG" : "ProKN"}</label>)}</fieldset>
              <p className="step-follow">The analysis freezes that evidence, searches the graphs you leave checked, and drafts one scientific account. If the account is accepted, a research paragraph follows, and you can follow the run in Research activity.</p>
              {pending && <div className="notice"><span>A submission needs confirmation. Recover it with the original request key before starting another.</span><button className="quiet small" onClick={discardSubmission} disabled={!!busy}>Discard recovery</button></div>}
              <div className="actions"><button onClick={() => { if (!pending && !draft) { setDraftPrompt("start"); return; } if (!pending && dirty) { setUnsavedPrompt(true); return; } void beginAnalysis(); }} disabled={!mutable || suggesting || (!pending && (!composer.source_gap || !composer.eaggl_anchors.length))}>{busy === "submit" ? "Submitting…" : pending ? "Recover submission" : "Start analysis"}</button></div>
</div>}
          </section>
        </div>
        {(inspectGap || inspectFactorId) && <InspectColumn gap={inspectGap} factor={inspectFactor} factorPending={!!inspectFactorId && !inspectFactor} focus={inspectGap && inspectFactorId ? inspectFocus : inspectGap ? "gap" : "factor"} gapNote={inspectGapNote} factorNote={inspectFactorNote} onFocus={setInspectFocus} onCloseGap={closeGapInspect} onCloseFactor={closeFactorInspect} />}
        </div>
        {activityOpen ? <section className="activity-float activity-pane" aria-labelledby="activity-heading"><div className="section-heading"><div><p className="step-label">Follow the evidence</p><h2 id="activity-heading">Research activity</h2></div><div className="activity-heading-actions">{job && <span className={"status " + job.status}>{readable(job.status)}</span>}<button type="button" className="quiet" onClick={() => setActivityOpen(false)}>Collapse</button></div></div><label htmlFor="job-select">Workspace jobs</label><select id="job-select" value={job?.id || ""} disabled={!!busy} onChange={event => { if (event.target.value) void openJob(event.target.value); }}><option value="">Choose a job to follow</option>{jobs.map(value => <option key={value.id} value={value.id}>{value.kind === "analysis" ? "Analysis" : "Paragraph"} — {readable(value.status)} — {date(value.created_at)}</option>)}</select>
            {moreRecords && <p className="small muted">Showing the most recent 100 drafts and jobs. A saved page URL can reopen an older item.</p>}
            {!job ? <div className="activity-empty"><svg viewBox="0 0 64 64" aria-hidden="true"><path d="M12 43h8l7-22 10 32 7-23 4 13h8" /><path d="M8 12v44h48" /></svg><h3>Your investigation will appear here.</h3><p>Start an analysis or select an existing job. Events arrive live as evidence is collected and reviewed.</p></div> : <>
              <div className="job-meta"><p>{job.kind === "analysis" ? "Analysis" : "Research paragraph"} started {date(job.created_at)}</p><span className="small muted">{readable(job.stage)}</span></div>
              <div className="stream-toolbar"><span className="small muted" role="status">{streamState}</span><div><button className="quiet small" onClick={() => setStreamAttempt(value => value + 1)}>Reconnect</button>{!terminal(job.status) && <button className="danger small" onClick={cancel} disabled={!!busy || job.status === "cancel_requested"}>{job.status === "cancel_requested" ? "Stopping…" : "Stop job"}</button>}</div></div>
              {!!job.warnings.length && <div className="notice"><ul>{job.warnings.map(value => <li key={value}>{value}</li>)}</ul></div>}
              {job.failure && <div className="notice error" role="alert"><div><strong>Research could not complete</strong><p>{job.failure.message}</p><span className="small">{job.failure.code}</span>{job.failure.code.startsWith("REVIEW_") && job.failure.retryable && <p><button className="secondary" onClick={retryReview} disabled={!!busy}>{busy === "review" ? "Requesting review…" : "Retry saved review"}</button></p>}</div></div>}
              {job.status === "cancelled" && <p className="notice">This job was stopped. No successful result is implied.</p>}
              <div className="event-log" aria-label="Job activity events">{!activity.length && <p className="empty">Loading saved activity…</p>}{activity.map(event => <article className={"event " + event.event_type} key={event.id}><div className="event-header"><span>{event.detail?.tool_name || readable(event.event_type)}</span><time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleTimeString()}</time></div><p>{event.message}</p>{event.detail?.output_excerpt && <details><summary>Captured output</summary><pre>{event.detail.output_excerpt}</pre></details>}{event.detail?.artifact_sha256 && <a href={backend("artifacts/" + event.detail.artifact_sha256)} target="_blank" rel="noreferrer">Open captured artifact</a>}</article>)}</div>
              {job.result && <ResultView job={job} openJob={openJob} />}
              <details className="record-details"><summary>Job record and evidence</summary><p className="small">Job ID: {job.id}</p><div className="result-links"><a href={backend("jobs/" + job.id)} target="_blank" rel="noreferrer">Job JSON</a><a href={backend("jobs/" + job.id + "/evidence-package")} target="_blank" rel="noreferrer">Frozen evidence package</a></div><pre>{JSON.stringify(job, null, 2)}</pre></details>
            </>}
</section> : job ? <button type="button" className="activity-launcher" onClick={() => setActivityOpen(true)}>Research activity</button> : null}
      </>}
    </main>
    {unsavedPrompt && <div className="warning-stage"><section className="warning-panel" role="alertdialog" aria-modal="true" aria-labelledby="unsaved-heading"><h2 id="unsaved-heading">Save updates first</h2><p>This draft has unsaved changes. Saving them starts the investigation.</p><div className="actions"><button type="button" className="save-updates" onClick={async () => { const saved = await save(); if (saved) await beginAnalysis(saved); }} disabled={!mutable || suggesting}>{busy === "save" ? "Saving…" : busy === "submit" ? "Starting…" : "Save updates"}</button><button type="button" className="quiet" onClick={() => setUnsavedPrompt(false)}>Cancel</button></div></section></div>}
    {draftPrompt && <div className="warning-stage"><section className="warning-panel" role="dialog" aria-modal="true" aria-labelledby="draft-save-heading"><h2 id="draft-save-heading">{draftPrompt === "start" ? "Save a draft first" : "Save a draft"}</h2><p>{draftPrompt === "start" ? "Name this draft to keep the gap and factors you selected. Saving it starts the investigation." : "Name this draft to keep the gap and factors you selected."}</p><label htmlFor="gap-draft-name">Draft name</label><input id="gap-draft-name" value={name} maxLength={120} onChange={event => setName(event.target.value)} disabled={!mutable} placeholder="Name this investigation" autoFocus /><div className="actions"><button type="button" className="save-updates" onClick={async () => { const startAnalysis = draftPrompt === "start"; const saved = await save(); if (saved && startAnalysis) await beginAnalysis(saved); }} disabled={!mutable || suggesting || !name.trim()}>{busy === "save" ? "Saving…" : busy === "submit" ? "Starting…" : "Save draft"}</button><button type="button" className="quiet" onClick={() => setDraftPrompt(null)}>Cancel</button></div></section></div>}
    {draftPicker && <SavedDrafts drafts={drafts} jobs={jobs} requests={draftRequests} gapLabels={gapLabels} factorLabels={factorLabels} ready={catalogReady} requestsReady={requestsReady} page={draftPage} deletingId={deletingId} onPage={setDraftPage} onOpen={chooseDraft} onDelete={setPendingDelete} onClose={() => { setPendingDelete(null); setDraftPicker(false); }} />}
    {settingsOpen && <SettingsPanel tab={settingsTab} openLastDraft={openLastDraft} onTab={setSettingsTab} onOpenLastDraft={value => { localStorage.setItem(SETTINGS_KEY, JSON.stringify({ openLastDraft: value })); setOpenLastDraft(value); }} onClose={() => setSettingsOpen(false)} />}
    {pendingDelete && <div className="warning-stage"><section className="warning-panel" role="alertdialog" aria-modal="true" aria-labelledby="delete-draft-heading" aria-describedby="delete-draft-copy"><h2 id="delete-draft-heading">Delete this draft</h2><p id="delete-draft-copy">{`Delete "${pendingDelete.name || "Untitled draft"}"? A submitted investigation from this draft stays available.`}</p><div className="actions"><button type="button" className="secondary" autoFocus onClick={() => setPendingDelete(null)}>Cancel</button><button type="button" className="confirm-delete" disabled={!!deletingId} onClick={() => void removeDraft(pendingDelete)}>{deletingId ? "Deleting…" : "Delete"}</button></div></section></div>}
  </>;
}

type ResultRecord = { path: string; title: string; data?: Record<string, unknown>; error?: string };
function textValue(value: unknown): string { return typeof value === "string" ? value : ""; }
function ResultView({ job, openJob }: { job: Job; openJob: (id: string) => Promise<void> }) {
  const [records, setRecords] = useState<ResultRecord[]>([]);
  const result = job.result;
  useEffect(() => {
    if (!result) return;
    let active = true;
    const paths = result.kind === "analysis" ? result.account_ids.map(id => ({ path: "accounts/" + encodeURIComponent(id), title: "Scientific account" }))
      : result.kind === "paragraph" ? [{ path: "paragraphs/" + encodeURIComponent(result.paragraph_id), title: "Research paragraph" }]
      : [{ path: "analysis-outcomes/" + encodeURIComponent(result.outcome_id), title: "Insufficient evidence" }];
    setRecords(paths);
    void Promise.all(paths.map(async record => {
      try { return { ...record, data: await request<Record<string, unknown>>(backend(record.path)) }; }
      catch (error) { return { ...record, error: errorMessage(error) }; }
    })).then(values => { if (active) setRecords(values); });
    return () => { active = false; };
  }, [job.id, JSON.stringify(result)]);
  if (!result) return null;
  return <section className="results"><h3>{result.kind === "analysis_outcome" ? "Investigation outcome" : "Saved results"}</h3>
    {records.map(record => {
      const document = record.data?.document as Record<string, Record<string, unknown>[]> | undefined;
      const object = document?.scientific_accounts?.[0];
      const paragraphs = document?.paragraphs || [];
      const artifacts = (record.data?.artifacts || []) as Schema<"ArtifactAccess">[];
      return <article className="result-record" key={record.path}><h4>{textValue(object?.name) || record.title}</h4>{record.error ? <p role="alert" className="error-text">{record.error} <a href={backend(record.path)} target="_blank" rel="noreferrer">Open result</a></p> : !record.data ? <p className="muted">Loading saved result…</p> : <>
        {result.kind === "analysis_outcome" && <><p>{textValue(record.data.summary)}</p><p>{textValue(record.data.reason)}</p>
          {([['missing_evidence', 'Missing evidence'], ['limitations', 'Limitations'], ['next_steps', 'Possible next steps']] as const).map(([field, title]) => Array.isArray(record.data![field]) && (record.data![field] as unknown[]).length > 0 ? <div className="outcome-section" key={field}><strong>{title}</strong><ul>{(record.data![field] as unknown[]).map((value, index) => <li key={index}>{textValue(value)}</li>)}</ul></div> : null)}
          {textValue(record.data.scope_note) && <p className="scope-note">{textValue(record.data.scope_note)}</p>}
        </>}
        {object?.closing_remarks && <p>{textValue(object.closing_remarks)}</p>}
        {paragraphs.map((paragraph, index) => <p className="research-text" key={textValue(paragraph.id) || index}>{textValue(paragraph.text)}</p>)}
        <div className="result-links"><a href={backend(record.path)} target="_blank" rel="noreferrer">Open saved JSON</a>{result.kind === "paragraph" && <a href={backend("paragraphs/" + encodeURIComponent(result.paragraph_id) + "/export?format=markdown")} target="_blank" rel="noreferrer">Export paragraph Markdown</a>}{artifacts.filter(artifact => artifact.availability === "available" && /^[a-f0-9]{64}$/.test(artifact.file.sha256 || "")).map(artifact => <a key={artifact.file.id} href={backend("artifacts/" + artifact.file.sha256)} target="_blank" rel="noreferrer">{artifact.file.filename || artifact.file.name || "Evidence artifact"}</a>)}</div>
        <details><summary>Inspect result</summary><pre>{JSON.stringify(record.data, null, 2)}</pre></details>
      </>}</article>;
    })}
    {result.kind === "analysis" && result.paragraph_job_ids.length > 0 && <div className="paragraph-followup"><h4>Research paragraphs</h4><p className="small muted">Paragraphs run as separate jobs after account validation.</p>{result.paragraph_job_ids.map((id, index) => <button className="secondary" key={id} onClick={() => void openJob(id)}>Follow paragraph {index + 1}</button>)}</div>}
  </section>;
}
