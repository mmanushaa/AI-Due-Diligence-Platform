import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type ThemeChoice = "light" | "dark" | "system";
const KEY = "dd.theme";
const ORDER: ThemeChoice[] = ["system", "light", "dark"];

interface ThemeValue {
  choice: ThemeChoice;
  resolved: "light" | "dark";
  cycle: () => void;
}

const ThemeContext = createContext<ThemeValue | null>(null);

function systemPrefersDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function readChoice(): ThemeChoice {
  const stored = window.localStorage.getItem(KEY);
  return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
}

export function applyTheme(choice: ThemeChoice): "light" | "dark" {
  const resolved = choice === "system" ? (systemPrefersDark() ? "dark" : "light") : choice;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.colorScheme = resolved;
  return resolved;
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoice] = useState<ThemeChoice>(readChoice);
  const [resolved, setResolved] = useState<"light" | "dark">(() => applyTheme(readChoice()));

  useEffect(() => {
    setResolved(applyTheme(choice));
    window.localStorage.setItem(KEY, choice);
  }, [choice]);

  useEffect(() => {
    if (choice !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const listener = () => setResolved(applyTheme("system"));
    media.addEventListener("change", listener);
    return () => media.removeEventListener("change", listener);
  }, [choice]);

  const cycle = useCallback(() => {
    setChoice((current) => ORDER[(ORDER.indexOf(current) + 1) % ORDER.length]);
  }, []);

  const value = useMemo(() => ({ choice, resolved, cycle }), [choice, resolved, cycle]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
