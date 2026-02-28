"use client";

import * as React from "react";
import { useTheme } from "next-themes";

export function ThemeSwitchButton() {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = React.useState(false);

  React.useEffect(() => setMounted(true), []);

  if (!mounted) {
    return null;
  }

  const isDark = resolvedTheme === "dark";

  return (
    <button
      type="button"
      role="switch"
      aria-checked={isDark}
      onClick={() => setTheme(isDark ? "light" : "dark")}
      className={`theme-toggle ${isDark ? "is-dark" : "is-light"}`}
      aria-label={`Switch to ${isDark ? "light" : "dark"} mode`}
    >
      <span className={`thumb ${isDark ? "right" : "left"}`} />
    </button>
  );
}
