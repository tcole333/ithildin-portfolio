import { expect, test } from '@playwright/test';

test('evidence mode toggles and drives bidirectional highlighting on dossier prose', async ({ page }) => {
  await page.goto('/dossiers/darren-indyke');

  const root = page.locator('[data-evidence-page]').first();
  const toggle = root.locator('[data-evidence-mode-toggle]').first();
  if (await toggle.count() === 0) {
    test.skip(true, 'Evidence mode feature flag is disabled.');
  }
  await expect(toggle).toBeVisible();
  await expect(root).not.toHaveClass(/evidence-mode--enabled/);

  await toggle.check();
  await expect(root).toHaveClass(/evidence-mode--enabled/);

  const unsupportedSpan = root.locator('.support-span--unsupported').first();
  await expect(unsupportedSpan).toBeVisible();

  const supportedSpan = root.locator('.support-span--supported').first();
  await supportedSpan.click();
  await expect(root.locator('.support-span--active').first()).toBeVisible();
  await expect(root.locator('.citation--active').first()).toBeVisible();

  const sourceLink = root.locator('[data-source-key]').first();
  await expect(sourceLink).toBeVisible();
  await sourceLink.scrollIntoViewIfNeeded();
  await sourceLink.click();

  await expect(root.locator('[data-source-key].citation--active').first()).toBeVisible();
  await expect(root.locator('.support-span--active').first()).toBeVisible();
});
