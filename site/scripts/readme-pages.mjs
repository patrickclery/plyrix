// Split README.md's `##` sections into one Starlight docs page each, at build time.
//
// Everything before the first `##` is the README's header and lead, which the splash page covers.
// `###` and deeper headings move up one level, repo-relative links point at the file on GitHub,
// and an in-README `#anchor` points at the page that holds the heading. The pages are written to
// src/content/docs/docs/, which is gitignored and cleared first, as `.md`, so braces and angle
// brackets stay literal.
//
// It also copies the two approved lockups from assets/brand/ into src/assets/brand/, which is
// gitignored too, so the only tracked copies stay in assets/brand/.
import { copyFileSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const README = new URL('../../README.md', import.meta.url);
const OUT = new URL('../src/content/docs/docs/', import.meta.url);
const REPO_BLOB = 'https://github.com/patrickclery/plyrix/blob/main/';
const BASE = '/plyrix/docs/';
const BRAND_SRC = new URL('../../assets/brand/', import.meta.url);
const BRAND_OUT = new URL('../src/assets/brand/', import.meta.url);
export const BRAND_FILES = ['lockup.svg', 'lockup-dark.svg'];

// Copy the lockups byte for byte into out, a path or file URL, replacing what was there.
export function copyBrand(out = BRAND_OUT) {
  const dir = typeof out === 'string' ? out : fileURLToPath(out);
  rmSync(dir, { recursive: true, force: true });
  mkdirSync(dir, { recursive: true });
  for (const name of BRAND_FILES) {
    copyFileSync(new URL(name, BRAND_SRC), `${dir}/${name}`);
  }
}

export function slugify(heading) {
  return heading
    .toLowerCase()
    .replace(/[`*_]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

function isFence(line) {
  return /^\s*(```|~~~)/.test(line);
}

// Walk the lines once, outside fenced code, and hand each heading to visit.
function scanHeadings(lines, visit) {
  let fenced = false;
  lines.forEach((line, index) => {
    if (isFence(line)) {
      fenced = !fenced;
      return;
    }
    if (fenced) return;
    const match = /^(#{2,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (match) visit(match[1].length, match[2], index);
  });
}

function rewriteLinks(text, anchors) {
  // [text](target) and ![alt](target), leaving code spans alone.
  return text
    .split(/(`[^`]*`)/)
    .map((part, index) => {
      if (index % 2 === 1) return part;
      return part.replace(/(!?\[[^\]]*\])\(([^)\s]+)((?:\s+"[^"]*")?)\)/g, (whole, label, target, title) => {
        if (/^(https?:|mailto:)/i.test(target)) return whole;
        if (target.startsWith('#')) {
          const page = anchors.get(target.slice(1));
          return page ? `${label}(${BASE}${page}/${target})${title}` : whole;
        }
        const path = target.replace(/^\.\//, '');
        return `${label}(${REPO_BLOB}${path})${title}`;
      });
    })
    .join('');
}

export function splitReadme(text) {
  const lines = text.split('\n');
  const sections = [];
  scanHeadings(lines, (level, title, index) => {
    if (level === 2) sections.push({ title, start: index });
  });

  // Which page holds each heading's anchor, as GitHub would generate it.
  const anchors = new Map();
  sections.forEach((section, i) => {
    const end = i + 1 < sections.length ? sections[i + 1].start : lines.length;
    const slug = slugify(section.title);
    anchors.set(slug, slug);
    scanHeadings(lines.slice(section.start, end), (level, title) => {
      if (level > 2) anchors.set(slugify(title), slug);
    });
  });

  return sections.map((section, i) => {
    const end = i + 1 < sections.length ? sections[i + 1].start : lines.length;
    let fenced = false;
    const body = lines
      .slice(section.start + 1, end)
      .map((line) => {
        if (isFence(line)) {
          fenced = !fenced;
          return line;
        }
        if (fenced) return line;
        const demoted = line.replace(/^(#{3,6})(\s)/, (_, hashes, space) => hashes.slice(1) + space);
        return rewriteLinks(demoted, anchors);
      })
      .join('\n')
      .trim();
    return { slug: slugify(section.title), title: section.title, order: i + 1, body };
  });
}

function frontmatter(page) {
  return `---\ntitle: ${JSON.stringify(page.title)}\nsidebar:\n  order: ${page.order}\n---\n\n`;
}

export function writePages(pages, out = OUT) {
  rmSync(out, { recursive: true, force: true });
  mkdirSync(out, { recursive: true });
  for (const page of pages) {
    writeFileSync(new URL(`${page.slug}.md`, out), `${frontmatter(page)}${page.body}\n`);
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  copyBrand();
  const pages = splitReadme(readFileSync(README, 'utf8'));
  writePages(pages);
  console.log(`readme-pages: wrote ${pages.length} pages`);
}
