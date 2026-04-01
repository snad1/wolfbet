// Paste this in WolfBet browser DevTools Console (F12 → Console tab)
// It searches all loaded JS files for the x-hash-api generation logic

(async () => {
  console.log("🔍 Searching for x-hash-api generation logic...\n");

  // Method 1: Search all script sources
  const scripts = performance.getEntriesByType("resource")
    .filter(r => r.initiatorType === "script" || r.name.endsWith(".js"));

  const keywords = [
    "x-hash-api", "hashApi", "hash-api", "hash_api",
    "x-client-type", "xHashApi", "hashapi", "apiHash",
    "sha256", "crypto.subtle", "CryptoJS", "createHash"
  ];

  let found = [];

  for (const script of scripts) {
    try {
      const resp = await fetch(script.name);
      const text = await resp.text();

      for (const kw of keywords) {
        const idx = text.toLowerCase().indexOf(kw.toLowerCase());
        if (idx !== -1) {
          // Extract surrounding context (500 chars before and after)
          const start = Math.max(0, idx - 500);
          const end = Math.min(text.length, idx + 500);
          const snippet = text.substring(start, end);
          found.push({
            file: script.name.split("/").pop(),
            keyword: kw,
            position: idx,
            snippet: snippet
          });
          console.log(`✅ Found "${kw}" in ${script.name.split("/").pop()} at position ${idx}`);
        }
      }
    } catch (e) {
      // Skip CORS-blocked scripts
    }
  }

  // Method 2: Intercept XMLHttpRequest to capture the hash generation
  if (found.length === 0) {
    console.log("⚠️ No matches in script sources. Trying runtime intercept...");
  }

  // Method 3: Check for webpack modules
  if (window.webpackChunk || window.__NEXT_DATA__ || window.__webpack_modules__) {
    console.log("📦 Webpack detected — searching module sources...");
    const modules = window.__webpack_modules__ || {};
    for (const [id, fn] of Object.entries(modules)) {
      const src = fn.toString();
      for (const kw of keywords) {
        if (src.toLowerCase().includes(kw.toLowerCase())) {
          const idx = src.toLowerCase().indexOf(kw.toLowerCase());
          const start = Math.max(0, idx - 300);
          const end = Math.min(src.length, idx + 300);
          found.push({
            file: `webpack_module_${id}`,
            keyword: kw,
            snippet: src.substring(start, end)
          });
          console.log(`✅ Found "${kw}" in webpack module ${id}`);
        }
      }
    }
  }

  // Method 4: Monkey-patch XMLHttpRequest to capture hash at runtime
  console.log("\n🔧 Installing request interceptor — place a bet to capture the hash logic...");

  const origSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
    if (name.toLowerCase() === "x-hash-api") {
      console.log(`\n🎯 x-hash-api intercepted!`);
      console.log(`   Value: ${value}`);
      console.log(`   URL: ${this._url || "unknown"}`);
      console.log(`   Stack trace:`);
      console.trace();
    }
    return origSetRequestHeader.call(this, name, value);
  };

  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    this._url = url;
    return origOpen.apply(this, arguments);
  };

  // Also intercept fetch
  const origFetch = window.fetch;
  window.fetch = function(url, options) {
    if (options && options.headers) {
      const headers = options.headers;
      const hashVal = headers["x-hash-api"] || headers["X-Hash-Api"];
      if (hashVal) {
        console.log(`\n🎯 x-hash-api in fetch!`);
        console.log(`   Value: ${hashVal}`);
        console.log(`   URL: ${url}`);
        console.log(`   Full headers:`, JSON.stringify(headers, null, 2));
        console.trace();
      }
    }
    return origFetch.apply(this, arguments);
  };

  console.log("\n📊 Summary:");
  console.log(`   Found ${found.length} matches in source code`);
  console.log(`   Request interceptor active — place a bet now to see the stack trace`);

  if (found.length > 0) {
    console.log("\n📝 All matches:");
    found.forEach((f, i) => {
      console.log(`\n--- Match ${i+1}: "${f.keyword}" in ${f.file} ---`);
      console.log(f.snippet);
    });
  }

  // Store results globally for easy access
  window._hashApiResults = found;
  console.log("\n💡 Results stored in window._hashApiResults");
  console.log("💡 Now place a manual or auto bet — the interceptor will show the stack trace");
})();
