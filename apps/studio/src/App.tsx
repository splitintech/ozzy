import { HostedLauncher } from "./HostedLauncher";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, artifactBlob, desktop, importVoice, native, request, saveArtifact, setAccessKey } from "./api";
import { ACTIVE, type Capabilities, type Job, type ModelName, type Options, type Voice } from "./types";

type Page = "create" | "voice" | "history" | "settings";
type CloudSession = { sandbox_id: string; role: string; state: string };
const modelLabels: Record<ModelName, string> = { nano: "Nano", turbo: "Turbo", original: "Original" };
const phases: Record<Job["state"], string> = {
  queued: "Waiting in the queue", starting: "Starting your engine", loading: "Loading your voice",
  generating: "Giving your words a voice", processing: "Preparing your audio",
  completed: "Ready to listen", failed: "This take needs another try", cancelled: "Generation cancelled",
};

function Glyph({ kind = "wave" }: { kind?: string }) {
  return <span className={`glyph glyph-${kind}`} aria-hidden="true">
    {[0, 1, 2, 3, 4].map(i => <i key={i} />)}
  </span>;
}

function Player({ job, onPlayed, onError }: { job: Job; onPlayed?: () => void; onError: (e: unknown) => void }) {
  const [url, setUrl] = useState("");
  const artifact = job.artifacts.find(a => a.kind === "mp3");
  useEffect(() => {
    let alive = true; let objectUrl = "";
    setUrl("");
    if (artifact) artifactBlob(artifact.url).then(blob => {
      objectUrl = URL.createObjectURL(blob);
      if (alive) setUrl(objectUrl); else URL.revokeObjectURL(objectUrl);
    }).catch(e => { if (alive) onError(e); });
    return () => { alive = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [artifact?.id, onError]);
  return <div className="player">
    <div className="player-top"><Glyph /><div><strong>{phases[job.state]}</strong>
      <p>{job.voice_name} <span>·</span> {modelLabels[job.options.model]} {artifact ? `· ${Math.round(artifact.duration)} sec` : ""}</p>
    </div><span className="take-label">TAKE {job.id.slice(0, 4).toUpperCase()}</span></div>
    {url ? <audio aria-label="Generated speech preview" controls preload="metadata" src={url} onPlay={onPlayed} /> : <p role="status">Loading preview…</p>}
    <div className="exports">
      {job.artifacts.filter(a => ["mp3", "wav", "telephone"].includes(a.kind)).map(a =>
        <button className={a.kind === "mp3" ? "button primary" : "button secondary"} key={a.id}
          onClick={() => saveArtifact(a.url, `voice-${job.id.slice(0, 8)}-${a.kind}${a.kind === "mp3" ? ".mp3" : ".wav"}`).catch(onError)}>
          <span aria-hidden="true">↓</span> {a.kind === "telephone" ? "Phone WAV" : a.kind.toUpperCase()}
        </button>)}
    </div>
    <p className="hint">Phone WAV is an 8 kHz compatibility preset. Check your phone provider’s requirements.</p>
  </div>;
}

function Connection({ onConnected, error, onError }: { onConnected: () => void; error: string; onError: (e: unknown) => void }) {
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState("local");
  const [accessExpiresAt, setAccessExpiresAt] = useState<number | null>(null);
  const [pending, setPending] = useState(false);
  const [manifestUrl, setManifestUrl] = useState(import.meta.env.VITE_RUNTIME_MANIFEST_URL || "");
  const [runtimeInfo, setRuntimeInfo] = useState<{ bytes: number; unpacked_bytes: number; platform: string }>();
  const [runtime, setRuntime] = useState<{ installed: boolean; platform: string; available_devices?: string[] }>();
  const [device, setDevice] = useState("cpu");
  const [downloadStatus, setDownloadStatus] = useState("");
  useEffect(() => {
    if (!desktop) request<{ mode: string; local_access_expires_at?: number | null }>("/v1/session").then(v => {
      setMode(v.mode); setAccessExpiresAt(v.local_access_expires_at ?? null);
    }).catch(onError);
    else native<typeof runtime>("runtime_status").then(setRuntime).catch(onError);
  }, [onError]);
  async function connect() {
    setPending(true);
    try {
      if (desktop && mode === "local") await native("start_local", { device });
      else if (mode === "hosted") await request("/v1/auth/login", { method: "POST", body: JSON.stringify({ password }) });
      else setAccessKey(password.trim());
      await api.capabilities();
      setPassword("");
      onConnected();
    } catch (e) { onError(e); } finally { setPending(false); }
  }
  async function inspectRuntime() {
    setPending(true);
    try {
      const info = await native<{ bytes: number; unpacked_bytes: number; platform: string }>("inspect_runtime", { manifestUrl });
      setRuntimeInfo(info);
      setDownloadStatus("");
    } catch (e) { setRuntimeInfo(undefined); onError(e); } finally { setPending(false); }
  }
  async function install() {
    if (!runtimeInfo) return;
    setPending(true);
    setDownloadStatus("Downloading and verifying the runtime…");
    try {
      await native("install_runtime", { manifestUrl });
      setDownloadStatus("Runtime installed. You can now open your local workspace.");
      setRuntimeInfo(undefined);
      setRuntime(await native<typeof runtime>("runtime_status"));
    } catch (e) { onError(e); } finally { setPending(false); }
  }
  return <main className="connection">
    <div className="brand"><Glyph /><span>voice<span className="brand-light">studio</span><sup>01</sup></span></div>
    <span className="eyebrow">A SMALL STUDIO. YOUR OWN VOICE.</span>
    <h1>Make it<br /><em>sound like you.</em></h1>
    <p className="intro">A private space to turn your words into spoken messages. Start with one recording. Keep every take your own.</p>
    <form onSubmit={e => { e.preventDefault(); void connect(); }}>
      {desktop && mode === "local" && runtime?.installed && <label>Local processing device
        <select value={device} onChange={e => setDevice(e.target.value)} disabled={pending}>
          {["cpu", "mps", "cuda"].filter(value => value === "cpu" || runtime.available_devices?.includes(value)).map(value =>
            <option key={value} value={value}>{({ cpu: "CPU", mps: "Apple Silicon GPU (MPS)", cuda: "NVIDIA GPU (CUDA)" })[value]}</option>)}
        </select>
      </label>}
      {desktop && runtime?.platform && <p className="hint">{runtime.platform}{runtime.installed ? " · runtime installed" : " · install the runtime to begin"}</p>}
      {(!desktop || mode === "hosted") && <label>{mode === "hosted" ? "Owner password" : "Local access key"}
        <input type="password" autoComplete={mode === "hosted" ? "current-password" : "off"}
          value={password} onChange={e => setPassword(e.target.value)} required />
      </label>}
      {!desktop && mode === "local" && accessExpiresAt !== null && <p className="hint">
        Access key expires: <time dateTime={new Date(accessExpiresAt * 1000).toISOString()}>{new Date(accessExpiresAt * 1000).toLocaleString()}</time>.
        {" "}Restart the studio with a new key after expiry. Saved voices and takes stay in your workspace.
      </p>}
      <button className="button primary" disabled={pending}>{pending ? "Connecting…" : desktop && mode === "local" ? "Open local workspace" : "Open your studio"}{!pending && <span aria-hidden="true"> →</span>}</button>
    </form>
    {desktop && <details className="runtime-setup"><summary>First time? Install the local runtime</summary>
      <p>The engine downloads separately. Once the engine and model are installed, generation works offline.</p>
      {!manifestUrl && <p className="hint">This preview has no default runtime download. Use the signed manifest URL provided with your release.</p>}
      <details><summary>Advanced download source</summary><label>Signed runtime manifest URL<input type="url" value={manifestUrl} disabled={pending} onChange={e => { setManifestUrl(e.target.value); setRuntimeInfo(undefined); setDownloadStatus(""); }} placeholder="https://…/runtime-manifest.json" /></label></details>
      <button className="button secondary" disabled={pending || !manifestUrl} onClick={() => void inspectRuntime()}>Review runtime download</button>
      {runtimeInfo && <div className="notice">
        <p>{(runtimeInfo.bytes / 1e6).toFixed(0)} MB download · {((runtimeInfo.bytes + runtimeInfo.unpacked_bytes + 512_000_000) / 1e9).toFixed(1)} GB free disk space required · {runtimeInfo.platform}</p>
        <button className="button primary" disabled={pending} onClick={() => void install()}>Download and verify runtime</button>
      </div>}
      <p role="status">{downloadStatus}</p>
    </details>}
    {desktop && <HostedLauncher onConnected={() => setMode("hosted")} />}
    {desktop && <CloudConnection onConnected={() => setMode("hosted")} onError={onError} />}
    {error && <div role="alert" className="error">{error}</div>}
    <p className="privacy-note">Your recordings stay private. Local mode never uploads them.</p>
  </main>;
}

function CloudConnection({ onConnected, onError }: { onConnected: () => void; onError: (e: unknown) => void }) {
  const [key, setKey] = useState(""); const [snapshot, setSnapshot] = useState("");
  const [secretMap, setSecretMap] = useState("{}"); const [pending, setPending] = useState(false);
  const [sessions, setSessions] = useState<CloudSession[]>();
  return <details className="runtime-setup"><summary>Advanced: external PostgreSQL/S3 deployment</summary>
    <p>Your hosted reference and history are separate. This does not transfer local recordings.</p>
    <label>Daytona API key<input type="password" value={key} onChange={e => setKey(e.target.value)} autoComplete="off" /></label>
    <label>Controller snapshot<input value={snapshot} onChange={e => setSnapshot(e.target.value)} /></label>
    <label>Secret mapping JSON<textarea rows={3} value={secretMap} onChange={e => setSecretMap(e.target.value)} /></label>
    <p className="hint">Map environment-variable names to Daytona secret names. Configure storage, owner login, and worker snapshot in deployment setup.</p>
    <button className="button secondary" disabled={pending || !snapshot} onClick={async () => {
      setPending(true);
      try {
        await native("cloud_start", { apiKey: key || null, snapshot, secretNames: JSON.parse(secretMap) });
        setKey(""); onConnected();
      } catch (e) { onError(e); } finally { setPending(false); }
    }}>{pending ? "Starting hosted workspace…" : "Start hosted workspace"}</button>
    <button className="text-button" disabled={pending} onClick={async () => {
      setPending(true);
      try { setSessions(await native<CloudSession[]>("cloud_status")); }
      catch (e) { onError(e); } finally { setPending(false); }
    }}>Check hosted status</button>
    {sessions && <p role="status">{sessions.length ? sessions.map(s => `${s.role}: ${s.state}`).join(" · ") : "No hosted session is running."}</p>}
  </details>;
}

export default function App() {
  const [page, setPage] = useState<Page>("create");
  const [connected, setConnected] = useState(false);
  const [cap, setCap] = useState<Capabilities>();
  const [voices, setVoices] = useState<Voice[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [text, setText] = useState("");
  const [options, setOptions] = useState<Options>({ model: "nano", seed: 42, temperature: 0.8 });
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  const [currentJob, setCurrentJob] = useState("");
  const [file, setFile] = useState<File>(); const [voiceName, setVoiceName] = useState("My voice");
  const [start, setStart] = useState(0); const [end, setEnd] = useState(15);
  const endEdited = useRef(false);
  const [authorized, setAuthorized] = useState(false);
  const [referenceUrl, setReferenceUrl] = useState(""); const [played, setPlayed] = useState<string[]>([]);
  const [confirmDelete, setConfirmDelete] = useState<{ kind: "voice" | "job"; id: string }>();
  const [apiKeys, setApiKeys] = useState<{ id: string; name: string; revoked: boolean }[]>([]);
  const [newKey, setNewKey] = useState(""); const [keyName, setKeyName] = useState("");
  const [usage, setUsage] = useState<{ estimated_spend_usd: number; limit_usd: number; budget_alert?: "warning" | "limit" | null; percent_used?: number }>();
  const [update, setUpdate] = useState<{ available: boolean; version?: string; notes?: string }>();
  const [updateStatus, setUpdateStatus] = useState("");
  const [updating, setUpdating] = useState(false);
  const [cloudStatus, setCloudStatus] = useState("");
  const [saveState, setSaveState] = useState("");
  const activityAt = useRef(0);
  const workspaceEpoch = useRef(0);
  const deleteTrigger = useRef<HTMLElement | null>(null);
  const report = useCallback((e: unknown) => {
    setError(e instanceof Error ? e.message : String(e));
    if (e instanceof ApiError && e.status === 401) setConnected(false);
  }, []);
  const refresh = useCallback(async (quietUnauthorized = false) => {
    const epoch = workspaceEpoch.current;
    try {
      const [c, v, j] = await Promise.all([api.capabilities(), api.voices(), api.jobs()]);
      if (epoch !== workspaceEpoch.current) return;
      setCap(c); setVoices(v); setJobs(j); setConnected(true);
    } catch (e) { if (epoch === workspaceEpoch.current && !(quietUnauthorized && e instanceof ApiError && e.status === 401)) report(e); }
  }, [report]);
  useEffect(() => { if (!desktop) void refresh(true); }, [refresh]);
  useEffect(() => {
    if (!connected) return;
    const delay = jobs.some(j => ACTIVE.has(j.state)) || cap?.models.some(m => m.install?.state === "downloading") ? 1500 : 8000;
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, delay);
    return () => window.clearInterval(timer);
  }, [connected, jobs, cap, refresh]);
  useEffect(() => {
    if (!file) { setReferenceUrl(""); return; }
    const url = URL.createObjectURL(file); setReferenceUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  useEffect(() => {
    if (connected && page === "settings") {
      request<typeof apiKeys>("/v1/keys").then(setApiKeys).catch(report);
      request<typeof usage>("/v1/usage").then(setUsage).catch(report);
    }
  }, [connected, page, report]);
  useEffect(() => {
    if (!confirmDelete && deleteTrigger.current) {
      const target = deleteTrigger.current.isConnected ? deleteTrigger.current : document.getElementById("main");
      target?.focus();
      deleteTrigger.current = null;
    }
  }, [confirmDelete]);
  function openDelete(kind: "voice" | "job", id: string) {
    deleteTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setConfirmDelete({ kind, id });
  }
  function resetWorkspace() {
    workspaceEpoch.current += 1;
    setConnected(false); setCap(undefined); setJobs([]); setVoices([]); setText("");
    setCurrentJob(""); setFile(undefined); setAuthorized(false); setPlayed([]);
    setNewKey(""); setKeyName(""); setApiKeys([]); setUsage(undefined);
    setUpdate(undefined); setUpdateStatus("");
    setVoiceName("My voice"); setStart(0); setEnd(15); setPage("create");
    setConfirmDelete(undefined); setAccessKey(""); setError("");
  }
  function activity() {
    if (connected && Date.now() - activityAt.current > 30000) {
      activityAt.current = Date.now();
      void request("/v1/activity", { method: "POST" }).catch(() => {});
    }
  }
  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError(""); setSaveState("Saving…");
    try { await action(); await refresh(); setSaveState("Saved"); } catch (e) { setSaveState(e instanceof ApiError && e.status === 503 ? "Storage unavailable · retry the operation" : "Not saved"); report(e); } finally { setBusy(false); }
  }
  const active = voices.find(v => v.active);
  const selectedJob = jobs.find(j => j.id === currentJob);
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  const canGenerate = !!active && text.trim().length > 0 && words <= 280 && text.length <= 2000 && !busy;
  if (!connected) return <Connection onConnected={() => { setError(""); void refresh(); }} error={error} onError={report} />;

  return <div className="app" onPointerDown={activity} onKeyDown={activity}>
    {saveState && <p className="save-status" role="status" aria-live="polite">{saveState}</p>}
    <a className="skip-link" href="#main" inert={!!confirmDelete}>Skip to workspace</a>
    <aside className="sidebar" inert={!!confirmDelete}>
      <a className="brand" href="#create" onClick={() => setPage("create")}><Glyph /><span>voice<span className="brand-light">studio</span><sup>01</sup></span></a>
      <div className="sidebar-caption">YOUR WORKSPACE</div>
      <nav aria-label="Workspace">
        {(["create", "voice", "history", "settings"] as Page[]).map((item, i) =>
          <button key={item} aria-current={page === item ? "page" : undefined}
            className={page === item ? "nav-item current" : "nav-item"} onClick={() => { setPage(item); setError(""); }}>
            <span className="nav-number" aria-hidden="true">0{i + 1}</span>{({ create: "Create", voice: "Your voice", history: "History", settings: "Settings" })[item]}
            {page === item && <span className="nav-arrow" aria-hidden="true">↗</span>}
          </button>)}
      </nav>
      <div className="sidebar-bottom">
        <div className="mode-badge"><span className="status-dot" />{cap?.mode === "hosted" ? "Daytona workspace" : "Local workspace"}</div>
        <p>{cap?.mode === "hosted" ? "Private. On demand." : "On your device. Yours to keep."}</p>
        <span className="version">VOICE STUDIO / 0.1</span>
      </div>
    </aside>
    <div className="workspace" inert={!!confirmDelete}>
      <header className="topbar"><span>ONE VOICE. ENDLESS POSSIBILITIES.</span><span className="owner-label">OWNER WORKSPACE <span className="avatar">Y</span></span></header>
      <main id="main" tabIndex={-1}>
        {cap?.orchestration_error && <div role="status" className="notice">{cap.orchestration_error}</div>}
        {error && <div role="alert" className="error">{error}<button aria-label="Dismiss error" onClick={() => setError("")}>×</button></div>}
        {page === "create" && <>
          <div className="page-heading"><div><span className="eyebrow">THE RECORDING DESK</span><h1>Your words.<br /><em>Your voice.</em></h1></div>
            <p>Write a message.<br />Let your voice do the rest.</p></div>
          <div className="creation-grid">
            <section className="script-panel" aria-labelledby="script-heading">
              <div className="panel-heading"><h2 id="script-heading">The script</h2><span className="tag">ENGLISH · UP TO 2 MIN</span></div>
              <label className="sr-only" htmlFor="script">Your voicemail script</label>
              <textarea id="script" className="script-input" value={text} maxLength={2000}
                placeholder={"Hi, you’ve reached me.\n\nLeave your name and a message, and I’ll get back to you soon."}
                onChange={e => setText(e.target.value)} spellCheck />
              <div className="script-footer"><span>{words} / 280 words <span className="muted">· {text.length} / 2,000 characters</span></span><span>Plain text</span></div>
              <details className="advanced"><summary>Fine-tune this take <span>+</span></summary>
                <div className="form-grid">
                  <label>Model<select value={options.model} onChange={e => setOptions({ model: e.target.value as ModelName, seed: options.seed, temperature: options.temperature })}>
                    {cap?.models.map(m => <option key={m.id} value={m.id}>{modelLabels[m.id]}{m.configured_remote ? " · worker image" : m.installed ? "" : " · needs installation"}</option>)}
                  </select></label>
                  <label>Seed<input type="number" min="0" max="4294967295" value={options.seed} onChange={e => setOptions({ ...options, seed: Number(e.target.value) })} /></label>
                  <label>Variation<input type="range" min="0.1" max="1.2" step="0.1" value={options.temperature} onChange={e => setOptions({ ...options, temperature: Number(e.target.value) })} /></label>
                  {options.model === "original" && <>
                    <label>Expression<input type="range" min="0" max="1" step="0.05" value={options.exaggeration ?? 0.5} onChange={e => setOptions({ ...options, exaggeration: Number(e.target.value) })} /></label>
                    <label>Guidance<input type="range" min="0" max="1" step="0.05" value={options.cfg_weight ?? 0.5} onChange={e => setOptions({ ...options, cfg_weight: Number(e.target.value) })} /></label>
                  </>}
                </div>
              </details>
              <div className="generate-row"><span>Every generation is a new take.</span>
                <button className="button primary" disabled={!canGenerate} onClick={() => void perform(async () => {
                  const job = await api.generate(active!.id, text, options); setCurrentJob(job.id);
                })}>{busy ? "Submitting…" : "Generate voice"} <span aria-hidden="true">↗</span></button>
              </div>
            </section>
            <aside className="voice-card"><span className="eyebrow">ON THE MIC</span><div className="voice-orb"><Glyph /></div>
              <h2>{active?.name || "Your voice goes here"}</h2>
              <p>{active ? `${active.details.reference_duration.toFixed(1)} seconds of reference.\nA familiar voice for every message.` : "A short recording is all you need.\nStart with a clear, single-speaker clip."}</p>
              <button className="text-button" onClick={() => setPage("voice")}>{active ? "Manage your voice" : "Add a reference voice"} <span aria-hidden="true">→</span></button>
              <div className="voice-card-foot"><span className="status-dot" />{active ? "Approved by you" : "No voice connected"}</div>
            </aside>
          </div>
          <section className="output-section" aria-label="Current take">
            {selectedJob?.state === "completed" ? <Player job={selectedJob} onError={report} /> :
              selectedJob && ACTIVE.has(selectedJob.state) ? <div className="progress-card" role="status"><Glyph /><div><strong>{phases[selectedJob.state]}</strong><p>Startup and generation are shown separately. You can leave this tab open.</p></div>
                <button className="button secondary" onClick={() => void perform(() => api.cancel(selectedJob.id))}>Cancel</button></div> :
              selectedJob?.state === "failed" ? <div className="error" role="alert">{selectedJob.error?.message}</div> :
              selectedJob?.state === "cancelled" ? <div className="notice" role="status">This take was cancelled. You can generate another whenever you’re ready.</div> :
              <div className="empty-output"><span className="empty-icon" aria-hidden="true">↳</span><div><strong>A little space for your next take.</strong><p>Your generated message will appear here, ready to listen and export.</p></div></div>}
          </section>
        </>}
        {page === "voice" && <>
          <div className="page-heading"><div><span className="eyebrow">THE ORIGINAL RECORDING</span><h1>Start with<br /><em>something familiar.</em></h1></div><p>One speaker. A quiet room.<br />Ten to fifteen good seconds.</p></div>
          <section className="panel import-panel"><h2>Add a reference</h2>
            {cap?.mode === "hosted" && <p className="notice">Importing here uploads this recording to your private hosted storage. Local recordings are never transferred automatically.</p>}
            <label className="dropzone"><span className="upload-mark" aria-hidden="true">↑</span><strong>{file?.name || "Choose your recording"}</strong><span>MP3 or WAV · up to 50 MB</span>
              <input type="file" accept=".mp3,.wav,audio/mpeg,audio/wav" aria-label="Reference recording" onChange={e => {
                setFile(e.target.files?.[0]); endEdited.current = false; setStart(0); setEnd(15); setAuthorized(false);
                e.target.value = "";
              }} />
            </label>
            {referenceUrl && <audio controls src={referenceUrl} aria-label="Original reference preview" onLoadedMetadata={e => { if (!endEdited.current) setEnd(Math.min(15, e.currentTarget.duration)); }} />}
            <div className="form-grid"><label>Voice name<input value={voiceName} maxLength={100} onChange={e => setVoiceName(e.target.value)} /></label>
              <label>Start (seconds)<input type="number" min="0" step="0.1" value={start} onChange={e => setStart(Number(e.target.value))} /></label>
              <label>End (seconds)<input type="number" min="5.1" step="0.1" value={end} onFocus={() => { endEdited.current = true; }} onChange={e => { endEdited.current = true; setEnd(Number(e.target.value)); }} /></label></div>
            <label className="check-label"><input type="checkbox" checked={authorized} onChange={e => setAuthorized(e.target.checked)} />This is my voice, or I have the speaker’s authorization to use it.</label>
            <button className="button primary" disabled={!file || !authorized || !voiceName.trim() || busy} onClick={() => void perform(async () => {
              await importVoice(file!, voiceName, start, end); setFile(undefined); setAuthorized(false);
            })}>{busy ? "Preparing recording…" : "Prepare reference"}</button>
          </section>
          <div className="section-caption">YOUR REFERENCE VERSIONS <span>{voices.length}</span></div>
          {voices.map(voice => {
            const audition = jobs.find(j => j.voice_id === voice.id && j.purpose === "audition" && j.state === "completed");
            const pending = jobs.find(j => j.voice_id === voice.id && j.purpose === "audition" && ACTIVE.has(j.state));
            const latest = jobs.find(j => j.voice_id === voice.id && j.purpose === "audition");
            return <section className="panel reference-card" key={voice.id}><div className="panel-heading"><div><h2>{voice.name}</h2><p className="hint">{voice.details.reference_duration.toFixed(1)} sec · version {voice.id.slice(0, 6)}</p></div><span className={`tag ${voice.active ? "accent" : ""}`}>{voice.active ? "ACTIVE VOICE" : "AWAITING APPROVAL"}</span></div>
              {voice.details.warnings.map(w => <p className="notice" key={w}>{w}</p>)}
              {audition && <Player job={audition} onPlayed={() => setPlayed(v => [...v, audition.id])} onError={report} />}
              {pending && <p role="status">{phases[pending.state]}</p>}
              {latest?.state === "failed" && <p className="error" role="alert">{latest.error?.message || "The audition could not finish. Try again."}</p>}
              {latest?.state === "cancelled" && <p className="hint" role="status">The latest audition was cancelled. You can generate another.</p>}
              <div className="actions"><button className="button secondary" disabled={busy || !!pending} onClick={() => void perform(() => api.generate(voice.id, "Audition", options, "audition"))}>Generate audition</button>
                {audition && !voice.active && <button className="button primary" disabled={busy || !played.includes(audition.id)} onClick={() => void perform(() => api.activate(voice.id, audition.id))}>Approve this voice</button>}
                <button className="text-button" onClick={() => saveArtifact(`/v1/voices/${voice.id}/export`, "voice-reference.zip").catch(report)}>Export reference</button>
                <button className="text-button danger" onClick={() => openDelete("voice", voice.id)}>Delete</button></div>
              {audition && !voice.active && <p className="hint">Listen to the audition before approving your voice.</p>}
            </section>;
          })}
        </>}
        {page === "history" && <>
          <div className="page-heading"><div><span className="eyebrow">THE TAKES YOU KEEP</span><h1>A record of<br /><em>your words.</em></h1></div><p>{cap?.mode === "hosted" ? "Private history.\nAvailable for thirty days." : "Saved on this device.\nKeep them as long as you like."}</p></div>
          {!jobs.length && <div className="empty-output"><Glyph /><div><strong>A quiet studio, for now.</strong><p>Your first generated message will begin the collection.</p></div></div>}
          {jobs.map(job => <article key={job.id} className="panel history-item"><div className="panel-heading"><span className="eyebrow">{new Date(job.created_at * 1000).toLocaleString()} · {job.purpose}</span><span className="tag">{job.state}</span></div>
            <p className="script-excerpt">{job.text}</p>
            {job.state === "completed" && <Player job={job} onError={report} />}
            {job.error && <p className="error">{job.error.message}</p>}
            <div className="actions"><button className="text-button" onClick={() => { setText(job.text); setOptions(job.options); setPage("create"); }}>Use this script →</button>
              {ACTIVE.has(job.state) && <button className="text-button" onClick={() => void perform(() => api.cancel(job.id))}>Cancel</button>}
              <button className="text-button danger" onClick={() => openDelete("job", job.id)}>Delete take</button></div>
          </article>)}
        </>}
        {page === "settings" && <>
          <div className="page-heading"><div><span className="eyebrow">BEHIND THE DESK</span><h1>Make yourself<br /><em>at home.</em></h1></div></div>
          <section className="panel"><h2>Your workspace</h2><p>{cap?.mode === "local" ? "Local processing. Your scripts and recordings stay on this device." : "On-demand Daytona processing. Your reference and history are private."}</p>
            <p className="hint">Switching mode does not move your data. Export a reference, extract reference.wav from the ZIP, then explicitly import that recording in the other workspace.</p>
            {desktop && cap?.profile !== "hosted_daytona_single" && <div className="actions"><button className="button secondary" onClick={async () => {
              try { await native("disconnect"); resetWorkspace(); } catch (e) { report(e); }
            }}>Switch workspace</button><button className="text-button danger" onClick={async () => {
              try { await native("cloud_stop"); resetWorkspace(); } catch (e) { report(e); }
            }}>Stop hosted session</button><button className="text-button" disabled={busy} onClick={async () => {
              setBusy(true);
              try {
                const sessions = await native<CloudSession[]>("cloud_status");
                setCloudStatus(sessions.length ? sessions.map(s => `${s.role}: ${s.state}`).join(" · ") : "No hosted session is running.");
              } catch (e) { report(e); } finally { setBusy(false); }
            }}>Check hosted status</button></div>}
            {cloudStatus && <p role="status">{cloudStatus}</p>}
            {cap?.mode === "hosted" && <button className="text-button" disabled={busy} onClick={async () => {
              setBusy(true);
              try { await request("/v1/auth/logout", { method: "POST" }); resetWorkspace(); }
              catch (e) { report(e); } finally { setBusy(false); }
            }}>Sign out</button>}
          </section>
          {(desktop || cap?.mode === "local") && <HostedLauncher onConnected={resetWorkspace} />}
          <section className="panel"><div className="panel-heading"><h2>Voice engines</h2><span className="tag">{cap?.device.toUpperCase()}</span></div>
            <p>Install once, then generate offline in local mode. Downloads are pinned and verified.</p>
            {cap?.models.map(model => <div className="model-row" key={model.id}><div><strong>{modelLabels[model.id]}</strong><p className="hint">{(model.download_bytes / 1e9).toFixed(1)} GB · {model.id === "nano" ? "A smaller English engine, designed for CPU." : model.id === "turbo" ? "Faster English generation." : "English with expressive controls."}</p>
              {model.install && <p role="status" className="hint">{model.install.state}{model.install.file ? ` · ${model.install.file}` : ""}{model.install.message ? ` · ${model.install.message}` : ""}</p>}</div>
              <button className="button secondary" disabled={model.installed || model.install?.state === "downloading" || cap.mode === "hosted"} onClick={() => void perform(() => api.install(model.id))}>{model.configured_remote ? "In worker image" : model.installed ? "Installed ✓" : "Download"}</button></div>)}
          </section>
          {desktop && <section className="panel"><h2>Application updates</h2>
            <p>Check for a signed application release. Installing an update restarts Voice Studio.</p>
            <div className="actions"><button className="button secondary" disabled={updating} onClick={async () => {
              setUpdating(true); setUpdateStatus("Checking for an update…"); setError("");
              try {
                const result = await native<{ available: boolean; version?: string; notes?: string }>("check_update");
                setUpdate(result); setUpdateStatus(result.available ? `Version ${result.version} is available.` : "You have the latest available release.");
              } catch (e) { setUpdate(undefined); setUpdateStatus(""); report(e); } finally { setUpdating(false); }
            }}>Check for updates</button>
              {update?.available && <button className="button primary" disabled={updating || jobs.some(j => ACTIVE.has(j.state))} onClick={async () => {
                setUpdating(true); setUpdateStatus("Downloading and verifying the update. The application will restart…");
                try { await native("install_update"); } catch (e) { setUpdateStatus(""); report(e); } finally { setUpdating(false); }
              }}>Install {update.version} and restart</button>}
            </div>
            {updateStatus && <p role="status" className="hint">{updateStatus}</p>}
            {update?.notes && <p className="release-notes">{update.notes}</p>}
            {jobs.some(j => ACTIVE.has(j.state)) && <p className="hint">Finish or cancel active takes before installing an update or restoring a runtime.</p>}
            {cap?.mode === "local" && <details className="runtime-setup"><summary>Restore the previous local runtime</summary>
              <p>If a runtime update fails, restore the previous installed runtime. This closes your local workspace; open it again to start the restored version.</p>
              <button className="button secondary" disabled={updating || jobs.some(j => ACTIVE.has(j.state))} onClick={async () => {
                setUpdating(true); setError("");
                try { await native("rollback_runtime"); resetWorkspace(); } catch (e) { report(e); } finally { setUpdating(false); }
              }}>Restore previous runtime</button>
            </details>}
          </section>}
          {cap?.mode === "hosted" && usage && <section className="panel"><h2>Hosted usage</h2>{usage.budget_alert && <p className="notice" role="status">Compute budget alert: {Math.round(usage.percent_used ?? 80)}% of your monthly admission limit is used. New work is blocked when the configured limit is reached.</p>}<p className="usage-number">${usage.estimated_spend_usd.toFixed(2)} <span>/ ${usage.limit_usd.toFixed(0)} configured compute limit</span></p><p className="hint">Estimated compute charges. Database, storage, and transfer charges are separate. The provider’s billing is authoritative.</p></section>}
          <section className="panel"><h2>API access</h2><p>Create a revocable key for a trusted application. A hosted session must be running before it can accept requests.</p>
            <div className="inline-form"><label>Key name<input value={keyName} maxLength={80} onChange={e => setKeyName(e.target.value)} placeholder="My application" /></label><button className="button secondary" disabled={!keyName || busy} onClick={() => void perform(async () => {
              const created = await request<{ key: string }>("/v1/keys", { method: "POST", body: JSON.stringify({ name: keyName }) });
              setNewKey(created.key); setKeyName(""); setApiKeys(await request("/v1/keys"));
            })}>Create key</button></div>
            {newKey && <div className="notice"><label>Copy this key now. It is shown only once.<input readOnly value={newKey} onFocus={e => e.target.select()} /></label><button className="text-button" onClick={() => setNewKey("")}>Dismiss key</button></div>}
            {apiKeys.filter(k => !k.revoked).map(k => <div className="model-row" key={k.id}><strong>{k.name}</strong><button className="text-button danger" onClick={() => void perform(async () => {
              await request(`/v1/keys/${k.id}`, { method: "DELETE" }); setApiKeys(await request("/v1/keys"));
            })}>Revoke</button></div>)}
          </section>
        </>}
        <footer className="workspace-footer"><span>MADE TO SOUND HUMAN.</span><span>Private by design. Open by nature.</span></footer>
      </main>
    </div>
    {confirmDelete && <div className="modal-backdrop"><section className="modal" onKeyDown={e => {
      if (e.key === "Escape") setConfirmDelete(undefined);
      if (e.key === "Tab") {
        const buttons = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }} role="alertdialog" aria-modal="true" aria-labelledby="delete-title" aria-describedby="delete-description">
      <h2 id="delete-title">Delete {confirmDelete.kind === "voice" ? "this voice and its takes" : "this take"}?</h2>
      <p id="delete-description">{confirmDelete.kind === "voice" ? "The reference and all messages made from it will be removed. Running generations will be cancelled." : "The script and generated audio will be removed."}</p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="actions"><button autoFocus className="button secondary" onClick={() => setConfirmDelete(undefined)}>Keep it</button>
        <button className="button destructive" disabled={busy} onClick={() => void perform(async () => {
          if (confirmDelete.kind === "voice") await api.removeVoice(confirmDelete.id); else await api.removeJob(confirmDelete.id);
          deleteTrigger.current = document.getElementById("main");
          setConfirmDelete(undefined);
        })}>Delete</button></div>
    </section></div>}
  </div>;
}
