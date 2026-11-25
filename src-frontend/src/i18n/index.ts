import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import zh from "./locales/zh.json";
import en from "./locales/en.json";
import ja from "./locales/ja.json";
import ko from "./locales/ko.json";

const saved = (() => {
  try {
    return localStorage.getItem("yuncat-lang");
  } catch {
    return null;
  }
})();

i18n.use(initReactI18next).init({
  resources: { zh: { translation: zh }, en: { translation: en }, ja: { translation: ja }, ko: { translation: ko } },
  lng: saved || "zh",
  fallbackLng: "en",
  interpolation: { escapeValue: false },
});

export const LANGUAGES = [
  { code: "zh", label: "中文" },
  { code: "en", label: "English" },
  { code: "ja", label: "日本語" },
  { code: "ko", label: "한국어" },
] as const;

export function useLanguage() {
  const current = i18n.language;
  const setLanguage = (lang: string) => {
    i18n.changeLanguage(lang);
    try {
      localStorage.setItem("yuncat-lang", lang);
    } catch {
      // no-op
    }
  };
  return { current, setLanguage, languages: LANGUAGES };
}

export default i18n;
