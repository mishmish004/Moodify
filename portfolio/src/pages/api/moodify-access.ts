// POST /api/moodify-access: "Want to try Moodify?" on the privacy page
import type { APIRoute } from 'astro';
import { workerDeps } from '../../lib/deps';
import { handleForm } from '../../lib/endpoint';
import { accessSpec } from '../../lib/specs';

export const prerender = false;

export const POST: APIRoute = (ctx) => handleForm(ctx.request, accessSpec, workerDeps(ctx));
