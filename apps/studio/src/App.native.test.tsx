import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const fixtures = vi.hoisted(() => ({
  mode: "local",
  native: vi.fn(),
  request: vi.fn(),
  importVoice: vi.fn(),
}));
vi.mock("./api", () => ({
  desktop: true,
  ApiError: class extends Error {},
  native: fixtures.native,
  request: fixtures.request,
  importVoice: fixtures.importVoice,
  setAccessKey: vi.fn(),
  artifactBlob: vi.fn(),
  saveArtifact: vi.fn(),
  api: {
    capabilities: async () => ({
      mode: fixtures.mode, device: "cpu",
      models: [{ id: "nano", installed: true, download_bytes: 1 }],
    }),
    voices: async () => [],
    jobs: async () => [],
  },
}));

beforeEach(() => {
  fixtures.mode = "local";
  fixtures.native.mockReset();
  fixtures.request.mockReset();
  fixtures.importVoice.mockReset();
  fixtures.native.mockImplementation(async (command: string) => {
    if (command === "cloud_status") return [{ sandbox_id: "fixture", role: "controller", state: "started" }];
    if (command === "cloud_start") fixtures.mode = "hosted";
    if (command === "check_update") return { available: true, version: "0.2.0", notes: "Test release notes." };
    if (command === "inspect_runtime") return { bytes: 300_000_000, unpacked_bytes: 800_000_000, platform: "macos-aarch64" };
    if (command === "runtime_status") return { installed: true, platform: "macos-aarch64", available_devices: ["cpu", "mps"] };
    return {};
  });
  fixtures.request.mockImplementation(async (path: string) => {
    if (path === "/v1/keys") return [];
    if (path === "/v1/usage") return { estimated_spend_usd: 0, limit_usd: 50 };
    return {};
  });
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = vi.fn(() => "blob:synthetic-reference");
    static revokeObjectURL = vi.fn();
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function openWorkspace() {
  fireEvent.click(screen.getByRole("button", { name: "Open local workspace" }));
  await screen.findByRole("navigation", { name: "Workspace" });
}

describe("Desktop workspace boundaries", () => {
  it("checks hosted state through the native credential boundary without starting a session", async () => {
    render(<App />);
    await openWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Check hosted status" }));
    expect(await screen.findByText("controller: started")).toBeTruthy();
    expect(fixtures.native).toHaveBeenCalledWith("cloud_status");
    expect(fixtures.native.mock.calls.some(([name]) => name === "cloud_start")).toBe(false);
  });
  it("offers only detected local devices and starts the owner's selection", async () => {
    render(<App />);
    const device = await screen.findByLabelText("Local processing device");
    expect((device as HTMLSelectElement).value).toBe("cpu");
    expect(screen.queryByRole("option", { name: "NVIDIA GPU (CUDA)" })).toBeNull();
    fireEvent.change(device, { target: { value: "mps" } });
    await openWorkspace();
    expect(fixtures.native).toHaveBeenCalledWith("start_local", { device: "mps" });
  });
  it("shows verified runtime size before a separate download action", async () => {
    render(<App />);
    fireEvent.click(screen.getByText("First time? Install the local runtime"));
    fireEvent.click(screen.getByText("Advanced download source"));
    fireEvent.change(screen.getByLabelText("Signed runtime manifest URL"), { target: { value: "https://example.com/runtime.json" } });
    fireEvent.click(screen.getByRole("button", { name: "Review runtime download" }));
    const download = await screen.findByRole("button", { name: "Download and verify runtime" });
    expect(screen.getByText(/300 MB download · 1.6 GB free disk space required/)).toBeTruthy();
    expect(fixtures.native.mock.calls.some(([name]) => name === "install_runtime")).toBe(false);
    fireEvent.click(download);
    await waitFor(() => expect(fixtures.native.mock.calls.some(([name]) => name === "install_runtime")).toBe(true));
  });
  it("clears a selected local reference before explicitly connecting to hosted mode", async () => {
    render(<App />);
    await openWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Your voice" }));
    fireEvent.change(screen.getByLabelText("Reference recording"), {
      target: { files: [new File(["test"], "private-local-reference.wav", { type: "audio/wav" })] },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    expect((screen.getByRole("button", { name: "Prepare reference" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Switch workspace" }));
    await screen.findByRole("button", { name: "Open local workspace" });
    fireEvent.click(screen.getByText("Advanced: external PostgreSQL/S3 deployment"));
    fireEvent.change(screen.getByLabelText("Controller snapshot"), { target: { value: "test-controller" } });
    fireEvent.click(screen.getByRole("button", { name: "Start hosted workspace" }));
    const password = await screen.findByLabelText("Owner password");
    fireEvent.change(password, { target: { value: "test-only-owner-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Open your studio" }));
    fireEvent.click(await screen.findByRole("button", { name: "Your voice" }));
    expect(screen.getByText("Choose your recording")).toBeTruthy();
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText(/Importing here uploads this recording/)).toBeTruthy();
    expect(fixtures.importVoice).not.toHaveBeenCalled();
    expect(fixtures.native).toHaveBeenCalledWith("disconnect");
  });

  it("checks an update, then requires an explicit install action", async () => {
    render(<App />);
    await openWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Check for updates" }));
    const install = await screen.findByRole("button", { name: "Install 0.2.0 and restart" });
    expect(fixtures.native.mock.calls.some(([name]) => name === "install_update")).toBe(false);
    expect(screen.getByText("Test release notes.")).toBeTruthy();
    fireEvent.click(install);
    await waitFor(() => expect(fixtures.native.mock.calls.some(([name]) => name === "install_update")).toBe(true));
  });

  it("shows native update failures without losing the workspace", async () => {
    fixtures.native.mockImplementation(async (command: string) => {
      if (command === "check_update") throw new Error("This preview does not have an update feed.");
      return {};
    });
    render(<App />);
    await openWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Check for updates" }));
    expect((await screen.findByRole("alert")).textContent).toContain("does not have an update feed");
    expect(screen.getByRole("navigation")).toBeTruthy();
  });
});
