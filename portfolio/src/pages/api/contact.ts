// POST /api/contact: the "Say hello" form
import type { APIRoute } from 'astro';
import { workerDeps } from '../../lib/deps';
import { handleForm } from '../../lib/endpoint';
import { contactSpec } from '../../lib/specs';

export const prerender = false;

export const POST: APIRoute = (ctx) => handleForm(ctx.request, contactSpec, workerDeps(ctx));
