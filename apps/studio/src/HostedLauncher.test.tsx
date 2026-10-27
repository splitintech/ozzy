import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { HostedLauncher } from "./HostedLauncher";
const mocks = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("./api", () => ({ desktop: false, native: vi.fn(), request: mocks.request }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));
beforeEach(() => {
  mocks.request.mockReset();
  vi.stubGlobal("open", vi.fn(() => null));
  mocks.request.mockImplementation(async (path: string) => path === "/launcher/deployments" ? { deployments: ["my-studio"] } : path === "/launcher/operations" ? { id: "test", state: "completed", result: { url: "https://private.example/studio", state: "ready" } } : {});
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("starts a saved studio without credentials and preserves the local workspace", async () => {
  render(<HostedLauncher />);
  fireEvent.click(await screen.findByRole("button", { name: "Start hosted studio" }));
  const link = await screen.findByRole("link", { name: "Open hosted studio" });
  expect(link.getAttribute("href")).toBe("https://private.example/studio");
  const body = JSON.parse(mocks.request.mock.calls.find(([path]) => path === "/launcher/operations")![1].body);
  expect(body).toMatchObject({ action: "start", deployment: "my-studio", api_key: null, owner_password: null });
  expect(screen.getByRole("status").textContent).toContain("API ready");
  expect(screen.getByRole("status").textContent).toContain("Model loads on first generation");
});
it("shows a resumable setup failure without echoing secrets", async () => {
  mocks.request.mockImplementation(async (path: string) => path === "/launcher/deployments" ? { deployments: [] } : { id: "test", state: "failed", error: "Quota is insufficient. Retry after changing quota." });
  render(<HostedLauncher />);
  fireEvent.change(screen.getByLabelText("Daytona API key"), { target: { value: "provider-secret" } });
  fireEvent.change(screen.getByLabelText("New owner password"), { target: { value: "owner-password-secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Set up hosted studio" }));
  expect((await screen.findByRole("alert")).textContent).toContain("Quota");
  expect(screen.getByRole("alert").textContent).not.toContain("secret");
  expect((screen.getByLabelText("Daytona API key") as HTMLInputElement).value).toBe("");
  expect(screen.queryByRole("link", { name: "Open hosted studio" })).toBeNull();
});
it("distinguishes a busy shutdown from a safely stopped studio", async () => {
  mocks.request.mockImplementation(async (path: string) => path === "/launcher/deployments" ? { deployments: ["my-studio"] } : { id: "test", state: "completed", result: { reason: "Outstanding jobs must finish or be cancelled" } });
  render(<HostedLauncher />);
  fireEvent.click(screen.getByRole("button", { name: "Stop hosted studio" }));
  await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Outstanding jobs"));
  expect(screen.getByRole("status").textContent).not.toContain("stopped safely");
});
