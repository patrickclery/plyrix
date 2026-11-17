// The plyrix project site, served at https://patrickclery.com/plyrix/ from GitHub Pages.
// Docs pages are generated from README.md's `##` sections before every build
// (scripts/readme-pages.mjs), so the site can't drift from the tested README. The header logo
// and the favicon are the approved files from assets/brand/.
import { defineConfig, passthroughImageService } from 'astro/config';
import starlight from '@astrojs/starlight';

const lead =
  'Why rely on lyrics services that track your metadata? Download the entire LRCLIB database once and host it locally.';

export default defineConfig({
  site: 'https://patrickclery.com',
  base: '/plyrix',
  image: { service: passthroughImageService() },
  integrations: [
    starlight({
      title: 'plyrix',
      description: lead,
      // The approved 04.1 lockups, copied from assets/brand/ by scripts/readme-pages.mjs.
      logo: {
        light: './src/assets/brand/lockup.svg',
        dark: './src/assets/brand/lockup-dark.svg',
        alt: 'plyrix',
        replacesTitle: true,
      },
      favicon: '/favicon.svg',
      social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/patrickclery/plyrix' }],
      sidebar: [{ label: 'Docs', items: [{ autogenerate: { directory: 'docs' } }] }],
      components: { Footer: './src/components/Footer.astro' },
      customCss: ['./src/styles/custom.css'],
    }),
  ],
});
