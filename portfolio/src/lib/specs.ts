// The two forms: which table each one fills, and what the email copy says.
import type { FormSpec } from './endpoint.ts';
import { validateAccess, validateContact, type AccessInput, type ContactInput } from './forms.ts';

export const contactSpec: FormSpec<ContactInput> = {
  table: 'contact_messages',
  validate: validateContact,
  toRow: (v) => ({ topic: v.topic, name: v.name, email: v.email, message: v.message }),
  email: (v) => ({
    subject: `mish4mish.com: ${v.topic ?? 'a message'} from ${v.name}`,
    replyTo: v.email,
    text: `${v.name} <${v.email}>${v.topic ? `\nTopic: ${v.topic}` : ''}\n\n${v.message}\n`,
  }),
};

export const accessSpec: FormSpec<AccessInput> = {
  table: 'moodify_access_requests',
  validate: validateAccess,
  toRow: (v) => ({ name: v.name, email: v.email, note: v.note }),
  email: (v) => ({
    subject: `Moodify access request: ${v.email}`,
    replyTo: v.email,
    text: `Spotify email: ${v.email}\nName: ${v.name ?? '(not given)'}\nHow they know you: ${v.note ?? '(not given)'}\n\nAdd them in the Spotify developer dashboard (User Management), then email them back.\n`,
  }),
};
