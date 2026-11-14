import type { Config } from "tailwindcss";

export default {
  darkMode: "class",
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./dashboard/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: { 50: "#eef4ff", 400: "#7c9cff", 500: "#5b7cfa", 600: "#4361ee", 700: "#3249c7" },
      },
      fontFamily: { sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"] },
    },
  },
  plugins: [],
} satisfies Config;
