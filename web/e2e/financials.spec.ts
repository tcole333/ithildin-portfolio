import { expect, test } from '@playwright/test';

function parseCount(text: string | null): number {
  const match = text?.match(/\((\d+)\)/);
  return match ? Number(match[1]) : -1;
}

test('financial workbench supports orient focus verify flow', async ({ page }) => {
  await page.goto('/financials');

  const workbench = page.getByTestId('financial-workbench');
  await expect(workbench).toBeVisible();

  const corridorCountLabel = page.getByTestId('primary-corridors-count');
  await expect(corridorCountLabel).toBeVisible();

  const baselineCount = parseCount(await corridorCountLabel.textContent());
  expect(baselineCount).toBeGreaterThan(0);

  await expect(workbench.locator('a[href^="https://jmail.world/thread/EFTA"]').first()).toBeVisible();

  await page.getByRole('button', { name: '5.0M+' }).click();
  const narrowedCount = parseCount(await corridorCountLabel.textContent());
  expect(narrowedCount).toBeGreaterThan(0);
  expect(narrowedCount).toBeLessThanOrEqual(baselineCount);

  await workbench.getByRole('button', { name: /^focus .+/ }).first().click();
  await expect(workbench).toHaveAttribute('data-focus-entity', /^(?!ALL$).+/);
  await expect(workbench.getByText('Entity Lens')).toBeVisible();
});

test('entity heatmap focus button does not crash', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', error => {
    pageErrors.push(error.message);
  });

  await page.goto('/financials');
  const workbench = page.getByTestId('financial-workbench');
  await expect(workbench).toBeVisible();

  await expect(workbench.getByText('Entity Heatmap')).toBeVisible();
  await expect(workbench).toHaveAttribute('data-focus-entity', 'ALL');
  await workbench.getByTestId('heatmap-focus-button').first().click();

  await expect(workbench).toHaveAttribute('data-focus-entity', /^(?!ALL$).+/);
  await expect(workbench.getByText('Entity Lens')).toBeVisible();
  expect(pageErrors, pageErrors.join('\n')).toHaveLength(0);
});
