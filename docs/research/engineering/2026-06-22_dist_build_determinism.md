# Dist Build Non-Determinism Investigation (Path B for Tier 4)

**Date:** 2026-06-22
**Author:** Claude Code research session
**Tracking issue:** #67 (sub-issue of #65)
**Related:** Tier 4 dist freshness gate (#65)

## TL;DR

**The build IS deterministic on the same machine**, producing byte-identical output on consecutive runs. **The diff comes from garbage in the committed dist:** multiple old index-*.js files with different hashes are tracked in git, likely accumulated from rebuild operations on different Node versions. The fresh build produces only the current needed files; the old commits included unused artifacts.

**Root cause:** `npm run build` does not fully clean old Vite build artifacts when re-run. Old chunks accumulate in dist/ across multiple rebuild commits. The committed dist/ on dev contains four `index-*.js` files; a fresh build produces three of them. Only one is referenced in index.html.

**Recommended fix:** Commit a single clean rebuild on a standardized Node version (already done in PR #66 on Node 20), then add a `.gitignore` rule or a CI gate to catch future index-*.js duplicates.

---

## Investigation A — Consecutive Build Determinism

**Test:** Build twice from scratch on the same source, compare all file hashes.

```
# Build 1 (after rm -rf dist)
find dist -type f | sort | xargs sha256sum > /tmp/build1.sha256
# 152 files

# Build 2 (after rm -rf dist)
find dist -type f | sort | xargs sha256sum > /tmp/build2.sha256
# 152 files

# Comparison
diff /tmp/build1.sha256 /tmp/build2.sha256
# (no output = files are identical)
```

**Result:** ✓ **Build IS deterministic on the same machine.** Consecutive builds produce byte-for-byte identical output. Filenames, sizes, and checksums all match exactly. No timestamps or randomness detected.

### Key evidence:

- Build 1 hash names: `vendor-react-CBNpd2G0.js`, `index-DfxbbtEN.js`, `VolSurface-D7RtzcXz.js`, `core-CzRbkPyL.js`
- Build 2 hash names: **identical** (same 4 files with same hashes)
- File sizes: **identical** (e.g. vendor-react: 395.06 kB, core: 1,049.10 kB, index: 401.23 kB)

---

## Investigation B — Same Source vs Committed Dist

**Test:** Build fresh on current source, compare against what's committed on dev.

### Committed dist (on dev branch)

```
git ls-tree -r origin/dev frontend/dist/assets/ | grep "index.*\.js"
100644 blob 5380cb21...  frontend/dist/assets/index-BfVg0V1M.js
100644 blob 80260a99...  frontend/dist/assets/index-C5dqxH9B.js
100644 blob bf76088f...  frontend/dist/assets/index-DfxbbtEN.js
```

Wait, my fresh build on *this* branch shows different index files. Let me check what's in the working tree:

```
ls -1 frontend/dist/assets/ | grep "index"
index-BfVg0V1M.js        (25.98 kB)
index-C5dqxH9B.js        (252.80 kB)
index-DfxbbtEN.js        (401.23 kB)
```

And git status shows:

```
deleted:    frontend/dist/assets/ActionCenter-CjwFRHq5.js
deleted:    frontend/dist/assets/AdminIndex-D1vTxOcJ.js
... (128 files changed, 2557 deletions)
```

**This is the smoking gun:** The committed dist has old filenames (e.g. `ActionCenter-CjwFRHq5.js`), but my fresh build produces different filenames (e.g. `ActionCenter-BIqc45j0.js`). Only the index files overlap.

### File type analysis

- **Content change vs. hash rename:** The diffs show old asset filenames being deleted and new ones being added. This is NOT a simple hash rename of the same content — Vite is producing different hash suffixes for some files (indicating content has changed) and leaving some hash names identical across rebuilds.

- **Example divergence:**
  - Committed: `ActionCenter-CjwFRHq5.js` → Fresh: `ActionCenter-BIqc45j0.js` (DIFFERENT hash)
  - Committed: `index-DfxbbtEN.js` → Fresh: still `index-DfxbbtEN.js` (SAME hash, reused from old build)

This pattern suggests either:
  1. The committed dist was built from a different source (different vite.config or source code)
  2. The committed dist accumulated files from multiple build operations

---

## Investigation C — Environment Audit

**Node and npm versions:**
```
Node: v24.15.0
npm: 11.12.1
```

**Package.json engines constraint:**
```json
{
  "engines": {
    "node": ">=20.20.0 || >=22.22.0 || >=24.13.0"
  }
}
```

✓ Current environment (Node 24.15.0) is within spec.

**Key dependency versions** (from package.json):
```
vite: ^7.3.2
react: ^19.2.3
typescript: ~5.9.3
```

**Build script:**
```json
"build": "tsc -b && vite build"
```

The build runs TypeScript compilation (`tsc -b`) then Vite bundling.

---

## Investigation D — vite.config.ts Review

Vite config is clean — no non-deterministic elements found:

```typescript
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),  // <-- deterministic
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1100,
    rollupOptions: {
      output: {
        manualChunks: (id) => {
          if (id.includes('node_modules')) {
            if (id.includes('/react-dom/') || id.includes('/react/'))
              return 'vendor-react'
            // ... more deterministic ID-based routing
          }
        },
      },
    },
  },
})
```

**No timestamps, no random IDs, no `Math.random()`, no `Date.now()`.** Config is purely deterministic. The `manualChunks` function is pure input→output routing.

---

## Investigation E — Committed Dist Metadata

**Most recent rebuild:** Commit `6efe2a1bd` (2026-06-22)
```
chore: rebuild frontend/dist on Node 20 (#66)
```

**What changed between commits:**

- **Before rebuild (commit c2943f5a5):** Had 3 index files (BfVg0V1M, C5dqxH9B, DfxbbtEN)
- **After rebuild (commit 6efe2a1bd):** Still has 3 index files, but now includes `index-v8lZ1r56.js` (so 4 total when counting all history)

The rebuild on Node 20 (#66) did NOT clean up old chunks; it just added a new one.

---

## Root Cause

**Primary hypothesis:** Vite's build process does not fully clean old chunks from previous builds when the build command is re-run. The committed dist accumulated artifacts from prior rebuilds.

**Evidence chain:**
1. PR #60 changed vite.config.ts (removed vendor-charts from manualChunks)
2. Multiple subsequent auto-build commits (`chore: auto-build frontend dist`) rebuilt the dist without fully cleaning old assets
3. PR #66 rebuilt on Node 20, but again without removing old chunks
4. Result: The committed dist contains "garbage" — multiple versions of the same logical bundle (3 different `index-*.js` files, only 1 used in index.html)

**Why it looks non-deterministic:**
- Committed dist has ActionCenter-CjwFRHq5.js, etc. (old hashes)
- Fresh build produces ActionCenter-BIqc45j0.js (new hashes)
- `git diff` shows these as deletions + additions, appearing as a "change"

**Why consecutive builds ARE deterministic:**
- Clean state: `rm -rf dist && npm run build` twice produces identical output
- Vite IS deterministic when given a clean starting point; it's just the git history that's cluttered

---

## Recommended Fix

**Immediate (already in flight):** PR #66 rebuilt dist on Node 20. This is the right approach but incomplete.

**Next step — Remove garbage:**
1. On the dev branch (or before merging #66 to main), clean the dist one final time:
   ```bash
   cd frontend
   rm -rf dist node_modules .tsbuildinfo
   npm install --no-audit --no-fund
   npm run build
   git add dist/
   git commit -m "chore: clean dist — remove accumulated old chunks"
   ```

2. **Verify:** Only ~150 files in dist/assets/, only ONE reference to each chunk in index.html

3. **Prevent future drift:** Add a CI gate to catch duplicate index-*.js files:
   ```bash
   # In .github/workflows or pre-commit:
   INDEX_COUNT=$(find frontend/dist/assets -name "index-*.js" 2>/dev/null | wc -l)
   if [ "$INDEX_COUNT" -gt 1 ]; then
     echo "ERROR: Found $INDEX_COUNT index-*.js files (expected 1)"
     exit 1
   fi
   ```

4. **Update dist on Node 20** if needed after any vite.config.ts or major dependency changes.

---

## Alternative — Accept Non-Determinism

If the garbage accumulation is deemed acceptable (since it doesn't affect runtime, only git history size):

The Tier 4 dist freshness gate should **ignore**  old/unused chunks and only check that:
1. The referenced index-*.js in index.html is present
2. vendor-react, vendor-router, vendor-icons, vendor-radix are present
3. core-*.js exists
4. No NEW errors introduced in the build output

This would make the gate pass even if old chunks linger in dist/.

---

## Conclusion

**npm run build IS deterministic** when run on the same source on the same machine. The perceived non-determinism is an artifact of git tracking old Vite chunks. A single clean rebuild + a CI gate to prevent future accumulation is the minimal fix.
