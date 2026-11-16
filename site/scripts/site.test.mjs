// node --test: the README splitter's rules, and checks on the built site in dist/.
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { BRAND_FILES, copyBrand, splitReadme } from './readme-pages.mjs';

const SITE = fileURLToPath(new URL('..', import.meta.url));
const DIST = join(SITE, 'dist');
const README = readFileSync(join(SITE, '..', 'README.md'), 'utf8');
const BRAND = join(SITE, '..', 'assets', 'brand');
const LEAD =
  'plyrix: time-synced lyrics for your Plex music library, from your own LRCLIB database, without touching an audio file.';
const DISCLAIMER = 'plyrix is an independent project, not affiliated with or endorsed by Plex, Inc.';
// The same pattern as tests/test_lyric_leak.py's TIMESTAMPED_LINE.
const TIMESTAMPED_LINE = /^\s*\[\d{1,2}:\d{2}([.:]\d{1,3})?\]/;

const SAMPLE = [
  '# title',
  'lead before any section',
  '## First one',
  'see [the license](LICENSE) and [docs](./docs/a.md#x)',
  'see [web](https://example.invalid/a) and [mail](mailto:a@example.invalid)',
  'jump to [the later part](#a-later-part)',
  '```sh',
  '## not a heading',
  '```',
  '### Sub part',
  '## Second',
  '#### A later part',
  'text',
].join('\n');

test('content before the first ## is dropped', () => {
  const pages = splitReadme(SAMPLE);
  assert.equal(pages.length, 2);
  assert.ok(!pages.some((p) => p.body.includes('lead before any section')));
});

test('a ## inside a fenced block does not split', () => {
  const [first] = splitReadme(SAMPLE);
  assert.ok(first.body.includes('## not a heading'));
});

