import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const voice = { id: "voice-one", name: "My reference", active: true, authorized: true, created_at: 1, checksum: "hash",
  details: { reference_duration: 12, sample_rate: 24000, warnings: [], excerpt_start: 0, excerpt_end: 12 } };
let captured: { path: string; init?: RequestInit }[];
let mode = "local";
beforeEach(() => {
  captured = []; mode = "local";
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    const path = String(input); captured.push({ path, init });
    let body: unknown = {};
    if (path === "/v1/capabilities") body = { mode, device: "cpu", models: [{ id: "nano", installed: true, download_bytes: 1 }], limits: {}, performance_qualified: false };
    if (path === "/v1/voices") body = [voice];
    if (path === "/v1/generations") body = init?.method === "POST" ? { id: "take-one", state: "queued" } : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
describe("Studio workflow", () => {
  it("shows the server's fixed local access deadline on the sign-in screen", async () => {
    const deadline = 1800000000;
    vi.stubGlobal("fetch", vi.fn(async (path: string) => new Response(JSON.stringify(
      path === "/v1/session" ? { mode: "local", local_access_expires_at: deadline }
        : { code: "local_access_expired", message: "Restart with a new key" },
    ), { status: path === "/v1/session" ? 200 : 401 })));
    await act(async () => { render(<App />); });
    expect(screen.getByText(/Access key expires:/)).toBeTruthy();
    expect(document.querySelector("time")?.dateTime).toBe(new Date(deadline * 1000).toISOString());
    fireEvent.change(screen.getByLabelText("Local access key"), { target: { value: "expired-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Open your studio" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Restart with a new key");
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("opens a fresh local browser connection without an expired-session error", async () => {
    vi.stubGlobal("fetch", vi.fn(async (path: string) => new Response(JSON.stringify(
      path === "/v1/session" ? { mode: "local" } : { code: "unauthorized", message: "Access expired" },
    ), { status: path === "/v1/session" ? 200 : 401 })));
    await act(async () => { render(<App />); });
    expect(screen.getByLabelText("Local access key")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
  it("shows a hosted budget alert at the API's admission threshold", async () => {
    mode = "hosted";
    vi.stubGlobal("fetch", vi.fn(async (path: string) => new Response(JSON.stringify(
      path === "/v1/usage" ? { estimated_spend_usd: 41, limit_usd: 50, percent_used: 82, budget_alert: "warning" }
      : path === "/v1/capabilities" ? { mode: "hosted", device: "cpu", models: [] }
      : path === "/v1/session" ? { mode: "hosted" } : [],
    ))));
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    expect((await screen.findByRole("status")).textContent).toContain("82%");
  });
  it("requires a script, sends a new idempotent take, and never autoplays", async () => {
    render(<App />);
    const generate = await screen.findByRole("button", { name: /generate voice/i });
    expect((generate as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Your voicemail script"), { target: { value: "Hello, please leave a message." } });
    expect((generate as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(generate);
    await waitFor(() => expect(captured.some(c => c.path === "/v1/generations" && c.init?.method === "POST")).toBe(true));
    const call = captured.find(c => c.path === "/v1/generations" && c.init?.method === "POST")!;
    expect(new Headers(call.init?.headers).get("Idempotency-Key")).toBeTruthy();
    expect(JSON.parse(call.init?.body as string).voice_id).toBe("voice-one");
    expect(document.querySelector("audio[autoplay]")).toBeNull();
  });
  it("keeps importing disabled until a recording and authorization are supplied", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Your voice" }));
    expect((screen.getByRole("button", { name: "Prepare reference" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
    expect(screen.getByLabelText("Reference recording")).toBeTruthy();
  });
  it("preserves an edited excerpt when audio metadata arrives later", async () => {
    URL.createObjectURL = vi.fn(() => "blob:fixture");
    URL.revokeObjectURL = vi.fn();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Your voice" }));
    fireEvent.change(screen.getByLabelText("Reference recording"), { target: { files: [new File(["audio"], "test.wav")] } });
    const end = screen.getByLabelText("End (seconds)") as HTMLInputElement;
    fireEvent.focus(end);
    fireEvent.change(end, { target: { value: "7" } });
    const audio = screen.getByLabelText("Original reference preview");
    Object.defineProperty(audio, "duration", { value: 8 });
    fireEvent.loadedMetadata(audio);
    expect(end.value).toBe("7");
  });
  it("explains an explicit hosted transfer before importing", async () => {
    mode = "hosted"; render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Your voice" }));
    expect(screen.getByText(/Importing here uploads this recording/)).toBeTruthy();
    expect(captured.filter(c => c.path === "/v1/voices" && c.init?.method === "POST")).toHaveLength(0);
  });
  it("offers a keyboard-dismissible deletion confirmation", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Your voice" }));
    const trigger = screen.getByRole("button", { name: "Delete" });
    trigger.focus();
    fireEvent.click(trigger);
    expect(screen.getByRole("alertdialog")).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Keep it" }));
    fireEvent.keyDown(screen.getByRole("alertdialog"), { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete" }));
    fireEvent.keyDown(screen.getByRole("alertdialog"), { key: "Tab" });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Keep it" }));
    fireEvent.keyDown(screen.getByRole("alertdialog"), { key: "Escape" });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
  it("keeps scripts over the word limit from being submitted", async () => {
    render(<App />);
    const generate = await screen.findByRole("button", { name: "Generate voice" });
    fireEvent.change(screen.getByLabelText("Your voicemail script"), { target: { value: Array(281).fill("word").join(" ") } });
    expect((generate as HTMLButtonElement).disabled).toBe(true);
    expect(captured.filter(c => c.init?.method === "POST")).toHaveLength(0);
  });
});
