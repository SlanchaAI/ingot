import js from "@eslint/js";
import globals from "globals";

export default [
  js.configs.recommended,
  {files: ["**/*.js"], languageOptions: {sourceType: "script", globals: globals.browser}},
  {files: ["**/*.mjs"], languageOptions: {globals: globals.node}},
];
