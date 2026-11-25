import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("study page shows pooled results from real data", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const study = await (await page.request.get("/data/study.json")).json();
  const strata = study.strata.length;
  await page.goto("/#estudo");
  await expect(
    page.getByRole("heading", { name: "PM2,5 e internações." }),
  ).toBeVisible();
  await expect(page.locator("#study-scope")).toContainText(
    `${study.regions} regiões`,
  );
  await expect(page.locator(".study-headline strong").first()).toHaveText(
    /^\+\d,\d%$/,
  );
  await expect(page.locator(".study-scroll svg rect")).toHaveCount(strata);
  await expect(page.locator(".study-table tbody tr")).toHaveCount(strata);
  await expect(page.locator(".study-shift-row")).toHaveCount(6);
  await expect(page).toHaveTitle("Estudo PM2,5 · Fôlego");
  await page
    .getByRole("navigation")
    .getByRole("link", { name: "Painel" })
    .click();
  await expect(page.locator("#dashboard")).toBeVisible();
  await page
    .getByRole("navigation")
    .getByRole("link", { name: "Estudo" })
    .click();
  await expect(page.locator(".study-table tbody tr")).toHaveCount(strata);
  const axe = await new AxeBuilder({ page }).include("#content-page").analyze();
  expect(axe.violations).toEqual([]);
  expect(errors).toEqual([]);
});
