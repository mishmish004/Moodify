// Read the collections the way every page wants them: drafts hidden in production, newest first.
import { getCollection, type CollectionEntry } from 'astro:content';
import { PUBLIC_SHOW_DRAFTS } from 'astro:env/client';

export type Post = CollectionEntry<'posts'>;
export type Project = CollectionEntry<'projects'>;
export type Song = CollectionEntry<'songs'>;

const visible = (e: { data: { draft: boolean } }) => import.meta.env.DEV || PUBLIC_SHOW_DRAFTS || !e.data.draft;

/** Incident write-ups for the log, newest first */
export async function incidentPosts(): Promise<Post[]> {
  const all = await getCollection('posts', (p) => visible(p) && p.data.kind === 'incident');
  return all.sort((a, b) => b.data.date.valueOf() - a.data.date.valueOf());
}

/** Posts listed only on the secret page, newest first */
export async function offTheRecordPosts(): Promise<Post[]> {
  const all = await getCollection('posts', (p) => visible(p) && p.data.kind === 'off-the-record');
  return all.sort((a, b) => b.data.date.valueOf() - a.data.date.valueOf());
}

export async function allPosts(): Promise<Post[]> {
  return [...(await incidentPosts()), ...(await offTheRecordPosts())];
}

export async function latestPost(): Promise<Post | undefined> {
  return (await incidentPosts())[0];
}

export async function projects(): Promise<Project[]> {
  const all = await getCollection('projects', visible);
  return all.sort((a, b) => a.data.order - b.data.order);
}

/** A project gets a case study page only when its file has a body */
export const hasCaseStudy = (p: Project) => Boolean(p.body?.trim());

export async function songs(): Promise<Song[]> {
  const all = await getCollection('songs', visible);
  return all.sort((a, b) => a.data.rank - b.data.rank);
}

export function readMinutes(markdown: string | undefined): number {
  const words = (markdown ?? '').trim().split(/\s+/).filter(Boolean).length;
  return Math.max(1, Math.round(words / 220));
}

const monthYear = new Intl.DateTimeFormat('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' });
const fullDate = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
export const formatMonth = (d: Date) => monthYear.format(d);
export const formatDate = (d: Date) => fullDate.format(d);

/** 1 -> ०१, 12 -> १२: Devanagari digits, at least two places */
export function hindiNumber(n: number): string {
  const digits = '०१२३४५६७८९';
  return String(n).padStart(2, '0').replace(/[0-9]/g, (d) => digits[Number(d)]);
}

export function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
