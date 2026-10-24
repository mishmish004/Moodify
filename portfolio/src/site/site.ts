// Who this site belongs to, and the links that appear on every page.

export const site = {
  name: 'Amish Shah',
  handle: 'amish',
  role: 'Software Developer',
  city: 'Mumbai',
  url: 'https://mish4mish.com',
  description: 'Amish Shah builds backend systems that have to be right, and writes up every production bug.',
  email: 'amish.shah.work@gmail.com',
  github: { url: 'https://github.com/mishmish004', handle: 'mishmish004' },
  linkedin: { url: 'https://www.linkedin.com/in/amish-shah-47691b3b6', handle: 'amish-shah-47691b3b6' },
  moodify: 'https://moodify.mish4mish.com',
  // Square photo for write-up bylines, e.g. '/images/avatar.jpg' in public/. Empty hides it.
  avatar: '',
};

export type NavKey = 'serious-work' | 'dilly-dallying' | 'yapping' | 'about' | 'resume' | 'contact';

export const primaryNav: { key: NavKey; href: string; label: string }[] = [
  { key: 'serious-work', href: '/#serious-work', label: 'serious-work' },
  { key: 'dilly-dallying', href: '/#dilly-dallying', label: 'dilly-dallying' },
  { key: 'yapping', href: '/yapping', label: 'yapping' },
  { key: 'about', href: '/about', label: 'about' },
];

export const footerNav = [
  { href: '/#serious-work', label: 'serious-work' },
  { href: '/#dilly-dallying', label: 'dilly-dallying' },
  { href: '/yapping', label: 'yapping' },
  { href: '/about', label: 'about' },
  { href: '/resume', label: 'résumé' },
  { href: '/uses', label: 'uses' },
  { href: '/now', label: 'now' },
  { href: '/contact', label: 'contact' },
  { href: '/privacy', label: 'privacy' },
];