test('### moves up one level, and deeper headings too', () => {
  const [first, second] = splitReadme(SAMPLE);
  assert.match(first.body, /^## Sub part$/m);
  assert.match(second.body, /^### A later part$/m);
});

test('a repo-relative link becomes a GitHub blob URL', () => {
  const [first] = splitReadme(SAMPLE);
  assert.ok(first.body.includes('[the license](https://github.com/patrickclery/plyrix/blob/main/LICENSE)'));
  assert.ok(first.body.includes('[docs](https://github.com/patrickclery/plyrix/blob/main/docs/a.md#x)'));
});

test('http and mailto links are unchanged', () => {
  const [first] = splitReadme(SAMPLE);
  assert.ok(first.body.includes('[web](https://example.invalid/a)'));
  assert.ok(first.body.includes('[mail](mailto:a@example.invalid)'));
});

test("an anchor to another section's heading points at that page", () => {
  const [first] = splitReadme(SAMPLE);
  assert.ok(first.body.includes('[the later part](/plyrix/docs/second/#a-later-part)'));
});

test("the page count equals the README's ## headings outside code", () => {
  let fenced = false;
  let count = 0;
  for (const line of README.split('\n')) {
    if (/^\s*(```|~~~)/.test(line)) fenced = !fenced;
    else if (!fenced && /^## /.test(line)) count += 1;
  }
  const pages = splitReadme(README);
  assert.equal(pages.length, count);
  assert.deepEqual(
    pages.map((p) => p.order),
    pages.map((_, i) => i + 1),
  );
  assert.ok(pages.some((p) => p.slug === 'install'));
  assert.ok(pages.some((p) => p.slug === 'lyrics-and-legal'));
  assert.ok(!README.toLowerCase().includes('takedown'));
});

test('the favicon is byte-identical to assets/brand/mark.svg', () => {
  assert.ok(readFileSync(join(SITE, 'public', 'favicon.svg')).equals(readFileSync(join(BRAND, 'mark.svg'))));
});

test('copyBrand copies both lockups byte for byte, and nothing else', async () => {
  const { mkdtempSync, rmSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const out = mkdtempSync(join(process.env.TMPDIR ?? tmpdir(), 'brand-'));
  try {
    copyBrand(join(out, 'brand'));
    assert.deepEqual(readdirSync(join(out, 'brand')).sort(), ['lockup-dark.svg', 'lockup.svg']);
    assert.deepEqual([...BRAND_FILES].sort(), ['lockup-dark.svg', 'lockup.svg']);
    for (const name of BRAND_FILES) {
      assert.ok(readFileSync(join(out, 'brand', name)).equals(readFileSync(join(BRAND, name))), name);
    }
  } finally {
    rmSync(out, { recursive: true, force: true });
  }
});

// Built-site checks. They need `npm run build` first.

test('the header shows the lockup for light and dark, with alt "plyrix", and no text title', () => {
  const index = readFileSync(join(DIST, 'index.html'), 'utf8');
  const header = /<a[^>]*class="site-title[^"]*"[^>]*>([\s\S]*?)<\/a>/.exec(index);
  assert.ok(header, 'no site-title link in the header');
  const images = [...header[1].matchAll(/<img\b[^>]*>/g)].map((m) => m[0]);
  assert.equal(images.length, 2, header[1]);
  for (const img of images) assert.match(img, /\salt="plyrix"/);
  assert.match(header[1], /sr-only/);
  const built = images.map((img) => readFileSync(join(DIST, /src="\/plyrix\/([^"]+)"/.exec(img)[1])));
  const wanted = BRAND_FILES.map((name) => readFileSync(join(BRAND, name)));
  for (const want of wanted) assert.ok(built.some((b) => b.equals(want)), 'a lockup is not byte-identical');
});

test('the built favicon is byte-identical to assets/brand/mark.svg', () => {
  assert.ok(readFileSync(join(DIST, 'favicon.svg')).equals(readFileSync(join(BRAND, 'mark.svg'))));
});


function htmlFiles(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return htmlFiles(path);
    return name.endsWith('.html') ? [path] : [];
  });
}

function pages() {
  assert.ok(existsSync(DIST), 'dist/ is missing: run `npm run build` first');
  const files = htmlFiles(DIST);
  assert.ok(files.length > 0);
  return files.map((file) => [file, readFileSync(file, 'utf8')]);
}

function text(html) {
  return html
    .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, ' ')
    .replace(/<\/?(p|div|li|tr|h[1-6]|br|section|header|footer|pre|table)\b[^>]*>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#39;|&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/\s+\n/g, '\n');
}

function resolves(link) {
  const path = decodeURIComponent(link.split(/[?#]/)[0]).replace(/^\/plyrix\/?/, '');
  const target = join(DIST, path);
  if (existsSync(target) && statSync(target).isFile()) return true;
  return existsSync(join(target, 'index.html'));
}

test('every root-relative href and src is under /plyrix/ and resolves in dist', () => {
  const problems = [];
  for (const [file, html] of pages()) {
    for (const match of html.matchAll(/\s(?:href|src)="([^"]+)"/g)) {
      const link = match[1];
      if (!link.startsWith('/') || link.startsWith('//')) continue;
      if (!link.startsWith('/plyrix/')) problems.push(`${file}: ${link} is outside /plyrix/`);
      else if (!resolves(link)) problems.push(`${file}: ${link} does not resolve`);
    }
  }
  assert.deepEqual(problems, []);
});

test('the splash page holds the lead and the disclaimer', () => {
  const index = readFileSync(join(DIST, 'index.html'), 'utf8');
  assert.ok(text(index).includes(LEAD));
  assert.ok(text(index).includes(DISCLAIMER));
});

test('every page holds the disclaimer and the LRCLIB credit', () => {
  for (const [file, html] of pages()) {
    if (file.endsWith('404.html')) continue;
    assert.ok(text(html).includes(DISCLAIMER), file);
    assert.ok(html.includes('https://github.com/tranxuanthang/lrclib'), file);
  }
});

test('no page holds a timestamped lyric line', () => {
  for (const [file, html] of pages()) {
    const lines = text(html).split('\n').filter((line) => TIMESTAMPED_LINE.test(line));
    assert.deepEqual(lines, [], file);
  }
});
