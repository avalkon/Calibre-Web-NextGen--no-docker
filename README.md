<p align="center">
  <img src="README_images/calibre-web-nextgen-banner.png" alt="Calibre-Web NextGen" width="520">
</p>

[![Latest release](https://img.shields.io/github/v/release/new-usemame/Calibre-Web-NextGen)](https://github.com/new-usemame/Calibre-Web-NextGen/releases/latest)
[![Container](https://img.shields.io/badge/ghcr.io-calibre--web--nextgen-blue?logo=docker)](https://github.com/new-usemame/Calibre-Web-NextGen/pkgs/container/calibre-web-nextgen)
[![Open issues](https://img.shields.io/github/issues/new-usemame/Calibre-Web-NextGen)](https://github.com/new-usemame/Calibre-Web-NextGen/issues)
[![Sponsor](https://img.shields.io/badge/Sponsor-nothing%20paywalled-ea4aaa?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/new-usemame)


## Table of contents

- [Why this fork exists](#why-this-fork-exists)
- [What's working](#whats-working)
- [Dockerless Install](#dockerless-install)
- [Runtime path overrides for packagers](#runtime-path-overrides-for-packagers)
- [First run](#first-run)
- [Troubleshooting](#troubleshooting)
- [Differences from upstream](#differences-from-upstream)
- [Contributing](#contributing)
- [How AI is used](#how-ai-is-used)
- [Credits](#credits)

---

## Why this fork exists

All upstream sources have determined that porting the install to bare-metal is not on their roadmap. Here, we're breaking Docker functionality and replacing it with bare-metal capability. Don't try to run this with Docker, it won't work. Too many things have already been changed.

---

## What's working

- Cover saves from Hardcover, Google Books, iTunes, and Open Library (was returning "not a valid image" since 4.0.6).
- Automatic ingest and convert is operational.
- Automatic metadata change detector/cover enforcer is working.
- Check nextgen status button in admin panel now works.

---

## Dockerless install

```
sudo mkdir /opt/calibre-web-nextgen
cd /opt/calibre-web-nextgen
sudo git clone https://github.com/avalkon/Calibre-Web-NextGen--no-docker.git .
```
```sudo nano scripts/calwebng_install.sh ```:

```
SERVICE_USER="if acw is already a user, change this."
SERVICE_GROUP="same as SERVICE_USER"
USER_USER="yourusername"
CALIBRE_LIBRARY="/path/to/your/library"
```
Finally:
```
sudo chmod +x ./scripts/calwebng_install.sh
sudo ./scripts/calwebng_install.sh
```

---

### What goes in each volume

| Volume | What it is | Notes |
|---|---|---|
| `/config` | App settings, user accounts, OAuth tokens, KOReader sync state, logs | Empty folder for new installs. Carries over from CWA verbatim. |
| `/calibre-library` | Books and Calibre's `metadata.db` | If empty, CWA creates a fresh library. If multiple `metadata.db` files exist inside, CWA picks the largest. |
| `/srv/calibre-ingest/` | Drop zone for new books | Files here are **deleted** after processing. Don't park books here long-term. |

> Don't nest the binds. All three should be separate top-level folders. Putting `ingest` inside `library` produces recursive ingest behavior.

---

## Runtime path overrides for packagers

Direct edit of /opt/calibre-web-nextgen/dirs.json is currently the recommended action if you need to change a path after install.

| Environment variable | `dirs.json` fallback | Compiled-in default |
|---|---|---|
| `CWA_INGEST_FOLDER` | `ingest_folder` | `/cwa-book-ingest` |
| `CWA_CALIBRE_LIBRARY_DIR` | `calibre_library_dir` | `/calibre-library` |
| `CWA_TMP_CONVERSION_DIR` | `tmp_conversion_dir` | `/config/.cwa_conversion_tmp` |

Each non-blank environment value wins for its key. If it is unset or blank,
CWNG reads that key from the file selected by `CWA_DIRS_JSON`; a missing or
malformed file, a non-object document, or a null/blank value falls back to the
compiled-in default. Existing hand-edited `dirs.json` files therefore remain
supported.

Runtime path values are trimmed and lexically normalized, and must be absolute,
non-root paths without a `..` component. Repeated separators, `.` components,
and trailing separators are collapsed without resolving symlinks. A non-blank
environment or `dirs.json` value that violates that contract stops the affected
startup service instead of letting an unsafe path reach file watchers or
recursive ownership operations.

For example, a systemd unit can load a packager-owned file:

```ini
[Service]
EnvironmentFile=/etc/calibre-web-nextgen/paths.env
```

```bash
CWA_INGEST_FOLDER=/srv/calibre-web-nextgen/ingest
CWA_CALIBRE_LIBRARY_DIR=/srv/calibre/library
CWA_TMP_CONVERSION_DIR=/var/cache/calibre-web-nextgen/conversion
```

When `CWA_CALIBRE_LIBRARY_DIR` is set, it is authoritative.

---

## First run

1. Open the UI at `http://your-host:8083`.
2. Log in with `admin` / `admin123`.
3. Change the admin password (Profile → Account).
4. Go to Admin → Edit Basic Configuration → Feature Configuration and enable **Allow Uploads**. Without this, the metadata-fetch and cover-from-URL features can't write to your library.
5. Drop a book into your ingest folder. It should appear in the library within a few seconds.

The Admin → Settings panel has many optional toggles (auto-convert formats, automatic backups, EPUB fixer, KOReader sync, OAuth, etc.). Some of these options have been remapped to the same or similar function in a new or modified script. Some things have not yet been tested on bare-metal application, mostly because I either haven't gotten there yet, or don't own the necessary device to do the testing.

---

## Pair with Shelfmark(functionality unknown, proceed with care)

[Shelfmark](https://github.com/calibrain/shelfmark) by @calibrain is a self-hosted book search and request interface. Users search across torrent, usenet, IRC, and direct sources from a single UI; Shelfmark hands the download to your client of choice and drops the finished file straight into the CWA ingest folder, where this build picks it up automatically. Multi-user requests are built in, so you can share an instance with household readers and approve their picks.

This release does not include a built-in Store / Discover page or an Anna's Archive download provider. Configure acquisition sources in the separate Shelfmark service.

**With My Library enabled:** files delivered through the shared ingest folder enter the global library. The folder does not identify the Shelfmark requester, so the import does not automatically add the book to that person's selection. Readers with Global Library access can find and add it there; an administrator can add it for a managed account. Accounts using the whole-library mode continue to see permitted imports automatically.

After Shelfmark starts, open it and pick **Settings → Security → Authentication Method → Calibre-Web Database**, then **Sync from Calibre-Web** to import users. The [Shelfmark docs](https://github.com/calibrain/shelfmark#readme) cover Prowlarr, qBittorrent, SABnzbd, and IRC source setup.

> Shelfmark went into maintenance-only status in May 2026; the v1.3.0 build is stable and the integration with CWA is settled, but new feature work upstream has paused. If you want to pin for reproducibility, use `ghcr.io/calibrain/shelfmark:v1.3.0` instead of `:latest`.

---

## Common configurations

### Calibre desktop coexistence

If you want to open the same library in calibre desktop while calibre-web-nextgen is running, set both:

### Calibre plugins (DeDRM and others)(Currently untested, next on list.)

calibre-web-nextgen doesn't ship any Calibre plugins, but it can load ones you install yourself — the same plugin `.zip` files Calibre desktop uses. This is how you add things like DRM removal (DeDRM, Obok) or `.acsm` fulfillment (the ACSM Input plugin): you supply the plugins, and they run automatically during ingest, library conversion, and metadata embedding.

1. Turn the feature on in your compose environment, then restart:

   ```yaml
   - CWA_CALIBRE_USER_PLUGINS=true
   ```

2. Copy the plugin `.zip` files into the `plugins` folder inside your config volume — from the host that's `<your config folder>/.config/calibre/plugins/` (the folder is created automatically once the option is on).

3. Restart the container again. Each plugin is registered at startup; confirm with:

   ```
   docker logs calibre-web 2>&1 | grep "Registered Calibre plugin"
   ```

Plugins that need keys or an account (DeDRM wants your device keys, ACSM Input wants an Adobe login) keep their settings in files next to the zips. Easiest path: configure the plugin in Calibre desktop on your computer first, then copy its settings files (e.g. `plugins/dedrm.json`, the `plugins/DeACSM/` folder) from your desktop Calibre configuration folder into the same container `plugins/` folder and restart.

To add another plugin **after** the first batch is registered, drop the zip in the same folder and run:

```
docker exec -e HOME=/config calibre-web /opt/calibre/calibre-customize -a "/config/.config/calibre/plugins/<plugin file>.zip"
```

The feature is off by default because it runs third-party plugin code inside your container — only install plugins you trust, from their official release pages. Which plugins are appropriate to use is your call.

### Reverse proxy with a prefix(This info is preserved from upstream, and may not be applicable here.)

To deploy CWA behind a reverse proxy, configure your reverse proxy to forward
requests to the CWA service and handle the path prefix (e.g. `/cwa/`). For
instance, with Nginx:

```
location /cwa/ {
    proxy_pass http://calibre-web-automated:8083/;

    proxy_set_header Host              $http_host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

The trailing slash on `proxy_pass` matters: it strips `/cwa/` before the request
reaches CWA. The headers are written out rather than pulled in with
`include proxy_params;` because that file ships with Debian and Ubuntu's nginx
package only — the official `nginx` Docker images don't have it, and nginx
refuses to start when the include is missing.

You must also configure the application with the external URL prefix by setting
the following environment variable in your Docker compose file:

```yaml
environment:
  - PROXY_SCRIPT_NAME=/cwa
```

Leave the trailing slash off `PROXY_SCRIPT_NAME` — CWA joins it to each path
directly, so `/cwa/` would generate doubled-slash URLs.

This ensures that CWA correctly generates URLs when it is served from the
prefix path instead of the web server root.

For TLS, upload limits, and the larger proxy buffers Kobo sync needs, see
[`examples/nginx-reverse-proxy.conf`](examples/nginx-reverse-proxy.conf) — the
settings there apply to a prefixed deployment too.

### Reverse proxy / Cloudflare Tunnel(This info is preserved from upstream, and may not be applicable here.)

Behind multiple proxies (e.g. Cloudflare Tunnel then nginx then CWA), set the proxy count:

```yaml
- TRUSTED_PROXY_COUNT=2
```

Set `TRUSTED_PROXY_COUNT` to the number of trusted proxy hops: for
HAProxy → traefik → container, use `2`. With `X-Forwarded-Proto: https, http`,
the default of `1` selects the inner hop's `http`; `2` selects the browser-facing
`https`. If headers have different chain lengths, use the per-header overrides below.

The New UI's API Origin guard accepts a same-host HTTPS origin even when the
proxy-derived URL is HTTP, so upstream TLS termination does not block writes.
The reverse direction (HTTP origin against an HTTPS-derived URL) is still rejected.
For a `Rejected cross-site` warning, check `TRUSTED_PROXY_COUNT` and the forwarded
headers. If the proxy rewrites Host without preserving the public host in
`X-Forwarded-Host`, set `CWNG_TRUSTED_ORIGINS` to your public origin
(comma-separated for multiple origins). Correct proxy configuration is still
needed for generated URLs and other scheme-sensitive behavior.

Without this, CWA may see different client IPs across requests and trigger Session Protection warnings, forcing re-login on every page load. It can also mistake an externally secure OIDC callback for plain HTTP. Default is `1`.

`TRUSTED_PROXY_COUNT` applies one trust depth to `X-Forwarded-For`,
`X-Forwarded-Proto`, `X-Forwarded-Host`, and `X-Forwarded-Prefix`. If your
proxy chain appends or replaces those headers at different layers, override the
first three independently; each falls back to `TRUSTED_PROXY_COUNT`, then `1`:

```yaml
- PROXYFIX_X_FOR=2
- PROXYFIX_X_PROTO=1
- PROXYFIX_X_HOST=1
```

The corresponding ProxyFix arguments are `x_for`, `x_proto`, and `x_host`.
Count only proxies you control and that overwrite or sanitize the corresponding
header.

These headers are believed only when the connection comes from a trusted proxy
network. By default that is this host, private networks (the docker network,
your LAN), and Tailscale's `100.64.0.0/10`. A client that reaches CWNG directly
from anywhere else is taken at its own address and scheme, whatever headers it
sends. If your proxy connects from a public address, add it to
`TRUSTED_PROXY_NETWORKS`. That includes Cloudflare's proxy forwarding straight
to the container, with no proxy of your own in between: list Cloudflare's
published IP ranges.

Without the setting, CWNG sees every visitor as the proxy. Sign-in pacing is
then shared by everyone, links come out as `http://` behind an HTTPS proxy,
and sign-in providers may reject the callback address. The log says
`Ignoring reverse-proxy headers from <address>` when this applies.

```yaml
- TRUSTED_PROXY_NETWORKS=private, 203.0.113.7, 198.51.100.0/24
```

Entries are addresses or networks, separated by commas or spaces. `private`
stands for the default list, so keep it when you add your proxy. `*` trusts
every peer, which was the behaviour before this setting existed.

### Hardcover metadata provider(Untested)

[Hardcover](https://hardcover.app/) is a free metadata provider. To enable it:

1. Sign up at https://hardcover.app and grab an API token at https://hardcover.app/account/api.
2.   Paste it into Admin → Edit Basic Configuration → Hardcover API Key in the UI.
3. Restart the systemd service.

Hardcover then appears in the Fetch Metadata modal.

If you set the token through the `HARDCOVER_TOKEN` environment variable, the **Hardcover API Key** field in the admin UI stays empty — that field only shows a key entered through the UI, and an environment-supplied token is not echoed back into the page. The admin page identifies whether `HARDCOVER_TOKEN` or `HARDCOVER_TOKEN_FILE` is active without displaying its value; a key typed into the field overrides either environment source.

Precedence: UI-configured key → `HARDCOVER_TOKEN` → `HARDCOVER_TOKEN_FILE`.

Enable the server-wide integration once under Admin → Edit Basic Configuration → **Enable Hardcover Sync**. This single switch controls both scheduled Hardcover ID fetching and Kobo/KOReader reading-progress sync.

### KOReader sync(Not compatible with, for example Crosspoint kosync. Check out Crosspoint-sync)

CWA has built-in KOReader sync; no separate kosync server is needed. With the plugin, a Kindle or any KOReader e-reader opens on your library: covers of every book (or of the shelves you choose for e-readers), downloaded when you open them, with reading position, read status and highlights synced automatically. **Setup and daily use: [docs/koreader-kindle.md](docs/koreader-kindle.md).**

1. In CWA open **E-readers ▸ Pair a Kobo or KOReader** and either download the **ready-made plugin** (copy one folder over USB; nothing to type on the e-reader) or **pair with a code** shown on the e-reader.
2. To install the plain plugin by hand instead, visit `http://your-cwa:8083/kosync`, then sign in from **Tools ▸ CWNG library ▸ Connect this device**.
3. Read on any device. Progress syncs back to CWA, and from there to Kobo if Kobo sync is enabled.

**Keeping the plugin updated.** KOReader's [Updates Manager](https://github.com/advokatb/updatesmanager.koplugin) and [appstore.koplugin](https://github.com/kaz-utashiro/appstore.koplugin) can both update the plugin in place. Point either at the plugin's own repository, [`new-usemame/cwngsync.koplugin`](https://github.com/new-usemame/cwngsync.koplugin/releases) — not at this one. The plugin publishes a release only when the plugin itself changes, and its version is the server version it last changed in, so it can legitimately sit behind your server version; that alone doesn't mean anything is wrong. With the plugin repository configured, a check that reports no new release means the plugin stream has nothing newer.

If your update manager is still pointed at this repository, switch it. That setup keeps working — a release that changes the plugin attaches the plugin download — but the plugin only appears on those releases, which is easy to misread as "no update available". The download on `/kosync` always serves the plugin bundled with your running server if you would rather update by hand.

**Matching filenames across devices (OPDS downloads).** If you download books to KOReader over OPDS and sync progress by filename across several e-readers, turn on **Use server filenames** in KOReader's OPDS catalog settings (the checkbox when you add or edit the catalog). By default KOReader names a downloaded file `Author - Title.epub` from the catalog entry, which differs from the on-disk library name `Title - Author.epub` and forces a manual rename. CWA already sends the library name in the download's `Content-Disposition` header; with **Use server filenames** on, KOReader uses that name, so the file matches your library and your other devices without renaming.

### Kobo sync

Read your CWA library on a Kobo e-reader, with reading progress syncing both ways. Sync runs against your own server, so your library never leaves your network.

1. In Admin → Edit Basic Configuration, turn on **Enable Kobo sync**.
2. Open your user page (Admin → Users → your user, or your own profile) and click **Create/View** next to **Kobo Sync Token**. The dialog shows the exact `api_endpoint=` line for your account.
3. Plug the Kobo into a computer over USB and open `.kobo/Kobo/Kobo eReader.conf` in a text editor. Add or replace the `api_endpoint=` line with the one from the dialog, save, and eject the device cleanly.
4. On the Kobo, sync. Books on your Kobo Sync shelves appear on the device, and progress flows back to CWA.

> ### ℹ️ Where your highlights travel, and how to check
>
> `api_endpoint` routes **library sync**. Your **highlights and notes** travel over a separate
> reading-services channel governed by a different key, `reading_services_host`.
>
> **You should not normally need to touch that key.** CWA advertises the right value during sync
> initialization, and a device that performs a full initialization against your server adopts it on
> its own. That is the supported path.
>
> 🚨 **Do not hand-edit `reading_services_host` in the conf file.** Doing so has been measured to
> break syncing outright on at least one device — a Kobo Clara BW on firmware 4.42.23291 began
> failing every sync with `FailedSync / WebRequestErr`, and recovered only when the key was set back
> to `readingservices.kobo.com`. A Kobo Libra Colour on 4.45.23697 is unaffected and routes
> annotations through CWA happily, so this is **not** universal — but we cannot yet predict which
> devices tolerate it, and the failure leaves you with a reader that will not sync and no obvious
> cause.
>
> **If your sync has already broken after editing that key:** set `reading_services_host` back to
> `readingservices.kobo.com`, save, eject cleanly, and sync again.
>
> **To see whether annotations are reaching CWA**, make a highlight on the device, sync, and watch:
>
> ```bash
> docker logs -f calibre-web 2>&1 | grep -iE "annotations|reading services"
> ```
>
> Silence means your highlights are going to Kobo's servers rather than yours. The safe way to
> change that is to get the device to perform a **full initialization** against CWA — re-generate
> the Kobo Sync Token and re-pair — rather than editing the key by hand.
>
> This matters because CWA's protection against a Kobo deleting its own highlights after a sync
> (upstream [calibre-web#2610](https://github.com/janeczku/calibre-web/issues/2610)) works by
> answering that channel, and it cannot protect a request it never receives. Until the device is
> routing annotations through CWA, treat highlights made on it as device-only and back them up.

To confirm the device is reaching your server, watch the logs while you sync — you should see requests to `/kobo/<token>/v1/...`:

```bash
docker logs -f calibre-web 2>&1 | grep /kobo/
```

**Behind a reverse proxy (nginx, Nginx Proxy Manager, Caddy, Cloudflare Tunnel)**

Kobo devices sync over HTTPS, so the `api_endpoint` has to be your public `https://` address. Put a proxy with a valid certificate in front and point it at the container's plain HTTP port:

- Proxy target is `http://<container-host>:8083`. The proxy terminates TLS on 443; the connection from the proxy to CWA stays HTTP. WebSocket support is not needed for Kobo sync.
- Generate the token while visiting CWA through the HTTPS address, so the `api_endpoint=` line the dialog shows already carries your public hostname.
- If you stack proxies (for example Cloudflare Tunnel in front of nginx), set [`TRUSTED_PROXY_COUNT`](#reverse-proxy--cloudflare-tunnel) to the number of proxies.

**nginx buffer sizes (important for Kobo sync)**

Kobo's `/v1/library/sync` response carries large headers (auth, sync tokens, library state). Nginx's default `proxy_buffer_size` (4 KB) and `proxy_buffers` (8 × 4 KB) are too small; the response is silently dropped before it reaches the device, and the Kobo shows *"Sync failed, please try again"* with **no error in the CWA log**. The nginx error log shows `upstream sent too big header while reading response header from upstream`. Add these to the `location /` block proxying CWA:

```nginx
proxy_buffer_size       32k;
proxy_buffers           4 32k;
proxy_busy_buffers_size 64k;
```

(Larger libraries may need `128k / 4 256k / 256k`.) Reload nginx after the change. On Synology DSM, the built-in reverse-proxy GUI doesn't expose these directives — drop a custom config at `/etc/nginx/conf.d/http.calibre_web.conf` that mirrors the DSM entry plus the buffer lines, then disable the DSM entry. DSM rewrites `nginx.conf` on reboot, so a Task Scheduler boot-event job that runs `nginx -s reload` reapplies the custom file. Nginx Proxy Manager users: add the three lines under the proxy host's *Advanced* tab.

See [`examples/nginx-reverse-proxy.conf`](examples/nginx-reverse-proxy.conf) for a complete reference snippet.

**If you keep a Kobo account signed in**

Signing into a Kobo account, or doing a factory reset, can rewrite the `api_endpoint=` line back to Kobo's own server, which sends sync to Kobo instead of your library. After signing in, re-check the conf line over USB and set it back if it changed. Many sideloaded setups sign out of the Kobo account so the device stops resetting the endpoint.

To keep the Kobo Store and your library working at the same time, turn on **Proxy unknown requests to Kobo Store** in Admin → Edit Basic Configuration. With it off (the default), any request CWA doesn't recognize gets an empty response — fine for a sideload-only device, but store features won't load.

---

## Troubleshooting

### "Cover-file is not a valid image file, or could not be stored"

Fixed in v4.0.13 and later. If you're still seeing it after upgrading, you probably have `root:root`-owned book directories from a pre-fix install.

### "Generate Kobo Auth Token" returns a blank page

Fixed in v4.0.14 and later. Upgrade the image.

### Kobo says "Sync failed, please try again"

Almost always one of these:

1. The device isn't reaching your server. The `api_endpoint=` line in `.kobo/Kobo/Kobo eReader.conf` must point at your CWA address (not `storeapi.kobo.com`), and that address must be reachable over HTTPS. See [Kobo sync](#kobo-sync).
2. A Kobo account is signed in and **Proxy unknown requests to Kobo Store** is off, so the device's store calls get an empty response mid-sync. Turn that setting on, or sign out of the Kobo account on the device.
3. Behind a reverse proxy, the proxy can't reach the container. Confirm the proxy target is `http://<host>:8083` and that the certificate is valid.
4. **nginx is silently dropping the sync response because its default buffers are too small for Kobo's library-sync headers.** The CWA log shows the request arriving but nothing else; the nginx error log shows `upstream sent too big header`. Add `proxy_buffer_size 32k; proxy_buffers 4 32k; proxy_busy_buffers_size 64k;` to the proxy location. See the [nginx buffer sizes](#kobo-sync) note in the Kobo sync section.

### "Database is locked" / app frozen

If your library is on a network share, set `NETWORK_SHARE_MODE=true` (see above). On local disk, this usually means a previous container shutdown was unclean: restart the systemd services.

### Session Protection warnings, forced re-login on every page

Set `TRUSTED_PROXY_COUNT` to match your proxy depth. See [Reverse proxy](#reverse-proxy--cloudflare-tunnel).

### Books in `/srv/calibre-ingest` aren't picked up

Three common causes:

1. Files owned by root. Make sure ingest files are owned by your `PUID:PGID` user.
2. Watcher missed them. Click the **Refresh Library** button on the navbar; it does a one-shot scan.(Untested in current version)
3. Format isn't allowed. Check Admin → CWA Settings → Ingest for your allowed formats.

### Default login isn't working

The defaults are `admin` / `admin123` (lowercase). If you've already changed the password and forgotten it: stop the container, delete `config/app.db`, and restart. This resets the database. User accounts are lost; the library itself is untouched. Unfortunately it also means you're going to have to inject your library location into app.db again. 
Run:

sudo /opt/calibre-web-nextgen/scripts/re-add-app-db.sh

### Something else

Check the [issue tracker](https://github.com/avalkon/Calibre-Web-NextGen--no-docker/issues) or [open a new issue](https://github.com/avalkon/Calibre-Web-NextGen--no-docker/issues/new). Useful information:

- Recent logs:
- `sudo journalctl -u calibre-web-nextgen.service -f -n 100`
- `sudo journalctl -u calibre-web-ingest.service -f -n 100`
- `sudo journalctl -u calibre-web-meta.service -f -n 100`
- What you did and what you expected to happen

---

## Differences from upstream

| Behavior | Upstream CWA `:latest` | This build |
|---|---|---|
| Cover saves from Hardcover/Google Books/iTunes/Open Library | Returns "not a valid image" | Saves and persists |
| Generate Kobo Auth Token | Blank page | ? |
| Safari metadata search | Silent 400 | ? |
| Safari book-delete button | Broken since the Feb-4 commit | ? |
| Kobo bookmark sync with missing `Location` | Crashes | ? |
| `/kobo_auth/generate_auth_token` IDOR | Open (any user can mint another user's token) | ? |
| Reverse-proxy user-profile updates | Drops path prefix | ? |
| `.cbr` / `.cbz` OPDS mimetypes | Non-IANA | IANA-compliant |
| Cover resolution on high-DPI readers | Often 290×475 (Hardcover thumbnail) | 1000×1500+ via booster |
| Admin routes (`cwa_logs`, `convert`, `epub_fixer`, …) | 14 unauthenticated | All require admin |

Backports are conservative. Anything that touches auth, schema, or dependencies gets a manual review before merging.

---

## Translations(Preserved from upstream. Translations are occasionally merged from upstream, but there's a lot of upstream to dig through to keep from breaking anything here)

The interface ships with the locales below.

<!-- TRANSLATION_STATUS_START -->
| Language | Completion | Strings | Fuzzy |
|---|---|---:|---:|
| English (source) | 100% | source | — |
| Russian (`ru`) | `████████████████████` 100% | 3384/3387 | 0 |
| Slovak (`sk`) | `████████████████████` 100% | 3381/3387 | 0 |
| Swedish (`sv`) | `███████████████████░` 97% | 3292/3387 | 0 |
| Italian (`it`) | `██████████████████░░` 92% | 3103/3387 | 0 |
| Spanish (`es`) | `██████████████████░░` 91% | 3087/3387 | 0 |
| French (`fr`) | `█████████████████░░░` 84% | 2849/3387 | 125 |
| Chinese (Traditional, Taiwan) (`zh_Hant_TW`) | `████████████████░░░░` 78% | 2654/3387 | 181 |
| Polish (`pl`) | `███████████████░░░░░` 76% | 2570/3387 | 0 |
| Dutch (`nl`) | `██████████████░░░░░░` 71% | 2396/3387 | 288 |
| German (`de`) | `█████████████░░░░░░░` 67% | 2281/3387 | 12 |
| Hungarian (`hu`) | `██████████░░░░░░░░░░` 48% | 1636/3387 | 119 |
| Portuguese (Brazil) (`pt_BR`) | `████████░░░░░░░░░░░░` 41% | 1396/3387 | 305 |
| Japanese (`ja`) | `████████░░░░░░░░░░░░` 39% | 1310/3387 | 244 |
| Slovenian (`sl`) | `███████░░░░░░░░░░░░░` 36% | 1204/3387 | 312 |
| Chinese (Simplified, China) (`zh_Hans_CN`) | `███████░░░░░░░░░░░░░` 34% | 1167/3387 | 342 |
| Korean (`ko`) | `██████░░░░░░░░░░░░░░` 28% | 939/3387 | 266 |
| Arabic (`ar`) | `█████░░░░░░░░░░░░░░░` 23% | 784/3387 | 281 |
| Portuguese (`pt`) | `████░░░░░░░░░░░░░░░░` 21% | 697/3387 | 354 |
| Galician (`gl`) | `████░░░░░░░░░░░░░░░░` 20% | 673/3387 | 355 |
| Indonesian (`id`) | `████░░░░░░░░░░░░░░░░` 20% | 674/3387 | 356 |
| Greek (`el`) | `███░░░░░░░░░░░░░░░░░` 15% | 505/3387 | 393 |
| Czech (`cs`) | `███░░░░░░░░░░░░░░░░░` 14% | 476/3387 | 402 |
| Ukrainian (`uk`) | `███░░░░░░░░░░░░░░░░░` 13% | 445/3387 | 367 |
| Norwegian (`no`) | `███░░░░░░░░░░░░░░░░░` 13% | 430/3387 | 430 |
| Vietnamese (`vi`) | `██░░░░░░░░░░░░░░░░░░` 12% | 423/3387 | 351 |
| Finnish (`fi`) | `██░░░░░░░░░░░░░░░░░░` 10% | 356/3387 | 382 |
| Turkish (`tr`) | `██░░░░░░░░░░░░░░░░░░` 9% | 290/3387 | 379 |
| Khmer (`km`) | `█░░░░░░░░░░░░░░░░░░░` 6% | 208/3387 | 339 |
<!-- TRANSLATION_STATUS_END -->

---

## Contributing

- **Bug reports:** [open a bug issue](https://github.com/avalkon/Calibre-Web-NextGen--no-docker/issues/new?template=bug_report.md). Reproduction steps, version tag, and a `docker logs` snippet help a lot.
- **Feature requests:** [open a feature issue](https://github.com/avalkon/Calibre-Web-NextGen--no-docker/issues/new?template=feature_request.md). The bar is low — bug reports get prioritized for code work, but feature requests shape what gets looked at when the bug queue is quiet, and they help upstream see what users actually want. Don't worry about whether it's "in scope"; just file it.
- **Pull requests:** welcome. The merge bar is "doesn't break anything that currently works." Changes touching auth, schema, or dependencies get a closer review. Backports keep the original author's handle in the commit message.
- **CWA PR authors with stalled work upstream:** if you'd like your PR shipped here too, open an issue or send the PR our way.

---

## How AI is used

The codebase itself is Calibre-Web and Calibre-Web-Automated. — written over many years by their
human maintainers and contributors. Calibre-Web-NextGen is largely produced by an AI assistant working from a written 
brief, with human review gates: merges require CI plus a regression test verified to fail without the fix, and anything 
adding a dependency,changing a license or introducing an external URL is decided by a person.

What this fork adds on top — Shifts primary focus from Docker to being bare-metal capable. Many of the changes to this 
code are first written by hand, and then run through AI to find bugs, refine operation, and just generally make it 
prettier than I can. Some code is written primarily by AI, then heavily reviewed and tested, and modified as necessary.
I am slowly trying to extricate the Docker-related files to clean things up.

I'm one woman, I can't do everything, I'm not even a real developer! I just learn things quickly. I don't write code 
very quickly though. 

**The shipped application itself contains no AI:** no model dependency, no inference call, no
telemetry, and your library is not sent anywhere.

[**Read the full disclosure →**](docs/AI-USAGE.md)

---

## Credits

Built on:

- **Calibe-web-NextGen** ([@new-username](https://github.com/new-username) and contributors) — the core software this build is based on.
- **Calibre-Web-Automated** ([@crocodilestick](https://github.com/crocodilestick) and contributors) — the core software NextGen is based on. Original PR authors are credited by handle in every backport commit.
- **Calibre-Web** ([@janeczku](https://github.com/janeczku) and contributors) — the web UI underneath CWA.
- **Calibre** ([@kovidgoyal](https://github.com/kovidgoyal)) — the library underneath all of it.


To support upstream NextGen, see [@new-username](https://github.com/new-username). To support the upstream project it builds on, [@crocodilestick has a Ko-fi](https://ko-fi.com/crocodilestick) too.

---

*License: GPL-3.0-or-later. See [`LICENSE`](LICENSE).*
