import { expect, test } from '@playwright/test';

test('portfolio demo surfaces load and expose source records', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Ithildin' })).toBeVisible();
  await expect(page.getByText('Investigative Infrastructure')).toBeVisible();

  await page.goto('/articles/port-watch-procurement-network');
  await expect(page.getByRole('heading', { name: 'The Port Watch Procurement Network' })).toBeVisible();
  const sourceRecordLink = page.locator('[data-citation-record]').first();
  await expect(sourceRecordLink).toBeVisible();
  await sourceRecordLink.click();

  await expect(page).toHaveURL(/\/sources\//);
  await expect(page.getByText('Source Record').first()).toBeVisible();
  await expect(page.getByText(/metadata only|hosted copy|public artifact/i).first()).toBeVisible();

  await page.goto('/financials');
  await expect(page.getByRole('heading', { name: 'Procurement Corridor', exact: true })).toBeVisible();
});
