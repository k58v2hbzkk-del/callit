# CALLIT stake page

Served by GitHub Pages. `app.js` is an esbuild bundle of `src/app.js` + `src/escrow.js` + @solana/web3.js 1.98.4:

    npx esbuild src/app.js --bundle --minify --format=iife --target=es2020 --define:global=globalThis --outfile=app.js
