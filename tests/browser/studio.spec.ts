import { expect, test, type Page } from "@playwright/test";
import { mkdir, readFile } from "node:fs/promises";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";

const token = "test-local-token";
const screenshots = "artifacts/browser";
const script = "Hello, you have reached my test studio. Please leave a message.";

function testTone(): Buffer {
  const sampleRate = 24_000, count = 8 * sampleRate;
  const wav = Buffer.alloc(44 + count * 2);
  wav.write("RIFF"); wav.writeUInt32LE(wav.length - 8, 4); wav.write("WAVEfmt ", 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(sampleRate, 24); wav.writeUInt32LE(sampleRate * 2, 28);
  wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write("data", 36);
  wav.writeUInt32LE(count * 2, 40);
  for (let index = 0; index < count; index++) {
    wav.writeInt16LE(Math.round(6500 * Math.sin(2 * Math.PI * 180 * index / sampleRate)
      * (0.6 + 0.4 * Math.sin(index / 1700) ** 2)), 44 + index * 2);
  }
  return wav;
}

async function connect(page: Page) {
  await page.goto("/");
  await page.getByLabel("Local access key").fill(token);
  await page.getByRole("button", { name: "Open your studio" }).click();
  await expect(page.getByRole("navigation", { name: "Workspace" })).toBeVisible();
}

async function screenshot(page: Page, name: string, project: string) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await mkdir(screenshots, { recursive: true });
  await page.screenshot({ path: join(screenshots, `${project}-${name}.png`), fullPage: true });
}

test.beforeEach(async ({ request }) => {
  const response = await request.get("/v1/voices", { headers: { Authorization: `Bearer ${token}` } });
  for (const voice of await response.json()) {
    await request.delete(`/v1/voices/${voice.id}`, { headers: { Authorization: `Bearer ${token}` } });
  }
});

