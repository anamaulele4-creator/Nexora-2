"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import { isLang, translate, type Lang, type MessageKey } from "@/lib/i18n.ts";

type Theme = "dark" | "light";
type Ctx = {
  lang: Lang;
  setLang: (l: Lang) => void;
  theme: Theme;
  toggleTheme: () => void;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
};

const UiContext = createContext<Ctx | null>(null);

export function Providers({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>("en");
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    const storedLang = localStorage.getItem("ca_lang");
    if (isLang(storedLang)) setLangState(storedLang);
    else if (navigator.language?.toLowerCase().startsWith("pt")) setLangState("pt");
    setTheme(document.documentElement.classList.contains("dark") ? "dark" : "light");
  }, []);

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);

  const setLang = useCallback((l: Lang) => {
    localStorage.setItem("ca_lang", l);
    setLangState(l);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next: Theme = prev === "dark" ? "light" : "dark";
      document.documentElement.classList.toggle("dark", next === "dark");
      localStorage.setItem("ca_theme", next);
      return next;
    });
  }, []);

  const t = useCallback((key: MessageKey, vars?: Record<string, string | number>) => translate(lang, key, vars), [lang]);

  return <UiContext.Provider value={{ lang, setLang, theme, toggleTheme, t }}>{children}</UiContext.Provider>;
}

export function useUi(): Ctx {
  const ctx = useContext(UiContext);
  if (!ctx) throw new Error("useUi fora de <Providers>");
  return ctx;
}
