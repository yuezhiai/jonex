// @ts-check

import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactPlugin from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import prettierConfig from 'eslint-config-prettier';

export default tseslint.config(
  // Global ignore
  {
    ignores: [
      '**/node_modules/**',
      '**/dist/**',
      '**/build/**',
      '**/*.js',
      '**/*.mjs',
      '**/*.cjs',
    ],
  },

  // Base recommended rules
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    plugins: {
      react: reactPlugin,
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    settings: {
      react: { version: 'detect' },
    },
    rules: {
      // ─── React ───
      'react/jsx-uses-react': 'off', // automatic JSX runtime
      'react/react-in-jsx-scope': 'off', // automatic JSX runtime
      'react/jsx-uses-vars': 'error',
      'react/jsx-no-target-blank': 'error',

      // ─── React Hooks ───
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',

      // ─── React Refresh ───
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],

      // ─── TypeScript ───
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      '@typescript-eslint/ban-ts-comment': 'warn',
      '@typescript-eslint/no-require-imports': 'error',

      // ─── General ───
      'no-console': ['warn', { allow: ['warn', 'error', 'info'] }],
      'no-debugger': 'warn',
    },
  },

  // eslint-config-prettier：关闭所有与 Prettier 冲突的 ESLint 规则
  prettierConfig,
);