test("reference → audition → approval → generation → export → history → delete", async ({ page }, testInfo) => {
  const pageErrors: string[] = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await connect(page);
  await expect(page.getByRole("button", { name: "Generate voice" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Add a reference voice" })).toBeVisible();
  await screenshot(page, "create-empty", testInfo.project.name);
  expect((await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]).analyze()).violations).toEqual([]);
  await page.getByRole("button", { name: "Add a reference voice" }).click();
  await expect(page.getByRole("button", { name: "Prepare reference" })).toBeDisabled();
  await page.getByLabel("Reference recording").setInputFiles({
    name: "authorized-synthetic-test-tone.wav", mimeType: "audio/wav", buffer: testTone(),
  });
  await page.getByLabel("Voice name").fill("Authorized test fixture");
  await page.getByLabel("End (seconds)").fill("8");
  await expect(page.getByRole("button", { name: "Prepare reference" })).toBeDisabled();
  await expect(page.getByLabel("Original reference preview")).toHaveJSProperty("paused", true);
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Prepare reference" }).click();
  const reference = page.locator(".reference-card");
  await expect(reference.getByRole("heading", { name: "Authorized test fixture" })).toBeVisible();
  await expect(reference.getByText("AWAITING APPROVAL")).toBeVisible();
  await reference.getByRole("button", { name: "Generate audition" }).click();
  const audition = reference.getByLabel("Generated speech preview");
  await expect(audition).toBeVisible();
  await expect(audition).toHaveJSProperty("paused", true);
  await expect(reference.getByRole("button", { name: "Approve this voice" })).toBeDisabled();

  // A browser media play event exercises the approval gate. No human listening
  // or voice-quality judgment is implied by this synthetic fixture.
  await audition.evaluate(async element => {
    const audio = element as HTMLAudioElement;
    audio.muted = true;
    await audio.play();
  });
  await expect(reference.getByRole("button", { name: "Approve this voice" })).toBeEnabled();
  await audition.evaluate(element => (element as HTMLAudioElement).pause());
  await reference.getByRole("button", { name: "Approve this voice" }).click();
  await expect(reference.getByText("ACTIVE VOICE")).toBeVisible();
  await screenshot(page, "reference-approved", testInfo.project.name);

  const zipEvent = page.waitForEvent("download");
  await reference.getByRole("button", { name: "Export reference" }).click();
  const referenceExport = await zipEvent;
  expect(referenceExport.suggestedFilename()).toBe("voice-reference.zip");
  const referencePath = testInfo.outputPath("reference.zip");
  await referenceExport.saveAs(referencePath);
  expect((await readFile(referencePath)).subarray(0, 2).toString()).toBe("PK");

  await page.getByRole("button", { name: "Create", exact: true }).click();
  await page.getByLabel("Your voicemail script").fill(script);
  await page.getByText("Fine-tune this take").click();
  await page.getByRole("combobox", { name: "Model", exact: true }).selectOption("original");
  await expect(page.getByLabel("Expression")).toBeVisible();
  await expect(page.getByLabel("Guidance")).toBeVisible();
  await page.getByRole("combobox", { name: "Model", exact: true }).selectOption("nano");
  await expect(page.getByLabel("Expression")).toHaveCount(0);
  await page.getByText("Fine-tune this take").click();
  await page.getByRole("button", { name: "Generate voice" }).click();
  const current = page.getByRole("region", { name: "Current take" });
  await expect(current.getByLabel("Generated speech preview")).toBeVisible();
  await expect(current.getByLabel("Generated speech preview")).toHaveJSProperty("paused", true);
  await expect(page.locator("audio[autoplay]")).toHaveCount(0);
  await screenshot(page, "generated", testInfo.project.name);
  for (const format of ["MP3", "WAV", "Phone WAV"]) {
    const event = page.waitForEvent("download");
    await current.getByRole("button", { name: format, exact: false }).filter({ hasText: new RegExp(`^↓ ${format}$`) }).click();
    const downloaded = await event;
    const path = testInfo.outputPath(downloaded.suggestedFilename());
    await downloaded.saveAs(path);
    const bytes = await readFile(path);
    expect(bytes.length).toBeGreaterThan(1000);
    if (format !== "MP3") {
      expect(bytes.subarray(0, 4).toString()).toBe("RIFF");
      expect(bytes.readUInt32LE(24)).toBe(format === "Phone WAV" ? 8000 : 24000);
    }
  }

  await page.getByRole("button", { name: "History", exact: true }).click();
  const message = page.getByRole("article").filter({ hasText: script });
  await expect(message).toBeVisible();
  await expect(page.getByRole("article")).toHaveCount(2);
  await screenshot(page, "history", testInfo.project.name);
  const deleteTake = message.getByRole("button", { name: "Delete take" });
  await deleteTake.click();
  let dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Keep it" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Delete", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Keep it" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(deleteTake).toBeFocused();
  await deleteTake.click();
  dialog = page.getByRole("alertdialog");
  await dialog.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(message).toHaveCount(0);
  await expect(page.getByRole("main")).toBeFocused();

  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByText(/extract reference.wav from the ZIP/)).toBeVisible();
  await page.getByLabel("Key name").fill("Browser test application");
  await page.getByRole("button", { name: "Create key" }).click();
  const key = page.getByLabel("Copy this key now. It is shown only once.");
  await expect(key).toHaveValue(/^vs_/);
  await page.getByRole("button", { name: "Dismiss key" }).click();
  await expect(key).toHaveCount(0);
  await page.getByRole("button", { name: "Revoke" }).click();
  await expect(page.getByRole("button", { name: "Revoke" })).toHaveCount(0);
  await screenshot(page, "settings", testInfo.project.name);

  await page.getByRole("button", { name: "Your voice", exact: true }).click();
  await page.locator(".reference-card").getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.locator(".reference-card")).toHaveCount(0);
  await page.getByRole("button", { name: "History", exact: true }).click();
  await expect(page.getByText("A quiet studio, for now.")).toBeVisible();
  expect(pageErrors).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("authentication errors and invalid audio are recoverable and keyboard navigation works", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Local access key").fill("incorrect-test-key");
  await page.getByRole("button", { name: "Open your studio" }).click();
  await expect(page.getByRole("alert")).toContainText("expired");
  await page.getByLabel("Local access key").fill(token);
  await page.getByLabel("Local access key").press("Enter");
  await expect(page.getByRole("navigation")).toBeVisible();
  await page.getByRole("button", { name: "Your voice", exact: true }).focus();
  await page.keyboard.press("Enter");
  await page.getByLabel("Reference recording").setInputFiles({
    name: "invalid.wav", mimeType: "audio/wav", buffer: Buffer.from("not an audio recording"),
  });
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Prepare reference" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByRole("button", { name: "Prepare reference" })).toBeEnabled();
  await page.getByRole("button", { name: "Dismiss error" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
