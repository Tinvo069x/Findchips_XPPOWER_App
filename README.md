# Findchips Purchasing Matcher — GitHub Pages + Cloud Backend

This package is built from the supplied working files:

- `app(3).py`
- `Findchips_Purchasing_Matcher.html`
- `start(3).bat`

The scraper/parsing logic is preserved. Only deployment plumbing was changed.

## Repository structure

```text
backend/
  app.py
  Findchips_Purchasing_Matcher.html
  Dockerfile
  requirements.txt
  smoke_test.py

docs/
  index.html
  config.js
  .nojekyll

render.yaml
run_local.bat
```

## 1. Upload this folder to GitHub

Upload the CONTENTS of this folder to the repository root.

## 2. Deploy backend on Render

Render Dashboard -> New -> Blueprint -> select this GitHub repository.

Render will read `render.yaml`.

Wait until the service is Live. Copy the Render URL, for example:

```text
https://findchips-purchasing-backend.onrender.com
```

Test:

```text
https://findchips-purchasing-backend.onrender.com/api/health
```

## 3. Configure GitHub Pages frontend

Edit:

```text
docs/config.js
```

Set:

```javascript
window.FINDCHIPS_API_BASE =
  "https://findchips-purchasing-backend.onrender.com";
```

Commit the change.

## 4. Enable GitHub Pages

GitHub repository -> Settings -> Pages:

- Source: Deploy from a branch
- Branch: `main`
- Folder: `/docs`

The public URL will look like:

```text
https://YOUR-USERNAME.github.io/YOUR-REPO/
```

## Important

GitHub Pages only hosts the frontend. The Findchips browser-render scraper runs on the Render backend.

The backend Docker image installs Chromium so the existing Playwright/Chrome logic can render lazy-loaded distributor rows.
