<p align="center">
  <img src="README_images/calibre-web-nextgen-banner.png" alt="Calibre-Web NextGen" width="520">
</p>

[![Latest release](https://img.shields.io/github/v/release/new-usemame/Calibre-Web-NextGen)](https://github.com/avalkon/Calibre-Web-NextGen--no-docker/releases/latest)
[![Container](https://img.shields.io/badge/ghcr.io-calibre--web--nextgen-blue?logo=docker)](https://github.com/avalkon/Calibre-Web-NextGen--no-docker/pkgs/container/calibre-web-nextgen)
[![Open issues](https://img.shields.io/github/issues/avalkon/Calibre-Web-NextGen--no-docker)](https://github.com/avalkon/Calibre-Web-NextGen--no-docker/issues)

## Table of contents

- [Why this fork exists](#why-this-fork-exists)
- [What's working](#whats-working)
- [Dockerless Install](#dockerless-install)
- [Runtime path overrides](#runtime-path-overrides)
- [First run](#first-run)
- [Troubleshooting](#troubleshooting)
- [Differences from upstream](#differences-from-upstream)
- [Contributing](#contributing)
- [How AI is used](#how-ai-is-used)
- [Credits](#credits)

---

## Why this fork exists

In short, it's for all of us old people who tried Docker and found it to be alien and undesirable, but still 
want things like auto-ingest. It's a bit of a mix of NextGen, AutoCaliWeb, Calibre-web-Automated, 
and Calibre-web, with many many hours of code holding the scavenged bits together. 

All upstream sources have determined that porting the install to bare-metal is not on their roadmap. 
Here, we're breaking Docker functionality and replacing it with bare-metal capability. Don't try to 
run this with Docker, it won't work the way the original would. Too many things have already been changed.
Stick to the upstream version if you use containers.

Autocaliweb has bare-metal capability, why not use that? Well, auto-ingest and metadata change detection 
still don't work on ACW's bare-metal installation, and I had to do a lot of modifying to get things 
like edit-selected-books running, some of these are things that just plain worked when I tried running 
NextGen by itself on my local machine. Both ACW and CWNG have explicitly stated that bare-metal deployment 
is not on their priority list. It's the only thing on my priority list.

---

## What's working

- Automatic ingest and convert is operational.
- Automatic metadata change detector/cover enforcer is working.
- Check nextgen status button in admin panel now works.
- OPDS works.
- Koreader sync works

---

## Dockerless install

The absolute easiest way to install is to jump over to https://github.com/avalkon/server-builder
and download server-install.sh and run:

```
sudo chmod +x ./server-install.sh
sudo ./server-install.sh --install-calibre
```

It will prompt you for your library location, clone the repo, install the most recent version of Calibre, 
and install this version of Nextgen. It can also install a lot of other services, if you want them.

If you rather use my provided install script:

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

### What's in each folder

| Volume | What it is | Notes |
|---|---|---|
| `/config` | App settings, user accounts, OAuth tokens, KOReader sync state, logs | Empty folder for new installs. Carries over from CWA verbatim. |
| `/calibre-library` | Books and Calibre's `metadata.db` | If empty, CWA creates a fresh library. If multiple `metadata.db` files exist inside, CWA picks the largest. |
| `/srv/calibre-ingest/` | Drop zone for new books | Files here are **deleted** after processing. Don't park books here long-term. |

All three should be separate top-level folders. Putting `ingest` inside `library` produces recursive ingest behavior.

---

## Runtime path overrides

Direct edit of /opt/calibre-web-nextgen/dirs.json is currently the recommended action if you need to change a path after install.

| Environment variable | `dirs.json` fallback | Compiled-in default |
|---|---|---|
| `CWA_INGEST_FOLDER` | `ingest_folder` | `/cwa-book-ingest` |
| `CWA_CALIBRE_LIBRARY_DIR` | `calibre_library_dir` | `/calibre-library` |
| `CWA_TMP_CONVERSION_DIR` | `tmp_conversion_dir` | `/config/.cwa_conversion_tmp` |


For example, a systemd unit can load a packager-owned file:


CWA_INGEST_FOLDER=/srv/calibre-web-nextgen/ingest

CWA_CALIBRE_LIBRARY_DIR=/srv/calibre/library

CWA_TMP_CONVERSION_DIR=/var/cache/calibre-web-nextgen/conversion


When `CWA_CALIBRE_LIBRARY_DIR` is set, it is authoritative.

---

## First run

1. Open the UI at `http://your-host:8083`.
2. Log in with `admin` / `admin123`.
3. Change the admin password (Profile → Account).
4. Go to Admin → Edit Basic Configuration → Feature Configuration and enable **Allow Uploads**. Without this, the
metadata-fetch and cover-from-URL features can't write to your library.
5. Drop a book into your ingest folder. It should appear in the library within a few seconds.

The Admin → Settings panel has many optional toggles (auto-convert formats, automatic backups, EPUB fixer, KOReader
sync, OAuth, etc.). Some of these options have been remapped to the same or similar function in a new or modified
script. Some things have not yet been tested on bare-metal application, mostly because I either haven't gotten
there yet, or don't own the necessary device to do the testing.

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

calibre-web-nextgen doesn't ship any Calibre plugins, but it can load ones you install yourself — the same plugin
`.zip` files Calibre desktop uses. This is how you add things like DRM removal (DeDRM, Obok) or `.acsm` fulfillment
(the ACSM Input plugin): you supply the plugins, and they run automatically during ingest, library conversion, and
metadata embedding.


Plugins that need keys or an account (DeDRM wants your device keys, ACSM Input wants an Adobe login) keep their 
settings in files next to the zips. Easiest path: configure the plugin in Calibre desktop on your computer first, 
then copy its settings files (e.g. `plugins/dedrm.json`, the `plugins/DeACSM/` folder) from your desktop Calibre 
configuration folder into the same container `plugins/` folder and restart.

The feature is off by default because it runs third-party plugin code inside your container — only install plugins 
you trust, from their official release pages. Which plugins are appropriate to use is your call.



### Reverse proxy / Cloudflare Tunnel(This info is preserved from upstream, and may not be applicable here.)

Behind multiple proxies (e.g. Cloudflare Tunnel then nginx then CWA), set the proxy count to however many jumps are made.

The New UI's API Origin guard accepts a same-host HTTPS origin even when the
proxy-derived URL is HTTP, so upstream TLS termination does not block writes.
The reverse direction (HTTP origin against an HTTPS-derived URL) is still rejected.
For a `Rejected cross-site` warning, check `TRUSTED_PROXY_COUNT` and the forwarded
headers. If the proxy rewrites Host without preserving the public host in
`X-Forwarded-Host`, set `CWNG_TRUSTED_ORIGINS` to your public origin
(comma-separated for multiple origins). Correct proxy configuration is still
needed for generated URLs and other scheme-sensitive behavior.

Without this, CWA may see different client IPs across requests and trigger Session Protection 
warnings, forcing re-login on every page load. It can also mistake an externally secure OIDC 
callback for plain HTTP. Default is `1`.

### Hardcover metadata provider(Untested)

[Hardcover](https://hardcover.app/) is a free metadata provider. To enable it:

1. Sign up at https://hardcover.app and grab an API token at https://hardcover.app/account/api.
2.   Paste it into Admin → Edit Basic Configuration → Hardcover API Key in the UI.
3. Restart the systemd service.

Hardcover then appears in the Fetch Metadata modal.

If you set the token through the `HARDCOVER_TOKEN` environment variable, the **Hardcover API Key** 
field in the admin UI stays empty — that field only shows a key entered through the UI, and an 
environment-supplied token is not echoed back into the page. The admin page identifies whether 
`HARDCOVER_TOKEN` or `HARDCOVER_TOKEN_FILE` is active without displaying its value; a key typed 
into the field overrides either environment source.

Precedence: UI-configured key → `HARDCOVER_TOKEN` → `HARDCOVER_TOKEN_FILE`.

Enable the server-wide integration once under Admin → Edit Basic Configuration → **Enable Hardcover 
Sync**. This single switch controls both scheduled Hardcover ID fetching and Kobo/KOReader reading-progress sync.

### KOReader sync(Not compatible with, for example Crosspoint kosync. Check out Crosspoint-sync)

CWA has built-in KOReader sync; no separate kosync server is needed. With the plugin, a Kindle or 
any KOReader e-reader opens on your library: covers of every book (or of the shelves you choose 
for e-readers), downloaded when you open them, with reading position, read status and highlights 
synced automatically.

1. In CWA open **E-readers ▸ Pair a Kobo or KOReader** and either download the **ready-made plugin**
(copy one folder over USB; nothing to type on the e-reader) or **pair with a code** shown on the e-reader.
2. To install the plain plugin by hand instead, visit `http://your-cwa:8083/kosync`, then sign in from **Tools ▸ CWNG library ▸ Connect this device**.
3. Read on any device. Progress syncs back to CWA, and from there to Kobo if Kobo sync is enabled.

**Keeping the plugin updated.** KOReader's [Updates Manager](https://github.com/advokatb/updatesmanager.koplugin) 
and [appstore.koplugin](https://github.com/kaz-utashiro/appstore.koplugin) can both update the plugin in 
place. Point either at the plugin's own repository, [`new-usemame/cwngsync.koplugin`](https://github.com/new-usemame/cwngsync.koplugin/releases)
— not at this one. The plugin publishes a release only when the plugin itself changes, and its version 
is the server version it last changed in, so it can legitimately sit behind your server version; that alone 
doesn't mean anything is wrong. With the plugin repository configured, a check that reports no new release 
means the plugin stream has nothing newer.

If your update manager is still pointed at this repository, switch it. That setup keeps working — a release 
that changes the plugin attaches the plugin download — but the plugin only appears on those releases, 
which is easy to misread as "no update available". The download on `/kosync` always serves the plugin 
bundled with your running server if you would rather update by hand.

**Matching filenames across devices (OPDS downloads).** If you download books to KOReader over OPDS and sync progress 
by filename across several e-readers, turn on **Use server filenames** in KOReader's OPDS catalog settings 
(the checkbox when you add or edit the catalog). By default KOReader names a downloaded file `Author - Title.epub` 
from the catalog entry, which differs from the on-disk library name `Title - Author.epub` and forces a manual rename. 
CWA already sends the library name in the download's `Content-Disposition` header; with **Use server filenames** on, 
KOReader uses that name, so the file matches your library and your other devices without renaming.

---

## Troubleshooting

### "Cover-file is not a valid image file, or could not be stored"

Fixed in v4.0.13 and later. If you're still seeing it after upgrading, you probably have `root:root`-owned book directories from a pre-fix install.

### "Generate Kobo Auth Token" returns a blank page

Fixed in v4.0.14 and later. Upgrade the image.

### "Database is locked" / app frozen

If your library is on a network share, set `NETWORK_SHARE_MODE=true` (see above). On local disk, this usually means a 
previous container shutdown was unclean: restart the systemd services.

### Session Protection warnings, forced re-login on every page

Set `TRUSTED_PROXY_COUNT` to match your proxy depth. See [Reverse proxy](#reverse-proxy--cloudflare-tunnel).

### Books in `/srv/calibre-ingest` aren't picked up

Three common causes:

1. Files owned by root. Make sure ingest files are owned by your `PUID:PGID` user.
2. Watcher missed them. Click the **Refresh Library** button on the navbar; it does a one-shot scan.(Untested in current version)
3. Format isn't allowed. Check Admin → CWA Settings → Ingest for your allowed formats.

### Default login isn't working

The defaults are `admin` / `admin123` (lowercase). If you've already changed the password and forgotten it: stop the 
container, delete `config/app.db`, and restart. This resets the database. User accounts are lost; the library itself 
is untouched. Unfortunately it also means you're going to have to inject your library location into app.db again. 

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

## Translations are occasionally merged from upstream, but there's a lot of upstream to dig through to keep from breaking anything here)

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

---

## How AI is used

The codebase itself is Calibre-Web and Calibre-Web-Automated. — written over many years by their
human maintainers and contributors. Calibre-Web-NextGen is largely produced by an AI assistant working from a written 
brief, with human review gates.

This fork shifts primary focus from Docker to being bare-metal capable. Many of the changes to this 
code are first written by hand, and then run through AI to find bugs, refine operation, and just generally make it 
prettier than I can. Some code is written primarily by AI, then heavily reviewed and tested, and modified as necessary.
I am slowly trying to extricate the Docker-related files to clean things up.

I'm one woman, I can't do everything, I'm not even a real developer! I just learn things quickly. I don't write code 
very quickly though. 

**The shipped application itself contains no AI:** no model dependency, no inference call, no
telemetry, and your library is not sent anywhere.


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
