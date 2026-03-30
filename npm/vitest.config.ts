import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
    globals: false,
    isolate: true,
    // Each test file gets fresh module state
    pool: 'forks',
    testTimeout: 10_000,
  },
  resolve: {
    // Allow importing .js extensions that resolve to .ts source
    extensionAlias: {
      '.js': ['.ts', '.js'],
    },
  },
});
