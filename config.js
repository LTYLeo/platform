/* ==========================================================================
   TAI Developer Platform — runtime configuration
   --------------------------------------------------------------------------
   This site is deployed on GitHub Pages, which serves **static files only**.
   It cannot run the Python/FastAPI authentication service, so that service
   lives on its own host and this file tells the front-end where to find it.

        ''                      -> same origin. Use this for local development
                                  (`python3 -m uvicorn server.main:app`) or any
                                  setup where one process serves both the pages
                                  and the API.
     'https://api.example.com' -> the deployed API. Required on GitHub Pages.

   IMPORTANT — cookies:
   Session cookies are SameSite=Lax by default, which browsers do NOT send on
   cross-site requests. That means github.io -> api.example.com will fail to
   stay logged in. Two supported ways out:

     A. (recommended) Put both on the same registrable domain, e.g. the Pages
        site on dev.taistudy.com and the API on api.taistudy.com. Same-site, so
        SameSite=Lax works and nothing else is needed.

     B. Different domains entirely: run the API with
          TAI_COOKIE_SAMESITE=none  TAI_COOKIE_SECURE=true
        and set TAI_ALLOWED_ORIGINS to this site's origin. This relies on
        third-party cookies, which Safari and some Chrome configurations block.

   See server/README.md -> "Deploying" for the full walkthrough.
   ========================================================================== */
window.TAI_CONFIG = {
  // Overridden at runtime by api-endpoint.json (see auth.js), so moving the
  // tunnel is a one-file change rather than a rebuild of the site.
  apiBase: 'https://synchronistical-dede-coeducationally.ngrok-free.dev'
};
