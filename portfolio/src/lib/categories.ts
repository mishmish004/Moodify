// Write-up categories, in the order the filter shows them
export const CATEGORIES = ['databases', 'deploys', 'networking', 'performance', 'infra'] as const;
export type Category = (typeof CATEGORIES)[number];
