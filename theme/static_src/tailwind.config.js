/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    // Templates
    '../../templates/**/*.html',
    '../../**/templates/**/*.html',
    // Theme
    './src/**/*.css',
  ],
  theme: {
    extend: {
      colors: {
        // White-label overridable via CSS custom properties
        brand: {
          primary: 'var(--brand-primary, #657329)',
          'primary-hover': 'var(--brand-primary-hover, #525f20)',
          secondary: 'var(--brand-secondary, #0e4a56)',
        },
      },
    },
  },
  plugins: [],
}
