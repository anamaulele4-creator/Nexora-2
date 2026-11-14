"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { useUi } from "./Providers";

export function Logo() {
  return (
    <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
      <span className="grid h-8 w-8 place-items-center rounded-lg bg-brand-600 text-sm font-bold text-white">C</span>
      <span>CONNECTOR AI</span>
    </Link>
  );
}

export function Header({ children }: { children?: ReactNode }) {
  const { t, theme, toggleTheme, lang, setLang } = useUi();
  return (
    <header className="border-b border-slate-200 dark:border-slate-800">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-4">
        <Logo />
        <div className="flex items-center gap-2">
          {children}
          <button
            type="button"
            className="btn-ghost"
            aria-label={t("lang.toggle")}
            onClick={() => setLang(lang === "en" ? "pt" : "en")}
          >
            {lang === "en" ? "PT" : "EN"}
          </button>
          <button type="button" className="btn-ghost" aria-label={t("theme.toggle")} onClick={toggleTheme}>
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </div>
      </div>
    </header>
  );
}
