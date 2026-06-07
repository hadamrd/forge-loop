import type { Config } from "tailwindcss";

// Tailwind is available for new work, but the operator-console design system lives in
// src/index.css (CSS variables + semantic classes ported verbatim from the design spec).
// Preflight is OFF so Tailwind's reset never fights the hand-tuned base styles.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  corePlugins: { preflight: false },
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        panel: "var(--panel)",
        elevated: "var(--elevated)",
        raised: "var(--raised)",
        border: "var(--border)",
        hi: "var(--hi)",
        mid: "var(--mid)",
        dim: "var(--dim)",
        faint: "var(--faint)",
        accent: "var(--accent)",
        emerald: "var(--emerald)",
        amber: "var(--amber)",
        red: "var(--red)",
        violet: "var(--violet)",
        blue: "var(--blue)",
      },
      fontFamily: {
        sans: ["var(--sans)"],
        mono: ["var(--mono)"],
      },
    },
  },
  plugins: [],
} satisfies Config;
