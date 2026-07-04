/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        background: { DEFAULT: "#0a0e17", subtle: "#0f1421", muted: "#131722" },
        surface: { DEFAULT: "#131722", raised: "#181d2b", overlay: "#1e2434" },
        border: { DEFAULT: "#2a2e39", strong: "#3a404e" },
        foreground: { DEFAULT: "#e1e5eb", muted: "#949aa8", subtle: "#5e6478" },
        primary: { DEFAULT: "#2962ff" },
        success: { DEFAULT: "#00c853" },
        danger: { DEFAULT: "#ff1744" },
        warning: { DEFAULT: "#ffc107" },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "monospace"],
      },
      animation: {
        "fade-in": "fadeIn 0.3s ease-out",
        "slide-up": "slideUp 0.4s cubic-bezier(0.16, 1, 0.3, 1)",
        shimmer: "shimmer 1.8s linear infinite",
      },
      keyframes: {
        fadeIn: { "0%": { opacity: 0 }, "100%": { opacity: 1 } },
        slideUp: { "0%": { opacity: 0, transform: "translateY(12px)" }, "100%": { opacity: 1, transform: "translateY(0)" } },
        shimmer: { "0%": { backgroundPosition: "-200% 0" }, "100%": { backgroundPosition: "200% 0" } },
      },
    },
  },
  plugins: [],
};
