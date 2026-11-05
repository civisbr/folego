import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("filters, accessible list, search, chart, details, layers and downloads", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(page.locator("#total-excess")).not.toHaveText("—");
  await expect(
    page.getByText("Demonstração · dados ilustrativos"),
  ).toBeVisible();
  const initial = await page.locator("#total-excess").textContent();
  await page.getByLabel("Faixa etária").selectOption("60+");
  await expect(page.locator("#total-excess")).not.toHaveText(initial);
  await page.getByLabel("Estado", { exact: true }).selectOption("RO");
  await expect(page.locator("#snapshot-label")).toContainText("Rondônia");
  await page.getByLabel("Horizonte da previsão").selectOption("3");
  await expect(page.locator("#chart svg circle")).toHaveCount(3);
  await page.getByRole("button", { name: "Limpar filtros" }).click();
  await expect(page.locator("#total-excess")).toHaveText(initial);
  await page.getByRole("searchbox").fill("Sorocaba");
  await page.locator("#search-results").getByRole("button").first().click();
  await expect(page.locator("#region-subtitle")).toContainText("SOROCABA");
  await page.getByRole("button", { name: "PM2,5", exact: true }).click();
  await expect(page.locator("#pm-layer")).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await page.getByRole("button", { name: "Ver detalhes da região" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.locator("#dialog-content")).toContainText("Sorocaba");
  await page.getByRole("button", { name: "Fechar detalhes" }).click();
  await page.getByRole("button", { name: "Ver lista de regiões" }).click();
  await expect(page.locator("#regions-body tr")).toHaveCount(439);
  await page.locator("#regions-body button").first().click();
  await page
    .getByRole("navigation")
    .getByRole("link", { name: "Como funciona" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Da fumaça ao planejamento." }),
  ).toBeVisible();
  await page
    .getByRole("navigation")
    .getByRole("link", { name: "Dados abertos" })
    .click();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Exportar recorte em CSV" }).click();
  expect((await downloadPromise).suggestedFilename()).toBe(
    "folego-demonstracao.csv",
  );
  await page
    .getByRole("navigation")
    .getByRole("link", { name: "Sobre", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Mais antecedência. Mais fôlego para o SUS.",
    }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test("dashboard has no horizontal overflow and passes accessibility checks", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator("#total-excess")).not.toHaveText("—");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  const scan = await new AxeBuilder({ page }).analyze();
  expect(scan.violations).toEqual([]);
});

test("data failure is visible", async ({ page }) => {
  await page.route("**/data/forecast.json", (route) =>
    route.fulfill({ status: 503, body: "Unavailable" }),
  );
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText(
    "Não foi possível carregar",
  );
});

test("map renders the local health-region polygons", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#map")).toHaveAttribute("data-ready", "true", {
    timeout: 20000,
  });
  await expect(page.locator("#map canvas")).toBeVisible();
});
