# CoverBrand — vendor concept videos for the container cover

Upload the maker's concept video (or GIF) once, mark the cover panel, import your vendor list,
and the app prints each vendor's **name, logo and contact details onto the moving cover**,
then gives you **one video + one share link per vendor**.

## How it works (4 steps)
1. **Upload the video** – the maker's MP4 / MOV / WEBM / GIF (up to 2 minutes).
2. **Mark the cover** – drag the 4 orange dots onto the cover area where the name should print and click
   **Save cover position**; the app follows the cover through the whole video (green box).
   *More options* has manual/static modes and extra anchor frames if the print slips.
3. **Add vendors** – drop Excel / CSV / PDF / Outlook .msg/.eml / logo images, or paste a list. Every cell is editable.
4. **Make & share** – one click per batch: the vendor's name, logo and contact are printed on the cover, then
   Shotstack designs the finished video (intro, captions, end card, music) in 16:9, 9:16 and 1:1.
   Each vendor gets a **WhatsApp** button (opens WhatsApp with their number and the link pre-filled),
   **Copy link**, the public page, and downloads for every size. *Download all (ZIP)* gets everything.

*Customise the look (optional)* holds the print style (font, colours, plate, realism) and the video design
(texts, colours, music, sizes, free layout previews).

## Video design (Shotstack)
CoverBrand tracks the cover and prints the vendor branding itself; Shotstack then turns that clip into a
finished, designed video from the **Corporate reveal** preset:

1. **Intro** – "Prepared for" + vendor name animation, headline, vendor logo on a white card (monogram if no logo)
2. **Cover clip** – the branded cover video with a lower-third (name + caption) and logo bug;
   vertical/square versions use a blurred fill behind the clip
3. **End card** – logo, name, contact details, call-to-action button, optional footer line
4. Royalty-free music (Unminus licence: commercial use, no attribution), fades in/out

Sizes: 16:9 (1920×1080), 9:16 (1080×1920), 1:1 (1080×1080). Fonts: Montserrat (OFL) bundled as CB Display/Semi/Text.
Step 6 shows free local layout previews; *Auto-make* sends every newly rendered vendor to Shotstack.
Finished files are stored in MongoDB, shown on the vendor's share page (with per-size downloads) and included in the ZIP.

| Variable | Value |
|---|---|
| `SHOTSTACK_API_KEY` | Key from the Shotstack dashboard (sandbox and production keys differ) |
| `SHOTSTACK_ENV` | `stage` (default – free sandbox, watermarked) or `v1` (production, clean output, uses credits) |

Shotstack downloads the clip, logo and fonts from your app's public URLs (`/media/...`, `/assets/fonts/...`),
so `PUBLIC_URL` must be the public Railway address. Cost: ~12 s of video per size per vendor.

## Run locally
```bash
pip install -r requirements.txt     # needs ffmpeg on PATH
uvicorn app.main:app --port 8000
# open http://localhost:8000
```
Or `docker build -t coverbrand . && docker run -p 8000:8000 -v $PWD/data:/data coverbrand`

## Deploy on Railway (with MongoDB)
1. Push this repo → *New Project → Deploy from GitHub repo* (Dockerfile is picked up).
2. Add a **MongoDB** service in the same project (*+ Create → Database → MongoDB*).
3. In the **Cover-Brand** service → *Variables*:
   | Variable | Value |
   |---|---|
   | `MONGO_URL` | `${{MongoDB.MONGO_URL}}` (reference to the MongoDB service) |
   | `APP_PASSWORD` | Password for the team UI (browser login prompt; any username). Share links stay public. |
   | `PUBLIC_URL` | Your public domain, e.g. `https://cover-brand-production.up.railway.app` (no trailing `/`) |
   | `MONGO_DB` | Optional database name (default `coverbrand`) |
   | `RENDER_WORKERS` | Optional, parallel render jobs (default 2) |
4. *Settings → Networking → Generate Domain*, then put that address in `PUBLIC_URL`.
5. Open `https://<your-domain>/health` – it should show `"storage": "mongodb"`.

### What is stored where
- MongoDB collections: `projects` (video info, cover corners, vendors, style, render status),
  `tracks` (cover position per frame), `shares` (public link tokens)
- MongoDB GridFS (`files` bucket): the prepared concept video, vendor logos, rendered MP4/GIF and thumbnails
- The Cover-Brand service needs **no volume**: `/tmp/coverbrand` is only a working cache and is
  refilled from MongoDB automatically after a redeploy.
- Videos count towards the MongoDB volume size – delete old projects you no longer need
  (*Delete project* removes their files from MongoDB too).
- If the app restarts during tracking/rendering, those jobs are marked *Interrupted* – click Re-render.

Without `MONGO_URL` the app falls back to plain files under `DATA_DIR` (handy for local runs).

## Good to know
- Rendering is CPU work: a 10 s 1080p clip takes roughly 5–15 s per vendor on a normal instance.
- The print sits on top of the cover. If something passes **in front of** the cover (a hand, crane hook),
  the print will show over it — pick concept clips where the cover stays unobstructed, or use Manual mode.
- Tracking needs some texture on the cover (corrugation, rivets, edges). A perfectly plain, glossy
  CGI panel may track better with Manual mode (2–4 saved frames).
- Share links are unguessable tokens; anyone with the link can view that one vendor's video only.
- Jobs run inside the single web process, so keep `--workers 1`.

## Code map
- `app/main.py` – API, auth, jobs, share pages
- `app/tracking.py` – cover tracking (SIFT homography + optical-flow fallback, keyframe anchors)
- `app/decal.py` – builds the vendor print (logo + name + contact)
- `app/finishing.py` – Shotstack templates (layout boxes → Shotstack JSON + local preview), submit/poll/download
- `app/render.py` – perspective warp + lighting blend onto each frame, FFmpeg encoding, GIF output
- `app/vendors.py` – Excel / CSV / PDF / email / text parsing, logo matching
- `app/static/` – web UI and the public share page
