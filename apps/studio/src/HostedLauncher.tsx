import { useEffect, useState } from "react";
import { desktop, native, request } from "./api";

type Result = { state?: string; url?: string; ready?: boolean; model_ready?: boolean; stopped?: boolean; reason?: string };
type Operation = { id: string; state: string; phase?: string; error?: string; result?: Result };
const labels: Record<string, string> = { connecting: "Connecting to Daytona", preflight: "Checking account and quota", building: "Building your studio remotely", volumes: "Preparing persistent storage", starting: "Starting the sandbox", bootstrapping: "Preparing PostgreSQL", checking: "Checking API readiness", restoring: "Restoring your saved workspace", ready: "Studio ready" };

export function HostedLauncher({ onConnected }: { onConnected?: () => void }) {
  const [deployment, setDeployment] = useState("my-studio");
  const [saved, setSaved] = useState<string[]>([]);
  const [key, setKey] = useState("");
  const [password, setPassword] = useState("");
  const [source, setSource] = useState("");
  const [region, setRegion] = useState("");
  const [pending, setPending] = useState(false);
  const [phase, setPhase] = useState("");
  const [error, setError] = useState("");
  const [url, setUrl] = useState("");
  useEffect(() => {
    const promise = desktop ? native<{ deployments: string[] }>("hosted_deployments") : request<{ deployments: string[] }>("/launcher/deployments");
    void promise.then(v => setSaved(v.deployments || [])).catch(() => {});
  }, []);
  useEffect(() => {
    if (!desktop) return;
    let remove: (() => void) | undefined; let disposed = false;
    void import("@tauri-apps/api/event").then(({ listen }) => listen<string>("hosted-progress", event => setPhase(labels[event.payload] || event.payload)))
      .then(unlisten => { if (disposed) unlisten(); else remove = unlisten; }).catch(() => {});
    return () => { disposed = true; remove?.(); };
  }, []);
  async function run(action: string) {
    setPending(true); setError(""); setPhase("Connecting to Daytona"); setUrl("");
    const tab = !desktop && ["start", "setup"].includes(action) ? window.open("about:blank", "_blank") : null;
    if (tab) tab.opener = null;
    try {
      const payload = { deployment, action, source: source || null, region: region || null,
        api_key: action === "setup" ? key || null : null, owner_password: action === "setup" ? password || null : null };
      let result: Result;
      if (desktop) {
        result = await native<Result>("hosted_action", { payload });
      } else {
        let operation = await request<Operation>("/launcher/operations", { method: "POST", body: JSON.stringify(payload) });
        setKey(""); setPassword("");
        while (operation.state === "running") {
          await new Promise(resolve => window.setTimeout(resolve, 1000));
          operation = await request<Operation>(`/launcher/operations/${operation.id}`);
          setPhase(labels[operation.phase || ""] || operation.phase || "Working…");
        }
        if (operation.state !== "completed") throw new Error(operation.error || "Hosted operation failed");
        result = operation.result || {};
      }
      setKey(""); setPassword("");
      setSaved(values => values.includes(deployment) ? values : [...values, deployment]);
      setPhase(result.stopped ? "Studio stopped safely" : result.reason || (result.ready || result.state === "ready" ? `API ready · ${result.model_ready ? "Model ready" : "Model loads on first generation"}` : result.state || "Operation complete"));
      if (result.url) {
        const destination = new URL(result.url);
        if (destination.protocol !== "https:") throw new Error("Hosted access requires HTTPS");
        if (desktop) onConnected?.();
        else { setUrl(destination.href); if (tab) tab.location.href = destination.href; }
      } else tab?.close();
    } catch (e) { tab?.close(); setError(e instanceof Error ? e.message : "Hosted operation failed"); setPhase(""); }
    finally { setPending(false); }
  }
  return <section className="panel hosted-launcher" aria-label="Hosted studio launcher">
    <h2>Your hosted studio</h2>
    <p>Start a private studio on Daytona. Your local recordings and history stay in this workspace.</p>
    {saved.length > 0 && <label>Saved deployment<select disabled={pending} value={saved.includes(deployment) ? deployment : ""} onChange={e => setDeployment(e.target.value)}><option value="" disabled>Select a deployment</option>{saved.map(name => <option key={name}>{name}</option>)}</select></label>}
    <label>Deployment name<input value={deployment} disabled={pending} pattern="[a-z][a-z0-9-]{0,47}" onChange={e => setDeployment(e.target.value)} /></label>
    <div className="actions"><button className="button primary" disabled={pending || !deployment} onClick={() => void run("start")}>Start hosted studio</button>
      <button className="button secondary" disabled={pending} onClick={() => void run("status")}>Check status</button>
      <button className="text-button" disabled={pending} onClick={() => void run("backup")}>Create checkpoint</button>
      <button className="text-button" disabled={pending} onClick={() => void run("stop")}>Stop hosted studio</button></div>
    <details><summary>Set up hosted studio</summary>
      <p>First setup builds remotely and may take several minutes. Allocation: 4 CPUs, 8 GiB RAM, 20 GiB disk. Daytona compute and storage charges apply.</p>
      <label>Daytona API key<input type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} disabled={pending} /></label>
      <label>New owner password<input type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} minLength={12} disabled={pending} /></label>
      <details><summary>Advanced setup</summary><label>Source directory<input value={source} onChange={e => setSource(e.target.value)} placeholder="Use packaged release sources" disabled={pending} /></label>
        <label>Daytona region<input value={region} onChange={e => setRegion(e.target.value)} placeholder="Account default" disabled={pending} /></label></details>
      <button className="button primary" disabled={pending || (!saved.includes(deployment) && (!key || password.length < 12))} onClick={() => void run("setup")}>Set up hosted studio</button>
    </details>
    {phase && <p role="status" aria-live="polite">{phase}</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {url && <a href={url} target="_blank" rel="noreferrer">Open hosted studio</a>}
    <p className="hint">Stops after ten idle minutes without outstanding jobs. Hosted content expires after 30 days. Physical cleanup runs while active and at next launch; stopped storage can retain files longer.</p>
  </section>;
}
