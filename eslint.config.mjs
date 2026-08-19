import { defineConfig, globalIgnores } from "eslint/config";
import eslint from "@eslint/js";
import jsxA11y from "eslint-plugin-jsx-a11y";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

const eslintConfig = defineConfig([
  globalIgnores([
    "dist/**",
    "node_modules/**",
    ".next/**",
    ".vinext/**",
    ".wrangler/**",
    // Local agent worktrees may contain their own dependency and build trees.
    // They are not part of this application or its validation surface.
    ".claude/**",
    // The production Vite entrypoint is app/main.tsx. These retained research
    // surfaces use the older Next-compatible toolchain and are not shipped.
    "app/page.tsx",
    "app/layout.tsx",
    "app/chatgpt-auth.ts",
    "app/ground-truth.tsx",
  ]),
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  react.configs.flat.recommended,
  react.configs.flat["jsx-runtime"],
  reactHooks.configs.flat["recommended-latest"],
  jsxA11y.flatConfigs.recommended,
  {
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
        ...globals.serviceworker,
      },
    },
    settings: {
      react: {
        version: "detect",
      },
    },
  },
]);

export default eslintConfig;
